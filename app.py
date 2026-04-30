#!/usr/bin/env python3
"""
Fox ESS Monitor — Flask Web App
================================
Polls a FoxESS H3 Pro hybrid inverter via Modbus TCP and serves a live
monitoring dashboard.

Default deployment:
  * Host:     desky.local (192.168.55.33), Debian + Apache2
  * Inverter: 192.168.11.81 : 502  (slave 247)
  * Flask:    bound to 127.0.0.1:5000, reverse-proxied by Apache
"""

import argparse
import logging
import os

from flask import Flask, jsonify, render_template

from fox_reader import FoxModbusReader

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("fox_monitor")

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__, template_folder="templates", static_folder="static")

# Global reader (set up in main)
reader: FoxModbusReader = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    """Serve the dashboard page."""
    return render_template("dashboard.html")


@app.route("/api/data")
def api_data():
    """Current decoded inverter values."""
    if reader is None:
        return jsonify({"error": "Reader not initialised"}), 503
    return jsonify(reader.get_data())


@app.route("/api/history")
def api_history():
    """24-hour rolling history for charts."""
    if reader is None:
        return jsonify({"error": "Reader not initialised"}), 503
    return jsonify(reader.get_history())


@app.route("/api/status")
def api_status():
    """Connection / polling status."""
    if reader is None:
        return jsonify({"error": "Reader not initialised"}), 503
    return jsonify(reader.get_status())


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
    global reader

    parser = argparse.ArgumentParser(
        description="Fox ESS Monitor — H3 Pro Modbus TCP web dashboard"
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="Flask listen address (default: 127.0.0.1, served by Apache)")
    parser.add_argument("--port", type=int, default=5000,
                        help="Flask listen port (default: 5000)")
    parser.add_argument("--inverter-ip", default="192.168.11.81",
                        help="Fox ESS inverter Modbus TCP IP (default: 192.168.11.81)")
    parser.add_argument("--inverter-port", type=int, default=502,
                        help="Fox ESS Modbus TCP port (default: 502)")
    parser.add_argument("--slave-id", type=int, default=247,
                        help="Modbus slave/device ID (default: 247 — H3 Pro stock)")
    parser.add_argument("--poll-interval", type=int, default=10,
                        help="Poll interval in seconds (default: 10)")
    parser.add_argument("--debug", action="store_true",
                        help="Enable Flask debug mode")
    args = parser.parse_args()

    reader = FoxModbusReader(
        host=args.inverter_ip,
        port=args.inverter_port,
        slave_id=args.slave_id,
        poll_interval=args.poll_interval,
    )
    reader.start()

    log.info(f"Starting web server on {args.host}:{args.port}")
    try:
        app.run(host=args.host, port=args.port, debug=args.debug, use_reloader=False)
    except KeyboardInterrupt:
        pass
    finally:
        if reader:
            reader.stop()


if __name__ == "__main__":
    main()
