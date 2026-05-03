#!/usr/bin/env python3
"""
Solis S6-EH3P (50kW Hybrid) — Modbus TCP Reader
================================================
Polls a Solis hybrid inverter via Modbus TCP and exposes decoded values
to the Flask front-end.

Protocol notes:
  * Modbus-TCP, default port 502
  * Function code 0x04 (Read Input Registers) — Solis uses input regs,
    NOT holding regs (which is the opposite of Fox).
  * Solis spec recommends max 50 regs per frame with >300ms between
    frames — this reader respects that with bulk reads + inter-block
    sleep.
  * Slave ID typically 1 on a stock Solis configuration.

Mirrors the architecture of fox_reader.py (auto-grouped bulk reads via
_build_blocks) so both readers have the same shape.

Author: Solarquip (Fox + Solis combined dashboard)
"""

import logging
import threading
import time
from collections import deque
from datetime import datetime

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusIOException

log = logging.getLogger("solis_reader")

# ---------------------------------------------------------------------------
# Register map — Solis S6-EH3P (function code 0x04, input registers)
#
# Tuple format: (start_register, count, name, data_type, unit, gain, description)
# "gain" is the divisor: engineering value = raw / gain
# ---------------------------------------------------------------------------
SOLIS_REGISTERS = [
    # Inverter identity / time
    (33000, 1, "model_no",            "U16", "",    1,    "Inverter model number"),
    (33022, 1, "year",                "U16", "",    1,    "Inverter clock year"),
    (33023, 1, "month",               "U16", "",    1,    "Inverter clock month"),
    (33024, 1, "day",                 "U16", "",    1,    "Inverter clock day"),
    (33025, 1, "hour",                "U16", "",    1,    "Inverter clock hour"),
    (33026, 1, "minute",              "U16", "",    1,    "Inverter clock minute"),

    # PV (DC) inputs — strings 1-4
    (33049, 1, "pv1_voltage",         "U16", "V",   10,   "PV String 1 Voltage"),
    (33050, 1, "pv1_current",         "U16", "A",   10,   "PV String 1 Current"),
    (33051, 1, "pv2_voltage",         "U16", "V",   10,   "PV String 2 Voltage"),
    (33052, 1, "pv2_current",         "U16", "A",   10,   "PV String 2 Current"),
    (33053, 1, "pv3_voltage",         "U16", "V",   10,   "PV String 3 Voltage"),
    (33054, 1, "pv3_current",         "U16", "A",   10,   "PV String 3 Current"),
    (33055, 1, "pv4_voltage",         "U16", "V",   10,   "PV String 4 Voltage"),
    (33056, 1, "pv4_current",         "U16", "A",   10,   "PV String 4 Current"),
    (33057, 2, "pv_total_power",      "U32", "W",   1,    "Total DC (PV) Power"),

    # PV energy
    (33035, 1, "pv_today_energy",     "U16", "kWh", 10,   "PV Generation Today"),
    (33029, 2, "pv_total_energy",     "U32", "kWh", 1,    "PV Lifetime Generation"),

    # AC grid side
    (33073, 1, "grid_voltage_ab",     "U16", "V",   10,   "Grid Voltage A-B (Phase A)"),
    (33074, 1, "grid_voltage_bc",     "U16", "V",   10,   "Grid Voltage B-C"),
    (33075, 1, "grid_voltage_ca",     "U16", "V",   10,   "Grid Voltage C-A"),
    (33076, 1, "grid_current_a",      "U16", "A",   10,   "Grid Current Phase A"),
    (33077, 1, "grid_current_b",      "U16", "A",   10,   "Grid Current Phase B"),
    (33078, 1, "grid_current_c",      "U16", "A",   10,   "Grid Current Phase C"),
    (33079, 2, "active_power",        "S32", "W",   1,    "Active Power (+ export / - import)"),
    (33081, 2, "reactive_power",      "S32", "Var", 1,    "Reactive Power"),
    (33083, 2, "apparent_power",      "S32", "VA",  1,    "Apparent Power"),
    (33094, 1, "grid_frequency",      "U16", "Hz",  100,  "Grid Frequency"),

    # Battery
    (33133, 1, "battery_voltage",     "U16", "V",   10,   "Battery Voltage"),
    (33134, 1, "battery_current",     "S16", "A",   10,   "Battery Current (signed)"),
    (33135, 1, "battery_current_dir", "U16", "",    1,    "Battery direction (0=charge, 1=discharge)"),
    (33139, 1, "battery_soc",         "U16", "%",   1,    "Battery State of Charge"),
    (33140, 1, "battery_soh",         "U16", "%",   1,    "Battery State of Health"),
    (33141, 1, "bms_battery_voltage", "U16", "V",   100,  "BMS Battery Voltage"),
    (33142, 1, "bms_battery_current", "S16", "A",   10,   "BMS Battery Current"),
    (33143, 1, "bms_charge_limit",    "U16", "A",   10,   "BMS Charge Current Limit"),
    (33144, 1, "bms_discharge_limit", "U16", "A",   10,   "BMS Discharge Current Limit"),

    # Temperatures
    (33093, 1, "inverter_temp",       "S16", "°C",  10,   "Inverter Module Temperature"),

    # Status
    (33091, 1, "working_mode",        "U16", "",    1,    "Standard Working Mode"),
    (33095, 1, "inverter_status",     "U16", "",    1,    "Inverter Current Status"),
    (33121, 1, "operating_status",    "U16", "",    1,    "Operating Status"),
    (33111, 1, "bms_status",          "U16", "",    1,    "Battery BMS Status"),

    # Fault codes
    (33116, 1, "fault_code_01",       "U16", "",    1,    "Fault Code 01"),
    (33117, 1, "fault_code_02",       "U16", "",    1,    "Fault Code 02"),
    (33118, 1, "fault_code_03",       "U16", "",    1,    "Fault Code 03"),
    (33119, 1, "fault_code_04",       "U16", "",    1,    "Fault Code 04"),

    # DC bus & backup
    (33071, 1, "dc_bus_voltage",      "U16", "V",   10,   "DC Bus Voltage"),
    (33137, 1, "backup_voltage",      "U16", "V",   10,   "Backup AC Voltage (Phase A)"),
    (33138, 1, "backup_current",      "U16", "A",   10,   "Backup AC Current (Phase A)"),
]

