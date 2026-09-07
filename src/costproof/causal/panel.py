"""Panel construction and donor selection for causal analysis.

Nothing in this module may touch ground truth. Estimators see exactly what a real
deployment would see: a billing feed, and a change log saying an action was taken on a
resource on a date. The true effect is revealed only to the scoring code in
``costproof.causal.validate``.

Keeping that boundary strict is what makes the validation honest. If donor selection
could peek at true effects, the whole exercise would be circular.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

#: Log cost floor. Resources can bill trivially small amounts on some days, and log(0)
#: is undefined; clipping keeps the panel balanced instead of silently dropping days,
#: which would break the two-way fixed-effects design.
_MIN_COST = 1e-3


@dataclass(frozen=True)
class PanelSpec:
    """Analysis window around one intervention, in days relative to the action."""

    pre_days: int = 84
    post_days: int = 56
    gap_days: int = 3
    """Days after the action to exclude. An optimisation takes a few days to roll out,
    so the transition period is neither clean pre nor clean post. Including it biases
    the estimate toward zero."""

    min_donors: int = 12
    max_donors: int = 40


def daily_resource_costs(billing: pd.DataFrame) -> pd.DataFrame:
    """Collapse the FOCUS feed to a resource x day matrix of EffectiveCost.

    EffectiveCost, not BilledCost. Committed usage carries zero BilledCost because it
    was prepaid, so a BilledCost panel would show large blocks of the estate as free
    and any analysis built on it would be nonsense.
    """
    usage = billing[billing["ChargeCategory"] == "Usage"]
    wide = usage.pivot_table(
        index="ChargePeriodStart",
        columns="ResourceId",
        values="EffectiveCost",
        aggfunc="sum",
    ).sort_index()
    return wide.fillna(0.0)


def build_panel(
    costs: pd.DataFrame,
    treated_resource: str,
    action_date: pd.Timestamp,
    donors: list[str],
    spec: PanelSpec = PanelSpec(),
) -> pd.DataFrame:
    """Long-format panel for one intervention: treated unit plus its donors.

    Returns columns: resource_id, date, rel_day, cost, log_cost, treated, post, treat_post.
    Rows inside the roll-out gap are dropped.
    """
    if treated_resource not in costs.columns:
        raise KeyError(f"treated resource {treated_resource!r} not in cost matrix")

    dates = costs.index
    pos = dates.get_indexer([action_date])[0]
    if pos < 0:
        raise KeyError(f"action date {action_date} not in cost index")

    lo = max(0, pos - spec.pre_days)
    hi = min(len(dates), pos + spec.post_days + 1)
    window = dates[lo:hi]

    units = [treated_resource, *[d for d in donors if d != treated_resource]]
    block = costs.loc[window, units]

    long = (
        block.stack()
        .rename("cost")
        .reset_index()
        .rename(columns={"ChargePeriodStart": "date", "ResourceId": "resource_id"})
    )
    long["rel_day"] = (long["date"] - action_date).dt.days
    long["log_cost"] = np.log(long["cost"].clip(lower=_MIN_COST))
    long["treated"] = (long["resource_id"] == treated_resource).astype(int)
    long["post"] = (long["rel_day"] >= spec.gap_days).astype(int)
    long["treat_post"] = long["treated"] * long["post"]

    # Drop the roll-out window: neither clean pre nor clean post.
    long = long[(long["rel_day"] < 0) | (long["rel_day"] >= spec.gap_days)]
    return long.reset_index(drop=True)


def select_donors(
    costs: pd.DataFrame,
    treated_resource: str,
    action_date: pd.Timestamp,
    excluded: set[str],
    spec: PanelSpec = PanelSpec(),
) -> list[str]:
    """Choose control units for one intervention.

    Donors are ranked by how well their pre-period *growth* tracks the treated unit's.
    Matching on growth rather than level is the point: parallel trends is an assumption
    about changes, not about magnitudes. A donor that costs a tenth as much but moves in
    step is a good control; one that costs the same but drifts is a bad one.

    ``excluded`` must contain every resource treated anywhere in the study, because a
    treated donor would contaminate the control group and bias the estimate toward zero.
    """
    dates = costs.index
    pos = dates.get_indexer([action_date])[0]
    lo = max(0, pos - spec.pre_days)
    pre = costs.iloc[lo:pos]

    eligible = [
        c for c in costs.columns
        if c != treated_resource and c not in excluded and (pre[c] > _MIN_COST).mean() > 0.9
    ]
    if not eligible:
        return []

    log_pre = np.log(pre[[treated_resource, *eligible]].clip(lower=_MIN_COST))
    growth = log_pre.diff().iloc[1:]

    target = growth[treated_resource]
    if target.std() < 1e-9:
        return eligible[: spec.max_donors]

    corr = growth[eligible].corrwith(target).fillna(-1.0)

    # Also penalise donors whose pre-period drift differs sharply from the treated unit's:
    # a donor that is correlated day to day but trending the other way still violates
    # parallel trends over the post window.
    drift_gap = (growth[eligible].mean() - target.mean()).abs()
    drift_scale = drift_gap.median() if drift_gap.median() > 0 else 1.0
    score = corr - 0.35 * (drift_gap / drift_scale).clip(upper=3.0)

    ranked = score.sort_values(ascending=False)
    return ranked.head(spec.max_donors).index.tolist()


def change_log(interventions) -> pd.DataFrame:
    """The observable record of what was done, with no effect information.

    This is deliberately lossy. It is what a CMDB, a ticket queue or a Turbonomic action
    log would actually contain, and it is the only intervention information any estimator
    in this project is allowed to use.
    """
    return pd.DataFrame(
        [
            {
                "intervention_id": iv.intervention_id,
                "resource_id": iv.resource_id,
                "action_date": iv.start_date,
                "action": iv.action,
            }
            for iv in interventions
        ]
    )
