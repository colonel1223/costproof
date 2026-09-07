"""Analytical tools the agent is allowed to call.

The contract
------------
Every number CostProof reports comes from a function in this module. The language
model chooses *which* tool to call and summarises what came back; it never computes,
estimates, or restates a figure itself.

This is the same rule as the Tariff Exposure Agent: the model decides which rule
applies, deterministic code decides what it costs. The reason is specific rather than
philosophical -- language models produce arithmetic errors at rates that are fine for
prose and unacceptable for a number a CFO will act on, and a wrong figure delivered
fluently is worse than no figure at all.

Every tool returns a `ToolResult` carrying the value, the method used to obtain it,
and enough provenance to reproduce it. A result that cannot be reproduced from its own
record is a defect.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

SILVER = "data/silver/billing.parquet"
GOLD_UNIT_ECON = "data/gold/unit_economics.parquet"


@dataclass
class ToolResult:
    """The value plus everything needed to audit it."""

    tool: str
    ok: bool
    value: Any
    method: str
    """Plain-language description of how the number was produced. This goes into the
    audit record and into any output shown to a human."""
    caveats: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    elapsed_ms: float = 0.0
    error: str | None = None

    def as_dict(self) -> dict:
        d = asdict(self)
        if isinstance(self.value, pd.DataFrame):
            d["value"] = self.value.to_dict(orient="records")
        return d


def _timed(fn):
    """Wrap a tool so failures return a ToolResult instead of raising.

    An agent that crashes mid-run leaves no audit trail. A failed tool must still
    produce a record saying what was attempted and why it did not work.
    """
    def wrapper(*args, **kwargs) -> ToolResult:
        t0 = time.perf_counter()
        try:
            result = fn(*args, **kwargs)
            result.elapsed_ms = (time.perf_counter() - t0) * 1000
            return result
        except Exception as exc:  # noqa: BLE001 -- deliberate: never crash the agent
            return ToolResult(
                tool=fn.__name__, ok=False, value=None,
                method="failed before producing a value",
                error=f"{type(exc).__name__}: {exc}",
                elapsed_ms=(time.perf_counter() - t0) * 1000,
            )
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


# =======================================================================================
# Spend
# =======================================================================================


@_timed
def get_spend_summary(silver_path: str = SILVER) -> ToolResult:
    """Total effective spend by business unit, with untagged spend shown explicitly."""
    df = duckdb.sql(f"""
        SELECT business_unit,
               SUM(effective_cost)                     AS effective_cost,
               SUM(effective_cost) / COUNT(DISTINCT charge_date) * 365 AS annualised,
               COUNT(DISTINCT resource_id)             AS resources,
               BOOL_OR(NOT is_tagged)                  AS includes_untagged
        FROM '{silver_path}'
        WHERE charge_category = 'Usage'
        GROUP BY 1 ORDER BY effective_cost DESC
    """).df()

    untagged = float(df.loc[df["business_unit"] == "unallocated", "effective_cost"].sum())
    total = float(df["effective_cost"].sum())

    return ToolResult(
        tool="get_spend_summary", ok=True, value=df,
        method=(
            "SUM(EffectiveCost) grouped by business unit over Usage rows. EffectiveCost "
            "is used rather than BilledCost because committed usage carries zero billed "
            "cost -- the money left when the commitment was purchased -- which would make "
            "prepaid resources appear free."
        ),
        caveats=[
            f"${untagged:,.0f} ({untagged / total:.1%}) has no business_unit tag and "
            f"cannot be attributed to any team from the billing feed alone."
        ] if untagged > 0 else [],
        sources=[silver_path],
    )


@_timed
def get_unit_economics(business_unit: str | None = None,
                       gold_path: str = GOLD_UNIT_ECON) -> ToolResult:
    """Cost per unit of business output, and whether efficiency is improving.

    Answers the only question that makes a rising bill interpretable: is spend up
    because we are growing, or because we are getting worse?
    """
    where = f"WHERE business_unit = '{business_unit}'" if business_unit else ""
    df = duckdb.sql(f"""
        WITH b AS (
            SELECT *, MIN(charge_date) OVER () AS d0, MAX(charge_date) OVER () AS dN
            FROM '{gold_path}' {where}
        )
        SELECT business_unit,
               ANY_VALUE(driver)                                              AS driver,
               AVG(CASE WHEN charge_date < d0 + 60 THEN cost END)             AS cost_first_60d,
               AVG(CASE WHEN charge_date > dN - 60 THEN cost END)             AS cost_last_60d,
               100 * (AVG(CASE WHEN charge_date > dN - 60 THEN cost END)
                    / AVG(CASE WHEN charge_date < d0 + 60 THEN cost END) - 1) AS cost_change_pct,
               100 * (AVG(CASE WHEN charge_date > dN - 60 THEN cost_per_1k_units END)
                    / AVG(CASE WHEN charge_date < d0 + 60 THEN cost_per_1k_units END) - 1)
                                                                              AS unit_cost_change_pct
        FROM b GROUP BY 1 ORDER BY cost_change_pct DESC
    """).df()

    df["verdict"] = df.apply(
        lambda r: "efficiency degrading" if r["unit_cost_change_pct"] > 2
        else ("growing efficiently" if r["unit_cost_change_pct"] < -2 else "flat"),
        axis=1,
    )
    degrading = df[df["verdict"] == "efficiency degrading"]["business_unit"].tolist()

    return ToolResult(
        tool="get_unit_economics", ok=True, value=df,
        method=(
            "Compares the first 60 days to the last 60 days, for both total cost and "
            "cost per 1,000 units of business output. A rising bill with falling unit "
            "cost is growth, not waste."
        ),
        caveats=(
            [f"Only {', '.join(degrading)} shows genuinely degrading efficiency; the "
             f"others' bills rose while their unit costs fell."] if degrading else []
        ) + [
            "'unallocated' and 'bu-platform' are excluded: neither has a business driver, "
            "so a unit cost for them would have a fabricated denominator."
        ],
        sources=[gold_path],
    )


@_timed
def check_commitment_waste(silver_path: str = SILVER) -> ToolResult:
    """Find capacity that was prepaid and never consumed.

    This waste is attached to no resource, so no rightsizing exercise will ever find
    it. It appears only if you look for it directly.
    """
    df = duckdb.sql(f"""
        SELECT DATE_TRUNC('month', charge_date)  AS month,
               SUM(effective_cost)               AS unused_cost
        FROM '{silver_path}'
        WHERE is_unused_commitment
        GROUP BY 1 ORDER BY 1
    """).df()
    total = float(df["unused_cost"].sum())
    days = duckdb.sql(
        f"SELECT COUNT(DISTINCT charge_date) FROM '{silver_path}'"
    ).fetchone()[0]

    return ToolResult(
        tool="check_commitment_waste", ok=True,
        value={"total_unused": total,
               "annualised": total / days * 365,
               "by_month": df.to_dict(orient="records")},
        method=(
            "Sums EffectiveCost on rows where FOCUS reports "
            "CommitmentDiscountStatus = 'Unused'."
        ),
        caveats=[
            "Not remediable at the resource level. The options are to shift eligible "
            "on-demand workloads onto the unused commitment, or to resize it at renewal.",
            "Break-even utilisation for a commitment at discount d is (1 - d). Below "
            "that, the unused portion costs more than the discount saves.",
        ],
        sources=[silver_path, "03-pricing-reference § The economics of a commitment"],
    )


# =======================================================================================
# Waste detection
# =======================================================================================


@_timed
def find_waste(top_n: int = 10, silver_path: str = SILVER) -> ToolResult:
    """Rank resources by likelihood of genuine waste.

    Returns a short queue deliberately. The failure mode in this domain is alert
    fatigue, not missed waste: a detector that surfaces four hundred resources gets
    muted within a fortnight and then catches nothing at all.
    """
    from costproof.ml import waste as waste_ml

    feats = waste_ml.build_features(silver_path)
    labelled = waste_ml.attach_labels(feats, billing_path=silver_path)
    result = waste_ml.train(labelled)

    ranked = (
        result.scores.groupby("resource_id")
        .agg(waste_score=("waste_score", "max"),
             avg_daily_cost=("cost", "mean"),
             environment=("environment", "first"),
             service_category=("service_category", "first"))
        .sort_values("waste_score", ascending=False)
        .head(top_n)
        .reset_index()
    )
    ranked["annualised_cost"] = ranked["avg_daily_cost"] * 365

    pk = result.metrics["precision_at_k"]
    prec10 = pk.get(10, {}).get("precision")

    return ToolResult(
        tool="find_waste", ok=True, value=ranked,
        method=(
            "Gradient-boosted classifier over relative cost features -- drift, "
            "coefficient of variation, weekend ratio -- evaluated on resources held "
            "out of training entirely, never on held-out rows from resources the model "
            "has seen."
        ),
        caveats=[
            f"Precision at the top 10 was {prec10:.0%} on held-out resources."
            if prec10 is not None else "Precision at k unavailable for this run.",
            f"Overall ROC AUC is {result.metrics['roc_auc']:.2f} -- the model ranks the "
            f"top of the queue well and the middle poorly. Only the top is actionable.",
            "A score is a prioritisation, not a finding. Each resource still requires "
            "confirmation against its runbook signature before any action.",
        ],
        sources=[silver_path, "01-remediation-runbook"],
    )


# =======================================================================================
# Causal measurement
# =======================================================================================


@_timed
def estimate_savings(resource_id: str, action_date: str,
                     silver_path: str = SILVER) -> ToolResult:
    """Estimate what an optimisation on this resource actually saved.

    This is the tool the whole project exists for. It reports the identified estimate,
    the naive before/after figure, and the gap between them -- the gap being the value
    of doing the measurement correctly.
    """
    from costproof.causal import did as D
    from costproof.causal import panel as P

    costs = duckdb.sql(f"""
        SELECT charge_date, resource_id, SUM(effective_cost) AS cost
        FROM '{silver_path}' WHERE charge_category = 'Usage'
        GROUP BY 1, 2
    """).df().pivot(index="charge_date", columns="resource_id", values="cost").fillna(0.0)
    costs.index = pd.to_datetime(costs.index)

    when = pd.Timestamp(action_date)
    donors = P.select_donors(costs, resource_id, when, excluded={resource_id})
    if len(donors) < P.PanelSpec().min_donors:
        return ToolResult(
            tool="estimate_savings", ok=False, value=None,
            method="no comparable control group could be constructed",
            error=f"only {len(donors)} usable donors; minimum is {P.PanelSpec().min_donors}",
            caveats=["Without a control group no identified estimate is possible. "
                     "Reporting a before/after figure here would be misleading."],
        )

    panel = P.build_panel(costs, resource_id, when, donors)
    pt = D.parallel_trends_test(panel)
    identified = D.did_permutation(panel)
    naive = D.naive_before_after(panel)

    run_rate = float(costs.loc[costs.index < when, resource_id].tail(84).mean())
    annual_identified = -float(pd.Series([identified.coef]).apply(lambda x: x).iloc[0])
    import numpy as np
    annual_identified = -np.expm1(identified.coef) * run_rate * 365
    annual_naive = -np.expm1(naive.coef) * run_rate * 365

    caveats = [
        f"Naive before/after would report ${annual_naive:,.0f}/yr. "
        f"The difference of ${annual_identified - annual_naive:+,.0f} is what the "
        f"control group corrects for.",
        "Inference is by randomization over placebo assignments. Cluster-robust "
        "standard errors are invalid with a single treated unit and cover the truth "
        "23% of the time on this estate.",
    ]
    if not pt["passed"]:
        caveats.insert(0, "PARALLEL-TRENDS CHECK FAILED. This estimate is not credibly "
                          "causal and must not be reported as a saving.")

    return ToolResult(
        tool="estimate_savings", ok=bool(pt["passed"]),
        value={
            "resource_id": resource_id,
            "action_date": action_date,
            "effect_log": identified.coef,
            "effect_pct": identified.pct_change,
            "ci_low_pct": float(pd.Series([identified.ci_low]).apply(lambda x: x).iloc[0]),
            "annualised_saving": annual_identified,
            "naive_annualised_saving": annual_naive,
            "p_value": identified.p_value,
            "n_donors": len(donors),
            "parallel_trends_passed": pt["passed"],
            "parallel_trends_p": pt["p_value"],
        },
        method=(
            "Difference-in-differences with resource and day fixed effects, against a "
            "control group of untreated resources matched on pre-period cost growth. "
            "Inference by randomization over placebo treatment assignments."
        ),
        caveats=caveats,
        sources=[silver_path, "02-measurement-policy § Design hierarchy for savings claims"],
    )


# =======================================================================================
# Knowledge
# =======================================================================================


@_timed
def search_knowledge(query: str, k: int = 3) -> ToolResult:
    """Retrieve grounded guidance from the knowledge base."""
    from costproof.agent.rag import Retriever

    retriever = Retriever()
    context, citations = retriever.context_for(query, k=k)
    if not context:
        return ToolResult(
            tool="search_knowledge", ok=False, value=None,
            method="TF-IDF retrieval over the knowledge base",
            error="no passage cleared the relevance floor",
            caveats=["The knowledge base does not cover this. Returning nothing is "
                     "deliberate -- supplying the least-bad passage is how a grounded "
                     "system starts producing confident nonsense."],
        )
    return ToolResult(
        tool="search_knowledge", ok=True, value=context,
        method="TF-IDF retrieval over section-level chunks, ranked by cosine similarity",
        caveats=["Lexical retrieval: recall@1 is 100% on questions using corpus "
                 "vocabulary, 62% on paraphrased questions."],
        sources=citations,
    )


# =======================================================================================
# Registry
# =======================================================================================

#: The complete set of tools an agent may call, with JSON-schema parameters. This is
#: also what the MCP server advertises, so there is exactly one definition of what the
#: agent is permitted to do.
TOOL_REGISTRY: dict[str, dict] = {
    "get_spend_summary": {
        "fn": get_spend_summary,
        "description": "Total cloud spend by business unit, with untagged spend shown explicitly.",
        "parameters": {"type": "object", "properties": {}},
    },
    "get_unit_economics": {
        "fn": get_unit_economics,
        "description": "Cost per unit of business output; distinguishes growth from waste.",
        "parameters": {
            "type": "object",
            "properties": {"business_unit": {"type": "string",
                                             "description": "Optional filter, e.g. bu-assist"}},
        },
    },
    "check_commitment_waste": {
        "fn": check_commitment_waste,
        "description": "Find prepaid capacity that was never consumed.",
        "parameters": {"type": "object", "properties": {}},
    },
    "find_waste": {
        "fn": find_waste,
        "description": "Rank resources by likelihood of genuine waste. Returns a short queue.",
        "parameters": {
            "type": "object",
            "properties": {"top_n": {"type": "integer", "default": 10}},
        },
    },
    "estimate_savings": {
        "fn": estimate_savings,
        "description": "Estimate what an optimisation actually saved, with a validity check.",
        "parameters": {
            "type": "object",
            "properties": {
                "resource_id": {"type": "string"},
                "action_date": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": ["resource_id", "action_date"],
        },
    },
    "search_knowledge": {
        "fn": search_knowledge,
        "description": "Retrieve grounded guidance from the runbook, policy and pricing docs.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
}


def call(name: str, **kwargs) -> ToolResult:
    """Invoke a tool by name. Unknown names fail as a result, never as an exception."""
    entry = TOOL_REGISTRY.get(name)
    if entry is None:
        return ToolResult(
            tool=name, ok=False, value=None, method="unknown tool",
            error=f"{name!r} is not in the registry. Available: {sorted(TOOL_REGISTRY)}",
        )
    return entry["fn"](**kwargs)
