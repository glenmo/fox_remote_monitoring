#!/usr/bin/env python3
"""
Fox ESS H3 Pro — Modbus TCP Reader
==================================
Polls a FoxESS H3 Pro hybrid inverter via Modbus TCP and exposes the decoded
values to a Flask front-end.

Protocol notes (from "FOX Inverter Modbus definition V1.05.03.00"):
  * Modbus-TCP, default port 502
  * Function code 0x03 (Read Holding Registers) — *not* 0x04
  * Most slave IDs are 247 by default for H3 Pro; configurable
  * Live PV / inverter / battery / meter / EPS / load registers live at 39000+
  * Energy totals at 39600+
  * BMS1 at 37600+
  * Meter1 / CT1 at 38800+
  * Gain = scale divisor (raw / gain = engineering value)

Author: Solarquip (Fox ESS Monitor project)
"""

import logging
import struct
import threading
import time
from collections import deque
from datetime import datetime

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusIOException

log = logging.getLogger("fox_reader")

# ---------------------------------------------------------------------------
# Register map — Fox ESS H3 Pro (function code 0x03)
#
# Tuple format: (start_register, count, name, data_type, unit, gain, description)
#   * For U32 / I32 types, count = 2 (high word first, low word second)
#   * For U16 / I16 types, count = 1
#   * "gain" is the divisor: engineering value = raw / gain
# ---------------------------------------------------------------------------

