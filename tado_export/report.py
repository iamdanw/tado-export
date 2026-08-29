"""Build a single self-contained HTML report.

The output is one file with no external dependencies: the plotly bundle, all
chart data, the styles, the filter controls and the theme toggle are inlined, so
it opens from a USB stick or an email attachment with no network at all.

The page is interactive without a server. Series are embedded once at hourly
resolution, together with per-zone-per-day sums, and the browser rebuilds the
charts, tiles and tables whenever the zone or date filters change. Aggregates
come from the sums rather than from the plotted points, so any range is exact.
"""

from __future__ import annotations

import html
import json
import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import plotly.io as pio
from plotly.offline import get_plotlyjs

from . import charts, frames
from .theme import FONT_STACK, SEQUENTIAL_BLUE, SURFACE, zone_colors

MODES = ("light", "dark")
BASE_MODE = "light"

# Style keys that differ between themes. Everything else is data, which is
# identical in both and therefore emitted only once.
_STYLE_KEYS = ("line", "marker", "colorscale", "colorbar", "opacity",
               "fillcolor", "textfont")

# Above this many days the charts switch from the hourly series to the daily
# aggregates: bucket means rather than thinned points, so nothing aliases.
HOURLY_SPAN_LIMIT = 60


def _cdn_url() -> str:
    """CDN build matching the plotly.js this plotly version bundles."""
    match = re.search(r"plotly\.js v([0-9][0-9.]*)", get_plotlyjs()[:4000])
    version = match.group(1) if match else "3.0.1"
    return f"https://cdn.plot.ly/plotly-{version}.min.js"


def _pretty_type(value) -> str:
    return {"HEATING": "Heating", "HOT_WATER": "Hot water",
            "AIR_CONDITIONING": "Air conditioning"}.get(value, str(value or "-"))


def _round(series, digits=2):
    """A JSON-safe list of numbers, with NaN as null."""
    return [None if pd.isna(v) else round(float(v), digits) for v in series]


