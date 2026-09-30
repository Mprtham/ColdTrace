"""Temperature-history chart for the evidence panel.

One series over time, so a line; no legend (the title names it). The safe range is a
neutral band, flagged readings are warning-status triangles (always paired with the
"⚠ flag" label and the table view, never color alone), and a nearest-point crosshair
carries the tooltip. Colors are the reference palette, validated against Streamlit's
light (#ffffff) and dark (#0e1117) surfaces.
"""

from __future__ import annotations

from typing import Any

import altair as alt
import pandas as pd

LIGHT = {
    "series": "#2a78d6",
    "surface": "#ffffff",
    "band": "#898781",
    "grid": "#e1e0d9",
    "axis": "#c3c2b7",
    "muted": "#898781",
}
DARK = {
    "series": "#3987e5",
    "surface": "#0e1117",
    "band": "#898781",
    "grid": "#2c2c2a",
    "axis": "#383835",
    "muted": "#898781",
}
WARNING = "#fab219"  # status: warning — only ever for flagged readings


def temperature_frame(readings: list[dict[str, Any]]) -> pd.DataFrame:
    df = pd.DataFrame(
        [
            {
                "recorded_at": pd.Timestamp(r["recorded_at"]),
                "temperature_c": r["temperature_c"],
                "min_temp_c": r["min_temp_c"],
                "max_temp_c": r["max_temp_c"],
                "flag": " + ".join(r["data_quality_flags"]) or "CLEAN",
                "flagged": bool(r["data_quality_flags"]),
            }
            for r in readings
        ]
    )
    return df.sort_values("recorded_at") if not df.empty else df


def temperature_chart(readings: list[dict[str, Any]], dark: bool = False) -> alt.LayerChart:
    c = DARK if dark else LIGHT
    df = temperature_frame(readings)
    x = alt.X("recorded_at:T", title=None, axis=alt.Axis(format="%H:%M", labelColor=c["muted"]))
    y_scale = alt.Scale(domain=y_domain(df), nice=False)
    base = alt.Chart(df)

    band = base.mark_area(opacity=0.16, color=c["band"]).encode(
        x=x, y=alt.Y("min_temp_c:Q", scale=y_scale), y2="max_temp_c:Q"
    )
    line = base.mark_line(strokeWidth=2, color=c["series"], interpolate="monotone").encode(
        x=x,
        y=alt.Y(
            "temperature_c:Q",
            title="°C",
            scale=y_scale,
            axis=alt.Axis(labelColor=c["muted"], titleColor=c["muted"]),
        ),
    )
    flagged = (
        base.transform_filter(alt.datum.flagged)
        .mark_point(
            shape="triangle-up",
            filled=True,
            size=110,
            color=WARNING,
            stroke=c["surface"],
            strokeWidth=2,
            opacity=1,
        )
        .encode(x=x, y="temperature_c:Q")
    )

    # Direct labels on the band edges, at the right end of the plot.
    edges = alt.Chart(
        pd.DataFrame(
            [
                {"x": df["recorded_at"].max(), "y": df["max_temp_c"].max(), "dy": -7,
                 "label": f"safe max {df['max_temp_c'].max():g} °C"},
                {"x": df["recorded_at"].max(), "y": df["min_temp_c"].min(), "dy": 13,
                 "label": f"safe min {df['min_temp_c'].min():g} °C"},
            ]
        )
        if not df.empty
        else pd.DataFrame(columns=["x", "y", "dy", "label"])
    )  # fmt: skip
    edge_labels = alt.layer(
        edges.transform_filter("datum.dy < 0")
        .mark_text(align="right", dy=-7, fontSize=11, color=c["muted"])
        .encode(x="x:T", y="y:Q", text="label:N"),
        edges.transform_filter("datum.dy > 0")
        .mark_text(align="right", dy=13, fontSize=11, color=c["muted"])
        .encode(x="x:T", y="y:Q", text="label:N"),
    )
    edge_rules = alt.layer(
        base.mark_rule(color=c["axis"], strokeWidth=1).encode(y="max(max_temp_c):Q"),
        base.mark_rule(color=c["axis"], strokeWidth=1).encode(y="min(min_temp_c):Q"),
    )

    nearest = alt.selection_point(
        nearest=True, on="pointerover", fields=["recorded_at"], empty=False, clear="pointerout"
    )
    tooltip = [
        alt.Tooltip("recorded_at:T", title="Time (UTC)", format="%Y-%m-%d %H:%M"),
        alt.Tooltip("temperature_c:Q", title="Temp °C", format=".1f"),
        alt.Tooltip("min_temp_c:Q", title="Safe min °C"),
        alt.Tooltip("max_temp_c:Q", title="Safe max °C"),
        alt.Tooltip("flag:N", title="Data quality"),
    ]
    # Invisible, oversized hit targets so hovering anywhere near a reading selects it.
    hit = base.mark_point(size=500, opacity=0).encode(x=x, tooltip=tooltip).add_params(nearest)
    rule = base.mark_rule(color=c["muted"], strokeWidth=1).encode(x=x).transform_filter(nearest)
    focus = (
        base.mark_point(filled=True, size=70, color=c["series"], stroke=c["surface"], strokeWidth=2)
        .encode(x=x, y="temperature_c:Q")
        .transform_filter(nearest)
    )

    chart: alt.LayerChart = (
        alt.layer(band, edge_rules, edge_labels, line, flagged, rule, focus, hit)
        .properties(height=220, title=alt.TitleParams("Temperature vs safe range", anchor="start"))
        .configure_axis(gridColor=c["grid"], domainColor=c["axis"], tickColor=c["axis"])
        .configure_view(strokeWidth=0)
        .configure_title(color=c["muted"], fontSize=12, fontWeight="normal")
    )
    return chart


def y_domain(df: pd.DataFrame) -> list[float]:
    """Room above and below the safe band, so the band reads as a band with edges
    rather than filling the plot."""
    if df.empty:
        return [0.0, 1.0]
    lo = float(min(df["min_temp_c"].min(), df["temperature_c"].min()))
    hi = float(max(df["max_temp_c"].max(), df["temperature_c"].max()))
    pad = max(1.5, (hi - lo) * 0.2)
    return [round(lo - pad, 1), round(hi + pad, 1)]
