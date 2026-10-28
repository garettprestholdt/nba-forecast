"""Static public site, built from the data branch and the backtest outputs on main.

    nbaf site --data-dir <data branch checkout> --out <dir>

Everything is rendered at build time into plain HTML; charts are Vega-Lite specs embedded in
the page. The site makes no runtime API calls (live scores load in the visitor's browser).

Modules: `build` (render all pages), `context` (shared page data), `charts` (Vega-Lite specs),
`filters` (Jinja filters), `fan` (teams, matchups, colors), one module per page type
(`players`, `teams`, `games`, `history`, `play`), and `links` (page file names).
Templates are in `templates/`, CSS and JS in `static/`.
"""

from nbaforecast.web.build import build
from nbaforecast.web.context import build_context

__all__ = ["build", "build_context"]
