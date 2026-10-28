"""Static public site, built from the data branch and the backtest outputs on main.

    nbaf site --data-dir <data branch checkout> --out <dir>

Everything is rendered at build time into plain HTML; charts are Vega-Lite specs embedded in
the page. The site makes no runtime API calls.
"""

import csv
import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

from nbaforecast import availability, config, metrics, projections, recap
from nbaforecast.ingest.espn import EASTERN  # noqa: F401  (slate dates stay US Eastern)
from nbaforecast.live import Store
from nbaforecast.models.elo import win_probability
from nbaforecast.web import fan, pages

HERE = Path(__file__).parent
REPO_URL = "https://github.com/garettprestholdt/nba-forecast"
CENTRAL = ZoneInfo("America/Chicago")  # every time shown on the site is Central
LIVE_CALIBRATION_MIN_GAMES = 100  # fewer than this and live calibration bins are mostly noise


def _pct(p: float | None) -> str:
    return "–" if p is None else f"{p:.0%}"


def _num(x: float | None, digits: int = 3) -> str:
    return "–" if x is None else f"{x:.{digits}f}"


def _nice_date(iso: str | None) -> str:
    return "–" if not iso else datetime.fromisoformat(iso).strftime("%a, %b %-d")


def _long_date(iso: str | None) -> str:
    return "–" if not iso else datetime.fromisoformat(iso).strftime("%A, %B %-d")


def _metric(x: float | None, fmt: str) -> str:
    if x is None:
        return "–"
    return {"pct1": f"{x * 100:.1f}%", "num1": f"{x:.1f}", "num3": f"{x:.3f}"}.get(fmt, str(x))


def _tip_day(tipoff_utc: str) -> str:
    return (
        datetime.fromisoformat(tipoff_utc.replace("Z", "+00:00"))
        .astimezone(CENTRAL)
        .strftime("%a %b %-d")
    )


def _signed(x: float | None) -> str:
    return "–" if x is None else f"{x:+.1f}"


def _odds(p: float | None) -> str:
    """Simulation odds: never print a flat 0% or 100% for something that can still happen."""
    if p is None:
        return "–"
    if p < 0.005:  # including 0 in 10,000 simulations: unlikely, not impossible
        return "<1%"
    if p > 0.995 and p < 1:
        return ">99%"
    return f"{p:.0%}"


def _simple_backtest(summary: dict, published: Path) -> list[dict]:
    """The held-out comparison in plain language: a few approaches, one line each."""
    rows = {r["model"]: r for r in summary["holdout"]}
    hybrid_path = published / "hybrid" / "summary.json"
    hybrid = {r["model"]: r for r in json.loads(hybrid_path.read_text())["holdout"]} \
        if hybrid_path.exists() else {}  # fmt: skip
    plan = [
        (rows.get("Coin flip"), "Coin flip", "Says 50% for every game.", "muted-row"),
        (rows.get("Previous season's home win rate"), "Always back the home team",
         "Says the home team wins as often as home teams did last season.", "muted-row"),
        (next((r for m, r in rows.items() if "adaptive" in m), None), "Our model (published)",
         "Rates every team from its results and margins; the forecast on this site.", "ours"),
        (hybrid.get("Hybrid, realistic"), "Team + player model",
         "Our model plus every player's production and playing time. Running in shadow mode.",
         ""),
        (hybrid.get("Hybrid, knows the lineup"), "…if we knew every lineup",
         "The same, told who's available each night. A ceiling, not a forecast we can make yet.",
         "muted-row"),
    ]  # fmt: skip
    return [{"name": name, "what": what, "cls": cls, "games": r["n"],
             "accuracy": r["accuracy"], "log_loss": r["log_loss"]}
            for r, name, what, cls in plan if r]  # fmt: skip


WIN_AXIS = (10, 72)  # the range-of-wins track on the standings page


def _win_pos(wins: float) -> float:
    lo, hi = WIN_AXIS
    return round(max(0.0, min(100.0, (wins - lo) / (hi - lo) * 100)), 2)


def _tip_et(tipoff_utc: str) -> str:
    t = datetime.fromisoformat(tipoff_utc).astimezone(CENTRAL)
    return t.strftime("%-I:%M %p CT")


def _franchises() -> dict[str, dict[str, str]]:
    with open(config.REPO_ROOT / "dbt" / "seeds" / "franchises.csv") as f:
        return {row["franchise_id"]: row for row in csv.DictReader(f)}


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


