-- Rates are proportions, and efficiency and pace land in a believable range. Pace is
-- possessions per 48 minutes, so overtime games aren't flagged for being long.
select game_id, team_id
from {{ ref('fct_team_games') }}
where efg_pct not between 0 and 1
   or tov_rate not between 0 and 1
   or oreb_rate not between 0 and 1
   or off_rating not between 60 and 180
   or possessions * 240 / minutes not between 70 and 140
