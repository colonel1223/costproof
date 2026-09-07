# The Problem

> An industry spending hundreds of billions of dollars a year cannot prove what its own
> cost-saving work is worth. This is not a tooling gap. It is an identification problem,
> and identification problems belong to economists.

---

## 1. The money

Cloud is now one of the largest controllable line items in an enterprise P&L, and the
share of it that is wasted is **rising for the first time in five years**.

| Metric | 2024 | 2025 | 2026 | Source |
|---|---|---|---|---|
| Estimated cloud waste (share of IaaS/PaaS spend) | — | 27% | **29%** | Flexera *State of the Cloud 2026*, N=753 |
| Average overrun vs. cloud budget | — | — | **17%** | Flexera 2026 |
| Organisations actively managing **AI** spend | 31% | 63% | **98%** | FinOps Foundation *State of FinOps 2026*, N=685 |
| Organisations measuring unit economics (cost per service/transaction) | — | — | **49%** | Flexera 2026 |
| Organisations running hybrid cloud | — | — | **73%** | Flexera 2026 |
| Organisations with a dedicated FinOps team | — | — | **63%** | Flexera 2026 |

Read the first and last rows together. **63% of organisations have a dedicated team whose
entire job is reducing cloud waste, and waste went up anyway.** Either those teams are not
working, or nobody can measure whether they are working. This project argues the second.

The 49% figure is the other half of the indictment: **more than half of enterprises cannot
connect a dollar spent to a unit of anything produced.** They know their bill. They do not
know their cost per transaction, per customer, or per inference. Without a denominator, a
rising bill is uninterpretable — it might be waste, or it might be growth, and no dashboard
in the market can tell you which.

---

## 2. The admission

The FinOps Foundation's 2026 survey contains this quote from a practitioner, describing why
they cannot justify their own team's existence:

> **"Once you fix it, it's gone… how do we give developers credit for shift-left activities?"**

That sentence deserves to be read slowly, because an entire industry has just stated an
econometric problem in plain English without recognising it as one.

The practitioner is saying: *we intervened, the cost went away, and now we cannot observe
what the cost would have been had we not intervened.* The quantity they want — spend under
the world where the fix never shipped — **is by construction unobservable**. It is a
counterfactual.

This is the fundamental problem of causal inference, stated by a cloud engineer.

### Why the industry's current answer is wrong

The prevailing method for claiming cloud savings is **before-versus-after**:

```
claimed savings = (spend in the 30 days before the fix) − (spend in the 30 days after)
```

This estimator is biased by every single thing that changed at the same time as the fix.
Concretely, it is confounded by:

- **Organic demand growth or decline** — traffic does not hold still while you optimise
- **Seasonality** — retail in November, education in September, finance at quarter-end
- **Concurrent deployments** — engineering ships other things during your measurement window
- **Price changes** — provider discounts, commitment renewals, regional price moves
- **Mean reversion** — teams optimise *because* spend spiked, so it was going to fall anyway
  (this is regression to the mean masquerading as a win, and it is the most common error)

The result is an industry where savings claims are unfalsifiable. Finance does not believe
engineering's numbers; engineering cannot defend them; and the argument is settled by
seniority rather than evidence. Every FinOps practitioner has had this fight. None of them
have the instrument to win it.

---

## 3. What every tool on the market actually does

| Capability | Cloudability / Apptio | Turbonomic | CloudZero | Kubecost | **The gap** |
|---|---|---|---|---|---|
| Report what was spent | ✅ | ✅ | ✅ | ✅ | |
| Allocate spend to teams | ✅ | partial | ✅ | ✅ | |
| Detect an anomaly | ✅ | ✅ | ✅ | ✅ | |
| Recommend an action | partial | ✅ | partial | ✅ | |
| **Prove the action caused the saving** | ❌ | ❌ | ❌ | ❌ | ← **CostProof** |

Every product in this category is **descriptive**. They are, architecturally, very good
reporting layers over a billing feed. None of them construct a counterfactual, and therefore
none of them can answer the only question a CFO actually asks:

> *"You told me we saved $2.4M. Prove it."*

This is the gap CostProof fills. It is not a better dashboard. It is the **inference layer**
that sits underneath the claim.

---

## 4. Why this is specifically IBM's problem

IBM is not a bystander in this market — it is the largest consolidator in it.

- **$4.6B for Apptio** in 2023 (Cloudability, Targetprocess, ApptioOne) — IBM's largest
  software acquisition in years, bought explicitly to own cloud financial management
- **Turbonomic** — automated resource optimisation; it *takes the actions* whose value
  nobody can currently prove
- **Kubecost** — Kubernetes-level cost allocation
- **TBM (Technology Business Management)** — the taxonomy Apptio authored and IBM now
  stewards, which is the standard language for showback and chargeback
- **watsonx** — IBM's enterprise AI platform, and `watsonx.governance`, its AI governance
  product, in a market where *"AI cost management"* is the **#1 skill gap** and
  *"FinOps for AI"* is the **#1 forward-looking priority**

