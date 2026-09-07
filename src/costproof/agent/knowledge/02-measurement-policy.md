# Measurement policy

The rules governing what CostProof will and will not claim. The agent retrieves from
this when asked how a number was produced, or whether a claim can be made.

## Which cost measure to use

Always `EffectiveCost` for economic analysis.

`BilledCost` shows zero on committed usage, because the money left months earlier when
the commitment was purchased. Analysing `BilledCost` per resource makes prepaid
resources appear free. They are not.

`ListCost` is the public sticker price and is used only to compute discount rates.
`ContractedCost` is the negotiated rate before commitment discounts apply.

## Design hierarchy for savings claims

Every savings estimate must state which design produced it. In descending order of
credibility:

1. **Difference-in-differences** — requires a comparable untreated control group.
   Strongest available design.
2. **Synthetic control** — requires a donor pool. Used when no single resource is
   comparable to the treated one.
3. **Bayesian structural time series** — requires only pre-period history. Weakest;
   must be labelled as such in any output.
4. **Before/after** — **not a valid design.** Computed only to show the gap between it
   and an identified estimate. That gap is the value of doing the measurement correctly.

## Inference with a single treated unit

Cluster-robust standard errors are consistent as the number of *treated* clusters grows.
With one treated resource they are severely downward biased, and their intervals cover
the truth far less often than they claim.

Randomization inference is required: re-estimate the same specification treating each
untreated donor as if it were treated, and use the spread of that distribution as the
scale of uncertainty.

Measured on this estate: cluster-robust intervals covered the true effect 23% of the
time; randomization inference covered it 91% of the time, on identical point estimates.

## Validity gate

No savings figure is reported as causal unless the parallel-trends check passes.

The check itself uses randomization inference, not a Wald test built on cluster-robust
standard errors — a diagnostic inherits every weakness of the variance estimate beneath
it. A Wald version of this test rejected 98.3% of designs, including valid ones.

Measured on this estate: estimates passing the check had RMSE 0.151 and 95.1% interval
coverage. Estimates failing it had RMSE 0.402 and 64.3% coverage. The gate works, and
failing estimates must be withheld rather than reported with a caveat.

## What the language model may and may not do

The model may:

- decide which analytical tool to call
- summarise what a tool returned
- retrieve and cite relevant runbook or policy text
- draft a remediation ticket for human review

The model may **not**:

- compute, estimate, or restate any dollar figure
- assert causality about any intervention
- choose an identification strategy
- approve or execute a remediation

Every number in any output originates from deterministic code. This mirrors the design
rule from the Tariff Exposure Agent: the model decides which rule applies; the code
decides what it costs.

## Human approval gate

Any recommendation with an estimated impact above $1,000 annually, or any irreversible
action (volume deletion, commitment purchase, job termination), requires explicit human
approval before execution.

Actions on untagged resources always require approval, because ownership cannot be
established from the billing feed and the approver must identify the owner.

## Audit record

Every recommendation is logged with: the tools called and their arguments, the retrieved
sources and their relevance scores, the identification strategy used, the parallel-trends
result, the point estimate with its interval, the model and prompt version, and the
approval decision with its timestamp.

An estimate that cannot be reproduced from its audit record is a defect.