# 1. Inverter live data — Table 3-5 (39000-39423)
INVERTER_REGISTERS = [
    # Identity / version
    (39000, 2, "protocol_version",    "U32",  "",    1,    "Modbus protocol version"),
    (39050, 1, "model_id",            "U16",  "",    1,    "Inverter model ID"),
    (39051, 1, "string_count",        "U16",  "",    1,    "Number of PV strings"),
    (39052, 1, "mppt_count",          "U16",  "",    1,    "Number of MPPTs"),
    (39053, 2, "rated_power",         "I32",  "kW",  1000, "Rated power (Pn)"),
    (39055, 2, "max_active_power",    "I32",  "kW",  1000, "Maximum active power (Pmax)"),
    (39057, 2, "max_apparent_power",  "I32",  "kVA", 1000, "Maximum apparent power (Smax)"),

    # Status / alarms (bitfields)
    (39063, 1, "status_1",            "U16",  "",    1,    "Status1 bitfield (b0=Standby b2=Op b6=Fault)"),
    (39065, 1, "status_3",            "U16",  "",    1,    "Status3 bitfield (b0=Off-grid)"),
    (39067, 1, "alarm_1",             "U16",  "",    1,    "Alarm1 bitfield"),
    (39068, 1, "alarm_2",             "U16",  "",    1,    "Alarm2 bitfield"),
    (39069, 1, "alarm_3",             "U16",  "",    1,    "Alarm3 bitfield"),

    # PV strings (we expose 1-4; H3 Pro typically has 2-3 active)
    (39070, 1, "pv1_voltage",         "I16",  "V",   10,   "PV1 Voltage"),
    (39071, 1, "pv1_current",         "I16",  "A",   100,  "PV1 Current"),
    (39072, 1, "pv2_voltage",         "I16",  "V",   10,   "PV2 Voltage"),
    (39073, 1, "pv2_current",         "I16",  "A",   100,  "PV2 Current"),
    (39074, 1, "pv3_voltage",         "I16",  "V",   10,   "PV3 Voltage"),
    (39075, 1, "pv3_current",         "I16",  "A",   100,  "PV3 Current"),
    (39076, 1, "pv4_voltage",         "I16",  "V",   10,   "PV4 Voltage"),
    (39077, 1, "pv4_current",         "I16",  "A",   100,  "PV4 Current"),

    # Total PV power (kW @ gain 1000 == W resolution)
    (39118, 2, "pv_total_power",      "I32",  "kW",  1000, "Total PV input power"),

    # AC grid side
    (39123, 1, "grid_voltage_r",      "I16",  "V",   10,   "Grid R-phase voltage"),
    (39124, 1, "grid_voltage_s",      "I16",  "V",   10,   "Grid S-phase voltage"),
    (39125, 1, "grid_voltage_t",      "I16",  "V",   10,   "Grid T-phase voltage"),
    (39126, 2, "inv_current_r",       "I32",  "A",   1000, "Inverter R-phase current"),
    (39128, 2, "inv_current_s",       "I32",  "A",   1000, "Inverter S-phase current"),
    (39130, 2, "inv_current_t",       "I32",  "A",   1000, "Inverter T-phase current"),
    (39134, 2, "active_power",        "I32",  "kW",  1000, "Active power (kW)"),
    (39136, 2, "reactive_power",      "I32",  "kVar",1000, "Reactive power"),
    (39138, 1, "power_factor",        "I16",  "",    1000, "Power factor"),
    (39139, 1, "grid_frequency",      "I16",  "Hz",  100,  "Grid frequency"),
    (39141, 1, "internal_temp",       "I16",  "°C",  10,   "Inverter internal temperature"),

    # Energy totals (also shown here for convenience)
    (39149, 2, "total_energy",        "U32",  "kWh", 100,  "Cumulative power generation"),
    (39151, 2, "today_energy",        "U32",  "kWh", 100,  "Power generation today"),

    # Battery & meter combined
    (39162, 2, "battery_charge_power","I32",  "W",   1,    "Battery charge/discharge power (>0 charging, <0 discharging)"),
    (39168, 2, "meter_active_power",  "I32",  "W",   1,    "Meter active power (>0 export, <0 import)"),

    # EPS (off-grid backup output)
    (39201, 1, "eps_voltage_r",       "U16",  "V",   10,   "EPS R-phase voltage"),
    (39202, 1, "eps_voltage_s",       "U16",  "V",   10,   "EPS S-phase voltage"),
    (39203, 1, "eps_voltage_t",       "U16",  "V",   10,   "EPS T-phase voltage"),
    (39204, 2, "eps_current_r",       "I32",  "A",   1000, "EPS R-phase current"),
    (39206, 2, "eps_current_s",       "I32",  "A",   1000, "EPS S-phase current"),
    (39208, 2, "eps_current_t",       "I32",  "A",   1000, "EPS T-phase current"),
    (39210, 2, "eps_power_r",         "I32",  "W",   1,    "EPS R-phase power"),
    (39212, 2, "eps_power_s",         "I32",  "W",   1,    "EPS S-phase power"),
    (39214, 2, "eps_power_t",         "I32",  "W",   1,    "EPS T-phase power"),
    (39216, 2, "eps_power_total",     "I32",  "W",   1,    "EPS combined power"),
    (39218, 1, "eps_frequency",       "I16",  "Hz",  100,  "EPS frequency"),

    # Load
    (39219, 2, "load_power_r",        "I32",  "W",   1,    "Load R-phase power"),
    (39221, 2, "load_power_s",        "I32",  "W",   1,    "Load S-phase power"),
    (39223, 2, "load_power_t",        "I32",  "W",   1,    "Load T-phase power"),
    (39225, 2, "load_power_total",    "I32",  "W",   1,    "Load combined power"),

    # Battery 1 & 2
    (39227, 1, "battery1_voltage",    "I16",  "V",   10,   "Battery1 voltage"),
    (39228, 2, "battery1_current",    "I32",  "A",   1000, "Battery1 current"),
    (39230, 2, "battery1_power",      "I32",  "W",   1,    "Battery1 power"),
    (39232, 1, "battery2_voltage",    "I16",  "V",   10,   "Battery2 voltage"),
    (39233, 2, "battery2_current",    "I32",  "A",   1000, "Battery2 current"),
    (39235, 2, "battery2_power",      "I32",  "W",   1,    "Battery2 power"),
    (39237, 2, "battery_power_total", "I32",  "W",   1,    "Battery combined power"),

    # INV phase active / reactive / apparent
    (39248, 2, "inv_active_r",        "I32",  "W",   1,    "INV R-phase active power"),
    (39250, 2, "inv_active_s",        "I32",  "W",   1,    "INV S-phase active power"),
    (39252, 2, "inv_active_t",        "I32",  "W",   1,    "INV T-phase active power"),
    (39256, 2, "inv_reactive_r",      "I32",  "Var", 1,    "INV R-phase reactive power"),
    (39258, 2, "inv_reactive_s",      "I32",  "Var", 1,    "INV S-phase reactive power"),
    (39260, 2, "inv_reactive_t",      "I32",  "Var", 1,    "INV T-phase reactive power"),
    (39264, 2, "inv_apparent_r",      "I32",  "VA",  1,    "INV R-phase apparent power"),
    (39266, 2, "inv_apparent_s",      "I32",  "VA",  1,    "INV S-phase apparent power"),
    (39268, 2, "inv_apparent_t",      "I32",  "VA",  1,    "INV T-phase apparent power"),
    (39270, 2, "inv_apparent_total",  "I32",  "VA",  1,    "INV combined apparent power"),
    (39272, 1, "inv_frequency_r",     "I16",  "Hz",  100,  "INV R-phase frequency"),
    (39273, 1, "inv_frequency_s",     "I16",  "Hz",  100,  "INV S-phase frequency"),
    (39274, 1, "inv_frequency_t",     "I16",  "Hz",  100,  "INV T-phase frequency"),

    # Available import/export window
    (39275, 2, "avail_import_power",  "I32",  "W",   1,    "Available import power"),
    (39277, 2, "avail_export_power",  "I32",  "W",   1,    "Available export power"),

    # Per-string PV power (1-4)
    (39279, 2, "pv1_power",           "I32",  "W",   1,    "PV1 Power"),
    (39281, 2, "pv2_power",           "I32",  "W",   1,    "PV2 Power"),
    (39283, 2, "pv3_power",           "I32",  "W",   1,    "PV3 Power"),
    (39285, 2, "pv4_power",           "I32",  "W",   1,    "PV4 Power"),

    # MPPT 1-3
    (39327, 1, "mppt1_voltage",       "I16",  "V",   10,   "MPPT1 voltage"),
    (39328, 1, "mppt1_current",       "I16",  "A",   100,  "MPPT1 current"),
    (39329, 2, "mppt1_power",         "I32",  "W",   1,    "MPPT1 power"),
    (39331, 1, "mppt2_voltage",       "I16",  "V",   10,   "MPPT2 voltage"),
    (39332, 1, "mppt2_current",       "I16",  "A",   100,  "MPPT2 current"),
    (39333, 2, "mppt2_power",         "I32",  "W",   1,    "MPPT2 power"),
    (39335, 1, "mppt3_voltage",       "I16",  "V",   10,   "MPPT3 voltage"),
    (39336, 1, "mppt3_current",       "I16",  "A",   100,  "MPPT3 current"),
    (39337, 2, "mppt3_power",         "I32",  "W",   1,    "MPPT3 power"),

    # System SoC
    (39423, 1, "system_soc",          "U16",  "%",   1,    "System State of Charge"),
]