def _facts(
    hourly: pd.DataFrame,
    daily_heat: pd.DataFrame,
    raw: pd.DataFrame,
    zone_rows: pd.DataFrame,
    selected: list[int],
) -> dict:
    """Everything the page needs to recompute charts, tiles and tables in the browser.

    Series are emitted once at hourly resolution, plus per-zone-per-day sums so
    that any date range can be aggregated exactly rather than approximated.
    """
    days = sorted({str(d) for d in raw["local_date"]})
    day_index = {d: i for i, d in enumerate(days)}

    # Canonical hourly grid. Built from the data rather than assuming 24 slots a
    # day, because a DST change makes a local day 23 or 25 hours long.
    stamps = sorted({t for t in hourly["ts_local"]})
    stamp_key = [s.strftime("%Y-%m-%dT%H:%M") for s in stamps]
    stamp_index = {s: i for i, s in enumerate(stamps)}
    n_hours = len(stamps)

    # First hour index belonging to each day, so a date range slices in O(1).
    day_start = [n_hours] * (len(days) + 1)
    day_start[len(days)] = n_hours
    for i, s in enumerate(stamps):
        d = day_index.get(str(s.date()))
        if d is not None and i < day_start[d]:
            day_start[d] = i
    for i in range(len(days) - 1, -1, -1):
        if day_start[i] == n_hours:
            day_start[i] = day_start[i + 1]

    zone_meta = {int(r["id"]): r for _, r in zone_rows.iterrows()}
    heat_lookup = {
        (int(r.zone_id), str(r.local_date)): (float(r.hours), float(r.coverage))
        for r in daily_heat.itertuples()
    }

    zones, series, summary, demand = [], {}, {}, {}
    for zone_id in selected:
        meta = zone_meta.get(zone_id)
        name = str(meta["name"]) if meta is not None and meta["name"] else f"Zone {zone_id}"
        ztype = str(meta["type"]) if meta is not None else "HEATING"
        zones.append({"id": zone_id, "name": name, "type": ztype,
                      "pretty": _pretty_type(ztype)})

        part = hourly[hourly["zone_id"] == zone_id]
        temp = [None] * n_hours
        setp = [None] * n_hours
        hum = [None] * n_hours
        for row in part.itertuples():
            i = stamp_index.get(row.ts_local)
            if i is None:
                continue
            if not pd.isna(row.inside_temp_c):
                temp[i] = round(float(row.inside_temp_c), 2)
            if not pd.isna(getattr(row, "setpoint_c", float("nan"))):
                setp[i] = round(float(row.setpoint_c), 2)
            if not pd.isna(row.humidity_pct):
                hum[i] = round(float(row.humidity_pct), 1)
        series[str(zone_id)] = {"temp": temp, "setpoint": setp, "humidity": hum}

        # Per-day aggregates, from the 15-minute readings so every range total is exact.
        grouped = raw[raw["zone_id"] == zone_id].groupby("local_date", observed=True)
        agg = grouped["inside_temp_c"].agg(["sum", "count", "min", "max"])
        hagg = grouped["humidity_pct"].agg(["sum", "count"])
        t_sum, t_n, t_min, t_max, h_sum, h_n, hours, cov = ([] for _ in range(8))
        for d in days:
            key = pd.to_datetime(d).date()
            if key in agg.index:
                row = agg.loc[key]
                t_sum.append(None if pd.isna(row["sum"]) else round(float(row["sum"]), 2))
                t_n.append(int(row["count"]))
                t_min.append(None if pd.isna(row["min"]) else round(float(row["min"]), 2))
                t_max.append(None if pd.isna(row["max"]) else round(float(row["max"]), 2))
            else:
                t_sum.append(None); t_n.append(0); t_min.append(None); t_max.append(None)
            if key in hagg.index:
                hrow = hagg.loc[key]
                h_sum.append(None if pd.isna(hrow["sum"]) else round(float(hrow["sum"]), 1))
                h_n.append(int(hrow["count"]))
            else:
                h_sum.append(None); h_n.append(0)
            hh, cc = heat_lookup.get((zone_id, d), (None, None))
            hours.append(None if hh is None else round(hh, 3))
            cov.append(None if cc is None else round(cc, 3))
        summary[str(zone_id)] = {"tSum": t_sum, "tN": t_n, "tMin": t_min, "tMax": t_max,
                                 "hSum": h_sum, "hN": h_n, "hours": hours, "cov": cov}

        # Mean boiler demand per day and hour, for the heatmap.
        cells = [None] * (len(days) * 24)
        part_raw = raw[raw["zone_id"] == zone_id].dropna(subset=["call_for_heat_num"])
        if not part_raw.empty:
            means = part_raw.groupby(["local_date", "hour"], observed=True)["call_for_heat_num"].mean()
            for (d, h), value in means.items():
                di = day_index.get(str(d))
                if di is not None:
                    cells[di * 24 + int(h)] = round(float(value), 3)
        demand[str(zone_id)] = cells

    # Weather is home-wide: one series, not one per zone.
    out_hourly = (
        hourly[["ts_local", "outside_temp_c"]].dropna()
        .drop_duplicates("ts_local").set_index("ts_local")["outside_temp_c"]
    )
    outside_hours = [None] * n_hours
    for s, v in out_hourly.items():
        i = stamp_index.get(s)
        if i is not None:
            outside_hours[i] = round(float(v), 2)

    out_raw = raw[["ts", "local_date", "outside_temp_c"]].dropna().drop_duplicates("ts")
    oagg = out_raw.groupby("local_date", observed=True)["outside_temp_c"].agg(["sum", "count", "min", "max"])
    o_sum, o_n, o_min, o_max = [], [], [], []
    for d in days:
        key = pd.to_datetime(d).date()
        if key in oagg.index:
            row = oagg.loc[key]
            o_sum.append(round(float(row["sum"]), 2)); o_n.append(int(row["count"]))
            o_min.append(round(float(row["min"]), 2)); o_max.append(round(float(row["max"]), 2))
        else:
            o_sum.append(None); o_n.append(0); o_min.append(None); o_max.append(None)

    return {
        "days": days,
        "stamps": stamp_key,
        "dayStart": day_start,
        "zones": zones,
        "series": series,
        "summary": summary,
        "demand": demand,
        "outside": {"hourly": outside_hours, "sum": o_sum, "n": o_n, "min": o_min, "max": o_max},
        "hourlySpanLimit": HOURLY_SPAN_LIMIT,
    }


