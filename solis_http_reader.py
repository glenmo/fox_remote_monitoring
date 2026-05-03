#!/usr/bin/env python3
"""
Solis HTTP Bridge Reader
========================
Mirrors the public surface of SolisModbusReader (start/stop/get_data/
get_history/get_status) but instead of polling Modbus directly, it
fetches from another instance of microgrid_remote_monitor (or any
service exposing the same /api/data, /api/history, /api/status JSON).

Use this when the Solis is reachable from a *different* host than the
one running fox_remote_monitoring — typically because the Solis
WiFi/LAN dongle only accepts one Modbus TCP client at a time, so you
can't have two services hammering it. Run microgrid on whichever host
is closest to the Solis dongle, then bridge from there.

Layout:

    rubberduck.local:5000             desky.local:5000
    ┌──────────────────────────┐     ┌──────────────────────────┐
    │ microgrid_remote_monitor │     │ fox_remote_monitoring    │
    │   polls Solis via Modbus │ <── │ polls Fox via Modbus,    │
    │   exposes /api/data ...  │     │ proxies Solis via HTTP   │
    └──────────────────────────┘     └──────────────────────────┘
                                              │
                                              ▼  one combined dashboard
                                        http://desky.local/

Constructor signature is intentionally compatible with
SolisModbusReader so app.py can swap one for the other behind a flag.
"""

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

log = logging.getLogger("solis_http_reader")


class SolisHttpReader:
    """Background-polling HTTP bridge to a microgrid_remote_monitor instance."""

    # Match SolisModbusReader's constructor so app.py can swap them transparently.
    # The bridge uses `host` as the bridge URL (e.g. http://rubberduck.local:5000)
    # and ignores port/slave_id/poll_interval semantics from the Modbus side.
    def __init__(self, host, port=5000, slave_id=1, poll_interval=10,
                 bridge_url=None, request_timeout=5):
        if bridge_url:
            self.bridge_url = bridge_url.rstrip("/")
        else:
            # Allow callers that haven't been updated yet to pass host/port
            scheme = "http" if not host.startswith(("http://", "https://")) else ""
            base = host if scheme == "" else f"{scheme}://{host}"
            if ":" not in base.split("//")[-1]:
                base = f"{base}:{port}"
            self.bridge_url = base.rstrip("/")

        self.poll_interval = poll_interval
        self.request_timeout = request_timeout

        # Compatibility fields (some referenced by app.py / dashboard)
        self.host = self.bridge_url
        self.port = port
        self.slave_id = slave_id

        self.connected = False
        self.last_read_time = None
        self.read_errors = 0
        self.total_reads = 0

        self._data = {}
        self._history = {}
        self._upstream_status = {}

        self.lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None

    # -- HTTP helpers -----------------------------------------------------
    def _fetch_json(self, path):
        url = f"{self.bridge_url}{path}"
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=self.request_timeout) as resp:
                if resp.status != 200:
                    log.warning(f"Solis bridge: {url} returned HTTP {resp.status}")
                    return None
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            log.warning(f"Solis bridge: cannot reach {url}: {e.reason}")
            return None
        except (json.JSONDecodeError, ValueError) as e:
            log.warning(f"Solis bridge: bad JSON from {url}: {e}")
            return None
        except Exception as e:
            log.warning(f"Solis bridge: unexpected error fetching {url}: {e}")
            return None

    # -- main poll cycle --------------------------------------------------
    def poll_once(self):
        """Refresh data + status from the upstream microgrid service.

        History is fetched on a slower cadence (every 6 cycles, ~1 minute
        with the default poll_interval) since it doesn't change minute-
        to-minute.
        """
        data = self._fetch_json("/api/data")
        status = self._fetch_json("/api/status")

        # Treat the bridge as connected only if BOTH:
        #   - the HTTP fetch succeeded (we got *something* back), AND
        #   - the upstream reader itself reports connected to its Solis.
        bridge_ok = data is not None and status is not None
        upstream_ok = bool(status and status.get("connected"))

        with self.lock:
            self.connected = bridge_ok and upstream_ok
            self.total_reads += 1
            if not bridge_ok:
                self.read_errors += 1

            if data is not None:
                # Stamp it so the dashboard's last-read display works
                if "_timestamp" not in data:
                    data["_timestamp"] = datetime.now().isoformat()
                self._data = data
                self.last_read_time = datetime.now()
            if status is not None:
                self._upstream_status = status

        # Fetch history less frequently (it's an O(N) payload)
        if self.total_reads % 6 == 1:
            history = self._fetch_json("/api/history")
            if history is not None:
                with self.lock:
                    self._history = history

    def _poll_loop(self):
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception as e:
                log.error(f"Solis bridge: poll error: {e}")
            self._stop_event.wait(self.poll_interval)

    # -- public API -------------------------------------------------------
    def start(self):
        log.info(f"Solis bridge: polling {self.bridge_url} every {self.poll_interval}s")
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)

    def get_data(self):
        with self.lock:
            return dict(self._data)

    def get_history(self):
        with self.lock:
            # Defensive copy so dashboard mutations can't corrupt our cache
            return {k: list(v) if isinstance(v, list) else v
                    for k, v in self._history.items()}

    def get_status(self):
        """Return a status dict in the same shape as SolisModbusReader.

        Importantly, `connected` reflects "is the bridged data trustworthy
        right now?" — i.e. both the HTTP hop AND the upstream Modbus poll
        succeeded. The dashboard uses this to decide whether to show
        "Solis disconnected" or live data.
        """
        with self.lock:
            up = dict(self._upstream_status)
            return {
                "connected":     self.connected,
                "host":          f"{self.bridge_url} (bridged)",
                "port":          self.port,
                "slave_id":      self.slave_id,
                "poll_interval": self.poll_interval,
                "total_reads":   self.total_reads,
                "read_errors":   self.read_errors,
                "last_read":     self.last_read_time.isoformat() if self.last_read_time else None,
                "upstream":      {
                    "connected": up.get("connected"),
                    "host":      up.get("host"),
                    "last_read": up.get("last_read"),
                },
            }
