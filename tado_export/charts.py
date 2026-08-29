"""Figure builders. Pure functions of a dataframe -> plotly Figure."""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from .theme import SEQUENTIAL_BLUE, SURFACE, template

EMPTY_NOTE = "No data for this selection.<br><span style='font-size:12px'>Widen the date range, or run a sync.</span>"


def empty_figure(mode: str, message: str = EMPTY_NOTE) -> go.Figure:
    colors = SURFACE[mode]
    fig = go.Figure()
    fig.update_layout(template=template(mode), height=320)
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    fig.add_annotation(
        text=message, showarrow=False, x=0.5, y=0.5, xref="paper", yref="paper",
        font=dict(size=14, color=colors["text_muted"]),
    )
    return fig


def _apply_labels(
    fig: go.Figure, entries: list[tuple], mode: str, axis_span: float | None = None
) -> None:
    """Label each series at its last point, unless the labels would collide.

    These labels are the relief for the light palette's low-contrast slots, but
    two labels stacked on top of each other are worse than none — the legend is
    always present, so in that case fall back to it.

    Collision is judged against the height of the y-axis, not the spread of the
    labels themselves: three series 0.1 apart on a 0-55 axis land in the same
    few pixels however far apart they are from each other.
    """
    points = [(x, float(y), text) for x, y, text in entries if not pd.isna(y)]
    if not points:
        return

    if len(points) > 1:
        values = sorted(p[1] for p in points)
        reference = axis_span if axis_span and axis_span > 0 else (values[-1] - values[0])
        if not reference or reference <= 0:
            return
        tightest = min(b - a for a, b in zip(values, values[1:]))
        if tightest < reference * 0.045:
            return

    for x, y, text in points:
        # Annotation coordinates must be JSON-native: a pandas Timestamp survives
        # the Dash encoder but breaks kaleido's static image export.
        if isinstance(x, pd.Timestamp):
            x = x.isoformat()
        fig.add_annotation(
            x=x, y=y, text=f" {text}", showarrow=False,
            xanchor="left", yanchor="middle",
            font=dict(size=11, color=SURFACE[mode]["text_secondary"]),
            bgcolor="rgba(0,0,0,0)",
        )



def _with_gaps(group: pd.DataFrame, columns: list[str], factor: float = 2.5) -> pd.DataFrame:
    """Insert a NaN row wherever the series jumps a sampling interval.

    Without this, a missing day joins its neighbours with a straight line — the
    chart would invent a constant temperature across a hole in the data.
    """
    if len(group) < 3:
        return group
    deltas = group["ts_local"].diff()
    step = deltas.median()
    if pd.isna(step) or step.total_seconds() <= 0:
        return group
    breaks = group.index[deltas > step * factor]
    if len(breaks) == 0:
        return group

    filler = group.loc[breaks].copy()
    filler["ts_local"] = group["ts_local"].shift(1).loc[breaks] + step
    for column in columns:
        if column in filler.columns:
            filler[column] = float("nan")
    return pd.concat([group, filler]).sort_values("ts_local")


