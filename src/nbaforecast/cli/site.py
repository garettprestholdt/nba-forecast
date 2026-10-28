"""`nbaf` commands: build the static site."""

import argparse


def _site(args: argparse.Namespace) -> int:
    from pathlib import Path

    from nbaforecast import web

    written = web.build(Path(args.data_dir), Path(args.out))
    print(f"Built {len(written)} pages into {args.out}")
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    site = sub.add_parser("site", help="build the static site from the data branch")
    site.add_argument("--data-dir", required=True, help="checkout of the data branch")
    site.add_argument("--out", required=True, help="output directory")
    site.set_defaults(func=_site)
