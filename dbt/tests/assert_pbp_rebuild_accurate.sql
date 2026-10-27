-- The play-by-play rebuild is trusted only because it reproduces real box scores. On validation
-- games (ESPN box score present, rebuild usable), each counting stat must match exactly for at
-- least 98.5% of player-games, minutes must be within 1 for 99.5% (ESPN rounds; we track the
-- clock to the second), and plus-minus within 2 for 98%. Thresholds sit just under what the
-- full validation achieves (docs/findings/pbp-box-rebuild.md), so a regression fails the build.
-- Rates need a real sample, so a stat is only checked with 500+ player-games (not on the CI
-- fixture's three games).
with rates as (
    select
        stat,
        count(*) as player_games,
        avg(case when error = 0 then 1 else 0 end) as exact,
        avg(case when abs(error) <= 1 then 1 else 0 end) as within_1,
        avg(case when abs(error) <= 2 then 1 else 0 end) as within_2
    from {{ ref('fct_pbp_validation') }}
    where usable and error is not null
    group by stat
)

select *
from rates
where player_games >= 500
  and case stat
        when 'minutes' then within_1 < 0.995
        when 'plus_minus' then within_2 < 0.98
        else exact < 0.985
  end
