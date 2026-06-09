#!/usr/bin/env python3
"""
Fox H3 — Operational Logger + Efficiency Calculator
====================================================
Stores 10 s samples of every operational quantity needed to compute
end-to-end inverter efficiency:

    AC-AC round-trip       =  energy_returned_to_AC / energy_pulled_from_AC
    DC-AC discharge eff.   =  energy_out_at_AC_port / energy_out_of_battery_DC
    DC-AC charging  eff.   =  energy_into_battery_DC / energy_in_at_AC_port

To attribute energy cleanly between PV-only, grid-only and mixed-source
operation, each sample is classified into a mode and energy is
trapezoidally integrated between samples into hourly buckets.

Storage: a single SQLite database (default
/var/lib/fox-monitor/fox_log.sqlite, overridable with $FOX_LOG_DB).
WAL mode + a single writer thread make concurrent reads safe.
"""

import logging
import math
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

log = logging.getLogger("fox_logger")

# ---------------------------------------------------------------------------
# Mode classification thresholds
# ---------------------------------------------------------------------------
# Below these the channel is considered "off" for mode-classification
# purposes — avoids tiny stand-by currents pulling samples into the wrong
# bucket.
BATT_IDLE_W   = 50.0     # |P_batt| below this counts as idle
PV_IDLE_W     = 100.0    # PV below this counts as off
GRID_IDLE_W   = 100.0    # |P_meter| below this counts as off

MODES = (
    "idle",
    "discharge_pure",      # batt out, no significant PV
    "discharge_mixed",     # batt out + PV both contributing
    "charge_pv",           # batt in, no significant grid import
    "charge_grid",         # batt in, no significant PV, grid importing
    "charge_mixed",        # batt in, PV + grid both contributing
    "passthrough",         # batt idle, PV/grid/load all moving
)


def classify_mode(p_batt_w, p_pv_w, p_meter_w):
    """
    Sign conventions (canonical, as exposed by FoxModbusReader):
        p_batt_w  > 0 charging,  < 0 discharging
        p_pv_w   >= 0
        p_meter_w > 0 exporting, < 0 importing
    """
    if p_batt_w is None or p_pv_w is None or p_meter_w is None:
        return "idle"

    if abs(p_batt_w) < BATT_IDLE_W:
        return "passthrough" if (p_pv_w > PV_IDLE_W or abs(p_meter_w) > GRID_IDLE_W) else "idle"

    if p_batt_w < 0:                              # discharging
        return "discharge_pure" if p_pv_w < PV_IDLE_W else "discharge_mixed"

    # charging
    pv_on    = p_pv_w > PV_IDLE_W
    grid_in  = p_meter_w < -GRID_IDLE_W           # importing
    if pv_on and grid_in:    return "charge_mixed"
    if pv_on:                return "charge_pv"
    if grid_in:              return "charge_grid"
    # Charging without PV and without grid import is anomalous (only
    # possible if the meter has lost sync). Treat as grid charge so the
    # AC-side integral still has a denominator to land in.
    return "charge_grid"


# ---------------------------------------------------------------------------
# SQLite schema
# ---------------------------------------------------------------------------
SCHEMA_SAMPLES = """
CREATE TABLE IF NOT EXISTS samples (
    ts            INTEGER PRIMARY KEY,   -- unix seconds (UTC)

    -- Battery DC side
    batt_v        REAL,                  -- V
    batt_i        REAL,                  -- A (signed, +charging)
    batt_p_dc     REAL,                  -- W (signed, +charging)  -- canonical battery_flow_w
    batt_temp_amb REAL,                  -- °C
    batt_temp_max REAL,                  -- °C
    batt_temp_min REAL,                  -- °C
    soc           REAL,                  -- %
    soh           REAL,                  -- %
    cell_max_mv   INTEGER,
    cell_min_mv   INTEGER,

    -- AC inverter port
    inv_p_ac      REAL,                  -- W (signed, +inverter->AC, -AC->inverter)
    inv_pf        REAL,                  -- power factor
    inv_freq      REAL,                  -- Hz
    inv_temp      REAL,                  -- °C

    -- Grid AC (per phase)
    grid_v_r      REAL, grid_v_s REAL, grid_v_t REAL,
    grid_i_r      REAL, grid_i_s REAL, grid_i_t REAL,
    meter_p       REAL,                  -- W (+export, -import)

    -- PV / load
    pv_p          REAL,                  -- W
    load_p        REAL,                  -- W

    -- Inverter energy counters (kWh, cumulative)
    e_charge_today    REAL,
    e_discharge_today REAL,
    e_import_today    REAL,
    e_feedin_today    REAL,
    e_pv_today        REAL,
    e_load_today      REAL,

    -- Computed
    mode TEXT
);
"""

