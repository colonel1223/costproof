"""FOCUS 1.2 schema definition and conformance validation.

FOCUS (FinOps Open Cost and Usage Specification) is the FinOps Foundation's open
standard for normalising billing data across cloud providers. AWS, Azure, GCP and
OCI all publish FOCUS-conformant exports.

Building on FOCUS rather than inventing a schema is a deliberate choice:

1. Anything CostProof ingests works against real AWS/Azure/GCP exports unchanged.
2. The cost concepts that matter for economics -- BilledCost vs EffectiveCost vs
   ListCost -- are already defined precisely by the spec, so we inherit the
   industry's definitions instead of arguing about ours.
3. It is verifiable. A reviewer can check our columns against a published spec.

Reference: https://focus.finops.org/focus-specification/v1-2/
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import pandas as pd

# --------------------------------------------------------------------------------------
# Controlled vocabularies (FOCUS defines these as closed enumerations)
# --------------------------------------------------------------------------------------


class ChargeCategory(str, Enum):
    """Highest-level classification of a charge row."""

    USAGE = "Usage"
    PURCHASE = "Purchase"
    TAX = "Tax"
    CREDIT = "Credit"
    ADJUSTMENT = "Adjustment"


class ChargeFrequency(str, Enum):
    ONE_TIME = "One-Time"
    RECURRING = "Recurring"
    USAGE_BASED = "Usage-Based"


class PricingCategory(str, Enum):
    """How the unit price was arrived at."""

    STANDARD = "Standard"  # on-demand list
    DYNAMIC = "Dynamic"  # spot / preemptible
    COMMITTED = "Committed"  # covered by a commitment discount
    OTHER = "Other"


class CommitmentDiscountCategory(str, Enum):
    SPEND = "Spend"  # e.g. Savings Plans -- commit dollars/hour
    USAGE = "Usage"  # e.g. Reserved Instances -- commit capacity


class CommitmentDiscountStatus(str, Enum):
    USED = "Used"
    UNUSED = "Unused"  # commitment waste: paid for, not consumed


class ServiceCategory(str, Enum):
    """FOCUS service taxonomy (subset relevant to this estate)."""

    COMPUTE = "Compute"
    STORAGE = "Storage"
    DATABASES = "Databases"
    NETWORKING = "Networking"
    AI_AND_ML = "AI and Machine Learning"
    ANALYTICS = "Analytics"
    MANAGEMENT = "Management and Governance"


# --------------------------------------------------------------------------------------
# Column specification
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Column:
    name: str
    dtype: str
    required: bool
    description: str


#: The FOCUS columns CostProof implements. This is a working subset of the full 1.2
#: specification -- every column here matches the spec's name, type and meaning.
FOCUS_COLUMNS: tuple[Column, ...] = (
    # --- Billing period / invoice identity -------------------------------------------
    Column("BillingAccountId", "string", True, "Account the invoice is issued to."),
    Column("BillingAccountName", "string", False, "Display name of the billing account."),
    Column("SubAccountId", "string", False, "Account consuming the resource."),
    Column("SubAccountName", "string", False, "Display name of the sub account."),
    Column("BillingPeriodStart", "datetime64[ns]", True, "Inclusive start of billing period."),
    Column("BillingPeriodEnd", "datetime64[ns]", True, "Exclusive end of billing period."),
    Column("ChargePeriodStart", "datetime64[ns]", True, "Inclusive start of the charge."),
    Column("ChargePeriodEnd", "datetime64[ns]", True, "Exclusive end of the charge."),
    Column("BillingCurrency", "string", True, "ISO 4217 currency code."),
    # --- Cost measures ----------------------------------------------------------------
    # The distinction between these four is the whole of cloud cost accounting.
    Column("BilledCost", "float64", True, "What appears on the invoice for this period."),
    Column(
        "EffectiveCost",
        "float64",
        True,
        "Amortised cost including the period's share of prepaid commitments. "
        "This is the correct measure for unit economics and for causal analysis, "
        "because BilledCost puts a whole prepayment in one period and would read "
        "as a spend shock that no optimisation caused.",
    ),
    Column("ListCost", "float64", True, "Cost at public list price, before any discount."),
    Column("ContractedCost", "float64", True, "Cost at negotiated rate, before commitments."),
    # --- What was bought ---------------------------------------------------------------
    Column("ProviderName", "string", True, "Entity that provided the resource."),
    Column("PublisherName", "string", False, "Entity that produced the offering."),
    Column("InvoiceIssuerName", "string", True, "Entity that issued the invoice."),
    Column("ServiceName", "string", True, "Provider's name for the service."),
    Column("ServiceCategory", "string", True, "FOCUS service taxonomy category."),
    Column("ResourceId", "string", False, "Unique identifier of the resource."),
    Column("ResourceName", "string", False, "Display name of the resource."),
    Column("ResourceType", "string", False, "Provider's type label for the resource."),
    Column("RegionId", "string", False, "Provider region identifier."),
    Column("RegionName", "string", False, "Region display name."),
    Column("AvailabilityZone", "string", False, "Zone within the region."),
    # --- Charge classification ----------------------------------------------------------
    Column("ChargeCategory", "string", True, "Usage | Purchase | Tax | Credit | Adjustment."),
    Column("ChargeClass", "string", False, "'Correction' when this row restates a prior period."),
    Column("ChargeDescription", "string", True, "Human-readable description of the charge."),
    Column("ChargeFrequency", "string", False, "One-Time | Recurring | Usage-Based."),
    # --- Quantities and prices ----------------------------------------------------------
    Column("ConsumedQuantity", "float64", False, "Amount of the service actually consumed."),
    Column("ConsumedUnit", "string", False, "Unit of ConsumedQuantity."),
    Column("PricingQuantity", "float64", False, "Quantity the price is applied to."),
    Column("PricingUnit", "string", False, "Unit of PricingQuantity."),
    Column("ListUnitPrice", "float64", False, "Public list price per pricing unit."),
    Column("ContractedUnitPrice", "float64", False, "Negotiated price per pricing unit."),
    Column("PricingCategory", "string", False, "Standard | Dynamic | Committed | Other."),
    Column("SkuId", "string", False, "Provider SKU identifier."),
    Column("SkuPriceId", "string", False, "Identifier for the specific price applied."),
    # --- Commitment discounts ------------------------------------------------------------
    Column("CommitmentDiscountId", "string", False, "Identifier of the commitment applied."),
    Column("CommitmentDiscountName", "string", False, "Display name of the commitment."),
    Column("CommitmentDiscountCategory", "string", False, "Spend | Usage."),
    Column("CommitmentDiscountType", "string", False, "Provider's commitment product name."),
    Column("CommitmentDiscountStatus", "string", False, "Used | Unused."),
    # --- Allocation --------------------------------------------------------------------
    Column("Tags", "object", False, "Key/value map applied to the resource."),
)

REQUIRED_COLUMNS: tuple[str, ...] = tuple(c.name for c in FOCUS_COLUMNS if c.required)
ALL_COLUMNS: tuple[str, ...] = tuple(c.name for c in FOCUS_COLUMNS)
COLUMN_INDEX: dict[str, Column] = {c.name: c for c in FOCUS_COLUMNS}


# --------------------------------------------------------------------------------------
# Conformance validation
# --------------------------------------------------------------------------------------


class FocusConformanceError(ValueError):
    """Raised when a dataframe violates the FOCUS specification.

    This raises rather than warning. A silently malformed billing feed produces
    confidently wrong cost figures, which is the failure mode this project exists
    to eliminate -- so bad data must stop the pipeline, not flow through it.
    """


def validate(df: pd.DataFrame, *, strict: bool = True) -> list[str]:
    """Check a dataframe against the FOCUS specification.

    Args:
        df: The billing dataframe to validate.
        strict: If True, raise on any violation. If False, return the list of
            violations so a caller can report them.

    Returns:
        A list of human-readable violation messages (empty when conformant).

    Raises:
        FocusConformanceError: If ``strict`` and any violation is found.
    """
    problems: list[str] = []

    # 1. Required columns present
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        problems.append(f"missing required columns: {sorted(missing)}")

    # 2. No unknown columns (catches typos that would silently drop data downstream)
    unknown = [c for c in df.columns if c not in COLUMN_INDEX]
    if unknown:
        problems.append(f"columns not in the FOCUS spec: {sorted(unknown)}")

    # 3. Cost measures must be present and finite
    for col in ("BilledCost", "EffectiveCost", "ListCost", "ContractedCost"):
        if col not in df.columns:
            continue
        if df[col].isna().any():
            problems.append(f"{col} contains nulls ({int(df[col].isna().sum())} rows)")
        if not pd.api.types.is_numeric_dtype(df[col]):
            problems.append(f"{col} must be numeric, found {df[col].dtype}")

    # 4. Controlled vocabularies
    vocab_checks: list[tuple[str, set[str]]] = [
        ("ChargeCategory", {c.value for c in ChargeCategory}),
        ("ChargeFrequency", {c.value for c in ChargeFrequency}),
        ("PricingCategory", {c.value for c in PricingCategory}),
        ("ServiceCategory", {c.value for c in ServiceCategory}),
        ("CommitmentDiscountCategory", {c.value for c in CommitmentDiscountCategory}),
        ("CommitmentDiscountStatus", {c.value for c in CommitmentDiscountStatus}),
    ]
    for col, allowed in vocab_checks:
        if col not in df.columns:
            continue
        seen = set(df[col].dropna().unique())
        invalid = seen - allowed
        if invalid:
            problems.append(f"{col} has values outside the FOCUS vocabulary: {sorted(invalid)}")

    # 5. Charge periods must be well-formed
    if {"ChargePeriodStart", "ChargePeriodEnd"} <= set(df.columns):
        bad = (df["ChargePeriodEnd"] <= df["ChargePeriodStart"]).sum()
        if bad:
            problems.append(f"ChargePeriodEnd <= ChargePeriodStart on {int(bad)} rows")

    # 6. Economic coherence. ContractedCost is negotiated-rate cost before commitment
    #    discounts, so it can never exceed list. A violation here means the price
    #    model is broken, which would corrupt every downstream savings estimate.
    if {"ListCost", "ContractedCost"} <= set(df.columns):
        tol = 1e-6
        bad = (df["ContractedCost"] > df["ListCost"] + tol).sum()
        if bad:
            problems.append(f"ContractedCost exceeds ListCost on {int(bad)} rows")

    # 7. Single currency (multi-currency needs an FX policy we have not defined)
    if "BillingCurrency" in df.columns and df["BillingCurrency"].nunique() > 1:
        problems.append("multiple billing currencies present; no FX policy is defined")

    if problems and strict:
        raise FocusConformanceError(
            "FOCUS conformance failed:\n  - " + "\n  - ".join(problems)
        )
    return problems


def empty_frame() -> pd.DataFrame:
    """Return an empty dataframe with the full FOCUS column set and correct dtypes."""
    return pd.DataFrame(
        {
            c.name: pd.Series(dtype=("object" if c.dtype == "object" else c.dtype))
            for c in FOCUS_COLUMNS
        }
    )
