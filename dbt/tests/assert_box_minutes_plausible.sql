-- 240 team minutes in regulation, 25 more per overtime; ESPN rounds each player's minutes, so
-- allow a few either way. Games with unknown overtime only get the lower bound, and games listed
-- in seeds/known_data_issues.csv (a player's line missing at ESPN) skip it.
select t.game_id, t.team_id, t.minutes, g.overtime_periods
from {{ ref('fct_team_games') }} as t
inner join {{ ref('fct_games') }} as g using (game_id)
where (t.minutes < 233 and g.known_issue is null)
   or (g.overtime_periods is not null and t.minutes > 247 + 25 * g.overtime_periods)
