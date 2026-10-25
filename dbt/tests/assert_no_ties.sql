-- NBA games can't end tied. A tie means a bad score.
select game_id, home_score, away_score
from {{ ref('fct_games') }}
where home_score = away_score
