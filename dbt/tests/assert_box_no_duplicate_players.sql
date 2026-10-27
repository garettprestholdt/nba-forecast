-- A name listed twice in one team's box score means ESPN used two ids for one player (as in
-- 2018-20). Add the extra id to seeds/player_id_aliases.csv.
select game_id, team_id, player, count(*) as rows
from {{ ref('stg_espn_player_games') }}
where player != ''
group by game_id, team_id, player
having count(*) > 1
