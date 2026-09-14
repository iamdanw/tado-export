"""Command line entry point."""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

from . import db, exporter, report as report_mod, sync as sync_mod
from .api import TadoApiError, TadoClient
from .auth import AuthError, TokenManager, TokenStore, device_login
from .config import (
    DEFAULT_DAILY_BUDGET,
    DEFAULT_PROFILE,
    DEFAULT_REFRESH_WINDOW_DAYS,
    FREE_TIER_DAILY_BUDGET,
    default_db_path,
    known_profiles,
)


def _progress_writer():
    """Redraw in place on a terminal; emit plain lines when piped to a file.

    Carriage returns concatenate into one unreadable line in a log, so a
    non-interactive run gets periodic newlines instead.
    """
    interactive = sys.stdout.isatty()
    state = {"last": 0.0}

    def write(text: str, *, final: bool = False) -> None:
        if interactive:
            sys.stdout.write("\r" + text + "   ")
            sys.stdout.flush()
            if final:
                sys.stdout.write("\n")
            return
        now = time.monotonic()
        if final or now - state["last"] >= 5:
            state["last"] = now
            print(text.strip())

    return write


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{value}' is not a date (expected YYYY-MM-DD)")


def _open_db(args) -> "db.sqlite3.Connection":
    conn = db.connect(args.db)
    db.init(conn)
    return conn


def _refresh_home_metadata(conn, client: TadoClient, home_id: int | None) -> list[int]:
    """Ensure home + zone rows exist. Returns the home ids that are known."""
    me = client.me()
    ids = [h["id"] for h in me.get("homes", [])]
    if home_id is not None and home_id not in ids:
        raise SystemExit(f"Home {home_id} is not on this tado account (found: {ids or 'none'}).")
    targets = [home_id] if home_id is not None else ids

    for hid in targets:
        home = client.home(hid)
        db.upsert_home(conn, home)
        zones = client.zones(hid)
        if not zones and home.get("generation") == "LINE_X":
            zones = _tado_x_rooms_as_zones(client, hid)
        db.upsert_zones(conn, hid, zones)
    conn.commit()
    return targets


def _tado_x_rooms_as_zones(client: TadoClient, home_id: int) -> list[dict]:
    """Reshape tado X rooms into the classic zone dict shape.

    That keeps everything downstream — ``db.upsert_zones``, and ``dayReport``
    called with a room id in place of a zoneId — unaware of the difference.
    Rooms carry no creation date or zone type of their own: tado X is heating
    only (hot water is a home-level feature there, not a zone).
    """
    payload = client.rooms_and_devices(home_id) or {}
    zones = []
    for room in payload.get("rooms", []):
        if room.get("roomId") is None:
            continue
        zones.append(
            {
                "id": room["roomId"],
                "name": room.get("roomName"),
                "type": "HEATING",
                "dateCreated": None,
                "deviceTypes": [d["type"] for d in room.get("devices", []) if d.get("type")],
            }
        )
    return zones


# -- commands -------------------------------------------------------------


def cmd_login(args) -> int:
    store = TokenStore(profile=args.profile)
    device_login(store)
    client = TadoClient(TokenManager(store))
    me = client.me()
    print(f"\nLinked as {me.get('name') or me.get('username')} (profile '{args.profile}').")
    for home in me.get("homes", []):
        print(f"  home {home['id']}  {home.get('name', '')}")
    return 0


def cmd_logout(args) -> int:
    store = TokenStore(profile=args.profile)
    store.clear()
    print(f"Removed {store.path}.")
    return 0


def cmd_homes(args) -> int:
    conn = _open_db(args)
    client = TadoClient(TokenManager(profile=args.profile))
    for home_id in _refresh_home_metadata(conn, client, args.home):
        row = conn.execute("SELECT name, timezone, generation FROM home WHERE id=?", (home_id,)).fetchone()
        print(f"\nHome {home_id} — {row['name']}  [{row['generation'] or 'unknown generation'}, tz {row['timezone']}]")
        for zone in conn.execute(
            "SELECT id, name, type, device_types FROM zone WHERE home_id=? ORDER BY id", (home_id,)
        ):
            print(f"   zone {zone['id']:>3}  {zone['name']:<24} {zone['type']:<18} {zone['device_types']}")
    print()
    return 0


