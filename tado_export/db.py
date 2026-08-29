"""SQLite storage.

Two layers live side by side:

* ``day_report_raw`` keeps the gzipped JSON exactly as tado returned it. Every
  normalised table can be rebuilt from it without spending a single API call,
  which is what makes ``tado-export reparse`` cheap.
* The normalised tables are what the dashboard and the CSV export read.

All timestamps are stored as ISO-8601 UTC text ("2024-01-10T22:45:00Z"), which
sorts correctly as a string and stays readable in a SQL browser.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
from datetime import date
from pathlib import Path

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS home (
    id               INTEGER PRIMARY KEY,
    name             TEXT,
    timezone         TEXT,
    temperature_unit TEXT,
    generation       TEXT,
    date_created     TEXT,
    latitude         REAL,
    longitude        REAL,
    raw              TEXT
);

CREATE TABLE IF NOT EXISTS zone (
    home_id      INTEGER NOT NULL,
    id           INTEGER NOT NULL,
    name         TEXT,
    type         TEXT,
    date_created TEXT,
    device_types TEXT,
    raw          TEXT,
    PRIMARY KEY (home_id, id)
);

-- Verbatim API payloads, gzipped. Source of truth for reparsing.
CREATE TABLE IF NOT EXISTS day_report_raw (
    home_id    INTEGER NOT NULL,
    zone_id    INTEGER NOT NULL,
    day        TEXT    NOT NULL,
    fetched_at TEXT    NOT NULL,
    payload    BLOB    NOT NULL,
    PRIMARY KEY (home_id, zone_id, day)
);

-- Fetch bookkeeping, so a backfill can resume exactly where it stopped.
CREATE TABLE IF NOT EXISTS sync_day (
    home_id      INTEGER NOT NULL,
    zone_id      INTEGER NOT NULL,
    day          TEXT    NOT NULL,
    status       TEXT    NOT NULL,   -- ok | empty | error
    fetched_at   TEXT    NOT NULL,
    measurements INTEGER NOT NULL DEFAULT 0,
    note         TEXT,
    PRIMARY KEY (home_id, zone_id, day)
);

-- Verbatim payloads for the non-dayReport sources, same rationale as above.
CREATE TABLE IF NOT EXISTS raw_document (
    home_id    INTEGER NOT NULL,
    kind       TEXT    NOT NULL,   -- running_times | consumption | meter_readings | tariffs
    key        TEXT    NOT NULL,   -- month, date range, or '' for whole-history documents
    fetched_at TEXT    NOT NULL,
    payload    BLOB    NOT NULL,
    PRIMARY KEY (home_id, kind, key)
);

-- Heating running time per zone per day. Reaches back further than dayReport.
CREATE TABLE IF NOT EXISTS running_time (
    home_id INTEGER NOT NULL,
    day     TEXT    NOT NULL,
    zone_id INTEGER NOT NULL,
    seconds INTEGER,
    PRIMARY KEY (home_id, day, zone_id)
);

-- tado's own whole-home total, which is not always the sum of the zones.
CREATE TABLE IF NOT EXISTS running_time_day (
    home_id       INTEGER NOT NULL,
    day           TEXT    NOT NULL,
    total_seconds INTEGER,
    PRIMARY KEY (home_id, day)
);

-- Energy IQ: daily consumption and cost.
CREATE TABLE IF NOT EXISTS energy_consumption (
    home_id     INTEGER NOT NULL,
    day         TEXT    NOT NULL,
    consumption REAL,
    heating     REAL,
    cost_cents  REAL,
    unit        TEXT,
    has_data    INTEGER,
    PRIMARY KEY (home_id, day)
);

CREATE TABLE IF NOT EXISTS meter_reading (
    home_id      INTEGER NOT NULL,
    reading_date TEXT    NOT NULL,
    reading      REAL,
    reading_id   TEXT,
    PRIMARY KEY (home_id, reading_date)
);

CREATE TABLE IF NOT EXISTS tariff (
    home_id    INTEGER NOT NULL,
    start_date TEXT    NOT NULL,
    end_date   TEXT,
    unit       TEXT,
    cents      REAL,
    tariff_id  TEXT,
    PRIMARY KEY (home_id, start_date)
);

CREATE TABLE IF NOT EXISTS measurement (
    home_id       INTEGER NOT NULL,
    zone_id       INTEGER NOT NULL,
    ts            TEXT    NOT NULL,
    inside_temp_c REAL,
    humidity_pct  REAL,
    PRIMARY KEY (home_id, zone_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_measurement_ts ON measurement (ts);

CREATE TABLE IF NOT EXISTS setpoint (
    home_id   INTEGER NOT NULL,
    zone_id   INTEGER NOT NULL,
    ts_from   TEXT    NOT NULL,
    ts_to     TEXT    NOT NULL,
    zone_type TEXT,
    power     TEXT,
    temp_c    REAL,
    PRIMARY KEY (home_id, zone_id, ts_from)
);
CREATE INDEX IF NOT EXISTS idx_setpoint_range ON setpoint (home_id, zone_id, ts_from, ts_to);

CREATE TABLE IF NOT EXISTS call_for_heat (
    home_id INTEGER NOT NULL,
    zone_id INTEGER NOT NULL,
    ts_from TEXT    NOT NULL,
    ts_to   TEXT    NOT NULL,
    level   TEXT,               -- NONE | LOW | MEDIUM | HIGH
    PRIMARY KEY (home_id, zone_id, ts_from)
);
CREATE INDEX IF NOT EXISTS idx_cfh_range ON call_for_heat (home_id, zone_id, ts_from, ts_to);

CREATE TABLE IF NOT EXISTS stripe (
    home_id         INTEGER NOT NULL,
    zone_id         INTEGER NOT NULL,
    ts_from         TEXT    NOT NULL,
    ts_to           TEXT    NOT NULL,
    stripe_type     TEXT,       -- HOME | AWAY | OVERLAY_ACTIVE | OPEN_WINDOW_DETECTED | ...
    setting_power   TEXT,
    setting_temp_c  REAL,
    PRIMARY KEY (home_id, zone_id, ts_from)
);

CREATE TABLE IF NOT EXISTS hot_water (
    home_id   INTEGER NOT NULL,
    zone_id   INTEGER NOT NULL,
    ts_from   TEXT    NOT NULL,
    ts_to     TEXT    NOT NULL,
    producing INTEGER,
    PRIMARY KEY (home_id, zone_id, ts_from)
);

CREATE TABLE IF NOT EXISTS device_connected (
    home_id   INTEGER NOT NULL,
    zone_id   INTEGER NOT NULL,
    ts_from   TEXT    NOT NULL,
    ts_to     TEXT    NOT NULL,
    connected INTEGER,
    PRIMARY KEY (home_id, zone_id, ts_from)
);

-- Weather is home-wide: every zone's day report repeats it, so it is stored once.
CREATE TABLE IF NOT EXISTS weather_condition (
    home_id INTEGER NOT NULL,
    ts_from TEXT    NOT NULL,
    ts_to   TEXT    NOT NULL,
    state   TEXT,
    temp_c  REAL,
    PRIMARY KEY (home_id, ts_from)
);
CREATE INDEX IF NOT EXISTS idx_weather_range ON weather_condition (home_id, ts_from, ts_to);

CREATE TABLE IF NOT EXISTS weather_sunny (
    home_id INTEGER NOT NULL,
    ts_from TEXT    NOT NULL,
    ts_to   TEXT    NOT NULL,
    sunny   INTEGER,
    PRIMARY KEY (home_id, ts_from)
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


# -- writes ---------------------------------------------------------------


def upsert_home(conn: sqlite3.Connection, home: dict) -> None:
    geo = home.get("geolocation") or {}
    conn.execute(
        """
        INSERT INTO home (id, name, timezone, temperature_unit, generation,
                          date_created, latitude, longitude, raw)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name, timezone=excluded.timezone,
            temperature_unit=excluded.temperature_unit,
            generation=excluded.generation, date_created=excluded.date_created,
            latitude=excluded.latitude, longitude=excluded.longitude,
            raw=excluded.raw
        """,
        (
            home["id"],
            home.get("name"),
            home.get("dateTimeZone"),
            home.get("temperatureUnit"),
            home.get("generation"),
            home.get("dateCreated"),
            geo.get("latitude"),
            geo.get("longitude"),
            json.dumps(home),
        ),
    )


def upsert_zones(conn: sqlite3.Connection, home_id: int, zones: list[dict]) -> None:
    conn.executemany(
        """
        INSERT INTO zone (home_id, id, name, type, date_created, device_types, raw)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(home_id, id) DO UPDATE SET
            name=excluded.name, type=excluded.type,
            date_created=excluded.date_created,
            device_types=excluded.device_types, raw=excluded.raw
        """,
        [
            (
                home_id,
                z["id"],
                z.get("name"),
                z.get("type"),
                z.get("dateCreated"),
                ",".join(z.get("deviceTypes") or []),
                json.dumps(z),
            )
            for z in zones
        ],
    )


def store_raw(
    conn: sqlite3.Connection, home_id: int, zone_id: int, day: date, payload: dict, fetched_at: str
) -> None:
    blob = gzip.compress(json.dumps(payload, separators=(",", ":")).encode(), 6)
    conn.execute(
        """
        INSERT INTO day_report_raw (home_id, zone_id, day, fetched_at, payload)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(home_id, zone_id, day) DO UPDATE SET
            fetched_at=excluded.fetched_at, payload=excluded.payload
        """,
        (home_id, zone_id, day.isoformat(), fetched_at, blob),
    )


def load_raw(conn: sqlite3.Connection, home_id: int, zone_id: int, day: str) -> dict | None:
    row = conn.execute(
        "SELECT payload FROM day_report_raw WHERE home_id=? AND zone_id=? AND day=?",
        (home_id, zone_id, day),
    ).fetchone()
    if row is None:
        return None
    return json.loads(gzip.decompress(row["payload"]).decode())


def iter_raw_days(conn: sqlite3.Connection, home_id: int | None = None):
    sql = "SELECT home_id, zone_id, day FROM day_report_raw"
    args: tuple = ()
    if home_id is not None:
        sql += " WHERE home_id=?"
        args = (home_id,)
    sql += " ORDER BY day, zone_id"
    yield from conn.execute(sql, args)


def stored_status(conn: sqlite3.Connection, home_id: int, zone_id: int, day: date) -> str | None:
    """Status already recorded for a zone-day, or None if it was never fetched."""
    row = conn.execute(
        "SELECT status FROM sync_day WHERE home_id=? AND zone_id=? AND day=?",
        (home_id, zone_id, day.isoformat()),
    ).fetchone()
    return row["status"] if row else None


def mark_day(
    conn: sqlite3.Connection,
    home_id: int,
    zone_id: int,
    day: date,
    status: str,
    fetched_at: str,
    measurements: int = 0,
    note: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO sync_day (home_id, zone_id, day, status, fetched_at, measurements, note)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(home_id, zone_id, day) DO UPDATE SET
            status=excluded.status, fetched_at=excluded.fetched_at,
            measurements=excluded.measurements, note=excluded.note
        """,
        (home_id, zone_id, day.isoformat(), status, fetched_at, measurements, note),
    )


