"""A price book anchored to published public cloud list prices.

Every rate here is an approximate public on-demand list price (US East / equivalent,
as published 2025-2026). Exact figures move; magnitudes and *relative* prices are what
matter for this project, because the economics we are testing -- substitution between
resource classes, the size of commitment discounts, the dominance of GPU cost in an AI
estate -- depend on ratios rather than absolute cents.

Anchoring to real prices rather than inventing them matters: a reviewer who knows cloud
pricing will immediately see whether a synthetic estate is plausible, and an implausible
estate discredits everything downstream.
"""

from __future__ import annotations

from dataclasses import dataclass

from costproof.simulate.focus import ServiceCategory


@dataclass(frozen=True)
class Sku:
    """A purchasable unit with a published list price."""

    sku_id: str
    provider: str
    service_name: str
    service_category: ServiceCategory
    resource_type: str
    list_unit_price: float  # USD per pricing unit
    pricing_unit: str
    consumed_unit: str
    #: Typical negotiated discount off list for a large enterprise agreement.
    enterprise_discount: float = 0.0
    #: Whether this SKU can be covered by a commitment (RI / Savings Plan).
    commitment_eligible: bool = False
    #: Discount when covered by a 1-year commitment.
    commitment_discount: float = 0.0
    #: Rough relative volatility of demand for this SKU (multiplicative noise sd).
    demand_volatility: float = 0.12
    #: Baseline daily quantity for a unit-scale resource of this SKU, in PRICING units.
    #: Set explicitly per SKU rather than derived from the unit string, because deriving
    #: it let one $32/hr GPU SKU take 63% of estate spend and crowd out everything else.
    #: For GB-Months this is already prorated to a per-day share of a month.
    base_units_per_day: float = 24.0


# ---------------------------------------------------------------------------------------
# Compute
# ---------------------------------------------------------------------------------------

_COMPUTE = [
    Sku("aws-ec2-m6i-xlarge", "AWS", "Amazon EC2", ServiceCategory.COMPUTE,
        "m6i.xlarge", 0.1920, "Hours", "Hours",
        enterprise_discount=0.05, commitment_eligible=True, commitment_discount=0.28,
        demand_volatility=0.10, base_units_per_day=24 * 12),
    Sku("aws-ec2-c6i-2xlarge", "AWS", "Amazon EC2", ServiceCategory.COMPUTE,
        "c6i.2xlarge", 0.3400, "Hours", "Hours",
        enterprise_discount=0.05, commitment_eligible=True, commitment_discount=0.30,
        demand_volatility=0.14, base_units_per_day=24 * 8),
    Sku("aws-ec2-r6i-4xlarge", "AWS", "Amazon EC2", ServiceCategory.COMPUTE,
        "r6i.4xlarge", 1.0080, "Hours", "Hours",
        enterprise_discount=0.07, commitment_eligible=True, commitment_discount=0.32,
        demand_volatility=0.11, base_units_per_day=24 * 3),
    Sku("azure-vm-d8s-v5", "Azure", "Virtual Machines", ServiceCategory.COMPUTE,
        "Standard_D8s_v5", 0.3840, "Hours", "Hours",
        enterprise_discount=0.08, commitment_eligible=True, commitment_discount=0.29,
        demand_volatility=0.12, base_units_per_day=24 * 6),
    Sku("gcp-ce-n2-standard-8", "GCP", "Compute Engine", ServiceCategory.COMPUTE,
        "n2-standard-8", 0.3880, "Hours", "Hours",
        enterprise_discount=0.06, commitment_eligible=True, commitment_discount=0.27,
        demand_volatility=0.13, base_units_per_day=24 * 5),
    Sku("aws-fargate-vcpu", "AWS", "AWS Fargate", ServiceCategory.COMPUTE,
        "fargate-vcpu", 0.04048, "vCPU-Hours", "vCPU-Hours",
        enterprise_discount=0.03, commitment_eligible=True, commitment_discount=0.20,
        demand_volatility=0.22, base_units_per_day=24 * 70),
    Sku("aws-lambda-gbsec", "AWS", "AWS Lambda", ServiceCategory.COMPUTE,
        "lambda-gb-second", 0.0000166667, "GB-Seconds", "GB-Seconds",
        enterprise_discount=0.0, commitment_eligible=False,
        demand_volatility=0.30, base_units_per_day=24 * 3600 * 40),
]

# ---------------------------------------------------------------------------------------
# AI and Machine Learning -- the fastest growing and least governed category.
# 98% of organisations now manage AI spend, up from 31% in 2024 (State of FinOps 2026).
# ---------------------------------------------------------------------------------------

