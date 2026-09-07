"""Waste detection: separating genuine waste from normal variation.

The problem this solves
-----------------------
Every FinOps tool on the market detects anomalies. None of them are trusted, because
the real failure mode is not missed waste -- it is ALERT FATIGUE. A tool that flags
four hundred resources a week gets muted in a fortnight, and after that it catches
nothing at all regardless of how good its detector is.

So the metric that matters is not accuracy, and not even recall. It is:

    "If we surface the top N resources this week, how many are genuinely wasteful?"

That is precision at a workable alert volume, and it is what the evaluation below
reports. A detector with 95% recall and 20% precision is worse than useless in
production; a detector that surfaces fifty resources of which forty are real gets
acted on every week.

Honest evaluation
-----------------
Two decisions here matter more than the model choice:

1. **Split by resource, not by row.** A resource contributes ~540 daily rows. Split
   randomly and the same resource lands in both train and test, so the model can
   memorise that resource rather than learn what waste looks like. Scores come out
   inflated and meaningless. We split on resource id.

2. **No ground truth in the features.** Every feature is computable from a billing
   feed alone. Labels exist only to train and to score.
"""

from __future__ import annotations

from dataclasses import dataclass

import duckdb
import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.metrics import (
    average_precision_score,
    precision_recall_curve,
    roc_auc_score,
)

# =======================================================================================
# Feature engineering
# =======================================================================================

#: Features are deliberately RELATIVE rather than absolute. "This resource costs $400
#: a day" says nothing -- a GPU cluster should. "This resource costs 3.2x its own
#: 28-day average and its weekend dip disappeared" is evidence. Absolute cost as a
#: feature would just teach the model that expensive resources are wasteful, which is
#: both false and useless.
FEATURE_SQL = """
WITH daily AS (
    SELECT
        resource_id,
        charge_date,
        ANY_VALUE(service_category)  AS service_category,
        ANY_VALUE(environment)       AS environment,
        ANY_VALUE(is_tagged)         AS is_tagged,
        ANY_VALUE(is_weekend)        AS is_weekend,
        SUM(effective_cost)          AS cost,
        SUM(consumed_quantity)       AS quantity
    FROM 'data/silver/billing.parquet'
    WHERE charge_category = 'Usage'
    GROUP BY 1, 2
),
windowed AS (
    SELECT
        *,
        AVG(cost)    OVER w28  AS cost_avg_28d,
        STDDEV(cost) OVER w28  AS cost_std_28d,
        AVG(cost)    OVER w7   AS cost_avg_7d,
        MIN(cost)    OVER w28  AS cost_min_28d,
        MAX(cost)    OVER w28  AS cost_max_28d,
        AVG(CASE WHEN is_weekend THEN cost END) OVER w28 AS weekend_avg_28d,
        AVG(CASE WHEN NOT is_weekend THEN cost END) OVER w28 AS weekday_avg_28d,
        ROW_NUMBER() OVER (PARTITION BY resource_id ORDER BY charge_date) AS day_index
    FROM daily
    WINDOW
        w28 AS (PARTITION BY resource_id ORDER BY charge_date
                ROWS BETWEEN 27 PRECEDING AND CURRENT ROW),
        w7  AS (PARTITION BY resource_id ORDER BY charge_date
                ROWS BETWEEN 6 PRECEDING AND CURRENT ROW)
)
SELECT
    resource_id,
    charge_date,
    service_category,
    environment,
    is_tagged,
    cost,

    -- How elevated is today against this resource's own recent normal? The core
    -- signal. Ratios, not differences, so a $10/day resource and a $10,000/day
    -- resource are on the same scale.
    CASE WHEN cost_avg_28d > 0 THEN cost / cost_avg_28d END       AS ratio_28d,
    CASE WHEN cost_avg_7d  > 0 THEN cost / cost_avg_7d  END       AS ratio_7d,

    -- Is the recent week elevated against the recent month? Catches sustained
    -- creep that a single-day spike test misses entirely -- and gradual creep is
    -- the waste pattern that most often escapes threshold alerting.
    CASE WHEN cost_avg_28d > 0 THEN cost_avg_7d / cost_avg_28d END AS drift_7d_28d,

    -- Standardised deviation from own history.
    CASE WHEN cost_std_28d > 0 THEN (cost - cost_avg_28d) / cost_std_28d END AS zscore_28d,

    -- Flatness. A resource doing real work has variation: traffic rises and falls.
    -- An idle or orphaned resource bills a near-constant amount forever, so a
    -- narrow range relative to its level is itself suspicious.
    CASE WHEN cost_avg_28d > 0
         THEN (cost_max_28d - cost_min_28d) / cost_avg_28d END     AS range_ratio_28d,
    CASE WHEN cost_avg_28d > 0 THEN cost_std_28d / cost_avg_28d END AS coef_variation_28d,

    -- Weekend/weekday ratio. Genuine workloads dip at weekends -- batch jobs stop,
    -- office traffic falls. A resource whose weekend spend equals its weekday spend
    -- is probably running whether or not anyone needs it.
    CASE WHEN weekday_avg_28d > 0
         THEN weekend_avg_28d / weekday_avg_28d END                AS weekend_ratio,

    -- Non-production resources are far more likely to be forgotten. Untagged ones
    -- have no owner to forget them in the first place.
    environment IN ('development', 'staging', 'sandbox')           AS is_non_production,

    day_index
FROM windowed
WHERE day_index > 28          -- need a full window before any ratio means anything
ORDER BY resource_id, charge_date
"""

