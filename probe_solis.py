#!/usr/bin/env python3
"""
probe_solis.py — Solis S6-EH3P Modbus TCP diagnostic.

Goes through the layers one at a time so we can see exactly *where* the
Solis is failing:

  1. TCP socket open to <ip>:502   (is the Modbus server listening at all?)
  2. HTTP fingerprint on port 80   (what kind of device is at this IP?)
  3. Modbus-TCP framing (MBAP)     — standard pymodbus path, function 0x04
  4. Modbus-RTU-over-TCP framing   — raw frame + CRC, in case the dongle
                                     uses RTU framing inside TCP (some
                                     Solis WiFi sticks do this)
  5. Deep read of known-good registers on whichever framing/slave worked

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
* If TCP opens but every slave ID returns RST/BrokenPipe under TCP
  framing AND the RTU-framing test also fails, Modbus TCP is likely
  disabled at the application layer (Solis Cloud portal setting),
  or the device at this IP isn't actually the Solis.
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


# ---------------------------------------------------------------------------
# HTTP fingerprint — figure out what device is actually at this IP
# ---------------------------------------------------------------------------
def http_fingerprint(host, port=80, timeout=3):
    """GET / on port 80 and return the Server: header + first 200 bytes."""
    print(f"[2] HTTP fingerprint  ->  http://{host}:{port}/")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
    except (socket.timeout, ConnectionRefusedError, OSError) as e:
        print(f"    No HTTP on port {port} ({e}) — device may not have a web UI.")
        s.close()
        return
    try:
        s.sendall(f"GET / HTTP/1.0\r\nHost: {host}\r\n\r\n".encode())
        buf = b""
        while len(buf) < 2048:
            try:
                chunk = s.recv(1024)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
        if not buf:
            print(f"    Connected but no response — odd.")
            return
        head = buf[:300].decode("latin-1", errors="replace")
        # Try to surface the Server: line and the <title> if present
        server_line = ""
        for line in head.splitlines():
            if line.lower().startswith("server:"):
                server_line = line.strip()
                break
        title = ""
        full = buf.decode("latin-1", errors="replace")
        import re as _re
        m = _re.search(r"<title>(.*?)</title>", full, _re.S | _re.I)
        if m:
            title = m.group(1).strip()
        print(f"    First line     : {head.splitlines()[0] if head.splitlines() else '(empty)'}")
        if server_line:
            print(f"    {server_line}")
        if title:
            print(f"    <title>        : {title}")
        # Heuristic: known device fingerprints
        ftext = (server_line + " " + title + " " + head).lower()
        for needle, verdict in [
            ("solis",       "looks like a Solis device"),
            ("ginlong",     "looks like a Solis (Ginlong) device"),
            ("fox",         "looks like a FoxESS device"),
            ("eastron",     "looks like an Eastron meter — wrong device for Solis polling"),
            ("sdm",         "looks like an Eastron SDM meter — wrong device for Solis polling"),
            ("modbus",      "generic Modbus gateway"),
            ("openwrt",     "OpenWrt / generic Linux box — probably not the inverter"),
            ("router",      "router or gateway — probably not the inverter"),
            ("pylontech",   "Pylontech BMS — wrong device for Solis polling"),
        ]:
            if needle in ftext:
                print(f"    Verdict        : {verdict}")
                break
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Raw Modbus-RTU-over-TCP — for dongles that wrap RTU framing in TCP
# instead of the standard MBAP-header Modbus-TCP framing.
# ---------------------------------------------------------------------------
def crc16_modbus(data: bytes) -> bytes:
    """Modbus CRC-16 (poly 0xA001), little-endian on the wire."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def rtu_over_tcp_read(host, port, slave, address, count, fc=0x04, timeout=TIMEOUT):
    """Send a Modbus-RTU frame over a raw TCP socket and read the response."""
    pdu = bytes([
        slave & 0xFF,
        fc & 0xFF,
        (address >> 8) & 0xFF, address & 0xFF,
        (count >> 8) & 0xFF,   count & 0xFF,
    ])
    frame = pdu + crc16_modbus(pdu)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        s.sendall(frame)
        buf = b""
        # Expected response length: slave(1) + fc(1) + bc(1) + 2*count + crc(2) = 5 + 2*count
        expected = 5 + 2 * count
        while len(buf) < expected:
            try:
                chunk = s.recv(64)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
        return buf
    except (ConnectionResetError, BrokenPipeError, socket.timeout, OSError) as e:
        return f"socket error: {type(e).__name__}: {e}"
    finally:
        s.close()


