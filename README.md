# Fox + Solis Combined Monitor

A live, side-by-side monitoring dashboard for **two hybrid inverters
in one place**:

- **FoxESS H3-15.0-SMART** (or any H3 / H3 Pro / H3 Smart per spec V1.05.03.00)
- **Solis S6-EH3P 50 kW** hybrid inverter

Both polled directly over Modbus TCP from a single Flask service on a
Debian / Ubuntu box behind Apache 2.

```
   ┌──────────────────────┐  Modbus/TCP
   │  Fox H3-15 SMART     │  port 502
   │  192.168.11.81       │  func 0x03   ┐
   │  slave 247  PROT-F   │              │
   └──────────────────────┘              │     ┌────────────────────────┐
                                         ├──── │  desky.local           │
   ┌──────────────────────┐              │     │  192.168.55.33         │
   │  Solis S6-EH3P 50 kW │  port 502    │     │  ├─ Flask app :5000    │
   │  192.168.11.214      │  func 0x04   ┘     │  └─ Apache2 :80 →:5000 │
   │  slave 1             │                    └────────────────────────┘
   └──────────────────────┘                              │
                                                         ▼
                                                http://desky.local/
```

## What you get

A single dashboard with two side-by-side panels, plus a combined
site-wide section:

**Left panel — Fox H3-15.0-SMART**
- SoC gauge with charge / discharge direction badge
- Power-flow tiles (PV, Grid, Battery, Load)
- 3-phase grid voltages & inverter currents, frequency, PF, internal temp
- Energy today / lifetime: PV, feed-in, import, charge, discharge, load
- Active alarms (Alarm 1/2/3 bitfields decoded)
- Battery / BMS line: voltage, current, cell min/max mV, SoH

**Right panel — Solis S6-EH3P**
- SoC gauge + charge/discharge badge
- Power-flow tiles (PV, Active power, Battery, DC bus)
- 3-phase line-line voltages, currents, frequency, module temp
- PV strings (V / A per string)
- Energy lifetime + today
- Faults (raw codes — Solis fault bitfields are extensive)
- Battery + BMS limits (charge/discharge current limits)

**Bottom — Site totals**
- PV total now (Fox + Solis)
- Battery flow now (Fox + Solis), with breakdown
- Combined SoC (simple average)
- PV today (Fox + Solis)

**Bottom charts**
- 24-hour PV power: Fox, Solis, and combined dashed line
- 24-hour Battery SoC for both inverters

Plus a JSON API for integrations (see Endpoints below).

## Inverter prerequisites

### Fox H3
1. **Modbus TCP enabled** — under the inverter's communication settings
2. **Protocol set to `PROT-F`** (FoxESS native). The default is often
   `PROT-S` (SunSpec), which exposes a *completely different* register
   layout — none of this codebase will work against it.
3. Slave / device ID: `247` on stock H3 firmware.

### Solis S6-EH3P
1. **Modbus TCP enabled** on the WiFi/LAN dongle (or via a separate
   Modbus TCP gateway on the RS485 bus).
2. Slave ID: `1` is the default for stock Solis configurations.
3. Function code is `0x04` (Read Input Registers) — the reader knows
   this; nothing to configure.

## Quick start

The installer auto-detects the host's name and IP, so the same script
deploys cleanly on any Debian / Ubuntu / Raspberry Pi OS machine — just
SSH to the target and run it.

```bash
ssh you@<host>.local             # e.g. desky.local or rubberduck.local
git clone https://github.com/glenmo/fox_remote_monitoring.git
cd fox_remote_monitoring
bash install.sh
# open http://<host>.local/
```

### Where should this run?

Both the Fox H3 and the Solis Modbus TCP gateway need to be reachable
*from the host*. The installer doesn't tunnel anything for you. Pick
the host that already sits on the same LAN as both inverters:

| Scenario | Recommended host |
|---|---|
| Both inverters at one site, on the same LAN as a Pi | The Pi (e.g. **rubberduck.local**) |
| Both on a desktop/server's LAN | That box (e.g. **desky.local**) |
| Two sites, one inverter each, separate VPN | Run two services — one per site — and have one push to the other (see Architecture below) |

### Coexistence with the older `microgrid_remote_monitor` project

If the target host is already running `microgrid_remote_monitor` (which
also reads the Solis), the new combined service will collide on Flask
port `5000` *and* will fight microgrid for the Solis dongle's single
Modbus TCP slot. Two coexistence modes work; pick whichever fits.

**Mode A — replace microgrid (cleanest):**

```bash
sudo systemctl stop    microgrid-monitor
sudo systemctl disable microgrid-monitor
bash install.sh
```

The combined service in this repo is a strict superset of what
`microgrid_remote_monitor` did for the Solis — same register layout,
plus the Fox H3. If you only used the microgrid project for the
Solis, you can safely retire it. If you also relied on its Eastron /
SP Pro / SwitchDin readers, keep the old project around (paused) until
those are ported across.

**Mode B — keep microgrid for the Solis, add fox+solis for the Fox only:**

