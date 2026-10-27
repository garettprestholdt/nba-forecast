-- One row per team per game: box-score totals, possessions, efficiency, and the four factors
-- for the team and its opponent. These are the "good team vs. bad team" metrics.
-- Only games where both teams' box scores have stats (see stg_espn_player_games). Points come
-- from the final score: ESPN's player lines occasionally sum a point or two short of it.

with totals as (
    select
        game_id,
        team_id,
        any_value(opponent_id) as opponent_id,
        any_value(is_home) as is_home,
        any_value(season) as season,
        any_value(season_type) as season_type,
        any_value(game_date) as game_date,
        sum(minutes) as minutes,
        sum(fgm) as fgm,
        sum(fga) as fga,
        sum(fg3m) as fg3m,
        sum(fg3a) as fg3a,
        sum(ftm) as ftm,
        sum(fta) as fta,
        sum(oreb) as oreb,
        sum(dreb) as dreb,
        sum(ast) as ast,
        sum(stl) as stl,
        sum(blk) as blk,
        sum(tov) as tov,
        sum(pf) as pf,
        count(*) filter (where played) as players_used
    from {{ ref('fct_player_games') }}
    where not stats_missing
    group by game_id, team_id
),

scored as (
    select
        totals.*,
        case when totals.is_home then g.home_score else g.away_score end as pts
    from totals
    inner join {{ ref('fct_games') }} as g using (game_id)
),

paired as (
    select
        t.*,
        o.pts as opp_pts,
        o.fgm as opp_fgm,
        o.fga as opp_fga,
        o.fg3m as opp_fg3m,
        o.fta as opp_fta,
        o.ftm as opp_ftm,
        o.oreb as opp_oreb,
        o.dreb as opp_dreb,
        o.tov as opp_tov,
        -- Possessions: the standard box-score estimate, averaged over both teams.
        (
            (t.fga + 0.44 * t.fta - t.oreb + t.tov) + (o.fga + 0.44 * o.fta - o.oreb + o.tov)
        ) / 2 as possessions
    from scored as t
    inner join scored as o on t.game_id = o.game_id and t.team_id != o.team_id
)

select
    game_id,
    team_id,
    opponent_id,
    is_home,
    season,
    season_type,
    game_date,
    pts,
    opp_pts,
    pts - opp_pts as margin,
    pts > opp_pts as won,
    minutes,
    players_used,
    round(possessions, 1) as possessions,
    round(100 * pts / possessions, 1) as off_rating,
    round(100 * opp_pts / possessions, 1) as def_rating,
    round(100 * (pts - opp_pts) / possessions, 1) as net_rating,
    -- Four factors (Dean Oliver), for the team and its opponent.
    round((fgm + 0.5 * fg3m) / nullif(fga, 0), 4) as efg_pct,
    round(tov / nullif(fga + 0.44 * fta + tov, 0), 4) as tov_rate,
    round(oreb / nullif(oreb + opp_dreb, 0), 4) as oreb_rate,
    round(ftm / nullif(fga, 0), 4) as ft_rate,
    round((opp_fgm + 0.5 * opp_fg3m) / nullif(opp_fga, 0), 4) as opp_efg_pct,
    round(opp_tov / nullif(opp_fga + 0.44 * opp_fta + opp_tov, 0), 4) as opp_tov_rate,
    round(opp_oreb / nullif(opp_oreb + dreb, 0), 4) as opp_oreb_rate,
    round(opp_ftm / nullif(opp_fga, 0), 4) as opp_ft_rate
from paired
