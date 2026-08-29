"""Turn a raw dayReport payload into normalised row tuples.

Every field is read defensively: which blocks are present depends on the zone
type (HEATING / HOT_WATER / AIR_CONDITIONING) and on what hardware the zone has,
so a missing block is normal rather than an error.
"""

from __future__ import annotations

from datetime import datetime, timezone

TABLES = (
    "measurement",
    "setpoint",
    "call_for_heat",
    "stripe",
    "hot_water",
    "device_connected",
    "weather_condition",
    "weather_sunny",
)


def _iso(value: str | None) -> str | None:
    """Normalise a tado timestamp to second-precision ISO-8601 UTC."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _celsius(node) -> float | None:
    if isinstance(node, dict):
        value = node.get("celsius")
        return float(value) if value is not None else None
    return None


def _intervals(node) -> list[dict]:
    if isinstance(node, dict):
        return node.get("dataIntervals") or []
    return []


def _points(node) -> list[dict]:
    if isinstance(node, dict):
        return node.get("dataPoints") or []
    return []


def _humidity_scale(node) -> float:
    """Humidity comes as a 0..1 fraction (UNIT_INTERVAL); store it as percent."""
    if isinstance(node, dict) and node.get("percentageUnit") == "PERCENTAGE":
        return 1.0
    return 100.0


def is_placeholder(payload: dict) -> bool:
    """True when tado served filler instead of real measurements.

    Beyond its retention horizon (roughly 13 months) the dayReport endpoint keeps
    answering 200, but with a synthetic day: inside temperature pinned to a single
    value for every sample, humidity likewise, one whole-day `NONE` call-for-heat
    interval, and — the give-away — no weather at all. A real day always carries a
    weather condition, and a real room never holds one value to the centidegree
    across ninety-six samples.

    Requiring the flat temperature *and* a second signal keeps a genuinely steady
    room from being thrown away.
    """
    if not isinstance(payload, dict):
        return False

    measured = payload.get("measuredData") or {}
    temp_points = _points(measured.get("insideTemperature"))
    if len(temp_points) < 4:
        return False
    values = {_celsius(p.get("value")) for p in temp_points}
    if len(values) != 1 or None in values:
        return False

    humidity_points = _points(measured.get("humidity"))
    flat_humidity = (
        len(humidity_points) >= 4
        and len({p.get("value") for p in humidity_points}) == 1
    )

    weather = (payload.get("weather") or {}).get("condition")
    has_weather = any(
        (interval.get("value") or {}).get("state")
        for interval in _intervals(weather)
    )
    return flat_humidity or not has_weather


def parse_day_report(home_id: int, zone_id: int, payload: dict) -> dict[str, list[tuple]]:
    """Return {table_name: [row tuples]} ready for ``db.write_rows``."""
    rows: dict[str, list[tuple]] = {table: [] for table in TABLES}
    if not isinstance(payload, dict):
        return rows

    measured = payload.get("measuredData") or {}

    # --- measurements: temperature and humidity, merged on timestamp --------
    merged: dict[str, list[float | None]] = {}
    for point in _points(measured.get("insideTemperature")):
        ts = _iso(point.get("timestamp"))
        if ts:
            merged.setdefault(ts, [None, None])[0] = _celsius(point.get("value"))

    humidity_node = measured.get("humidity")
    scale = _humidity_scale(humidity_node)
    for point in _points(humidity_node):
        ts = _iso(point.get("timestamp"))
        value = point.get("value")
        if ts and value is not None:
            merged.setdefault(ts, [None, None])[1] = round(float(value) * scale, 2)

    rows["measurement"] = [
        (home_id, zone_id, ts, values[0], values[1]) for ts, values in sorted(merged.items())
    ]

    # --- zone settings (the target temperature timeline) --------------------
    for interval in _intervals(payload.get("settings")):
        value = interval.get("value") or {}
        rows["setpoint"].append(
            (
                home_id,
                zone_id,
                _iso(interval.get("from")),
                _iso(interval.get("to")),
                value.get("type"),
                value.get("power"),
                _celsius(value.get("temperature")),
            )
        )

    # --- boiler demand ------------------------------------------------------
    for interval in _intervals(payload.get("callForHeat")):
        rows["call_for_heat"].append(
            (
                home_id,
                zone_id,
                _iso(interval.get("from")),
                _iso(interval.get("to")),
                interval.get("value"),
            )
        )

    # --- stripes: away / overlay / open window / device offline -------------
    for interval in _intervals(payload.get("stripes")):
        value = interval.get("value") or {}
        setting = value.get("setting") or {}
        rows["stripe"].append(
            (
                home_id,
                zone_id,
                _iso(interval.get("from")),
                _iso(interval.get("to")),
                value.get("stripeType"),
                setting.get("power"),
                _celsius(setting.get("temperature")),
            )
        )

    for interval in _intervals(payload.get("hotWaterProduction")):
        rows["hot_water"].append(
            (
                home_id,
                zone_id,
                _iso(interval.get("from")),
                _iso(interval.get("to")),
                _as_int(interval.get("value")),
            )
        )

    for interval in _intervals(measured.get("measuringDeviceConnected")):
        rows["device_connected"].append(
            (
                home_id,
                zone_id,
                _iso(interval.get("from")),
                _iso(interval.get("to")),
                _as_int(interval.get("value")),
            )
        )

    # --- weather (home-wide; identical in every zone's report) --------------
    weather = payload.get("weather") or {}
    for interval in _intervals(weather.get("condition")):
        value = interval.get("value") or {}
        rows["weather_condition"].append(
            (
                home_id,
                _iso(interval.get("from")),
                _iso(interval.get("to")),
                value.get("state"),
                _celsius(value.get("temperature")),
            )
        )
    for interval in _intervals(weather.get("sunny")):
        rows["weather_sunny"].append(
            (
                home_id,
                _iso(interval.get("from")),
                _iso(interval.get("to")),
                _as_int(interval.get("value")),
            )
        )

    return rows


def _as_int(value) -> int | None:
    if value is None:
        return None
    return 1 if bool(value) else 0


def merge_rows(target: dict[str, list[tuple]], extra: dict[str, list[tuple]]) -> None:
    for table, values in extra.items():
        target.setdefault(table, []).extend(values)


# -- the longer-lived sources ---------------------------------------------


def parse_running_times(home_id: int, payload: dict) -> dict[str, list[tuple]]:
    """Per-day heating runtime, per zone and for the home as a whole."""
    rows: dict[str, list[tuple]] = {"running_time": [], "running_time_day": []}
    if not isinstance(payload, dict):
        return rows
    for entry in payload.get("runningTimes") or []:
        day = (entry.get("startTime") or "")[:10]
        if not day:
            continue
        rows["running_time_day"].append(
            (home_id, day, _as_seconds(entry.get("runningTimeInSeconds")))
        )
        for zone in entry.get("zones") or []:
            if zone.get("id") is None:
                continue
            rows["running_time"].append(
                (home_id, day, int(zone["id"]), _as_seconds(zone.get("runningTimeInSeconds")))
            )
    return rows


def parse_consumption(home_id: int, payload: dict) -> dict[str, list[tuple]]:
    """Daily energy consumption and cost from an Energy IQ month document."""
    rows: dict[str, list[tuple]] = {"energy_consumption": []}
    if not isinstance(payload, dict):
        return rows
    unit = payload.get("unit")
    month = (payload.get("monthlyAggregation") or {}).get("requestedMonth") or {}
    for entry in month.get("consumptionPerDate") or []:
        day = entry.get("date")
        if not day:
            continue
        rows["energy_consumption"].append(
            (
                home_id,
                day,
                _as_float(entry.get("consumption")),
                _as_float(entry.get("heating")),
                _as_float(entry.get("costInCents")),
                unit,
                _as_int(entry.get("hasData")),
            )
        )
    return rows


def parse_meter_readings(home_id: int, payload) -> dict[str, list[tuple]]:
    rows: dict[str, list[tuple]] = {"meter_reading": []}
    entries = payload.get("readings") if isinstance(payload, dict) else payload
    for entry in entries or []:
        if not isinstance(entry, dict) or not entry.get("date"):
            continue
        rows["meter_reading"].append(
            (home_id, entry["date"], _as_float(entry.get("reading")), entry.get("id"))
        )
    return rows


def parse_tariffs(home_id: int, payload) -> dict[str, list[tuple]]:
    rows: dict[str, list[tuple]] = {"tariff": []}
    entries = payload.get("tariffs") if isinstance(payload, dict) else payload
    for entry in entries or []:
        if not isinstance(entry, dict) or not entry.get("startDate"):
            continue
        rows["tariff"].append(
            (
                home_id,
                entry["startDate"],
                entry.get("endDate"),
                entry.get("unit"),
                _as_float(entry.get("tariffInCents")),
                entry.get("id"),
            )
        )
    return rows


def _as_float(value) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _as_seconds(value) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None