@dataclass
class Game:
    game_id: str
    game_date: str
    tip: str
    away: str
    home: str
    p_home: float
    market_p_home: float | None
    home_score: int | None = None
    away_score: int | None = None

    @property
    def final(self) -> bool:
        return self.home_score is not None

    @property
    def pick(self) -> str:
        return self.home if self.p_home >= 0.5 else self.away

    @property
    def pick_prob(self) -> float:
        return max(self.p_home, 1 - self.p_home)

    @property
    def market_pick(self) -> str | None:
        if self.market_p_home is None:
            return None
        return self.home if self.market_p_home >= 0.5 else self.away

    @property
    def home_won(self) -> bool | None:
        return None if not self.final else self.home_score > self.away_score

    @property
    def correct(self) -> bool | None:
        return None if not self.final else (self.p_home >= 0.5) == self.home_won

    @property
    def gap(self) -> float | None:
        """Elo minus market, in percentage points for the home team."""
        return None if self.market_p_home is None else self.p_home - self.market_p_home


def _games(store: Store, season: str) -> list[Game]:
    results = {r["game_id"]: r for r in store.read(store.results_path(season))}
    games = []
    for p in store.read(store.predictions_path(season)):
        r = results.get(p["game_id"])
        games.append(
            Game(
                game_id=p["game_id"],
                game_date=p["game_date"],
                tip=_tip_et(p["tipoff_utc"]),
                away=p["away_team"],
                home=p["home_team"],
                p_home=float(p["p_home"]),
                market_p_home=float(p["market_p_home"]) if p["market_p_home"] else None,
                home_score=int(r["home_score"]) if r else None,
                away_score=int(r["away_score"]) if r else None,
            )
        )
    return games


def _latest_season(store: Store) -> str:
    files = sorted((store.root / "predictions").glob("*.csv")) or sorted(
        (store.root / "results").glob("*.csv")
    )
    if not files:
        raise FileNotFoundError(f"no predictions or results under {store.root}")
    return files[-1].stem


# --- Vega-Lite specs. Colors are tokens ("@series-1") that charts.js swaps for the page's
# --- CSS custom properties, so light and dark mode both use their validated steps.

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


def _live_calibration(games: list[Game]) -> list[dict]:
    final = [g for g in games if g.final]
    if len(final) < LIVE_CALIBRATION_MIN_GAMES:
        return []
    table = metrics.calibration_table([g.p_home for g in final], [int(g.home_won) for g in final])
    return [
        {**row, "series": LIVE, "bin": f"{row['bin_low']:.0%}–{row['bin_high']:.0%}"}
        for row in table
    ]


def _injury_watch(store: Store, season: str, day: str | None) -> dict[str, Any] | None:
    """The latest slate's injury calls (keyword rule vs. agent) and the season's head-to-head."""
    log_path, results_path = availability.paths(store, season)
    if not log_path.exists() or day is None:
        return None
    outcomes = {(r["game_id"], r["player_id"]): r for r in store.read(results_path)}
    calls = []
    for r in store.read(log_path):
        if r["game_date"] != day:
            continue
        o = outcomes.get((r["game_id"], r["player_id"]))
        calls.append(
            {
                "player": r["player"],
                "team": r["team"],
                "opponent": r["opponent"],
                "note": r["note"],
                "baseline_p": float(r["baseline_p"]),
                "agent_p": float(r["agent_p"]) if r["agent_p"] else None,
                "quote": r["agent_quote"],
                "minutes_restriction": r["minutes_restriction"] == "True",
                "played": None if o is None else o["played"] == "True",
                "minutes": int(o["minutes"]) if o else None,
            }
        )
    card_path = store.root / "scorecard" / f"{season}-availability.json"
    card = json.loads(card_path.read_text()) if card_path.exists() else None
    return {"date": day, "calls": calls, "scorecard": card} if calls or card else None


def _shadow(store: Store, season: str, published: Path) -> dict[str, Any] | None:
    """The hybrid challenger's live scorecard (shadow mode) and its backtest, if present."""
    card_path = store.root / "scorecard" / f"{season}-hybrid.json"
    summary_path = published / "hybrid" / "summary.json"
    if not card_path.exists() or not summary_path.exists():
        return None
    holdout = {r["model"]: r for r in json.loads(summary_path.read_text())["holdout"]}
    return {
        "card": json.loads(card_path.read_text()),
        "backtest_hybrid": holdout.get("Hybrid, realistic"),
        "backtest_elo": holdout.get("Elo + back-to-back"),
    }


