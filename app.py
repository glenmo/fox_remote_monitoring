#!/usr/bin/env python3
"""
Fox + Solis Monitor — Flask Web App
====================================
Polls a FoxESS H3 hybrid inverter AND a Solis S6-EH3P hybrid inverter
via Modbus TCP and serves a combined dashboard.

Default deployment:
  * Host:    desky.local (192.168.55.33), Debian + Apache2
  * Fox:     192.168.11.81  : 502  (slave 247, function 0x03, PROT-F)
  * Solis:   192.168.11.214 : 502  (slave   1, function 0x04)
  * Flask:   bound to 127.0.0.1:5000, reverse-proxied by Apache
"""

import argparse
import logging
import os

from flask import Flask, jsonify, render_template

from fox_reader        import FoxModbusReader
from solis_reader      import SolisModbusReader
from solis_http_reader import SolisHttpReader
from fox_logger        import (
    FoxEfficiencyLogger,
    compute_efficiency,
    window_seconds,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("combined_monitor")

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__, template_folder="templates", static_folder="static")

# Global readers (set up in main)
fox:    FoxModbusReader     = None
solis:  SolisModbusReader   = None
logger: FoxEfficiencyLogger = None


# ---------------------------------------------------------------------------
# Routes — Fox (existing endpoints kept for backward compatibility)
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/data")
def api_data():
    """Current decoded Fox values (back-compat — same as /api/fox/data)."""
    if fox is None:
        return jsonify({"error": "Fox reader not initialised"}), 503
    return jsonify(fox.get_data())


@app.route("/api/history")
def api_history():
    if fox is None:
        return jsonify({"error": "Fox reader not initialised"}), 503
    return jsonify(fox.get_history())


@app.route("/api/status")
def api_status():
    if fox is None:
        return jsonify({"error": "Fox reader not initialised"}), 503
    return jsonify(fox.get_status())


# ---------------------------------------------------------------------------
# Routes — explicit per-inverter endpoints
# ---------------------------------------------------------------------------
@app.route("/api/fox/data")
def api_fox_data():
    if fox is None:
        return jsonify({"error": "Fox reader not initialised"}), 503
    return jsonify(fox.get_data())


@app.route("/api/fox/history")
def api_fox_history():
    if fox is None:
        return jsonify({"error": "Fox reader not initialised"}), 503
    return jsonify(fox.get_history())


@app.route("/api/fox/status")
def api_fox_status():
    if fox is None:
        return jsonify({"error": "Fox reader not initialised"}), 503
    return jsonify(fox.get_status())


@app.route("/api/solis/data")
def api_solis_data():
    if solis is None:
        return jsonify({"error": "Solis reader not initialised"}), 503
    return jsonify(solis.get_data())


@app.route("/api/solis/history")
def api_solis_history():
    if solis is None:
        return jsonify({"error": "Solis reader not initialised"}), 503
    return jsonify(solis.get_history())


@app.route("/api/solis/status")
def api_solis_status():
    if solis is None:
        return jsonify({"error": "Solis reader not initialised"}), 503
    return jsonify(solis.get_status())


# ---------------------------------------------------------------------------
# Routes — Efficiency dashboard
# ---------------------------------------------------------------------------
import csv
import io
import time as _time
from datetime import datetime
from flask import request, Response


@app.route("/efficiency")
def efficiency_page():
    return render_template("efficiency.html")


@app.route("/api/efficiency/live")
def api_eff_live():
    """Latest sample plus current poll status — drives the live panel."""
    if fox is None or logger is None:
        return jsonify({"error": "Logger not initialised"}), 503
    return jsonify({
        "fox":    fox.get_data(),
        "sample": logger.latest(),
        "status": fox.get_status(),
    })


@app.route("/api/efficiency/rolling")
def api_eff_rolling():
    """Rolling efficiency over a window (1h, 24h, 7d, 30d, lifetime)."""
    if logger is None:
        return jsonify({"error": "Logger not initialised"}), 503
    window = request.args.get("window", "24h")
    now = int(_time.time())
    secs = window_seconds(window)
    if window == "lifetime":
        start = logger.lifetime_start()
    else:
        start = now - (secs or 86400)
    agg = logger.aggregate(start, now + 1)
    eff = compute_efficiency(agg)
    eff["window"]       = window
    eff["window_start"] = start
    eff["window_end"]   = now
    eff["aggregate"]    = agg
    return jsonify(eff)


@app.route("/api/efficiency/history")
def api_eff_history():
    """Time-series samples in a window for the chart plots."""
    if logger is None:
        return jsonify({"error": "Logger not initialised"}), 503
    window = request.args.get("window", "24h")
    max_points = int(request.args.get("max_points", "1500"))
    now = int(_time.time())
    secs = window_seconds(window) or 86400
    start = (logger.lifetime_start() if window == "lifetime" else now - secs)
    rows = logger.samples_between(start, now + 1, max_points=max_points)
    return jsonify({"window": window, "samples": rows})


