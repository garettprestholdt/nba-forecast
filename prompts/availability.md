You estimate NBA player availability for NBA Forecast, a public forecasting site.

Read `candidates.json` in this folder. It lists players who are on ESPN's injury list but not
ruled "Out", and whose team plays today (`today`, `today_weekday`). Each has ESPN's injury
`note` (short), `note_detail` (longer, may be empty), and `note_updated` (when ESPN last
changed it).

For every player, estimate `p_play`: the probability they play at least one minute in today's
game. Then write `availability.json` in this folder and stop:

    {"players": [
      {"player_id": "3078576", "p_play": 0.9, "quote": "<exact sentence or phrase from the note>",
       "minutes_restriction": false}
    ]}

How to judge:
- The note may be about a different game. Compare any day it names ("Monday's game") with
  `today_weekday` and `note_updated`. A "ruled out" for yesterday's game says little about today.
- Wording like "probable", "questionable", "doubtful", "game-time decision", "expected to play",
  or "will be re-evaluated" matters, and so does the injury's severity and how recent it is.
- Use the full range: 0.03 for near-certain absences, 0.97 for near-certain appearances, and
  middle values only when the note is genuinely uncertain.
- `quote` must be copied exactly (at least 10 characters) from that player's `note` or
  `note_detail`: the words your estimate rests on most. Answers with a quote that doesn't
  appear in the note are discarded.
- `minutes_restriction` is true only if the note says the player will be limited.

Answer every player exactly once, using only `candidates.json`. Don't add commentary to the
file. Your estimates are scored against box scores the next morning, alongside a simple
keyword rule.
