# Fox ESS H3 Monitor

A live monitoring web dashboard for **FoxESS H3** family inverters
(developed and field-tested against an **H3-15.0-SMART**, but the same
register map covers H3 / H3 Pro / H3 Smart per FoxESS Modbus spec
V1.05.03.00). Polled directly over **Modbus TCP** and served from a
Debian/Ubuntu box behind **Apache 2**.

```
   ┌──────────────────┐    Modbus/TCP     ┌────────────────────────┐
   │  Fox H3 inverter │ <───────────────  │  desky.local           │
   │  192.168.11.81   │   port 502        │  192.168.55.33         │
   │  slave 247       │   func 0x03       │  ├─ Flask app :5000    │
   │  PROT-F          │                   │  └─ Apache2 :80 →:5000 │
   └──────────────────┘                   └────────────────────────┘
                                                       │
                                                       ▼
                                              http://desky.local/
```

## What you get

A single-page dashboard with:

- Battery SoC gauge with charge / discharge direction badge
- Power-flow tiles: PV, Grid (meter), Battery, Load, EPS, Frequency
- 3-phase grid voltages, inverter currents, power factor
- PV string detail (V / A / W per string)
- Battery & BMS detail — voltage, current, cell min/max, temps, SoH
- Energy totals (today / lifetime) for PV, feed-in, import, charge,
  discharge, load
- Active alarms decoded from Alarm 1 / 2 / 3 bitfields
- 24-hour line charts for power flow and SoC + grid frequency

Plus a small JSON API for integrating with anything else
(`/api/data`, `/api/history`, `/api/status`).

## Inverter prerequisites

Two settings on the H3's LCD must be in place before any of this works:

1. **Modbus TCP enabled** — under the inverter's communication settings
2. **Protocol set to `PROT-F`** (the FoxESS native protocol). The default
   is often `PROT-S` (SunSpec), which exposes a *completely different*
   register layout starting at 40000+ — none of this codebase will work
   against it.

If `PROT-F` isn't in the protocol menu, your firmware may call it
`PROT-FOX`, `PROT-NORM`, or simply offer it as the non-`PROT-S` option.

The slave / device ID is `247` on stock H3 firmware. If you've changed
it via the FoxESS app, update `--slave-id` accordingly.

## Quick start

```bash
# 1. Clone onto the Debian server
ssh you@desky.local
git clone https://github.com/glenmo/fox_remote_monitoring.git
cd fox_remote_monitoring

# 2. Run the installer
bash install.sh

# 3. Open the dashboard
#    http://desky.local/   (or  http://192.168.55.33/)
```

The installer:

1. Installs `python3`, `python3-venv`, `apache2`
2. Creates a venv at `./venv` and installs Flask + pymodbus
3. Drops a systemd unit at `/etc/systemd/system/fox-monitor.service`
4. Enables the Apache modules `proxy`, `proxy_http`, `headers`
5. Installs the vhost at `/etc/apache2/sites-available/fox-monitor.conf`
6. Disables Apache's default site so port 80 is free
7. Starts the service

## Default settings

| Setting | Default | Override |
|---|---|---|
| Inverter IP | `192.168.11.81` | `INV_IP=… bash install.sh` |
| Inverter port | `502` | `INV_PORT=…` |
| Modbus slave ID | `247` | `SLAVE_ID=…` |
| Poll interval | `10` s | `POLL=…` |
| Flask port | `5000` | `FLASK_PORT=…` |

To change after install, edit `/etc/systemd/system/fox-monitor.service`,
then:

```bash
sudo systemctl daemon-reload
sudo systemctl restart fox-monitor
```

## Command-line options (for manual runs)

```text
python app.py --help

  --host             Flask listen address          (default: 127.0.0.1)
  --port             Flask listen port             (default: 5000)
  --inverter-ip      Fox H3 IP address             (default: 192.168.11.81)
  --inverter-port    Modbus TCP port               (default: 502)
  --slave-id         Modbus slave/device ID        (default: 247)
  --poll-interval    Poll interval in seconds      (default: 10)
  --debug            Enable Flask debug mode
```

## Modbus architecture

The reader uses **function code 0x03 (Read Holding Registers)** — what
the FoxESS spec mandates. It does NOT use 0x04 (Read Input Registers)
even though that's more common for inverters.

The reader's poll cycle is built around **bulk reads**: instead of
issuing one Modbus PDU per field (~120 round-trips), the register map
is grouped at startup into 11 contiguous blocks that are each fetched
in a single PDU. This converts a typical poll cycle from ~10 s of
back-and-forth (and reliable RST-by-the-dongle) into well under 1 s.

Read plan currently looks like:

| # | Range | Count | Fields | Block content |
|---|---|---|---|---|
| 1 | 37609–37624 | 16 | 9 | BMS1 V/I/temp/SoC/cell min-max/SoH |
| 2 | 37632 | 1 | 1 | BMS1 remaining energy |
| 3 | 39000–39001 | 2 | 1 | Protocol version |
| 4 | 39050–39077 | 28 | 19 | Model ID / strings / status / alarms / PV1-4 V&I |
| 5 | 39118–39141 | 24 | 12 | PV total / 3-phase grid / power / freq / temp |
| 6 | 39149–39152 | 4 | 2 | Cumulative + today PV energy |
| 7 | 39162–39169 | 8 | 2 | Battery & meter active power |
| 8 | 39201–39238 | 38 | 22 | EPS, Load, Battery 1+2 detail |
| 9 | 39248–39286 | 39 | 19 | INV phase active/reactive/apparent + freq |
| 10 | 39327–39338 | 12 | 9 | MPPT 1-3 |
| 11 | 39601–39632 | 32 | 16 | All energy totals (PV/charge/discharge/feed/import/load) |