# Column order for the CSV export — anything the samples table has but
# isn't listed here is appended at the end so we don't silently drop
# fields if the schema grows.
CSV_PRIMARY_COLS = [
    "timestamp", "ts",
    "mode",
    "soc", "soh",
    "batt_v", "batt_i", "batt_p_dc",
    "batt_temp_amb", "batt_temp_max", "batt_temp_min",
    "cell_min_mv", "cell_max_mv",
    "inv_p_ac", "inv_pf", "inv_freq", "inv_temp",
    "grid_v_r", "grid_v_s", "grid_v_t",
    "grid_i_r", "grid_i_s", "grid_i_t",
    "meter_p", "pv_p", "load_p",
    "e_pv_today", "e_load_today",
    "e_charge_today", "e_discharge_today",
    "e_import_today", "e_feedin_today",
]


@app.route("/api/efficiency/export.csv")
def api_eff_export_csv():
    """Stream the 10 s sample log as CSV for a chosen window."""
    if logger is None:
        return jsonify({"error": "Logger not initialised"}), 503

    window = request.args.get("window", "24h")
    now = int(_time.time())
    secs = window_seconds(window)
    if window == "lifetime":
        start = logger.lifetime_start()
    else:
        start = now - (secs or 86400)

    # Discover the actual column set from the schema so we don't have
    # to keep CSV_PRIMARY_COLS in sync with the table by hand. Anything
    # new on the table shows up at the end of the CSV.
    sample_keys = logger.sample_columns()
    extra  = [k for k in sample_keys if k not in CSV_PRIMARY_COLS and k != "ts"]
    header = CSV_PRIMARY_COLS + extra

    def generate():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(header)
        yield buf.getvalue()
        buf.seek(0); buf.truncate(0)

        for row in logger.samples_iter(start, now + 1):
            d = dict(row)
            ts_unix = d.get("ts")
            iso = datetime.fromtimestamp(ts_unix).isoformat(timespec="seconds") if ts_unix else ""
            line = [iso if c == "timestamp" else d.get(c, "") for c in header]
            writer.writerow(line)
            yield buf.getvalue()
            buf.seek(0); buf.truncate(0)

    start_iso = datetime.fromtimestamp(start).strftime("%Y%m%dT%H%M")
    end_iso   = datetime.fromtimestamp(now).strftime("%Y%m%dT%H%M")
    filename  = f"fox_log_{window}_{start_iso}_to_{end_iso}.csv"
    return Response(
        generate(),
        mimetype="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@app.route("/api/efficiency/modes")
def api_eff_modes():
    """Mode breakdown across the window — share of time and energy per mode."""
    if logger is None:
        return jsonify({"error": "Logger not initialised"}), 503
    window = request.args.get("window", "24h")
    now = int(_time.time())
    secs = window_seconds(window) or 86400
    start = (logger.lifetime_start() if window == "lifetime" else now - secs)
    agg = logger.aggregate(start, now + 1)
    total_secs = (
        agg["discharge_pure_secs"] + agg["discharge_mixed_secs"]
        + agg["charge_pv_secs"] + agg["charge_grid_secs"] + agg["charge_mixed_secs"]
    )
    return jsonify({
        "window": window,
        "modes": {
            "discharge_pure":  {
                "secs": agg["discharge_pure_secs"],
                "dc_wh": agg["discharge_pure_dc_wh"],
                "ac_wh": agg["discharge_pure_ac_wh"],
            },
            "discharge_mixed": {
                "secs": agg["discharge_mixed_secs"],
                "dc_wh": agg["discharge_mixed_dc_wh"],
                "ac_wh": agg["discharge_mixed_ac_wh"],
            },
            "charge_pv":       {
                "secs": agg["charge_pv_secs"],
                "dc_wh": agg["charge_pv_dc_wh"],
            },
            "charge_grid":     {
                "secs": agg["charge_grid_secs"],
                "dc_wh": agg["charge_grid_dc_wh"],
                "ac_wh": agg["charge_grid_ac_wh"],
            },
            "charge_mixed":    {
                "secs": agg["charge_mixed_secs"],
                "dc_wh": agg["charge_mixed_dc_wh"],
                "ac_wh": agg["charge_mixed_ac_wh"],
            },
        },
        "totals": {
            "active_secs":    total_secs,
            "pv_wh":          agg["pv_total_wh"],
            "load_wh":        agg["load_total_wh"],
            "grid_import_wh": agg["grid_import_wh"],
            "grid_export_wh": agg["grid_export_wh"],
        },
    })


# ---------------------------------------------------------------------------
# Editable dashboard message (read live from message.txt)
# ---------------------------------------------------------------------------
MESSAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "message.txt")


