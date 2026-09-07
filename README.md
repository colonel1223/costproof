# CostProof

Measures whether cloud cost optimisations actually saved money.

Cloud cost tools report what was spent. When a team fixes something and the bill goes down,
the standard way to credit the fix is before/after: spend was $10K, now it's $7K, we saved
$3K. That number is usually wrong. Teams don't fix things at random; they fix whatever just
spiked, and spikes revert on their own. Before/after credits the team for the reversion.

CostProof treats each optimisation as a treatment and estimates its effect against a control
group of untouched resources, using difference-in-differences with permutation inference. It
runs on a simulated billing estate where the true effect of every intervention is known, so it
can report how accurate its own estimates are. The estate conforms to FOCUS 1.2, the billing
schema AWS, Azure and GCP publish, so the same code runs on a real export.

## Quick start

```bash
pip install -e ".[dev,agent,report]"
python -m costproof.cli data          # generate the estate (216,036 rows, ~5s)
python -m costproof.cli warehouse     # bronze -> silver -> gold
python -m costproof.cli study         # estimate 117 interventions four ways, score vs truth
python -m costproof.cli review        # the governed cost review -> reports/cost-review.md
pytest                                # 54 tests
```

Everything is deterministic given the seed in `SimConfig`. Data is regenerated, not committed.
watsonx is optional: with no `.env` the review runs on a deterministic backend and produces the
same figures.

## Results

117 interventions on a simulated cloud estate of roughly $9M a year, 30 of them with a true
effect of exactly zero (actions taken on resources that had merely spiked). Each estimator is
scored against the known effect.

| Method | Bias | RMSE | 95% coverage | False positives on null interventions | Power |
|---|---:|---:|---:|---:|---:|
| Naive before/after | 0.098 | 0.238 | 27% | 80% | 100% |
| DiD, cluster-robust SEs | 0.066 | 0.199 | 23% | 83% | 100% |
| DiD, permutation inference | 0.066 | 0.199 | 91% | 13% | 91% |
| Synthetic control | 0.083 | 0.244 | 84% | 0% | 48% |

Bias and RMSE are in log points.

Before/after reports a significant saving on 80% of the interventions that did nothing. Rows
two and three have the same point estimate; only the standard error differs. Cluster-robust
standard errors are asymptotic in the number of treated clusters, and there is one treated
resource per panel, so the intervals are far too narrow. Building the null distribution by
permutation instead takes coverage from 23% to 91%.

The parallel-trends check does its job. Of the 117 estimates, 103 pass it; those have 95.1%
coverage and RMSE 0.151. The 14 that fail have 64.3% coverage and RMSE 0.402.

In dollars, against $2,020,441 of true annual savings, before/after misstates the total by
$326,700 net. The gross misattribution, summing absolute errors per intervention, is $450,968:
the aggregate looks close because errors cancel.

![Estimator scorecard](outputs/figures/scorecard.png)

The hand-rolled DiD estimator is checked against R's `plm` on all 117 panels. Largest
disagreement 1.2e-13; clustered-SE ratio 1.00000000. See `reports/r-crossvalidation.md`.

## Why this problem

IBM paid $4.6B for Apptio and owns Turbonomic and Kubecost: tools that take optimisation
actions and tools that report spend. Nothing in that stack substantiates the actions. The
FinOps Foundation's 2026 survey reports cloud waste at 29% of spend, rising, while 63% of
organisations now run a FinOps team, and quotes a practitioner: "Once you fix it, it's gone,
how do we give developers credit?" That is a question about an unobservable counterfactual.

## How it works

```
FOCUS billing feed -> bronze/silver/gold -> panel per intervention -> donor selection
                   -> 4 estimators -> parallel-trends gate -> score vs truth
                   -> agent + grounding guard -> report, MCP, Excel
```

**Simulator.** Daily demand per resource follows

```
log q_it = log(base_i) + trend_i·t + weekly_i(t) + annual_i(t) + u_it + waste_it + τ_i·1[t ≥ T₀]
```

with `u_it = ρ·u_i,t-1 + ε_it`, ρ = 0.90. It reproduces negotiated discounts, commitment
amortisation (`EffectiveCost` vs `BilledCost`), unused commitment, heavy-tailed resource
scale and a 10.6% untagged rate. Interventions are triggered when a resource breaches a
120-day rolling baseline, with a 95 to 150 day detection lag. That is the point: treatment is
assigned the way real teams assign it, so the bias before/after suffers from is actually in the
data. If treatment were random there would be nothing to fix.