FEATURES: tuple[str, ...] = (
    "ratio_28d",
    "ratio_7d",
    "drift_7d_28d",
    "zscore_28d",
    "range_ratio_28d",
    "coef_variation_28d",
    "weekend_ratio",
    "is_non_production",
    "is_tagged",
)


def build_features(silver_path: str = "data/silver/billing.parquet") -> pd.DataFrame:
    """Compute per-resource-per-day features from the silver billing table."""
    sql = FEATURE_SQL.replace("'data/silver/billing.parquet'", f"'{silver_path}'")
    return duckdb.sql(sql).df()


def attach_labels(features: pd.DataFrame,
                  waste_path: str = "data/gold/ground_truth_waste.parquet",
                  billing_path: str = "data/silver/billing.parquet") -> pd.DataFrame:
    """Label each resource-day as inside a known waste event or not.

    Ground truth is used ONLY here and in scoring -- never as a model input.
    """
    waste = duckdb.sql(f"SELECT * FROM '{waste_path}'").df()
    dates = duckdb.sql(
        f"SELECT DISTINCT charge_date FROM '{billing_path}' ORDER BY 1"
    ).df()["charge_date"].to_numpy()

    df = features.copy()
    df["is_waste"] = False
    for w in waste.itertuples(index=False):
        lo, hi = int(w.start_day), int(w.end_day)
        if lo >= len(dates):
            continue
        start = dates[lo]
        end = dates[min(hi, len(dates) - 1)]
        mask = (
            (df["resource_id"] == w.resource_id)
            & (df["charge_date"] >= start)
            & (df["charge_date"] <= end)
        )
        df.loc[mask, "is_waste"] = True
    return df


# =======================================================================================
# Model
# =======================================================================================


@dataclass
class WasteModelResult:
    model: GradientBoostingClassifier
    scores: pd.DataFrame
    metrics: dict
    importance: pd.Series

    def report(self) -> str:
        m = self.metrics
        lines = [
            "WASTE DETECTION -- held-out resources",
            f"  train / test resources     {m['n_train_resources']} / {m['n_test_resources']}",
            f"  test rows                  {m['n_test_rows']:,}",
            f"  waste prevalence           {m['prevalence']:.1%}",
            "",
            "  Ranking quality",
            f"    ROC AUC                  {m['roc_auc']:.3f}",
            f"    PR AUC (avg precision)   {m['pr_auc']:.3f}   (baseline = prevalence)",
            "",
            "  Operating points -- what an analyst actually sees",
        ]
        for k, v in m["precision_at_k"].items():
            lines.append(
                f"    top {k:>4} alerts          precision {v['precision']:.1%}"
                f"   recall {v['recall']:.1%}"
            )
        lines += [
            "",
            "  Baseline: simple 2x-of-28-day-average threshold",
            f"    precision                {m['baseline_precision']:.1%}",
            f"    recall                   {m['baseline_recall']:.1%}",
            f"    alerts raised            {m['baseline_alerts']:,}",
        ]
        return "\n".join(lines)


