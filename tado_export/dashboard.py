"""Local Dash dashboard over the exported SQLite database.

Runs entirely offline: no CDN assets, no API calls. Everything it draws comes
from the tables ``tado-export sync`` filled in.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from dash import Dash, Input, Output, State, dash_table, dcc, html

from . import charts, frames
from .theme import FONT_STACK, SURFACE, zone_colors

GRANULARITY = [
    {"label": "Auto", "value": "auto"},
    {"label": "Raw", "value": "raw"},
    {"label": "Hourly", "value": "1h"},
    {"label": "6-hourly", "value": "6h"},
    {"label": "Daily", "value": "1D"},
]

_CSS = """
:root {
  --surface: %(l_surface)s; --panel: %(l_panel)s; --border: %(l_border)s;
  --text-primary: %(l_text_primary)s; --text-secondary: %(l_text_secondary)s;
  --text-muted: %(l_text_muted)s;
  color-scheme: light;
}
[data-theme="dark"] {
  --surface: %(d_surface)s; --panel: %(d_panel)s; --border: %(d_border)s;
  --text-primary: %(d_text_primary)s; --text-secondary: %(d_text_secondary)s;
  --text-muted: %(d_text_muted)s;
  color-scheme: dark;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--surface); color: var(--text-primary);
       font-family: %(font)s; }
.wrap { max-width: 1240px; margin: 0 auto; padding: 24px 20px 64px; }
.head { display: flex; align-items: baseline; justify-content: space-between;
        gap: 16px; flex-wrap: wrap; margin-bottom: 20px; }
.head h1 { font-size: 20px; font-weight: 600; margin: 0; letter-spacing: -0.01em; }
.head .sub { font-size: 13px; color: var(--text-secondary); margin-top: 4px; }
.ghost { background: none; border: 1px solid var(--border); color: var(--text-secondary);
         border-radius: 8px; padding: 7px 12px; font-size: 12px; cursor: pointer;
         font-family: inherit; }
.ghost:hover { color: var(--text-primary); }
.filters { display: flex; gap: 12px; flex-wrap: wrap; align-items: flex-end;
           background: var(--panel); border: 1px solid var(--border);
           border-radius: 12px; padding: 14px 16px; margin-bottom: 20px; }
.field { display: flex; flex-direction: column; gap: 5px; }
.field > label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em;
                 color: var(--text-muted); font-weight: 600; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
         gap: 12px; margin-bottom: 20px; }
.tile { background: var(--panel); border: 1px solid var(--border);
        border-radius: 12px; padding: 14px 16px; }
.tile .k { font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em;
           color: var(--text-muted); font-weight: 600; }
.tile .v { font-size: 26px; font-weight: 600; margin-top: 6px; letter-spacing: -0.02em;
           font-variant-numeric: tabular-nums; }
.tile .n { font-size: 12px; color: var(--text-secondary); margin-top: 2px; }
.card { background: var(--panel); border: 1px solid var(--border);
        border-radius: 12px; padding: 8px 8px 4px; margin-bottom: 18px; }
.note { font-size: 12px; color: var(--text-muted); margin: 0 0 18px; }
.chart-control { display: flex; align-items: center; gap: 9px; padding: 8px 10px 2px; }
.chart-control > label { font-size: 11px; text-transform: uppercase;
                         letter-spacing: 0.06em; color: var(--text-muted); font-weight: 600; }
details.card { padding: 14px 16px; }
details.card > summary { cursor: pointer; font-size: 13px; font-weight: 600;
                         color: var(--text-secondary); }
details.card > summary:hover { color: var(--text-primary); }
"""


def _stylesheet() -> str:
    light, dark = SURFACE["light"], SURFACE["dark"]
    return _CSS % {
        **{f"l_{k}": v for k, v in light.items()},
        **{f"d_{k}": v for k, v in dark.items()},
        "font": FONT_STACK,
    }


_INDEX = """<!DOCTYPE html>
<html>
<head>
  {%metas%}<title>{%title%}</title>{%favicon%}{%css%}
  <style>__CSS__</style>
  <script>
    (function () {
      var stored = null;
      try { stored = localStorage.getItem('tado-theme'); } catch (e) {}
      var dark = stored ? stored === 'dark'
        : window.matchMedia('(prefers-color-scheme: dark)').matches;
      document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
    })();
  </script>
</head>
<body>
  {%app_entry%}
  <footer>{%config%}{%scripts%}{%renderer%}</footer>