def cmd_sync(args) -> int:
    conn = _open_db(args)
    client = TadoClient(TokenManager(profile=args.profile), min_interval_s=args.min_interval)

    home_ids = _refresh_home_metadata(conn, client, args.home)
    if not home_ids:
        raise SystemExit("No homes found on this tado account.")

    overall = 0
    for home_id in home_ids:
        zone_ids = [
            int(r["id"])
            for r in conn.execute("SELECT id FROM zone WHERE home_id=? ORDER BY id", (home_id,))
        ]
        if args.zone:
            unknown = set(args.zone) - set(zone_ids)
            if unknown:
                raise SystemExit(f"Home {home_id} has no zone(s) {sorted(unknown)}. Known: {zone_ids}")
            zone_ids = [z for z in zone_ids if z in set(args.zone)]
        if not zone_ids:
            print(f"Home {home_id}: no zones, skipping.")
            continue

        end = args.to or (sync_mod.today_local(conn, home_id) - timedelta(days=0))
        if args.days is not None:
            start = end - timedelta(days=args.days - 1)
        else:
            start = args.since or sync_mod.earliest_known_day(conn, home_id, zone_ids)
        if start > end:
            raise SystemExit(f"--since {start} is after --to {end}.")

        total_days = (end - start).days + 1
        print(
            f"\nHome {home_id}: {len(zone_ids)} zone(s), {start} → {end} "
            f"({total_days} days, up to {total_days * len(zone_ids):,} API calls)"
        )

        write = _progress_writer()

        def show(result: sync_mod.SyncResult) -> None:
            quota = f", quota left {result.remaining_quota:,}" if result.remaining_quota is not None else ""
            done = result.fetched + result.empty + result.failed
            write(
                f"  {done:,} fetched · {result.measurements:,} readings · "
                f"{result.pending:,} to go{quota}"
            )

        try:
            result = sync_mod.sync(
                conn, client, home_id, zone_ids, start, end,
                workers=args.workers,
                refresh_window_days=args.refresh_window,
                force=args.force,
                max_calls=args.max_calls,
                progress=show,
            )
        except AuthError as exc:
            raise SystemExit(str(exc))
        except TadoApiError as exc:
            raise SystemExit(f"tado API error: {exc}")

        write("", final=True)
        print(
            f"  done: {result.fetched:,} days stored, {result.skipped:,} already had, "
            f"{result.empty:,} with no data, {result.failed:,} failed, "
            f"{result.measurements:,} readings, {result.calls:,} API calls"
        )
        if result.protected:
            print(
                f"  kept: {result.protected:,} day(s) already held real measurements that "
                "tado no longer serves — the stored data was left untouched"
            )
        if result.placeholder:
            print(
                f"  note: {result.placeholder:,} day(s) came back as filler rather than "
                "measurements — tado only retains about 13 months of history"
            )
        for message in result.errors:
            print(f"    ! {message}")
        if result.failed > len(result.errors):
            print(f"    ! ...and {result.failed - len(result.errors)} more failures")
        if result.stopped_reason:
            print(f"  stopped early: {result.stopped_reason}")
            overall = 1

        if not args.no_extras:
            overall = max(overall, _sync_extras(conn, client, home_id, start, end, args))

    return overall


