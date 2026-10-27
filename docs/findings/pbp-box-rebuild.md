# Finding: blank box scores can be rebuilt from play-by-play, and checked

*Phase 6 data work. Code: `ingest/playbyplay.py`; validation: dbt model `fct_pbp_validation`
and test `assert_pbp_rebuild_accurate`. Reproduce with `uv run nbaf pbp`, then
`uv run nbaf dbt build`.*

## Problem
For about 13% of games from 2012-13 to 2017-18 (1,025 games, plus 6 in 2020-21), ESPN's box score
lists who played and who started but leaves every stat blank. The same games' play-by-play is
there. A player model trained on 2012-18 would otherwise lose one game in eight.

## Method
- **Counting stats** come straight from the plays: the shooter is the first participant; on a make
  the second is the assister, on a miss the blocker; on a turnover the second is the stealer.
  Team rebounds and turnovers have no player and are skipped. Blocking fouls ("Shooting Block")
  are personal fouls, technicals aren't, and a double personal foul charges both players.
- **Minutes** come from lineup tracking: ESPN's box score gives first-quarter starters; later
  quarters start with whoever acts or is subbed out before being subbed in. Players who play a whole
  quarter without appearing in the plays are carried over from the end of the previous quarter.
- **Plus-minus** credits each scoring play to the lineups on the floor. Two details mattered:
  - Points come from the scoring play itself, not ESPN's running score, which often updates a
    play late. With the running score, only 40% of plus-minus values matched; with play-based
    points, 96% did.
  - Substitutions at the same moment of game clock go after the other plays, so free throws
    count for the lineup on the floor when the foul was called (the NBA's convention). Without
    that, the 96% fell back to 40%.

## ESPN's play-by-play isn't always complete
The plays occasionally drop a made basket or a substitution. In 2015-16 they're also often out of
order. Missing plays can't be recovered, so every rebuilt team box score has to pass two checks
before it's used:
1. **Points match:** the rebuilt points add up to the official final score.
2. **Lineup consistent:** exactly five players are on the floor at every basket, and nobody is
   subbed out who wasn't on the floor.

A blank team box score that fails either check stays `stats_missing`, never guessed.

## Validation (1,050 games that have both, 19,356 player-games)
Of each season's games with a real ESPN box score, 150 were sampled at random (plus 2020-21).
Each was rebuilt the same way and compared player by player. On team-games that pass both checks:

| Stat | Exact | Within 1 |
|---|---|---|
| Points | 99.93% | 99.93% |
| FGM / FGA | 99.95% / 99.39% | 100% / 99.99% |
| 3PM / 3PA | 99.99% / 99.75% | 100% / 100% |
| FTM / FTA | 99.98% / 99.95% | 99.98% / 99.98% |
| Off. / def. rebounds | 99.47% / 99.11% | 100% / 99.96% |
| Assists | 99.41% | 99.97% |
| Steals / blocks | 98.85% / 99.54% | 99.98% / 100% |
| Turnovers / fouls | 99.24% / 99.56% | 99.99% / 100% |
| Minutes | 98.54% | 99.96% |
| Plus-minus | 95.70% | 97.38% (99.33% within 2) |

Minutes miss by one at most because ESPN rounds to the minute and the clock is tracked to the
second. `assert_pbp_rebuild_accurate` fails the build if any of these drops below its threshold.

## Result
| Season | Blank team box scores | Rebuilt | Games with full stats, before → after |
|---|---|---|---|
| 2012-13 | 356 | 257 | 86.5% → 94.0% |
| 2013-14 | 336 | 333 | 87.3% → 99.8% |
| 2014-15 | 356 | 349 | 86.4% → 99.5% |
| 2015-16 | 322 | 182 | 87.8% → 93.6% |
| 2016-17 | 336 | 329 | 87.2% → 99.6% |
| 2017-18 | 344 | 315 | 86.9% → 98.2% |
| 2020-21 | 12 | 12 | 99.5% → 100% |

**1,777 of 2,062 blank team box scores (86%) rebuilt.** Season by season, the share that passes
the checks is about the same for blank games as for validation games. So the blank games'
play-by-play is no worse than the rest, and the validation sample stands in for them fairly. The
285 left are mostly 2015-16 and 2012-13, where the play-by-play itself is missing plays.

## Caveats
- Rebuilt rows are marked `stats_source = 'pbp'`, so any analysis can leave them out.
- The checks catch missing points and broken lineups, but a missing *missed* shot or rebound
  leaves points intact. That's why the validation measures every stat, not just points.
