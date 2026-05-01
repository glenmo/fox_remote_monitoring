#!/usr/bin/env python3
"""
probe_modbus.py — quick FoxESS Modbus TCP slave-ID prober.

Tries a handful of common slave IDs and reads:
  - 30000 (Model name, STR, 16 regs)
  - 39423 (System SoC, U16, 1 reg)
  - 39139 (Grid frequency, I16, 1 reg)

Run on desky:
    cd ~/fox_remote_monitoring
    source venv/bin/activate
    python probe_modbus.py 192.168.11.81
"""
import struct, sys, time
from pymodbus.client import ModbusTcpClient

HOST = sys.argv[1] if len(sys.argv) > 1 else "192.168.11.81"
PORT = 502
CANDIDATES = [1, 247, 2, 248, 100]

def regs_to_str(regs):
    raw = b"".join(struct.pack(">H", r) for r in regs)
    return raw.split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()

def kw(client):
    """Pick whichever kwarg name this pymodbus version uses."""
    import inspect
    sig = inspect.signature(client.read_holding_registers)
    return "device_id" if "device_id" in sig.parameters else "slave"

def try_read(client, addr, count, slave):
    try:
        kwargs = {kw(client): slave}
        r = client.read_holding_registers(address=addr, count=count, **kwargs)
        if hasattr(r, "isError") and r.isError():
            return f"Modbus error: {r}"
        return r.registers
    except Exception as e:
        return f"Exception: {e}"

print(f"Probing {HOST}:{PORT} ...\n")
client = ModbusTcpClient(host=HOST, port=PORT, timeout=3)
if not client.connect():
    print("FAILED to open TCP socket — inverter not reachable.")
    sys.exit(1)
print(f"TCP open OK (using kwarg '{kw(client)}')\n")

for sid in CANDIDATES:
    print(f"--- Slave ID {sid} ---")
    res = try_read(client, 30000, 16, sid)
    if isinstance(res, list):
        print(f"  30000 model_name : {regs_to_str(res)!r}")
    else:
        print(f"  30000 model_name : {res}")
    time.sleep(0.3)
    res = try_read(client, 39423, 1, sid)
    if isinstance(res, list):
        print(f"  39423 system_soc : {res[0]} %")
    else:
        print(f"  39423 system_soc : {res}")
    time.sleep(0.3)
    res = try_read(client, 39139, 1, sid)
    if isinstance(res, list):
        v = res[0]
        if v >= 0x8000:
            v -= 0x10000
        print(f"  39139 grid_freq  : {v/100:.2f} Hz")
    else:
        print(f"  39139 grid_freq  : {res}")
    print()
    time.sleep(0.5)

client.close()
print("Probe complete.")
