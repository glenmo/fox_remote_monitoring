#!/bin/bash
# ============================================================
# Fox ESS Monitor — Debian / Apache2 Setup
# ============================================================
# Target host: desky.local  (192.168.55.33)  Debian + Apache2
# Run on the server:    bash install.sh
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

FLASK_PORT="${FLASK_PORT:-5000}"

INSTALL_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
VENV_DIR="$INSTALL_DIR/venv"
SERVICE_FILE="/etc/systemd/system/fox-monitor.service"
APACHE_CONF="/etc/apache2/sites-available/fox-monitor.conf"

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
    --fox-ip $FOX_IP \\
    --fox-port $FOX_PORT \\
    --fox-slave $FOX_SLAVE \\
    --fox-poll $FOX_POLL \\
    --solis-ip $SOLIS_IP \\
    --solis-port $SOLIS_PORT \\
    --solis-slave $SOLIS_SLAVE \\
    --solis-poll $SOLIS_POLL
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
echo "[4/5] Configuring Apache reverse proxy on desky.local ..."
sudo a2enmod proxy proxy_http headers >/dev/null

# Copy the bundled site config (or create one if missing)
if [ -f "$INSTALL_DIR/fox-monitor.conf" ]; then
    sudo cp "$INSTALL_DIR/fox-monitor.conf" "$APACHE_CONF"
else
    sudo tee "$APACHE_CONF" > /dev/null <<APACHEEOF
<VirtualHost *:80>
    ServerName desky.local
    ServerAlias 192.168.55.33

    ProxyPreserveHost On
    ProxyPass        / http://127.0.0.1:$FLASK_PORT/
    ProxyPassReverse / http://127.0.0.1:$FLASK_PORT/

    # Don't cache the JSON API
    <LocationMatch "^/api/">
        Header set Cache-Control "no-store, no-cache, must-revalidate"
    </LocationMatch>

    ErrorLog \${APACHE_LOG_DIR}/fox-monitor-error.log
    CustomLog \${APACHE_LOG_DIR}/fox-monitor-access.log combined
</VirtualHost>
APACHEEOF
fi

sudo a2ensite fox-monitor.conf >/dev/null

# Disable the default site if it's still on (it conflicts with port 80)
if [ -f /etc/apache2/sites-enabled/000-default.conf ]; then
    sudo a2dissite 000-default.conf >/dev/null
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
echo " Fox H3 target   : $FOX_IP:$FOX_PORT  (slave $FOX_SLAVE, poll ${FOX_POLL}s)"
echo " Solis target    : $SOLIS_IP:$SOLIS_PORT (slave $SOLIS_SLAVE, poll ${SOLIS_POLL}s)"
echo " Flask backend   : http://127.0.0.1:$FLASK_PORT"
echo " Dashboard URL   : http://desky.local/   (or http://192.168.55.33/)"
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
