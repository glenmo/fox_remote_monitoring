#!/bin/bash
# ============================================================
# Fox + Solis Combined Monitor — Debian / Ubuntu / Pi installer
# ============================================================
# Works on:
#   * Ubuntu / Debian server (e.g. desky.local)
#   * Raspberry Pi OS         (e.g. rubberduck.local)
# Run on the target host:   bash install.sh
# ============================================================

set -e

echo "============================================"
echo " Fox + Solis Combined Monitor — Server Setup"
echo "============================================"

# ----- Parameters (override via env if you like) -------------
# Fox H3 inverter
FOX_IP="${FOX_IP:-${INV_IP:-192.168.11.81}}"      # back-compat with old INV_IP
FOX_PORT="${FOX_PORT:-${INV_PORT:-502}}"
FOX_SLAVE="${FOX_SLAVE:-${SLAVE_ID:-247}}"
FOX_POLL="${FOX_POLL:-${POLL:-10}}"

# Solis S6-EH3P inverter
SOLIS_IP="${SOLIS_IP:-192.168.11.214}"
SOLIS_PORT="${SOLIS_PORT:-502}"
SOLIS_SLAVE="${SOLIS_SLAVE:-1}"
SOLIS_POLL="${SOLIS_POLL:-10}"

# Per-inverter disable flags. Set to 1 to skip that inverter entirely.
# Useful when another service (e.g. microgrid_remote_monitor) is already
# polling one of the inverters and you don't want to fight it for the
# dongle's single Modbus TCP slot.
#
#   NO_SOLIS=1 bash install.sh   -> fox+solis service polls Fox only
#   NO_FOX=1   bash install.sh   -> fox+solis service polls Solis only
NO_FOX="${NO_FOX:-0}"
NO_SOLIS="${NO_SOLIS:-0}"

# Optional: bridge Solis from another monitor's HTTP API instead of
# polling Modbus directly. Useful when microgrid_remote_monitor on a
# different host already owns the Solis dongle's single TCP slot.
#   SOLIS_BRIDGE_URL=http://rubberduck.local:5000 bash install.sh
SOLIS_BRIDGE_URL="${SOLIS_BRIDGE_URL:-}"

FLASK_PORT="${FLASK_PORT:-5000}"

# Hostname used in the Apache vhost — auto-detected so the same script
# deploys cleanly on desky.local AND rubberduck.local without edits.
# Override with SERVER_NAME=foo.local bash install.sh
SERVER_NAME="${SERVER_NAME:-$(hostname).local}"
SERVER_IP="${SERVER_IP:-$(hostname -I 2>/dev/null | awk '{print $1}')}"

INSTALL_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
VENV_DIR="$INSTALL_DIR/venv"
SERVICE_FILE="/etc/systemd/system/fox-monitor.service"
APACHE_CONF="/etc/apache2/sites-available/fox-monitor.conf"

# Warn if microgrid_remote_monitor (the older project) is currently bound
# to the same Flask port — they would conflict.
if sudo -n systemctl is-active --quiet microgrid-monitor 2>/dev/null; then
    echo ""
    echo "  WARNING: microgrid-monitor.service is currently RUNNING on this host."
    echo "  The Solis reader in fox+solis combined will collide with it on"
    echo "  Flask port $FLASK_PORT and on Modbus polling of the Solis gateway."
    echo "  Recommended: stop and disable it before continuing:"
    echo "      sudo systemctl stop microgrid-monitor"
    echo "      sudo systemctl disable microgrid-monitor"
    echo ""
    read -r -p "  Continue anyway? [y/N] " ans
    case "$ans" in
        y|Y|yes|YES) echo "  proceeding ..." ;;
        *) echo "  aborting."; exit 1 ;;
    esac
fi

# ----- 1. System packages ------------------------------------
echo ""
echo "[1/5] Installing system packages (python3, venv, apache2)..."
sudo apt-get update -qq
sudo apt-get install -y python3 python3-pip python3-venv apache2

# ----- 2. Python venv + deps ---------------------------------
echo ""
echo "[2/5] Creating Python virtual environment at $VENV_DIR..."
python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"
pip install --upgrade pip
pip install -r "$INSTALL_DIR/requirements.txt"
deactivate

# ----- 3. systemd service ------------------------------------
echo ""
echo "[3/5] Installing systemd service..."

# Build the per-inverter argument blocks. If an inverter is disabled,
# emit a bare --no-foo flag instead of its IP/port/slave/poll args.
if [ "$NO_FOX" = "1" ]; then
    FOX_ARGS="    --no-fox"
    echo "  (NO_FOX=1) — fox-monitor service will skip the Fox inverter."
else
    FOX_ARGS="    --fox-ip $FOX_IP \\
    --fox-port $FOX_PORT \\
    --fox-slave $FOX_SLAVE \\
    --fox-poll $FOX_POLL"
fi

if [ "$NO_SOLIS" = "1" ]; then
    SOLIS_ARGS="    --no-solis"
    echo "  (NO_SOLIS=1) — fox-monitor service will skip the Solis inverter."
    echo "                 (Solis polling is presumably handled by another service.)"
elif [ -n "$SOLIS_BRIDGE_URL" ]; then
    SOLIS_ARGS="    --solis-bridge-url $SOLIS_BRIDGE_URL \\
    --solis-poll $SOLIS_POLL"
    echo "  (SOLIS_BRIDGE_URL set) — fox-monitor will proxy Solis data from"
    echo "                           $SOLIS_BRIDGE_URL  (no direct Modbus polling)."
