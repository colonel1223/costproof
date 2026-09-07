"""Score causal estimators against known ground truth.

This module is the reason CostProof is more than a regression demo. On real billing data
the counterfactual is unobservable, so an estimator can be run but never checked. Here
the true effect of every intervention is known by construction, so each estimator can be
graded on the properties that actually matter:

    bias      -- is it systematically wrong?
    RMSE      -- how wrong is a typical estimate?
    coverage  -- do the 95% intervals contain the truth 95% of the time?
    size      -- how often does it claim an effect where there is none?
    power     -- how often does it find an effect that is really there?

Coverage is the one to watch. A 95% interval that covers the truth 60% of the time is
worse than no interval at all, because it is confidently wrong -- which is the exact
failure mode this project exists to eliminate. If our own estimators under-cover, that
goes in the README.

This is the ONLY module permitted to read ``Intervention.true_effect_log``.
"""

from __future__ import annotations

import warnings

import numpy as np
import pandas as pd

from costproof.causal import did as D
from costproof.causal import panel as P
from costproof.causal.synth import synthetic_control


def run_study(
    estate,
    spec: P.PanelSpec | None = None,
    methods: tuple[str, ...] = ("naive_before_after", "did_twfe", "did_permutation",
                                "synthetic_control"),
    verbose: bool = True,
) -> pd.DataFrame:
    """Estimate every intervention with every method and join on ground truth.

    Returns one row per (intervention, method) with the estimate, its inference, the
    parallel-trends diagnostic, and the true effect.
    """
    spec = spec or P.PanelSpec()
    costs = P.daily_resource_costs(estate.billing)

    # Every treated resource is excluded from every donor pool. A treated donor would
    # carry its own treatment effect into the control group and bias tau toward zero.
    treated_all = {iv.resource_id for iv in estate.interventions}

    rows: list[dict] = []
    for n, iv in enumerate(estate.interventions, 1):
        if verbose and n % 25 == 0:
            print(f"  ... {n}/{len(estate.interventions)} interventions")

        try:
            donors = P.select_donors(costs, iv.resource_id, iv.start_date,
                                     excluded=treated_all, spec=spec)
            if len(donors) < spec.min_donors:
                continue
            pan = P.build_panel(costs, iv.resource_id, iv.start_date, donors, spec=spec)
        except (KeyError, ValueError):
            continue

        try:
            pt = D.parallel_trends_test(pan)
        except (ValueError, np.linalg.LinAlgError):
            pt = {"passed": None, "p_value": np.nan}

        for method in methods:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    if method == "naive_before_after":
                        e = D.naive_before_after(pan)
                    elif method == "did_twfe":
                        e = D.did(pan)
                    elif method == "did_permutation":
                        e = D.did_permutation(pan)
                    elif method == "synthetic_control":
                        e = synthetic_control(pan)
                    else:
                        raise ValueError(f"unknown method {method}")
            except (ValueError, np.linalg.LinAlgError, KeyError):
                continue

            rows.append(
                {
                    "intervention_id": iv.intervention_id,
                    "resource_id": iv.resource_id,
                    "business_unit": iv.business_unit,
                    "action": iv.action,
                    "n_donors": len(donors),
                    "trigger_z": iv.trigger_z,
                    # --- ground truth, used only for scoring -----------------------
                    "true_effect": iv.true_effect_log,
                    "is_null": iv.is_null,
                    # --- the estimate ----------------------------------------------
                    **e.as_dict(),
                    "pt_passed": pt.get("passed"),
                    "pt_p_value": pt.get("p_value"),
                }
            )

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["error"] = df["coef"] - df["true_effect"]
    df["covers"] = (df["ci_low"] <= df["true_effect"]) & (df["true_effect"] <= df["ci_high"])
    df["significant"] = df["p_value"] < 0.05
    return df


