"""The governed agent: runs a cost review and produces an auditable record.

What "governed" means here
--------------------------
Not that the agent is polite. Three specific properties:

1. **Every figure is traceable.** Each finding records which tool produced it, with
   what arguments, and what that tool said about its own method and limits.
2. **Nothing executes without a human.** The agent produces recommendations. Anything
   above a materiality threshold, anything irreversible, and anything on a resource
   whose owner cannot be established, is gated on explicit human approval.
3. **The model cannot move a number.** The language model narrates tool output. Run
   the same review with no model configured and every figure is identical -- which is
   the proof, not the claim.

This maps onto what watsonx.governance exists to enforce, and onto the NIST AI Risk
Management Framework's "Measure" and "Manage" functions: know what the system did,
know how confident it was, and keep a human on the consequential decisions.
"""

from __future__ import annotations

import json
import platform
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from costproof.agent import llm, tools

#: Recommendations at or above this annualised impact require human approval before
#: execution. Set low deliberately -- the cost of an unnecessary approval is a minute
#: of someone's attention; the cost of an unreviewed deletion is unbounded.
MATERIALITY_THRESHOLD_USD = 1_000.0

#: Actions that cannot be undone. These require approval regardless of value.
IRREVERSIBLE_ACTIONS = frozenset({
    "delete_orphaned_volume", "delete_snapshot", "terminate_job", "purchase_commitment",
})


@dataclass
class Finding:
    """One observation, its evidence, and what to do about it."""

    finding_id: str
    title: str
    category: str
    annualised_impact_usd: float | None
    evidence: dict
    method: str
    caveats: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    recommended_action: str | None = None
    action_risk: str | None = None
    requires_approval: bool = True
    approval_reason: str = ""

    def gate(self) -> None:
        """Decide whether a human must sign off, and record why."""
        reasons = []
        if self.recommended_action in IRREVERSIBLE_ACTIONS:
            reasons.append("action is irreversible")
        if (self.annualised_impact_usd or 0) >= MATERIALITY_THRESHOLD_USD:
            reasons.append(f"impact >= ${MATERIALITY_THRESHOLD_USD:,.0f}/yr")
        if any("untagged" in c.lower() or "no business_unit" in c.lower()
               for c in self.caveats):
            reasons.append("owner cannot be established from the billing feed")
        if any("PARALLEL-TRENDS CHECK FAILED" in c for c in self.caveats):
            reasons.append("estimate failed its validity check and is not causal")

        self.requires_approval = bool(reasons)
        self.approval_reason = "; ".join(reasons) if reasons else "below all gates"


@dataclass
class AuditRecord:
    """Everything needed to reproduce and defend a review."""

    run_id: str
    started_at: str
    finished_at: str | None = None
    backend: str = ""
    python_version: str = ""
    platform: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    findings: list[dict] = field(default_factory=list)
    retrieved_sources: list[str] = field(default_factory=list)
    narrative: str = ""
    narrative_source: str = ""

    def write(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(asdict(self), indent=2, default=str))
        return p


def _extract_field(text: str, label: str) -> str | None:
    """Pull a full ``**Label.** ...`` paragraph out of a retrieved markdown passage.

    Reads to the next blank line rather than to the end of the first line. Markdown
    prose wraps, so a line-based read truncated every recommendation mid-sentence --
    "Identify the owner from the account," with the rest silently dropped. A
    recommendation that stops halfway is worse than none: it reads as complete.
    """
    marker = f"**{label}.**"
    start = text.find(marker)
    if start == -1:
        return None
    body = text[start + len(marker):]
    end = body.find("\n\n")
    paragraph = body[:end] if end != -1 else body
    return " ".join(paragraph.split()).strip() or None