The full register list (with type, gain, units, description) lives at
the top of `fox_reader.py`. Add or remove rows there — `_build_blocks()`
auto-groups them at startup and the polling loop and JSON API pick up
the new fields automatically.

Bitfield decoders are included for:

- **Status1** — running / standby / fault
- **Status3** — off-grid
- **Alarm1 / Alarm2 / Alarm3** — full Table 4-1 mapping to alarm names

## Firmware-specific quirks (H3-15.0-SMART)

A few places where the spec PDF and reality diverge — handled in the
reader:

| Issue | Fix in `fox_reader.py` |
|---|---|
| Spec register **39423** (system_soc) returns no response | Commented out; reader populates `system_soc` from `bms1_soc` (37612) instead. |
| **39162** (battery_charge_power) — POSITIVE when charging ✓ matches spec | Used as canonical source. |
| **39237** (battery_power_total) — NEGATIVE when charging | Sign-flipped or ignored. The reader exposes a derived `battery_flow_w` (>0 charging, <0 discharging) and `battery_power_w` (chart-friendly, same sign convention) so the dashboard never has to guess. |
| **Meter1 / CT1** registers (38801+) — only populated if a Fox energy meter is wired | `METER_REGISTERS` is commented out by default. Uncomment if you have one. |

If you're on a different H3 firmware revision and SoC reads as 0,
run the included `scan_soc.py` from the project root:

```bash
sudo systemctl stop fox-monitor
source venv/bin/activate
python scan_soc.py 192.168.11.81
sudo systemctl start fox-monitor
```

It probes a handful of likely SoC / SOH register candidates and prints
their raw and decoded values. Once you spot the address that matches
your LCD's SoC, point the reader at it.

## Diagnostic scripts

Two small utilities live alongside the main code:

- **`probe_modbus.py <ip>`** — tries common slave IDs (1, 247, 2, 248,
  100) against three known-good registers and reports which (if any)
  answer. Use when first connecting a new inverter or when troubleshooting
  Modbus reads.
- **`scan_soc.py <ip>`** — probes the documented SoC register plus
  several alternates seen on different H3 firmware revisions. Useful
  when SoC reads as 0 but the LCD shows a different value.

Run them with the systemd service stopped (so they own the Modbus
socket cleanly).

## API

| Endpoint | Description |
|---|---|
| `GET /` | Dashboard |
| `GET /api/data` | All current decoded values + derived fields + alarm strings |
| `GET /api/history` | 24-h rolling 1-minute samples for charts |
| `GET /api/status` | Connection / poll-rate / error counters |
| `GET /api/message` | Contents of `message.txt` (optional banner text) |

## Files

```
fox_remote_monitoring/
├── app.py              Flask app + REST API
├── fox_reader.py       Modbus TCP reader & register map
├── probe_modbus.py     Slave-ID probe (diagnostic)
├── scan_soc.py         SoC register scanner (diagnostic)
├── requirements.txt    Python deps
├── install.sh          One-shot Debian + Apache installer
├── fox-monitor.conf    Apache vhost
├── message.txt         Optional banner text shown at the bottom
├── templates/
│   └── dashboard.html  Web UI (Chart.js, no build step)
└── README.md
```

## Apache vhost

The bundled `fox-monitor.conf` does three things:

1. Reverse-proxies `desky.local` → `127.0.0.1:5000` (Flask).
2. Adds `Cache-Control: no-store` to `/api/*` responses.
3. Has a `<LocationMatch>` block that **403s** legacy Fronius
   DataManager probes (`/img/Fronius-Logo.png`, `/uiLib/`,
   `/product/list`, `/point.shtml`, `/device-manager`, etc.) so they
   never reach Flask. If a Python script or browser tab on your network
   is wandering around looking for a Fronius inverter, it'll be silently
   blocked at Apache instead of cluttering the fox-monitor journal with
   404s.

To find the source of stray Fronius requests on your network, check
the Apache access log:

```bash
sudo grep -E '403' /var/log/apache2/fox-monitor-access.log | awk '{print $1}' | sort -u
```

## Notes & gotchas

- **Function code is 0x03**, not 0x04 — the FoxESS spec is unusual
  among hybrid inverters in using *holding* registers for live data.
- **Gain** is a divisor: `value = raw / gain`. Watch out for kW
  registers with gain 1000, which decode to watt resolution as a float.
- **Don't put this repo inside Dropbox** (or any sync tool that touches
  files in real time). Git's index.lock and Dropbox's sync agent fight
  over the same files and you'll get spurious "another git process is
  running" errors. `~/Code/` or any non-synced directory is fine —
  GitHub already gives you cloud sync.
- All chart data is in memory only — restart the service and the
  24-hour history starts empty again.
- Polling cadence (`POLL`) below 5 s starts to stress the FoxESS LAN
  dongle. 10 s is the sweet spot for a hybrid inverter.

## Dependencies

- Python ≥ 3.9 (tested on 3.10 and 3.13)
- `Flask` ≥ 3.0
- `pymodbus` ≥ 3.6 (uses `device_id=` kwarg on `read_holding_registers`;
  `slave=` for older versions is auto-detected)
- Chart.js 4.4 (loaded from cdnjs at runtime — no build step)

## License

Same terms as the rest of the project — see `LICENSE` if present, or
treat as MIT-style: use, modify, share at will.
