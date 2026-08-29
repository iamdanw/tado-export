"""Query helpers that turn the SQLite tables into tidy pandas frames.

Shared by the CSV exporter and the dashboard so both see identical numbers.

The interval tables (setpoint, call-for-heat, weather) are aligned onto the
measurement timeline with a backward ``merge_asof``: for each reading, take the
interval that started most recently, then discard it if the reading falls after
that interval ended.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pandas as pd

CALL_FOR_HEAT_LEVELS = {"NONE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}


def _read(conn: sqlite3.Connection, sql: str, params: tuple, ts_cols: list[str]) -> pd.DataFrame:
    frame = pd.read_sql_query(sql, conn, params=params)
    for column in ts_cols:
        if column in frame.columns:
            frame[column] = pd.to_datetime(frame[column], utc=True, format="ISO8601")
    return frame


def _bounds(start: date | None, end: date | None) -> tuple[str, str]:
    lower = f"{start.isoformat()}T00:00:00Z" if start else "0000"
    upper = f"{end.isoformat()}T23:59:59Z" if end else "9999"
    return lower, upper


def homes(conn: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql_query("SELECT id, name, timezone FROM home ORDER BY name", conn)


def zones(conn: sqlite3.Connection, home_id: int) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT id, name, type FROM zone WHERE home_id=? ORDER BY id", conn, params=(home_id,)
    )


def data_extent(conn: sqlite3.Connection, home_id: int) -> tuple[date | None, date | None]:
    row = conn.execute(
        "SELECT MIN(day) AS lo, MAX(day) AS hi FROM sync_day "
        "WHERE home_id=? AND status='ok'",
        (home_id,),
    ).fetchone()
    if not row or not row["lo"]:
        return None, None
    return date.fromisoformat(row["lo"]), date.fromisoformat(row["hi"])


def measurements(
    conn: sqlite3.Connection,
    home_id: int,
    zone_ids: list[int] | None = None,
    start: date | None = None,
    end: date | None = None,
) -> pd.DataFrame:
    lower, upper = _bounds(start, end)
    sql = (
        "SELECT m.home_id, m.zone_id, z.name AS zone_name, z.type AS zone_type, m.ts, "
        "       m.inside_temp_c, m.humidity_pct "
        "FROM measurement m LEFT JOIN zone z "
        "  ON z.home_id = m.home_id AND z.id = m.zone_id "
        "WHERE m.home_id=? AND m.ts BETWEEN ? AND ?"
    )
    params: list = [home_id, lower, upper]
    if zone_ids:
        sql += f" AND m.zone_id IN ({','.join('?' * len(zone_ids))})"
        params += list(zone_ids)
    sql += " ORDER BY m.ts"
    return _read(conn, sql, tuple(params), ["ts"])


def _interval_table(
    conn: sqlite3.Connection,
    table: str,
    columns: str,
    home_id: int,
    zone_ids: list[int] | None,
    start: date | None,
    end: date | None,
    *,
    zone_scoped: bool = True,
) -> pd.DataFrame:
    lower, upper = _bounds(start, end)
    # An interval can start before the window and reach into it, so widen the
    # lower bound by a day rather than clipping on ts_from alone.
    sql = f"SELECT {columns} FROM {table} WHERE home_id=? AND ts_to >= ? AND ts_from <= ?"
    params: list = [home_id, lower, upper]
    if zone_scoped and zone_ids:
        sql += f" AND zone_id IN ({','.join('?' * len(zone_ids))})"
        params += list(zone_ids)
    sql += " ORDER BY ts_from"
    return _read(conn, sql, tuple(params), ["ts_from", "ts_to"])


def setpoints(conn, home_id, zone_ids=None, start=None, end=None) -> pd.DataFrame:
    return _interval_table(
        conn, "setpoint", "home_id, zone_id, ts_from, ts_to, zone_type, power, temp_c",
        home_id, zone_ids, start, end,
    )


def call_for_heat(conn, home_id, zone_ids=None, start=None, end=None) -> pd.DataFrame:
    frame = _interval_table(
        conn, "call_for_heat", "home_id, zone_id, ts_from, ts_to, level",
        home_id, zone_ids, start, end,
    )
    if not frame.empty:
        frame["level_num"] = frame["level"].map(CALL_FOR_HEAT_LEVELS).astype("float64")
    return frame


def stripes(conn, home_id, zone_ids=None, start=None, end=None) -> pd.DataFrame:
    return _interval_table(
        conn, "stripe", "home_id, zone_id, ts_from, ts_to, stripe_type, setting_power, setting_temp_c",
        home_id, zone_ids, start, end,
    )


def weather(conn, home_id, start=None, end=None) -> pd.DataFrame:
    return _interval_table(
        conn, "weather_condition", "home_id, ts_from, ts_to, state, temp_c",
        home_id, None, start, end, zone_scoped=False,
    )


def _align(
    base: pd.DataFrame,
    intervals: pd.DataFrame,
    value_cols: dict[str, str],
    by_zone: bool,
) -> pd.DataFrame:
    """Attach the interval covering each reading (backward asof + end check)."""
    if base.empty or intervals.empty:
        for target in value_cols.values():
            base[target] = pd.NA
        return base

    left = base.sort_values("ts")
    right = intervals.sort_values("ts_from")
    keep = ["ts_from", "ts_to", *value_cols.keys()]
    if by_zone:
        keep.append("zone_id")

    merged = pd.merge_asof(
        left,
        right[keep].rename(columns={"ts_from": "ts"}),
        on="ts",
        by="zone_id" if by_zone else None,
        direction="backward",
        suffixes=("", "_iv"),
    )
    # merge_asof only guarantees the interval started before the reading;
    # drop matches whose interval had already ended.
    expired = merged["ts_to"].notna() & (merged["ts"] >= merged["ts_to"])
    for source, target in value_cols.items():
        merged[target] = merged[source].mask(expired)
        if source != target and source in merged.columns:
            merged = merged.drop(columns=[source])
    return merged.drop(columns=["ts_to"], errors="ignore")


def enriched(
    conn: sqlite3.Connection,
    home_id: int,
    zone_ids: list[int] | None = None,
    start: date | None = None,
    end: date | None = None,
    tz: str | None = None,
) -> pd.DataFrame:
    """One wide row per reading: measurement + setpoint + demand + weather.

    ``start``/``end`` are calendar days in the home's local timezone. The SQL
    window is padded by a day on each side because local midnight is not UTC
    midnight; the rows are trimmed back to whole local days at the end.
    """
    pad_start = start - timedelta(days=1) if start else None
    pad_end = end + timedelta(days=1) if end else None

    frame = measurements(conn, home_id, zone_ids, pad_start, pad_end)
    if frame.empty:
        return frame

    frame = _align(
        frame,
        setpoints(conn, home_id, zone_ids, pad_start, pad_end),
        {"temp_c": "setpoint_c", "power": "power"},
        by_zone=True,
    )
    frame = _align(
        frame,
        call_for_heat(conn, home_id, zone_ids, pad_start, pad_end),
        {"level": "call_for_heat", "level_num": "call_for_heat_num"},
        by_zone=True,
    )
    frame = _align(
        frame,
        stripes(conn, home_id, zone_ids, pad_start, pad_end),
        {"stripe_type": "presence"},
        by_zone=True,
    )
    frame = _align(
        frame,
        weather(conn, home_id, pad_start, pad_end),
        {"temp_c": "outside_temp_c", "state": "weather_state"},
        by_zone=False,
    )

    if tz:
        frame["ts_local"] = frame["ts"].dt.tz_convert(tz)
    else:
        frame["ts_local"] = frame["ts"]
    frame["local_date"] = frame["ts_local"].dt.date
    frame["hour"] = frame["ts_local"].dt.hour

    # Trim the padding: keep only whole local days inside the requested range.
    if start is not None:
        frame = frame[frame["local_date"] >= start]
    if end is not None:
        frame = frame[frame["local_date"] <= end]

    columns = [
        "home_id", "zone_id", "zone_name", "zone_type", "ts", "ts_local", "local_date", "hour",
        "inside_temp_c", "humidity_pct", "setpoint_c", "power",
        "call_for_heat", "call_for_heat_num", "presence",
        "outside_temp_c", "weather_state",
    ]
    return frame[[c for c in columns if c in frame.columns]]


# -- the longer-lived sources ---------------------------------------------


def _day_bounds(start: date | None, end: date | None) -> tuple[str, str]:
    return (start.isoformat() if start else "0000",
            end.isoformat() if end else "9999")


def running_times(conn, home_id, zone_ids=None, start=None, end=None) -> pd.DataFrame:
    """Per-day heating runtime per zone, with tado's own whole-home total."""
    lo, hi = _day_bounds(start, end)
    sql = (
        "SELECT r.home_id, r.day, r.zone_id, z.name AS zone_name, r.seconds, "
        "       r.seconds / 3600.0 AS hours, d.total_seconds, "
        "       d.total_seconds / 3600.0 AS home_hours "
        "FROM running_time r "
        "LEFT JOIN zone z ON z.home_id = r.home_id AND z.id = r.zone_id "
        "LEFT JOIN running_time_day d ON d.home_id = r.home_id AND d.day = r.day "
        "WHERE r.home_id=? AND r.day BETWEEN ? AND ?"
    )
    params: list = [home_id, lo, hi]
    if zone_ids:
        sql += f" AND r.zone_id IN ({','.join('?' * len(zone_ids))})"
        params += list(zone_ids)
    return pd.read_sql_query(sql + " ORDER BY r.day, r.zone_id", conn, params=tuple(params))