def _latest_recap(store: Store) -> dict[str, Any] | None:
    """The newest published recap as blocks: {"p": text} paragraphs and {"ul": [items]} lists."""
    files = sorted((store.root / "recaps").glob("*.md"))
    if not files:
        return None
    source, body = recap.read_published(files[-1])
    blocks = []
    for chunk in body.split("\n\n"):
        lines = [line.strip() for line in chunk.strip().splitlines() if line.strip()]
        if lines and all(line.startswith("- ") for line in lines):
            blocks.append({"ul": [line[2:] for line in lines]})
        elif lines:
            blocks.append({"p": " ".join(lines)})
    day = files[-1].stem
    return {"date": day, "source": source, "blocks": blocks, "facts_file": f"recaps/{day}.json"}


def build_context(data_dir: Path, published_dir: Path | None = None) -> dict[str, Any]:
    store = Store(data_dir)
    published = published_dir or config.PUBLISHED_DIR
    season = _latest_season(store)
    games = _games(store, season)
    scorecard = (
        json.loads(store.scorecard_path(season).read_text())
        if store.scorecard_path(season).exists()
        else None
    )
    state = store.load_state()
    franchises = _franchises()

    latest_date = max((g.game_date for g in games), default=None)
    slate = [g for g in games if g.game_date == latest_date]
    recent = sorted(
        (g for g in games if g.final), key=lambda g: (g.game_date, g.game_id), reverse=True
    )

    teams = sorted(
        (
            {
                "name": franchises[t]["name"] if t in franchises else t,
                "abbr": franchises[t]["abbr"] if t in franchises else t,
                "elo": rating,
                "vs_average": rating - state.params.mean,
                "p_vs_average": win_probability(rating - state.params.mean),
            }
            for t, rating in state.ratings.items()
            if t in franchises
        ),
        key=lambda row: -row["elo"],
    )

    backtest_dir = published / "backtest"
    summary = json.loads((backtest_dir / "summary.json").read_text())
    labels = {
        "Elo (chosen)": "Elo, fixed home court (chosen on training seasons)",
        "Elo, best adaptive home court": "Elo, adaptive home court (the live model)",
    }
    for row in summary["holdout"]:
        row["model"] = labels.get(row["model"], row["model"])
    live_backtest = next(r for r in summary["holdout"] if "live model" in r["model"])
    backtest_calibration = [
        {
            "series": BACKTEST_NAMES[r["model"]],
            "bin": f"{float(r['bin_low']):.0%}–{float(r['bin_high']):.0%}",
            "n": int(r["n"]),
            "mean_forecast": float(r["mean_forecast"]),
            "observed_rate": float(r["observed_rate"]),
        }
        for r in _read_csv(backtest_dir / "calibration.csv")
        if r["model"] in BACKTEST_NAMES
    ]
    by_season = [
        {
            "season": r["season"],
            "split": r["split"],
            "series": BACKTEST_NAMES[r["model"]],
            "n": int(r["n"]),
            "log_loss": float(r["log_loss"]),
        }
        for r in _read_csv(backtest_dir / "by_season.csv")
        if r["split"] in ("train", "holdout") and r["model"] in BACKTEST_NAMES
    ]
    live_cal = _live_calibration(games)
    market_points = [
        {
            "date": g.game_date,
            "matchup": f"{g.away} @ {g.home}",
            "elo": g.p_home,
            "market": g.market_p_home,
            "result": f"{g.away_score}–{g.home_score}" if g.final else "Not played yet",
            "gap": round((g.p_home - g.market_p_home) * 100, 1),
            "outcome": "Not played yet"
            if not g.final
            else ("We got it right" if g.correct else "We got it wrong"),
        }
        for g in games
        if g.market_p_home is not None
    ]

    hca = state.home_court()
    team_map = fan.teams(store.root, state.ratings, state.params.mean)
    tonight = fan.matchups(store, season, latest_date, team_map) if latest_date else []
    hybrid_summary = published / "hybrid" / "summary.json"
    standings = fan.standings(store.root, team_map)
    ratings_now = [t.rating for t in team_map.values() if t.rating is not None]
    return {
        "team_map": team_map,
        "espn_images": fan.USE_ESPN_IMAGES,
        "slate_date": latest_date,
        "games": tonight,
        "spotlight": fan.game_of_the_night([m for m in tonight if not m.final]),
        "ticker": fan.last_night(store, season, latest_date, team_map) if latest_date else [],
        "standings": standings,
        "power": sorted(
            (t for t in team_map.values() if t.rating is not None), key=lambda t: -t.rating
        ),
        "win_axis": WIN_AXIS,
        "rating_span": max([abs(r) for r in ratings_now] + [1.0]),
        "home_court_points": hca / projections.ELO_PER_POINT,
        "hybrid_backtest": (
            json.loads(hybrid_summary.read_text())["holdout"] if hybrid_summary.exists() else None
        ),
        "shadow": _shadow(store, season, published),
        "repo_url": REPO_URL,
        "season": season,
        "built_at": datetime.now(UTC).astimezone(CENTRAL).strftime("%b %-d, %Y %-I:%M %p CT"),
        "ratings_through": state.last_date.isoformat() if state.last_date else None,
        "latest_date": latest_date,
        "slate": slate,
        "recent": recent[:30],
        "scorecard": scorecard,
        "teams": teams,
        "home_court": hca,
        "home_court_p": win_probability(hca),
        "model_params": state.params.as_dict(),
        "backtest": summary,
        "live_backtest": live_backtest,
        "charts": {
            "calibration": _calibration_spec(
                [r for r in backtest_calibration if r["series"] == ADAPTIVE] + live_cal
            ),
            "market_gap": _market_gap_spec(market_points) if market_points else None,
            "by_season": _by_season_spec(by_season),
            "elo_vs_market": _elo_vs_market_spec(market_points) if market_points else None,
            "ratings": _ratings_spec(teams),
        },
        "simple_backtest": _simple_backtest(summary, published),
        "live_calibration_ready": bool(live_cal),
        "live_calibration_min": LIVE_CALIBRATION_MIN_GAMES,
        "backtest_calibration": backtest_calibration,
        "games_scored": sum(1 for g in games if g.final),
        "recap": _latest_recap(store),
        "injury_watch": _injury_watch(store, season, latest_date),
    }


