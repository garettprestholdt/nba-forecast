"""The Play page: which Cold Case files are open and get published."""

import json
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Any

from nbaforecast.games import coldcase as cc


# --- Cold Case (the game) ----------------------------------------------------------------
def coldcase(published: Path, out_dir: Path | None, today: date) -> dict[str, Any] | None:
    """Publish the cases that are open (through tomorrow's, so the page can switch at midnight
    Central before the next build) and return what the Play page needs. Later cases are never
    copied to the site."""
    index_path = cc.bank_dir(published) / "index.json"
    if not index_path.exists():
        return None
    index = json.loads(index_path.read_text())
    last = min(cc.case_number(today) + 1, len(index["cases"]))
    if last < 1:
        return None
    if out_dir is not None:
        folder = out_dir / "coldcase"
        folder.mkdir(parents=True, exist_ok=True)
        for n in range(1, last + 1):
            shutil.copyfile(cc.case_path(published, n), folder / cc.case_path(published, n).name)
    return {"start": index["start"], "today": max(1, min(cc.case_number(today), last)),
            "cases": index["cases"][:last]}  # fmt: skip


def today_label(day: str | None) -> str:
    return datetime.fromisoformat(day).strftime("%b %-d") if day else ""
