"""Fan-facing pieces of the site: matchup cards with player scores, last night's results,
projected standings, team identity (logos and colors).

Logos and player photos are loaded from ESPN's image CDN at view time (not copied into the
repo). Set USE_ESPN_IMAGES = False to fall back to team monograms and player initials.
"""

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from nbaforecast import projections, shadow
from nbaforecast.ingest import rosters
from nbaforecast.ingest.espn import EASTERN
from nbaforecast.live import Store

USE_ESPN_IMAGES = True
CENTRAL = ZoneInfo("America/Chicago")  # times shown on the site
KEY_PLAYERS = 5  # per team, by usual minutes, among players not ruled out
OUT_MIN_ROLE = 15.0  # list a ruled-out player if they usually play at least this many minutes
SCORE_SCALE = 25.0  # a projected Game Score of 25 fills the bar


ESPN_IMG = "https://a.espncdn.com/combiner/i?img="  # ESPN's resizer: ~5-8 KB instead of 50-200 KB


def logo_url(slug: str, dark: bool = False) -> str | None:
    """Team logo, resized to 160 px (sharp at the largest size shown on high-density screens)."""
    if not USE_ESPN_IMAGES:
        return None
    return f"{ESPN_IMG}/i/teamlogos/nba/500{'-dark' if dark else ''}/{slug}.png&w=160&h=160"


def headshot_url(player_id: str) -> str | None:
    """Player photo, resized for the 34 px avatars (2x for high-density screens)."""
    if not USE_ESPN_IMAGES or not player_id.isdigit():
        return None
    return f"{ESPN_IMG}/i/headshots/nba/players/full/{player_id}.png&w=96&h=70"


