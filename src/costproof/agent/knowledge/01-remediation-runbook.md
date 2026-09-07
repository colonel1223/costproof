# Remediation runbook

Standard responses to each waste pattern, with the risk each carries. The agent
retrieves from this to explain *what to do*, and never invents a remediation that
is not written here.

## Idle over-provisioned compute

**Signature.** Cost is flat across weekdays and weekends, coefficient of variation
below roughly 0.15, and the resource never approaches its provisioned ceiling.

**Action.** Rightsize to the next smaller instance family, or enable autoscaling with
a floor at observed p50 utilisation.

**Risk.** Rightsizing on an unrepresentative window under-provisions for real peaks.
Require at least 28 days of observation covering a month-end close before acting on a
production resource.

**Typical effect.** 20-45% reduction in that resource's cost.

## Orphaned storage volumes and snapshots

**Signature.** Storage billing continues after the attached compute resource stops
appearing in the billing feed. Cost is perfectly flat with zero variance.

**Action.** Snapshot the volume, verify the snapshot restores, then delete the volume.
Snapshots older than the retention policy are deleted outright.

**Risk.** Irreversible. Volumes are sometimes retained deliberately for compliance or
forensic hold. Confirm ownership before deletion; where the resource is untagged,
ownership cannot be confirmed from the billing feed alone and the action must be
escalated to a human.

**Typical effect.** 100% of that resource's cost, permanently.

## Zombie non-production environments

**Signature.** A development, staging or sandbox environment billing at a constant rate
with no weekend dip and no deployment activity for an extended period.

**Action.** Schedule shutdown outside working hours (typically 19:00-07:00 and weekends),
which removes roughly 70% of billable hours without deleting anything.

**Risk.** Low. Scheduling is reversible. Confirm no long-running batch jobs or overnight
CI pipelines depend on the environment.

**Typical effect.** 55-70% reduction for that environment.

## Runaway jobs and retry storms

**Signature.** Sharp onset, sustained elevation, often with a step change rather than a
ramp. Common causes are an unbounded retry loop, a stuck training job, or a query with
no result limit.

**Action.** Terminate the job, then add a budget guard: a maximum retry count, a job
timeout, or a query scan limit.

**Risk.** Terminating a legitimate long-running job destroys its work. Confirm with the
owning team before terminating anything in production.

**Typical effect.** Returns to baseline, but the fix is the guard rather than the
termination.

## Unused commitment

**Signature.** `CommitmentDiscountStatus = 'Unused'` on purchase rows. Capacity was
prepaid and never consumed.

**Action.** Not a resource-level fix. Either shift eligible on-demand workloads onto the
unused commitment, or resize the commitment at renewal. Some providers permit selling
unused reservations on a marketplace.

**Risk.** None from analysis. The commitment is already sunk; the only decision is
whether to renew at the same level.

**Note.** This waste is invisible to every rightsizing exercise, because it is attached
to no resource. There is nothing to resize. It appears only if you look for it directly.

## Untagged spend

**Signature.** No `business_unit` tag on the resource.

**Action.** Not a cost fix — a governance fix. Identify the owner from the account,
region and naming convention, apply tags, and enforce tagging at provisioning time
through policy.

**Risk.** None. But untagged spend cannot be allocated, so it cannot appear in any
unit-economics calculation, and no team's budget carries it. Nobody has a reason to
reduce it.
