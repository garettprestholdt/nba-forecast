-- One row per ESPN game, typed, with All-Star games and unplayed games flagged (not dropped).

with source as (
    select * from {{ source('raw', 'espn_games') }}
),

franchises as (
    select franchise_id from {{ ref('franchises') }}
)

select
    game_id::varchar as game_id,
    season::varchar as season,
    season_type::varchar as season_type,
    tipoff_utc::timestamptz as tipoff_utc,
    game_date::date as game_date,
    status::varchar as status,
    completed::boolean as is_completed,
    neutral_site::boolean as is_neutral_site,
    venue::varchar as venue,
    home_team_id::varchar as home_team_id,
    home_team_abbr::varchar as home_team_abbr,
    home_team_name::varchar as home_team_name,
    away_team_id::varchar as away_team_id,
    away_team_abbr::varchar as away_team_abbr,
    away_team_name::varchar as away_team_name,
    home_score::integer as home_score,
    away_score::integer as away_score,
    periods::integer as periods,
    -- All-Star games are played by East/West (or captain-picked) teams, not franchises.
    (
        home_team_id::varchar not in (select franchise_id from franchises)
        or away_team_id::varchar not in (select franchise_id from franchises)
    ) as is_exhibition,
    fetched_at::timestamptz as fetched_at
from source
