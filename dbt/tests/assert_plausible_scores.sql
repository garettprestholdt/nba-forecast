-- Scores and period counts inside what has actually happened in the NBA since 2004
-- (lowest team score ~55, highest ~180 in overtime; the longest game had 4 overtimes).
select game_id, home_score, away_score, overtime_periods
from {{ ref('fct_games') }}
where home_score not between 50 and 200
   or away_score not between 50 and 200
   or overtime_periods not between 0 and 6