_AI_ML = [
    Sku("aws-sm-g5-2xlarge", "AWS", "Amazon SageMaker", ServiceCategory.AI_AND_ML,
        "ml.g5.2xlarge", 1.5150, "Hours", "Hours",
        enterprise_discount=0.05, commitment_eligible=True, commitment_discount=0.24,
        demand_volatility=0.26, base_units_per_day=24 * 1.6),
    Sku("aws-ec2-p4d-24xlarge", "AWS", "Amazon EC2", ServiceCategory.AI_AND_ML,
        "p4d.24xlarge", 32.7726, "Hours", "Hours",
        enterprise_discount=0.10, commitment_eligible=True, commitment_discount=0.35,
        demand_volatility=0.34, base_units_per_day=24 * 0.09),
    Sku("azure-ml-nc24ads", "Azure", "Azure Machine Learning", ServiceCategory.AI_AND_ML,
        "NC24ads_A100_v4", 3.6730, "Hours", "Hours",
        enterprise_discount=0.09, commitment_eligible=True, commitment_discount=0.31,
        demand_volatility=0.30, base_units_per_day=24 * 0.7),
    # Token-priced inference. Priced per 1K tokens, which is why AI cost allocation is
    # hard: the billing unit has no natural owner without application-level tagging.
    Sku("aws-bedrock-in-1k", "AWS", "Amazon Bedrock", ServiceCategory.AI_AND_ML,
        "foundation-model-input", 0.0030, "1K Tokens", "1K Tokens",
        enterprise_discount=0.0, commitment_eligible=False,
        demand_volatility=0.42, base_units_per_day=22_000.0),
    Sku("aws-bedrock-out-1k", "AWS", "Amazon Bedrock", ServiceCategory.AI_AND_ML,
        "foundation-model-output", 0.0150, "1K Tokens", "1K Tokens",
        enterprise_discount=0.0, commitment_eligible=False,
        demand_volatility=0.42, base_units_per_day=4_200.0),
    Sku("gcp-vertex-pred", "GCP", "Vertex AI", ServiceCategory.AI_AND_ML,
        "vertex-prediction-node", 0.7500, "Hours", "Hours",
        enterprise_discount=0.05, commitment_eligible=False,
        demand_volatility=0.28, base_units_per_day=24 * 3),
]

# ---------------------------------------------------------------------------------------
# Storage, Databases, Networking, Analytics
# GB-Months quantities are prorated per day (a 50 TB bucket bills ~1,640 GB-Months/day).
# ---------------------------------------------------------------------------------------

_STORAGE = [
    Sku("aws-s3-standard", "AWS", "Amazon S3", ServiceCategory.STORAGE,
        "s3-standard", 0.0230, "GB-Months", "GB-Months",
        enterprise_discount=0.04, demand_volatility=0.05, base_units_per_day=1_700.0),
    Sku("aws-ebs-gp3", "AWS", "Amazon EBS", ServiceCategory.STORAGE,
        "gp3", 0.0800, "GB-Months", "GB-Months",
        enterprise_discount=0.03, demand_volatility=0.04, base_units_per_day=520.0),
    Sku("aws-ebs-snapshot", "AWS", "Amazon EBS", ServiceCategory.STORAGE,
        "snapshot", 0.0500, "GB-Months", "GB-Months",
        enterprise_discount=0.0, demand_volatility=0.06, base_units_per_day=700.0),
    Sku("azure-blob-hot", "Azure", "Azure Blob Storage", ServiceCategory.STORAGE,
        "blob-hot-lrs", 0.0208, "GB-Months", "GB-Months",
        enterprise_discount=0.05, demand_volatility=0.05, base_units_per_day=1_900.0),
]

_DATABASES = [
    Sku("aws-rds-r6g-2xlarge", "AWS", "Amazon RDS", ServiceCategory.DATABASES,
        "db.r6g.2xlarge", 0.9600, "Hours", "Hours",
        enterprise_discount=0.06, commitment_eligible=True, commitment_discount=0.31,
        demand_volatility=0.08, base_units_per_day=24 * 2.2),
    Sku("aws-dynamodb-wcu", "AWS", "Amazon DynamoDB", ServiceCategory.DATABASES,
        "provisioned-wcu", 0.00065, "WCU-Hours", "WCU-Hours",
        enterprise_discount=0.0, commitment_eligible=True, commitment_discount=0.20,
        demand_volatility=0.18, base_units_per_day=24 * 3_500),
    Sku("gcp-cloudsql-db-n1-8", "GCP", "Cloud SQL", ServiceCategory.DATABASES,
        "db-n1-standard-8", 0.7840, "Hours", "Hours",
        enterprise_discount=0.05, commitment_eligible=True, commitment_discount=0.25,
        demand_volatility=0.09, base_units_per_day=24 * 2.6),
]

