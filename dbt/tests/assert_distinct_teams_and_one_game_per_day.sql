-- A team never plays itself and never plays twice on the same (Eastern) date.
-- Duplicate copies of one game under two IDs would show up here. Games with a documented
-- bad date (seeds/known_data_issues.csv) are excluded.
with games as (
    select * from {{ ref('fct_games') }} where known_issue is null
),

appearances as (
    select game_id, game_date, home_team_id as team_id from games
    union all
    select game_id, game_date, away_team_id as team_id from games
)

select team_id, game_date, count(*) as games, string_agg(game_id, ', ') as game_ids
from appearances
group by team_id, game_date
having count(*) > 1