@app.route("/api/message")
def api_message():
    try:
        if os.path.exists(MESSAGE_FILE):
            with open(MESSAGE_FILE, "r") as f:
                return jsonify({"message": f.read().strip()})
    except Exception as e:
        log.warning(f"Error reading message file: {e}")
    return jsonify({"message": ""})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    global fox, solis, logger

    parser = argparse.ArgumentParser(
        description="Fox + Solis Combined Monitor — Modbus TCP web dashboard"
    )

    # Web server
    parser.add_argument("--host", default="127.0.0.1",
                        help="Flask listen address (default: 127.0.0.1, served by Apache)")
    parser.add_argument("--port", type=int, default=5000,
                        help="Flask listen port (default: 5000)")

    # Fox H3 inverter
    parser.add_argument("--fox-ip", default="192.168.11.81",
                        help="Fox ESS H3 Modbus TCP IP (default: 192.168.11.81)")
    parser.add_argument("--fox-port", type=int, default=502,
                        help="Fox ESS Modbus TCP port (default: 502)")
    parser.add_argument("--fox-slave", type=int, default=247,
                        help="Fox Modbus slave/device ID (default: 247)")
    parser.add_argument("--fox-poll", type=int, default=10,
                        help="Fox poll interval in seconds (default: 10)")
    parser.add_argument("--no-fox", action="store_true",
                        help="Disable the Fox inverter reader")

    # Solis S6-EH3P inverter
    parser.add_argument("--solis-ip", default="192.168.11.214",
                        help="Solis Modbus TCP IP (default: 192.168.11.214)")
    parser.add_argument("--solis-port", type=int, default=502,
                        help="Solis Modbus TCP port (default: 502)")
    parser.add_argument("--solis-slave", type=int, default=1,
                        help="Solis Modbus slave/device ID (default: 1)")
    parser.add_argument("--solis-poll", type=int, default=10,
                        help="Solis poll interval in seconds (default: 10)")
    parser.add_argument("--no-solis", action="store_true",
                        help="Disable the Solis inverter reader")
    parser.add_argument("--solis-bridge-url", default=None,
                        help="If set, fetch Solis data from another monitor's "
                             "HTTP API at this URL (e.g. http://rubberduck.local:5000) "
                             "instead of polling Modbus directly. Useful when "
                             "another service already owns the Solis dongle's "
                             "single Modbus TCP slot.")

    parser.add_argument("--debug", action="store_true",
                        help="Enable Flask debug mode")

    # Logger
    parser.add_argument("--log-db", default=None,
                        help="SQLite path for the operational log "
                             "(default: ./data/fox_log.sqlite next to app.py, "
                             "overridable with $FOX_LOG_DB)")
    parser.add_argument("--no-log", action="store_true",
                        help="Disable the operational logger / efficiency page")

    # Legacy compatibility for the older Fox-only flags
    parser.add_argument("--inverter-ip",   default=None, help=argparse.SUPPRESS)
    parser.add_argument("--inverter-port", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--slave-id",      type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--poll-interval", type=int, default=None, help=argparse.SUPPRESS)

    args = parser.parse_args()

    # Apply legacy flag values onto the new --fox-* args if provided
    fox_ip    = args.inverter_ip   or args.fox_ip
    fox_port  = args.inverter_port or args.fox_port
    fox_slave = args.slave_id      or args.fox_slave
    fox_poll  = args.poll_interval or args.fox_poll

    if not args.no_fox:
        fox = FoxModbusReader(
            host=fox_ip,
            port=fox_port,
            slave_id=fox_slave,
            poll_interval=fox_poll,
        )
        if not args.no_log:
            try:
                logger = FoxEfficiencyLogger(db_path=args.log_db)
                fox.subscribe(logger.on_sample)
                log.info("Logger: subscribed to Fox poll")
            except Exception as e:
                log.error(f"Logger: init failed, continuing without logging: {e}")
                logger = None
        fox.start()

    if not args.no_solis:
        if args.solis_bridge_url:
            log.info(f"Solis: HTTP bridge mode -> {args.solis_bridge_url}")
            solis = SolisHttpReader(
                host=args.solis_ip,                # kept for compat
                port=args.solis_port,              # kept for compat
                slave_id=args.solis_slave,         # kept for compat
                poll_interval=args.solis_poll,
                bridge_url=args.solis_bridge_url,
            )
        else:
            solis = SolisModbusReader(
                host=args.solis_ip,
                port=args.solis_port,
                slave_id=args.solis_slave,
                poll_interval=args.solis_poll,
            )
        solis.start()

    log.info(f"Starting web server on {args.host}:{args.port}")
    try:
        app.run(host=args.host, port=args.port, debug=args.debug, use_reloader=False)
    except KeyboardInterrupt:
        pass
    finally:
        if fox:
            fox.stop()
        if solis:
            solis.stop()


if __name__ == "__main__":
    main()
