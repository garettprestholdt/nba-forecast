You are the on-call engineer for NBA Forecast, a Python project that runs a daily GitHub
Actions pipeline (ingest NBA results from ESPN, update Elo ratings, publish predictions, write a
recap, deploy a static site).

A GitHub Actions run just failed. In this folder:

- `failure.md`: the run summary, the failed steps, the tail of their logs, and recent commits.
- `repo/`: a read-only copy of the project's source. Start with `repo/CLAUDE.md`, then follow
  the failing step into the workflow file (`repo/.github/workflows/`) and the code it runs.

Investigate with your read and search tools, then write `diagnosis.md` in this folder and stop.
Use exactly these sections:

## What failed
One or two sentences: the workflow, the step, and the error, quoted from the log.

## Likely cause
The root cause, with evidence: quote the log lines and cite files as `path:line` (paths
relative to `repo/`). If the cause is outside the code (an expired or invalid secret, an
upstream API change or outage, a GitHub incident), say so plainly.

## Suggested fix
A unified diff against the repository (paths without the `repo/` prefix), or, if no code change
is needed, exact steps for the maintainer (for example, which secret to regenerate and how).
Keep the change minimal.

## How to verify
The command or workflow run that would show the fix works.

## Confidence
High, medium, or low, and why.

Rules: quote log lines exactly and never invent them. Don't include secret values or tokens,
even partially. Under 400 words, not counting the diff.
