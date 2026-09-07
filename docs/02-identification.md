# Identification Strategy

> This document defines what CostProof is estimating, what has to be true for the estimate to
> be valid, how each assumption is tested, and how the estimator is scored against a known
> truth. If you can explain this file, you can defend the project.

---

## 1. The estimand

We are not estimating "how much spend fell." We are estimating the **Average Treatment
Effect on the Treated (ATT)**: the difference between what the treated resources actually
cost, and what those same resources *would have* cost had the optimisation never shipped.

In potential-outcomes notation, for resource $i$ in period $t$:

- $Y_{it}(1)$ = spend **if treated** (optimisation applied)
- $Y_{it}(0)$ = spend **if untreated**

$$\text{ATT} = \mathbb{E}\!\left[\, Y_{it}(1) - Y_{it}(0) \mid D_i = 1,\; t > T_0 \,\right]$$

where $D_i = 1$ marks treated resources and $T_0$ is the intervention date.

**The fundamental problem of causal inference:** for any treated resource after treatment we
observe $Y_{it}(1)$ and never $Y_{it}(0)$. The second term is missing from the data by
construction. Everything below is machinery for constructing a credible estimate of it.

This is precisely the practitioner's complaint — *"once you fix it, it's gone"* — restated
formally. They are describing a missing potential outcome.

---

## 2. Why the industry estimator is biased

The prevailing method is **before-versus-after** on treated units only:

$$\hat{\tau}_{\text{naive}} = \bar{Y}_{i,\,t>T_0} - \bar{Y}_{i,\,t<T_0}$$

This implicitly assumes $Y_{i,t>T_0}(0) = \bar{Y}_{i,t<T_0}$ — that absent the fix, spend
would have simply continued at its pre-period average. That assumption fails whenever
anything else moves. Decomposing the bias:

$$\hat{\tau}_{\text{naive}} = \underbrace{\text{ATT}}_{\text{what we want}} + \underbrace{\left[\bar{Y}_{i,t>T_0}(0) - \bar{Y}_{i,t<T_0}\right]}_{\text{time-varying confounding}}$$

The bracketed term is non-zero under:

| Confounder | Direction of bias | Why it happens |
|---|---|---|
| Organic demand growth | **Understates** savings | Baseline would have risen; you get blamed for growth |
| Seasonal decline | **Overstates** savings | Spend was going to fall anyway (Q1 after holiday peak) |
| Concurrent deployments | Either | Other teams ship during your window |
| Provider price changes | Either | Discounts and commitment renewals move the baseline |
| **Mean reversion** | **Overstates** savings | ← the dangerous one |

### Mean reversion deserves its own paragraph

Optimisation is not randomly assigned. **Teams optimise the things that just spiked.** That
is selection on the dependent variable: treatment is triggered *by* a high realisation of a
noisy series. Even with zero true effect, the next period's draw is lower in expectation
purely by regression to the mean.

So the naive estimator systematically credits the FinOps team for a decline that was going
to happen regardless. This is not a small effect — in a series with high transitory variance
it can dominate the true effect entirely. Every "we saved $2M" claim built on before/after
contains an unknown amount of it.

**A well-specified control group absorbs mean reversion, because untreated units drawn from
the same spike experience the same reversion.** That is the whole point of what follows.

---

## 3. Estimator 1 — Difference-in-Differences

### Setup

Compare the change in treated units to the change in untreated units over the same window:

$$\hat{\tau}_{\text{DiD}} = \left(\bar{Y}^{\text{treat}}_{\text{post}} - \bar{Y}^{\text{treat}}_{\text{pre}}\right) - \left(\bar{Y}^{\text{ctrl}}_{\text{post}} - \bar{Y}^{\text{ctrl}}_{\text{pre}}\right)$$

The control group's change estimates what *would* have happened to the treated group. The
second difference **subtracts out everything that hit both groups** — seasonality, price
changes, company-wide demand shifts, and mean reversion.

### Regression form (what we actually estimate)

$$Y_{it} = \alpha_i + \lambda_t + \tau\,(D_i \times \text{Post}_t) + X_{it}'\beta + \varepsilon_{it}$$

- $\alpha_i$ — **resource fixed effects**: absorb all time-invariant differences between
  resources (a GPU node is permanently more expensive than a queue; we never have to model
  why)
- $\lambda_t$ — **time fixed effects**: absorb everything common to all resources in a
  period (a provider price change, a company-wide traffic event)
- $\tau$ — **the ATT**, our coefficient of interest
- $X_{it}$ — time-varying covariates (request volume, if observed)