SCHEMA_INDEX = "CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);"

SCHEMA_BUCKETS = """
CREATE TABLE IF NOT EXISTS energy_buckets (
    bucket_hour  INTEGER PRIMARY KEY,    -- unix ts at the top of the hour (UTC)

    -- Integrated energies in Wh, segregated by mode
    discharge_pure_dc_wh   REAL DEFAULT 0,
    discharge_pure_ac_wh   REAL DEFAULT 0,
    discharge_pure_secs    INTEGER DEFAULT 0,

    discharge_mixed_dc_wh  REAL DEFAULT 0,
    discharge_mixed_ac_wh  REAL DEFAULT 0,
    discharge_mixed_secs   INTEGER DEFAULT 0,

    charge_pv_dc_wh        REAL DEFAULT 0,
    charge_pv_secs         INTEGER DEFAULT 0,

    charge_grid_dc_wh      REAL DEFAULT 0,
    charge_grid_ac_wh      REAL DEFAULT 0,
    charge_grid_secs       INTEGER DEFAULT 0,

    charge_mixed_dc_wh     REAL DEFAULT 0,
    charge_mixed_ac_wh     REAL DEFAULT 0,
    charge_mixed_secs      INTEGER DEFAULT 0,

    -- Totals (any mode) for the system-totals tile
    pv_total_wh            REAL DEFAULT 0,
    load_total_wh          REAL DEFAULT 0,
    grid_import_wh         REAL DEFAULT 0,
    grid_export_wh         REAL DEFAULT 0,

    samples                INTEGER DEFAULT 0
);
"""

# Columns we trapezoidally integrate between consecutive samples; each
# entry is (bucket_column, sample_field, sign_filter). sign_filter:
#   'pos'  → only positive part of the field contributes
#   'neg'  → only negative part contributes, value stored as |x|
#   None   → take the value as-is
INTEGRATE_PER_MODE = {
    # mode: list of (bucket_col, sample_field, transform)
    "discharge_pure": [
        ("discharge_pure_dc_wh",  "batt_p_dc",  "neg_abs"),   # discharging → -ve, store |x|
        ("discharge_pure_ac_wh",  "inv_p_ac",   "pos"),       # inverter sending to AC → +ve
    ],
    "discharge_mixed": [
        ("discharge_mixed_dc_wh", "batt_p_dc",  "neg_abs"),
        ("discharge_mixed_ac_wh", "inv_p_ac",   "pos"),
    ],
    "charge_pv": [
        ("charge_pv_dc_wh",       "batt_p_dc",  "pos"),
    ],
    "charge_grid": [
        ("charge_grid_dc_wh",     "batt_p_dc",  "pos"),
        ("charge_grid_ac_wh",     "inv_p_ac",   "neg_abs"),   # AC into inverter → -ve, store |x|
    ],
    "charge_mixed": [
        ("charge_mixed_dc_wh",    "batt_p_dc",  "pos"),
        ("charge_mixed_ac_wh",    "inv_p_ac",   "neg_abs"),
    ],
}

# Always-integrated totals (regardless of mode)
INTEGRATE_TOTALS = [
    ("pv_total_wh",     "pv_p",   "pos"),
    ("load_total_wh",   "load_p", "pos"),
    ("grid_import_wh",  "meter_p", "neg_abs"),
    ("grid_export_wh",  "meter_p", "pos"),
]


def _signed(value, filt):
    if value is None:
        return 0.0
    if filt == "pos":      return value if value > 0 else 0.0
    if filt == "neg_abs":  return -value if value < 0 else 0.0
    return value


# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------
class FoxEfficiencyLogger:
    """
    Writes one row per Fox poll into SQLite, plus rolls up energy
    integrals into hourly buckets. Thread-safe for reads from Flask
    while a single writer thread (the Fox poll loop callback) is
    appending.
    """

    # Default sits next to the app code so the service user can write
    # without root setup. Override with --log-db or $FOX_LOG_DB.
    DEFAULT_DB_PATH = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "data", "fox_log.sqlite"
    )

    def __init__(self, db_path=None):
        self.db_path = db_path or os.environ.get("FOX_LOG_DB") or self.DEFAULT_DB_PATH
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.Lock()
        self._prev_sample = None    # (ts, mode, field_dict) for trapezoidal integration
        self._init_db()
        log.info(f"Logger: SQLite at {self.db_path}")

    def _connect(self):
        # check_same_thread=False because Flask reads from a different
        # thread than the Fox poll loop. We serialise writes with self._lock.
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute(SCHEMA_SAMPLES)
            conn.execute(SCHEMA_INDEX)
            conn.execute(SCHEMA_BUCKETS)
            conn.commit()
        finally:
            conn.close()

    # -----------------------------------------------------------------
    # Field extraction — translate the Fox reader's data dict to our
    # narrower sample schema. Tolerates missing keys.
    # -----------------------------------------------------------------
    @staticmethod
    def _extract(data):
        g = data.get
        def f(*names):
            for n in names:
                v = g(n)
                if v is not None:
                    return float(v)
            return None

        return {
            "batt_v":        f("battery1_voltage", "bms1_voltage"),
            "batt_i":        f("battery1_current", "bms1_current"),
            "batt_p_dc":     f("battery_flow_w", "battery1_power"),
            "batt_temp_amb": f("bms1_ambient_temp"),
            "batt_temp_max": f("bms1_max_temp"),
            "batt_temp_min": f("bms1_min_temp"),
            "soc":           f("system_soc", "bms1_soc"),
            "soh":           f("bms1_soh"),
            "cell_max_mv":   int(g("bms1_max_cell_mv")) if g("bms1_max_cell_mv") is not None else None,
            "cell_min_mv":   int(g("bms1_min_cell_mv")) if g("bms1_min_cell_mv") is not None else None,

            # active_power is kW; multiply.
            "inv_p_ac":      (f("active_power") or 0) * 1000.0 if g("active_power") is not None else None,
            "inv_pf":        f("power_factor"),
            "inv_freq":      f("grid_frequency"),
            "inv_temp":      f("internal_temp"),

            "grid_v_r":      f("grid_voltage_r"),
            "grid_v_s":      f("grid_voltage_s"),
            "grid_v_t":      f("grid_voltage_t"),
            "grid_i_r":      f("inv_current_r"),
            "grid_i_s":      f("inv_current_s"),
            "grid_i_t":      f("inv_current_t"),
            "meter_p":       f("meter_active_power"),

            "pv_p":          (f("pv_total_power") or 0) * 1000.0 if g("pv_total_power") is not None else None,
            "load_p":        f("load_power_total"),

            "e_charge_today":    f("charge_today_kwh"),
            "e_discharge_today": f("discharge_today_kwh"),
            "e_import_today":    f("import_today_kwh"),
            "e_feedin_today":    f("feedin_today_kwh"),
            "e_pv_today":        f("pv_today_kwh"),
            "e_load_today":      f("load_today_kwh"),
        }

    # -----------------------------------------------------------------
    # Subscriber callback — called by FoxModbusReader after each poll
    # -----------------------------------------------------------------
    def on_sample(self, data):
        ts_iso = data.get("_timestamp")
        if not ts_iso:
            return
        try:
            ts_dt  = datetime.fromisoformat(ts_iso)
            ts_unix = int(ts_dt.timestamp())
        except ValueError:
            return

        fields = self._extract(data)
        mode   = classify_mode(fields["batt_p_dc"], fields["pv_p"], fields["meter_p"])

        try:
            with self._lock:
                self._write_sample(ts_unix, fields, mode)
                self._update_buckets(ts_unix, fields, mode)
        except Exception as e:
            log.error(f"Logger: write failed: {e}")

    # -----------------------------------------------------------------
    # Sample insert
    # -----------------------------------------------------------------
    def _write_sample(self, ts_unix, fields, mode):
        cols = ["ts"] + list(fields.keys()) + ["mode"]
        vals = [ts_unix] + list(fields.values()) + [mode]
        placeholders = ",".join("?" * len(cols))
        sql = f"INSERT OR REPLACE INTO samples ({','.join(cols)}) VALUES ({placeholders})"
        conn = self._connect()
        try:
            conn.execute(sql, vals)
            conn.commit()
        finally:
            conn.close()

    # -----------------------------------------------------------------
    # Trapezoidal energy integration into hourly buckets
    # -----------------------------------------------------------------
    def _update_buckets(self, ts_unix, fields, mode):
        prev = self._prev_sample
        self._prev_sample = (ts_unix, mode, fields)
        if prev is None:
            return
        prev_ts, prev_mode, prev_fields = prev
        dt_s = ts_unix - prev_ts
        if dt_s <= 0 or dt_s > 120:           # gap too long → don't integrate over the gap
            return

        # Hour bucket (UTC) the interval falls into. If the interval
        # crosses an hour boundary we just attribute everything to the
        # *end* hour — at 10 s cadence this is at most 10 s of slop per
        # hour, well below noise.
        bucket = (ts_unix // 3600) * 3600
        hours = dt_s / 3600.0

        # Build delta updates
        deltas = {}

        # Per-mode integrals — only count seconds when the mode held
        # steady across the interval (sample-to-sample). Mixed
        # transitions are still useful but get attributed to the *new*
        # mode since the secs/duration column represents the interval
        # ending here.
        spec = INTEGRATE_PER_MODE.get(mode, [])
        for bucket_col, field, filt in spec:
            cur = _signed(fields[field], filt)
            prv = _signed(prev_fields.get(field), filt)
            wh = (cur + prv) / 2.0 * hours
            deltas[bucket_col] = deltas.get(bucket_col, 0.0) + wh

        # Mode duration in this bucket
        secs_col = {
            "discharge_pure":  "discharge_pure_secs",
            "discharge_mixed": "discharge_mixed_secs",
            "charge_pv":       "charge_pv_secs",
            "charge_grid":     "charge_grid_secs",
            "charge_mixed":    "charge_mixed_secs",
        }.get(mode)
        if secs_col:
            deltas[secs_col] = deltas.get(secs_col, 0) + dt_s

        # Always-on totals
        for bucket_col, field, filt in INTEGRATE_TOTALS:
            cur = _signed(fields[field], filt)
            prv = _signed(prev_fields.get(field), filt)
            wh = (cur + prv) / 2.0 * hours
            deltas[bucket_col] = deltas.get(bucket_col, 0.0) + wh

        deltas["samples"] = 1

        # UPSERT into the hour bucket
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO energy_buckets (bucket_hour) VALUES (?)",
                (bucket,),
            )
            assignments = ", ".join(f"{c} = {c} + ?" for c in deltas.keys())
            conn.execute(
                f"UPDATE energy_buckets SET {assignments} WHERE bucket_hour = ?",
                list(deltas.values()) + [bucket],
            )
            conn.commit()
        finally:
            conn.close()

    # -----------------------------------------------------------------
    # Query helpers (read-only — Flask side)
    # -----------------------------------------------------------------
    def latest(self):
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM samples ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def sample_columns(self):
        """Return the samples-table column names in schema order."""
        conn = self._connect()
        try:
            return [r[1] for r in conn.execute("PRAGMA table_info(samples)")]
        finally:
            conn.close()

    def samples_iter(self, start_unix, end_unix, chunk=1000):
        """
        Stream samples in [start_unix, end_unix] without materialising
        the full list. Yields sqlite3.Row objects in chunks for use by
        the CSV exporter.
        """
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT * FROM samples WHERE ts BETWEEN ? AND ? ORDER BY ts",
                (start_unix, end_unix),
            )
            while True:
                rows = cur.fetchmany(chunk)
                if not rows:
                    break
                for r in rows:
                    yield r
        finally:
            conn.close()

    def samples_between(self, start_unix, end_unix, max_points=2000):
        """Return downsampled samples in [start_unix, end_unix]."""
        conn = self._connect()
        try:
            total = conn.execute(
                "SELECT COUNT(*) FROM samples WHERE ts BETWEEN ? AND ?",
                (start_unix, end_unix),
            ).fetchone()[0]
            stride = max(1, math.ceil(total / max_points))
            # Pick every Nth row by row_number modulo stride
            rows = conn.execute(
                f"""
                SELECT * FROM (
                    SELECT *, ROW_NUMBER() OVER (ORDER BY ts) AS rn
                    FROM samples WHERE ts BETWEEN ? AND ?
                )
                WHERE (rn - 1) % ? = 0
                ORDER BY ts
                """,
                (start_unix, end_unix, stride),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def aggregate(self, start_unix, end_unix):
        """
        Sum hourly energy buckets in the half-open range
        [start_unix, end_unix) and return as a dict of {column: value}.
        """
        conn = self._connect()
        try:
            cols = [
                "discharge_pure_dc_wh", "discharge_pure_ac_wh", "discharge_pure_secs",
                "discharge_mixed_dc_wh", "discharge_mixed_ac_wh", "discharge_mixed_secs",
                "charge_pv_dc_wh", "charge_pv_secs",
                "charge_grid_dc_wh", "charge_grid_ac_wh", "charge_grid_secs",
                "charge_mixed_dc_wh", "charge_mixed_ac_wh", "charge_mixed_secs",
                "pv_total_wh", "load_total_wh", "grid_import_wh", "grid_export_wh",
                "samples",
            ]
            sums = ", ".join(f"COALESCE(SUM({c}), 0) AS {c}" for c in cols)
            row = conn.execute(
                f"SELECT {sums} FROM energy_buckets "
                f"WHERE bucket_hour >= ? AND bucket_hour < ?",
                (start_unix, end_unix),
            ).fetchone()
            return dict(row) if row else {c: 0 for c in cols}
        finally:
            conn.close()

    def hourly_buckets(self, start_unix, end_unix):
        """Return the raw hourly bucket rows for charting."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM energy_buckets WHERE bucket_hour BETWEEN ? AND ? "
                "ORDER BY bucket_hour",
                (start_unix, end_unix),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def lifetime_start(self):
        """Unix ts of the oldest hourly bucket (or now if empty)."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT MIN(bucket_hour) AS m FROM energy_buckets"
            ).fetchone()
            return row["m"] or int(time.time())
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Efficiency math (pure functions on aggregate dicts)
# ---------------------------------------------------------------------------
def _safe_div(num, den):
    if not den or den <= 0:
        return None
    return num / den


