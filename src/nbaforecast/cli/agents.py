"""`nbaf` commands for agent jobs: prepare an isolated folder, then check and publish the
agent's work."""

import argparse
import os

from nbaforecast.cli.common import _github_output, _recap_date


def _recap_prepare(args: argparse.Namespace) -> int:
    from pathlib import Path

    from nbaforecast.agents import recap
    from nbaforecast.pipeline import live

    store = live.Store(Path(args.data_dir))
    prompt = recap.prepare(
        store, store.load_state(), _recap_date(args), Path(args.workdir), force=args.force
    )
    print(f"Recap prompt: {prompt}" if prompt else "Nothing to recap (or already published).")
    _github_output(ready="true" if prompt else "false")
    return 0


def _recap_publish(args: argparse.Namespace) -> int:
    import json
    from pathlib import Path

    from nbaforecast.agents import recap
    from nbaforecast.pipeline import live

    draft_path = Path(args.workdir) / "recap_draft.md"
    draft = draft_path.read_text() if draft_path.exists() else None
    outcome = recap.publish(live.Store(Path(args.data_dir)), _recap_date(args), draft, args.model)
    print(json.dumps(outcome, indent=2))
    _, body = recap.read_published(Path(outcome["path"]))
    print(f"--- published recap ({outcome['source']}) ---\n{body}")
    if step_summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(step_summary, "a") as f:
            f.write(f"### Recap\n- published: **{outcome['source']}**\n")
            for problem in outcome["problems"]:
                f.write(f"- fact-check: {problem}\n")
            f.write("\n" + "\n".join(f"> {line}" for line in body.splitlines()) + "\n")
    if args.require_agent and outcome["source"] != "agent":
        print("The agent's draft wasn't published (see problems above).")
        return 1
    return 0


def _triage_prepare(args: argparse.Namespace) -> int:
    from pathlib import Path

    from nbaforecast.agents import triage

    summary = triage.prepare(args.run_id, Path(args.workdir))
    print(f"Collected failure context for {summary.get('name')}: {summary.get('url')}")
    for step in triage.failed_steps(summary):
        print(f"  failed: {step}")
    return 0


def _triage_file(args: argparse.Namespace) -> int:
    import json
    from pathlib import Path

    from nbaforecast.agents import triage

    workdir = Path(args.workdir)
    summary = json.loads((workdir / "run.json").read_text())
    outcome = triage.file_issue(summary, workdir, dry_run=args.dry_run)
    body = outcome.pop("body", None)
    print(json.dumps(outcome, indent=2))
    if body is not None:
        print(f"--- issue that would be filed ---\n# {outcome['title']}\n\n{body}")
    if step_summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(step_summary, "a") as f:
            f.write(f"### Triage\n- {outcome['action']}\n- title: {outcome['title']}\n")
            f.write(f"- agent diagnosis: {'yes' if outcome['agent_diagnosis'] else 'no'}\n")
            if body is not None:
                f.write(f"\n---\n\n{body}\n")
    if args.require_agent and not outcome["agent_diagnosis"]:
        print("The agent didn't write a diagnosis.")
        return 1
    return 0


def _availability_prepare(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime
    from pathlib import Path

    import requests

    from nbaforecast.agents import availability
    from nbaforecast.ingest import injuries
    from nbaforecast.pipeline import live

    session = requests.Session()
    summary = availability.prepare(
        live.Store(Path(args.data_dir)),
        _recap_date(args),
        datetime.now(UTC),
        Path(args.workdir),
        fetch_injuries=lambda: injuries.fetch_injuries(session),
        fetch_summary=lambda game_id: injuries.fetch_summary(game_id, session),
    )
    print(summary)
    _github_output(ready="true" if summary["candidates"] else "false")
    return 0


def _availability_publish(args: argparse.Namespace) -> int:
    import json
    from datetime import UTC, datetime
    from pathlib import Path

    from nbaforecast.agents import availability
    from nbaforecast.pipeline import live

    out = availability.publish(
        live.Store(Path(args.data_dir)), _recap_date(args), datetime.now(UTC), Path(args.workdir)
    )
    calls = out.pop("calls")
    print(json.dumps(out, indent=2))
    table = ["| Player | Team | Keyword rule | Agent | Agent's quote |", "|---|---|---|---|---|"]
    for r in calls:
        agent = f"{float(r['agent_p']):.0%}" if r["agent_p"] != "" else "–"
        table.append(
            f"| {r['player']} | {r['team']} | {float(r['baseline_p']):.0%} | {agent} "
            f"| {r['agent_quote'] or '–'} |"
        )
    print("\n".join(table))
    if step_summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(step_summary, "a") as f:
            f.write(
                f"### Injury calls\n- logged: {out['logged']} "
                f"({out['agent_answers']} with an accepted agent answer)\n"
            )
            for problem in out["problems"]:
                f.write(f"- validation: {problem}\n")
            f.write("\n" + "\n".join(table) + "\n\n")
            h2h = out["scorecard"]["head_to_head"]
            f.write(
                f"- season so far: {h2h['calls']} scored head-to-head; agent {h2h['agent']}, "
                f"keyword rule {h2h['keyword_rule']}\n"
            )
    if args.require_agent and out["logged"] and not out["agent_answers"]:
        print("No agent answers were accepted.")
        return 1
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    for name, func, helptext in [
        ("recap-prepare", _recap_prepare, "write recap facts and the agent's prompt"),
        ("recap-publish", _recap_publish, "fact-check the agent's draft and publish the recap"),
    ]:
        rp = sub.add_parser(name, help=helptext)
        rp.add_argument("--data-dir", required=True, help="checkout of the data branch")
        rp.add_argument("--workdir", required=True, help="isolated folder the agent works in")
        rp.add_argument("--date", help="recap date (default: today, US Eastern)")
        rp.add_argument("--model", default="", help="model the agent ran with (for the record)")
        rp.add_argument(
            "--force", action="store_true", help="recap test only: ignore an existing recap"
        )
        rp.add_argument(
            "--require-agent",
            action="store_true",
            help="recap test only: fail if the template is used",
        )
        rp.set_defaults(func=func)

    for name, func, helptext in [
        ("availability-prepare", _availability_prepare, "score past injury calls; pick today's"),
        ("availability-publish", _availability_publish, "validate the agent's calls and log them"),
    ]:
        ap = sub.add_parser(name, help=helptext)
        ap.add_argument("--data-dir", required=True, help="checkout of the data branch")
        ap.add_argument("--workdir", required=True, help="isolated folder the agent works in")
        ap.add_argument("--date", help="game date (default: today, US Eastern)")
        ap.add_argument(
            "--require-agent", action="store_true", help="test only: fail without answers"
        )
        ap.set_defaults(func=func)

    tp = sub.add_parser("triage-prepare", help="collect a failed run's context for the agent")
    tp.add_argument("--run-id", required=True, help="GitHub Actions run id that failed")
    tp.add_argument("--workdir", required=True, help="isolated folder the agent works in")
    tp.set_defaults(func=_triage_prepare)

    tf = sub.add_parser("triage-file", help="file (or update) the triage issue")
    tf.add_argument("--workdir", required=True, help="folder from triage-prepare")
    tf.add_argument("--dry-run", action="store_true", help="print the issue; file nothing")
    tf.add_argument("--require-agent", action="store_true", help="fail without a diagnosis")
    tf.set_defaults(func=_triage_file)