Inference uses **standard errors clustered at the resource level**, because a resource's
errors are serially correlated across time. Failing to cluster is the single most common
error in applied DiD and produces standard errors that are far too small — you will reject
no-effect nulls that you should not reject.

### The identifying assumption: parallel trends

> **Absent treatment, treated and control spend would have moved in parallel.**

Formally: $\mathbb{E}[Y_{it}(0) - Y_{i,t-1}(0) \mid D_i=1] = \mathbb{E}[Y_{it}(0) - Y_{i,t-1}(0) \mid D_i=0]$

This is **untestable** — it concerns an unobserved counterfactual. But it has a testable
implication in the pre-period, which is what we check.

### How we test it

1. **Event-study / pre-trend plot.** Estimate a coefficient for each period relative to
   treatment:

   $$Y_{it} = \alpha_i + \lambda_t + \sum_{k \neq -1} \tau_k \,\mathbb{1}[t - T_0 = k] \times D_i + \varepsilon_{it}$$

   Normalising $\tau_{-1}=0$. **All pre-treatment $\tau_k$ should be statistically
   indistinguishable from zero.** If they trend, parallel trends is already violated before
   treatment and the design is invalid. This plot is the single most important diagnostic in
   the project and belongs in the README.

2. **Placebo-in-time.** Move the fake treatment date into the pre-period and re-estimate.
   A significant "effect" where no treatment occurred means the design is detecting
   something other than treatment.

3. **Placebo-in-space.** Assign treatment to untreated resources and re-estimate. The
   distribution of these placebo effects gives a randomisation-inference reference
   distribution for the real estimate.

### Known failure mode: staggered adoption

Optimisations do not all ship on the same day. When treatment timing varies across units,
the standard two-way fixed-effects estimator is **not** a clean average of treatment
effects. It uses already-treated units as controls for later-treated units, and when effects
vary over time those comparisons enter with **negative weights** — the estimate can carry the
wrong sign even when every unit-level effect is positive (Goodman-Bacon 2021;
Callaway & Sant'Anna 2021; de Chaisemartin & D'Haultfœuille 2020).

**Our response:** where treatment timing is staggered we report the
Callaway–Sant'Anna group-time ATT alongside naive TWFE, and show the Goodman-Bacon
decomposition of where the TWFE estimate's weight is coming from. Knowing this literature
exists — and that the obvious estimator is wrong — is itself a differentiator; it is current
applied-econometrics practice, not textbook material.

---

## 4. Estimator 2 — Synthetic Control

### When DiD is not available

DiD needs a control group that plausibly shares trends. Sometimes there is exactly **one**
treated unit — a single large service, a single cluster — and no comparable twin. Averaging
all other resources into a control is indefensible; they are not similar.

### The method

Build the counterfactual as a **weighted average of untreated "donor" units**, with weights
chosen so the synthetic unit tracks the treated unit closely *before* treatment
(Abadie, Diamond & Hainmueller 2010).

Choose weights $w_j \ge 0$, $\sum_j w_j = 1$, minimising pre-treatment fit:

$$\min_{w} \sum_{t < T_0} \left( Y_{1t} - \sum_{j \in \mathcal{D}} w_j Y_{jt} \right)^2$$

Then the estimated effect in each post period is the gap:

$$\hat{\tau}_t = Y_{1t} - \sum_j w_j Y_{jt}, \qquad t > T_0$$

### Why the constraints matter

$w_j \ge 0$ and $\sum w_j = 1$ force **interpolation, not extrapolation**. The synthetic
control must lie inside the convex hull of the donor pool. If the treated unit is more
expensive than every donor, no valid synthetic control exists — and the method tells you so
rather than silently extrapolating. That refusal is a feature: it is the estimator declining
to answer a question the data cannot support.

### Inference

There is one treated unit, so conventional standard errors do not apply. Inference is by
**permutation**: run the identical procedure treating each donor as if it were treated, and
compare the real gap to the placebo distribution. The p-value is the treated unit's rank.
We also report the **post/pre RMSPE ratio**, which normalises the effect by pre-period fit
quality so that units the method fits badly are not mistaken for large effects.

### Donor pool discipline

Donors must be **untreated during the entire window** and not affected by the treatment
(no spillovers). In cloud terms: a donor cannot be a resource that received traffic shed by
the treated resource, or the design is contaminated. Donor selection is documented per
analysis in the audit record.

---

## 5. Estimator 3 — Bayesian Structural Time Series