```bash
NO_SOLIS=1 bash install.sh
```

This generates a systemd unit with `--no-solis`, so fox-monitor only
polls the Fox H3 and never touches the Solis dongle. The combined
dashboard's Solis panel will show "Not polled by this service" in the
banner; you'd continue to use microgrid's own dashboard for live
Solis data, on whatever port/host it's bound to.

**Mode C — bridge Solis from microgrid into the combined dashboard:**

```bash
SOLIS_BRIDGE_URL=http://rubberduck.local:5000 bash install.sh
```

This is the cleanest "one combined dashboard" outcome when microgrid
is already polling the Solis from a different host. The fox-monitor
service runs `solis_http_reader.py` instead of `solis_reader.py` —
it never touches the Solis Modbus dongle, instead fetching live data
every `SOLIS_POLL` seconds from microgrid's `/api/data`,
`/api/history`, and `/api/status` endpoints. The Solis panel on the
combined dashboard then shows live values from microgrid, with a
status host shown as `http://rubberduck.local:5000 (bridged)`. No
contention with the dongle, no duplicate Modbus polling.

**Why fox+solis can't share the Solis dongle with microgrid:**
the Solis WiFi/LAN dongle accepts exactly one Modbus TCP client at a
time. Two services polling it simultaneously results in `RST` /
`BrokenPipe` errors on whichever one loses the race. `probe_solis.py`
will tell you which protocol layer is failing if you're unsure.

The installer is conservative about Apache: it won't disable any
custom vhosts that already exist on the target host. The `fox-monitor`
vhost is configured to answer for `ServerName=$(hostname).local` only,
so running both `microgrid-monitor` (on its own ServerName) and
`fox-monitor` Apache vhosts side by side is safe.

The installer:

1. Installs `python3`, `python3-venv`, `apache2`
2. Creates a venv at `./venv` and installs Flask + pymodbus
3. Drops a systemd unit at `/etc/systemd/system/fox-monitor.service`
   that polls **both** inverters
4. Enables Apache modules `proxy`, `proxy_http`, `headers`
5. Installs the vhost at `/etc/apache2/sites-available/fox-monitor.conf`
6. Disables Apache's default site so port 80 is free
7. Starts the service

## Default settings

| Setting | Default | Override |
|---|---|---|
| Fox IP | `192.168.11.81` | `FOX_IP=… bash install.sh` |
| Fox port | `502` | `FOX_PORT=…` |
| Fox slave | `247` | `FOX_SLAVE=…` |
| Fox poll | `10` s | `FOX_POLL=…` |
| Solis IP | `192.168.11.214` | `SOLIS_IP=…` |
| Solis port | `502` | `SOLIS_PORT=…` |
| Solis slave | `1` | `SOLIS_SLAVE=…` |
| Solis poll | `10` s | `SOLIS_POLL=…` |
| Flask port | `5000` | `FLASK_PORT=…` |
| Disable Fox | off | `NO_FOX=1 bash install.sh` |
| Disable Solis | off | `NO_SOLIS=1 bash install.sh` |
| Solis HTTP bridge | off | `SOLIS_BRIDGE_URL=http://rubberduck.local:5000 bash install.sh` |

Backwards compatibility: `INV_IP`, `INV_PORT`, `SLAVE_ID`, `POLL` from
the original Fox-only deployment still work and map onto the Fox flags.

To tweak after install, edit `/etc/systemd/system/fox-monitor.service`,
then:

```bash
sudo systemctl daemon-reload
sudo systemctl restart fox-monitor
```

To **temporarily disable** one inverter (e.g. while the Solis is offline
for maintenance), add `--no-solis` (or `--no-fox`) to the `ExecStart` line.

## Command-line options (manual runs)

```text
python app.py --help

  --host           Flask listen address          (default: 127.0.0.1)
  --port           Flask listen port             (default: 5000)

  --fox-ip         Fox H3 inverter IP            (default: 192.168.11.81)
  --fox-port       Fox Modbus TCP port           (default: 502)
  --fox-slave      Fox slave/device ID           (default: 247)
  --fox-poll       Fox poll interval, seconds    (default: 10)
  --no-fox         Disable the Fox reader entirely

  --solis-ip       Solis IP                      (default: 192.168.11.214)
  --solis-port     Solis Modbus TCP port         (default: 502)
  --solis-slave    Solis slave/device ID         (default: 1)
  --solis-poll     Solis poll interval, seconds  (default: 10)
  --no-solis       Disable the Solis reader entirely

  --debug          Enable Flask debug mode
```

## API

| Endpoint | Description |
|---|---|
| `GET /` | Combined dashboard |
| `GET /api/fox/data` | Fox H3 current values |
| `GET /api/fox/history` | Fox 24-h rolling history |
| `GET /api/fox/status` | Fox connection / poll counters |
| `GET /api/solis/data` | Solis current values |
| `GET /api/solis/history` | Solis 24-h rolling history |
| `GET /api/solis/status` | Solis connection / poll counters |
| `GET /api/data` / `/api/history` / `/api/status` | Aliases for `/api/fox/*` (back-compat) |
| `GET /api/message` | Optional banner text |

