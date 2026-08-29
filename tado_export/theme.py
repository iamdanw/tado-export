"""Chart colours and Plotly templates.

Palette slots are the validated categorical order (adjacent-pair CVD ΔE 9.1
light / 8.4 dark). Three light-mode slots sit below 3:1 against the surface, so
the light theme carries the required relief: a legend is always present, series
are direct-labelled at their last point, and a table view of the same numbers is
one click away.
"""

from __future__ import annotations

import plotly.graph_objects as go

CATEGORICAL = {
    "light": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100",
              "#e87ba4", "#008300", "#4a3aa7", "#e34948"],
    "dark": ["#3987e5", "#d95926", "#199e70", "#c98500",
             "#d55181", "#008300", "#9085e9", "#e66767"],
}

# Single-hue blue ramp, light -> dark, for magnitude (the demand heatmap).
SEQUENTIAL_BLUE = [
    "#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef",
    "#6da7ec", "#5598e7", "#3987e5", "#2a78d6",
    "#256abf", "#1c5cab", "#184f95", "#104281", "#0d366b",
]

SURFACE = {
    "light": {
        "surface": "#fcfcfb",
        "panel": "#ffffff",
        "border": "#e4e3de",
        "grid": "#eceae4",
        "text_primary": "#0b0b0b",
        "text_secondary": "#52514e",
        "text_muted": "#7c7b76",
        "reference": "#8b8a84",
    },
    "dark": {
        "surface": "#1a1a19",
        "panel": "#222220",
        "border": "#3a3a37",
        "grid": "#2f2f2c",
        "text_primary": "#ffffff",
        "text_secondary": "#c3c2b7",
        "text_muted": "#93928a",
        "reference": "#8b8a84",
    },
}

FONT_STACK = (
    'ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, '
    '"Helvetica Neue", Arial, sans-serif'
)


def zone_colors(zone_ids: list[int], mode: str = "light") -> dict[int, str]:
    """Stable colour per zone.

    Keyed off the home's full zone list so filtering the chart never repaints
    the zones that remain.
    """
    palette = CATEGORICAL[mode]
    return {zid: palette[i % len(palette)] for i, zid in enumerate(sorted(zone_ids))}


def template(mode: str = "light") -> go.layout.Template:
    colors = SURFACE[mode]
    axis = dict(
        showgrid=True,
        gridcolor=colors["grid"],
        gridwidth=1,
        zeroline=False,
        linecolor=colors["border"],
        linewidth=1,
        ticks="outside",
        ticklen=4,
        tickcolor=colors["border"],
        tickfont=dict(color=colors["text_secondary"], size=11),
        title=dict(font=dict(color=colors["text_secondary"], size=12)),
        automargin=True,
    )
    return go.layout.Template(
        layout=go.Layout(
            colorway=CATEGORICAL[mode],
            paper_bgcolor=colors["panel"],
            plot_bgcolor=colors["panel"],
            font=dict(family=FONT_STACK, size=12, color=colors["text_primary"]),
            # Title sits in the container band so the legend never collides with it.
            title=dict(font=dict(size=15, color=colors["text_primary"]),
                       x=0, xanchor="left", y=0.98, yanchor="top", yref="container"),
            xaxis=axis,
            yaxis=axis,
            margin=dict(l=56, r=96, t=84, b=44),
            hovermode="x unified",
            hoverlabel=dict(
                bgcolor=colors["panel"],
                bordercolor=colors["border"],
                font=dict(family=FONT_STACK, size=12, color=colors["text_primary"]),
            ),
            legend=dict(
                orientation="h",
                yanchor="bottom",
                y=1.02,
                xanchor="left",
                x=0,
                font=dict(color=colors["text_secondary"], size=11),
                bgcolor="rgba(0,0,0,0)",
            ),
        )
    )