def _skeletons(figures: dict) -> dict:
    """Layouts and per-theme colours; the traces themselves are built in the browser."""
    out = {"layout": {}, "colors": {}}
    for mode, group in figures.items():
        rendered = {name: json.loads(pio.to_json(fig)) for name, fig in group.items()}
        out["layout"][mode] = {name: fig["layout"] for name, fig in rendered.items()}
    return out


def build(
    conn: sqlite3.Connection,
    home_id: int,
    *,
    zone_ids: list[int] | None = None,
    start: date | None = None,
    end: date | None = None,
    cdn: bool = False,
    title: str | None = None,
) -> str:
    """Render the whole report and return it as one HTML string."""
    home = conn.execute("SELECT name, timezone FROM home WHERE id=?", (home_id,)).fetchone()
    if home is None:
        raise SystemExit(f"Home {home_id} is not in this database.")
    tz = home["timezone"] or "UTC"
    home_name = home["name"] or f"Home {home_id}"

    extent_lo, extent_hi = frames.data_extent(conn, home_id)
    start = start or extent_lo
    end = end or extent_hi
    if start is None or end is None:
        raise SystemExit("This database has no synced days yet - run `tado-export sync` first.")

    zone_rows = frames.zones(conn, home_id)
    all_zone_ids = [int(z) for z in zone_rows["id"]]
    if zone_ids:
        selected = [z for z in all_zone_ids if z in set(zone_ids)]
    else:
        selected = [int(r["id"]) for _, r in zone_rows.iterrows()
                    if r["type"] != "HOT_WATER"] or all_zone_ids
    if not selected:
        raise SystemExit("No zones selected.")

    raw = frames.resample(frames.enriched(conn, home_id, selected, start, end, tz=tz), None)
    if raw.empty:
        raise SystemExit(f"No measurements stored for {start} -> {end}.")

    hourly = frames.resample(raw, "1h", min_coverage=0.5)
    daily = frames.resample(raw, "1D", min_coverage=0.5)
    daily_heat = frames.daily_heating(raw, min_coverage=0.0)

    # Built only to harvest axis/title/template layouts; the browser supplies the
    # traces, so which slice of data goes in here does not matter.
    colors_by_mode = {mode: zone_colors(all_zone_ids, mode) for mode in MODES}
    figures = {
        mode: {
            "temp": charts.temperature(daily, colors_by_mode[mode], mode, show_labels=False),
            "humidity": charts.humidity(daily, colors_by_mode[mode], mode, show_labels=False),
            "demand": charts.demand_heatmap(frames.hourly_demand(raw), mode),
            "hours": charts.heating_hours(frames.daily_heating(raw), colors_by_mode[mode], mode),
        }
        for mode in MODES
    }

    facts = _facts(hourly, daily_heat, raw, zone_rows, selected)
    for zone in facts["zones"]:
        zone["color"] = {mode: colors_by_mode[mode][zone["id"]] for mode in MODES}
    facts.update(_skeletons(figures))
    facts["reference"] = {mode: SURFACE[mode]["reference"] for mode in MODES}
    facts["panel"] = {mode: SURFACE[mode]["panel"] for mode in MODES}
    facts["sequential"] = SEQUENTIAL_BLUE

    generated = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    document = _HTML
    for token, value in {
        "__TITLE__": html.escape(title or f"tado report - {home_name}"),
        "__HOME__": html.escape(home_name),
        "__SUBTITLE__": html.escape(
            f"{start:%d %b %Y} - {end:%d %b %Y} - {len(selected)} zones - times in {tz}"
        ),
        "__GENERATED__": html.escape(generated),
        "__CSS__": _css(),
        "__ZONE_CHECKBOXES__": _zone_controls(facts["zones"]),
        "__SCRIPT_TAG__": (f'<script src="{_cdn_url()}" charset="utf-8"></script>'
                           if cdn else f"<script>{get_plotlyjs()}</script>"),
        "__REPORT__": json.dumps(facts, separators=(",", ":")),
        "__APP_JS_TOKEN__": _app_js(),
    }.items():
        document = document.replace(token, value)
    return document