def store_document(conn: sqlite3.Connection, home_id: int, kind: str, key: str,
                   payload, fetched_at: str) -> None:
    blob = gzip.compress(json.dumps(payload, separators=(",", ":")).encode(), 6)
    conn.execute(
        """
        INSERT INTO raw_document (home_id, kind, key, fetched_at, payload)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(home_id, kind, key) DO UPDATE SET
            fetched_at=excluded.fetched_at, payload=excluded.payload
        """,
        (home_id, kind, key, fetched_at, blob),
    )


def load_document(conn: sqlite3.Connection, home_id: int, kind: str, key: str):
    row = conn.execute(
        "SELECT payload FROM raw_document WHERE home_id=? AND kind=? AND key=?",
        (home_id, kind, key),
    ).fetchone()
    return json.loads(gzip.decompress(row["payload"]).decode()) if row else None


def document_keys(conn: sqlite3.Connection, home_id: int, kind: str) -> set[str]:
    return {
        r["key"] for r in conn.execute(
            "SELECT key FROM raw_document WHERE home_id=? AND kind=?", (home_id, kind)
        )
    }


_INSERTS = {
    "measurement": "INSERT OR REPLACE INTO measurement (home_id, zone_id, ts, inside_temp_c, humidity_pct) VALUES (?,?,?,?,?)",
    "setpoint": "INSERT OR REPLACE INTO setpoint (home_id, zone_id, ts_from, ts_to, zone_type, power, temp_c) VALUES (?,?,?,?,?,?,?)",
    "call_for_heat": "INSERT OR REPLACE INTO call_for_heat (home_id, zone_id, ts_from, ts_to, level) VALUES (?,?,?,?,?)",
    "stripe": "INSERT OR REPLACE INTO stripe (home_id, zone_id, ts_from, ts_to, stripe_type, setting_power, setting_temp_c) VALUES (?,?,?,?,?,?,?)",
    "hot_water": "INSERT OR REPLACE INTO hot_water (home_id, zone_id, ts_from, ts_to, producing) VALUES (?,?,?,?,?)",
    "device_connected": "INSERT OR REPLACE INTO device_connected (home_id, zone_id, ts_from, ts_to, connected) VALUES (?,?,?,?,?)",
    "weather_condition": "INSERT OR REPLACE INTO weather_condition (home_id, ts_from, ts_to, state, temp_c) VALUES (?,?,?,?,?)",
    "weather_sunny": "INSERT OR REPLACE INTO weather_sunny (home_id, ts_from, ts_to, sunny) VALUES (?,?,?,?)",
    "running_time": "INSERT OR REPLACE INTO running_time (home_id, day, zone_id, seconds) VALUES (?,?,?,?)",
    "running_time_day": "INSERT OR REPLACE INTO running_time_day (home_id, day, total_seconds) VALUES (?,?,?)",
    "energy_consumption": "INSERT OR REPLACE INTO energy_consumption (home_id, day, consumption, heating, cost_cents, unit, has_data) VALUES (?,?,?,?,?,?,?)",
    "meter_reading": "INSERT OR REPLACE INTO meter_reading (home_id, reading_date, reading, reading_id) VALUES (?,?,?,?)",
    "tariff": "INSERT OR REPLACE INTO tariff (home_id, start_date, end_date, unit, cents, tariff_id) VALUES (?,?,?,?,?,?)",
}


