-- Every player appearance (or DNP) in a competitive game, with a per-game Game Score.

with box as (
    select * from {{ ref('stg_espn_player_games') }}
),

games as (
    select game_id, season_type, home_score, away_score from {{ ref('fct_games') }}
)

select
    box.*,
    games.season_type,
    -- Without stats, "played" falls back to ESPN's did-not-play flag.
    case
        when box.stats_missing then not box.did_not_play
        else box.minutes > 0 and not box.did_not_play
    end as played,
    -- John Hollinger's Game Score: a box-score summary of one game's production, on a points
    -- scale (about 10 is an average game, 40 is a great one). It favors offense; defense
    -- shows up mostly through steals and blocks.
    round(
        box.pts + 0.4 * box.fgm - 0.7 * box.fga - 0.4 * (box.fta - box.ftm)
        + 0.7 * box.oreb + 0.3 * box.dreb + box.stl + 0.7 * box.ast + 0.7 * box.blk
        - 0.4 * box.pf - box.tov,
        1
    ) as game_score,
    case
        when box.is_home then games.home_score - games.away_score
        else games.away_score - games.home_score
    end as team_margin
from box
inner join games using (game_id)
