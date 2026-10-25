-- Every completed, competitive NBA game: the input to the rating models.

with games as (
    select * from {{ ref('stg_espn_games') }}
    where is_completed and not is_exhibition
),

known_issues as (
    select game_id::varchar as game_id, issue from {{ ref('known_data_issues') }}
)

select
    game_id,
    season,
    season_type,
    tipoff_utc,
    game_date,
    home_team_id,
    home_team_abbr,
    away_team_id,
    away_team_abbr,
    home_score,
    away_score,
    home_score - away_score as home_margin,
    home_score > away_score as home_win,
    -- A few old games have incomplete line scores; overtime is unknown for those.
    case when periods >= 4 then periods - 4 end as overtime_periods,
    -- ESPN doesn't flag the 2020 Orlando bubble as neutral, but nobody had home court there.
    (season = '2019-20' and game_date >= date '2020-07-30') as is_bubble,
    is_neutral_site or (season = '2019-20' and game_date >= date '2020-07-30') as is_neutral_site,
    venue,
    known_issues.issue as known_issue
from games
left join known_issues using (game_id)