For interventions with **no usable control group at all**, we fall back to a model-based
counterfactual: a state-space model with local level, local trend, and seasonal components,
fit on the pre-period and forecast forward (Brodersen et al. 2015, `CausalImpact`).

This is the **weakest** design in the stack and is labelled as such in output. It assumes
the pre-period model is correctly specified and that no unmodelled shock coincides with
treatment. We report it with wide credible intervals and never as the headline when DiD or
synthetic control is available.

**Design hierarchy, always reported in this order:**

| Rank | Design | Requires | Strength |
|---|---|---|---|
| 1 | Difference-in-Differences | Comparable untreated group | Strongest |
| 2 | Synthetic Control | Donor pool, single treated unit | Strong |
| 3 | Bayesian Structural TS | Only pre-period history | Weak — labelled |
| — | Before/after | Nothing | **Invalid — computed only to show the bias** |

The naive before/after estimate *is* computed for every intervention — not as an answer, but
so the report can display the gap between it and the identified estimate. **That gap is the
product.** It is the dollar value of doing the econometrics correctly, and it is the number
that makes a CFO care.

---

## 6. Validation against ground truth

This is the section that distinguishes CostProof from a project that merely "ran a regression."

Because the data comes from a simulator, **the true effect $\tau^{*}$ of every injected
intervention is known**. So the estimator can be scored:

| Metric | Definition | What it answers |
|---|---|---|
| **Bias** | $\mathbb{E}[\hat{\tau}] - \tau^{*}$ | Is the estimator systematically off? |
| **RMSE** | $\sqrt{\mathbb{E}[(\hat{\tau}-\tau^{*})^2]}$ | How far off is a typical estimate? |
| **Coverage** | $\Pr(\tau^{*} \in \widehat{\text{CI}}_{95})$ | Do the intervals mean what they claim? |
| **Power** | $\Pr(\text{reject } H_0 \mid \tau^{*} \neq 0)$ | Do real effects get detected? |
| **Size** | $\Pr(\text{reject } H_0 \mid \tau^{*} = 0)$ | How often do we invent an effect? |

Run over $N$ simulated interventions spanning a range of true effect sizes, noise levels, and
confounding structures.

**Coverage is the one to watch.** A 95% interval that covers the truth 70% of the time is
worse than useless — it is confidently wrong, which is exactly the failure mode this project
exists to eliminate. If our own intervals under-cover, we say so in the README.

We report the same metrics for the naive before/after estimator. The expected headline:
**naive is badly biased and its intervals under-cover; the identified estimators are
approximately unbiased with near-nominal coverage.** That contrast, measured rather than
asserted, is the core empirical result of the project.

---

## 7. Threats to validity we do not solve

Stated explicitly, because knowing the limits of your own design is what separates an analyst
from someone running library functions.

1. **SUTVA violations / spillovers.** Rightsizing one service can shift load onto another,
   contaminating the donor pool. We screen donors for correlation breaks at $T_0$ but cannot
   fully rule this out.
2. **Anticipation.** If teams throttle usage *before* the recorded intervention date, the
   pre-period is already partly treated and the effect is understated. We test by excluding
   windows immediately before $T_0$.
3. **Simulated data is not real data.** The simulator encodes our beliefs about how cloud
   spend behaves. If those beliefs are wrong in a way that favours our estimator, validation
   is optimistic. Mitigated by anchoring to published list prices and stress-testing under
   misspecification, not eliminated.
4. **External validity.** Effects estimated on one workload need not transfer to another.
   We report per-intervention effects and never a single global multiplier.

---

## 8. References

- Abadie, A., Diamond, A., & Hainmueller, J. (2010). *Synthetic Control Methods for
  Comparative Case Studies.* JASA 105(490).
- Bertrand, M., Duflo, E., & Mullainathan, S. (2004). *How Much Should We Trust
  Differences-in-Differences Estimates?* QJE 119(1). — the clustering result
- Brodersen, K. et al. (2015). *Inferring Causal Impact Using Bayesian Structural Time-Series
  Models.* Annals of Applied Statistics 9(1).
- Callaway, B., & Sant'Anna, P. (2021). *Difference-in-Differences with Multiple Time
  Periods.* Journal of Econometrics 225(2).
- de Chaisemartin, C., & D'Haultfœuille, X. (2020). *Two-Way Fixed Effects Estimators with
  Heterogeneous Treatment Effects.* AER 110(9).
- Goodman-Bacon, A. (2021). *Difference-in-Differences with Variation in Treatment Timing.*
  Journal of Econometrics 225(2).