def train(labelled: pd.DataFrame, test_fraction: float = 0.3,
          seed: int = 20260907) -> WasteModelResult:
    """Fit and honestly evaluate a waste classifier.

    The split is BY RESOURCE. Splitting rows at random would place the same
    resource's days in both train and test, letting the model memorise individual
    resources instead of learning what waste looks like -- which inflates every score
    and produces a model that fails on anything new.
    """
    df = labelled.dropna(subset=list(FEATURES)).copy()

    rng = np.random.default_rng(seed)
    resources = df["resource_id"].unique()
    rng.shuffle(resources)
    n_test = int(len(resources) * test_fraction)
    test_ids = set(resources[:n_test])

    is_test = df["resource_id"].isin(test_ids)
    train_df, test_df = df[~is_test], df[is_test]

    X_train = train_df[list(FEATURES)].astype(float).to_numpy()
    y_train = train_df["is_waste"].to_numpy()
    X_test = test_df[list(FEATURES)].astype(float).to_numpy()
    y_test = test_df["is_waste"].to_numpy()

    model = GradientBoostingClassifier(
        n_estimators=220, learning_rate=0.06, max_depth=3,
        subsample=0.85, random_state=seed,
    )
    model.fit(X_train, y_train)
    proba = model.predict_proba(X_test)[:, 1]

    scored = test_df.assign(waste_score=proba)

    # --- precision at realistic alert volumes ------------------------------------
    # Reported per DISTINCT RESOURCE, not per row. An analyst works a queue of
    # resources to investigate, not a queue of resource-days -- 400 alerts for the
    # same resource on 400 days is one problem, not four hundred.
    per_resource = (
        scored.groupby("resource_id")
        .agg(score=("waste_score", "max"), is_waste=("is_waste", "any"))
        .sort_values("score", ascending=False)
    )
    total_wasteful = int(per_resource["is_waste"].sum())
    precision_at_k = {}
    for k in (10, 25, 50, 100):
        if k > len(per_resource):
            continue
        top = per_resource.head(k)
        hits = int(top["is_waste"].sum())
        precision_at_k[k] = {
            "precision": hits / k,
            "recall": hits / total_wasteful if total_wasteful else float("nan"),
        }

    # --- baseline: the rule every FinOps tool ships -------------------------------
    # "Alert when today exceeds 2x the 28-day average." This is what CostProof is
    # competing against, and it is what makes the comparison meaningful rather than
    # a model score floating in a vacuum.
    base_flag = test_df["ratio_28d"] > 2.0
    base_tp = int((base_flag & y_test).sum())
    base_alerts = int(base_flag.sum())

    metrics = {
        "n_train_resources": len(resources) - n_test,
        "n_test_resources": n_test,
        "n_test_rows": len(test_df),
        "prevalence": float(y_test.mean()),
        "roc_auc": float(roc_auc_score(y_test, proba)),
        "pr_auc": float(average_precision_score(y_test, proba)),
        "precision_at_k": precision_at_k,
        "baseline_precision": base_tp / base_alerts if base_alerts else 0.0,
        "baseline_recall": base_tp / int(y_test.sum()) if y_test.sum() else 0.0,
        "baseline_alerts": base_alerts,
    }
    importance = pd.Series(
        model.feature_importances_, index=list(FEATURES)
    ).sort_values(ascending=False)

    return WasteModelResult(model, scored, metrics, importance)


def precision_recall_table(result: WasteModelResult) -> pd.DataFrame:
    """Precision/recall across every threshold, for plotting or threshold selection."""
    y = result.scores["is_waste"].to_numpy()
    p = result.scores["waste_score"].to_numpy()
    precision, recall, thresholds = precision_recall_curve(y, p)
    return pd.DataFrame(
        {
            "threshold": np.append(thresholds, 1.0),
            "precision": precision,
            "recall": recall,
        }
    )
