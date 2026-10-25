"""Where data lives.

Raw and large data (historical backfill, box scores, play-by-play) stays outside the repo and is
never pushed. Only small published outputs (the predictions log, current ratings, the scorecard)
are committed under `data/published/`, so the daily cloud run and the website can use them.
"""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PUBLISHED_DIR = REPO_ROOT / "data" / "published"

DEFAULT_LOCAL_DATA_DIR = Path.home() / "Desktop" / "nbaforecast"


def local_data_dir() -> Path:
    """Directory for raw and large data. Override with the NBAF_DATA_DIR environment variable."""
    override = os.environ.get("NBAF_DATA_DIR")
    return Path(override).expanduser() if override else DEFAULT_LOCAL_DATA_DIR