_NETWORKING = [
    Sku("aws-dto-internet", "AWS", "AWS Data Transfer", ServiceCategory.NETWORKING,
        "data-transfer-out", 0.0900, "GB", "GB",
        enterprise_discount=0.12, demand_volatility=0.20, base_units_per_day=380.0),
    Sku("aws-natgw-hours", "AWS", "Amazon VPC", ServiceCategory.NETWORKING,
        "nat-gateway", 0.0450, "Hours", "Hours",
        enterprise_discount=0.0, demand_volatility=0.03, base_units_per_day=24 * 20),
    Sku("aws-natgw-data", "AWS", "Amazon VPC", ServiceCategory.NETWORKING,
        "nat-gateway-data", 0.0450, "GB", "GB",
        enterprise_discount=0.0, demand_volatility=0.21, base_units_per_day=700.0),
]

_ANALYTICS = [
    Sku("aws-redshift-ra3-4xl", "AWS", "Amazon Redshift", ServiceCategory.ANALYTICS,
        "ra3.4xlarge", 3.2600, "Hours", "Hours",
        enterprise_discount=0.08, commitment_eligible=True, commitment_discount=0.33,
        demand_volatility=0.10, base_units_per_day=24 * 0.7),
    Sku("aws-athena-tb", "AWS", "Amazon Athena", ServiceCategory.ANALYTICS,
        "data-scanned", 5.0000, "TB", "TB",
        enterprise_discount=0.0, demand_volatility=0.35, base_units_per_day=9.0),
    Sku("gcp-bigquery-tb", "GCP", "BigQuery", ServiceCategory.ANALYTICS,
        "on-demand-query", 6.2500, "TB", "TB",
        enterprise_discount=0.05, demand_volatility=0.33, base_units_per_day=7.0),
]

PRICE_BOOK: tuple[Sku, ...] = tuple(
    _COMPUTE + _AI_ML + _STORAGE + _DATABASES + _NETWORKING + _ANALYTICS
)

SKU_INDEX: dict[str, Sku] = {s.sku_id: s for s in PRICE_BOOK}


def by_category(category: ServiceCategory) -> list[Sku]:
    return [s for s in PRICE_BOOK if s.service_category == category]


# ---------------------------------------------------------------------------------------
# Organisational structure
# ---------------------------------------------------------------------------------------

#: Business units that consume cloud, with the service mix each one skews toward and
#: the business driver whose volume determines their demand. The driver is what makes
#: unit economics computable -- without a denominator, a rising bill is uninterpretable.
BUSINESS_UNITS: tuple[dict, ...] = (
    {
        "id": "bu-checkout",
        "name": "Checkout Platform",
        "cost_centre": "CC-1010",
        "driver": "transactions",
        "categories": [ServiceCategory.COMPUTE, ServiceCategory.DATABASES,
                       ServiceCategory.NETWORKING],
        "seasonality": "retail",
    },
    {
        "id": "bu-search",
        "name": "Search & Discovery",
        "cost_centre": "CC-1020",
        "driver": "queries",
        "categories": [ServiceCategory.COMPUTE, ServiceCategory.ANALYTICS,
                       ServiceCategory.STORAGE],
        "seasonality": "retail",
    },
    {
        "id": "bu-assist",
        "name": "AI Assistant",
        "cost_centre": "CC-1030",
        "driver": "inferences",
        "categories": [ServiceCategory.AI_AND_ML, ServiceCategory.COMPUTE],
        "seasonality": "growth",
    },
    {
        "id": "bu-datasci",
        "name": "Data Science Platform",
        "cost_centre": "CC-1040",
        "driver": "training_jobs",
        "categories": [ServiceCategory.AI_AND_ML, ServiceCategory.ANALYTICS,
                       ServiceCategory.STORAGE],
        "seasonality": "workweek",
    },
    {
        "id": "bu-datalake",
        "name": "Enterprise Data Lake",
        "cost_centre": "CC-1050",
        "driver": "pipeline_runs",
        "categories": [ServiceCategory.ANALYTICS, ServiceCategory.STORAGE,
                       ServiceCategory.DATABASES],
        "seasonality": "workweek",
    },
    {
        "id": "bu-platform",
        "name": "Shared Platform Services",
        "cost_centre": "CC-1060",
        "driver": None,  # shared -- must be allocated, cannot be directly attributed
        "categories": [ServiceCategory.COMPUTE, ServiceCategory.NETWORKING,
                       ServiceCategory.MANAGEMENT],
        "seasonality": "flat",
    },
)

ENVIRONMENTS: tuple[tuple[str, float], ...] = (
    ("production", 0.62),
    ("staging", 0.16),
    ("development", 0.15),
    ("sandbox", 0.07),
)

REGIONS: tuple[tuple[str, str, str], ...] = (
    ("us-east-1", "US East (N. Virginia)", "AWS"),
    ("us-west-2", "US West (Oregon)", "AWS"),
    ("eu-west-1", "EU (Ireland)", "AWS"),
    ("eastus", "East US", "Azure"),
    ("us-central1", "US Central", "GCP"),
)
