{{ config(enabled=var('check_season_counts', true)) }}

-- At least 99% of each season's games must have a box score (a few may be missing at ESPN).
with coverage as (
    select
        g.season,
        count(*) as games,
        count(*) filter (where b.game_id is not null) as with_box
    from {{ ref('fct_games') }} as g
    left join (select distinct game_id from {{ ref('fct_player_games') }}) as b using (game_id)
    group by g.season
)

select * from coverage where with_box < 0.99 * games
