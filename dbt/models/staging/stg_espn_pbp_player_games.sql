-- Player lines rebuilt from ESPN play-by-play (`nbaf pbp`), for games whose box score is blank
-- plus a validation sample of games that have one. ESPN's play-by-play sometimes drops plays,
-- so each team-game records whether its rebuilt points add up to the final score
-- (points_match) and whether lineup tracking stayed consistent (five on the floor, no
-- unexplained substitutions). A team-game is usable only when both hold.

with rebuilt as (
    select
        box.game_id::varchar as game_id,
        coalesce(aliases.player_id, box.player_id::varchar) as player_id,
        box.team_id::varchar as team_id,
        box.is_home::boolean as is_home,
        box.lineup_consistent::boolean as lineup_consistent,
        {% for col in box_stat_columns() -%}
        box.{{ col }}::integer as {{ col }}{{ ',' if not loop.last }}
        {% endfor %}
    from {{ source('raw', 'espn_pbp_boxscores') }} as box
    left join (
        select alias_id::varchar as alias_id, player_id from {{ ref('player_id_aliases') }}
    ) as aliases on box.player_id::varchar = aliases.alias_id
),

team_points as (
    select game_id, team_id, any_value(is_home) as is_home, sum(pts) as pts, sum(minutes) as minutes
    from rebuilt
    group by game_id, team_id
),

checked as (
    select
        t.game_id,
        t.team_id,
        t.pts = case when t.is_home then g.home_score else g.away_score end as points_match,
        t.minutes as team_minutes
    from team_points as t
    inner join {{ ref('stg_espn_games') }} as g using (game_id)
)

select
    rebuilt.*,
    rebuilt.game_id || '-' || rebuilt.player_id as player_game_id,
    checked.points_match,
    checked.team_minutes,
    checked.points_match and rebuilt.lineup_consistent as usable
from rebuilt
inner join checked using (game_id, team_id)