# 2. Energy totals — Table 3-6 (39600-39631)
ENERGY_REGISTERS = [
    (39601, 2, "pv_total_kwh",         "U32", "kWh", 100, "PV total energy (lifetime)"),
    (39603, 2, "pv_today_kwh",         "U32", "kWh", 100, "PV total energy today"),
    (39605, 2, "charge_total_kwh",     "U32", "kWh", 100, "Battery charge total energy"),
    (39607, 2, "charge_today_kwh",     "U32", "kWh", 100, "Battery charge today"),
    (39609, 2, "discharge_total_kwh",  "U32", "kWh", 100, "Battery discharge total"),
    (39611, 2, "discharge_today_kwh",  "U32", "kWh", 100, "Battery discharge today"),
    (39613, 2, "feedin_total_kwh",     "U32", "kWh", 100, "Grid feed-in total"),
    (39615, 2, "feedin_today_kwh",     "U32", "kWh", 100, "Grid feed-in today"),
    (39617, 2, "import_total_kwh",     "U32", "kWh", 100, "Grid import total"),
    (39619, 2, "import_today_kwh",     "U32", "kWh", 100, "Grid import today"),
    (39621, 2, "output_total_kwh",     "U32", "kWh", 100, "Output total"),
    (39623, 2, "output_today_kwh",     "U32", "kWh", 100, "Output today"),
    (39625, 2, "input_total_kwh",      "U32", "kWh", 100, "Input total"),
    (39627, 2, "input_today_kwh",      "U32", "kWh", 100, "Input today"),
    (39629, 2, "load_total_kwh",       "U32", "kWh", 100, "Load total"),
    (39631, 2, "load_today_kwh",       "U32", "kWh", 100, "Load today"),
]