def energy(conn, home_id, start=None, end=None) -> pd.DataFrame:
    """Daily Energy IQ consumption and cost."""
    lo, hi = _day_bounds(start, end)
    return pd.read_sql_query(
        "SELECT home_id, day, consumption, heating, cost_cents, "
        "       cost_cents / 100.0 AS cost, unit, has_data "
        "FROM energy_consumption WHERE home_id=? AND day BETWEEN ? AND ? ORDER BY day",
        conn, params=(home_id, lo, hi),
    )


def meter_readings(conn, home_id) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT home_id, reading_date, reading, reading_id FROM meter_reading "
        "WHERE home_id=? ORDER BY reading_date", conn, params=(home_id,))


def tariffs(conn, home_id) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT home_id, start_date, end_date, unit, cents, tariff_id FROM tariff "
        "WHERE home_id=? ORDER BY start_date", conn, params=(home_id,))


# -- aggregation ----------------------------------------------------------

_AUTO_RULES = ((7, None), (90, "1h"), (400, "6h"))


def auto_rule(frame: pd.DataFrame) -> str | None:
    """Pick a resampling step from the span, so long ranges stay plottable."""
    if frame.empty:
        return None
    span_days = (frame["ts"].max() - frame["ts"].min()).days + 1
    for limit, rule in _AUTO_RULES:
        if span_days <= limit:
            return rule
    return "1D"


