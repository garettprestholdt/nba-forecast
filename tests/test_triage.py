import json

import pytest

from nbaforecast.agents import triage

SUMMARY = {
    "name": "Daily predictions",
    "displayTitle": "Daily predictions",
    "conclusion": "failure",
    "event": "schedule",
    "headSha": "abcdef1234567",
    "headBranch": "main",
    "createdAt": "2026-10-28T14:00:00Z",
    "url": "https://github.com/o/r/actions/runs/1",
    "jobs": [
        {
            "name": "daily",
            "conclusion": "failure",
            "steps": [
                {"name": "Install dependencies", "conclusion": "success"},
                {"name": "Ingest results, update ratings, predict today", "conclusion": "failure"},
            ],
        }
    ],
}

RAW_LOG = (
    "daily\tIngest results\t﻿2026-10-28T14:01:02.1234567Z ##[group]Run uv run nbaf daily\n"
    "daily\tIngest results\t2026-10-28T14:01:05.0000000Z \x1b[31mKeyError: 'season'\x1b[0m\n"
)


class FakeGh:
    """Records gh/git calls and returns canned output."""

    def __init__(self, open_issues=()):
        self.calls = []
        self.open_issues = list(open_issues)

    def __call__(self, args):
        self.calls.append(args)
        if args[:3] == ["gh", "run", "view"] and "--json" in args:
            return json.dumps(SUMMARY)
        if args[:3] == ["gh", "run", "view"]:
            return RAW_LOG
        if args[:2] == ["git", "-C"]:
            return "233ea09 Merge pull request #7\n"
        if args[:3] == ["gh", "issue", "list"]:
            return json.dumps(self.open_issues)
        if args[:3] == ["gh", "issue", "create"]:
            return "https://github.com/o/r/issues/9\n"
        return ""


def test_clean_log_strips_timestamps_ansi_and_keeps_step():
    text = triage.clean_log(RAW_LOG)
    assert "Ingest results | KeyError: 'season'" in text
    assert "\x1b[" not in text and "2026-10-28T14" not in text


def test_clean_log_keeps_only_the_tail():
    raw = "\n".join(f"job\tstep\t2026-10-28T14:00:00Z line {i}" for i in range(1000))
    lines = triage.clean_log(raw).splitlines()
    assert len(lines) == triage.LOG_TAIL_LINES
    assert lines[-1] == "step | line 999"


def test_failed_steps():
    assert triage.failed_steps(SUMMARY) == ["daily / Ingest results, update ratings, predict today"]
    assert triage.failed_steps({"jobs": []}) == ["(no failed step reported)"]


def test_prepare_builds_isolated_folder(tmp_path):
    triage.prepare("1", tmp_path, run=FakeGh())
    names = {p.name for p in tmp_path.iterdir()}
    assert names == {"failure.md", "run.json", "repo", "prompt.txt"}
    report = (tmp_path / "failure.md").read_text()
    assert "KeyError: 'season'" in report and "233ea09" in report
    source = tmp_path / "repo"
    assert (source / "CLAUDE.md").exists() and (
        source / "src" / "nbaforecast" / "pipeline" / "live.py"
    ).exists()
    assert not (source / ".git").exists() and not (source / ".venv").exists()


def test_issue_with_agent_diagnosis(tmp_path):
    triage.prepare("1", tmp_path, run=FakeGh())
    (tmp_path / "diagnosis.md").write_text("## What failed\nThe ingest step.\n")
    gh = FakeGh()
    outcome = triage.file_issue(SUMMARY, tmp_path, run=gh)
    assert outcome["action"] == "opened https://github.com/o/r/issues/9"
    assert (
        outcome["title"] == "Triage: Daily predictions failed at "
        "daily / Ingest results, update ratings, predict today"
    )
    create = next(c for c in gh.calls if c[:3] == ["gh", "issue", "create"])
    body = (tmp_path / "issue_body.md").read_text()
    assert "## What failed" in body and "Diagnosed by an AI agent" in body
    assert "--label" in create


def test_repeat_failure_comments_instead_of_new_issue(tmp_path):
    triage.prepare("1", tmp_path, run=FakeGh())
    title = triage.issue_title(SUMMARY)
    gh = FakeGh(open_issues=[{"number": 4, "title": title}])
    outcome = triage.file_issue(SUMMARY, tmp_path, run=gh)
    assert outcome["action"] == "commented on #4"
    assert not any(c[:3] == ["gh", "issue", "create"] for c in gh.calls)


def test_no_diagnosis_still_files_raw_context(tmp_path):
    triage.prepare("1", tmp_path, run=FakeGh())
    outcome = triage.file_issue(SUMMARY, tmp_path, run=FakeGh())
    body = (tmp_path / "issue_body.md").read_text()
    assert outcome["agent_diagnosis"] is False
    assert "didn't produce a diagnosis" in body and "KeyError: 'season'" in body


def test_dry_run_files_nothing(tmp_path):
    triage.prepare("1", tmp_path, run=FakeGh())
    gh = FakeGh()
    outcome = triage.file_issue(SUMMARY, tmp_path, run=gh, dry_run=True)
    assert outcome["action"] == "none (dry run)" and "body" in outcome
    assert gh.calls == []


def test_body_is_capped(tmp_path):
    triage.prepare("1", tmp_path, run=FakeGh())
    (tmp_path / "diagnosis.md").write_text("x" * 100_000)
    outcome = triage.file_issue(SUMMARY, tmp_path, run=FakeGh(), dry_run=True)
    assert len(outcome["body"]) <= triage.MAX_ISSUE_CHARS


@pytest.mark.parametrize("name", ["failure.md", "prompt.txt"])
def test_prompt_and_report_never_include_tokens(tmp_path, name):
    triage.prepare("1", tmp_path, run=FakeGh())
    assert "sk-ant" not in (tmp_path / name).read_text()