PAGES = {
    "index.html": "index.html",
    "standings.html": "standings.html",
    "history.html": "history.html",
    "model.html": "model.html",
    "play.html": "play.html",
    "ratings.html": "ratings.html",  # redirect to standings.html#power
}


def build(data_dir: Path, out_dir: Path, published_dir: Path | None = None) -> list[Path]:
    """Render every page into `out_dir`. Returns the files written."""
    env = Environment(
        loader=FileSystemLoader(HERE / "templates"), autoescape=select_autoescape(["html"])
    )
    env.filters["pct"] = _pct
    env.filters["num"] = _num
    env.filters["nice_date"] = _nice_date
    env.filters["long_date"] = _long_date
    env.filters["signed"] = _signed
    env.filters["odds"] = _odds
    env.globals["win_pos"] = _win_pos
    env.filters["metric"] = _metric
    env.filters["tip_day"] = _tip_day
    context = build_context(data_dir, published_dir)
    # Cache-busting: asset URLs change whenever the CSS or JS changes.
    digest = hashlib.sha256()
    for asset in sorted((HERE / "static").iterdir()):
        digest.update(asset.read_bytes())
    context["asset_version"] = digest.hexdigest()[:10]

    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    root, season = Path(data_dir), context["season"]
    team_map = context["team_map"]
    court_data = pages.load_court(root, season)
    payloads = pages.player_payloads(root, season, team_map, court_data, context["slate_date"])
    published = published_dir or config.PUBLISHED_DIR
    context.update(
        history=pages.history(Store(root), season, team_map),
        puzzle=pages.rotation_puzzle(
            Store(root), season, context["slate_date"], team_map, published
        ),  # fmt: skip
        team_list=sorted(team_map.values(), key=lambda t: t.name),
        team_metrics=pages.TEAM_METRICS,
    )
    for name, template in PAGES.items():
        path = out_dir / name
        path.write_text(env.get_template(template).render(page=name, **context))
        written.append(path)
    state = Store(root).load_state()
    for tp in pages.team_pages(root, season, team_map, state, payloads).values():
        path = out_dir / tp.slug
        path.write_text(env.get_template("team.html").render(page=tp.slug, tp=tp, **context))
        written.append(path)
    for gp in pages.game_pages(root, season, context["games"], team_map, court_data):
        path = out_dir / gp["slug"]
        path.write_text(env.get_template("game.html").render(page=gp["slug"], gp=gp, **context))
        written.append(path)
    pages.write_player_payloads(out_dir, payloads)
    assets = out_dir / "assets"
    if assets.exists():
        shutil.rmtree(assets)
    shutil.copytree(HERE / "static", assets)
    (out_dir / ".nojekyll").write_text("")
    return written