def temperature(
    frame: pd.DataFrame,
    colors: dict[int, str],
    mode: str = "light",
    *,
    show_setpoint: bool = True,
    show_outside: bool = True,
    show_labels: bool = True,
) -> go.Figure:
    """Room temperature per zone, with the outside reading beneath it.

    Inside and outside are both degrees Celsius but sit ~15 °C apart, so they get
    stacked panels on a shared time axis rather than one axis (which squashes the
    room lines) or two y-scales on one plot (which is never right).
    """
    if frame.empty:
        return empty_figure(mode)

    surface = SURFACE[mode]
    outside = pd.DataFrame()
    if show_outside and "outside_temp_c" in frame.columns:
        outside = (
            frame[["ts_local", "outside_temp_c"]]
            .dropna()
            .drop_duplicates("ts_local")
            .sort_values("ts_local")
        )

    two_panel = not outside.empty
    if two_panel:
        fig = make_subplots(
            rows=2, cols=1, shared_xaxes=True,
            row_heights=[0.68, 0.32], vertical_spacing=0.07,
        )
    else:
        fig = go.Figure()

    def add(trace, row):
        if two_panel:
            fig.add_trace(trace, row=row, col=1)
        else:
            fig.add_trace(trace)

    zones = list(frame.groupby("zone_id", sort=True, observed=True))
    label_series = show_labels and len(zones) <= 4
    labels: list[tuple] = []

    for zone_id, group in zones:
        color = colors.get(zone_id, "#2a78d6")
        name = str(group["zone_name"].iloc[0] or f"Zone {zone_id}")
        group = _with_gaps(group.sort_values("ts_local"), ["inside_temp_c", "setpoint_c"])

        if show_setpoint and group["setpoint_c"].notna().any():
            add(
                go.Scatter(
                    x=group["ts_local"], y=group["setpoint_c"],
                    name=f"{name} · target", mode="lines",
                    line=dict(color=color, width=1.2, dash="dash", shape="hv"),
                    opacity=0.45, legendgroup=name, connectgaps=False,
                    hovertemplate=f"{name} target: %{{y:.1f}} °C<extra></extra>",
                ),
                1,
            )

        add(
            go.Scatter(
                x=group["ts_local"], y=group["inside_temp_c"],
                name=name, mode="lines",
                line=dict(color=color, width=2),
                legendgroup=name, connectgaps=False,
                hovertemplate=f"{name}: %{{y:.1f}} °C<extra></extra>",
            ),
            1,
        )
        if label_series:
            last = group.dropna(subset=["inside_temp_c"]).tail(1)
            if not last.empty:
                labels.append((last["ts_local"].iloc[0], last["inside_temp_c"].iloc[0], name))

    inside_values = frame["inside_temp_c"].dropna()
    _apply_labels(fig, labels, mode,
                  float(inside_values.max() - inside_values.min()) if len(inside_values) else None)

    if two_panel:
        outside = _with_gaps(outside, ["outside_temp_c"])
        add(
            go.Scatter(
                x=outside["ts_local"], y=outside["outside_temp_c"],
                name="Outside", mode="lines",
                line=dict(color=surface["reference"], width=1.5),
                connectgaps=False,
                hovertemplate="Outside: %{y:.1f} °C<extra></extra>",
            ),
            2,
        )

    fig.update_layout(template=template(mode), height=470, title="Temperature")
    if two_panel:
        fig.update_yaxes(title_text="Inside °C", row=1, col=1)
        fig.update_yaxes(title_text="Outside °C", row=2, col=1)
        fig.update_xaxes(title_text=None, row=2, col=1)
    else:
        fig.update_layout(yaxis_title="°C", xaxis_title=None, height=420)
    return fig


def humidity(
    frame: pd.DataFrame,
    colors: dict[int, str],
    mode: str = "light",
    *,
    show_labels: bool = True,
) -> go.Figure:
    if frame.empty or frame["humidity_pct"].isna().all():
        return empty_figure(mode, "No humidity readings for this selection.")

    fig = go.Figure()
    # A hot-water zone reports no humidity; do not give it an empty legend entry.
    present = frame.dropna(subset=["humidity_pct"])
    groups = list(present.groupby("zone_id", sort=True))
    labels: list[tuple] = []
    for zone_id, group in groups:
        color = colors.get(zone_id, "#2a78d6")
        name = str(group["zone_name"].iloc[0] or f"Zone {zone_id}")
        group = _with_gaps(group.sort_values("ts_local"), ["humidity_pct"])
        fig.add_trace(
            go.Scatter(
                x=group["ts_local"], y=group["humidity_pct"],
                name=name, mode="lines", connectgaps=False,
                line=dict(color=color, width=2),
                hovertemplate=f"{name}: %{{y:.0f}} %<extra></extra>",
            )
        )
        if show_labels and len(groups) <= 4:
            last = group.dropna(subset=["humidity_pct"]).tail(1)
            if not last.empty:
                labels.append((last["ts_local"].iloc[0], last["humidity_pct"].iloc[0], name))

    humid_values = present["humidity_pct"].dropna()
    _apply_labels(fig, labels, mode,
                  float(humid_values.max()) if len(humid_values) else None)

    fig.update_layout(
        template=template(mode), height=300,
        title="Relative humidity", yaxis_title="%", xaxis_title=None,
    )
    fig.update_yaxes(rangemode="tozero")
    return fig


