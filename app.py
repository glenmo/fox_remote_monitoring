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
fox:   FoxModbusReader   = None
solis: SolisModbusReader = None


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
    global fox, solis

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