def _heating_frac(frame: pd.DataFrame) -> pd.Series:
    if "call_for_heat" not in frame.columns:
        return pd.Series(float("nan"), index=frame.index, dtype="float64")
    level = frame["call_for_heat"]
    frac = (level.notna() & (level != "NONE")).astype("float64")
    return frac.mask(level.isna())


def resample(
    frame: pd.DataFrame,
    rule: str | None,
    min_coverage: float = 0.0,
) -> pd.DataFrame:
    """Downsample per zone. Returns the frame unchanged when rule is None.

    Each bucket carries a ``coverage`` column — the share of its expected samples
    that actually exist. A daily mean built from four readings is not comparable
    to one built from ninety-six, and plotting them on the same line invents a
    spike; ``min_coverage`` drops those buckets instead.
    """
    if frame.empty:
        return frame
    working = frame.copy()
    working["heating_frac"] = _heating_frac(working)
    if rule is None:
        return working

    numeric = ["inside_temp_c", "humidity_pct", "setpoint_c",
               "call_for_heat_num", "outside_temp_c", "heating_frac"]
    numeric = [c for c in numeric if c in working.columns]

    per_sample = step_hours(working)
    bucket_hours = pd.Timedelta(rule).total_seconds() / 3600.0
    expected = max(bucket_hours / per_sample, 1.0)

    chunks = []
    group_keys = ["zone_id", "zone_name"] + (["zone_type"] if "zone_type" in working.columns else [])
    for keys, group in working.groupby(group_keys, dropna=False, observed=True):
        indexed = group.set_index("ts_local")
        agg = indexed[numeric].resample(rule).mean().dropna(how="all")
        counts = indexed["inside_temp_c"].resample(rule).count().reindex(agg.index)
        agg = agg.reset_index()
        agg["coverage"] = (counts.to_numpy() / expected).clip(max=1.0)
        if min_coverage:
            agg = agg[agg["coverage"] >= min_coverage]
        if agg.empty:
            continue
        for column, value in zip(group_keys, keys if isinstance(keys, tuple) else (keys,)):
            agg[column] = value
        agg["ts"] = agg["ts_local"].dt.tz_convert("UTC")
        agg["local_date"] = agg["ts_local"].dt.date
        agg["hour"] = agg["ts_local"].dt.hour
        chunks.append(agg)

    if not chunks:
        return working.iloc[0:0]
    return pd.concat(chunks, ignore_index=True).sort_values("ts_local")


