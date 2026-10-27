-- Each team's player points must add up to its final score. ESPN's own player lines are
-- occasionally a few points short of its team total (about 30 of 28,000 games since 2004), so a
-- team-game may be off by up to 4 points, in at most 0.5% of a season's team-games. Anything
-- bigger means a parsing problem (a dropped or double-counted player) unless the game was
-- investigated and listed in seeds/known_data_issues.csv.
with box as (
    select game_id, is_home, any_value(season) as season, sum(pts) as pts
    from {{ ref('fct_player_games') }}
    where not stats_missing
    group by game_id, is_home
),

checked as (
    select
        box.*,
        box.pts - case when box.is_home then g.home_score else g.away_score end as gap,
        g.known_issue
    from box
    inner join {{ ref('fct_games') }} as g using (game_id)
),

seasons as (
    select season, count(*) filter (where gap != 0) / count(*) as share_off
    from checked
    group by season
)

select checked.game_id, checked.is_home, checked.gap, seasons.share_off
from checked
inner join seasons using (season)
where (abs(checked.gap) > 4 and checked.known_issue is null)
   or (checked.gap != 0 and seasons.share_off > 0.005)