# 3. BMS1 — Table 3-3 (37609-37635) — populated only if a Fox battery is wired
BMS_REGISTERS = [
    (37609, 1, "bms1_voltage",        "U16", "V",    10,  "BMS1 voltage"),
    (37610, 1, "bms1_current",        "I16", "A",    10,  "BMS1 current (signed)"),
    (37611, 1, "bms1_ambient_temp",   "I16", "°C",   10,  "BMS1 ambient temperature"),
    (37612, 1, "bms1_soc",            "U16", "%",    1,   "BMS1 SoC"),
    (37617, 1, "bms1_max_temp",       "I16", "°C",   10,  "BMS1 max cell temperature"),
    (37618, 1, "bms1_min_temp",       "I16", "°C",   10,  "BMS1 min cell temperature"),
    (37619, 1, "bms1_max_cell_mv",    "U16", "mV",   1,   "BMS1 max cell voltage"),
    (37620, 1, "bms1_min_cell_mv",    "U16", "mV",   1,   "BMS1 min cell voltage"),
    (37624, 1, "bms1_soh",            "U16", "%",    1,   "BMS1 State of Health"),
    (37632, 1, "bms1_remain_wh",      "U16", "Wh",   1,   "BMS1 remaining energy (raw, gain 0.1 multiplied)"),
]

# 4. Meter1 / CT1 — Table 3-4 (38800-38846) — populated only if a meter is wired
METER_REGISTERS = [
    (38801, 1, "meter1_connected",    "U16", "",     1,   "Meter1 connection state (0/1)"),
    (38802, 2, "meter1_voltage_r",    "I32", "V",    10,  "Meter1 R-phase voltage"),
    (38804, 2, "meter1_voltage_s",    "I32", "V",    10,  "Meter1 S-phase voltage"),
    (38806, 2, "meter1_voltage_t",    "I32", "V",    10,  "Meter1 T-phase voltage"),
    (38808, 2, "meter1_current_r",    "I32", "A",    1000,"Meter1 R-phase current"),
    (38810, 2, "meter1_current_s",    "I32", "A",    1000,"Meter1 S-phase current"),
    (38812, 2, "meter1_current_t",    "I32", "A",    1000,"Meter1 T-phase current"),
    (38814, 2, "meter1_power_total",  "I32", "W",    10,  "Meter1 combined active power"),
    (38830, 2, "meter1_va_total",     "I32", "VA",   10,  "Meter1 combined apparent power"),
    (38838, 2, "meter1_pf_total",     "I32", "",     1000,"Meter1 combined power factor"),
    (38846, 2, "meter1_freq",         "I32", "Hz",   100, "Meter1 frequency"),
]

# Combined map — read in this order
ALL_REGISTERS = INVERTER_REGISTERS + ENERGY_REGISTERS + BMS_REGISTERS + METER_REGISTERS

# ---------------------------------------------------------------------------
# Lookup tables (decode bitfields and enums into human-readable strings)
# ---------------------------------------------------------------------------

# Status1 bit definitions — Table 3-5 footnotes
STATUS_1_BITS = {
    0: "Standby",
    2: "Operation",
    6: "Fault",
}

# Work-mode codes (register 49203) for context if ever read
WORK_MODES = {
    1: "Self Use",
    2: "Feed-in Priority",
    3: "BackUp",
    4: "Peak Shaving",
    6: "Force Charge",
    7: "Force Discharge",
}

# Alarm1 bit map — Table 4-1
ALARM_1_BITS = {
    0:  "PV input voltage high",
    1:  "DC arc fault",
    2:  "PV string reverse polarity",
    7:  "Grid power outage",
    8:  "Grid voltage abnormal",
    11: "Grid frequency abnormal",
    14: "Output overcurrent",
    15: "Output current DC component too high",
}

ALARM_2_BITS = {
    0:  "Residual current abnormal",
    1:  "System grounding abnormal",
    2:  "Insulation resistance low",
    3:  "Temperature too high",
    9:  "Energy storage abnormality",
    10: "Islanding detected",
    14: "Off-grid output overload",
}