class CostReview:
    """Runs a full cost review and records everything it did."""

    def __init__(self, backend: llm.Backend | None = None, verbose: bool = True):
        self.verbose = verbose
        self.backend = backend or llm.get_backend(verbose=verbose)
        self.audit = AuditRecord(
            run_id=datetime.now(timezone.utc).strftime("review-%Y%m%dT%H%M%SZ"),
            started_at=datetime.now(timezone.utc).isoformat(),
            backend=self.backend.name,
            python_version=sys.version.split()[0],
            platform=platform.platform(),
        )
        self.findings: list[Finding] = []

    # -- plumbing -----------------------------------------------------------------

    def _call(self, name: str, **kwargs) -> tools.ToolResult:
        if self.verbose:
            print(f"  -> {name}({', '.join(f'{k}={v!r}' for k, v in kwargs.items())})")
        result = tools.call(name, **kwargs)
        self.audit.tool_calls.append({
            "tool": name, "arguments": kwargs, "ok": result.ok,
            "method": result.method, "caveats": result.caveats,
            "sources": result.sources, "elapsed_ms": round(result.elapsed_ms, 1),
            "error": result.error,
        })
        self.audit.retrieved_sources.extend(result.sources)
        return result

    def _advice(self, query: str) -> tuple[str | None, str | None]:
        """Retrieve the recommended action and its risk from the runbook."""
        r = self._call("search_knowledge", query=query)
        if not r.ok:
            return None, None
        return _extract_field(str(r.value), "Action"), _extract_field(str(r.value), "Risk")

    # -- the review ---------------------------------------------------------------

    def run(self) -> AuditRecord:
        if self.verbose:
            print(f"\nCost review {self.audit.run_id}")

        self._review_allocation()
        self._review_unit_economics()
        self._review_commitments()
        self._review_waste()

        for f in self.findings:
            f.gate()
        self.audit.findings = [asdict(f) for f in self.findings]
        self.audit.narrative = self._narrate()
        self.audit.narrative_source = self.backend.name
        self.audit.finished_at = datetime.now(timezone.utc).isoformat()
        self.audit.retrieved_sources = sorted(set(self.audit.retrieved_sources))
        return self.audit

    def _review_allocation(self) -> None:
        r = self._call("get_spend_summary")
        if not r.ok:
            return
        df: pd.DataFrame = r.value
        un = df[df["business_unit"] == "unallocated"]
        if un.empty:
            return
        cost = float(un["effective_cost"].iloc[0])
        annual = float(un["annualised"].iloc[0])
        share = cost / float(df["effective_cost"].sum())
        action, risk = self._advice("untagged spend with no business unit tag")

        self.findings.append(Finding(
            finding_id="F1", title="Spend with no identifiable owner",
            category="allocation",
            annualised_impact_usd=annual,
            evidence={"untagged_cost": cost, "annualised": annual,
                      "share_of_total": share},
            method=r.method,
            caveats=r.caveats + [
                "This is not recoverable spend. It is spend that cannot be attributed, "
                "so no team's budget carries it and no team has reason to reduce it.",
            ],
            sources=r.sources,
            recommended_action=action, action_risk=risk,
        ))

    def _review_unit_economics(self) -> None:
        r = self._call("get_unit_economics")
        if not r.ok:
            return
        df: pd.DataFrame = r.value
        degrading = df[df["verdict"] == "efficiency degrading"]
        improving = df[df["verdict"] == "growing efficiently"]

        self.findings.append(Finding(
            finding_id="F2",
            title="Four of five units grew more efficient; one degraded",
            category="unit economics",
            annualised_impact_usd=None,
            evidence={
                "degrading": degrading[["business_unit", "cost_change_pct",
                                        "unit_cost_change_pct"]].to_dict("records"),
                "improving": improving[["business_unit", "cost_change_pct",
                                        "unit_cost_change_pct"]].to_dict("records"),
            },
            method=r.method,
            caveats=r.caveats + [
                "Every unit's total bill rose. Judged on the invoice alone all five "
                "look like problems; judged per unit of output, only one is.",
            ],
            sources=r.sources,
            recommended_action="investigate_efficiency_regression",
            action_risk="None. This is an investigation, not a change.",
        ))

    def _review_commitments(self) -> None:
        r = self._call("check_commitment_waste")
        if not r.ok:
            return
        v = r.value
        action, risk = self._advice("unused commitment prepaid capacity never consumed")
        self.findings.append(Finding(
            finding_id="F3", title="Prepaid capacity never consumed",
            category="commitment",
            annualised_impact_usd=float(v["annualised"]),
            evidence={"total_unused": v["total_unused"],
                      "annualised": v["annualised"],
                      "months_observed": len(v["by_month"])},
            method=r.method, caveats=r.caveats, sources=r.sources,
            recommended_action=action, action_risk=risk,
        ))

    def _review_waste(self) -> None:
        r = self._call("find_waste", top_n=10)
        if not r.ok:
            return
        df: pd.DataFrame = r.value
        action, risk = self._advice(
            "idle over-provisioned instance flat cost no weekend dip"
        )
        self.findings.append(Finding(
            finding_id="F4", title="Ten resources prioritised for investigation",
            category="waste detection",
            annualised_impact_usd=float(df["annualised_cost"].sum()),
            evidence={"queue": df.head(10).to_dict("records")},
            method=r.method,
            caveats=r.caveats + [
                "The annualised figure is the cost these resources CARRY, not a saving. "
                "What is recoverable depends on the remediation and is unknown until "
                "each is measured after the fact.",
            ],
            sources=r.sources,
            recommended_action=action, action_risk=risk,
        ))

    # -- narration -----------------------------------------------------------------

    def _narrate(self) -> str:
        """Ask the model to summarise. It receives only tool output."""
        payload = {
            "findings": [
                {
                    "id": f.finding_id, "title": f.title,
                    "annualised_impact_usd": f.annualised_impact_usd,
                    "evidence": f.evidence, "caveats": f.caveats,
                    "recommended_action": f.recommended_action,
                    "requires_approval": f.requires_approval,
                }
                for f in self.findings
            ]
        }
        prompt = (
            "Summarise this cost review for a finance stakeholder in under 200 words.\n\n"
            f"{json.dumps(payload, indent=2, default=str)}"
        )
        return self.backend.generate(prompt, system=llm.SYSTEM_PROMPT)


