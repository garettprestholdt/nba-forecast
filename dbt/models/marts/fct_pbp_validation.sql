-- How well the play-by-play rebuild reproduces real box scores: for every validation game (one
-- that has ESPN's box score and was also rebuilt), one row per player per stat with both values.
-- usable marks team-games whose rebuild would be used in place of a blank box score (points
-- add up to the final score and lineups were tracked consistently).

with box as (
    select * from {{ ref('stg_espn_player_games') }}
    where stats_source = 'box'
),

pbp as (
    select * from {{ ref('stg_espn_pbp_player_games') }}
)

{% for col in box_stat_columns() %}
select
    box.game_id,
    box.season,
    box.team_id,
    box.player_id,
    box.player,
    pbp.points_match,
    pbp.usable,
    '{{ col }}' as stat,
    box.{{ col }} as box_value,
    pbp.{{ col }} as pbp_value,
    pbp.{{ col }} - box.{{ col }} as error
from box
inner join pbp using (game_id, player_id)
where box.minutes > 0 or pbp.minutes > 0
{% if not loop.last %}union all{% endif %}
{% endfor %}