def _sync_extras(conn, client, home_id: int, start: date, end: date, args) -> int:
    """Running times and Energy IQ: history that outlives the dayReport window."""
    row = conn.execute("SELECT raw FROM home WHERE id=?", (home_id,)).fetchone()
    country = None
    if row and row["raw"]:
        import json as _json
        country = ((_json.loads(row["raw"]).get("address") or {}).get("country"))

    # These sources reach back to when the home was created, not just 13 months.
    created = _parse_home_created(conn, home_id) or start
    extras_start = min(start, created)

    print(f"  extras: running times and Energy IQ from {extras_start} "
          f"({'country ' + country if country else 'no country on file, skipping Energy IQ'})")

    write = _progress_writer()

    def show(r):
        write(f"    {r.running_days:,} running-time days · {r.months:,} months "
              f"({r.consumption_days:,} consumption days)")

    result = sync_mod.sync_extras(
        conn, client, home_id, extras_start, end,
        country=country, force=args.force, progress=show,
    )
    write("", final=True)
    print(
        f"  extras done: {result.running_days:,} running-time days, "
        f"{result.consumption_days:,} consumption days across {result.months:,} months, "
        f"{result.readings:,} meter readings, {result.tariffs:,} tariffs"
        + (f", {result.skipped_months:,} months already stored" if result.skipped_months else "")
    )
    for message in result.errors[:5]:
        print(f"    ! {message}")
    return 1 if result.errors else 0


def _parse_home_created(conn, home_id: int):
    row = conn.execute("SELECT date_created FROM home WHERE id=?", (home_id,)).fetchone()
    if not row or not row["date_created"]:
        return None
    return sync_mod._parse_created(row["date_created"])


def cmd_status(args) -> int:
    conn = _open_db(args)
    homes = list(conn.execute("SELECT id, name, timezone FROM home ORDER BY id"))
    if not homes:
        print("Nothing synced yet. Run `tado-export login` then `tado-export sync`.")
        return 0

    print(f"Database: {Path(args.db).resolve()}")
    linked = known_profiles()
    if linked:
        print(f"Linked accounts: {', '.join(linked)}")
    for home in homes:
        print(f"\nHome {home['id']} — {home['name']} (tz {home['timezone']})")
        rows = list(conn.execute(
            """
            SELECT z.id, z.name, z.type,
                   COUNT(CASE WHEN s.status='ok' THEN 1 END)    AS ok_days,
                   COUNT(CASE WHEN s.status='empty' THEN 1 END) AS empty_days,
                   COUNT(CASE WHEN s.status='error' THEN 1 END) AS error_days,
                   COUNT(CASE WHEN s.status='placeholder' THEN 1 END) AS filler_days,
                   MIN(CASE WHEN s.status='ok' THEN s.day END)  AS first_day,
                   MAX(CASE WHEN s.status='ok' THEN s.day END)  AS last_day,
                   COALESCE(SUM(s.measurements), 0)             AS readings
            FROM zone z LEFT JOIN sync_day s
              ON s.home_id = z.home_id AND s.zone_id = z.id
            WHERE z.home_id = ?
            GROUP BY z.id ORDER BY z.id
            """,
            (home["id"],),
        ))
        header = (f"  {'zone':<5}{'name':<22}{'days':>7}{'gaps':>7}{'filler':>8}"
                  f"{'readings':>11}   range")
        print(header)
        print("  " + "-" * (len(header) - 2))
        for row in rows:
            span = f"{row['first_day']} → {row['last_day']}" if row["first_day"] else "—"
            gaps = _gap_count(row["first_day"], row["last_day"], row["ok_days"], row["empty_days"])
            print(
                f"  {row['id']:<5}{(row['name'] or ''):<22}{row['ok_days']:>7}{gaps:>7}"
                f"{row['filler_days']:>8}{row['readings']:>11,}   {span}"
                + (f"  [{row['error_days']} errors]" if row["error_days"] else "")
            )

        filler_total = sum(r["filler_days"] for r in rows)
        if filler_total:
            print(
                "\n  'filler' = days tado answered with synthetic values instead of\n"
                "  measurements. It retains roughly 13 months of history; older days are\n"
                "  recorded as such and kept out of the charts and exports."
            )

    size_mb = Path(args.db).stat().st_size / 1e6
    print(f"\nDatabase size: {size_mb:,.1f} MB")
    return 0


