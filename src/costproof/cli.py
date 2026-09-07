"""Command line entry point.

    costproof study     regenerate every number and figure in the README
    costproof data      generate the estate and write it to data/
    costproof check     validate FOCUS conformance of a generated estate
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]


def _banner(msg: str) -> None:
    print(f"\n\033[1m{msg}\033[0m", flush=True)


def cmd_data(args: argparse.Namespace) -> int:
    from costproof.simulate import focus
    from costproof.simulate.generator import SimConfig, generate

    cfg = SimConfig(seed=args.seed)
    _banner(f"Generating estate (seed={cfg.seed}, fingerprint={cfg.fingerprint()})")
    t0 = time.time()
    est = generate(cfg)
    print(f"  {len(est.billing):,} billing rows in {time.time() - t0:.1f}s")

    problems = focus.validate(est.billing, strict=False)
    print(f"  FOCUS 1.2 conformance: {'PASS' if not problems else 'FAIL'}")
    for p in problems:
        print(f"    - {p}")

    out = ROOT / "data"
    (out / "bronze").mkdir(parents=True, exist_ok=True)
    (out / "gold").mkdir(parents=True, exist_ok=True)
    est.billing.to_parquet(out / "bronze" / "focus_billing.parquet")
    est.resources.to_parquet(out / "gold" / "dim_resource.parquet")
    est.drivers.to_parquet(out / "gold" / "fact_business_driver.parquet")
    est.interventions_frame().to_parquet(out / "gold" / "ground_truth_interventions.parquet")
    est.waste_frame().to_parquet(out / "gold" / "ground_truth_waste.parquet")
    print(f"  written to {out}")
    return 0


def cmd_study(args: argparse.Namespace) -> int:
    from costproof.causal import panel as P
    from costproof.causal import did as D
    from costproof.causal import validate as V
    from costproof.causal.synth import synthetic_control_detail
    from costproof.report import figures as F
    from costproof.simulate.generator import SimConfig, generate

    cfg = SimConfig(seed=args.seed)
    _banner(f"1/3  Generating estate (fingerprint={cfg.fingerprint()})")
    est = generate(cfg)
    usage = est.billing[est.billing["ChargeCategory"] == "Usage"]
    days = usage["ChargePeriodStart"].nunique()
    annual = usage["EffectiveCost"].sum() / days * 365
    print(f"  {len(est.resources)} resources | {len(est.billing):,} rows | "
          f"${annual / 1e6:.1f}M annualised")
    print(f"  {len(est.interventions)} interventions "
          f"({sum(i.is_null for i in est.interventions)} with a true effect of zero)")

    _banner("2/3  Estimating (4 methods x every intervention, with permutation inference)")
    t0 = time.time()
    results = V.run_study(est, verbose=True)
    print(f"  {len(results)} estimates in {time.time() - t0:.0f}s")

    tables = ROOT / "outputs" / "tables"
    figs = ROOT / "outputs" / "figures"
    tables.mkdir(parents=True, exist_ok=True)
    figs.mkdir(parents=True, exist_ok=True)

    results.to_parquet(tables / "study_results.parquet")
    score = V.score(results)
    score.to_csv(tables / "scorecard.csv", index=False)
    dollars = V.dollar_impact(est, results)
    dollars.to_csv(tables / "dollar_impact.csv", index=False)

    _banner("3/3  Figures")
    F.estimator_scorecard_plot(score, figs / "scorecard.png")
    F.error_distribution_plot(results, figs / "error_distribution.png")

    costs = P.daily_resource_costs(est.billing)
    treated_all = {iv.resource_id for iv in est.interventions}
    for iv in est.interventions:
        if iv.is_null:
            continue
        donors = P.select_donors(costs, iv.resource_id, iv.start_date, excluded=treated_all)
        if len(donors) < 25:
            continue
        pan = P.build_panel(costs, iv.resource_id, iv.start_date, donors)
        if D.parallel_trends_test(pan)["passed"]:
            F.event_study_plot(D.event_study(pan), figs / "event_study.png",
                               f"Event study: {iv.action} on {iv.resource_id}")
            F.synthetic_control_plot(synthetic_control_detail(pan),
                                     figs / "synthetic_control.png",
                                     f"Synthetic control: {iv.resource_id}")
            break
    print(f"  written to {figs}")

    pd.set_option("display.width", 200)
    _banner("SCORECARD (against known ground truth)")
    print(score[["method", "n", "bias", "rmse", "coverage_95",
                 "size_false_positive", "power"]].round(4).to_string(index=False))

    d = results[results["method"] == "did_permutation"]
    ok, bad = d[d["pt_passed"] == True], d[d["pt_passed"] == False]  # noqa: E712
    _banner("PARALLEL-TRENDS FILTER")
    print(f"  pass rate {d['pt_passed'].mean():.1%}")
    if len(ok):
        import numpy as np
        print(f"  passing (n={len(ok)}): rmse {np.sqrt((ok['error'] ** 2).mean()):.4f} "
              f"coverage {ok['covers'].mean():.1%}")
    if len(bad):
        import numpy as np
        print(f"  failing (n={len(bad)}): rmse {np.sqrt((bad['error'] ** 2).mean()):.4f} "
              f"coverage {bad['covers'].mean():.1%}")

    _banner("ANNUALISED DOLLAR MISSTATEMENT")
    for r in dollars.itertuples(index=False):
        print(f"  {r.method:22s} true ${r.true_annual_savings:>11,.0f} | "
              f"claimed ${r.claimed_annual_savings:>11,.0f} | "
              f"net ${r.net_misstatement:>+11,.0f}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    from costproof.simulate import focus
    from costproof.simulate.generator import SimConfig, generate

    est = generate(SimConfig(seed=args.seed))
    problems = focus.validate(est.billing, strict=False)
    if problems:
        print("FOCUS conformance FAILED:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"FOCUS 1.2 conformance PASS ({len(est.billing):,} rows, "
          f"{len(focus.ALL_COLUMNS)} columns)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="costproof",
        description="The counterfactual layer for cloud and AI spend.",
    )
    parser.add_argument("--seed", type=int, default=20260907)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("data", help="generate the estate and write it to data/").set_defaults(
        func=cmd_data)
    sub.add_parser("study", help="run the full validation study and regenerate figures"
                   ).set_defaults(func=cmd_study)
    sub.add_parser("check", help="validate FOCUS conformance").set_defaults(func=cmd_check)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
