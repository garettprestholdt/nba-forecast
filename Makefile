# One command per stage. `make help` lists them; docs/reproduce.md explains each one.
# Local data goes to $NBAF_DATA_DIR (default ~/Desktop/nbaforecast), never into the repo.

# checkout of the `data` branch, for `make site`
DATA ?= .data-branch
SITE ?= _site
PLAY_SEASONS ?= 2017-18 2018-19 2019-20 2020-21 2021-22 2022-23 2023-24 2024-25 2025-26

.PHONY: help setup check lint test data plays backtests models site reproduce clean-site

help:  ## list targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-12s %s\n", $$1, $$2}'

setup:  ## install pinned dependencies and the git hooks
	uv sync --locked
	uv run pre-commit install

check: lint test  ## everything CI runs on a pull request (minus dbt on the fixture)

lint:  ## ruff lint + format check
	uv run ruff check .
	uv run ruff format --check .

test:  ## unit tests (no network)
	uv run pytest -q

data:  ## games, box scores, play-by-play rebuilds, then the dbt warehouse (~4 h first time)
	uv run nbaf backfill
	uv run nbaf boxscores
	uv run nbaf pbp
	uv run nbaf dbt build

plays:  ## full play-by-play for the RAPM backtest seasons (~5 min per season)
	uv run nbaf plays --seasons $(PLAY_SEASONS)

backtests:  ## every model comparison in docs/findings (writes data/published/)
	uv run nbaf backtest
	uv run nbaf challenger
	uv run nbaf hybrid
	uv run nbaf impact-backtest

models:  ## the model files and season inputs the daily job reads (writes data/published/)
	uv run nbaf hybrid-fit
	uv run nbaf hybrid-v2-fit
	uv run nbaf season-start
	uv run nbaf coldcase-bank

site: $(DATA)  ## build the public site into _site/ from a data-branch checkout
	uv run nbaf site --data-dir $(DATA) --out $(SITE)

$(DATA):
	git fetch origin data
	git worktree add --detach $(DATA) origin/data

reproduce: setup data plays backtests models check  ## everything, from a clean machine

clean-site:  ## remove the local site build and data-branch checkout
	rm -rf $(SITE)
	-git worktree remove --force $(DATA)