ALARM_3_BITS = {
    3:  "External fan abnormal",
    4:  "Energy storage reverse connection",
    9:  "Meter Lost",
    10: "BMS Lost",
}


def _decode_alarm(bits, value):
    """Return a list of human-readable alarm names for a 16-bit alarm word."""
    if value is None:
        return []
    out = []
    for bit, name in bits.items():
        if value & (1 << bit):
            out.append(name)
    return out


def _decode_status_1(value):
    """Return a primary status string from the Status1 bitfield."""
    if value is None:
        return "Unknown"
    if value & (1 << 6):
        return "Fault"
    if value & (1 << 2):
        return "Operation"
    if value & (1 << 0):
        return "Standby"
    return f"Unknown (0x{value:04X})"


# ---------------------------------------------------------------------------
# Modbus TCP reader class
# ---------------------------------------------------------------------------
class FoxModbusReader:
    """Polls a FoxESS H3 Pro inverter via Modbus TCP (function 0x03)."""

    def __init__(self, host, port=502, slave_id=247, poll_interval=10):
        self.host = host
        self.port = port
        self.slave_id = slave_id
        self.poll_interval = poll_interval

        self.client = None
        self.connected = False
        self.last_read_time = None
        self.read_errors = 0
        self.total_reads = 0

        # Current decoded values
        self.data = {}
        self.raw_data = {}

        # Rolling history for charts (1-min average, 24h = 1440 points)
        self.history_max = 1440
        self.history = {
            "timestamps": deque(maxlen=self.history_max),
            "system_soc": deque(maxlen=self.history_max),
            "pv_total_power_w": deque(maxlen=self.history_max),
            "active_power_w": deque(maxlen=self.history_max),
            "battery_power_w": deque(maxlen=self.history_max),
            "load_power_w": deque(maxlen=self.history_max),
            "meter_active_power_w": deque(maxlen=self.history_max),
            "grid_frequency": deque(maxlen=self.history_max),
        }
        self._last_history_minute = -1

        self.lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None

    # -- connection management ---------------------------------------------
    def connect(self):
        try:
            self.client = ModbusTcpClient(host=self.host, port=self.port, timeout=5)
            self.connected = self.client.connect()
            if self.connected:
                log.info(f"Fox: Connected to {self.host}:{self.port} (slave {self.slave_id})")
            else:
                log.warning(f"Fox: Failed to connect to {self.host}:{self.port}")
        except Exception as e:
            log.error(f"Fox: Connection error: {e}")
            self.connected = False

    def disconnect(self):
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
            self.connected = False
            log.info("Fox: Disconnected")

    # -- low level read helpers --------------------------------------------
    def _read_registers(self, address, count):
        """Read `count` holding registers starting at `address` (function 0x03)."""
        if not self.connected:
            self.connect()
            if not self.connected:
                return None
        try:
            # pymodbus 3.6+ uses device_id; older 3.0-3.5 used 'slave'.
            try:
                result = self.client.read_holding_registers(
                    address=address, count=count, device_id=self.slave_id
                )
            except TypeError:
                # Fallback for older pymodbus
                result = self.client.read_holding_registers(
                    address=address, count=count, slave=self.slave_id
                )
            if isinstance(result, ModbusIOException) or result.isError():
                log.warning(f"Fox: Modbus error at register {address}: {result}")
                return None
            return result.registers
        except Exception as e:
            log.error(f"Fox: Exception reading register {address}: {e}")
            self.connected = False
            return None

    @staticmethod
    def _decode(registers, dtype, gain):
        """Decode raw register words into engineering value."""
        if registers is None:
            return None
        try:
            if dtype == "U16":
                raw = registers[0]
                return raw / gain if gain != 1 else raw

            if dtype == "I16":
                raw = registers[0]
                if raw >= 0x8000:
                    raw -= 0x10000
                return raw / gain if gain != 1 else raw

            if dtype == "U32":
                if len(registers) < 2:
                    return None
                raw = (registers[0] << 16) | registers[1]
                return raw / gain if gain != 1 else raw

            if dtype == "I32":
                if len(registers) < 2:
                    return None
                raw = (registers[0] << 16) | registers[1]
                if raw >= 0x80000000:
                    raw -= 0x100000000
                return raw / gain if gain != 1 else raw

            if dtype == "STR":
                # Each register is 2 ASCII bytes, big-endian
                raw = b"".join(struct.pack(">H", r) for r in registers)
                return raw.split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()
        except Exception as e:
            log.debug(f"Fox: decode error ({dtype}): {e}")
            return None
        return None

    # -- main poll cycle ---------------------------------------------------
    def poll_once(self):
        new_data = {}
        new_raw = {}
        success = True

        # Group reads by contiguous-ish blocks for efficiency. We keep the
        # individual approach for simplicity & robustness — the H3 Pro polling
        # rate is slow (5-30s) so dozens of small reads per cycle is fine.
        for entry in ALL_REGISTERS:
            addr, count, name, dtype, unit, gain, _desc = entry
            registers = self._read_registers(addr, count)
            if registers is None:
                success = False
                continue
            value = self._decode(registers, dtype, gain)
            if value is None:
                success = False
                continue
            new_data[name] = value
            new_raw[name] = registers[0] if count == 1 else list(registers)
            # Tiny sleep — Fox inverters don't require a long inter-frame gap
            # over TCP, but RS485-bridged gateways often do.
            time.sleep(0.02)

        if not new_data:
            self.read_errors += 1
            return

        # Derived / decoded fields
        new_data["status_1_str"]   = _decode_status_1(int(new_data.get("status_1", 0)))
        new_data["off_grid"]       = bool(int(new_data.get("status_3", 0)) & 0x1)
        new_data["alarms_1"]       = _decode_alarm(ALARM_1_BITS, int(new_data.get("alarm_1", 0) or 0))
        new_data["alarms_2"]       = _decode_alarm(ALARM_2_BITS, int(new_data.get("alarm_2", 0) or 0))
        new_data["alarms_3"]       = _decode_alarm(ALARM_3_BITS, int(new_data.get("alarm_3", 0) or 0))
        new_data["alarms_active"]  = (
            new_data["alarms_1"] + new_data["alarms_2"] + new_data["alarms_3"]
        )

        # Provide watt-resolution copies of the kW fields for charts
        new_data["pv_total_power_w"] = round((new_data.get("pv_total_power") or 0) * 1000, 1)
        new_data["active_power_w"]   = round((new_data.get("active_power")   or 0) * 1000, 1)
        new_data["battery_power_w"]  = new_data.get("battery_power_total")  or 0
        new_data["load_power_w"]     = new_data.get("load_power_total")     or 0
        new_data["meter_active_power_w"] = new_data.get("meter_active_power") or 0

        # Fox sign convention: register 39162 >0 charging, <0 discharging.
        # We surface battery_charge_power as-is so the UI can render direction.

        # Metadata
        now = datetime.now()
        new_data["_timestamp"] = now.isoformat()
        new_data["_read_ok"]   = success
        new_data["_slave_id"]  = self.slave_id

        self.total_reads += 1
        if not success:
            self.read_errors += 1

        with self.lock:
            self.data = new_data
            self.raw_data = new_raw
            self.last_read_time = now

            # Append a sample once per minute
            current_minute = now.minute
            if current_minute != self._last_history_minute:
                self._last_history_minute = current_minute
                self.history["timestamps"].append(now.strftime("%H:%M"))
                for key in ["system_soc", "pv_total_power_w", "active_power_w",
                            "battery_power_w", "load_power_w",
                            "meter_active_power_w", "grid_frequency"]:
                    self.history[key].append(new_data.get(key, 0))

    def _poll_loop(self):
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception as e:
                log.error(f"Fox: poll error: {e}")
            self._stop_event.wait(self.poll_interval)

    # -- public API --------------------------------------------------------
    def start(self):
        self.connect()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        log.info(f"Fox: polling started (every {self.poll_interval}s)")

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
        self.disconnect()

    def get_data(self):
        with self.lock:
            return dict(self.data)

    def get_history(self):
        with self.lock:
            return {k: list(v) for k, v in self.history.items()}

    def get_status(self):
        return {
            "connected":     self.connected,
            "host":          self.host,
            "port":          self.port,
            "slave_id":      self.slave_id,
            "poll_interval": self.poll_interval,
            "total_reads":   self.total_reads,
            "read_errors":   self.read_errors,
            "last_read":     self.last_read_time.isoformat() if self.last_read_time else None,
        }
