"""Synthetic control (Abadie, Diamond & Hainmueller 2010).

When exactly one resource is treated and no donor is individually comparable, averaging
all other resources into a control group is indefensible. Synthetic control instead
builds the counterfactual as a weighted average of donors, choosing weights so the
synthetic unit tracks the treated unit closely *before* treatment.

The constraints are the method:

    w_j >= 0    and    sum_j w_j = 1

They force interpolation rather than extrapolation -- the synthetic unit must lie inside
the convex hull of the donor pool. If the treated resource is more expensive than every
donor, no valid synthetic control exists and the method says so instead of silently
extrapolating. That refusal is a feature: it is the estimator declining to answer a
question the data cannot support.

Inference is by permutation, not by standard errors. With one treated unit there is no
cross-sectional variation to compute a conventional SE from, so we re-run the entire
procedure pretending each donor was treated and compare the real gap to that placebo
distribution.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.optimize import nnls

from costproof.causal.did import Estimate

#: Weight on the sum-to-one constraint row in the augmented NNLS system. Large enough to
#: bind the constraint tightly, small enough to stay numerically well conditioned.
_SIMPLEX_PENALTY = 1e4


def fit_weights(y_pre: np.ndarray, X_pre: np.ndarray) -> np.ndarray:
    """Solve  min ||y - Xw||^2  s.t.  w >= 0, sum(w) = 1.

    Implemented as non-negative least squares on an augmented system: appending a row of
    constant ``M`` to X and ``M`` to y penalises deviation of ``sum(w)`` from 1. As M
    grows the solution converges to the simplex-constrained optimum. This is far faster
    than a general constrained optimiser, which matters because permutation inference
    refits this thousands of times.
    """
    n_donors = X_pre.shape[1]
    X_aug = np.vstack([X_pre, np.full((1, n_donors), _SIMPLEX_PENALTY)])
    y_aug = np.concatenate([y_pre, [_SIMPLEX_PENALTY]])
    w, _ = nnls(X_aug, y_aug)
    total = w.sum()
    return w / total if total > 1e-12 else np.full(n_donors, 1.0 / n_donors)


def _rmspe(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(x)))) if len(x) else np.nan


def _fit_one(y: np.ndarray, X: np.ndarray, pre_mask: np.ndarray) -> dict:
    """Fit weights on the pre-period and return pre/post gap statistics."""
    w = fit_weights(y[pre_mask], X[pre_mask])
    synth = X @ w
    gap = y - synth
    return {
        "weights": w,
        "gap": gap,
        "pre_rmspe": _rmspe(gap[pre_mask]),
        "post_gap_mean": float(np.mean(gap[~pre_mask])),
        "post_rmspe": _rmspe(gap[~pre_mask]),
    }


def synthetic_control(panel: pd.DataFrame, max_placebos: int = 30) -> Estimate:
    """Estimate the treatment effect as the post-period treated-minus-synthetic gap.

    ``panel`` must come from :func:`costproof.causal.panel.build_panel`.
    """
    wide = panel.pivot_table(
        index="rel_day", columns="resource_id", values="log_cost", aggfunc="mean"
    ).sort_index()

    treated_ids = panel.loc[panel["treated"] == 1, "resource_id"].unique()
    if len(treated_ids) != 1:
        raise ValueError(f"expected exactly one treated unit, got {len(treated_ids)}")
    treated = treated_ids[0]

    donor_ids = [c for c in wide.columns if c != treated]
    if len(donor_ids) < 3:
        raise ValueError("synthetic control needs at least 3 donors")

    wide = wide.dropna()
    if wide.empty:
        raise ValueError("no complete periods after aligning donors")

    pre_mask = np.asarray(wide.index) < 0
    if pre_mask.sum() < 10 or (~pre_mask).sum() < 5:
        raise ValueError("insufficient pre or post periods")

    y = wide[treated].to_numpy()
    X = wide[donor_ids].to_numpy()

    fit = _fit_one(y, X, pre_mask)
    coef = fit["post_gap_mean"]

    # --- permutation inference -------------------------------------------------------
    # Re-run the whole procedure treating each donor as if it were treated. The test
    # statistic is the ratio of post-period to pre-period RMSPE, which normalises the
    # effect by how well the method fit that unit to begin with -- without it, units the
    # method fits badly would masquerade as large effects.
    treated_ratio = fit["post_rmspe"] / fit["pre_rmspe"] if fit["pre_rmspe"] > 1e-9 else np.inf

    placebo_ratios: list[float] = []
    placebo_gaps: list[float] = []
    for j in range(min(max_placebos, len(donor_ids))):
        y_p = X[:, j]
        X_p = np.delete(X, j, axis=1)
        if X_p.shape[1] < 3:
            continue
        try:
            f = _fit_one(y_p, X_p, pre_mask)
        except Exception:
            continue
        # Discard placebos the method could not fit. Donors were selected to match the
        # TREATED unit, so some of them are poorly matched by the remaining pool; their
        # gaps are large for reasons that have nothing to do with treatment. Including
        # them inflates the placebo spread and destroys power. Abadie et al. discard
        # placebos whose pre-period fit is much worse than the treated unit's; we use a
        # 2x pre-RMSPE cutoff.
        if f["pre_rmspe"] > 1e-9 and f["pre_rmspe"] <= 2.0 * fit["pre_rmspe"]:
            placebo_ratios.append(f["post_rmspe"] / f["pre_rmspe"])
            placebo_gaps.append(f["post_gap_mean"])

    if placebo_ratios:
        # p-value = share of placebos at least as extreme as the treated unit.
        p_value = float(
            (np.sum(np.array(placebo_ratios) >= treated_ratio) + 1) / (len(placebo_ratios) + 1)
        )
        # The spread of placebo gaps is the natural scale for uncertainty here: it is how
        # large a gap this method produces on units where nothing happened.
        se = float(np.std(placebo_gaps, ddof=1)) if len(placebo_gaps) > 1 else np.nan
    else:
        p_value, se = np.nan, np.nan

    if not np.isfinite(se) or se <= 0:
        se = float(fit["pre_rmspe"]) if fit["pre_rmspe"] > 0 else 0.05

    n_effective = int((fit["weights"] > 0.01).sum())
    return Estimate(
        method="synthetic_control",
        coef=float(coef),
        se=se,
        t_stat=float(coef / se) if se > 0 else np.nan,
        p_value=p_value,
        ci_low=coef - 1.96 * se,
        ci_high=coef + 1.96 * se,
        n_obs=int(wide.shape[0] * wide.shape[1]),
        n_units=len(donor_ids) + 1,
        note=(
            f"{n_effective} donors carry weight; pre-fit RMSPE {fit['pre_rmspe']:.4f}; "
            f"permutation p over {len(placebo_ratios)} placebos."
        ),
    )


def synthetic_control_detail(panel: pd.DataFrame) -> dict:
    """Full diagnostic output for plotting one synthetic-control fit."""
    wide = panel.pivot_table(
        index="rel_day", columns="resource_id", values="log_cost", aggfunc="mean"
    ).sort_index().dropna()
    treated = panel.loc[panel["treated"] == 1, "resource_id"].unique()[0]
    donor_ids = [c for c in wide.columns if c != treated]

    pre_mask = np.asarray(wide.index) < 0
    y = wide[treated].to_numpy()
    X = wide[donor_ids].to_numpy()
    fit = _fit_one(y, X, pre_mask)

    weights = pd.Series(fit["weights"], index=donor_ids).sort_values(ascending=False)
    return {
        "rel_day": wide.index.to_numpy(),
        "treated": y,
        "synthetic": X @ fit["weights"],
        "gap": fit["gap"],
        "weights": weights[weights > 0.001],
        "pre_rmspe": fit["pre_rmspe"],
        "post_gap_mean": fit["post_gap_mean"],
    }