def compute_efficiency(agg):
    """
    Given an aggregate dict (sum of energy_buckets columns over a
    window), compute the three efficiency numbers.

    Returns a dict with each ratio as a fraction (None if no data) and
    the underlying energy totals so the UI can show "computed from X Wh
    DC / Y Wh AC".
    """
    # DC-AC discharge efficiency: pure discharge mode only (no PV
    # confounding the AC measurement).
    eta_disc = _safe_div(agg["discharge_pure_ac_wh"], agg["discharge_pure_dc_wh"])

    # DC-AC charging efficiency: pure grid-charge mode only (no PV).
    eta_chg = _safe_div(agg["charge_grid_dc_wh"], agg["charge_grid_ac_wh"])

    # AC-AC round-trip is the product of the above (energy at AC out
    # returned per unit energy at AC in, end-to-end through the
    # battery). This assumes the same SoC swing both directions, which
    # averages out over a long enough window.
    eta_rt = (eta_chg * eta_disc) if (eta_chg and eta_disc) else None

    return {
        "ac_ac_roundtrip":     eta_rt,
        "dc_ac_discharge":     eta_disc,
        "dc_ac_charge":        eta_chg,
        "discharge_pure_dc_wh": agg["discharge_pure_dc_wh"],
        "discharge_pure_ac_wh": agg["discharge_pure_ac_wh"],
        "discharge_pure_secs":  agg["discharge_pure_secs"],
        "charge_grid_dc_wh":    agg["charge_grid_dc_wh"],
        "charge_grid_ac_wh":    agg["charge_grid_ac_wh"],
        "charge_grid_secs":     agg["charge_grid_secs"],
    }


def window_seconds(name):
    """Translate a window name to (seconds, label)."""
    table = {
        "1h":       3600,
        "24h":      86400,
        "7d":      7 * 86400,
        "30d":    30 * 86400,
        "lifetime": None,    # caller decides
    }
    return table.get(name, 86400)
