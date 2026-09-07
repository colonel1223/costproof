# Cloud cost review

**Run** `review-20260907T195526Z`  ·  **Reporting backend** `watsonx:ibm/granite-4-h-small`  ·  **Generated** 2026-09-07T19:55:26Z

> Every figure below is produced by a deterministic analytical tool. The language model, when configured, rewrites this summary into prose and changes no number, method, or recommendation. Run with no model and the figures are identical.

---

## Findings

### F1 — Spend with no identifiable owner

| | |
|---|---|
| Category | allocation |
| Annualised impact | **$970,158** |
| Governance | 🔒 requires human approval |
| Gate reason | impact >= $1,000/yr; owner cannot be established from the billing feed |

**How this was measured.** SUM(EffectiveCost) grouped by business unit over Usage rows. EffectiveCost is used rather than BilledCost because committed usage carries zero billed cost -- the money left when the commitment was purchased -- which would make prepaid resources appear free.

**Recommended action.** Not a cost fix — a governance fix. Identify the owner from the account, region and naming convention, apply tags, and enforce tagging at provisioning time through policy.

**Risk.** None. But untagged spend cannot be allocated, so it cannot appear in any unit-economics calculation, and no team's budget carries it. Nobody has a reason to reduce it.

**Caveats.**

- $1,435,302 (10.6%) has no business_unit tag and cannot be attributed to any team from the billing feed alone.
- This is not recoverable spend. It is spend that cannot be attributed, so no team's budget carries it and no team has reason to reduce it.

*Sources: data/silver/billing.parquet*

### F2 — Four of five units grew more efficient; one degraded

| | |
|---|---|
| Category | unit economics |
| Annualised impact | **not quantified** |
| Governance | ✅ auto-approvable |
| Gate reason | below all gates |

**How this was measured.** Compares the first 60 days to the last 60 days, for both total cost and cost per 1,000 units of business output. A rising bill with falling unit cost is growth, not waste.

**Recommended action.** investigate_efficiency_regression

**Risk.** None. This is an investigation, not a change.

**Caveats.**

- Only bu-datasci shows genuinely degrading efficiency; the others' bills rose while their unit costs fell.
- 'unallocated' and 'bu-platform' are excluded: neither has a business driver, so a unit cost for them would have a fabricated denominator.
- Every unit's total bill rose. Judged on the invoice alone all five look like problems; judged per unit of output, only one is.

*Sources: data/gold/unit_economics.parquet*

### F3 — Prepaid capacity never consumed

| | |
|---|---|
| Category | commitment |
| Annualised impact | **$165,406** |
| Governance | 🔒 requires human approval |
| Gate reason | impact >= $1,000/yr |

**How this was measured.** Sums EffectiveCost on rows where FOCUS reports CommitmentDiscountStatus = 'Unused'.

**Recommended action.** Not a resource-level fix. Either shift eligible on-demand workloads onto the unused commitment, or resize the commitment at renewal. Some providers permit selling unused reservations on a marketplace.

**Risk.** None from analysis. The commitment is already sunk; the only decision is whether to renew at the same level.

**Caveats.**

- Not remediable at the resource level. The options are to shift eligible on-demand workloads onto the unused commitment, or to resize it at renewal.
- Break-even utilisation for a commitment at discount d is (1 - d). Below that, the unused portion costs more than the discount saves.

*Sources: data/silver/billing.parquet, 03-pricing-reference § The economics of a commitment*

### F4 — Ten resources prioritised for investigation

| | |
|---|---|
| Category | waste detection |
| Annualised impact | **$212,576** |
| Governance | 🔒 requires human approval |
| Gate reason | impact >= $1,000/yr |

**How this was measured.** Gradient-boosted classifier over relative cost features -- drift, coefficient of variation, weekend ratio -- evaluated on resources held out of training entirely, never on held-out rows from resources the model has seen.

**Recommended action.** Rightsize to the next smaller instance family, or enable autoscaling with a floor at observed p50 utilisation.

**Risk.** Rightsizing on an unrepresentative window under-provisions for real peaks. Require at least 28 days of observation covering a month-end close before acting on a production resource.

**Caveats.**

- Precision at the top 10 was 100% on held-out resources.
- Overall ROC AUC is 0.64 -- the model ranks the top of the queue well and the middle poorly. Only the top is actionable.
- A score is a prioritisation, not a finding. Each resource still requires confirmation against its runbook signature before any action.
- The annualised figure is the cost these resources CARRY, not a saving. What is recoverable depends on the remediation and is unknown until each is measured after the fact.

*Sources: data/silver/billing.parquet, 01-remediation-runbook*

---

## Governance summary

- **4** findings, **3** requiring human approval
- **7** tool calls, all recorded with arguments and timings
- **$1,348,140** total annualised impact identified

No action in this report has been executed. Every gated finding requires explicit human approval before anything changes.

## Tool call log

| Tool | Arguments | OK | ms |
|---|---|---|---:|
| `get_spend_summary` | — | ✅ | 29 |
| `search_knowledge` | query=untagged spend with no business unit tag | ✅ | 1816 |
| `get_unit_economics` | — | ✅ | 6 |
| `check_commitment_waste` | — | ✅ | 8 |
| `search_knowledge` | query=unused commitment prepaid capacity never consumed | ✅ | 32 |
| `find_waste` | top_n=10 | ✅ | 59236 |
| `search_knowledge` | query=idle over-provisioned instance flat cost no weekend dip | ✅ | 23 |

## Sources retrieved

- 01-remediation-runbook
- 01-remediation-runbook § Idle over-provisioned compute
- 01-remediation-runbook § Orphaned storage volumes and snapshots
- 01-remediation-runbook § Untagged spend
- 01-remediation-runbook § Unused commitment
- 01-remediation-runbook § Zombie non-production environments
- 02-measurement-policy § Human approval gate
- 02-measurement-policy § Which cost measure to use
- 03-pricing-reference § AI and machine learning
- 03-pricing-reference § The economics of a commitment
- data/gold/unit_economics.parquet
- data/silver/billing.parquet

---

## Narrative summary
*Generated by `deterministic (narrative from watsonx:ibm/granite-4-h-small rejected: 3 figure(s) not present in tool output: $970,157, 61.7%, 12.2%)`*

*Grounding check FAILED: every figure in the narrative must appear in the tool output the model was shown (6 checked; ungrounded: $970,157, 61.7%, 12.2%).*

[deterministic backend -- no language model configured]

Findings and figures below are produced entirely by the analytical tools. A language model, when configured, only rewrites this into prose; it changes no number, no method, and no recommendation.

<details><summary>Rejected model narrative (kept for audit)</summary>

Spend with no identifiable owner accounts for $970,157 annually (10.6% of total). This is unattributed spend that cannot be recovered; a governance fix is required to tag the responsible business unit.

Four of five business units showed efficiency gains, but bu-datasci's cost grew 61.7% despite a 12.2% drop in unit cost. All units' total bills increased, but only bu-datasci's efficiency degraded.

$165,406 annually is tied up in prepaid capacity never consumed. This cannot be fixed at the resource level; options are to shift eligible workloads onto the unused commitment or resize it at renewal.

A model identified ten high-priority resources with a combined $212,576 annualised cost. These require investigation and possible rightsizing or autoscaling, but the exact recoverable amount depends on remediation.

</details>
