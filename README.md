# Fox ESS H3 Pro Monitor

A live monitoring web dashboard for the **FoxESS H3 Pro** 3-phase hybrid
inverter, polled directly over **Modbus TCP** and served from a Debian box
behind **Apache 2**.

```
   ┌──────────────────┐    Modbus/TCP     ┌────────────────────────┐
   │  Fox H3 Pro      │ <───────────────  │  desky.local           │
   │  192.168.11.81   │   (port 502)      │  192.168.55.33         │
   └──────────────────┘                   │  ├─ Flask app :5000    │
                                          │  └─ Apache2 :80 →:5000 │
                                          └────────────────────────┘
                                                       │
                                                       ▼
                                              http://desky.local/
```

## What you get

- A single-page dashboard with:
  - Battery SoC gauge (with charge/discharge direction badge)
  - Power-flow tiles: PV, Grid, Battery, Load, EPS, Frequency
  - 3-phase grid voltages and inverter currents
  - PV string detail (V / A / W per string)
  - Battery & BMS detail (cell min/max, temps, SoH)
  - Energy totals (today / lifetime) for PV, feed-in, import, charge,
    discharge and load
  - Active alarms decoded from Alarm 1 / 2 / 3 bitfields
  - 24-hour charts for power flow and SoC + grid frequency
- A small JSON API for integrating with anything else (`/api/data`,
  `/api/history`, `/api/status`)

## Quick start

```bash
# 1. Drop the project into the user's home on desky.local
scp -r "Fox ESS Monitor" you@desky.local:~/fox-monitor/

# 2. Run the installer
ssh you@desky.local
cd ~/fox-monitor
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
| Inverter port | `502`           | `INV_PORT=…` |
| Modbus slave ID | `247`         | `SLAVE_ID=…` |
| Poll interval | `10` s         | `POLL=…` |
| Flask port | `5000`            | `FLASK_PORT=…` |

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
  --inverter-ip      Fox H3 Pro IP address         (default: 192.168.11.81)
  --inverter-port    Modbus TCP port               (default: 502)
  --slave-id         Modbus slave/device ID        (default: 247)
  --poll-interval    Poll interval in seconds      (default: 10)
  --debug            Enable Flask debug mode
```

## Modbus register coverage (Fox V1.05.03.00)

The reader uses **function code 0x03 (Read Holding Registers)** which is
what the FoxESS spec mandates.

Implemented blocks:

| Block | Range | Source table |
|---|---|---|
| Inverter live data | 39000–39423 | Table 3-5 |
| Energy totals | 39600–39631 | Table 3-6 |
| BMS1 metrics | 37609–37635 | Table 3-3 |
| Meter1 / CT1 | 38801–38846 | Table 3-4 |

The full register list (with type, gain, units, description) lives at the
top of `fox_reader.py`. Add or remove rows there — they are picked up
automatically by both the polling loop and the JSON API.

Bitfield decoders are included for:

- **Status1** (running / standby / fault)
- **Status3** (off-grid)
- **Alarm1 / Alarm2 / Alarm3** (Table 4-1)

## API

| Endpoint | Description |
|---|---|
| `GET /` | Dashboard |
| `GET /api/data` | All current decoded values + alarm strings |
| `GET /api/history` | 24-h rolling 1-minute samples for charts |
| `GET /api/status` | Connection / poll-rate / error counters |
| `GET /api/message` | Contents of `message.txt` (banner text) |

## Files

```
Fox ESS Monitor/
├── app.py              Flask app + REST API
├── fox_reader.py       Modbus TCP reader & register map
├── requirements.txt    Python deps
├── install.sh          One-shot Debian + Apache installer
├── fox-monitor.conf    Apache vhost
├── message.txt         Optional banner text shown at the bottom
├── templates/
│   └── dashboard.html  Web UI (Chart.js, no build step)
└── README.md
```

## Notes & gotchas

- The H3 Pro stock Modbus slave ID is **247**, not 1. If you have
  changed it via the FoxESS app, update `--slave-id`.
- Function code is **0x03**, not 0x04 — the FoxESS spec is unusual
  among hybrid inverters in using holding registers for live data.
- **Gain** is a divisor: `value = raw / gain`. Watch out for kW with
  gain 1000, which ends up at watt resolution.
- Battery sign convention: **register 39162 > 0 charging, < 0 discharging**.
- All chart data is in memory only — restart the service and the
  24-hour history starts empty again.

## Dependencies

- Python ≥ 3.9
- `Flask` ≥ 3.0
- `pymodbus` ≥ 3.6 (uses the `device_id=` kwarg on `read_holding_registers`)
- Chart.js 4.4 (loaded from cdnjs at runtime)
