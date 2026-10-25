{{ config(enabled=var('check_season_counts', true)) }}

-- Each completed season has the exact number of regular-season games we expect, all 30
-- franchises, and a playoff count a best-of-7 bracket allows (15 series: 60 to 105 games).
with counts as (
    select
        season,
        count(*) filter (where season_type = 'regular_season') as regular_season_games,
        count(*) filter (where season_type = 'playoffs') as playoff_games,
        count(distinct home_team_id) filter (where season_type = 'regular_season') as franchises
    from {{ ref('fct_games') }}
    group by season
)

select s.season, s.expected_regular_season_games, c.regular_season_games, c.playoff_games, c.franchises
from {{ ref('seasons') }} as s
left join counts as c using (season)
where c.season is null
   or c.regular_season_games != s.expected_regular_season_games
   or c.playoff_games not between 60 and 105
   or c.franchises != 30
