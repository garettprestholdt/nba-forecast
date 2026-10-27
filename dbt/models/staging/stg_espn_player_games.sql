-- One row per player per game from ESPN box scores, typed and cleaned:
--   * Duplicate ESPN ids (seeds/player_id_aliases.csv) map to the player's main id. In 2018-20
--     ESPN sometimes listed a player twice under both ids; the row with minutes is kept. The
--     seed also maps stand-in ids for players ESPN listed without one (`noid-...`).
--   * Some team box scores have the roster but no stats (every line blank, mostly 2012-18).
--     Those rows keep who started and who sat, with stats null instead of fake zeros.

with raw as (
    select
        game_id::varchar as game_id,
        coalesce(aliases.player_id::varchar, box.player_id::varchar) as player_id,
        season::varchar as season,
        game_date::date as game_date,
        team_id::varchar as team_id,
        opponent_id::varchar as opponent_id,
        is_home::boolean as is_home,
        coalesce(aliases.player, box.player::varchar) as player,
        position::varchar as position,
        starter::boolean as is_starter,
        did_not_play::boolean as did_not_play,
        dnp_reason::varchar as dnp_reason,
        ejected::boolean as was_ejected,
        minutes::integer as minutes,
        pts::integer as pts,
        fgm::integer as fgm,
        fga::integer as fga,
        fg3m::integer as fg3m,
        fg3a::integer as fg3a,
        ftm::integer as ftm,
        fta::integer as fta,
        oreb::integer as oreb,
        dreb::integer as dreb,
        reb::integer as reb,
        ast::integer as ast,
        stl::integer as stl,
        blk::integer as blk,
        tov::integer as tov,
        pf::integer as pf,
        plus_minus::integer as plus_minus
    from {{ source('raw', 'espn_boxscores') }} as box
    left join (
        select alias_id::varchar as alias_id, player_id, player from {{ ref('player_id_aliases') }}
    ) as aliases on box.player_id::varchar = aliases.alias_id
),

deduped as (
    select *
    from raw
    qualify row_number() over (
        partition by game_id, player_id order by minutes desc, pts desc
    ) = 1
),

flagged as (
    select
        *,
        max(minutes + pts + fga + reb + ast) over (partition by game_id, team_id) = 0
            as stats_missing
    from deduped
)

select
    game_id,
    player_id,
    game_id || '-' || player_id as player_game_id,
    season,
    game_date,
    team_id,
    opponent_id,
    is_home,
    player,
    position,
    is_starter,
    did_not_play,
    dnp_reason,
    was_ejected,
    stats_missing,
    {% for col in ['minutes', 'pts', 'fgm', 'fga', 'fg3m', 'fg3a', 'ftm', 'fta', 'oreb', 'dreb',
                   'reb', 'ast', 'stl', 'blk', 'tov', 'pf', 'plus_minus'] -%}
    case when not stats_missing then {{ col }} end as {{ col }}{{ ',' if not loop.last }}
    {% endfor %}
from flagged
