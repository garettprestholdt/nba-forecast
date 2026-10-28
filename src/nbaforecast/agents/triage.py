"""Failure triage: when a workflow fails, an AI agent diagnoses it and a GitHub issue is filed.

1. `prepare` (our code) collects the failed run's summary, the failed steps' log tail, and recent
   commits into `failure.md`, plus a read-only copy of the source (no .git, no credentials),
   in an isolated folder.
2. Claude Code (headless) reads them, investigates the code, and writes `diagnosis.md`.
   See `prompts/triage.md`.
3. `file_issue` (our code) posts the diagnosis as an issue, or comments on the open issue for
   the same failing step. The agent never touches GitHub itself.
"""

import json
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from nbaforecast import config

PROMPT_TEMPLATE = config.REPO_ROOT / "prompts" / "triage.md"
LABEL = "triage"
LOG_TAIL_LINES = 300
MAX_LOG_CHARS = 20_000
MAX_ISSUE_CHARS = 60_000  # GitHub's limit is 65,536

# Copied for the agent to read. Everything else (git metadata, caches, local data) is left out.
SOURCE_IGNORE = shutil.ignore_patterns(
    ".git", ".venv", "__pycache__", ".ruff_cache", ".pytest_cache", "node_modules", "target",
    "logs", "_site", ".claude", "*.duckdb", ".env", ".env.*",
)  # fmt: skip

ANSI = re.compile(r"\x1b\[[0-9;]*m|\^\[\[[0-9;]*m")
TIMESTAMP = re.compile(r"^﻿?\d{4}-\d\d-\d\dT[\d:.]+Z ?")

Runner = Callable[[list[str]], str]


def run_command(args: list[str]) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def clean_log(raw: str) -> str:
    """`gh run view --log-failed` output -> 'step | message' lines, tail only, no ANSI codes."""
    lines = []
    for line in raw.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3:
            _, step, rest = parts
            line = f"{step} | {TIMESTAMP.sub('', rest)}"
        lines.append(ANSI.sub("", line).rstrip())
    text = "\n".join(lines[-LOG_TAIL_LINES:])
    return text[-MAX_LOG_CHARS:]


def failed_steps(summary: dict) -> list[str]:
    return [
        f"{job['name']} / {step['name']}"
        for job in summary.get("jobs", [])
        if job.get("conclusion") == "failure"
        for step in job.get("steps", [])
        if step.get("conclusion") == "failure"
    ] or ["(no failed step reported)"]


def failure_report(summary: dict, log: str, commits: str) -> str:
    steps = "\n".join(f"- {s}" for s in failed_steps(summary))
    sha = summary.get("headSha", "")[:7]
    return f"""# Failed run

- Workflow: {summary.get("name")}
- Run: {summary.get("url")}
- Trigger: {summary.get("event")} on branch `{summary.get("headBranch")}` at commit `{sha}`
- Started: {summary.get("createdAt")}
- Title: {summary.get("displayTitle")}

## Failed steps
{steps}

## Log tail of the failed steps (last {LOG_TAIL_LINES} lines; 'step | message')
```
{log}
```

## Recent commits on the default branch
```
{commits}
```
"""


def prepare(run_id: str, workdir: Path, run: Runner = run_command) -> dict:
    """Collect the failure context and a source copy into `workdir`. Returns the run summary."""
    summary = json.loads(
        run(
            ["gh", "run", "view", run_id, "--json",
             "name,displayTitle,conclusion,event,headSha,headBranch,createdAt,url,jobs"]
        )
    )  # fmt: skip
    log = clean_log(run(["gh", "run", "view", run_id, "--log-failed"]))
    commits = run(["git", "-C", str(config.REPO_ROOT), "log", "--oneline", "-10"]).strip()

    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "failure.md").write_text(failure_report(summary, log, commits))
    (workdir / "run.json").write_text(json.dumps(summary, indent=2))
    source = workdir / "repo"
    if source.exists():
        shutil.rmtree(source)
    shutil.copytree(config.REPO_ROOT, source, ignore=SOURCE_IGNORE)
    (workdir / "prompt.txt").write_text(PROMPT_TEMPLATE.read_text())
    return summary


def issue_title(summary: dict) -> str:
    return f"Triage: {summary.get('name')} failed at {failed_steps(summary)[0]}"


def issue_body(summary: dict, diagnosis: str | None, failure_md: str) -> str:
    if diagnosis and diagnosis.strip():
        analysis = diagnosis.strip()
        note = (
            "_Diagnosed by an AI agent (Claude Code) from the run's logs and a read-only copy of "
            "the code. Review it before acting. To apply the fix, ask Claude Code to work on "
            "this issue, which opens a normal PR._"
        )
    else:
        analysis = "The triage agent didn't produce a diagnosis; the raw failure context is below."
        note = "_Agent diagnosis unavailable._"
    body = f"""**Run:** {summary.get("url")}

{analysis}

{note}

<details><summary>Failure context given to the agent</summary>

{failure_md}
</details>
"""
    return body[:MAX_ISSUE_CHARS]


def open_issue_for(title: str, run: Runner = run_command) -> int | None:
    """The open triage issue with this exact title, if any (repeat failures get a comment)."""
    issues = json.loads(
        run(["gh", "issue", "list", "--label", LABEL, "--state", "open", "--json", "number,title"])
    )
    return next((i["number"] for i in issues if i["title"] == title), None)


def file_issue(
    summary: dict, workdir: Path, run: Runner = run_command, dry_run: bool = False
) -> dict:
    diagnosis_path = workdir / "diagnosis.md"
    diagnosis = diagnosis_path.read_text() if diagnosis_path.exists() else None
    title = issue_title(summary)
    body = issue_body(summary, diagnosis, (workdir / "failure.md").read_text())
    outcome = {"title": title, "agent_diagnosis": diagnosis is not None, "dry_run": dry_run}
    if dry_run:
        return {**outcome, "action": "none (dry run)", "body": body}

    run(["gh", "label", "create", LABEL, "--color", "d93f0b", "--force",
         "--description", "Failed workflow run, diagnosed by the triage agent"])  # fmt: skip
    body_file = workdir / "issue_body.md"
    body_file.write_text(body)
    existing = open_issue_for(title, run)
    if existing is not None:
        run(["gh", "issue", "comment", str(existing), "--body-file", str(body_file)])
        return {**outcome, "action": f"commented on #{existing}"}
    url = run(["gh", "issue", "create", "--title", title, "--label", LABEL,
               "--body-file", str(body_file)]).strip()  # fmt: skip
    return {**outcome, "action": f"opened {url}"}
