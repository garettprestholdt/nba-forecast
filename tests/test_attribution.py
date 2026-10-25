from nbaforecast.attribution import violations


def record(message, author="Garett <me@example.com>", committer="Garett <me@example.com>"):
    return f"abcdef1234\x1f{author}\x1f{committer}\x1f{message}\x1e"


def test_clean_history_passes():
    log = record("Phase 1: backfill\n\nMentions CLAUDE.md and Claude's agent rules.")
    assert violations(log) == []


def test_flags_claude_coauthor_trailer():
    log = record("Fix\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>")
    (problem,) = violations(log)
    assert "co-author" in problem


def test_flags_case_insensitive_and_anthropic():
    log = record("Fix\n\nco-authored-by: Some Bot <bot@anthropic.com>")
    assert len(violations(log)) == 1


def test_other_coauthors_are_fine():
    assert violations(record("Pair work\n\nCo-authored-by: Friend <f@example.com>")) == []


def test_flags_bot_author_and_committer():
    log = record("Recap", author="claude[bot] <x@users.noreply.github.com>")
    log += record("Fix", committer="Claude <noreply@anthropic.com>")
    problems = violations(log)
    assert len(problems) == 2
    assert "author" in problems[0] and "committer" in problems[1]