def _zone_controls(zones: list[dict]) -> str:
    return "".join(
        '<label class="chk">'
        f'<input type="checkbox" class="zone" value="{z["id"]}" checked>'
        f'<span class="dot" style="background:{z["color"]["light"]}"></span>'
        f'{html.escape(z["name"])}</label>'
        for z in zones
    )


def write(conn: sqlite3.Connection, home_id: int, out_path: str | Path, **kwargs) -> tuple[Path, int]:
    """Build the report and write it. Returns (path, bytes)."""
    document = build(conn, home_id, **kwargs)
    path = Path(out_path)
    if path.suffix.lower() not in (".html", ".htm"):
        path = path.with_suffix(".html")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")
    return path, path.stat().st_size


def _app_js() -> str:
    return (Path(__file__).with_name("report_app.js")).read_text(encoding="utf-8")


def _css() -> str:
    """Fill the palette tokens. Plain replacement, so a stray % in a rule
    (``border-radius: 50%``) can never be mistaken for a format placeholder."""
    tokens = {"__FONT__": FONT_STACK}
    for prefix, mode in (("L", "light"), ("D", "dark")):
        for key, value in SURFACE[mode].items():
            tokens[f"__{prefix}_{key.upper()}__"] = value
    css = _CSS
    for token, value in tokens.items():
        css = css.replace(token, value)
    return css


