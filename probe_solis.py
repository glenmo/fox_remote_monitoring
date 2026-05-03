#!/usr/bin/env python3
"""
probe_solis.py — Solis S6-EH3P Modbus TCP diagnostic.

Goes through the layers one at a time so we can see exactly *where* the
Solis is failing:

  1. TCP socket open to <ip>:502   (is the Modbus server listening at all?)
  2. Modbus read on slave 1, reg 33000 (model_no)  — function code 0x04
  3. Same on slaves 2, 100, 247  (in case the dongle was reconfigured)
  4. Read a handful of known-good registers on the working slave ID

Run on desky.local:
    cd ~/fox_remote_monitoring
    sudo systemctl stop fox-monitor      # free the dongle's single TCP slot
    source venv/bin/activate
    python probe_solis.py 192.168.11.214
    sudo systemctl start fox-monitor

Notes
-----
* The Solis WiFi/LAN dongle generally only accepts ONE Modbus TCP client
  at a time. If the fox-monitor service is already polling, this probe
  will fail to even open the socket. Stop the service first.
* If TCP open fails but ping works, the Modbus TCP server on the dongle
  has crashed — power-cycle the dongle (unplug the small USB/LAN stick
  on the side of the inverter for ~10 seconds, plug back in).
* If TCP opens but every slave ID returns errors, Modbus TCP may have
  been disabled in the dongle web UI, or the gateway is bridging to the
  wrong RS485 bus.
"""
import socket
import struct
import sys
import time

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusIOException

HOST = sys.argv[1] if len(sys.argv) > 1 else "192.168.11.214"
PORT = 502
TIMEOUT = 5
CANDIDATE_SLAVES = [1, 2, 100, 247]

# A few known-good Solis input registers (function 0x04)
# Format: (addr, count, name, decode)
PROBE_REGS = [
    (33000, 1,  "model_no",        "U16"),
    (33022, 5,  "clock(yr/mo/d/h/m)", "RAW"),
    (33049, 1,  "pv1_voltage",     "U16/10"),
    (33057, 2,  "pv_total_power",  "U32"),
    (33094, 1,  "grid_frequency",  "U16/100"),
    (33133, 1,  "battery_voltage", "U16/10"),
    (33139, 1,  "battery_soc",     "U16"),
    (33091, 1,  "working_mode",    "U16"),
    (33095, 1,  "inverter_status", "U16"),
]


def hr(c="-"):
    print(c * 70)


def tcp_check(host, port, timeout=TIMEOUT):
    """Plain socket open — does the Modbus server even answer SYN?"""
    print(f"[1] TCP connect test  ->  {host}:{port}")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    t0 = time.monotonic()
    try:
        s.connect((host, port))
        dt = (time.monotonic() - t0) * 1000
        print(f"    OK ({dt:.0f} ms)")
        return True
    except socket.timeout:
        print(f"    TIMEOUT after {timeout}s — port {port} not answering.")
        print(f"    Most likely cause: dongle's Modbus TCP server has crashed.")
        print(f"    Fix: power-cycle the WiFi/LAN dongle on the Solis.")
        return False
    except ConnectionRefusedError:
        print(f"    REFUSED — host alive but nothing on port {port}.")
        print(f"    Modbus TCP probably not enabled in the dongle config.")
        return False
    except OSError as e:
        print(f"    FAILED ({e})")
        return False
    finally:
        s.close()


def slave_kwarg(client):
    """pymodbus 3.6+ uses device_id=, older uses slave=."""
    import inspect
    sig = inspect.signature(client.read_input_registers)
    return "device_id" if "device_id" in sig.parameters else "slave"


def try_read(client, addr, count, slave, kwarg):
    try:
        kwargs = {kwarg: slave}
        r = client.read_input_registers(address=addr, count=count, **kwargs)
        if isinstance(r, ModbusIOException):
            return f"ModbusIOException: {r}"
        if r.isError():
            return f"Modbus error: {r}"
        return r.registers
    except Exception as e:
        return f"Exception: {type(e).__name__}: {e}"


def decode(regs, kind):
    if not isinstance(regs, list):
        return regs  # error string
    if kind == "RAW":
        return regs
    if kind == "U16":
        return regs[0]
    if kind == "U16/10":
        return f"{regs[0] / 10:.1f}"
    if kind == "U16/100":
        return f"{regs[0] / 100:.2f}"
    if kind == "U32":
        v = (regs[0] << 16) | regs[1]
        return v
    if kind == "S16":
        v = regs[0]
        if v >= 0x8000:
            v -= 0x10000
        return v
    return regs


def find_working_slave(client, kwarg):
    """Try each candidate slave ID against reg 33000 (model_no).

    Returns the first slave that answers without a Modbus error, or None.
    """
    print(f"[2] Modbus probe  ->  reg 33000 (model_no), function 0x04")
    print(f"    Trying slave IDs: {CANDIDATE_SLAVES}")
    for sid in CANDIDATE_SLAVES:
        res = try_read(client, 33000, 1, sid, kwarg)
        if isinstance(res, list):
            print(f"    slave {sid:3d}: model_no = {res[0]}  <-- responding")
            return sid
        else:
            print(f"    slave {sid:3d}: {res}")
        time.sleep(0.4)  # Solis spec: >300 ms between frames
    return None


def deep_probe(client, slave, kwarg):
    print(f"[3] Reading known-good registers on slave {slave}")
    for addr, count, name, kind in PROBE_REGS:
        res = try_read(client, addr, count, slave, kwarg)
        if isinstance(res, list):
            print(f"    {addr:5d}  {name:20s}  raw={res}  decoded={decode(res, kind)}")
        else:
            print(f"    {addr:5d}  {name:20s}  {res}")
        time.sleep(0.4)


def main():
    print(f"\nSolis Modbus TCP probe — target {HOST}:{PORT}")
    hr()

    if not tcp_check(HOST, PORT):
        hr()
        print("Stopping here — fix TCP reachability first.")
        sys.exit(2)
    hr()

    client = ModbusTcpClient(host=HOST, port=PORT, timeout=TIMEOUT)
    if not client.connect():
        print("pymodbus client.connect() returned False — strange after a")
        print("successful raw socket. Check pymodbus version: pip show pymodbus")
        sys.exit(3)
    kwarg = slave_kwarg(client)
    print(f"    pymodbus connected  (kwarg style: '{kwarg}')")
    hr()

    sid = find_working_slave(client, kwarg)
    hr()

    if sid is None:
        print("No slave ID answered. Possible causes:")
        print("  * Modbus TCP enabled in the dongle, but bridged to the")
        print("    wrong RS485 bus (no inverter on the other side).")
        print("  * Dongle in 'monitoring only' mode — needs Solis Cloud")
        print("    portal to enable third-party Modbus TCP.")
        print("  * Inverter in deep-sleep at night — try again in daylight.")
        client.close()
        sys.exit(4)

    deep_probe(client, sid, kwarg)
    hr()
    print(f"OK — Solis is responding on slave {sid}.")
    print(f"If the dashboard still shows disconnected, the fox-monitor")
    print(f"service may be holding a stale TCP slot — restart it:")
    print(f"   sudo systemctl restart fox-monitor")

    client.close()


if __name__ == "__main__":
    main()