def rtu_probe(host, port):
    """Try RTU-over-TCP framing against each candidate slave on reg 33000."""
    print(f"[4] Modbus-RTU-over-TCP probe  ->  reg 33000, fc 0x04")
    print(f"    Trying slave IDs: {CANDIDATE_SLAVES}")
    found = None
    for sid in CANDIDATE_SLAVES:
        resp = rtu_over_tcp_read(host, port, sid, 33000, 1)
        if isinstance(resp, str):
            print(f"    slave {sid:3d}: {resp}")
        elif len(resp) == 0:
            print(f"    slave {sid:3d}: no response (empty)")
        elif len(resp) >= 2 and resp[1] & 0x80:
            # Modbus exception response — device IS speaking RTU framing,
            # just doesn't recognise this slave/register combination.
            print(f"    slave {sid:3d}: Modbus exception ({resp.hex()}) — RTU framing recognised")
            if found is None:
                found = sid  # at least this slave is talking back
        elif len(resp) >= 5:
            byte_count = resp[2]
            if byte_count == 2 and len(resp) >= 7:
                model = (resp[3] << 8) | resp[4]
                print(f"    slave {sid:3d}: model_no = {model}  <-- responding under RTU-over-TCP")
                return sid
            print(f"    slave {sid:3d}: short response, raw={resp.hex()}")
        else:
            print(f"    slave {sid:3d}: short response, raw={resp.hex()}")
        time.sleep(0.4)
    return found


def main():
    print(f"\nSolis Modbus TCP probe — target {HOST}:{PORT}")
    hr()

    if not tcp_check(HOST, PORT):
        hr()
        print("Stopping here — fix TCP reachability first.")
        sys.exit(2)
    hr()

    # Layer 2 — fingerprint via HTTP (helps confirm what device this is)
    http_fingerprint(HOST, port=80)
    hr()

    # Layer 3 — standard Modbus-TCP framing
    print(f"[3] Modbus-TCP framing (MBAP header)")
    client = ModbusTcpClient(host=HOST, port=PORT, timeout=TIMEOUT)
    if not client.connect():
        print("    pymodbus client.connect() returned False — strange after a")
        print("    successful raw socket. Check pymodbus version: pip show pymodbus")
    else:
        kwarg = slave_kwarg(client)
        print(f"    pymodbus connected  (kwarg style: '{kwarg}')")
        sid = find_working_slave(client, kwarg)
        if sid is not None:
            hr()
            deep_probe(client, sid, kwarg)
            hr()
            print(f"OK — Solis is responding under Modbus-TCP framing, slave {sid}.")
            print(f"If the dashboard still shows disconnected, the fox-monitor")
            print(f"service may be holding a stale TCP slot — restart it:")
            print(f"   sudo systemctl restart fox-monitor")
            client.close()
            return
        client.close()
    hr()

    # Layer 4 — fall back to RTU-over-TCP framing
    rtu_sid = rtu_probe(HOST, PORT)
    hr()
    if rtu_sid is not None:
        print(f"DEVICE SPEAKS RTU-OVER-TCP, NOT MBAP MODBUS-TCP.")
        print(f"  Slave that responded: {rtu_sid}")
        print(f"  This means solis_reader.py needs to use a different pymodbus")
        print(f"  client (ModbusSerialClient with TCP transport, or raw socket).")
        print(f"  Tell me and I'll switch the reader over.")
        sys.exit(5)

    print("Both Modbus-TCP and Modbus-RTU-over-TCP framing failed.")
    print("Possible causes:")
    print("  * The device at this IP isn't the Solis at all — check the")
    print("    HTTP fingerprint above, or your router's DHCP lease table.")
    print("  * Dongle in 'monitoring only' mode — needs Solis Cloud portal")
    print("    to enable third-party Modbus TCP.")
    print("  * Inverter in deep-sleep at night — try again in daylight.")
    sys.exit(4)


if __name__ == "__main__":
    main()