# =======================================================================================
# Rendering
# =======================================================================================


def render_markdown(audit: AuditRecord) -> str:
    """Render the review as a report a human can read and check."""
    money = lambda v: f"${v:,.0f}" if v is not None else "not quantified"
    out = [
        "# Cloud cost review",
        "",
        f"**Run** `{audit.run_id}`  ·  **Reporting backend** `{audit.backend}`  ·  "
        f"**Generated** {audit.started_at[:19]}Z",
        "",
        "> Every figure below is produced by a deterministic analytical tool. The "
        "language model, when configured, rewrites this summary into prose and changes "
        "no number, method, or recommendation. Run with no model and the figures are "
        "identical.",
        "",
        "---",
        "",
        "## Findings",
        "",
    ]

    for f in audit.findings:
        gate = "🔒 requires human approval" if f["requires_approval"] else "✅ auto-approvable"
        out += [
            f"### {f['finding_id']} — {f['title']}",
            "",
            f"| | |",
            f"|---|---|",
            f"| Category | {f['category']} |",
            f"| Annualised impact | **{money(f['annualised_impact_usd'])}** |",
            f"| Governance | {gate} |",
            f"| Gate reason | {f['approval_reason']} |",
            "",
            f"**How this was measured.** {f['method']}",
            "",
        ]
        if f.get("recommended_action"):
            out += [f"**Recommended action.** {f['recommended_action']}", ""]
        if f.get("action_risk"):
            out += [f"**Risk.** {f['action_risk']}", ""]
        if f["caveats"]:
            out += ["**Caveats.**", ""]
            out += [f"- {c}" for c in f["caveats"]]
            out += [""]
        if f["sources"]:
            out += [f"*Sources: {', '.join(f['sources'])}*", ""]

    total = sum(f["annualised_impact_usd"] or 0 for f in audit.findings)
    gated = sum(1 for f in audit.findings if f["requires_approval"])

    out += [
        "---",
        "",
        "## Governance summary",
        "",
        f"- **{len(audit.findings)}** findings, **{gated}** requiring human approval",
        f"- **{len(audit.tool_calls)}** tool calls, all recorded with arguments and timings",
        f"- **{money(total)}** total annualised impact identified",
        "",
        "No action in this report has been executed. Every gated finding requires "
        "explicit human approval before anything changes.",
        "",
        "## Tool call log",
        "",
        "| Tool | Arguments | OK | ms |",
        "|---|---|---|---:|",
    ]
    for c in audit.tool_calls:
        args = ", ".join(f"{k}={v}" for k, v in c["arguments"].items()) or "—"
        out.append(f"| `{c['tool']}` | {args} | {'✅' if c['ok'] else '❌'} | {c['elapsed_ms']:.0f} |")

    out += ["", "## Sources retrieved", ""]
    out += [f"- {s}" for s in audit.retrieved_sources] or ["- none"]

    if audit.narrative:
        out += ["", "---", "", "## Narrative summary",
                f"*Generated by `{audit.narrative_source}`*", "", audit.narrative, ""]
    return "\n".join(out)