# Lookup tables for working mode + BMS status
WORKING_MODES = {
    0: "No response",
    1: "Volt-watt default",
    2: "Volt-var",
    3: "Fixed power factor",
    4: "Fix reactive power",
    5: "Power-PF",
    6: "Rule21 Volt-watt",
    12: "IEEE1547-2018 P-Q",
}

BMS_STATUS = {
    0: "Normal",
    1: "Comms Abnormal",
    2: "BMS Warning",
}


# ---------------------------------------------------------------------------
# Solis Modbus TCP reader (bulk-read, mirrors fox_reader pattern)
# ---------------------------------------------------------------------------
class SolisModbusReader:
    """Polls a Solis hybrid inverter via Modbus TCP (function 0x04)."""

    def __init__(self, host, port=502, slave_id=1, poll_interval=10):
        self.host = host
        self.port = port
        self.slave_id = slave_id
        self.poll_interval = poll_interval

        self.client = None
        self.connected = False
        self.last_read_time = None
        self.read_errors = 0
        self.total_reads = 0
        # Consecutive fully-failed poll cycles (no fields decoded). After
        # this many in a row we force a fresh TCP connection — this is the
        # classic Solis dongle "stuck socket" recovery path.
        self._consecutive_failures = 0
        self._FORCE_RECONNECT_AFTER = 3

        self.data = {}
        self.raw_data = {}

        # Rolling 24h history for charts
        self.history_max = 1440
        self.history = {
            "timestamps":      deque(maxlen=self.history_max),
            "battery_soc":     deque(maxlen=self.history_max),
            "pv_total_power":  deque(maxlen=self.history_max),
            "active_power":    deque(maxlen=self.history_max),
            "battery_power":   deque(maxlen=self.history_max),
            "battery_voltage": deque(maxlen=self.history_max),
            "grid_frequency":  deque(maxlen=self.history_max),
        }
        self._last_history_minute = -1

        self.lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None
        self._blocks = None

    # -- block planner -----------------------------------------------------
    @staticmethod
    def _build_blocks(registers, gap_tolerance=4, max_block=50):
        """Group register definitions into contiguous bulk-read blocks.

        Solis spec recommends max 50 registers per frame, so cap blocks
        at 50 (fox_reader uses 100 but the Fox dongle is more tolerant).
        """
        blocks = []
        sorted_regs = sorted(registers, key=lambda r: r[0])
        cur_start = None
        cur_end = None
        cur_entries = []

        for entry in sorted_regs:
            addr, count = entry[0], entry[1]
            end = addr + count - 1
            if cur_start is None:
                cur_start, cur_end, cur_entries = addr, end, [entry]
                continue
            gap = addr - (cur_end + 1)
            new_count = end - cur_start + 1
            if gap <= gap_tolerance and new_count <= max_block:
                cur_end = max(cur_end, end)
                cur_entries.append(entry)
            else:
                blocks.append({
                    "start": cur_start,
                    "count": cur_end - cur_start + 1,
                    "entries": cur_entries,
                })
                cur_start, cur_end, cur_entries = addr, end, [entry]
        if cur_start is not None:
            blocks.append({
                "start": cur_start,
                "count": cur_end - cur_start + 1,
                "entries": cur_entries,
            })
        return blocks

    # -- connection -------------------------------------------------------
    def connect(self):
        # Always start from a clean slate — pymodbus's internal state can
        # report connected=True over a half-dead socket, so dispose of any
        # previous client before opening a new one.
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                pass
            self.client = None
        try:
            self.client = ModbusTcpClient(host=self.host, port=self.port, timeout=5)
            self.connected = self.client.connect()
            if self.connected:
                log.info(f"Solis: Connected to {self.host}:{self.port} (slave {self.slave_id})")
            else:
                log.warning(f"Solis: Failed to connect to {self.host}:{self.port}")
        except Exception as e:
            log.error(f"Solis: Connection error: {e}")
            self.connected = False

    def disconnect(self):
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
            self.client = None
            self.connected = False
            log.info("Solis: Disconnected")

    def _force_reconnect(self, reason=""):
        """Drop the current TCP socket and force a fresh connect on next read.

        Solis WiFi/LAN dongles sometimes leave a socket half-open after
        the inverter sleeps for the night — pymodbus's client thinks it's
        still connected but every read returns an error. Burning the
        socket and reopening clears that.
        """
        if reason:
            log.info(f"Solis: forcing reconnect ({reason})")
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                pass
            self.client = None
        self.connected = False

    # -- low-level read ---------------------------------------------------
    def _read_input_registers(self, address, count):
        """Read `count` input registers starting at `address` (function 0x04)."""
        if not self.connected or self.client is None:
            self.connect()
            if not self.connected:
                return None
        try:
            try:
                result = self.client.read_input_registers(
                    address=address, count=count, device_id=self.slave_id
                )
            except TypeError:
                # Older pymodbus
                result = self.client.read_input_registers(
                    address=address, count=count, slave=self.slave_id
                )
            if isinstance(result, ModbusIOException) or result.isError():
                log.warning(f"Solis: Modbus error at register {address}: {result}")
                return None
            return result.registers
        except Exception as e:
            log.error(f"Solis: Exception reading register {address}: {e}")
            # Socket is almost certainly dead now — burn it so the next
            # poll cycle opens a fresh connection rather than reusing a
            # half-open TCP slot on the dongle.
            self._force_reconnect(reason=f"read exception: {e}")
            return None

    @staticmethod
    def _decode(registers, dtype, gain):
        if registers is None:
            return None
        try:
            if dtype == "U16":
                raw = registers[0]
                return raw / gain if gain != 1 else raw
            if dtype == "S16":
                raw = registers[0]
                if raw >= 0x8000:
                    raw -= 0x10000
                return raw / gain if gain != 1 else raw
            if dtype == "U32":
                if len(registers) < 2:
                    return None
                raw = (registers[0] << 16) | registers[1]
                return raw / gain if gain != 1 else raw
            if dtype == "S32":
                if len(registers) < 2:
                    return None
                raw = (registers[0] << 16) | registers[1]
                if raw >= 0x80000000:
                    raw -= 0x100000000
                return raw / gain if gain != 1 else raw
        except Exception as e:
            log.debug(f"Solis: decode error ({dtype}): {e}")
            return None
        return None

    # -- main poll cycle --------------------------------------------------
    def poll_once(self):
        new_data = {}
        new_raw = {}
        success = True

        if self._blocks is None:
            self._blocks = self._build_blocks(SOLIS_REGISTERS)
            for b in self._blocks:
                log.info(f"Solis: read plan — {b['start']} +{b['count']} regs ({len(b['entries'])} fields)")

        for blk in self._blocks:
            registers = self._read_input_registers(blk["start"], blk["count"])
            if registers is None or len(registers) < blk["count"]:
                success = False
                log.warning(f"Solis: bulk read failed at {blk['start']} (count {blk['count']})")
                # Solis spec says >300 ms between frames — give the bus room
                time.sleep(0.35)
                continue
            for entry in blk["entries"]:
                addr, count, name, dtype, _unit, gain, _desc = entry
                offset = addr - blk["start"]
                slice_ = registers[offset:offset + count]
                value = self._decode(slice_, dtype, gain)
                if value is None:
                    continue
                new_data[name] = value
                new_raw[name] = slice_[0] if count == 1 else list(slice_)
            # Inter-block sleep (Solis spec: >300 ms between frames)
            time.sleep(0.35)

        if not new_data:
            self.read_errors += 1
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._FORCE_RECONNECT_AFTER:
                self._force_reconnect(
                    reason=f"{self._consecutive_failures} consecutive empty polls"
                )
                self._consecutive_failures = 0
            return
        # We got *something* this cycle — reset the failure counter
        self._consecutive_failures = 0

        # Derived fields
        # battery_power (V*I, signed by direction flag)
        if "battery_voltage" in new_data and "battery_current" in new_data:
            v = new_data["battery_voltage"]
            i = abs(new_data["battery_current"])
            direction = new_data.get("battery_current_dir", 0)
            p = v * i
            if direction == 1:  # discharging
                p = -p
            new_data["battery_power"] = round(p, 1)

        # Decoded status strings
        wm = int(new_data.get("working_mode", 0) or 0)
        new_data["working_mode_str"] = WORKING_MODES.get(wm, f"Unknown ({wm})")
        bms = int(new_data.get("bms_status", 0) or 0)
        new_data["bms_status_str"] = BMS_STATUS.get(bms, f"Unknown ({bms})")

        # Active fault list (raw codes — Solis fault codes are large bitfields,
        # we surface the raw values; the dashboard can show "fault present" if any non-zero)
        faults = [new_data.get(f"fault_code_{i:02d}", 0) for i in (1, 2, 3, 4)]
        new_data["fault_present"] = any(int(f or 0) != 0 for f in faults)

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

            current_minute = now.minute
            if current_minute != self._last_history_minute:
                self._last_history_minute = current_minute
                self.history["timestamps"].append(now.strftime("%H:%M"))
                for key in ["battery_soc", "pv_total_power", "active_power",
                            "battery_power", "battery_voltage", "grid_frequency"]:
                    self.history[key].append(new_data.get(key, 0))

    def _poll_loop(self):
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception as e:
                log.error(f"Solis: poll error: {e}")
            self._stop_event.wait(self.poll_interval)

    # -- public API -------------------------------------------------------
    def start(self):
        self.connect()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        log.info(f"Solis: polling started (every {self.poll_interval}s)")

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