def _gap_count(first: str | None, last: str | None, ok_days: int, empty_days: int) -> int:
    if not first or not last:
        return 0
    span = (date.fromisoformat(last) - date.fromisoformat(first)).days + 1
    return max(span - ok_days - empty_days, 0)


def cmd_export(args) -> int:
    conn = _open_db(args)
    home_id = args.home or _only_home(conn)
    tz_row = conn.execute("SELECT timezone FROM home WHERE id=?", (home_id,)).fetchone()
    tz = tz_row["timezone"] if tz_row and tz_row["timezone"] else "UTC"

    written = exporter.export(
        conn, home_id, Path(args.out),
        zone_ids=args.zone or None,
        start=args.since, end=args.to,
        fmt=args.format, tz=tz,
        tables=args.tables,
    )
    for name, (path, count) in written.items():
        print(f"  {count:>9,} rows  {path}")
    return 0


def cmd_report(args) -> int:
    conn = _open_db(args)
    home_id = args.home or _only_home(conn)
    path, size = report_mod.write(
        conn, home_id, args.out,
        zone_ids=args.zone or None,
        start=args.since, end=args.to,
        cdn=args.cdn,
        title=args.title,
    )
    how = "needs internet for plotly.js" if args.cdn else "fully self-contained"
    print(f"  {path}  ({size / 1e6:,.1f} MB, {how})")
    return 0


def cmd_reparse(args) -> int:
    conn = _open_db(args)

    write = _progress_writer()

    def show(done: int, total: int) -> None:
        write(f"  reparsed {done:,}/{total:,} stored day reports")

    real, filler = sync_mod.reparse(conn, args.home, progress=show)
    write("", final=True)
    print(f"  rebuilt normalised tables from {real:,} real day reports (0 API calls)")
    if filler:
        print(f"  skipped {filler:,} day(s) where tado returned filler instead of measurements")
    return 0


def cmd_dashboard(args) -> int:
    from .dashboard import serve  # imported lazily: pulls in dash

    serve(args.db, host=args.host, port=args.port, debug=args.debug)
    return 0


def _only_home(conn) -> int:
    rows = [r["id"] for r in conn.execute("SELECT id FROM home ORDER BY id")]
    if not rows:
        raise SystemExit("No homes in the database. Run `tado-export sync` first.")
    if len(rows) > 1:
        raise SystemExit(f"Several homes stored ({rows}). Pick one with --home.")
    return rows[0]


# -- parser ---------------------------------------------------------------


