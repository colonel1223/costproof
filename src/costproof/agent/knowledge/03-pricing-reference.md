# Cloud pricing reference

Approximate public on-demand list prices, US East or equivalent, as published 2025-2026.
Prices move; ratios between resource classes move far less, and it is the ratios that
drive substitution decisions.

## Compute

| SKU | Provider | List price | Commitment discount |
|---|---|---|---|
| `m6i.xlarge` | AWS | $0.192/hour | ~28% (1yr) |
| `c6i.2xlarge` | AWS | $0.340/hour | ~30% |
| `r6i.4xlarge` | AWS | $1.008/hour | ~32% |
| `Standard_D8s_v5` | Azure | $0.384/hour | ~29% |
| `n2-standard-8` | GCP | $0.388/hour | ~27% |
| Fargate vCPU | AWS | $0.04048/vCPU-hour | ~20% |
| Lambda | AWS | $0.0000166667/GB-second | not eligible |

## AI and machine learning

| SKU | Provider | List price | Commitment discount |
|---|---|---|---|
| `ml.g5.2xlarge` | AWS SageMaker | $1.515/hour | ~24% |
| `p4d.24xlarge` | AWS EC2 | $32.7726/hour | ~35% |
| `NC24ads_A100_v4` | Azure ML | $3.673/hour | ~31% |
| Bedrock foundation model input | AWS | $0.003 per 1K tokens | not eligible |
| Bedrock foundation model output | AWS | $0.015 per 1K tokens | not eligible |
| Vertex AI prediction node | GCP | $0.750/hour | not eligible |

**Note on token pricing.** Output tokens cost roughly 5x input tokens. Token-priced
inference cannot be covered by a commitment discount, which is why AI spend is harder
to control than compute spend: the usual lever does not exist. It also has no natural
owner in the billing feed — a token has no resource tag — so allocating AI cost to a
business unit requires application-level instrumentation that most organisations have
not built.

## Storage

| SKU | Provider | List price |
|---|---|---|
| S3 Standard | AWS | $0.023/GB-month |
| EBS gp3 | AWS | $0.080/GB-month |
| EBS snapshot | AWS | $0.050/GB-month |
| Blob hot LRS | Azure | $0.0208/GB-month |

## Databases, networking, analytics

| SKU | Provider | List price |
|---|---|---|
| `db.r6g.2xlarge` RDS | AWS | $0.960/hour |
| DynamoDB provisioned WCU | AWS | $0.00065/WCU-hour |
| `db-n1-standard-8` Cloud SQL | GCP | $0.784/hour |
| Data transfer out to internet | AWS | $0.090/GB |
| NAT Gateway | AWS | $0.045/hour + $0.045/GB |
| `ra3.4xlarge` Redshift | AWS | $3.260/hour |
| Athena | AWS | $5.00/TB scanned |
| BigQuery on-demand | GCP | $6.25/TB scanned |

## Discount structure

Three layers stack, and they are frequently confused:

1. **Enterprise agreement discount** — negotiated percentage off list, applies to
   everything. Typically 3-12% depending on committed annual volume.
2. **Commitment discount** — Reserved Instances or Savings Plans. 20-35% for promising
   1-3 years of capacity or spend.
3. **Spot / preemptible** — up to 90% off, but the provider can reclaim the capacity
   with minutes of notice. Only viable for interruptible work.

**Effective Savings Rate** is `(ListCost − EffectiveCost) / ListCost` — the share of
sticker price actually avoided across all three layers. It is the headline KPI FinOps
teams report to finance.

## The economics of a commitment

A commitment is a forward contract on capacity under demand uncertainty. Committing
converts a variable cost into a fixed one in exchange for a discount, which is
worthwhile only if expected utilisation exceeds the break-even point.

Break-even utilisation for a 1-year commitment at discount `d` is `1 − d`. At a 30%
discount, a commitment pays off if it is used more than 70% of the time. Below that,
the unused portion costs more than the discount saved.

This is why unused commitment is treated as waste rather than as a rounding error: it is
the realised loss on a bet about future demand.
