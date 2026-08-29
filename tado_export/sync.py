"""Backfill and incremental sync.

One dayReport call covers one zone for one calendar day, so the work list is
simply zones x days. It is walked newest-first (recent data is the data you
usually want first), fetched with a small thread pool, and written from the
calling thread so SQLite only ever sees one writer.

Progress is recorded per zone-day in ``sync_day``, which is what makes an
interrupted run resumable: re-running skips everything already stored.
"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import db, ingest
from .api import RateLimitExceeded, TadoApiError, TadoClient
from .config import DEFAULT_REFRESH_WINDOW_DAYS


# tado keeps roughly 13 months of real measurements and answers 200 with
# synthetic filler beyond that. After this many consecutive filler days a zone is
# treated as exhausted, which stops a backfill burning thousands of calls on it.
PLACEHOLDER_RUN_LIMIT = 10


@dataclass
class SyncResult:
    fetched: int = 0
    placeholder: int = 0
    protected: int = 0
    exhausted_zones: list[int] = field(default_factory=list)
    empty: int = 0
    failed: int = 0
    skipped: int = 0
    measurements: int = 0
    calls: int = 0
    stopped_reason: str | None = None
    remaining_quota: int | None = None
    pending: int = 0
    errors: list[str] = field(default_factory=list)


def home_timezone(conn: sqlite3.Connection, home_id: int) -> ZoneInfo:
    row = conn.execute("SELECT timezone FROM home WHERE id=?", (home_id,)).fetchone()
    name = row["timezone"] if row and row["timezone"] else "UTC"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def today_local(conn: sqlite3.Connection, home_id: int) -> date:
    return datetime.now(home_timezone(conn, home_id)).date()


def earliest_known_day(conn: sqlite3.Connection, home_id: int, zone_ids: list[int]) -> date:
    """Oldest date worth asking for: when the zones (or the home) were created."""
    candidates: list[date] = []
    placeholders = ",".join("?" * len(zone_ids)) if zone_ids else "NULL"
    query = f"SELECT date_created FROM zone WHERE home_id=? AND id IN ({placeholders})"
    for row in conn.execute(query, (home_id, *zone_ids)):
        parsed = _parse_created(row["date_created"])
        if parsed:
            candidates.append(parsed)
    row = conn.execute("SELECT date_created FROM home WHERE id=?", (home_id,)).fetchone()
    if row:
        parsed = _parse_created(row["date_created"])
        if parsed:
            candidates.append(parsed)
    return min(candidates) if candidates else date.today() - timedelta(days=365)


def _parse_created(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).date()
    except ValueError:
        return None


def build_work_list(
    conn: sqlite3.Connection,
    home_id: int,
    zone_ids: list[int],
    start: date,
    end: date,
    *,
    refresh_window_days: int,
    force: bool,
    newest_first: bool = True,
) -> tuple[list[tuple[int, date]], int]:
    """Return ([(zone_id, day)], skipped_count)."""
    done = set() if force else db.existing_days(conn, home_id)
    refresh_from = today_local(conn, home_id) - timedelta(days=max(refresh_window_days, 0))

    work: list[tuple[int, date]] = []
    skipped = 0
    days = (end - start).days
    day_range = range(days, -1, -1) if newest_first else range(days + 1)
    for offset in day_range:
        day = start + timedelta(days=offset)
        for zone_id in zone_ids:
            # Recent days are re-fetched: today is partial and tado backfills
            # late-arriving measurements for a day or two.
            if not force and (zone_id, day.isoformat()) in done and day < refresh_from:
                skipped += 1
                continue
            work.append((zone_id, day))
    return work, skipped


def sync(
    conn: sqlite3.Connection,
    client: TadoClient,
    home_id: int,
    zone_ids: list[int],
    start: date,
    end: date,
    *,
    workers: int = 4,
    refresh_window_days: int = DEFAULT_REFRESH_WINDOW_DAYS,
    force: bool = False,
    max_calls: int | None = None,
    reserve_quota: int = 20,
    progress=None,
) -> SyncResult:
    work, skipped = build_work_list(
        conn, home_id, zone_ids, start, end,
        refresh_window_days=refresh_window_days, force=force,
    )
    result = SyncResult(skipped=skipped, pending=len(work))
    if not work:
        return result
    result.exhausted_zones = []

    def fetch(item: tuple[int, date]):
        zone_id, day = item
        try:
            return item, client.day_report(home_id, zone_id, day), None
        except RateLimitExceeded:
            raise
        except TadoApiError as exc:
            return item, None, exc

    chunk_size = max(workers * 8, 8)
    stop = False
    # Only meaningful walking backwards in time, which is the default order.
    watch_placeholders = end >= start
    placeholder_run: dict[int, int] = {zone: 0 for zone in zone_ids}
    exhausted: set[int] = set()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for offset in range(0, len(work), chunk_size):
            if stop:
                break
            chunk = [item for item in work[offset : offset + chunk_size]
                     if item[0] not in exhausted]
            if not chunk:
                result.pending = len(work) - (offset + chunk_size)
                continue
            try:
                outcomes = list(pool.map(fetch, chunk))
            except RateLimitExceeded as exc:
                result.stopped_reason = str(exc)
                break

            fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            batch: dict[str, list[tuple]] = {}

            for (zone_id, day), payload, error in outcomes:
                if error is not None:
                    result.failed += 1
                    if len(result.errors) < 10:
                        result.errors.append(f"{day} zone {zone_id}: {error}")
                    db.mark_day(conn, home_id, zone_id, day, "error", fetched_at, note=str(error)[:300])
                    continue
                previous = db.stored_status(conn, home_id, zone_id, day)

                if payload is None:
                    # A day we already hold real measurements for must not be
                    # downgraded because tado stopped serving it.
                    if previous == "ok":
                        result.protected += 1
                        continue
                    result.empty += 1
                    db.mark_day(conn, home_id, zone_id, day, "empty", fetched_at)
                    continue

                if ingest.is_placeholder(payload):
                    placeholder_run[zone_id] = placeholder_run.get(zone_id, 0) + 1
                    if watch_placeholders and placeholder_run[zone_id] >= PLACEHOLDER_RUN_LIMIT:
                        exhausted.add(zone_id)

                    # Real history that has since aged out of tado's retention
                    # window comes back as filler. Never let that overwrite the
                    # measurements — or the raw payload — already on disk.
                    if previous == "ok":
                        result.protected += 1
                        continue

                    result.placeholder += 1
                    db.store_raw(conn, home_id, zone_id, day, payload, fetched_at)
                    db.mark_day(conn, home_id, zone_id, day, "placeholder", fetched_at,
                                note="tado returned filler, not measurements")
                    continue

                placeholder_run[zone_id] = 0
                db.store_raw(conn, home_id, zone_id, day, payload, fetched_at)
                rows = ingest.parse_day_report(home_id, zone_id, payload)
                ingest.merge_rows(batch, rows)
                count = len(rows["measurement"])
                result.fetched += 1
                result.measurements += count
                db.mark_day(conn, home_id, zone_id, day, "ok", fetched_at, count)

            db.write_rows(conn, batch)
            conn.commit()

            result.calls = client.calls_made
            result.remaining_quota = client.rate_limit.remaining
            result.pending = len(work) - (offset + len(chunk))

            if progress:
                progress(result)

            if exhausted and set(zone_ids) <= exhausted:
                result.exhausted_zones = sorted(exhausted)
                result.stopped_reason = (
                    f"Reached the end of tado's retained history for all "
                    f"{len(zone_ids)} zone(s) — older days come back as filler, not measurements."
                )
                stop = True
            elif max_calls is not None and client.calls_made >= max_calls:
                result.stopped_reason = f"Reached the --max-calls limit of {max_calls}."
                stop = True
            elif (
                client.rate_limit.remaining is not None
                and client.rate_limit.remaining <= reserve_quota
            ):
                result.stopped_reason = (
                    f"Daily tado quota nearly exhausted ({client.rate_limit.remaining} left). "
                    "Re-run after it refills; progress is saved."
                )
                stop = True

    return result


@dataclass
class ExtrasResult:
    running_days: int = 0
    consumption_days: int = 0
    months: int = 0
    readings: int = 0
    tariffs: int = 0
    calls: int = 0
    skipped_months: int = 0
    errors: list[str] = field(default_factory=list)


def _months(start: date, end: date) -> list[str]:
    out, cursor = [], date(start.year, start.month, 1)
    while cursor <= end:
        out.append(cursor.strftime("%Y-%m"))
        cursor = date(cursor.year + (cursor.month == 12), (cursor.month % 12) + 1, 1)
    return out


def sync_extras(
    conn: sqlite3.Connection,
    client: TadoClient,
    home_id: int,
    start: date,
    end: date,
    *,
    country: str | None = None,
    force: bool = False,
    progress=None,
) -> ExtrasResult:
    """Fetch the sources that outlive the dayReport retention window.

    Heating running times come back for the whole span in a single call; Energy
    IQ consumption is one call per month, so months already stored are skipped
    unless ``force`` is set. Everything is kept verbatim in ``raw_document``
    alongside the parsed rows.
    """
    result = ExtrasResult()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def run(label: str, fn):
        try:
            return fn()
        except (TadoApiError, RateLimitExceeded) as exc:
            result.errors.append(f"{label}: {exc}")
            return None

    # --- running times: one call covers everything ------------------------
    payload = run("runningTimes", lambda: client.running_times(home_id, start, end))
    result.calls = client.calls_made
    if payload:
        key = f"{start.isoformat()}..{end.isoformat()}"
        db.store_document(conn, home_id, "running_times", key, payload, stamp)
        rows = ingest.parse_running_times(home_id, payload)
        db.write_rows(conn, rows)
        result.running_days = len(rows["running_time_day"])
        conn.commit()
    if progress:
        progress(result)

    # --- Energy IQ ---------------------------------------------------------
    if country:
        have = set() if force else db.document_keys(conn, home_id, "consumption")
        # The current and previous month are still moving, so always refresh them.
        volatile = {end.strftime("%Y-%m"), (end.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")}
        for month in _months(start, end):
            if month in have and month not in volatile:
                result.skipped_months += 1
                continue
            doc = run(f"consumption {month}", lambda m=month: client.eiq_consumption(home_id, m, country))
            if not doc:
                continue
            db.store_document(conn, home_id, "consumption", month, doc, stamp)
            rows = ingest.parse_consumption(home_id, doc)
            db.write_rows(conn, rows)
            result.consumption_days += len(rows["energy_consumption"])
            result.months += 1
            conn.commit()
            if progress:
                progress(result)

        readings = run("meterReadings", lambda: client.eiq_meter_readings(home_id))
        if readings:
            db.store_document(conn, home_id, "meter_readings", "", readings, stamp)
            rows = ingest.parse_meter_readings(home_id, readings)
            db.write_rows(conn, rows)
            result.readings = len(rows["meter_reading"])

        tariffs = run("tariffs", lambda: client.eiq_tariffs(home_id))
        if tariffs:
            db.store_document(conn, home_id, "tariffs", "", tariffs, stamp)
            rows = ingest.parse_tariffs(home_id, tariffs)
            db.write_rows(conn, rows)
            result.tariffs = len(rows["tariff"])
        conn.commit()

    result.calls = client.calls_made
    return result


def reparse(
    conn: sqlite3.Connection, home_id: int | None = None, progress=None
) -> tuple[int, int]:
    """Rebuild the normalised tables from the stored raw payloads.

    A full rebuild rather than an upsert, so rows written by an older version of
    the parser — filler days included — actually disappear. Costs no API calls;
    ``day_report_raw`` is never touched.

    Returns (real_days, placeholder_days).
    """
    days = list(db.iter_raw_days(conn, home_id))
    db.purge_derived(conn, home_id)
    batch: dict[str, list[tuple]] = {}
    processed = 0
    skipped = 0
    for row in days:
        payload = db.load_raw(conn, row["home_id"], row["zone_id"], row["day"])
        if payload is None:
            continue
        if ingest.is_placeholder(payload):
            skipped += 1
            # The raw payload is filler, so there is nothing real to preserve here.
            db.mark_day(conn, row["home_id"], row["zone_id"],
                        date.fromisoformat(row["day"]), "placeholder",
                        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        note="tado returned filler, not measurements")
            continue
        ingest.merge_rows(batch, ingest.parse_day_report(row["home_id"], row["zone_id"], payload))
        processed += 1
        if processed % 200 == 0:
            db.write_rows(conn, batch)
            conn.commit()
            batch = {}
            if progress:
                progress(processed, len(days))
    db.write_rows(conn, batch)
    conn.commit()
    if progress:
        progress(processed, len(days))
    return processed, skipped
