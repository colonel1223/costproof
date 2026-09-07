"""The narrative guard: the model's prose is checked against the tool output, not trusted.

The first live watsonx run returned an empty narrative and the report shipped with a
header crediting the model. These tests pin the two behaviours that stop that class of
failure: an empty narrative is refused, and a narrative containing a figure absent from
the evidence is refused, with the offending figures named.
"""

from __future__ import annotations

import json

from costproof.agent import guard

EVIDENCE = json.dumps({
    "findings": [
        {"id": "F1", "title": "Spend with no identifiable owner",
         "annualised_impact_usd": 970158.37,
         "evidence": {"untagged_effective_cost": 1435302.11, "untagged_share": 0.106},
         "caveats": ["$1,435,302 (10.6%) has no business_unit tag"]},
        {"id": "F3", "title": "Prepaid capacity never consumed",
         "annualised_impact_usd": 165406.02, "evidence": {}, "caveats": []},
        {"id": "F4", "title": "Ten resources prioritised",
         "annualised_impact_usd": 212576.4,
         "evidence": {"roc_auc": 0.648, "precision_at_10": 1.0}, "caveats": []},
    ]
})


def test_empty_narrative_is_refused():
    r = guard.check_narrative("", EVIDENCE)
    assert not r.ok
    assert "0 character" in r.problems[0]


def test_whitespace_only_is_refused():
    assert not guard.check_narrative("   \n  ", EVIDENCE).ok


def test_grounded_narrative_passes_including_rounded_dollars_and_percent():
    text = ("Unattributed spend is $970,158 a year; $1,435,302 (10.6%) of the bill carries "
            "no owner tag. Unused commitment costs $165,406 annually. Ten resources carrying "
            "$212,576 are queued for review; the ranking model's ROC AUC is 0.65 and its "
            "precision at the top 10 was 100%.")
    r = guard.check_narrative(text, EVIDENCE)
    assert r.ok, r.problems
    assert r.figures_checked >= 5


def test_invented_figure_is_named_and_refused():
    text = ("Unattributed spend is $970,158 a year and unused commitment $165,406. "
            "Together with the $212,576 waste queue this totals $1,348,140 -- roughly "
            "$1.3M of annual exposure.")
    r = guard.check_narrative(text, EVIDENCE)
    assert not r.ok
    # The total is arithmetic the model performed; the evidence never states it.
    assert any("1,348,140" in f for f in r.figures_ungrounded)
    assert "1.3" in " ".join(r.figures_ungrounded)


def test_small_counts_are_not_treated_as_figures():
    text = ("Four findings, 3 requiring approval, across 7 tool calls and a 60-day window. "
            "Unattributed spend is $970,158.")
    r = guard.check_narrative(text, EVIDENCE)
    assert r.ok, r.problems
    assert r.figures_checked == 1


def test_thousands_rounding_is_not_accepted():
    """$970K is a rounding the model was told not to perform."""
    text = "Unattributed spend is roughly $970,000 a year, with $165,406 in unused commitment."
    r = guard.check_narrative(text, EVIDENCE)
    assert not r.ok
    assert r.figures_ungrounded == ["$970,000"]


def test_review_falls_back_and_records_rejection(monkeypatch):
    """End to end through CostReview with a backend that returns an empty string:
    the report must carry the deterministic narrative, name the rejection, and keep
    the header honest."""
    import pytest

    pytest.importorskip("duckdb")
    from pathlib import Path

    from costproof.agent import review as R

    if not (Path("data/silver/billing.parquet").exists()):
        pytest.skip("needs the silver layer")

    class EmptyBackend:
        name = "watsonx:fake-model"
        available = True

        def generate(self, prompt, system="", max_tokens=700):
            return ""

    # Stub the expensive waste tool so the test is quick; it is exercised elsewhere.
    monkeypatch.setattr(R.CostReview, "_review_waste", lambda self: None)
    cr = R.CostReview(backend=EmptyBackend(), verbose=False)
    audit = cr.run()
    assert audit.narrative_check["ok"] is False
    assert audit.narrative_source.startswith("deterministic (narrative from watsonx:fake-model rejected")
    assert "deterministic backend" in audit.narrative
    md = R.render_markdown(audit)
    assert "Grounding check FAILED" in md
    assert "## Narrative summary" in md