So IBM sells the tools that **take** optimisation actions and the tools that **report**
spend — and owns nothing that **proves the actions worked**. A Turbonomic customer who
automates a thousand rightsizing actions has a thousand unverified savings claims. IBM's own
product portfolio manufactures the exact measurement problem this project solves.

That is the argument. It is not "I built a cloud dashboard." It is:

> **"Your portfolio generates savings claims at scale and cannot substantiate any of them.
> I built the substantiation layer, and I validated that it recovers a known true effect."**

---

## 5. The thesis

> **Every FinOps tool on the market is descriptive. None can establish causation.
> CostProof is the counterfactual layer: it detects cloud and AI waste, and then proves what
> the fix was actually worth using difference-in-differences and synthetic control — with a
> governed, auditable trail on every number.**

Every savings claim in this industry rests on an untested assumption: that nothing else
moved. CostProof is the test of that assumption.

---

## 6. Why an economist, and not a software engineer, solves this

This is the part that matters for the interview, so it is worth being precise about.

A software engineer looking at this problem sees a **data pipeline and a dashboard**, because
those are the tools they have. They will build excellent versions of both, and the savings
number at the end will still be `before − after`, and it will still be biased.

An economist recognises the shape immediately: **treatment applied to some units and not
others, outcomes observed over time, and a counterfactual that must be constructed rather
than measured.** That is the first week of an econometrics sequence. The toolkit —
difference-in-differences, synthetic control, event studies, parallel-trends diagnostics,
placebo inference — was built for exactly this and has been stress-tested for forty years in
labour and public economics, where the cost of a wrong causal claim is policy that hurts
people.

The contribution here is not inventing a method. It is **recognising that a $44B industry
problem is a solved problem in a different field, and carrying the solution across.**

That transfer is the entire value proposition, and it is not available to someone who has
not studied economics.

---

## 7. What "done" means

CostProof is finished when it can do the following, reproducibly, from a clean clone:

1. **Ingest** multi-cloud billing data conforming to the **FinOps FOCUS 1.2** open
   specification — the industry-standard billing schema — into a dimensional warehouse
2. **Allocate** shared and untagged cost to business units under the TBM taxonomy, and
   compute **unit economics** (cost per transaction, cost per inference)
3. **Detect** anomalies and **classify** them as genuine waste versus benign growth, with
   reported precision and recall — because alert fatigue, not detection, is the real
   failure mode
4. **Estimate** the causal effect of each remediation using difference-in-differences and
   synthetic control, with parallel-trends diagnostics and placebo tests
5. **Validate** those estimates against a **known injected ground-truth effect** — reporting
   bias, RMSE, and confidence-interval coverage
6. **Explain** each finding through a governed agent that retrieves grounded context, calls
   the analytics as tools, and routes every recommendation through a human approval gate
   with a complete audit record
7. **Deliver** a CFO-facing business case: savings waterfall, program NPV/IRR, sensitivity

Item 5 is the one almost no portfolio project has, and it is the one that converts
"I ran a regression" into "I measured how wrong my estimator is."

---

## 8. An honest statement about the data

Enterprise billing data is confidential; no real firm will hand a student their cloud
invoices. CostProof therefore runs on a **simulator** that produces FOCUS-conformant billing
records with realistic seasonality, growth, price structures, and commitment mechanics,
anchored to published public cloud list prices.

This is stated plainly and up front, because a fabricated metric is the fastest way to lose
an interview — the follow-up question is always *"how did you measure that?"*

But the simulator is not a weakness. It is the design decision that makes the central claim
testable. **Because the true effect of every injected intervention is known, the causal
estimates can be scored against ground truth.** That is impossible with real billing data,
where the true counterfactual is unobservable by definition.

So the claim CostProof makes is not *"I saved a company $X."* It is:

> **"Here is an estimator, and here is how accurately it recovers a known truth —
> bias, RMSE, and interval coverage, measured over N simulated interventions."**

That is a stronger claim, and a more honest one, than any dollar figure a portfolio project
could assert.

---

## Sources

- FinOps Foundation, *State of FinOps 2026* (N=685) — https://data.finops.org/
- Flexera, *State of the Cloud Report 2026* (N=753), as reported —
  https://tech-insider.org/cloud-waste-29-percent-finops-2026/
- FinOps Foundation, *FOCUS: FinOps Open Cost and Usage Specification* —
  https://focus.finops.org/what-is-focus/
- FOCUS Specification v1.2 —
  https://focus.finops.org/wp-content/uploads/2025/05/FOCUS-spec-v1_2.pdf
- TechTarget, *IBM aims to reduce cloud costs with $4.6B Apptio acquisition* —
  https://www.techtarget.com/searchcloudcomputing/news/366542853/IBM-aims-to-reduce-cloud-costs-with-46B-Apptio-acquisition
- IBM Newsroom, *IBM acquires Kubecost* —
  https://newsroom.ibm.com/blog-ibm-acquires-kubecost-to-broaden-hybrid-cloud-cost-management-capabilities
- TBM Council, *TBM Taxonomy* — https://www.tbmcouncil.org/taxonomy/
