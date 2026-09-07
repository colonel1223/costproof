# CostProof

**The counterfactual layer for cloud and AI spend.**
Every FinOps tool on the market reports what you *spent*. None can prove what you *saved*.


---

## The 30-second version

In 2026, cloud waste rose to **29% of all spend** — the first increase in five years — while
**63% of organisations now run a dedicated FinOps team**. Those two facts do not sit
comfortably together. Either the teams are not working, or nobody can measure whether they
are.

The FinOps Foundation's 2026 survey contains a practitioner explaining the problem in their
own words:

> **"Once you fix it, it's gone… how do we give developers credit for shift-left activities?"**

That is an entire industry stating an econometric problem in plain English without
recognising it as one. They intervened, the cost went away, and they cannot observe what
the cost *would have been* had they not intervened. The quantity they want is a
**counterfactual** — unobservable by construction.

This is the fundamental problem of causal inference, described by a cloud engineer.

CostProof is the missing layer. It ingests FOCUS-standard billing data, detects waste, and
then **proves what each remediation was actually worth** using difference-in-differences and
synthetic control — and, because it runs on a simulator with known ground truth, it reports
**how accurate its own estimates are**.

---

## Headline result

Across **117 interventions** on a simulated **$8.9M/year** cloud estate, scored against the
known true effect of every one:

| Method | Bias | RMSE | 95% coverage | False positives on null interventions | Power |
|---|---:|---:|---:|---:|---:|
| Naive before/after *(the industry standard)* | 0.098 | 0.238 | 27% | **80%** | 100% |
| DiD, cluster-robust SEs | 0.066 | 0.199 | 23% | **83%** | 100% |
| **DiD, randomization inference** | **0.066** | **0.199** | **91%** | **13%** | 91% |
| Synthetic control | 0.083 | 0.244 | 84% | **0%** | 48% |

*Bias and RMSE in log points. "Null interventions" are 30 actions with a true effect of
**exactly zero**, applied to resources that had merely spiked.*

Three things to take from this table.

**1. Before/after reports a statistically significant saving on 80% of interventions that
did nothing at all.** Not 5%. Eighty. Those are actions taken on resources that happened to
spike, followed by spend reverting on its own — and the estimator credits the team for it.
This is the number that should worry a CFO.

**2. Rows 2 and 3 are the same point estimate.** Only the inference differs. Cluster-robust
standard errors are consistent as the number of *treated* clusters grows, and here there is
exactly one treated resource — so the intervals are far too narrow and cover the truth 23%
of the time. Switching to randomization inference takes coverage to 91% and cuts false
positives from 83% to 13%. Nothing about the economics changed; only the honesty of the
uncertainty did.

**3. The diagnostic works.** Splitting the identified estimates by whether they pass the
pre-trend test:

| | n | Bias | RMSE | 95% coverage |
|---|---:|---:|---:|---:|
| Passes parallel-trends test | 103 | 0.032 | **0.151** | **95.1%** |
| Fails parallel-trends test | 14 | 0.310 | 0.402 | 64.3% |

Estimates that pass have **essentially nominal coverage and less than half the error**. The
test correctly identifies which numbers to trust — demonstrated against ground truth rather
than asserted. This is what makes the system safe to put in front of finance: it knows when
to refuse to answer.

**In dollars.** On a portfolio whose true annualised saving is **$2,020,441**, the industry's
before/after method misstates the programme by **$326,700 (16%)**. The identified estimator
misstates it by **$210,954 (10%)** — and, unlike the naive method, tells you honestly how
uncertain it is.

![Estimator scorecard](outputs/figures/scorecard.png)

---

## Why this is IBM's problem specifically

IBM is the largest consolidator in this market:

- **$4.6B for Apptio** (2023) — Cloudability, ApptioOne — bought explicitly to own cloud
  financial management
- **Turbonomic** — automated resource optimisation. It *takes the actions* whose value
  nobody can currently prove.
- **Kubecost** — Kubernetes cost allocation
- **TBM** — the taxonomy Apptio authored and IBM now stewards
- **watsonx / watsonx.governance** — in a market where *"AI cost management"* is the **#1
  skill gap** and *"FinOps for AI"* the **#1 forward-looking priority**

So IBM sells the tools that **take** optimisation actions and the tools that **report**
spend, and owns nothing that **substantiates the actions**. A Turbonomic customer who
automates a thousand rightsizing actions ends the quarter with a thousand unverified savings
claims.

> **The portfolio manufactures savings claims at scale and cannot substantiate any of them.
> This is the substantiation layer.**

---

## How it works

```
FOCUS 1.2 billing feed ──► panel construction ──► donor selection ──► estimation ──► scoring
   (industry standard)      (resource × day)     (match on growth)    (4 methods)   (vs truth)
```

**Data foundation.** The simulator emits billing rows conforming to the **FinOps FOCUS 1.2**
specification — the open standard AWS, Azure and GCP all publish against. Anything CostProof
ingests works on real exports unchanged. The generator models daily demand as

```
log q_it = log(base_i) + trend_i·t + weekly_i(t) + annual_i(t) + u_it + waste_it + τ_i·1[t ≥ T₀]
```

with `u_it = ρ·u_i,t-1 + ε_it` an AR(1) transitory shock. It reproduces enterprise-agreement
discounts, commitment amortisation (`EffectiveCost` vs `BilledCost`), unused commitment,
heavy-tailed resource scale, and a **10.6% untagged spend** rate.

**Treatment is assigned endogenously.** Resources are optimised *because they spiked* — which
is how real teams behave; you rightsize whatever showed up on the anomaly report. That is
selection on a high draw of a mean-reverting series, so spend falls afterward whether or not
the fix did anything. Reproducing that faithfully is what makes the whole exercise
meaningful: if treatment were random, before/after would be unbiased and there would be no
problem to solve.