def write_rows(conn: sqlite3.Connection, rows: dict[str, list[tuple]]) -> None:
    """Bulk-write the output of ``ingest.parse_day_report``."""
    for table, values in rows.items():
        if values:
            conn.executemany(_INSERTS[table], values)


DERIVED_TABLES = (
    "measurement", "setpoint", "call_for_heat", "stripe", "hot_water",
    "device_connected", "weather_condition", "weather_sunny",
)


def purge_derived(conn: sqlite3.Connection, home_id: int | None = None) -> None:
    """Empty the normalised tables. ``day_report_raw`` is untouched — it is the
    source of truth everything here is rebuilt from."""
    for table in DERIVED_TABLES:
        if home_id is None:
            conn.execute(f"DELETE FROM {table}")
        else:
            conn.execute(f"DELETE FROM {table} WHERE home_id=?", (home_id,))
    conn.commit()


def existing_days(conn: sqlite3.Connection, home_id: int) -> set[tuple[int, str]]:
    """(zone_id, day) pairs already fetched successfully."""
    return {
        (r["zone_id"], r["day"])
        for r in conn.execute(
            "SELECT zone_id, day FROM sync_day "
            "WHERE home_id=? AND status IN ('ok','empty','placeholder')",
            (home_id,),
        )
    }