# --- team identity ------------------------------------------------------------------------
def _rgb(hex_color: str) -> tuple[float, float, float]:
    h = hex_color.lstrip("#")
    return tuple(int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def _luminance(hex_color: str) -> float:
    def channel(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in _rgb(hex_color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _distance(a: str, b: str) -> float:
    return sum((x - y) ** 2 for x, y in zip(_rgb(a), _rgb(b), strict=True)) ** 0.5


SURFACES = {"light": "#ffffff", "dark": "#16171a"}  # card backgrounds in each theme
NEUTRAL = "#8a8a85"
MIN_CONTRAST = 1.8  # a team color must stand out this much from the card to carry a bar


def _contrast(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def display_color(team: dict, mode: str) -> str:
    """The team's primary color, or its alternate if the primary would vanish against this
    theme's card (black on dark, white on light), else a neutral gray."""
    for c in (team["color"], team["alt_color"]):
        if _contrast(c, SURFACES[mode]) >= MIN_CONTRAST:
            return c
    return NEUTRAL


def matchup_colors(home: dict, away: dict, mode: str = "light") -> tuple[str, str]:
    """Bar colors for the two sides in one theme. If they're too alike to tell apart, the away
    side switches to its other color, or to neutral gray."""
    h, a = display_color(home, mode), display_color(away, mode)
    if _distance(h, a) < 0.35:
        options = [c for c in (away["alt_color"], away["color"]) if c != a]
        a = next(
            (c for c in options
             if _contrast(c, SURFACES[mode]) >= MIN_CONTRAST and _distance(h, c) >= 0.35),
            NEUTRAL,
        )  # fmt: skip
    return h, a


def ink_on(hex_color: str) -> str:
    """Black or white text, whichever reads better on this background."""
    return "#0b0b0b" if _luminance(hex_color) > 0.4 else "#ffffff"


@dataclass
class Team:
    team_id: str
    abbr: str
    name: str
    short: str
    conference: str
    color: str
    alt_color: str
    logo: str | None
    logo_dark: str | None
    wins: int = 0
    losses: int = 0
    rating: float | None = None

    @property
    def record(self) -> str:
        return f"{self.wins}-{self.losses}"

    @property
    def city(self) -> str:
        return self.name.removesuffix(self.short).strip()

    @property
    def ink(self) -> str:
        return ink_on(self.color)

    @property
    def color_l(self) -> str:
        return display_color(self.__dict__, "light")

    @property
    def color_d(self) -> str:
        return display_color(self.__dict__, "dark")


def teams(root: Path, ratings: dict[str, float], mean: float) -> dict[str, Team]:
    """Every franchise with its logo, colors, rating in points (from the published Elo
    ratings), and record (official standings from the projections run, else the results log)."""
    proj = load_projections(root)
    records = {t: (v["wins"], v["losses"]) for t, v in (proj or {}).get("teams", {}).items()}
    if not records:
        records = _records_from_results(root)
    out = {}
    for tid, f in projections.franchises().items():
        wins, losses = records.get(tid, (0, 0))
        out[tid] = Team(
            tid, f["abbr"], f["name"], f["short_name"], f["conference"], f["color"],
            f["alt_color"], logo_url(f["logo"]), logo_url(f["logo"], dark=True), wins, losses,
            round(projections.rating_points(ratings[tid], mean), 1) if tid in ratings else None,
        )  # fmt: skip
    return out


def _records_from_results(root: Path) -> dict[str, tuple[int, int]]:
    files = sorted((Path(root) / "results").glob("*.csv"))
    if not files:
        return {}
    wl: dict[str, list[int]] = {}
    for r in Store.read(files[-1]):
        if r["season_type"] != "regular_season":
            continue
        home_won = int(r["home_score"]) > int(r["away_score"])
        for team, won in ((r["home_team_id"], home_won), (r["away_team_id"], not home_won)):
            wl.setdefault(team, [0, 0])[0 if won else 1] += 1
    return {t: (won, lost) for t, (won, lost) in wl.items()}


# --- players ------------------------------------------------------------------------------
@dataclass
class Player:
    player_id: str
    name: str
    short: str
    position: str
    status: str  # "", "Day-To-Day", "Out"
    minutes: float  # usual minutes when they play
    score: float  # projected Game Score tonight (production per minute x usual minutes)
    headshot: str | None

    @property
    def initials(self) -> str:
        return "".join(part[0] for part in self.name.split()[:2]).upper()

    @property
    def bar(self) -> float:
        return max(0.0, min(1.0, self.score / SCORE_SCALE))


@dataclass
class Lineup:
    key: list[Player] = field(default_factory=list)
    out: list[Player] = field(default_factory=list)


def lineups(root: Path, team_ids: set[str]) -> dict[str, Lineup]:
    """Key players and notable absences for each team, from the hybrid's player ratings and
    ESPN's current rosters. Empty if either isn't on the data branch yet."""
    directory = rosters.load_directory(root)
    try:
        state = shadow.load_state(root).players
    except FileNotFoundError:
        return {}
    if not directory:
        return {}
    out: dict[str, Lineup] = {t: Lineup() for t in team_ids}
    for pid, info in directory.items():
        team = info["team_id"]
        if team not in out:
            continue
        known = pid in state.players
        minutes = state.role(pid) if known else 0.0
        player = Player(
            pid, info["name"], info["short"], info["position"], info["status"], minutes,
            round(state.production(pid) * minutes, 1) if known else 0.0, headshot_url(pid),
        )  # fmt: skip
        if info["status"] == "Out":
            if minutes >= OUT_MIN_ROLE:
                out[team].out.append(player)
        elif known:
            out[team].key.append(player)
    for lineup in out.values():
        lineup.key = sorted(lineup.key, key=lambda p: -p.minutes)[:KEY_PLAYERS]
        lineup.key.sort(key=lambda p: -p.score)
        lineup.out = sorted(lineup.out, key=lambda p: -p.score)[:3]
    return out


# --- games --------------------------------------------------------------------------------
@dataclass
class Matchup:
    game_id: str
    tip: str
    tip_sort: str
    home: Team
    away: Team
    p_home: float  # published model (Elo)
    p_hybrid: float | None
    p_market: float | None
    colors: dict[str, str]  # home_l, away_l, home_d, away_d
    home_lineup: Lineup | None
    away_lineup: Lineup | None
    neutral: bool
    home_score: int | None = None
    away_score: int | None = None

    @property
    def favorite(self) -> Team:
        return self.home if self.p_home >= 0.5 else self.away

    @property
    def closeness(self) -> float:
        return abs(self.p_home - 0.5)

    @property
    def final(self) -> bool:
        return self.home_score is not None


def _tip(tipoff_utc: str) -> str:
    return datetime.fromisoformat(tipoff_utc).astimezone(CENTRAL).strftime("%-I:%M %p")


def matchups(store: Store, season: str, day: str, team_map: dict[str, Team]) -> list[Matchup]:
    preds = [p for p in store.read(store.predictions_path(season)) if p["game_date"] == day]
    hybrid = {r["game_id"]: float(r["p_home"])
              for r in store.read(shadow.log_path(store.root, season))}  # fmt: skip
    results = {r["game_id"]: r for r in store.read(store.results_path(season))}
    playing = {p["home_team_id"] for p in preds} | {p["away_team_id"] for p in preds}
    lines = lineups(store.root, playing)
    out = []
    for p in preds:
        home, away = team_map[p["home_team_id"]], team_map[p["away_team_id"]]
        hl, al = matchup_colors(home.__dict__, away.__dict__, "light")
        hd, ad = matchup_colors(home.__dict__, away.__dict__, "dark")
        colors = {"home_l": hl, "away_l": al, "home_d": hd, "away_d": ad}
        r = results.get(p["game_id"])
        out.append(
            Matchup(
                p["game_id"], _tip(p["tipoff_utc"]), p["tipoff_utc"], home, away,
                float(p["p_home"]), hybrid.get(p["game_id"]),
                float(p["market_p_home"]) if p["market_p_home"] else None, colors,
                lines.get(home.team_id), lines.get(away.team_id), p["is_neutral_site"] == "True",
                int(r["home_score"]) if r else None, int(r["away_score"]) if r else None,
            )
        )  # fmt: skip
    return sorted(out, key=lambda m: (m.tip_sort, m.game_id))


def game_of_the_night(games: list[Matchup]) -> Matchup | None:
    """The closest game between two good teams: highest combined rating among the three
    most even matchups."""
    if not games:
        return None
    even = sorted(games, key=lambda m: m.closeness)[:3]
    return max(even, key=lambda m: (m.home.rating or 0) + (m.away.rating or 0))


@dataclass
class Final:
    home: Team
    away: Team
    home_score: int
    away_score: int
    p_home: float | None  # None if the model didn't predict this game

    @property
    def winner(self) -> Team:
        return self.home if self.home_score > self.away_score else self.away

    @property
    def called(self) -> bool | None:
        if self.p_home is None:
            return None
        return (self.p_home >= 0.5) == (self.home_score > self.away_score)

    @property
    def winner_p(self) -> float | None:
        if self.p_home is None:
            return None
        return self.p_home if self.winner is self.home else 1 - self.p_home


def last_night(store: Store, season: str, today: str, team_map: dict[str, Team]) -> list[Final]:
    """Final scores from the most recent date before `today` that has results."""
    results = [r for r in store.read(store.results_path(season)) if r["game_date"] < today]
    if not results:
        return []
    day = max(r["game_date"] for r in results)
    preds = {p["game_id"]: float(p["p_home"]) for p in store.read(store.predictions_path(season))}
    return [
        Final(
            team_map[r["home_team_id"]],
            team_map[r["away_team_id"]],
            int(r["home_score"]),
            int(r["away_score"]),
            preds.get(r["game_id"]),
        )  # fmt: skip
        for r in results
        if r["game_date"] == day
    ]


# --- standings ----------------------------------------------------------------------------
def load_projections(root: Path) -> dict | None:
    files = sorted((Path(root) / "projections").glob("*.json"))
    return json.loads(files[-1].read_text()) if files else None


@dataclass
class Standing:
    team: Team
    proj_wins: float
    wins_p10: float
    wins_p90: float
    playoffs: float
    playin: float
    top_seed: float
    conf_finals: float
    finals: float
    title: float

    @property
    def proj_record(self) -> str:
        w = round(self.proj_wins)
        return f"{w}-{projections.GAMES - w}"


def standings(root: Path, team_map: dict[str, Team]) -> dict[str, Any] | None:
    proj = load_projections(root)
    if not proj:
        return None
    rows = []
    for tid, v in proj["teams"].items():
        rows.append(Standing(team_map[tid], v["proj_wins"], v["wins_p10"], v["wins_p90"],
                             v["playoffs"], v["playin"], v["top_seed"], v["conf_finals"],
                             v["finals"], v["title"]))  # fmt: skip
    by_conf = {
        c: sorted((r for r in rows if r.team.conference == c), key=lambda r: -r.proj_wins)
        for c in ("East", "West")
    }
    return {
        "updated_at": proj["updated_at"],
        "ratings_through": proj["ratings_through"],
        "simulations": proj["simulations"],
        "conferences": by_conf,
        "contenders": sorted(rows, key=lambda r: -r.title)[:8],
    }


def today_eastern() -> date:
    return datetime.now(EASTERN).date()


def previous_day(iso: str) -> str:
    return (date.fromisoformat(iso) - timedelta(days=1)).isoformat()
