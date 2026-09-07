"""Tests for the billing simulator.

These are not decorative. The simulator defines ground truth for every downstream
claim in the project, so a silent regression here would invalidate every number in
the README. Several of these tests encode bugs that were actually found and fixed
during development -- they exist to stop those bugs coming back.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from costproof.simulate import focus
from costproof.simulate.generator import SimConfig, generate
from costproof.simulate.pricebook import PRICE_BOOK


@pytest.fixture(scope="module")
def estate():
    # A smaller estate so the suite stays fast; the mechanisms are identical.
    return generate(SimConfig(n_resources=90, n_waste_events=32, n_interventions=26))


# ---------------------------------------------------------------------------------------
# FOCUS conformance
# ---------------------------------------------------------------------------------------


def test_billing_is_focus_conformant(estate):
    assert focus.validate(estate.billing, strict=False) == []


def test_no_columns_outside_the_spec(estate):
    assert set(estate.billing.columns) <= set(focus.ALL_COLUMNS)


def test_cost_measures_are_finite_and_non_negative(estate):
    for col in ("BilledCost", "EffectiveCost", "ListCost", "ContractedCost"):
        s = estate.billing[col]
        assert np.isfinite(s).all(), f"{col} has non-finite values"
        assert (s >= -1e-9).all(), f"{col} has negative values"


def test_contracted_never_exceeds_list(estate):
    """A negotiated rate above list price would mean the discount model is inverted,
    which would corrupt every savings figure downstream."""
    b = estate.billing
    assert (b["ContractedCost"] <= b["ListCost"] + 1e-6).all()


# ---------------------------------------------------------------------------------------
# Determinism and reproducibility
# ---------------------------------------------------------------------------------------


def test_generation_is_deterministic():
    cfg = SimConfig(n_resources=40, n_waste_events=10, n_interventions=8)
    a, b = generate(cfg), generate(cfg)
    pd.testing.assert_frame_equal(a.billing, b.billing)
    assert [i.true_effect_log for i in a.interventions] == [
        i.true_effect_log for i in b.interventions
    ]


def test_different_seeds_give_different_estates():
    a = generate(SimConfig(seed=1, n_resources=40, n_waste_events=10, n_interventions=8))
    b = generate(SimConfig(seed=2, n_resources=40, n_waste_events=10, n_interventions=8))
    assert a.billing["EffectiveCost"].sum() != b.billing["EffectiveCost"].sum()


def test_config_fingerprint_is_stable_and_sensitive():
    assert SimConfig().fingerprint() == SimConfig().fingerprint()
    assert SimConfig(seed=1).fingerprint() != SimConfig(seed=2).fingerprint()


# ---------------------------------------------------------------------------------------
# Tagging -- regression test for a real bug
# ---------------------------------------------------------------------------------------


def test_some_spend_is_genuinely_untagged(estate):
    """REGRESSION: pandas coerces None in an object column to NaN, and `NaN is not None`
    is True. The original tag filter used `v is not None`, so every untagged resource was
    silently tagged with a NaN owner and the estate reported a 0% untagged rate -- which
    would have made the entire cost-allocation problem disappear."""
    usage = estate.billing[estate.billing["ChargeCategory"] == "Usage"]
    untagged = usage["Tags"].apply(lambda d: "business_unit" not in d)
    share = usage.loc[untagged, "EffectiveCost"].sum() / usage["EffectiveCost"].sum()
    assert 0.02 < share < 0.45, f"untagged share {share:.1%} is implausible"


def test_tag_values_are_never_nan(estate):
    for tags in estate.billing["Tags"].head(5000):
        for k, v in tags.items():
            assert isinstance(v, str), f"tag {k} has non-string value {v!r}"


# ---------------------------------------------------------------------------------------
# Estate plausibility
# ---------------------------------------------------------------------------------------


def test_no_single_service_category_dominates(estate):
    """REGRESSION: deriving daily quantity from the pricing unit let one $32/hr GPU SKU
    take 63% of estate spend, which is not a plausible enterprise and would discredit
    every downstream figure."""
    usage = estate.billing[estate.billing["ChargeCategory"] == "Usage"]
    shares = usage.groupby("ServiceCategory")["EffectiveCost"].sum()
    shares = shares / shares.sum()
    assert shares.max() < 0.45, f"category mix is too concentrated:\n{shares}"
    assert len(shares) >= 5


def test_spend_is_heavy_tailed(estate):
    """Real estates are dominated by a small number of resources. If spend were uniform,
    donor-pool selection for synthetic control would be trivially easy and the method
    would not be tested properly."""
    usage = estate.billing[estate.billing["ChargeCategory"] == "Usage"]
    per_resource = usage.groupby("ResourceId")["EffectiveCost"].sum().sort_values()
    top_decile = per_resource.tail(max(1, len(per_resource) // 10)).sum()
    assert top_decile / per_resource.sum() > 0.25


def test_commitment_discounts_actually_reduce_cost(estate):
    usage = estate.billing[estate.billing["ChargeCategory"] == "Usage"]
    committed = usage[usage["PricingCategory"] == "Committed"]
    assert len(committed) > 0
    assert (committed["EffectiveCost"] <= committed["ContractedCost"] + 1e-9).all()


def test_committed_usage_has_zero_billed_cost(estate):
    """Committed usage is prepaid, so the usage row shows no billed cost. Anyone
    analysing BilledCost per resource would conclude these resources are free. That is
    exactly why EffectiveCost is the correct measure for unit economics."""
    usage = estate.billing[estate.billing["ChargeCategory"] == "Usage"]
    committed = usage[usage["PricingCategory"] == "Committed"]
    assert (committed["BilledCost"] == 0).all()
    assert (committed["EffectiveCost"] > 0).all()


def test_unused_commitment_is_recorded_as_waste(estate):
    purchases = estate.billing[estate.billing["ChargeCategory"] == "Purchase"]
    unused = purchases[purchases["CommitmentDiscountStatus"] == "Unused"]
    assert len(unused) > 0, "no unused commitment generated"
    assert unused["EffectiveCost"].sum() > 0


# ---------------------------------------------------------------------------------------
# Ground truth integrity -- the tests that matter most
# ---------------------------------------------------------------------------------------


def test_null_interventions_have_exactly_zero_true_effect(estate):
    for iv in estate.interventions:
        if iv.is_null:
            assert iv.true_effect_log == 0.0
            assert iv.true_effect_own == 0.0
            assert iv.waste_removed_log == 0.0
            assert iv.resolved_waste_id is None


def test_null_interventions_land_on_waste_free_resources(estate):
    """REGRESSION: null interventions were originally allowed on resources carrying
    transient waste (a runaway job lasting up to 95 days). The waste kept billing through
    the post-window, so the 'no-effect' cases showed spend RISING and the naive estimator
    looked biased in the wrong direction. That was an artefact of the simulator, not a
    property of the estimator."""
    wasted = {w.resource_id for w in estate.waste_events}
    for iv in estate.interventions:
        if iv.is_null:
            assert iv.resource_id not in wasted


def test_true_effect_decomposes_correctly(estate):
    for iv in estate.interventions:
        assert iv.true_effect_log == pytest.approx(
            iv.true_effect_own - iv.waste_removed_log
        )


def test_real_interventions_reduce_cost(estate):
    for iv in estate.interventions:
        if not iv.is_null:
            assert iv.true_effect_log < 0.0


def test_resolved_waste_events_are_truncated(estate):
    by_id = {w.waste_id: w for w in estate.waste_events}
    for iv in estate.interventions:
        if iv.resolved_waste_id:
            w = by_id[iv.resolved_waste_id]
            assert w.resolved_by == iv.intervention_id
            assert w.end_day == iv.start_day


def test_one_intervention_per_resource(estate):
    ids = [iv.resource_id for iv in estate.interventions]
    assert len(ids) == len(set(ids))


def test_interventions_leave_room_for_estimation(estate):
    cfg = estate.config
    for iv in estate.interventions:
        assert iv.start_day >= cfg.min_pre_days
        assert iv.start_day <= cfg.n_days - cfg.min_post_days


def test_null_interventions_are_triggered_by_real_spikes(estate):
    """They must be genuine spikes -- otherwise there is no mean reversion to demonstrate
    -- but not so extreme that they could only come from an injected waste event."""
    zs = [iv.trigger_z for iv in estate.interventions if iv.is_null]
    assert zs, "no null interventions generated"
    assert min(zs) > 1.0
    assert max(zs) < 5.0, f"z={max(zs):.1f} is too extreme for AR(1) noise"


# ---------------------------------------------------------------------------------------
# Price book
# ---------------------------------------------------------------------------------------


def test_price_book_is_well_formed():
    assert len({s.sku_id for s in PRICE_BOOK}) == len(PRICE_BOOK)
    for s in PRICE_BOOK:
        assert s.list_unit_price > 0
        assert s.base_units_per_day > 0
        assert 0.0 <= s.enterprise_discount < 0.5
        assert 0.0 <= s.commitment_discount < 0.6
        if s.commitment_discount > 0:
            assert s.commitment_eligible


def test_no_sku_dominates_daily_cost():
    daily = [s.list_unit_price * s.base_units_per_day for s in PRICE_BOOK]
    assert max(daily) / min(daily) < 12, "SKU daily costs span too wide a range"
