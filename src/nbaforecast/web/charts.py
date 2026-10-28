"""Vega-Lite chart specs. Colors are tokens (`@series-1`) that static/charts.js resolves
from CSS custom properties."""

from nbaforecast.ingest.espn import EASTERN  # noqa: F401  (slate dates stay US Eastern)

AXIS_PCT = {"format": ".0%", "tickCount": 5}


LOGLOSS_RANGE = (0.56, 0.71)


FIXED = "Elo, fixed home court"


ADAPTIVE = "Elo, adaptive home court (live model)"


LIVE = "Live, this season"


BACKTEST_NAMES = {"Elo (chosen)": FIXED, "Elo, best adaptive home court": ADAPTIVE}


# Color follows the entity on every chart: the live model is blue, the comparison is muted.
SERIES_COLORS = {ADAPTIVE: "@series-1", FIXED: "@ink-muted", LIVE: "@series-2"}


def _color(names: list[str]) -> dict:
    return {
        "field": "series",
        "type": "nominal",
        "scale": {"domain": names, "range": [SERIES_COLORS[n] for n in names]},
        "legend": {"orient": "top", "title": None, "labelLimit": 320},
    }


def _calibration_spec(rows: list[dict]) -> dict:
    """Mean forecast (x) vs. observed home win rate (y), one dot per probability bin."""
    names = [n for n in (ADAPTIVE, FIXED, LIVE) if any(r["series"] == n for r in rows)]
    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "width": "container",
        "height": 320,
        "layer": [
            {
                "data": {"values": [{"x": 0, "y": 0}, {"x": 1, "y": 1}]},
                "mark": {"type": "line", "color": "@ink-muted", "strokeWidth": 1, "opacity": 0.6},
                "encoding": {
                    "x": {"field": "x", "type": "quantitative"},
                    "y": {"field": "y", "type": "quantitative"},
                },
            },
            {
                "data": {"values": rows},
                "encoding": {
                    "x": {
                        "field": "mean_forecast",
                        "type": "quantitative",
                        "title": "Forecast home win probability",
                        "scale": {"domain": [0, 1]},
                        "axis": AXIS_PCT,
                    },
                    "y": {
                        "field": "observed_rate",
                        "type": "quantitative",
                        "title": "Home teams actually won",
                        "scale": {"domain": [0, 1]},
                        "axis": AXIS_PCT,
                    },
                    "color": _color(names),
                },
                "layer": [
                    {"mark": {"type": "line", "strokeWidth": 2, "strokeCap": "round"}},
                    {
                        "mark": {
                            "type": "point",
                            "filled": True,
                            "size": 80,
                            "strokeWidth": 2,
                            "stroke": "@surface",
                            "opacity": 1,
                        },
                        "encoding": {
                            "tooltip": [
                                {"field": "series", "title": "Model"},
                                {"field": "bin", "title": "Forecast bin"},
                                {"field": "n", "title": "Games", "format": ","},
                                {
                                    "field": "mean_forecast",
                                    "title": "Avg. forecast",
                                    "format": ".1%",
                                },
                                {"field": "observed_rate", "title": "Home won", "format": ".1%"},
                            ]
                        },
                    },
                ],
            },
        ],
    }


def _by_season_spec(rows: list[dict]) -> dict:
    """Log-loss per season for both Elo variants; held-out seasons shaded; coin flip for scale."""
    holdout = sorted({r["season"] for r in rows if r["split"] == "holdout"})
    names = [n for n in (ADAPTIVE, FIXED) if any(r["series"] == n for r in rows)]
    # Bars default to a zero-based scale; pin the range on every layer so the lines stay readable.
    y_scale = {"domain": list(LOGLOSS_RANGE), "zero": False, "nice": False}
    x = {
        "field": "season",
        "type": "ordinal",
        "title": None,
        "scale": {"paddingInner": 0, "paddingOuter": 0.1},
        "axis": {"labelAngle": -45, "labelExpr": "substring(datum.label, 2)"},
    }
    layers = []
    if holdout:
        layers.append(
            {
                "data": {"values": [{"season": s} for s in holdout]},
                "mark": {
                    "type": "bar",
                    "color": "@ink-muted",
                    "opacity": 0.1,
                    "width": {"band": 1},
                },
                "encoding": {
                    "x": x,
                    "y": {"datum": LOGLOSS_RANGE[1], "type": "quantitative", "scale": y_scale},
                    "y2": {"datum": LOGLOSS_RANGE[0]},
                },
            }
        )
    layers += [
        {
            "data": {"values": [{"y": 0.6931, "label": "Coin flip (0.693)"}]},
            "layer": [
                {
                    "mark": {"type": "rule", "color": "@ink-muted", "strokeWidth": 1},
                    "encoding": {"y": {"field": "y", "type": "quantitative"}},
                },
                {
                    "mark": {
                        "type": "text",
                        "align": "left",
                        "baseline": "bottom",
                        "dx": 4,
                        "dy": -3,
                        "color": "@ink-secondary",
                        "fontSize": 11,
                    },
                    "encoding": {
                        "y": {"field": "y", "type": "quantitative"},
                        "x": {"value": 0},
                        "text": {"field": "label"},
                    },
                },
            ],
        },
        {
            "data": {"values": rows},
            "encoding": {
                "x": x,
                "y": {
                    "field": "log_loss",
                    "type": "quantitative",
                    "title": "Log-loss (lower is better)",
                    "scale": y_scale,
                },
                "color": _color(names),
            },
            "layer": [
                {
                    "mark": {
                        "type": "line",
                        "strokeWidth": 2,
                        "strokeCap": "round",
                        "strokeJoin": "round",
                    }
                },
                {
                    "mark": {
                        "type": "point",
                        "filled": True,
                        "size": 64,
                        "strokeWidth": 2,
                        "stroke": "@surface",
                        "opacity": 1,
                    },
                    "encoding": {
                        "tooltip": [
                            {"field": "season", "title": "Season"},
                            {"field": "split", "title": "Used for"},
                            {"field": "series", "title": "Model"},
                            {"field": "log_loss", "title": "Log-loss", "format": ".4f"},
                            {"field": "n", "title": "Games", "format": ","},
                        ]
                    },
                },
            ],
        },
    ]
    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "width": "container",
        "height": 300,
        "layer": layers,
    }


