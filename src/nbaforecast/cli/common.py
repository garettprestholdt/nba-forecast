"""Helpers shared by the `nbaf` command modules."""

import argparse
import os

from nbaforecast import config


def _today_eastern():
    from datetime import datetime

    from nbaforecast.ingest.espn import EASTERN

    return datetime.now(EASTERN).date()


def _code_sha() -> str:
    import subprocess

    return (
        os.environ.get("GITHUB_SHA")
        or subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=config.REPO_ROOT
        ).stdout.strip()
    )[:12]


def _recap_date(args: argparse.Namespace):
    from datetime import date

    return date.fromisoformat(args.date) if args.date else _today_eastern()


def _github_output(**values: str) -> None:
    if path := os.environ.get("GITHUB_OUTPUT"):
        with open(path, "a") as f:
            for key, value in values.items():
                f.write(f"{key}={value}\n")
