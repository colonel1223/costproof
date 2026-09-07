"""Ground-truth billing simulator.

Why simulate at all
-------------------
Enterprise billing data is confidential. More importantly, on real billing data the
counterfactual is unobservable *by definition* -- which means a causal estimator can be
run but never scored. Simulation is what makes the central claim of this project
testable: because the true effect of every injected intervention is known, we can measure
how accurately each estimator recovers it (bias, RMSE, interval coverage).

The design decision that matters
--------------------------------
Treatment is assigned **endogenously**. Resources are optimised because their spend just
spiked, which is how real FinOps teams actually behave -- you rightsize the thing that
showed up on the anomaly report. That is selection on a high realisation of a noisy
series, so even a zero-effect intervention is followed by a decline through pure mean
reversion.

This is deliberate. If treatment were randomly assigned, before/after would be unbiased
and there would be no problem to solve. The endogeneity *is* the problem the industry
has, and reproducing it faithfully is what makes the validation exercise meaningful.

Data generating process
-----------------------
For resource ``i`` on day ``t``, log demand is:

    log q_it = log(base_i)
             + trend_i * t                     # secular growth or decline
             + weekly_i(t)                     # day-of-week effect
             + annual_i(t)                     # seasonal effect, BU-specific
             + u_it                            # AR(1) transitory shock
             + waste_it                        # injected inefficiency (known)
             + tau_i * 1[t >= T0_i]            # injected treatment effect (known)

with ``u_it = rho * u_i,t-1 + eps_it``. The AR(1) term is what produces spikes that
revert, and therefore what produces mean-reversion bias in the naive estimator.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict

import numpy as np
import pandas as pd

from costproof.simulate.focus import (
    ChargeCategory,
    ChargeFrequency,
    CommitmentDiscountCategory,
    CommitmentDiscountStatus,
    PricingCategory,
    ServiceCategory,
)
from costproof.simulate.pricebook import (
    BUSINESS_UNITS,
    ENVIRONMENTS,
    PRICE_BOOK,
    REGIONS,
    Sku,
    by_category,
)

# =======================================================================================
# Configuration
# =======================================================================================


@dataclass(frozen=True)
class SimConfig:
    """All knobs for the simulator. Frozen so a run is reproducible from its config."""

    seed: int = 20260907
    start_date: str = "2025-01-01"
    n_days: int = 540  # 18 months
    n_resources: int = 400

    # --- Demand dynamics ---------------------------------------------------------------
    ar1_rho: float = 0.90
    """Persistence of transitory shocks. Half-life is ln(0.5)/ln(rho) days: at 0.90 a
    spike takes ~6.6 days to half-decay, so a spike large enough to trigger an
    intervention is still elevating the 28-day pre-window and has largely decayed by the
    post-window. That asymmetry IS mean-reversion bias. At 0.72 the half-life is 2.1 days
    and spikes vanish before the pre-window closes, which is why an earlier calibration
    produced no measurable bias at all."""

    trend_sd: float = 0.0011
    """SD of per-resource daily log growth. ~0.11%/day => wide spread of growth rates,
    which is what breaks parallel trends for a carelessly chosen control group."""

    # --- Tagging ------------------------------------------------------------------------
    untagged_rate: float = 0.24
    """Share of resources with no owner tag. Real estates run 20-30% untagged; this is
    the single largest obstacle to cost allocation in practice."""

    # --- Commitments ---------------------------------------------------------------------
    commitment_coverage: float = 0.58
    """Share of eligible compute hours covered by a commitment discount."""

    commitment_waste_rate: float = 0.09
    """Share of purchased commitment that goes unused -- money spent on capacity never
    consumed. Invisible in on-demand cost analysis; visible in FOCUS via
    CommitmentDiscountStatus = 'Unused'."""

    # --- Injected waste -------------------------------------------------------------------
    n_waste_events: int = 140
    waste_magnitude: tuple[float, float] = (0.20, 1.10)
    """Excess demand during a waste event, in log points. 0.20-1.10 is a 1.2x to 3.0x
    over-provision, which spans the range of idle instances and orphaned volumes seen in
    practice. An earlier ceiling of 1.60 implied a 5x over-provision, which is possible
    but rare enough that it distorted the estate."""

    waste_duration_days: tuple[int, int] = (12, 95)

    # --- Interventions ---------------------------------------------------------------------
    n_interventions: int = 120
    true_effect_range: tuple[float, float] = (-0.42, -0.06)
    """True treatment effect in log points. -0.22 is roughly a 20% cost reduction."""

    null_effect_share: float = 0.25
    """Share of interventions with a TRUE EFFECT OF ZERO. These are the most important
    rows in the dataset: they are interventions that did nothing, applied to resources
    that had just spiked. A naive before/after estimator will confidently report savings
    on every one of them. Measuring that false-positive rate is the headline result."""

    spike_trigger_z: float = 1.05
    """How large a spike must be (in SD units of the resource's own history) before it
    becomes eligible for intervention. This is the endogeneity mechanism."""

    min_pre_days: int = 90
    min_post_days: int = 45

    def fingerprint(self) -> str:
        """Stable hash of the config, recorded with outputs for reproducibility."""
        payload = repr(sorted(asdict(self).items())).encode()
        return hashlib.sha256(payload).hexdigest()[:12]


# =======================================================================================
# Ground truth records
# =======================================================================================


@dataclass
class Intervention:
    """A recorded optimisation action with its KNOWN true effect.

    ``true_effect_log`` is the object every estimator in this project is trying to
    recover. It is never exposed to the estimation code -- only to the scoring code.
    """

    intervention_id: str
    resource_id: str
    business_unit: str
    start_day: int
    start_date: pd.Timestamp
    action: str
    true_effect_log: float
    """TOTAL true causal effect, in log points: the resource's own efficiency gain plus
    any persistent waste this action eliminated. This is the estimand."""
    true_effect_own: float
    """The efficiency component alone (zero for null interventions)."""
    waste_removed_log: float
    """Log-point reduction from terminating a persistent waste event, if any."""
    is_null: bool
    """True when the intervention had NO real effect of any kind -- it was applied to a
    resource that had merely spiked. These are the rows that expose false savings."""
    trigger_z: float
    """How big the spike was that triggered this intervention. Larger trigger => more
    mean reversion => more naive-estimator bias. Used to demonstrate the mechanism."""
    resolved_waste_id: str | None = None


@dataclass
class WasteEvent:
    waste_id: str
    resource_id: str
    start_day: int
    end_day: int
    kind: str
    magnitude_log: float
    persistent: bool = False
    """Persistent waste does not self-resolve. An idle over-provisioned instance, an
    orphaned volume and a zombie environment all bill forever until a human kills them,
    so they run to the end of the window unless an intervention terminates them.
    Transient waste (a runaway job, a retry storm) burns out on its own."""
    resolved_by: str | None = None


@dataclass
class Estate:
    """A generated cloud estate plus the ground truth used to score estimators."""

    billing: pd.DataFrame
    resources: pd.DataFrame
    drivers: pd.DataFrame
    interventions: list[Intervention] = field(default_factory=list)
    waste_events: list[WasteEvent] = field(default_factory=list)
    config: SimConfig = field(default_factory=SimConfig)

    def interventions_frame(self) -> pd.DataFrame:
        return pd.DataFrame([asdict(i) for i in self.interventions])

    def waste_frame(self) -> pd.DataFrame:
        return pd.DataFrame([asdict(w) for w in self.waste_events])


# =======================================================================================
# Seasonality
# =======================================================================================


def _weekly_profile(rng: np.random.Generator, pattern: str) -> np.ndarray:
    """Day-of-week log multipliers, indexed Monday=0."""
    if pattern == "workweek":
        # Batch/analytics workloads: heavy Mon-Fri, near-idle at the weekend.
        base = np.array([0.16, 0.18, 0.18, 0.17, 0.10, -0.42, -0.48])
    elif pattern == "retail":
        # Consumer traffic: mild weekday dip, weekend peak.
        base = np.array([-0.04, -0.06, -0.05, -0.01, 0.06, 0.12, 0.10])
    elif pattern == "growth":
        base = np.array([0.05, 0.06, 0.06, 0.05, 0.03, -0.10, -0.13])
    else:  # flat -- always-on platform services
        base = np.zeros(7)
    return base + rng.normal(0.0, 0.02, size=7)


def _annual_component(day_of_year: np.ndarray, pattern: str) -> np.ndarray:
    """Annual seasonality in log points."""
    theta = 2 * np.pi * day_of_year / 365.25
    if pattern == "retail":
        # Q4 peak: a holiday build that decays in January. This is the confounder that
        # makes a January intervention look successful when nothing was done.
        return 0.34 * np.exp(-((day_of_year - 330) ** 2) / (2 * 34.0**2)) - 0.06 * np.cos(theta)
    if pattern == "workweek":
        return 0.07 * np.sin(theta - 0.8)
    if pattern == "growth":
        return 0.04 * np.sin(theta)
    return np.zeros_like(day_of_year, dtype=float)


def _ar1(rng: np.random.Generator, n: int, rho: float, sigma: float) -> np.ndarray:
    """AR(1) transitory shock series, initialised at its stationary distribution."""
    out = np.empty(n)
    stationary_sd = sigma / np.sqrt(max(1e-9, 1 - rho**2))
    out[0] = rng.normal(0.0, stationary_sd)
    innov = rng.normal(0.0, sigma, size=n)
    for t in range(1, n):
        out[t] = rho * out[t - 1] + innov[t]
    return out


# =======================================================================================
# Estate construction
# =======================================================================================


def _build_resources(rng: np.random.Generator, cfg: SimConfig) -> pd.DataFrame:
    """Create the resource inventory: what exists, who owns it, how big it is."""
    bu_weights = np.array([0.20, 0.17, 0.19, 0.15, 0.16, 0.13])
    env_names = [e[0] for e in ENVIRONMENTS]
    env_probs = np.array([e[1] for e in ENVIRONMENTS])

    rows = []
    for idx in range(cfg.n_resources):
        bu = BUSINESS_UNITS[rng.choice(len(BUSINESS_UNITS), p=bu_weights)]
        category = bu["categories"][rng.integers(len(bu["categories"]))]
        candidates = by_category(category)
        if not candidates:
            candidates = list(PRICE_BOOK)
        sku: Sku = candidates[rng.integers(len(candidates))]

        env = env_names[rng.choice(len(env_names), p=env_probs)]
        region = REGIONS[rng.integers(len(REGIONS))]

        # Resource scale is heavy-tailed: a handful of resources dominate the bill.
        # This is universally true of real estates and it is why unweighted averages
        # across resources are misleading.
        scale = float(np.exp(rng.normal(0.0, 1.05)))
        env_scale = {"production": 1.0, "staging": 0.30, "development": 0.20,
                     "sandbox": 0.11}[env]

        # Non-production resources are far more likely to be untagged, because nobody
        # sets up governance for a sandbox. This correlation is what makes the untagged
        # bucket non-random and therefore dangerous to allocate proportionally.
        untag_p = cfg.untagged_rate * (0.5 if env == "production" else 1.9)
        tagged = rng.random() > min(0.85, untag_p)

        rows.append(
            {
                "resource_id": f"r-{idx:04d}",
                "resource_name": f"{bu['id'].removeprefix('bu-')}-{sku.resource_type}-{idx:04d}",
                "sku_id": sku.sku_id,
                "business_unit": bu["id"] if tagged else None,
                "business_unit_true": bu["id"],  # ground truth for allocation scoring
                "cost_centre": bu["cost_centre"] if tagged else None,
                "environment": env if tagged else None,
                "environment_true": env,
                "driver": bu["driver"],
                "seasonality": bu["seasonality"],
                "region_id": region[0],
                "region_name": region[1],
                "base_scale": scale * env_scale,
                "trend": float(rng.normal(0.00035, cfg.trend_sd)),
                "tagged": tagged,
            }
        )
    return pd.DataFrame(rows)


def _build_drivers(rng: np.random.Generator, cfg: SimConfig,
                   dates: pd.DatetimeIndex) -> pd.DataFrame:
    """Business driver volumes per BU per day.

    These are the denominators for unit economics. Without them a rising bill is
    uninterpretable -- it could be waste or it could be success. Only 49% of
    organisations measure this (Flexera 2026), which is why half the industry cannot
    tell growth from waste.
    """
    n = len(dates)
    doy = dates.dayofyear.to_numpy()
    dow = dates.dayofweek.to_numpy()

    frames = []
    for bu in BUSINESS_UNITS:
        if bu["driver"] is None:
            continue
        weekly = _weekly_profile(rng, bu["seasonality"])
        annual = _annual_component(doy, bu["seasonality"])
        # AI assistant grows fast; established services grow slowly.
        growth = 0.0042 if bu["id"] == "bu-assist" else float(rng.normal(0.0007, 0.0004))
        base = {"transactions": 1_450_000, "queries": 8_800_000, "inferences": 240_000,
                "training_jobs": 420, "pipeline_runs": 2_900}[bu["driver"]]

        log_v = (
            np.log(base)
            + growth * np.arange(n)
            + weekly[dow]
            + annual
            + _ar1(rng, n, 0.55, 0.055)
        )
        frames.append(
            pd.DataFrame(
                {
                    "date": dates,
                    "business_unit": bu["id"],
                    "driver": bu["driver"],
                    "volume": np.exp(log_v),
                }
            )
        )
    return pd.concat(frames, ignore_index=True)


# =======================================================================================
# Demand, waste, and endogenous treatment
# =======================================================================================


def _base_demand(rng: np.random.Generator, cfg: SimConfig, resources: pd.DataFrame,
                 dates: pd.DatetimeIndex) -> np.ndarray:
    """Log demand for every resource before waste or treatment. Shape (n_res, n_days)."""
    n_days = len(dates)
    doy = dates.dayofyear.to_numpy()
    dow = dates.dayofweek.to_numpy()
    t = np.arange(n_days)

    from costproof.simulate.pricebook import SKU_INDEX

    log_q = np.empty((len(resources), n_days))
    for i, row in enumerate(resources.itertuples(index=False)):
        sku = SKU_INDEX[row.sku_id]
        weekly = _weekly_profile(rng, row.seasonality)
        annual = _annual_component(doy, row.seasonality)
        # Innovation SD scaled so the stationary SD matches the SKU's stated volatility.
        sigma = sku.demand_volatility * np.sqrt(1 - cfg.ar1_rho**2)
        log_q[i] = (
            np.log(_units_per_day(sku) * row.base_scale)
            + row.trend * t
            + weekly[dow]
            + annual
            + _ar1(rng, n_days, cfg.ar1_rho, sigma)
        )
    return log_q


def _units_per_day(sku: Sku) -> float:
    """Baseline daily quantity for one unit-scale resource of this SKU."""
    return sku.base_units_per_day


def _inject_waste(rng: np.random.Generator, cfg: SimConfig, log_q: np.ndarray,
                  resources: pd.DataFrame) -> list[WasteEvent]:
    """Add known inefficiency episodes to the demand surface.

    These are the events the ML detector is scored against. Kinds mirror the waste
    patterns FinOps teams actually chase.
    """
    #: (kind, persistent). Persistent waste bills until a human intervenes; transient
    #: waste burns out. Getting this distinction right is what makes the true effect of
    #: a remediation constant and therefore knowable.
    kinds = (
        ("idle_overprovision", True),
        ("orphaned_resource", True),
        ("zombie_environment", True),
        ("runaway_job", False),
        ("retry_storm", False),
    )
    n_res, n_days = log_q.shape
    events: list[WasteEvent] = []
    occupied: set[int] = set()  # one waste event per resource keeps ground truth clean

    for k in range(cfg.n_waste_events):
        pool = [i for i in range(n_res) if i not in occupied]
        if not pool:
            break
        i = int(pool[rng.integers(len(pool))])
        occupied.add(i)

        kind, persistent = kinds[int(rng.integers(len(kinds)))]
        mag = float(rng.uniform(*cfg.waste_magnitude))
        start = int(rng.integers(40, max(41, n_days - 120)))

        if persistent:
            # Step uplift that runs to the end of the window. Nobody turns it off.
            end = n_days
            scale = 0.7 if kind == "zombie_environment" else 1.0
            log_q[i, start:] += mag * scale
            mag_effective = mag * scale
        else:
            dur = int(rng.integers(*cfg.waste_duration_days))
            end = min(n_days, start + dur)
            if kind == "runaway_job":
                log_q[i, start:end] += mag  # sharp onset, sharp end
            else:  # retry_storm -- escalates then is throttled
                log_q[i, start:end] += mag * np.linspace(0.0, 1.0, end - start)
            mag_effective = mag

        events.append(
            WasteEvent(
                waste_id=f"w-{k:03d}",
                resource_id=str(resources.iloc[i]["resource_id"]),
                start_day=start,
                end_day=end,
                kind=kind,
                magnitude_log=mag_effective,
                persistent=persistent,
            )
        )
    return events


def _assign_interventions(rng: np.random.Generator, cfg: SimConfig, log_q: np.ndarray,
                          resources: pd.DataFrame, dates: pd.DatetimeIndex,
                          waste_events: list[WasteEvent]) -> list[Intervention]:
    """Assign treatment ENDOGENOUSLY, triggered by observed spend spikes.

    This function is the reason the project exists. Real teams optimise what just
    spiked; that is selection on a high draw of a mean-reverting series. So even a
    zero-effect intervention is followed by a decline, and before/after credits the
    team for it.

    We record ``trigger_z`` -- how large the triggering spike was -- so the analysis
    can show the naive estimator's bias growing with the size of the trigger, which
    demonstrates the mechanism rather than merely asserting it.
    """
    n_res, n_days = log_q.shape
    interventions: list[Intervention] = []
    used: set[int] = set()

    # A resource is "spiking" when its trailing 7-day mean sits well above its own
    # LONG-RUN level. The baseline window matters enormously here.
    #
    # An earlier version compared the 7-day mean to a 28-day mean. That fires both when
    # recent spend is high AND when the short baseline happens to have dipped -- and for
    # a mean-reverting series the second case is common. It therefore selected moments
    # preceded by a trough, which dragged the 28-day pre-window average DOWN and cancelled
    # out the very mean reversion the design is meant to produce. Diagnosing that required
    # plotting the average spend profile around null interventions: it showed spend at
    # -0.37 below baseline fifteen days before treatment, which no correct trigger produces.
    #
    # A 120-day baseline is stable enough not to chase the noise it is meant to measure.
    frame = pd.DataFrame(log_q.T)
    roll7 = frame.rolling(7, min_periods=7).mean().to_numpy().T
    base = frame.rolling(120, min_periods=90).mean().to_numpy().T
    sd = frame.rolling(120, min_periods=90).std().to_numpy().T
    sd = np.where(~np.isfinite(sd) | (sd < 1e-6), 1e-6, sd)
    z = (roll7 - base) / sd

    lo, hi = cfg.min_pre_days, n_days - cfg.min_post_days
    idx = {rid: n for n, rid in enumerate(resources["resource_id"])}
    persistent_by_res: dict[int, WasteEvent] = {
        idx[w.resource_id]: w for w in waste_events if w.persistent
    }

    # A spike has one of two causes, and the distinction drives everything:
    #
    #   REAL  -- a persistent waste event just started on this resource. Remediating it
    #            genuinely removes cost, forever.
    #   NOISE -- a transitory AR(1) draw. Nothing is broken. Whatever the team does here
    #            has no real effect, and spend reverts on its own.
    #
    # Null interventions must be drawn ONLY from the noise pool. Assigning them to waste
    # onsets (as an earlier version did) meant the "no-effect" cases sat on top of waste
    # that then billed forever, so post-period cost ROSE and the estimator looked biased
    # in the wrong direction. That was an artefact of the simulator, not of the estimator.
    # The noise pool must be free of EVERY kind of waste, not just persistent waste.
    # Transient waste (a runaway job, a retry storm) lasts 12-95 days, so an intervention
    # placed at its onset has a post-window still full of the runaway job -- spend rises,
    # and the "no-effect" case looks like a cost increase. That produced 7-sigma triggers
    # in the null group, which pure AR(1) noise cannot generate. Requiring waste-free
    # resources is what makes a null intervention genuinely null.
    waste_free = {i for i in range(n_res)} - {idx[w.resource_id] for w in waste_events}

    # The two populations are found by DIFFERENT mechanisms, which is how it works in
    # practice and which matters for measurement:
    #
    #   REAL  -- found by a periodic inventory scan (an idle-resource or rightsizing
    #            report), not by an anomaly alert. Such scans run monthly at best, so
    #            detection lags onset badly. No spike trigger is required: a resource
    #            that has been quietly idle for four months never spikes at all.
    #
    #   NOISE -- found by an anomaly alert firing on a transitory spike. Nothing is
    #            actually broken. This is where phantom savings come from.
    #
    # DETECTION_LAG must exceed the analysis pre-window. If waste is remediated while
    # the pre-window still reaches back before the waste began, then the pre-period is
    # not the counterfactual -- it is a mixture of "waste running" and "no waste yet" --
    # and EVERY estimator understates the effect. An earlier calibration used a 20-75 day
    # lag against an 84-day pre-window and produced DiD coverage of 7%: the estimators
    # were fine, the analysis window was misspecified relative to the data.
    detection_lag = (95, 150)

    real_pool: list[tuple[float, int, int]] = []
    noise_pool: list[tuple[float, int, int]] = []
    for i in range(n_res):
        w = persistent_by_res.get(i)
        if w is not None:
            t_lo = max(lo, w.start_day + detection_lag[0])
            t_hi = min(hi, w.start_day + detection_lag[1])
            for t in range(t_lo, t_hi):
                # Rank by how much waste there is to find -- a scan surfaces the
                # biggest offenders first.
                real_pool.append((w.magnitude_log, i, t))
        elif i in waste_free:
            for t in range(lo, hi):
                if np.isfinite(z[i, t]) and z[i, t] > cfg.spike_trigger_z:
                    noise_pool.append((float(z[i, t]), i, t))

    # Bigger spikes get attention first -- that is how a triage queue is actually worked.
    real_pool.sort(key=lambda c: -c[0])
    noise_pool.sort(key=lambda c: -c[0])

    actions = ("rightsize_instance", "schedule_shutdown", "delete_orphaned_volume",
               "enable_autoscaling", "migrate_storage_class", "tune_batch_size",
               "consolidate_cluster", "cache_inference_results")

    n_null = int(round(cfg.n_interventions * cfg.null_effect_share))
    n_real = cfg.n_interventions - n_null
    plan = [(pool, is_null) for pool, is_null, count in
            ((real_pool, False, n_real), (noise_pool, True, n_null))
            for _ in range(count)]

    cursors = {id(real_pool): 0, id(noise_pool): 0}
    for pool, is_null in plan:
        # Advance this pool's cursor to the next unused resource.
        c = cursors[id(pool)]
        while c < len(pool) and pool[c][1] in used:
            c += 1
        cursors[id(pool)] = c + 1
        if c >= len(pool):
            continue
        zval, i, t = pool[c]
        used.add(i)
        k = len(interventions)

        # A real intervention terminates the persistent waste that triggered it.
        removed = 0.0
        resolved_id: str | None = None
        w = persistent_by_res.get(i)
        if w is not None and not is_null and w.start_day < t and w.resolved_by is None:
            removed = w.magnitude_log
            resolved_id = w.waste_id

        own = 0.0 if is_null else float(rng.uniform(*cfg.true_effect_range))
        res = resources.iloc[i]
        iv = Intervention(
            intervention_id=f"i-{k:03d}",
            resource_id=str(res["resource_id"]),
            business_unit=str(res["business_unit_true"]),
            start_day=t,
            start_date=dates[t],
            action=actions[int(rng.integers(len(actions)))],
            true_effect_log=own - removed,
            true_effect_own=own,
            waste_removed_log=removed,
            is_null=is_null,
            trigger_z=zval,
            resolved_waste_id=resolved_id,
        )
        if resolved_id is not None and w is not None:
            w.resolved_by = iv.intervention_id
            w.end_day = t
        interventions.append(iv)

    # Apply the true effects to the demand surface.
    for iv in interventions:
        i = idx[iv.resource_id]
        # Remove the waste this action eliminated, from the action date onward.
        if iv.waste_removed_log > 0.0:
            log_q[i, iv.start_day:] -= iv.waste_removed_log
        # Efficiency gains ramp in over a few days -- a rightsizing rolls out, it does
        # not teleport. An event-study plot should show exactly this shape.
        if iv.true_effect_own != 0.0:
            ramp_len = 4
            end_ramp = min(n_days, iv.start_day + ramp_len)
            log_q[i, iv.start_day:end_ramp] += iv.true_effect_own * np.linspace(
                0.25, 1.0, end_ramp - iv.start_day
            )
            log_q[i, end_ramp:] += iv.true_effect_own
    return interventions


# =======================================================================================
# Costing -- turn demand into FOCUS billing rows
# =======================================================================================


def _to_billing(rng: np.random.Generator, cfg: SimConfig, log_q: np.ndarray,
                resources: pd.DataFrame, dates: pd.DatetimeIndex) -> pd.DataFrame:
    """Convert the demand surface into FOCUS-conformant usage rows."""
    from costproof.simulate.pricebook import SKU_INDEX

    n_days = len(dates)
    quantity = np.exp(log_q)

    # Which resources are covered by a commitment. Only eligible SKUs, and coverage is
    # a property of the resource for the whole window (a simplification -- real coverage
    # floats daily, but this keeps the amortisation logic legible).
    covered = np.zeros(len(resources), dtype=bool)
    for i, row in enumerate(resources.itertuples(index=False)):
        sku = SKU_INDEX[row.sku_id]
        if sku.commitment_eligible and rng.random() < cfg.commitment_coverage:
            covered[i] = True

    blocks = []
    for i, row in enumerate(resources.itertuples(index=False)):
        sku = SKU_INDEX[row.sku_id]
        q = quantity[i]

        list_cost = q * sku.list_unit_price
        contracted_cost = list_cost * (1.0 - sku.enterprise_discount)

        if covered[i]:
            effective = contracted_cost * (1.0 - sku.commitment_discount)
            # Committed usage is prepaid, so the usage row itself shows zero billed cost.
            # Anyone analysing BilledCost per resource sees zero here and concludes the
            # resource is free. It is not. This is why EffectiveCost is the correct
            # measure and why FOCUS separates them.
            billed = np.zeros(n_days)
            pricing_cat = PricingCategory.COMMITTED.value
            commit_id = f"cd-{row.sku_id}"
            commit_status = CommitmentDiscountStatus.USED.value
            commit_cat = (
                CommitmentDiscountCategory.USAGE.value
                if sku.pricing_unit == "Hours"
                else CommitmentDiscountCategory.SPEND.value
            )
        else:
            effective = contracted_cost
            billed = contracted_cost
            pricing_cat = PricingCategory.STANDARD.value
            commit_id = None
            commit_status = None
            commit_cat = None

        starts = dates
        ends = dates + pd.Timedelta(days=1)
        blocks.append(
            pd.DataFrame(
                {
                    "BillingAccountId": "acct-000100",
                    "BillingAccountName": "Contoso Global Holdings",
                    "SubAccountId": row.business_unit_true,
                    "SubAccountName": row.business_unit_true,
                    "BillingPeriodStart": starts.to_period("M").to_timestamp(),
                    "BillingPeriodEnd": (
                        starts.to_period("M").to_timestamp() + pd.offsets.MonthBegin(1)
                    ),
                    "ChargePeriodStart": starts,
                    "ChargePeriodEnd": ends,
                    "BillingCurrency": "USD",
                    "BilledCost": billed,
                    "EffectiveCost": effective,
                    "ListCost": list_cost,
                    "ContractedCost": contracted_cost,
                    "ProviderName": sku.provider,
                    "PublisherName": sku.provider,
                    "InvoiceIssuerName": sku.provider,
                    "ServiceName": sku.service_name,
                    "ServiceCategory": sku.service_category.value,
                    "ResourceId": row.resource_id,
                    "ResourceName": row.resource_name,
                    "ResourceType": sku.resource_type,
                    "RegionId": row.region_id,
                    "RegionName": row.region_name,
                    "AvailabilityZone": None,
                    "ChargeCategory": ChargeCategory.USAGE.value,
                    "ChargeClass": None,
                    "ChargeDescription": f"{sku.service_name} {sku.resource_type} usage",
                    "ChargeFrequency": ChargeFrequency.USAGE_BASED.value,
                    "ConsumedQuantity": q,
                    "ConsumedUnit": sku.consumed_unit,
                    "PricingQuantity": q,
                    "PricingUnit": sku.pricing_unit,
                    "ListUnitPrice": sku.list_unit_price,
                    "ContractedUnitPrice": sku.list_unit_price * (1 - sku.enterprise_discount),
                    "PricingCategory": pricing_cat,
                    "SkuId": sku.sku_id,
                    "SkuPriceId": f"{sku.sku_id}-{row.region_id}",
                    "CommitmentDiscountId": commit_id,
                    "CommitmentDiscountName": (
                        f"1yr commitment {sku.resource_type}" if commit_id else None
                    ),
                    "CommitmentDiscountCategory": commit_cat,
                    "CommitmentDiscountType": (
                        "Savings Plan" if commit_cat == "Spend"
                        else ("Reserved Instance" if commit_cat else None)
                    ),
                    "CommitmentDiscountStatus": commit_status,
                    # NOTE: the membership test is `isinstance(v, str)`, not `v is not
                    # None`. pandas coerces None in an object column to NaN, and
                    # `NaN is not None` evaluates True -- so the naive test silently
                    # tagged every untagged resource with a NaN owner and reported a 0%
                    # untagged rate. Requiring a real string is the robust check.
                    "Tags": [
                        {
                            k: v
                            for k, v in (
                                ("business_unit", row.business_unit),
                                ("cost_centre", row.cost_centre),
                                ("environment", row.environment),
                            )
                            if isinstance(v, str)
                        }
                    ]
                    * n_days,
                }
            )
        )

    usage = pd.concat(blocks, ignore_index=True)
    purchases = _commitment_purchase_rows(cfg, usage, dates)
    if purchases.empty:
        return usage

    # Align dtypes before concatenating. Purchase rows legitimately have no
    # resource, no quantity and no SKU, so those columns arrive as all-NA.
    # pandas 2.x warns that all-NA columns will stop participating in dtype
    # inference in a future version, which would silently change the result's
    # types. Casting each all-NA column to the dtype it must end up with makes
    # the outcome explicit and identical across pandas versions.
    #
    # Reproducing this required matching the reporting machine's pandas 2.3.3;
    # it does not fire on 3.0. Environment differences are part of the bug.
    purchases = purchases.reindex(columns=usage.columns)
    for column, dtype in usage.dtypes.items():
        if purchases[column].isna().all():
            purchases[column] = purchases[column].astype(dtype)
    return pd.concat([usage, purchases], ignore_index=True)


def _commitment_purchase_rows(cfg: SimConfig, usage: pd.DataFrame,
                              dates: pd.DatetimeIndex) -> pd.DataFrame:
    """Monthly commitment purchase rows, including the portion that goes unused.

    FOCUS models a commitment as a Purchase charge (money leaves) plus Usage charges
    that draw against it. Unused commitment is real, recurring, invisible waste: it
    never appears as a resource, so no rightsizing exercise will ever find it. In this
    estate it is worth roughly `commitment_waste_rate` of all committed spend.
    """
    committed = usage[usage["PricingCategory"] == PricingCategory.COMMITTED.value]
    if committed.empty:
        return pd.DataFrame(columns=usage.columns)

    monthly = (
        committed.assign(month=committed["ChargePeriodStart"].dt.to_period("M"))
        .groupby("month", as_index=False)["EffectiveCost"]
        .sum()
    )

    rows = []
    for rec in monthly.itertuples(index=False):
        used_amount = float(rec.EffectiveCost)
        # Purchase covers used commitment plus the unused share that was paid for anyway.
        purchase = used_amount / max(1e-9, (1.0 - cfg.commitment_waste_rate))
        unused = purchase - used_amount
        start = rec.month.to_timestamp()
        end = start + pd.offsets.MonthBegin(1)
        for label, amount, status in (
            ("commitment purchase", purchase, CommitmentDiscountStatus.USED.value),
            ("unused commitment", unused, CommitmentDiscountStatus.UNUSED.value),
        ):
            if label == "unused commitment" and unused <= 0:
                continue
            rows.append(
                {
                    "BillingAccountId": "acct-000100",
                    "BillingAccountName": "Contoso Global Holdings",
                    "SubAccountId": None,
                    "SubAccountName": None,
                    "BillingPeriodStart": start,
                    "BillingPeriodEnd": end,
                    "ChargePeriodStart": start,
                    "ChargePeriodEnd": end,
                    "BillingCurrency": "USD",
                    # Purchase rows carry BilledCost; their EffectiveCost is zero because
                    # the value is amortised onto the usage rows that consumed them.
                    # Double counting here is the classic FinOps accounting error.
                    "BilledCost": amount if label == "commitment purchase" else 0.0,
                    "EffectiveCost": unused if label == "unused commitment" else 0.0,
                    "ListCost": amount if label == "commitment purchase" else 0.0,
                    "ContractedCost": amount if label == "commitment purchase" else 0.0,
                    "ProviderName": "AWS",
                    "PublisherName": "AWS",
                    "InvoiceIssuerName": "AWS",
                    "ServiceName": "Commitment Discounts",
                    "ServiceCategory": ServiceCategory.MANAGEMENT.value,
                    "ResourceId": None,
                    "ResourceName": None,
                    "ResourceType": None,
                    "RegionId": None,
                    "RegionName": None,
                    "AvailabilityZone": None,
                    "ChargeCategory": ChargeCategory.PURCHASE.value,
                    "ChargeClass": None,
                    "ChargeDescription": label,
                    "ChargeFrequency": ChargeFrequency.RECURRING.value,
                    "ConsumedQuantity": None,
                    "ConsumedUnit": None,
                    "PricingQuantity": None,
                    "PricingUnit": None,
                    "ListUnitPrice": None,
                    "ContractedUnitPrice": None,
                    "PricingCategory": PricingCategory.COMMITTED.value,
                    "SkuId": None,
                    "SkuPriceId": None,
                    "CommitmentDiscountId": "cd-portfolio",
                    "CommitmentDiscountName": "Enterprise commitment portfolio",
                    "CommitmentDiscountCategory": CommitmentDiscountCategory.SPEND.value,
                    "CommitmentDiscountType": "Savings Plan",
                    "CommitmentDiscountStatus": status,
                    "Tags": {},
                }
            )
    return pd.DataFrame(rows)


# =======================================================================================
# Entry point
# =======================================================================================


def generate(cfg: SimConfig | None = None) -> Estate:
    """Generate a complete estate with ground truth. Deterministic given ``cfg.seed``."""
    cfg = cfg or SimConfig()
    rng = np.random.default_rng(cfg.seed)
    dates = pd.date_range(cfg.start_date, periods=cfg.n_days, freq="D")

    resources = _build_resources(rng, cfg)
    drivers = _build_drivers(rng, cfg, dates)

    log_q = _base_demand(rng, cfg, resources, dates)
    waste_events = _inject_waste(rng, cfg, log_q, resources)
    interventions = _assign_interventions(rng, cfg, log_q, resources, dates, waste_events)

    billing = _to_billing(rng, cfg, log_q, resources, dates)

    return Estate(
        billing=billing,
        resources=resources,
        drivers=drivers,
        interventions=interventions,
        waste_events=waste_events,
        config=cfg,
    )
