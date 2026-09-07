"""Difference-in-differences estimation.

The two-way fixed-effects estimator is implemented directly rather than called from a
library. Three reasons:

1. The within transformation is four lines of algebra and hiding it behind a formula
   API obscures what the design is actually doing.
2. Cluster-robust inference is where applied DiD most often goes wrong. Bertrand, Duflo
   & Mullainathan (2004) showed that ignoring serial correlation within units produces
   standard errors far too small and rejection rates several times the nominal level.
   Writing the sandwich out makes that choice explicit and auditable.
3. It is materially faster across ~100 interventions than building hundreds of dummy
   columns per regression.

``test_did.py`` validates this implementation against ``statsmodels`` on the same data,
so the hand-rolled path is checked rather than merely asserted.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy import stats


@dataclass
class Estimate:
    """A treatment effect estimate with inference attached."""

    method: str
    coef: float
    """Effect in log points. exp(coef) - 1 is the proportional change in cost."""
    se: float
    t_stat: float
    p_value: float
    ci_low: float
    ci_high: float
    n_obs: int
    n_units: int
    note: str = ""

    @property
    def pct_change(self) -> float:
        return float(np.expm1(self.coef))

    def covers(self, true_value: float) -> bool:
        return bool(self.ci_low <= true_value <= self.ci_high)

    def as_dict(self) -> dict:
        return {
            "method": self.method,
            "coef": self.coef,
            "se": self.se,
            "t_stat": self.t_stat,
            "p_value": self.p_value,
            "ci_low": self.ci_low,
            "ci_high": self.ci_high,
            "pct_change": self.pct_change,
            "n_obs": self.n_obs,
            "n_units": self.n_units,
            "note": self.note,
        }


# =======================================================================================
# The estimator the industry actually uses
# =======================================================================================


def naive_before_after(panel: pd.DataFrame) -> Estimate:
    """Before-versus-after on the treated unit only -- the industry standard.

    This is not offered as a valid estimator. It is computed for every intervention so
    that reports can show the gap between it and an identified estimate. That gap is the
    dollar value of doing the econometrics correctly.

    It has no control group, so it cannot separate the intervention from anything else
    that moved at the same time: demand growth, seasonality, price changes, or -- most
    dangerously -- mean reversion, since the resource was selected for treatment
    precisely because it had just spiked.
    """
    t = panel[panel["treated"] == 1]
    pre = t[t["post"] == 0]["log_cost"]
    post = t[t["post"] == 1]["log_cost"]
    if len(pre) < 5 or len(post) < 5:
        raise ValueError("insufficient pre or post observations")

    coef = float(post.mean() - pre.mean())
    # A two-sample SE that ignores serial correlation entirely -- which is itself part of
    # why the industry's uncertainty around savings claims is understated.
    se = float(np.sqrt(post.var(ddof=1) / len(post) + pre.var(ddof=1) / len(pre)))
    df = len(pre) + len(post) - 2
    tcrit = stats.t.ppf(0.975, df)
    tstat = coef / se if se > 0 else np.nan
    return Estimate(
        method="naive_before_after",
        coef=coef,
        se=se,
        t_stat=float(tstat),
        p_value=float(2 * (1 - stats.t.cdf(abs(tstat), df))) if se > 0 else np.nan,
        ci_low=coef - tcrit * se,
        ci_high=coef + tcrit * se,
        n_obs=len(pre) + len(post),
        n_units=1,
        note="INVALID DESIGN: no control group; reported only to quantify its error.",
    )


# =======================================================================================
# Two-way fixed effects
# =======================================================================================


def _two_way_demean(
    values: np.ndarray, unit_idx: np.ndarray, time_idx: np.ndarray
) -> np.ndarray:
    """Sweep out unit and time means: v - mean_i - mean_t + mean_overall.

    For a balanced panel this is algebraically identical to including a full set of unit
    and time dummies, at a fraction of the cost.
    """
    v = np.asarray(values, dtype=float)
    two_d = v.ndim == 2
    if not two_d:
        v = v[:, None]

    n_units = unit_idx.max() + 1
    n_times = time_idx.max() + 1
    out = v.copy()
    for j in range(v.shape[1]):
        col = v[:, j]
        unit_mean = np.bincount(unit_idx, weights=col, minlength=n_units) / np.bincount(
            unit_idx, minlength=n_units
        )
        time_mean = np.bincount(time_idx, weights=col, minlength=n_times) / np.bincount(
            time_idx, minlength=n_times
        )
        out[:, j] = col - unit_mean[unit_idx] - time_mean[time_idx] + col.mean()
    return out if two_d else out[:, 0]


def _cluster_robust_se(
    X: np.ndarray, resid: np.ndarray, cluster: np.ndarray, n_absorbed: int
) -> np.ndarray:
    """Cluster-robust variance, clustering on the panel unit.

    A resource's cost errors are strongly serially correlated -- yesterday's overspend
    predicts today's. Treating each resource-day as independent would inflate the
    effective sample size by the length of the panel and shrink standard errors by
    roughly its square root. Clustering on the resource is the standard correction.
    """
    n, k = X.shape
    XtX_inv = np.linalg.pinv(X.T @ X)

    meat = np.zeros((k, k))
    for g in np.unique(cluster):
        m = cluster == g
        Xg, ug = X[m], resid[m]
        s = Xg.T @ ug
        meat += np.outer(s, s)

    n_clusters = len(np.unique(cluster))
    # Standard finite-sample correction (Cameron & Miller 2015).
    dof = n - k - n_absorbed
    correction = (n_clusters / max(1, n_clusters - 1)) * ((n - 1) / max(1, dof))
    V = XtX_inv @ meat @ XtX_inv * correction
    return np.sqrt(np.maximum(np.diag(V), 0.0))


def did(panel: pd.DataFrame) -> Estimate:
    """Two-way fixed-effects difference-in-differences.

        log(cost_it) = alpha_i + lambda_t + tau * (treated_i x post_t) + e_it

    Unit fixed effects absorb every time-invariant difference between resources, so we
    never have to model why a GPU node costs more than a queue. Time fixed effects absorb
    everything common to all units in a period -- a provider price change, a company-wide
    traffic event, or a seasonal swing. What remains for tau is the treated unit's
    deviation from the path its controls took.

    Identifying assumption: parallel trends. Test it with ``event_study`` and
    ``parallel_trends_test`` rather than assuming it.
    """
    units, unit_idx = np.unique(panel["resource_id"].to_numpy(), return_inverse=True)
    _, time_idx = np.unique(panel["rel_day"].to_numpy(), return_inverse=True)

    n_units = len(units)
    if n_units < 3:
        raise ValueError(f"DiD needs at least 3 units, got {n_units}")

    y = _two_way_demean(panel["log_cost"].to_numpy(), unit_idx, time_idx)
    X = _two_way_demean(panel[["treat_post"]].to_numpy(dtype=float), unit_idx, time_idx)

    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta

    n_absorbed = n_units + (time_idx.max() + 1) - 1
    se = _cluster_robust_se(X, resid, unit_idx, n_absorbed)[0]

    coef = float(beta[0])
    df = max(1, n_units - 1)  # cluster-robust inference uses G-1 degrees of freedom
    tcrit = stats.t.ppf(0.975, df)
    tstat = coef / se if se > 0 else np.nan

    return Estimate(
        method="did_twfe",
        coef=coef,
        se=float(se),
        t_stat=float(tstat),
        p_value=float(2 * (1 - stats.t.cdf(abs(tstat), df))) if se > 0 else np.nan,
        ci_low=coef - tcrit * se,
        ci_high=coef + tcrit * se,
        n_obs=len(panel),
        n_units=n_units,
        note=f"SEs clustered on resource ({n_units} clusters).",
    )


# =======================================================================================
# Event study and the parallel-trends diagnostic
# =======================================================================================


def did_permutation(panel: pd.DataFrame, max_placebos: int = 40) -> Estimate:
    """DiD point estimate with RANDOMIZATION INFERENCE instead of cluster-robust SEs.

    Why this exists
    ---------------
    Cluster-robust standard errors are consistent as the number of *treated* clusters
    grows. Here there is exactly one treated resource. Clustering on resource gives ~40
    clusters, but only one of them carries any information about tau, so the usual
    sandwich is severely downward biased and its intervals are far too narrow. In the
    validation study this shows up unmistakably: identical point estimates, but 95%
    intervals that cover the truth 23% of the time.

    This is a known result, not a quirk of this data (Conley & Taber 2011; Ferman &
    Pinto 2019; and the small-cluster literature generally). The standard remedy with a
    single treated unit is randomization inference.

    Method
    ------
    Re-estimate the identical DiD specification pretending each donor was the treated
    unit, with the real treated unit removed. That yields a distribution of estimates
    under the null of no effect, which reflects all the serial correlation and
    heterogeneity the parametric SE ignores. The spread of that distribution is the
    honest scale for uncertainty, and the treated estimate's rank in it is the p-value.
    """
    real = did(panel)

    donors = panel.loc[panel["treated"] == 0, "resource_id"].unique().tolist()
    placebo: list[float] = []
    for donor in donors[:max_placebos]:
        fake = panel[panel["treated"] == 0].copy()
        fake["treated"] = (fake["resource_id"] == donor).astype(int)
        fake["treat_post"] = fake["treated"] * fake["post"]
        if fake["treat_post"].sum() == 0:
            continue
        try:
            placebo.append(did(fake).coef)
        except (ValueError, np.linalg.LinAlgError):
            continue

    if len(placebo) < 5:
        real.note += " | too few placebos for randomization inference"
        return real

    arr = np.asarray(placebo)
    se = float(arr.std(ddof=1))
    # Two-sided permutation p-value, centred on the placebo distribution's own median to
    # guard against a donor pool that drifts as a whole.
    centre = float(np.median(arr))
    p = float((np.sum(np.abs(arr - centre) >= abs(real.coef - centre)) + 1) / (len(arr) + 1))

    coef = real.coef
    return Estimate(
        method="did_permutation",
        coef=coef,
        se=se,
        t_stat=float((coef - centre) / se) if se > 0 else np.nan,
        p_value=p,
        ci_low=coef - 1.96 * se,
        ci_high=coef + 1.96 * se,
        n_obs=real.n_obs,
        n_units=real.n_units,
        note=(
            f"Randomization inference over {len(arr)} placebo assignments "
            f"(cluster-robust SE would have been {real.se:.4f}, "
            f"{se / real.se:.1f}x narrower)."
        ),
    )


def event_study(panel: pd.DataFrame, bin_days: int = 7) -> pd.DataFrame:
    """Treatment effect by period relative to the action, normalised at the last pre-bin.

    This is the single most important diagnostic in the project. Pre-treatment
    coefficients should be statistically indistinguishable from zero: if they trend,
    parallel trends is already violated before treatment and the design is invalid
    regardless of how significant the post-period estimate looks.

    Post-treatment coefficients should show the effect ramping in over a few days, since
    an optimisation rolls out rather than teleporting.
    """
    df = panel.copy()
    df["bin"] = np.floor(df["rel_day"] / bin_days).astype(int)

    pre_bins = sorted(b for b in df["bin"].unique() if b < 0)
    if not pre_bins:
        raise ValueError("no pre-treatment periods available")
    base = max(pre_bins)  # omitted category: the bin just before treatment

    bins = sorted(b for b in df["bin"].unique() if b != base)
    units, unit_idx = np.unique(df["resource_id"].to_numpy(), return_inverse=True)
    _, time_idx = np.unique(df["rel_day"].to_numpy(), return_inverse=True)

    D = np.column_stack(
        [((df["bin"] == b) & (df["treated"] == 1)).to_numpy(dtype=float) for b in bins]
    )
    y = _two_way_demean(df["log_cost"].to_numpy(), unit_idx, time_idx)
    X = _two_way_demean(D, unit_idx, time_idx)

    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    n_absorbed = len(units) + (time_idx.max() + 1) - 1
    se = _cluster_robust_se(X, resid, unit_idx, n_absorbed)

    df_t = max(1, len(units) - 1)
    tcrit = stats.t.ppf(0.975, df_t)
    out = pd.DataFrame(
        {
            "bin": bins,
            "rel_day_start": [b * bin_days for b in bins],
            "coef": beta,
            "se": se,
            "ci_low": beta - tcrit * se,
            "ci_high": beta + tcrit * se,
            "is_pre": [b < 0 for b in bins],
        }
    )
    base_row = pd.DataFrame(
        [{"bin": base, "rel_day_start": base * bin_days, "coef": 0.0, "se": 0.0,
          "ci_low": 0.0, "ci_high": 0.0, "is_pre": True}]
    )
    return pd.concat([out, base_row]).sort_values("bin").reset_index(drop=True)


def _pre_trend_statistic(panel: pd.DataFrame, bin_days: int) -> float:
    """Root-mean-square of the pre-treatment event-study coefficients.

    Deliberately scale-free of any standard error: the whole point is to avoid building
    the specification test on the same broken variance estimate.
    """
    es = event_study(panel, bin_days=bin_days)
    pre = es[es["is_pre"]]
    if pre.empty:
        return np.nan
    return float(np.sqrt(np.mean(np.square(pre["coef"].to_numpy()))))


def parallel_trends_test(panel: pd.DataFrame, bin_days: int = 7,
                         max_placebos: int = 30) -> dict:
    """Test for pre-treatment divergence, using randomization inference.

    Why not the textbook Wald test
    ------------------------------
    The obvious test is a joint Wald test that all pre-treatment event-study
    coefficients are zero. That test divides each coefficient by a cluster-robust
    standard error -- and with a single treated unit those standard errors are far too
    narrow (see :func:`did_permutation`). Every t-statistic is inflated, so the Wald
    statistic is inflated by roughly the square of that factor, and the test rejects
    almost always. Running it on this study rejected parallel trends for 98.3% of
    interventions, including designs that visibly satisfy it.

    That is not evidence the designs are bad. It is the broken variance estimate
    propagating into the specification test built on top of it -- the same root cause,
    one layer down.

    What this does instead
    ----------------------
    Compare the treated unit's pre-period RMS coefficient to the distribution of the
    same statistic computed for donors pretending to be treated. If the treated unit's
    pre-period wobbles no more than a typical untreated donor's, there is no evidence
    against parallel trends. This needs no standard error at all.

    A large p-value is weak evidence: it means the pre-period gives no evidence
    *against* parallel trends, not that the assumption holds. A small p-value is strong
    evidence the design is broken and the estimate must not be reported as causal.
    """
    stat = _pre_trend_statistic(panel, bin_days)
    if not np.isfinite(stat):
        return {"passed": None, "p_value": np.nan, "n_placebos": 0,
                "note": "no testable pre-period bins"}

    donors = panel.loc[panel["treated"] == 0, "resource_id"].unique().tolist()
    placebo: list[float] = []
    donor_only = panel[panel["treated"] == 0]
    for donor in donors[:max_placebos]:
        fake = donor_only.copy()
        fake["treated"] = (fake["resource_id"] == donor).astype(int)
        fake["treat_post"] = fake["treated"] * fake["post"]
        try:
            s = _pre_trend_statistic(fake, bin_days)
        except (ValueError, np.linalg.LinAlgError):
            continue
        if np.isfinite(s):
            placebo.append(s)

    if len(placebo) < 5:
        return {"passed": None, "p_value": np.nan, "n_placebos": len(placebo),
                "pre_trend_rms": stat, "note": "too few placebos to test"}

    arr = np.asarray(placebo)
    p = float((np.sum(arr >= stat) + 1) / (len(arr) + 1))
    return {
        "passed": bool(p >= 0.05),
        "p_value": p,
        "pre_trend_rms": stat,
        "placebo_median_rms": float(np.median(arr)),
        "n_placebos": len(arr),
        "note": ("pre-period no worse than untreated donors" if p >= 0.05
                 else "PRE-TRENDS VIOLATED: estimate is not credibly causal"),
    }
