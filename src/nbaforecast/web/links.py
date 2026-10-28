"""File names of generated pages, shared so pages can link to each other."""

from nbaforecast.web import fan


def team_slug(team: fan.Team) -> str:
    return f"team-{team.abbr.lower()}.html"


def game_slug(game_id: str) -> str:
    return f"game-{game_id}.html"