## Modbus architecture

Both readers use **bulk reads** — registers are auto-grouped at startup
into contiguous blocks fetched in a single Modbus PDU each. This is
roughly 10× faster than reading one field at a time and avoids the
"connection reset by peer" issue Fox dongles exhibit under high
request rates.

| Reader | Function code | Blocks/cycle | Fields | Inter-block delay |
|---|---|---|---|---|
| `fox_reader.py`   | 0x03 (holding regs) | 11 | 112 | 50 ms |
| `solis_reader.py` | 0x04 (input regs)   | varies | ~50 | 350 ms (Solis spec >300 ms) |

The full register map for each lives at the top of its respective
file. Add or remove rows there — `_build_blocks()` auto-regroups them.

### Firmware-specific quirks

| Issue | Fix |
|---|---|
| Fox spec register **39423** (system_soc) returns no response on H3-15.0-SMART | Reader populates `system_soc` from `bms1_soc` (37612) instead. |
| Fox **39237** (battery_power_total) is negative-when-charging, opposite of **39162** | Reader exposes a derived `battery_flow_w` (>0 charging) so the dashboard never has to guess. |
| Fox **Meter1 / CT1** (38801+) only populates if a Fox energy meter is wired | `METER_REGISTERS` is empty by default; uncomment to enable. |
| Solis **battery_power** sign uses a separate direction flag (`battery_current_dir`) | Reader computes `battery_power = V × I × (-1 if direction == 1 else 1)`. |

## Diagnostics

Three utilities included for new (or sick) sites:

- **`probe_modbus.py <ip>`** — Fox-side slave-ID prober. Tries common
  IDs against three known-good Fox registers (function 0x03). Useful
  when first connecting a Fox H3.
- **`probe_solis.py <ip>`** — Solis-side diagnostic. Walks through (1)
  raw TCP socket open on port 502, (2) Modbus probe across common
  slave IDs using function 0x04, (3) deep read of known-good Solis
  input registers. Tells you exactly which layer is failing — most
  often a Solis WiFi/LAN dongle that has stopped accepting Modbus TCP
  connections and needs a power-cycle.
- **`scan_soc.py <ip>`** — probes a handful of likely SoC register
  candidates across H3 firmware revisions. Useful when SoC reads as 0
  but the LCD shows something else.

```bash
sudo systemctl stop fox-monitor       # free the dongle's single TCP slot
source venv/bin/activate
python probe_solis.py 192.168.11.214
python scan_soc.py    192.168.11.81
sudo systemctl start fox-monitor
```

## Files

```
fox_remote_monitoring/
├── app.py              Flask app + REST API (Fox + Solis)
├── fox_reader.py       Fox H3 Modbus reader (function 0x03)
├── solis_reader.py     Solis S6 Modbus reader (function 0x04)
├── solis_http_reader.py Solis HTTP bridge — proxies live data from a
│                        microgrid_remote_monitor instance instead of
│                        polling Modbus directly
├── probe_modbus.py     Fox slave-ID probe (diagnostic)
├── probe_solis.py      Solis TCP/Modbus diagnostic
├── scan_soc.py         SoC register scanner (diagnostic)
├── requirements.txt    Python deps
├── install.sh          One-shot Debian + Apache installer
├── fox-monitor.conf    Apache vhost
├── message.txt         Optional banner text
├── templates/
│   └── dashboard.html  Combined web UI (Chart.js, no build step)
└── README.md
```

## Apache vhost

The bundled `fox-monitor.conf`:

1. Reverse-proxies `desky.local` → `127.0.0.1:5000`
2. Adds `Cache-Control: no-store` to `/api/*` responses
3. **403s** legacy Fronius DataManager probes (`/img/Fronius-Logo.png`,
   `/uiLib/`, `/product/list`, `/point.shtml`, `/device-manager`, etc.)
   so they never reach Flask. If a stray script on your network is
   wandering around looking for a Fronius inverter, those requests
   stop at Apache instead of cluttering the journal.

## Notes & gotchas

- **Two readers polling 192.168.11.x simultaneously** is fine — they
  hit different IPs, so there's no bus contention. If you ever moved
  both onto the same RS485 gateway, you'd need to add a shared client
  + lock (see `microgrid_remote_monitor` for that pattern).
- All chart history is in-memory only — restart the service and the
  24-hour window starts empty.
- Polling cadence below 5 s starts to stress the FoxESS LAN dongle
  and the Solis WiFi dongle. 10 s is the sweet spot.
- **Don't put this repo inside Dropbox** — git's index.lock and
  Dropbox's sync agent fight over the same files.

## Dependencies

- Python ≥ 3.9 (tested on 3.10 and 3.13)
- `Flask` ≥ 3.0
- `pymodbus` ≥ 3.6 (uses `device_id=` kwarg; `slave=` for older versions auto-detected)
- Chart.js 4.4 (loaded from cdnjs at runtime)

## License

Same terms as the rest of the project — treat as MIT-style: use,
modify, share at will.