**Estimation** runs four methods per intervention. The naive estimator is computed *not as an
answer* but so every report can show the gap between it and an identified estimate. That gap
is the dollar value of doing the econometrics correctly.

**Diagnostics.** Event-study plots test parallel trends before any effect is believed:

![Event study](outputs/figures/event_study.png)

Pre-treatment coefficients sit on zero; the effect ramps in over the roll-out window, exactly
as an optimisation actually deploys. Synthetic control gives the same picture unit by unit:

![Synthetic control](outputs/figures/synthetic_control.png)

---

## Two things I got wrong, and what fixed them

Both are in the git history, and both are more interesting than the result.

**The estimator was fine; the analysis window was wrong.** The first full run gave DiD
coverage of **7%** and barely beat naive. My instinct was to distrust the estimator. The
actual cause: waste was being remediated 20–75 days after it started, but the analysis
pre-window reached back **84 days** — so the "counterfactual" period was a mixture of
*waste running* and *no waste yet*, and every method understated the effect. Lengthening the
detection lag past the pre-window took DiD RMSE from 0.60 to 0.199. The lesson I actually
care about: when every method fails together, suspect the data specification, not the
methods.

**A broken variance estimate corrupted the test built on top of it.** The parallel-trends
test initially rejected for **98.3% of interventions**, including designs that visibly
satisfied it. The test was a Wald statistic built on the same cluster-robust standard errors
that are invalid with one treated unit — each t-statistic inflated, so the Wald statistic
inflated by roughly the square. Rebuilding the test on randomization inference took the pass
rate to **88%**, and the passing set turned out to have 95.1% coverage. A diagnostic
inherits every weakness of the variance estimate it is built on.

---

## What is in here

```
src/costproof/
  simulate/    FOCUS 1.2 schema + conformance validation; price book anchored to
               published list prices; ground-truth billing generator
  causal/      panel construction, donor selection, DiD (two-way FE, hand-rolled),
               randomization inference, synthetic control, validation harness
  report/      figures
docs/
  00-problem.md          the business case, fully cited
  02-identification.md   the econometrics: estimand, assumptions, threats, references
tests/                   24 tests, several of which encode bugs found during development
```

**The DiD estimator is implemented directly rather than called from a library** — the within
transformation and the cluster-robust sandwich are written out, because that is where applied
DiD most often goes wrong. It is validated against `statsmodels` on identical data and agrees
to **machine precision** (coefficient and standard error, difference ~1e-16).

`tests/test_simulate.py` includes regression tests for real bugs: a `None`-vs-`NaN` coercion
that silently reported a 0% untagged rate, and a SKU-scaling error that let one GPU SKU take
63% of estate spend.

---

## Reproducing

```bash
pip install -e ".[dev]"
pytest                                    # 24 tests
python -m costproof.cli study               # regenerates every number above
```

Everything is deterministic given the seed in `SimConfig`. Data is regenerated rather than
committed, so nothing in this README can drift from what the code produces.

---

## An honest statement about the data

Enterprise billing data is confidential, so CostProof runs on a simulator. This is stated up
front because a fabricated metric is the fastest way to fail an interview — the follow-up
question is always *"how did you measure that?"*

But the simulator is not a compromise. On real billing data the counterfactual is
unobservable **by definition**, which means a causal estimator can be run but never *scored*.
Simulation is what makes the central claim testable. The claim here is not *"I saved a company
$2M."* It is:

> **"Here is an estimator, and here is exactly how accurately it recovers a known truth —
> bias, RMSE, and interval coverage, measured over 117 interventions."**

That is a stronger claim, and a more honest one, than any dollar figure a portfolio project
could assert.

**Limits I have not solved**, stated plainly: spillovers between resources (rightsizing one
service can shift load to another) are screened for but not eliminated; anticipation effects
if teams throttle usage before the recorded action date; and the simulator encodes my beliefs
about how cloud spend behaves, so validation is optimistic to the extent those beliefs are
wrong. See `docs/02-identification.md §7`.

---

## Roadmap

- [x] FOCUS 1.2 schema, conformance validation, ground-truth simulator
- [x] Panel construction, growth-matched donor selection
- [x] DiD (two-way FE + cluster-robust + randomization inference), synthetic control
- [x] Validation harness: bias, RMSE, coverage, size, power, dollar misstatement
- [ ] DuckDB medallion warehouse; allocation and unit economics (cost per transaction / per inference)
- [ ] Waste detection and classification (benign growth vs. genuine waste)
- [ ] MCP server exposing the analytics as agent-callable tools
- [ ] RAG over pricing docs, the FOCUS spec, and tagging policy
- [ ] Governed agent on watsonx with human approval gate and full audit trail
- [ ] CFO-facing Excel business case: savings waterfall, programme NPV/IRR, sensitivity

---

## Sources

- FinOps Foundation, *State of FinOps 2026* (N=685) — https://data.finops.org/
- Flexera, *State of the Cloud 2026* (N=753), as reported — https://tech-insider.org/cloud-waste-29-percent-finops-2026/
- *FOCUS Specification v1.2* — https://focus.finops.org/focus-specification/v1-2/
- TechTarget, *IBM aims to reduce cloud costs with $4.6B Apptio acquisition* — https://www.techtarget.com/searchcloudcomputing/news/366542853/IBM-aims-to-reduce-cloud-costs-with-46B-Apptio-acquisition
- Abadie, Diamond & Hainmueller (2010); Bertrand, Duflo & Mullainathan (2004); Conley & Taber (2011); Goodman-Bacon (2021) — see `docs/02-identification.md`

MIT licensed.