**Estimation.** For each intervention, donors are untreated resources matched on pre-period
growth, not level. Four estimators run on the same panel. The naive one is computed so every
report can show the gap between it and the identified estimate.

**Diagnostics.** An event study checks that pre-treatment coefficients sit on zero before any
effect is believed.

![Event study](outputs/figures/event_study.png)

## What's in the repository

```
src/costproof/
  simulate/    FOCUS 1.2 schema and validation; price book at published list prices; generator
  warehouse/   runs the SQL layers in order
  causal/      panel construction, donor selection, DiD (hand-rolled), permutation inference,
               synthetic control, validation harness, R cross-validation export
  ml/          waste classifier: relative features, resource-level hold-out, precision@k
  agent/       tool registry, TF-IDF retrieval over three runbooks, governed review with
               audit records, watsonx backend with deterministic fallback, narrative
               grounding guard, MCP server
  report/      figures
sql/           silver_billing.sql, gold_unit_economics.sql
R/             crossvalidate.R (plm within estimator, lm with dummies, sandwich clustered SE)
scripts/       build_business_case.py, build_finance_model.py
reports/       cost-review.md, r-crossvalidation.md, two .xlsx workbooks
docs/          00-problem.md, 02-identification.md, build-notes.html
tests/         54 tests
```

The DiD estimator is implemented directly rather than called from a library: the within
transformation and the cluster-robust sandwich are written out, because that is where applied
DiD tends to go wrong. It agrees with statsmodels to about 1e-16 and with R to 1e-13.

## The agent

`python -m costproof.cli review` runs a cost review through a small agent. It calls tools from
a registry (`agent/tools.py`), each of which returns a value together with the method used, its
caveats and its sources. Findings over $1,000 a year, irreversible actions, spend with no
identifiable owner, and any estimate that failed its parallel-trends check are gated for human
approval and not executed. Every run writes a JSON audit record with each tool call, its
arguments and timing, and every source retrieved.

The language model only writes the narrative at the end. It never computes a number, and that
is enforced two ways. The deterministic backend can produce the whole report without a model,
and the figures are identical. And a grounding guard (`agent/guard.py`) reads the model's
narrative back, extracts every figure, and checks it against the tool output the model was
shown. Display precision is allowed ($970,158 for 970158.37, 10.6% for 0.106); rounding to
$1.3M or a total the model added up itself is not. An ungrounded or empty narrative is
rejected, the deterministic backend writes the section instead, and the rejected text is kept
in the audit record.

The guard exists because the first live run on watsonx returned an empty narrative. Granite 4 is
a chat model and the code was calling the deprecated completion endpoint, so it emitted
end-of-sequence immediately. The report rendered without the section while its header still
credited the model.

The watsonx backend uses `ibm/granite-4-h-small` over the chat endpoint at temperature 0. It
holds an ordered list of model IDs because `granite-3-8b-instruct`, the original choice, was
withdrawn from the catalogue during development.

The same tool registry is served over the Model Context Protocol (`agent/mcp_server.py`,
mcp 2.x): six tools, six resources, one prompt. Tools are annotated read-only, calls are
validated against the tool's JSON Schema before dispatch, and eight tests speak the protocol to
the live server over stdio.

```bash
python -m costproof.cli mcp --self-test
```

Last run: 4 findings, 3 gated, $1,348,140 annualised impact identified. $970,158 of it is
spend with no business-unit tag, $165,406 is prepaid capacity never consumed, $212,576 is
carried by the ten resources the classifier ranked highest.

## The finance layer

Two Excel workbooks, generated from the pipeline's outputs. Blue cells are inputs, black are
formulas, green are links. Both recalculate with zero errors.

`reports/costproof-business-case.xlsx` asks whether an organisation should adopt the
measurement layer. Its NPV counts only decision quality: engineering effort no longer spent
scaling fixes that did nothing. NPV $557,508 over five years, benefit-to-cost 6.27×. The
$115,746 a year of reported-savings accuracy is shown but excluded from NPV, because reporting
a number more accurately does not by itself create cash.

