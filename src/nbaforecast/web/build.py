"""Render every page of the static site into an output folder."""

import hashlib
import shutil
from datetime import date, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from nbaforecast import config
from nbaforecast.ingest.espn import EASTERN  # noqa: F401  (slate dates stay US Eastern)
from nbaforecast.pipeline.live import Store
from nbaforecast.web import fan, games, history, play, players, teams
from nbaforecast.web.context import build_context
from nbaforecast.web.filters import (
    _long_date,
    _metric,
    _nice_date,
    _num,
    _odds,
    _pct,
    _signed,
    _tip_day,
    _tip_time,
    _win_pos,
)

HERE = Path(__file__).parent


PAGES = {
    "index.html": "index.html",
    "standings.html": "standings.html",
    "history.html": "history.html",
    "model.html": "model.html",
    "play.html": "play.html",
    "ratings.html": "ratings.html",  # redirect to standings.html#power
}


def build(
    data_dir: Path, out_dir: Path, published_dir: Path | None = None, today: date | None = None
) -> list[Path]:
    """Render every page into `out_dir`. Returns the files written. `today` (Central) decides
    which Cold Case files are published; it defaults to now."""
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
    env.filters["tip_time"] = _tip_time
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
    court_data = players.load_court(root, season)
    payloads = players.player_payloads(root, season, team_map, court_data, context["slate_date"])
    published = published_dir or config.PUBLISHED_DIR
    context.update(
        history=history.history(Store(root), season, team_map),
        coldcase=play.coldcase(published, out_dir, today or datetime.now(fan.CENTRAL).date()),
        team_list=sorted(team_map.values(), key=lambda t: t.name),
        team_metrics=teams.TEAM_METRICS,
    )
    for name, template in PAGES.items():
        path = out_dir / name
        path.write_text(env.get_template(template).render(page=name, **context))
        written.append(path)
    state = Store(root).load_state()
    slate_ids = {m.game_id for m in context["games"]}
    for tp in teams.team_pages(root, season, team_map, state, payloads, slate_ids).values():
        path = out_dir / tp.slug
        path.write_text(env.get_template("team.html").render(page=tp.slug, tp=tp, **context))
        written.append(path)
    for gp in games.game_pages(root, season, context["games"], team_map, court_data):
        path = out_dir / gp["slug"]
        path.write_text(env.get_template("game.html").render(page=gp["slug"], gp=gp, **context))
        written.append(path)
    players.write_player_payloads(out_dir, payloads)
    assets = out_dir / "assets"
    if assets.exists():
        shutil.rmtree(assets)
    shutil.copytree(HERE / "static", assets)
    (out_dir / ".nojekyll").write_text("")
    return written