def _elo_vs_market_spec(points: list[dict]) -> dict:
    """Ours vs. the market, both 0-100% on equal-length axes, so the diagonal means agreement."""
    axis_x = {"field": "market", "type": "quantitative", "title": "Betting market: home team",
              "scale": {"domain": [0, 1]}, "axis": AXIS_PCT}  # fmt: skip
    axis_y = {"field": "elo", "type": "quantitative", "title": "Our model: home team",
              "scale": {"domain": [0, 1]}, "axis": AXIS_PCT}  # fmt: skip
    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "width": 300,
        "height": 300,
        "layer": [
            {
                "data": {"values": [{"market": 0, "elo": 0}, {"market": 1, "elo": 1}]},
                "mark": {
                    "type": "line",
                    "color": "@ink-muted",
                    "strokeWidth": 1,
                    "strokeDash": [4, 4],
                },  # fmt: skip
                "encoding": {"x": axis_x, "y": axis_y},
            },
            {
                "data": {"values": points},
                "mark": {
                    "type": "point",
                    "filled": True,
                    "size": 70,
                    "strokeWidth": 2,
                    "stroke": "@surface",
                    "color": "@series-1",
                },  # fmt: skip
                "encoding": {
                    "x": axis_x,
                    "y": axis_y,
                    "tooltip": [
                        {"field": "date", "title": "Date"},
                        {"field": "matchup", "title": "Game"},
                        {"field": "elo", "title": "Us", "format": ".1%"},
                        {"field": "market", "title": "Market", "format": ".1%"},
                        {"field": "result", "title": "Result"},
                    ],
                },
            },
        ],
    }


def _market_gap_spec(points: list[dict]) -> dict:
    """The residual view: our probability minus the market's (points), against the market's."""
    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "width": "container",
        "height": 220,
        "layer": [
            {
                "data": {"values": [{"y": 0}]},
                "mark": {"type": "rule", "color": "@ink-muted", "strokeWidth": 1},
                "encoding": {"y": {"field": "y", "type": "quantitative"}},
            },
            {
                "data": {"values": points},
                "mark": {"type": "point", "size": 70, "strokeWidth": 2, "color": "@series-1"},
                "encoding": {
                    "x": {
                        "field": "market",
                        "type": "quantitative",
                        "scale": {"domain": [0, 1]},
                        "axis": AXIS_PCT,
                        "title": "Betting market: home team",
                    },  # fmt: skip
                    "y": {
                        "field": "gap",
                        "type": "quantitative",
                        "title": "Us minus market (points)",
                    },  # fmt: skip
                    "fill": {
                        "field": "outcome",
                        "type": "nominal",
                        "scale": {
                            "domain": ["We got it right", "We got it wrong", "Not played yet"],
                            "range": ["@series-1", "@surface", "@surface"],
                        },
                        "legend": {"orient": "top", "title": None},
                    },  # fmt: skip
                    "tooltip": [
                        {"field": "matchup", "title": "Game"},
                        {"field": "gap", "title": "Us minus market", "format": "+.1f"},
                        {"field": "result", "title": "Result"},
                    ],
                },
            },
        ],
    }


def _ratings_spec(teams: list[dict]) -> dict:
    """Diverging bars around the league average (1500): above in blue, below in red."""
    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "width": "container",
        "height": {"step": 22},
        "data": {"values": teams},
        "transform": [
            {"calculate": "datum.elo >= 1500 ? 'Above average' : 'Below average'", "as": "side"}
        ],
        "mark": {"type": "bar", "size": 14, "cornerRadiusEnd": 4},
        "encoding": {
            "y": {
                "field": "name",
                "type": "nominal",
                "sort": "-x",
                "title": None,
                "axis": {"labelLimit": 180},
            },
            "x": {
                "field": "vs_average",
                "type": "quantitative",
                "title": "Elo points above or below league average (1500)",
            },
            "color": {
                "field": "side",
                "type": "nominal",
                "scale": {
                    "domain": ["Above average", "Below average"],
                    "range": ["@diverge-pos", "@diverge-neg"],
                },
                "legend": {"orient": "top", "title": None},
            },
            "tooltip": [
                {"field": "name", "title": "Team"},
                {"field": "elo", "title": "Elo", "format": ".0f"},
                {
                    "field": "p_vs_average",
                    "title": "Win prob. vs average team (neutral court)",
                    "format": ".0%",
                },
            ],
        },
    }
