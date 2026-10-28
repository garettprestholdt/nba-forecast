"""`nbaf` commands: backtests and model fits: they choose and justify the models (run locally)."""

import argparse
import logging

from nbaforecast import config


def _backtest(args: argparse.Namespace) -> int:
    from nbaforecast import warehouse
    from nbaforecast.research import backtest

    games = warehouse.load_games()
    print(f"Backtesting {len(backtest.DEFAULT_GRID)} Elo configurations on {len(games):,} games")
    report = backtest.run(games)
    out_dir = config.PUBLISHED_DIR / "backtest"
    backtest.write(report, out_dir)
    # Full per-game predictions stay local (raw-sized data never goes in git).
    local = config.local_data_dir() / "backtest"
    local.mkdir(parents=True, exist_ok=True)
    report.predictions.to_parquet(local / "elo_predictions.parquet", index=False)
    print(f"Chosen: {report.champion.label()}")
    print(report.holdout.to_string(index=False, float_format=lambda v: f"{v:.4f}"))
    print(f"Wrote {out_dir} and {local / 'elo_predictions.parquet'}")
    return 0


def _challenger(args: argparse.Namespace) -> int:
    from nbaforecast import warehouse
    from nbaforecast.research import challenger

    games = warehouse.load_games()
    print(f"Tuning the Kalman challenger on {len(games):,} games (two-stage grid, train seasons)")
    result = challenger.compare(games)
    out_dir = config.PUBLISHED_DIR / "challenger"
    challenger.write(result, out_dir)
    print(f"Chosen: {result['params'].label()}")
    print(result["holdout"].to_string(index=False, float_format=lambda v: f"{v:.4f}"))
    for name, paired in result["paired"].items():
        lo, hi = paired["ci95"]
        print(
            f"Paired log-loss, {name}: {paired['mean_difference']:+.4f} "
            f"(95% CI {lo:+.4f} to {hi:+.4f})"
        )
    print("Learned coefficients now:", {k: round(v, 2) for k, v in result["coefficients"].items()})
    print(f"Wrote {out_dir}")
    return 0


def _hybrid(args: argparse.Namespace) -> int:
    from nbaforecast import warehouse
    from nbaforecast.research import hybrid_backtest

    inputs = hybrid_backtest.Inputs(
        warehouse.load_games(), warehouse.load_team_games(), warehouse.load_player_games()
    )
    print("Tuning the hybrid on training seasons (each player setting is a ~30 s walk)")
    best, trials = hybrid_backtest.tune(inputs)
    print(f"Chosen: {best['fit'].label()}; {best['players'].label()}; form={best['form_decay']}")
    result = hybrid_backtest.compare(inputs, best)
    out_dir = config.PUBLISHED_DIR / "hybrid"
    hybrid_backtest.write(result, trials, out_dir)
    cols = ["model", "train_log_loss", "log_loss", "brier", "accuracy", "reliability"]
    print(result["holdout"][cols].to_string(index=False, float_format=lambda v: f"{v:.4f}"))
    for name, diff in result["paired"].items():
        lo, hi = diff["ci95"]
        print(f"{name}: {diff['mean_difference']:+.4f} (95% CI {lo:+.4f} to {hi:+.4f})")
    print(result["absences"].to_string(index=False, float_format=lambda v: f"{v:.4f}"))
    print(f"Wrote {out_dir}")
    return 0


def _hybrid_fit(args: argparse.Namespace) -> int:
    import json

    from nbaforecast import warehouse
    from nbaforecast.pipeline import shadow

    chosen = json.loads((config.PUBLISHED_DIR / "hybrid" / "summary.json").read_text())["chosen"]
    model = shadow.fit_model(
        warehouse.load_games(), warehouse.load_team_games(), warehouse.load_player_games(),
        chosen, args.last_season,
    )  # fmt: skip
    shadow.MODEL_PATH.write_text(json.dumps(model.to_dict(), indent=2) + "\n")
    print(f"Wrote {shadow.MODEL_PATH}: fit on {model.meta['fit_seasons']}")
    print(json.dumps(model.fit.coefficients(), indent=2))
    return 0


def _impact_backtest(args: argparse.Namespace) -> int:

    from nbaforecast import config, warehouse
    from nbaforecast.research import impact_backtest

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    games = warehouse.load_games()
    feats = impact_backtest.build(
        games, warehouse.load_team_games(), warehouse.load_player_games(),
        warehouse.load_box_rows("2016-17"), config.local_data_dir(),
    )  # fmt: skip
    result = impact_backtest.run(feats)
    impact_backtest.write(result)
    cols = ["model", "train_from", "features", "in_sample_log_loss", "log_loss", "brier",
            "reliability"]  # fmt: skip
    print(result["models"][cols].to_string(index=False))
    print(f"Wrote {impact_backtest.OUT_DIR}")
    return 0


def _hybrid_v2_fit(args: argparse.Namespace) -> int:
    import json

    from nbaforecast import config, warehouse
    from nbaforecast.pipeline import shadow_v2
    from nbaforecast.research import impact_backtest

    logging.basicConfig(level=logging.WARNING)
    feats = impact_backtest.build(
        warehouse.load_games(), warehouse.load_team_games(), warehouse.load_player_games(),
        warehouse.load_box_rows("2016-17"), config.local_data_dir(),
    )  # fmt: skip
    model = shadow_v2.fit_model(feats, impact_backtest.chosen())
    shadow_v2.MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    shadow_v2.MODEL_PATH.write_text(json.dumps(model, indent=1) + "\n")
    print(f"Wrote {shadow_v2.MODEL_PATH} (fit through {model['fit_through']})")
    scores = impact_backtest.chosen_holdout(feats)
    (shadow_v2.MODEL_PATH.parent / "backtest.json").write_text(json.dumps(scores, indent=1) + "\n")
    for row in scores["holdout"]:
        print(f"  held out: {row['model']:40s} log-loss {row['log_loss']:.4f}")
    for k, v in model["coefficients"].items():
        print(f"  {k:20s} {v:+.4f}")
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    hy = sub.add_parser("hybrid", help="backtest the team + player hybrid model (~15 min)")
    hy.set_defaults(func=_hybrid)

    bt = sub.add_parser("backtest", help="tune Elo on 2005-2019, score it on 2020-2026")
    bt.set_defaults(func=_backtest)

    ch = sub.add_parser("challenger", help="tune the Kalman model and compare it with Elo")
    ch.set_defaults(func=_challenger)

    hf = sub.add_parser("hybrid-fit", help="fit the live hybrid on every completed season")
    hf.add_argument("--last-season", default="2025-26", help="last completed season to fit on")
    hf.set_defaults(func=_hybrid_fit)

    ib = sub.add_parser(
        "impact-backtest", help="challenger with RAPM + availability: walk-forward and diagnostics"
    )
    ib.set_defaults(func=_impact_backtest)

    hv = sub.add_parser("hybrid-v2-fit", help="fit hybrid v2 (RAPM + who's missing) (local)")
    hv.set_defaults(func=_hybrid_v2_fit)