`reports/costproof-finance-model.xlsx` asks what the optimisation programme delivered,
measured properly. Eleven sheets: event register, per-event attribution with the
parallel-trends gate, budget vs actual by business unit, a twelve-month forecast by two
methods, ROI, Base/Bull/Bear, two sensitivity grids, a KPI dashboard. 2,408 formulas.

| | |
|---|---|
| Before/after would report | $1,693,741 / yr |
| Identified, all 117 events | $1,809,487 / yr |
| Credited (identified, parallel-trends gate) | $1,872,634 / yr |
| Programme NPV, 5 years, Base | $2,833,109 |
| BCR, ROI, IRR, payback | 2.86×, 186%, 234%, 6.5 months |
| Bear case NPV | $421,595 |

Crediting all 117 estimates gives a lower total than crediting only the 103 that pass the
gate; the 14 failures net to −$63K. The first draft of this model had no engineering cost for
executing the interventions and no ramp, and produced a 0.4-month payback. That draft was
discarded.

## What went wrong along the way

The first full validation run gave 7% coverage for every method at once. I assumed the
estimator was broken. It wasn't: the analysis pre-window reached back 84 days while the
simulator's detection lag was 20 to 75 days, so the "before" period contained the waste the
intervention was about to remove. Fixing the specification took DiD RMSE from 0.60 to 0.20.
When every method fails together, suspect the data specification.

The parallel-trends test originally rejected 98.3% of interventions. It was a Wald test built
on the same cluster-robust standard errors that are invalid with one treated unit. Rebuilt on
randomisation inference, the pass rate is 88% and the passing set has 95.1% coverage.

Others, in the git history: a `None`-vs-`NaN` check that reported 0% untagged spend; a GPU SKU
taking 63% of the estate because quantity was derived from the pricing unit; $2.58M of phantom
commitment savings from summing a discount measure over purchase rows; three Excel workbooks
that recalculated cleanly with wrong numbers because a hard-coded cell reference pointed at the
wrong row; and the MCP SDK changing its handler API between 1.x and 2.x while the project was
being built. `docs/build-notes.html` has each with its diagnosis.

## Limits

Enterprise billing data is confidential, so this runs on a simulator. On real data the
counterfactual is unobservable, which means an estimator can be run but not scored; simulation
is what makes accuracy measurable. The cost is that the simulator encodes my assumptions about
how cloud spend behaves, so validation is optimistic to the extent those are wrong.

Not solved: spillovers between resources (rightsizing one service can move load to another;
screened for, not eliminated), anticipation effects if teams throttle usage before the recorded
action date, and staggered-adoption designs where one fix rolls across many resources over
weeks. Retrieval is lexical and drops to 62% recall on paraphrased questions; embeddings are
the obvious next step. The waste classifier's ROC AUC is 0.65; it ranks the top of the queue
well and the middle poorly. See `docs/02-identification.md`.

## Changing things

Most behaviour is controlled from a few places. `SimConfig` in `simulate/generator.py` sets
the estate: resource count, AR(1) persistence, null-intervention share, detection lag.
`PanelSpec` in `causal/panel.py` sets the analysis window and minimum donors. The Assumptions
sheet in each workbook drives every downstream cell. `PREFERRED_MODELS` in `agent/llm.py`
sets the watsonx model order. Change one, rerun `study` or the script, and compare.

## Status

- [x] FOCUS 1.2 simulator with known ground truth
- [x] Warehouse, unit economics
- [x] DiD, permutation inference, synthetic control, parallel-trends gate, validation harness
- [x] R cross-validation
- [x] Waste classifier
- [x] Retrieval, governed agent, audit records, grounding guard, watsonx, MCP server
- [x] Business case and finance model workbooks
- [ ] Embedding-based retrieval

## Sources

- FinOps Foundation, *State of FinOps 2026*, https://data.finops.org/
- Flexera, *State of the Cloud 2026*, as reported at https://tech-insider.org/cloud-waste-29-percent-finops-2026/
- FOCUS Specification v1.2, https://focus.finops.org/focus-specification/v1-2/
- TechTarget, *IBM aims to reduce cloud costs with $4.6B Apptio acquisition*
- Abadie, Diamond & Hainmueller (2010); Bertrand, Duflo & Mullainathan (2004); Conley & Taber (2011); Goodman-Bacon (2021); Croissant & Millo (2008)

MIT licensed.