_CSS = """
:root {
  --surface: __L_SURFACE__; --panel: __L_PANEL__; --border: __L_BORDER__;
  --text-primary: __L_TEXT_PRIMARY__; --text-secondary: __L_TEXT_SECONDARY__;
  --text-muted: __L_TEXT_MUTED__;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --surface: __D_SURFACE__; --panel: __D_PANEL__; --border: __D_BORDER__;
    --text-primary: __D_TEXT_PRIMARY__; --text-secondary: __D_TEXT_SECONDARY__;
    --text-muted: __D_TEXT_MUTED__;
    color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --surface: __D_SURFACE__; --panel: __D_PANEL__; --border: __D_BORDER__;
  --text-primary: __D_TEXT_PRIMARY__; --text-secondary: __D_TEXT_SECONDARY__;
  --text-muted: __D_TEXT_MUTED__;
  color-scheme: dark;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--surface); color: var(--text-primary);
       font-family: __FONT__; line-height: 1.55;
       -webkit-font-smoothing: antialiased; }
.wrap { max-width: 1180px; margin: 0 auto; padding: 32px 22px 80px; }

header { display: flex; justify-content: space-between; align-items: flex-start;
         gap: 20px; flex-wrap: wrap; padding-bottom: 20px;
         border-bottom: 1px solid var(--border); margin-bottom: 26px; }
h1 { font-size: 24px; font-weight: 600; margin: 0; letter-spacing: -0.02em; }
header .sub { font-size: 13.5px; color: var(--text-secondary); margin-top: 6px; }
header .gen { font-size: 12px; color: var(--text-muted); margin-top: 3px; }
button.ghost { background: none; border: 1px solid var(--border);
               color: var(--text-secondary); border-radius: 8px;
               padding: 8px 13px; font-size: 12.5px; cursor: pointer;
               font-family: inherit; white-space: nowrap; }
button.ghost:hover { color: var(--text-primary); border-color: var(--text-muted); }

h2 { font-size: 15px; font-weight: 600; margin: 34px 0 12px;
     letter-spacing: 0.01em; }
h2 .hint { font-weight: 400; font-size: 12.5px; color: var(--text-muted);
           margin-left: 8px; }

.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
         gap: 12px; margin-bottom: 8px; }
.tile { background: var(--panel); border: 1px solid var(--border);
        border-radius: 12px; padding: 14px 16px; }
.tile .k { font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.07em;
           color: var(--text-muted); font-weight: 600; }
.tile .v { font-size: 25px; font-weight: 600; margin-top: 5px;
           letter-spacing: -0.02em; font-variant-numeric: tabular-nums; }
.tile .n { font-size: 12px; color: var(--text-secondary); margin-top: 1px; }

.card { background: var(--panel); border: 1px solid var(--border);
        border-radius: 12px; padding: 6px 6px 2px; margin-bottom: 6px; }
.chart { width: 100%; }

.scroll { overflow-x: auto; background: var(--panel);
          border: 1px solid var(--border); border-radius: 12px; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { padding: 9px 14px; text-align: left; white-space: nowrap;
         border-bottom: 1px solid var(--border); }
th { font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.06em;
     color: var(--text-muted); font-weight: 600; }
tbody tr:last-child td { border-bottom: none; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
td.muted { color: var(--text-muted); }

footer { margin-top: 46px; padding-top: 20px;
         border-top: 1px solid var(--border);
         font-size: 12.5px; color: var(--text-secondary); }
footer ul { margin: 10px 0 0; padding-left: 18px; }
footer li { margin-bottom: 5px; }
.muted { color: var(--text-muted); }

@media print {
  button.ghost { display: none; }
  .card, .scroll, .tile { break-inside: avoid; }
}

.controls { display: flex; gap: 18px; flex-wrap: wrap; align-items: flex-start;
            background: var(--panel); border: 1px solid var(--border);
            border-radius: 12px; padding: 14px 16px; margin-bottom: 18px; }
.controls .group { display: flex; flex-direction: column; gap: 7px; }
.controls .cap { font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.07em;
                 color: var(--text-muted); font-weight: 600; }
.presets { display: flex; gap: 6px; flex-wrap: wrap; }
.presets button { background: none; border: 1px solid var(--border);
                  color: var(--text-secondary); border-radius: 7px;
                  padding: 5px 11px; font-size: 12.5px; cursor: pointer;
                  font-family: inherit; }
.presets button:hover { color: var(--text-primary); border-color: var(--text-muted); }
.presets button[aria-pressed="true"] { background: var(--text-primary);
                                       border-color: var(--text-primary);
                                       color: var(--panel); font-weight: 600; }
.dates { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.dates input { background: var(--surface); border: 1px solid var(--border);
               color: var(--text-primary); border-radius: 7px; padding: 5px 8px;
               font-size: 12.5px; font-family: inherit; color-scheme: inherit; }
.dates span { font-size: 12.5px; color: var(--text-muted); }
.zonebox { display: flex; gap: 10px 16px; flex-wrap: wrap; max-width: 620px; }
.chk { display: inline-flex; align-items: center; gap: 6px; font-size: 13px;
       cursor: pointer; color: var(--text-secondary); }
.chk input { accent-color: var(--text-primary); cursor: pointer; }
.chk .dot { width: 9px; height: 9px; border-radius: 50%; display: inline-block; }
.linkish { background: none; border: none; color: var(--text-muted);
           font-size: 12px; cursor: pointer; padding: 0; font-family: inherit;
           text-decoration: underline; }
.linkish:hover { color: var(--text-primary); }
.empty { padding: 40px 0; text-align: center; color: var(--text-muted); font-size: 14px; }
/* A heatmap has no legend, so it needs its own zone control -- the other
   charts get in-chart filtering free from clicking legend entries. */
.chart-control { display: flex; align-items: center; gap: 9px; padding: 10px 12px 2px; }
.chart-control label { font-size: 10.5px; text-transform: uppercase;
                       letter-spacing: 0.07em; color: var(--text-muted); font-weight: 600; }
.chart-control select { background: var(--surface); border: 1px solid var(--border);
                        color: var(--text-primary); border-radius: 7px;
                        padding: 5px 9px; font-size: 12.5px; font-family: inherit;
                        min-width: 220px; color-scheme: inherit; }
"""

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>__CSS__</style>
<script>
  (function () {
    var stored = null;
    try { stored = localStorage.getItem('tado-report-theme'); } catch (e) {}
    if (stored) document.documentElement.setAttribute('data-theme', stored);
  })();