def score(results: pd.DataFrame) -> pd.DataFrame:
    """Summarise estimator performance. One row per method."""
    out = []
    for method, g in results.groupby("method"):
        nulls = g[g["is_null"]]
        reals = g[~g["is_null"]]
        out.append(
            {
                "method": method,
                "n": len(g),
                # --- accuracy --------------------------------------------------------
                "bias": g["error"].mean(),
                "rmse": float(np.sqrt((g["error"] ** 2).mean())),
                "mae": g["error"].abs().mean(),
                # --- inference -------------------------------------------------------
                "coverage_95": g["covers"].mean(),
                "mean_ci_width": (g["ci_high"] - g["ci_low"]).mean(),
                # --- decision quality ------------------------------------------------
                # Size: share of genuinely-null interventions declared significant.
                # Should be ~0.05. Anything higher means phantom savings get reported.
                "size_false_positive": nulls["significant"].mean() if len(nulls) else np.nan,
                # Power: share of real effects detected.
                "power": reals["significant"].mean() if len(reals) else np.nan,
                # Share of null interventions that LOOK like savings (point estimate
                # negative and significant) -- the number a CFO is actually shown.
                "phantom_savings_rate": (
                    (nulls["significant"] & (nulls["coef"] < 0)).mean()
                    if len(nulls) else np.nan
                ),
                # --- economic magnitude ------------------------------------------------
                "mean_abs_pct_error": (
                    (np.expm1(g["coef"]) - np.expm1(g["true_effect"])).abs().mean()
                ),
            }
        )
    order = {"naive_before_after": 0, "did_twfe": 1, "did_permutation": 2,
             "synthetic_control": 3}
    return pd.DataFrame(out).sort_values(
        "method", key=lambda s: s.map(order).fillna(9)
    ).reset_index(drop=True)


def dollar_impact(estate, results: pd.DataFrame,
                  spec: P.PanelSpec | None = None) -> pd.DataFrame:
    """Translate log-point errors into annualised dollars.

    A log-point error is not persuasive to a CFO. The same error expressed as
    "this method would have misstated the savings programme by $X a year" is.

    For each intervention we take the treated resource's pre-period run rate, apply the
    estimated and true proportional effects, and annualise the difference.
    """
    spec = spec or P.PanelSpec()
    costs = P.daily_resource_costs(estate.billing)
    dates = costs.index

    run_rates: dict[str, float] = {}
    for iv in estate.interventions:
        pos = dates.get_indexer([iv.start_date])[0]
        if pos < 0:
            continue
        lo = max(0, pos - spec.pre_days)
        run_rates[iv.intervention_id] = float(costs.iloc[lo:pos][iv.resource_id].mean())

    df = results.copy()
    df["daily_run_rate"] = df["intervention_id"].map(run_rates)
    df = df[df["daily_run_rate"].notna()]

    df["est_annual_savings"] = -np.expm1(df["coef"]) * df["daily_run_rate"] * 365
    df["true_annual_savings"] = -np.expm1(df["true_effect"]) * df["daily_run_rate"] * 365
    df["misstatement"] = df["est_annual_savings"] - df["true_annual_savings"]

    rows = []
    for method, g in df.groupby("method"):
        nulls = g[g["is_null"]]
        rows.append(
            {
                "method": method,
                "n": len(g),
                "true_annual_savings": g["true_annual_savings"].sum(),
                "claimed_annual_savings": g["est_annual_savings"].sum(),
                "net_misstatement": g["misstatement"].sum(),
                "gross_misstatement": g["misstatement"].abs().sum(),
                "phantom_savings_claimed": (
                    nulls["est_annual_savings"].clip(lower=0).sum() if len(nulls) else 0.0
                ),
            }
        )
    order = {"naive_before_after": 0, "did_twfe": 1, "did_permutation": 2,
             "synthetic_control": 3}
    return pd.DataFrame(rows).sort_values(
        "method", key=lambda s: s.map(order).fillna(9)
    ).reset_index(drop=True)