</body>
</html>"""


def _tile(key: str, value: str, note: str = "") -> html.Div:
    return html.Div(
        [html.Div(key, className="k"), html.Div(value, className="v"),
         html.Div(note, className="n")],
        className="tile",
    )


def _fmt(value, unit: str = "", digits: int = 1) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    return f"{value:.{digits}f}{unit}"


class _Pool:
    """One SQLite connection per Flask worker thread.

    Opened read-only so the dashboard can never modify the database, and safe to
    run while a ``sync`` is writing: WAL mode allows concurrent readers. A
    read-only connection cannot create the ``-shm`` file a WAL database needs,
    so if that is missing we fall back to a normal connection (still read-only in
    practice — no callback issues a write).
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self.uri = f"file:{self.path.as_posix()}?mode=ro"
        self._local = threading.local()

    def _open(self) -> sqlite3.Connection:
        try:
            return sqlite3.connect(self.uri, uri=True, timeout=30)
        except sqlite3.OperationalError:
            return sqlite3.connect(str(self.path), timeout=30)

    def __call__(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._open()
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn



def build_app(db_path: str | Path) -> Dash:
    path = Path(db_path)
    if not path.exists():
        raise SystemExit(
            f"No database at {path}. Run `tado-export sync` first, "
            "or point at another file with --db."
        )
    pool = _Pool(path)

    home_options, default_home = _home_options(pool())

    app = Dash(__name__, title="tado° data")
    app.index_string = _INDEX.replace("__CSS__", _stylesheet())

    app.layout = html.Div(
        className="wrap",
        children=[
            dcc.Store(id="theme", data="light", storage_type="local"),
            html.Div(
                className="head",
                children=[
                    html.Div([
                        html.H1("tado° measurements"),
                        html.Div(id="subtitle", className="sub"),
                    ]),
                    html.Button("Toggle dark mode", id="theme-toggle", className="ghost", n_clicks=0),
                ],
            ),
            html.Div(
                className="filters",
                children=[
                    html.Div(className="field", children=[
                        html.Label("Home", htmlFor="home"),
                        dcc.Dropdown(id="home", options=home_options, value=default_home,
                                     clearable=False, style={"width": "220px"}),
                    ]),
                    html.Div(className="field", children=[
                        html.Label("Zones", htmlFor="zones"),
                        dcc.Dropdown(id="zones", multi=True, style={"minWidth": "300px"}),
                    ]),
                    html.Div(className="field", children=[
                        html.Label("Date range", htmlFor="dates"),
                        dcc.DatePickerRange(id="dates", display_format="YYYY-MM-DD",
                                            first_day_of_week=1),
                    ]),
                    html.Div(className="field", children=[
                        html.Label("Resolution", htmlFor="grain"),
                        dcc.RadioItems(id="grain", options=GRANULARITY, value="auto",
                                       inline=True,
                                       labelStyle={"marginRight": "12px", "fontSize": "13px"}),
                    ]),
                ],
            ),
            dcc.Loading(
                type="default",
                children=[
                    html.Div(id="tiles", className="tiles"),
                    html.Div(dcc.Graph(id="fig-temp", config={"displaylogo": False}), className="card"),
                    html.Div(dcc.Graph(id="fig-humidity", config={"displaylogo": False}), className="card"),
                    html.Div(
                        className="card",
                        children=[
                            # Its own zone picker: averaging eight zones hides a
                            # single hot one, and isolating a zone here should not
                            # strip it from every other chart.
                            html.Div(
                                className="chart-control",
                                children=[
                                    html.Label("Show", htmlFor="demand-zone"),
                                    dcc.Dropdown(id="demand-zone", clearable=False,
                                                 style={"minWidth": "260px"}),
                                ],
                            ),
                            dcc.Graph(id="fig-demand", config={"displaylogo": False}),
                        ],
                    ),
                    html.Div(dcc.Graph(id="fig-hours", config={"displaylogo": False}), className="card"),
                    html.Details(
                        className="card",
                        children=[
                            html.Summary("Table view — the same numbers, readable without colour"),
                            html.Div(id="table-wrap", style={"marginTop": "12px"}),
                        ],
                    ),
                ],
            ),
            html.P(id="footnote", className="note"),
        ],
    )

    _register_callbacks(app, pool)
    return app


def _home_options(conn: sqlite3.Connection):
    rows = frames.homes(conn)
    if rows.empty:
        return [], None
    options = [{"label": r["name"] or f"Home {r['id']}", "value": int(r["id"])} for _, r in rows.iterrows()]
    return options, options[0]["value"]


def _register_callbacks(app: Dash, pool: _Pool) -> None:

    @app.callback(
        Output("theme", "data"),
        Input("theme-toggle", "n_clicks"),
        State("theme", "data"),
        prevent_initial_call=True,
    )
    def _toggle(_clicks, current):
        return "light" if current == "dark" else "dark"

    app.clientside_callback(
        """
        function (mode) {
            document.documentElement.setAttribute('data-theme', mode);
            try { localStorage.setItem('tado-theme', mode); } catch (e) {}
            return '';
        }
        """,
        Output("footnote", "title"),
        Input("theme", "data"),
    )

    @app.callback(
        Output("zones", "options"), Output("zones", "value"),
        Output("dates", "min_date_allowed"), Output("dates", "max_date_allowed"),
        Output("dates", "start_date"), Output("dates", "end_date"),
        Output("subtitle", "children"),
        Input("home", "value"),
    )
    def _home_changed(home_id):
        conn = pool()
        if home_id is None:
            return [], [], None, None, None, None, "No homes in the database yet — run `tado-export sync`."

        zone_rows = frames.zones(conn, home_id)
        options = [
            {
                "label": (r["name"] or f"Zone {r['id']}")
                         + (" (hot water)" if r["type"] == "HOT_WATER" else ""),
                "value": int(r["id"]),
            }
            for _, r in zone_rows.iterrows()
        ]
        # Hot water sits ~25 °C above room temperature and would flatten the
        # temperature chart, so it is available but off by default.
        rooms = [int(r["id"]) for _, r in zone_rows.iterrows() if r["type"] != "HOT_WATER"]
        selected = rooms or [o["value"] for o in options]

        lo, hi = frames.data_extent(conn, home_id)
        if lo is None:
            return options, selected, None, None, None, None, "No synced days for this home yet."

        start = max(lo, hi - timedelta(days=29))
        subtitle = f"{len(options)} zones · data from {lo.isoformat()} to {hi.isoformat()}"
        return options, selected, lo, hi, start, hi, subtitle

    @app.callback(
        Output("demand-zone", "options"), Output("demand-zone", "value"),
        Input("zones", "value"), State("demand-zone", "value"),
    )
    def _demand_zone_choices(zone_ids, current):
        conn = pool()
        if not zone_ids:
            return [{"label": "Average of selected zones", "value": "all"}], "all"
        rows = conn.execute(
            "SELECT id, name FROM zone WHERE id IN (%s)" % ",".join("?" * len(zone_ids)),
            tuple(zone_ids),
        ).fetchall()
        names = {int(r["id"]): (r["name"] or f"Zone {r['id']}") for r in rows}
        options = [{"label": f"Average of {len(zone_ids)} selected zones", "value": "all"}] + [
            {"label": names.get(int(z), f"Zone {z}"), "value": str(z)} for z in zone_ids
        ]
        valid = {o["value"] for o in options}
        return options, (current if current in valid else "all")

    @app.callback(
        Output("tiles", "children"),
        Output("fig-temp", "figure"), Output("fig-humidity", "figure"),
        Output("fig-demand", "figure"), Output("fig-hours", "figure"),
        Output("table-wrap", "children"), Output("footnote", "children"),
        Input("home", "value"), Input("zones", "value"),
        Input("dates", "start_date"), Input("dates", "end_date"),
        Input("grain", "value"), Input("theme", "data"),
        Input("demand-zone", "value"),
    )
    def _render(home_id, zone_ids, start, end, grain, mode, demand_zone):
        conn = pool()
        mode = mode if mode in ("light", "dark") else "light"
        empty = charts.empty_figure(mode)

        if home_id is None or not zone_ids or not start or not end:
            return ([], empty, empty, empty, empty, None,
                    "Pick a home, at least one zone and a date range.")

        start_d, end_d = date.fromisoformat(start[:10]), date.fromisoformat(end[:10])
        tz_row = conn.execute("SELECT timezone FROM home WHERE id=?", (home_id,)).fetchone()
        tz = tz_row["timezone"] if tz_row and tz_row["timezone"] else "UTC"

        raw = frames.enriched(conn, home_id, list(zone_ids), start_d, end_d, tz=tz)
        if raw.empty:
            return ([], empty, empty, empty, empty, None,
                    "No measurements stored for that range yet.")

        rule = frames.auto_rule(raw) if grain == "auto" else (None if grain == "raw" else grain)
        plot = frames.resample(raw, rule, min_coverage=0.5 if rule else 0.0)
        raw_with_frac = frames.resample(raw, None)

        # Colours key off every zone in the home, so filtering never repaints.
        all_zones = [int(z) for z in frames.zones(conn, home_id)["id"]]
        colors = zone_colors(all_zones, mode)

        # The heatmap may be narrowed to one zone without touching the rest.
        demand_frame = raw_with_frac
        demand_label = f"averaged across {len(zone_ids)} zones"
        if demand_zone and demand_zone != "all":
            try:
                only = int(demand_zone)
            except (TypeError, ValueError):
                only = None
            if only is not None and only in set(zone_ids):
                demand_frame = raw_with_frac[raw_with_frac["zone_id"] == only]
                name = demand_frame["zone_name"].iloc[0] if not demand_frame.empty else f"Zone {only}"
                demand_label = str(name)

        daily = frames.daily_heating(raw_with_frac)
        all_days = frames.daily_heating(raw_with_frac, min_coverage=0)
        # Count days, not zone-days: one gap hides one bar, not one per zone.
        omitted = all_days["local_date"].nunique() - daily["local_date"].nunique()

        figures = (
            charts.temperature(plot, colors, mode),
            charts.humidity(plot, colors, mode),
            charts.demand_heatmap(frames.hourly_demand(demand_frame), mode,
                                  subtitle=demand_label),
            charts.heating_hours(daily, colors, mode, omitted_days=omitted),
        )

        tiles = _build_tiles(raw_with_frac, start_d, end_d)
        table = _build_table(plot, mode)

        shown = {"1h": "hourly", "6h": "6-hourly", "1D": "daily"}.get(rule, "raw 15-minute")
        note = (
            f"{len(raw):,} readings from {len(zone_ids)} zone(s), "
            f"{start_d} to {end_d}, shown at {shown} resolution. "
            f"Times are local to the home ({tz})."
        )
        return (tiles, *figures, table, note)


def _build_tiles(frame: pd.DataFrame, start: date, end: date) -> list[html.Div]:
    # A HOT_WATER zone measures water, not the room — averaging it into "inside
    # temperature" would put a 45 °C cylinder next to a 21 °C living room.
    rooms = frame[frame.get("zone_type", "HEATING") != "HOT_WATER"] if "zone_type" in frame else frame
    inside = rooms["inside_temp_c"].dropna()
    outside = frame[["ts", "outside_temp_c"]].dropna().drop_duplicates("ts")["outside_temp_c"]

    daily = frames.daily_heating(frame)
    zone_hours = daily["hours"].sum()
    n_zones = max(frame["zone_id"].nunique(), 1)
    days_covered = frame["local_date"].nunique()
    span_days = (end - start).days + 1

    return [
        _tile("Avg inside", _fmt(inside.mean() if len(inside) else None, " °C"),
              f"range {_fmt(inside.min())}–{_fmt(inside.max())} °C" if len(inside)
              else "hot-water zones only — no room readings"),
        _tile("Avg outside", _fmt(outside.mean() if len(outside) else None, " °C"),
              f"range {_fmt(outside.min() if len(outside) else None)}–{_fmt(outside.max() if len(outside) else None)} °C"),
        _tile("Heating demand", _fmt(zone_hours, " zone-h"),
              f"{_fmt(zone_hours / max(days_covered, 1) / n_zones)} h/day per zone"),
        _tile("Coverage", f"{days_covered}/{span_days}",
              f"days with data · {n_zones} zone(s)"),
    ]


def _build_table(frame: pd.DataFrame, mode: str):
    colors = SURFACE[mode]
    columns = [c for c in ["ts_local", "zone_name", "inside_temp_c", "humidity_pct",
                           "setpoint_c", "call_for_heat", "outside_temp_c"]
               if c in frame.columns]
    table = frame[columns].copy()
    if "ts_local" in table:
        table["ts_local"] = table["ts_local"].dt.strftime("%Y-%m-%d %H:%M")
    for column in table.select_dtypes("number").columns:
        table[column] = table[column].round(2)

    labels = {
        "ts_local": "Time", "zone_name": "Zone", "inside_temp_c": "Inside °C",
        "humidity_pct": "Humidity %", "setpoint_c": "Target °C",
        "call_for_heat": "Demand", "outside_temp_c": "Outside °C",
    }
    return dash_table.DataTable(
        data=table.to_dict("records"),
        columns=[{"name": labels.get(c, c), "id": c} for c in table.columns],
        page_size=25,
        sort_action="native",
        filter_action="native",
        style_table={"overflowX": "auto"},
        style_header={
            "backgroundColor": colors["surface"], "color": colors["text_secondary"],
            "border": f"1px solid {colors['border']}", "fontWeight": "600",
            "fontSize": "12px", "textTransform": "uppercase", "letterSpacing": "0.05em",
        },
        style_cell={
            "backgroundColor": colors["panel"], "color": colors["text_primary"],
            "border": f"1px solid {colors['border']}", "fontFamily": FONT_STACK,
            "fontSize": "13px", "padding": "8px 10px", "textAlign": "left",
        },
    )


def serve(db_path: str | Path, host: str = "127.0.0.1", port: int = 8050, debug: bool = False) -> None:
    app = build_app(db_path)
    print(f"\n  tado° dashboard → http://{host}:{port}\n")
    app.run(host=host, port=port, debug=debug)