class _SubParser(argparse.ArgumentParser):
    """Subparsers inherit the shared options; nothing else differs."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tado-export",
        description="Export historic measurement data from tado° and visualise it locally.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "typical first run:\n"
            "  tado-export login\n"
            "  tado-export homes\n"
            "  tado-export sync --days 30\n"
            "  tado-export dashboard\n"
            "  tado-export report --out tado.html    # one shareable file\n"
        ),
    )
    # --db is accepted either before or after the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    # SUPPRESS so an unused subcommand flag does not clobber the top-level one.
    common.add_argument("--db", default=argparse.SUPPRESS,
                        help="SQLite file to read/write (default: ./tado.db, or $TADO_EXPORT_DB)")
    common.add_argument("--profile", default=argparse.SUPPRESS,
                        help="which tado account to use (default: 'default'). "
                             "Each profile stores its own token; all of them can sync "
                             "into the same database.")
    parser.add_argument("--db", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--profile", default=None, help=argparse.SUPPRESS)

    sub = parser.add_subparsers(dest="command", required=True, parser_class=_SubParser)

    sub.add_parser("login", parents=[common], help="link this machine to tado (OAuth device flow)").set_defaults(func=cmd_login)
    sub.add_parser("logout", parents=[common], help="forget the stored refresh token").set_defaults(func=cmd_logout)

    homes = sub.add_parser("homes", parents=[common], help="list homes and zones")
    homes.add_argument("--home", type=int, help="restrict to one home id")
    homes.set_defaults(func=cmd_homes)

    s = sub.add_parser("sync", parents=[common], help="download day reports into the database")
    s.add_argument("--home", type=int, help="home id (default: every home on the account)")
    s.add_argument("--zone", type=int, action="append", help="zone id; repeatable (default: all)")
    s.add_argument("--since", type=_parse_date, help="first day, YYYY-MM-DD (default: when the zones were created)")
    s.add_argument("--to", type=_parse_date, help="last day, YYYY-MM-DD (default: today)")
    s.add_argument("--days", type=int, help="shorthand for the last N days, counting back from --to")
    s.add_argument("--workers", type=int, default=4, help="parallel requests (default: 4)")
    s.add_argument("--min-interval", type=float, default=0.0,
                   help="seconds between requests, to pace the API (default: 0)")
    s.add_argument("--max-calls", type=int,
                   help=f"stop after N API calls this run (quota is {FREE_TIER_DAILY_BUDGET}/day "
                        f"without a subscription, {DEFAULT_DAILY_BUDGET:,}/day with Auto-Assist)")
    s.add_argument("--refresh-window", type=int, default=DEFAULT_REFRESH_WINDOW_DAYS,
                   help="re-fetch the last N days even if already stored (default: %(default)s)")
    s.add_argument("--force", action="store_true", help="re-fetch every day in range, ignoring what is stored")
    s.add_argument("--no-extras", action="store_true",
                   help="skip heating running times and Energy IQ (they reach further "
                        "back than dayReport, so this loses history you cannot get later)")
    s.set_defaults(func=cmd_sync)

    st = sub.add_parser("status", parents=[common], help="show coverage and gaps per zone")
    st.set_defaults(func=cmd_status)

    e = sub.add_parser("export", parents=[common], help="write CSV or Parquet files")
    e.add_argument("--home", type=int)
    e.add_argument("--zone", type=int, action="append")
    e.add_argument("--since", type=_parse_date)
    e.add_argument("--to", type=_parse_date)
    e.add_argument("--out", default="export", help="output directory (default: ./export)")
    e.add_argument("--format", choices=["csv", "parquet"], default="csv")
    e.add_argument("--tables", nargs="+",
                   choices=["enriched", "measurements", "setpoints", "call_for_heat", "stripes",
                            "weather", "running_times", "energy", "meter_readings", "tariffs"],
                   help="which datasets to write (default: all)")
    e.set_defaults(func=cmd_export)

    rp = sub.add_parser("report", parents=[common],
                        help="build one shareable, self-contained HTML report")
    rp.add_argument("--home", type=int)
    rp.add_argument("--zone", type=int, action="append",
                    help="zone id; repeatable (default: all non hot-water zones)")
    rp.add_argument("--since", type=_parse_date)
    rp.add_argument("--to", type=_parse_date)
    rp.add_argument("--out", default="tado-report.html", help="output file (default: %(default)s)")
    rp.add_argument("--title", help="override the document title")
    rp.add_argument("--cdn", action="store_true",
                    help="load plotly.js from a CDN instead of inlining it: ~200 KB "
                         "instead of ~5 MB, but the file then needs internet to draw")
    rp.set_defaults(func=cmd_report)

    r = sub.add_parser("reparse", parents=[common], help="rebuild tables from stored raw payloads (no API calls)")
    r.add_argument("--home", type=int)
    r.set_defaults(func=cmd_reparse)

    d = sub.add_parser("dashboard", parents=[common], help="serve the local visualisation")
    d.add_argument("--host", default="127.0.0.1")
    d.add_argument("--port", type=int, default=8050)
    d.add_argument("--debug", action="store_true")
    d.set_defaults(func=cmd_dashboard)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "db", None) is None:
        args.db = str(default_db_path())
    if getattr(args, "profile", None) is None:
        args.profile = DEFAULT_PROFILE
    try:
        return args.func(args)
    except AuthError as exc:
        print(f"\nAuthentication problem: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted. Progress is saved — re-run the same command to continue.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