def dropped_buckets(frame: pd.DataFrame, rule: str | None, min_coverage: float) -> int:
    """How many buckets ``resample`` would discard — so it can be stated, not hidden."""
    if rule is None or not min_coverage:
        return 0
    full = resample(frame, rule)
    if full.empty or "coverage" not in full.columns:
        return 0
    thin = full[full["coverage"] < min_coverage]
    return int(thin["local_date"].nunique()) if "local_date" in thin else len(thin)


def hourly_demand(frame: pd.DataFrame) -> pd.DataFrame:
    """Mean call-for-heat level per calendar day and hour, across the selection."""
    if frame.empty or "call_for_heat_num" not in frame.columns:
        return pd.DataFrame(columns=["local_date", "hour", "demand"])
    working = frame.dropna(subset=["call_for_heat_num"])
    if working.empty:
        return pd.DataFrame(columns=["local_date", "hour", "demand"])
    return (
        working.groupby(["local_date", "hour"], observed=True)["call_for_heat_num"]
        .mean()
        .reset_index(name="demand")
    )


def step_hours(frame: pd.DataFrame, default: float = 0.25) -> float:
    """Sampling interval in hours, inferred from the data (tado samples every 15 min)."""
    if frame.empty:
        return default
    stamps = frame["ts"].drop_duplicates().sort_values()
    if len(stamps) < 3:
        return default
    step = stamps.diff().dropna().median()
    if pd.isna(step):
        return default
    hours = step.total_seconds() / 3600.0
    return hours if 0 < hours <= 24 else default


def daily_heating(frame: pd.DataFrame, min_coverage: float = 0.5) -> pd.DataFrame:
    """Hours per local day each zone called for heat.

    Hours are counted from the samples actually present — never scaled up to a
    notional 24 h. A local day at the edge of the queried range, or one hit by a
    fetch failure, can hold only a handful of readings; ``coverage`` says what
    fraction of the day those readings span so callers can drop the misleading
    ones instead of drawing a partial day as if it were whole.
    """
    columns = ["local_date", "zone_id", "zone_name", "hours", "coverage"]
    if frame.empty:
        return pd.DataFrame(columns=columns)

    working = frame.copy()
    if "heating_frac" not in working.columns:
        working["heating_frac"] = _heating_frac(working)
    working = working.dropna(subset=["heating_frac"])
    if working.empty:
        return pd.DataFrame(columns=columns)

    per_sample = step_hours(working)
    daily = (
        working.groupby(["local_date", "zone_id", "zone_name"], observed=True)["heating_frac"]
        .agg(["sum", "size"])
        .reset_index()
    )
    daily["hours"] = daily["sum"] * per_sample
    daily["coverage"] = (daily["size"] * per_sample / 24.0).clip(upper=1.0)

    if min_coverage:
        daily = daily[daily["coverage"] >= min_coverage]
    return daily[columns]
