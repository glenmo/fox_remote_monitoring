#!/usr/bin/env python3
"""
scan_soc.py — probe likely SoC / BMS register addresses on the Fox H3 series
to find where this particular firmware actually publishes the SoC value.

Run on desky:
    sudo systemctl stop fox-monitor
    source venv/bin/activate
    python scan_soc.py 192.168.11.81
"""
import sys, time
from pymodbus.client import ModbusTcpClient

HOST = sys.argv[1] if len(sys.argv) > 1 else "192.168.11.81"
SLAVE = 247

# Candidate SoC / SOH / battery registers across Fox H3 firmware revisions.
CANDIDATES = [
    # spec V1.05 — primary
    (39423, "U16", "system_soc (spec)"),
    (37612, "U16", "bms1_soc"),
    (37624, "U16", "bms1_soh"),
    (38310, "U16", "bms2_soc"),
    (38322, "U16", "bms2_soh"),
    # alternates seen on older / regional H3 firmware
    (11034, "U16", "alt_soc_h3_legacy"),
    (31024, "U16", "alt_soc_pack"),
    (37636, "U16", "alt_soc_after_design"),
    # paired energy registers (sanity)
    (39601, "U32", "pv_total_kwh"),
    (39603, "U32", "pv_today_kwh"),
    (39605, "U32", "charge_total_kwh"),
    (39607, "U32", "charge_today_kwh"),
    (39629, "U32", "load_total_kwh"),
    (39631, "U32", "load_today_kwh"),
    # battery combined power
    (39237, "I32", "battery_combined_w"),
    # battery1 detail (we know these work — sanity)
    (39227, "I16", "battery1_voltage"),
    (39228, "I32", "battery1_current"),
    (39230, "I32", "battery1_power"),
]

def decode(regs, dtype):
    if regs is None:
        return None
    if dtype == "U16":
        return regs[0]
    if dtype == "I16":
        v = regs[0]
        return v - 0x10000 if v >= 0x8000 else v
    if dtype == "U32":
        return (regs[0] << 16) | regs[1]
    if dtype == "I32":
        v = (regs[0] << 16) | regs[1]
        return v - 0x100000000 if v >= 0x80000000 else v

def read(client, addr, count):
    try:
        r = client.read_holding_registers(address=addr, count=count, device_id=SLAVE)
        if hasattr(r, "isError") and r.isError():
            return None, str(r)
        return r.registers, None
    except Exception as e:
        return None, str(e)

print(f"Probing {HOST}:502 slave {SLAVE} ...\n")
c = ModbusTcpClient(host=HOST, port=502, timeout=5)
if not c.connect():
    print("FAILED to open TCP socket — service still running?")
    sys.exit(1)

print(f"{'Addr':<6}  {'Name':<26}  {'Type':<4}  {'Raw':<14}  Decoded")
print("-" * 78)
for addr, dtype, name in CANDIDATES:
    count = 1 if dtype in ("U16", "I16") else 2
    regs, err = read(c, addr, count)
    if err:
        print(f"{addr:<6}  {name:<26}  {dtype:<4}  {'(error)':<14}  {err}")
    else:
        raw = " ".join(f"{r:04X}" for r in regs)
        val = decode(regs, dtype)
        print(f"{addr:<6}  {name:<26}  {dtype:<4}  {raw:<14}  {val}")
    time.sleep(0.1)

c.close()
print("\nDone.")