</script>
</head>
<body>
<div class="wrap">

  <header>
    <div>
      <h1>__HOME__</h1>
      <div class="sub">__SUBTITLE__</div>
      <div class="gen">Generated __GENERATED__ by tado-export</div>
    </div>
    <button class="ghost" id="theme-toggle" type="button">Switch theme</button>
  </header>

  <div class="controls">
    <div class="group">
      <span class="cap">Period</span>
      <div class="presets" id="presets">
        <button type="button" data-days="1">Day</button>
        <button type="button" data-days="7">Week</button>
        <button type="button" data-days="30">Month</button>
        <button type="button" data-days="365">Year</button>
        <button type="button" data-days="0">All</button>
      </div>
    </div>
    <div class="group">
      <span class="cap">Range</span>
      <div class="dates">
        <input type="date" id="from"><span>to</span><input type="date" id="to">
        <button type="button" class="linkish" id="shift-back">&larr; earlier</button>
        <button type="button" class="linkish" id="shift-fwd">later &rarr;</button>
      </div>
    </div>
    <div class="group">
      <span class="cap">Zones</span>
      <div class="zonebox" id="zones">__ZONE_CHECKBOXES__</div>
      <div><button type="button" class="linkish" id="zones-all">select all</button></div>
    </div>
  </div>

  <div id="report-body">
    <div class="tiles" id="tiles"></div>

    <h2>Temperature<span class="hint" id="temp-hint"></span></h2>
    <div class="card"><div class="chart" id="fig-temp"></div></div>

    <h2>Relative humidity<span class="hint" id="hum-hint"></span></h2>
    <div class="card"><div class="chart" id="fig-humidity"></div></div>

    <h2>When the heating actually ran<span class="hint" id="demand-hint"></span></h2>
    <div class="card">
      <div class="chart-control">
        <label for="demand-zone">Show</label>
        <select id="demand-zone"></select>
      </div>
      <div class="chart" id="fig-demand"></div>
    </div>

    <h2>Heating hours per day<span class="hint" id="hours-hint">stacked per zone</span></h2>
    <div class="card"><div class="chart" id="fig-hours"></div></div>

    <h2>Per zone</h2>
    <div id="zone-table"></div>

    <h2>Per month</h2>
    <div id="month-table"></div>
  </div>

  <div class="empty" id="empty" style="display:none">
    Nothing selected. Pick at least one zone and a date range that contains data.
  </div>

  <footer>
    <strong>About this report.</strong>
    Measurements come from the tado&deg; <code>dayReport</code> API, sampled every
    15&nbsp;minutes, and are grouped by calendar day in the home's own timezone.
    The controls above filter every chart and table on this page; all of the data
    is embedded, so it works with no network. A few things it is deliberately
    careful about:
    <ul>
      <li>Gaps in the data break the chart lines instead of being bridged with a
          straight segment, so a missing day never looks like a steady reading.</li>
      <li>Heating hours are counted from the samples actually present and are
          never scaled up to a notional 24&nbsp;hours; days covered by less than
          half their readings are left out of the daily chart.</li>
      <li>Hot-water zones measure water, not rooms, so they are excluded from
          room temperature statistics.</li>
      <li>Inside and outside temperature share a time axis but get separate
          scales &mdash; they are never plotted against two y-axes on one panel.</li>
    </ul>
  </footer>

</div>

__SCRIPT_TAG__
<script>
var R = __REPORT__;
</script>
<script>
__APP_JS_TOKEN__
</script>
</body>
</html>
"""