else
    SOLIS_ARGS="    --solis-ip $SOLIS_IP \\
    --solis-port $SOLIS_PORT \\
    --solis-slave $SOLIS_SLAVE \\
    --solis-poll $SOLIS_POLL"
fi

sudo tee "$SERVICE_FILE" > /dev/null <<SERVICEEOF
[Unit]
Description=Fox + Solis Combined Monitor (Modbus -> Flask)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$VENV_DIR/bin/python app.py \\
    --host 127.0.0.1 \\
    --port $FLASK_PORT \\
$FOX_ARGS \\
$SOLIS_ARGS
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SERVICEEOF

sudo systemctl daemon-reload
sudo systemctl enable fox-monitor.service

# ----- 4. Apache reverse-proxy vhost -------------------------
echo ""
echo "[4/5] Configuring Apache reverse proxy on $SERVER_NAME ..."
sudo a2enmod proxy proxy_http headers >/dev/null

# Always generate the vhost from this script so the right ServerName /
# ServerAlias / Flask port land in it for THIS host. The bundled
# fox-monitor.conf in the repo is used as a body template (we keep its
# <LocationMatch> Fronius block intact); the <VirtualHost> wrapper is
# rewritten here.
sudo tee "$APACHE_CONF" > /dev/null <<APACHEEOF
<VirtualHost *:80>
    ServerName  $SERVER_NAME
    ServerAlias $SERVER_IP

    # Drop legacy Fronius DataManager UI probes — these aren't us.
    <LocationMatch "^/(img/Fronius-Logo|uiLib/|src/style/|product/list|point\\.shtml|favicon\\.ico|device-manager)">
        Require all denied
    </LocationMatch>

    # Reverse proxy to the Flask backend
    ProxyPreserveHost On
    ProxyRequests     Off
    ProxyPass        / http://127.0.0.1:$FLASK_PORT/
    ProxyPassReverse / http://127.0.0.1:$FLASK_PORT/

    # Don't cache the JSON API
    <LocationMatch "^/api/">
        Header set Cache-Control "no-store, no-cache, must-revalidate"
    </LocationMatch>

    ErrorLog  \${APACHE_LOG_DIR}/fox-monitor-error.log
    CustomLog \${APACHE_LOG_DIR}/fox-monitor-access.log combined
</VirtualHost>
APACHEEOF

sudo a2ensite fox-monitor.conf >/dev/null

# Be conservative on systems that already have other monitoring vhosts
# (e.g. rubberduck.local already runs microgrid_remote_monitor). Only
# disable Apache's stock 000-default if no other custom vhost is on.
existing_sites=$(sudo find /etc/apache2/sites-enabled -maxdepth 1 -type l \
                  ! -name "000-default.conf" ! -name "fox-monitor.conf" 2>/dev/null | wc -l)
if [ -f /etc/apache2/sites-enabled/000-default.conf ] && [ "$existing_sites" -eq 0 ]; then
    echo "  Disabling Apache's stock 000-default.conf (no other custom vhosts on this host)"
    sudo a2dissite 000-default.conf >/dev/null
elif [ "$existing_sites" -gt 0 ]; then
    echo "  Detected $existing_sites other custom Apache vhost(s). Leaving them alone."
    echo "  fox-monitor.conf will respond for ServerName=$SERVER_NAME only."
fi

sudo apache2ctl configtest
sudo systemctl reload apache2

# ----- 5. Start service --------------------------------------
echo ""
echo "[5/5] Starting fox-monitor service..."
sudo systemctl restart fox-monitor.service
sleep 2
sudo systemctl --no-pager status fox-monitor.service | head -n 10 || true

echo ""
echo "============================================"
echo " Setup complete!"
echo "============================================"
echo ""
if [ "$NO_FOX" = "1" ]; then
    echo " Fox H3 target   : (DISABLED via NO_FOX=1)"
else
    echo " Fox H3 target   : $FOX_IP:$FOX_PORT  (slave $FOX_SLAVE, poll ${FOX_POLL}s)"
fi
if [ "$NO_SOLIS" = "1" ]; then
    echo " Solis target    : (DISABLED via NO_SOLIS=1 — handled by another service)"
elif [ -n "$SOLIS_BRIDGE_URL" ]; then
    echo " Solis target    : (HTTP-bridged from $SOLIS_BRIDGE_URL, poll ${SOLIS_POLL}s)"
else
    echo " Solis target    : $SOLIS_IP:$SOLIS_PORT (slave $SOLIS_SLAVE, poll ${SOLIS_POLL}s)"
fi
echo " Flask backend   : http://127.0.0.1:$FLASK_PORT"
echo " Dashboard URL   : http://$SERVER_NAME/   (or http://$SERVER_IP/)"
echo ""
echo " Useful commands:"
echo "   sudo systemctl status  fox-monitor"
echo "   sudo journalctl -u fox-monitor -f"
echo "   sudo systemctl restart fox-monitor"
echo ""
echo " To change inverter IPs later, edit $SERVICE_FILE then:"
echo "   sudo systemctl daemon-reload && sudo systemctl restart fox-monitor"
echo ""
echo " To temporarily disable one inverter, add --no-fox or --no-solis"
echo " to ExecStart in $SERVICE_FILE"
echo ""