def demand_heatmap(
    hourly: pd.DataFrame, mode: str = "light", *, subtitle: str | None = None
) -> go.Figure:
    """Mean heating demand by day x hour — where the boiler actually worked.

    Takes the output of ``frames.hourly_demand``. ``subtitle`` names what the
    cells average over: across several zones one hot zone is diluted by the quiet
    ones, so the reader has to be told which zones are in the mean.
    """
    if hourly.empty:
        return empty_figure(mode, "No call-for-heat data for this selection.")

    pivot = (
        hourly.pivot_table(index="hour", columns="local_date", values="demand", observed=True)
        .reindex(range(24))
        .sort_index()
    )
    if pivot.empty or pivot.isna().all().all():
        return empty_figure(mode, "No call-for-heat data for this selection.")

    surface = SURFACE[mode]
    fig = go.Figure(
        go.Heatmap(
            z=pivot.astype("float64").to_numpy(),
            x=[str(c) for c in pivot.columns],
            y=pivot.index,
            colorscale=[[i / (len(SEQUENTIAL_BLUE) - 1), c] for i, c in enumerate(SEQUENTIAL_BLUE)],
            zmin=0, zmax=3,
            xgap=1 if pivot.shape[1] <= 120 else 0,
            ygap=1,
            hovertemplate="%{x} at %{y}:00<br>demand %{z:.2f} / 3<extra></extra>",
            colorbar=dict(
                title=dict(text="Demand", font=dict(size=11, color=surface["text_secondary"])),
                tickvals=[0, 1, 2, 3], ticktext=["none", "low", "med", "high"],
                tickfont=dict(size=10, color=surface["text_secondary"]),
                outlinewidth=0, thickness=12, len=0.8,
            ),
        )
    )
    title = "Heating demand by hour"
    if subtitle:
        title += (f"<span style='font-size:12px;color:{surface['text_muted']}'>"
                  f"   {subtitle}</span>")
    fig.update_layout(
        template=template(mode), height=380,
        title=title, yaxis_title="Hour of day", xaxis_title=None,
    )
    fig.update_yaxes(showgrid=False, dtick=3, autorange="reversed")
    fig.update_xaxes(showgrid=False)
    return fig


def heating_hours(
    daily: pd.DataFrame,
    colors: dict[int, str],
    mode: str = "light",
    *,
    omitted_days: int = 0,
) -> go.Figure:
    """Hours per day each zone called for heat, stacked into a daily total.

    Takes the output of ``frames.daily_heating``. ``omitted_days`` is stated on
    the chart rather than dropped silently.
    """
    if daily.empty:
        return empty_figure(mode, "No call-for-heat data for this selection.")

    surface = SURFACE[mode]
    # The 2px separator only helps while bars are wider than it is; across a
    # year of daily bars it would paint over them completely.
    n_days = daily["local_date"].nunique()
    edge = 2 if n_days <= 60 else 0

    fig = go.Figure()
    for zone_id, group in daily.groupby("zone_id", sort=True, observed=True):
        name = str(group["zone_name"].iloc[0] or f"Zone {zone_id}")
        fig.add_trace(
            go.Bar(
                x=group["local_date"].astype(str), y=group["hours"],
                name=name,
                marker=dict(
                    color=colors.get(zone_id, "#2a78d6"),
                    line=dict(color=surface["panel"], width=edge),
                ),
                hovertemplate=f"{name}: %{{y:.1f}} h<extra></extra>",
            )
        )

    fig.update_layout(
        template=template(mode), height=340, barmode="stack",
        bargap=0.25 if n_days <= 120 else 0.0,
        title="Heating hours per day", yaxis_title="Hours", xaxis_title=None,
    )
    fig.update_xaxes(showgrid=False)
    if omitted_days:
        fig.add_annotation(
            text=f"{omitted_days} partly covered day{'' if omitted_days == 1 else 's'} hidden",
            showarrow=False, xref="paper", yref="paper", x=1, y=1.06,
            xanchor="right", yanchor="bottom",
            font=dict(size=11, color=SURFACE[mode]["text_muted"]),
        )
    return fig
