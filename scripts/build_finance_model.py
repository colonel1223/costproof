"""Build the CostProof finance model: the engine's output as an FP&A workbook.

Where `build_business_case.py` argues for *adopting* CostProof (the NPV of the tool),
this workbook is what a finance team would actually *receive* from it: every
optimisation event, what each one is credited with and on what evidence, spend against
a budget, a forecast, the programme's ROI, and the scenarios and sensitivities an FP&A
analyst would be asked for before anyone signs.

Rules the file is built to
--------------------------
* **Data cells are data; everything else is a formula.** Raw monthly cost, the event
  register and the per-event estimates are values read from the pipeline's outputs.
  Every total, variance, budget, forecast, NPV and ratio is an Excel formula that
  references a labelled cell. Change an assumption and the whole book moves.
* **Blue is an input, black is a formula, green is a link to another sheet.** The
  finance convention, so a reader knows what they may touch without reading a formula.
* **Simulated figures are labelled simulated on every sheet that shows them.** The
  estate is synthetic. The *method* is what the workbook demonstrates; the dollar
  figures illustrate it. Ground truth exists only because the estate is simulated, and
  every column that uses it says so.
* **Attribution is conservative by default.** Only estimates that pass the
  parallel-trends check are credited. The switch is on the Assumptions sheet, and
  flipping it is visible in every downstream number.
* **No hard-coded cell addresses across sheets.** Each builder returns the addresses
  of the cells it created; later sheets reference those. Three earlier workbooks
  evaluated cleanly with wrong numbers because a row moved and a `$B$11` did not.

Run:  python scripts/build_finance_model.py
Then: python <xlsx skill>/recalc.py reports/costproof-finance-model.xlsx
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, Reference
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

ROOT = Path(__file__).resolve().parents[1]
SILVER = ROOT / "data" / "silver" / "billing.parquet"
INTERVENTIONS = ROOT / "data" / "gold" / "ground_truth_interventions.parquet"
STUDY = ROOT / "outputs" / "tables" / "study_results.parquet"
OUT = ROOT / "reports" / "costproof-finance-model.xlsx"

# --- house style (shared with build_business_case.py) ------------------------------------
FONT = "Arial"
BLUE, GREEN, NAVY, GREY, RED = "0000FF", "008000", "1F3864", "595959", "C00000"
H1 = Font(name=FONT, size=16, bold=True, color=NAVY)
H2 = Font(name=FONT, size=10, bold=True, color="FFFFFF")
H3 = Font(name=FONT, size=11, bold=True, color=NAVY)
LABEL = Font(name=FONT, size=10)
LABEL_B = Font(name=FONT, size=10, bold=True)
INPUT = Font(name=FONT, size=10, color=BLUE)
LINK = Font(name=FONT, size=10, color=GREEN)
LINK_B = Font(name=FONT, size=10, bold=True, color=GREEN)
NOTE = Font(name=FONT, size=9, italic=True, color=GREY)
BIG = Font(name=FONT, size=18, bold=True, color=NAVY)
WARN = Font(name=FONT, size=9, italic=True, color=RED)

HDR_FILL = PatternFill("solid", fgColor=NAVY)
KEY_FILL = PatternFill("solid", fgColor="FFFF00")
BAND = PatternFill("solid", fgColor="F2F2F2")
SIM_FILL = PatternFill("solid", fgColor="FDE9D9")   # simulated-truth columns
TILE_FILL = PatternFill("solid", fgColor="EAF1FB")
THIN = Side(style="thin", color="BFBFBF")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
TOPLINE = Border(top=Side(style="thin", color=NAVY))

MONEY = '$#,##0;($#,##0);-'
PCT = '0.0%;(0.0%);-'
PCT0 = '0%;(0%);-'
NUM3 = '0.000;(0.000);-'
NUM1 = '#,##0.0;(#,##0.0);-'
INT = '#,##0;(#,##0);-'
MULT = '0.00"x"'
DATE = 'yyyy-mm-dd'
MON = 'mmm yyyy'

SIM_NOTE = ("SIMULATED DATA. The estate is synthetic (FOCUS 1.2, 400 resources, 540 days). "
            "Figures illustrate the method; ground truth exists only because the data is "
            "simulated.")

SHEETS = {
    "exec": "01 Executive Summary",
    "raw": "02 Raw Cost Data",
    "events": "03 Optimization Events",
    "attr": "04 Savings Attribution",
    "bva": "05 Budget vs Actual",
    "fcst": "06 Forecast",
    "roi": "07 ROI Analysis",
    "scen": "08 Scenario Analysis",
    "sens": "09 Sensitivity Analysis",
    "assume": "10 Assumptions",
    "kpi": "11 KPI Dashboard",
}


def q(sheet: str) -> str:
    """Quote a sheet name for a cross-sheet reference."""
    return f"'{sheet}'"


def ref(sheet: str, cell: str, absolute: bool = True) -> str:
    if absolute:
        col = "".join(ch for ch in cell if ch.isalpha())
        row = "".join(ch for ch in cell if ch.isdigit())
        cell = f"${col}${row}"
    return f"{q(sheet)}!{cell}"


# =======================================================================================
# Small helpers
# =======================================================================================


def title(ws, text: str, subtitle: str = "", simulated: bool = True):
    ws["A1"] = text
    ws["A1"].font = H1
    ws["A2"] = subtitle
    ws["A2"].font = NOTE
    if simulated:
        ws["A3"] = SIM_NOTE
        ws["A3"].font = WARN
    ws.freeze_panes = "A5"


def header(ws, row: int, headers: list[str], start_col: int = 1, fill=HDR_FILL):
    for i, h in enumerate(headers):
        c = ws.cell(row=row, column=start_col + i, value=h)
        c.font = H2
        c.fill = fill
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = BOX
    ws.row_dimensions[row].height = 30


def widths(ws, spec: dict[str, float]):
    for col, w in spec.items():
        ws.column_dimensions[col].width = w


def put(ws, cell: str, value, font=LABEL, fmt: str | None = None, fill=None, bold=False):
    c = ws[cell]
    c.value = value
    c.font = Font(name=FONT, size=font.size, bold=bold or font.bold, color=font.color,
                  italic=font.italic)
    if fmt:
        c.number_format = fmt
    if fill:
        c.fill = fill
    return cell


def col(i: int) -> str:
    return get_column_letter(i)


# =======================================================================================
# Data
# =======================================================================================


@dataclass
class Data:
    monthly: pd.DataFrame          # month, business_unit, effective, billed, list
    months: list[pd.Timestamp]     # complete months only
    bus: list[str]
    events: pd.DataFrame           # one row per intervention with estimates
    n_complete_months: int = 0
    expected: dict = field(default_factory=dict)   # figures the workbook must reproduce


def load() -> Data:
    for p in (SILVER, INTERVENTIONS, STUDY):
        if not p.exists():
            sys.exit(f"missing {p.relative_to(ROOT)} -- run `costproof data`, `warehouse` "
                     f"and `study` first")

    # Complete months only: the last month of the estate is partial and would read as a
    # spend collapse in any budget-vs-actual view.
    last = duckdb.sql(f"SELECT MAX(charge_date) FROM '{SILVER}'").fetchone()[0]
    cutoff = pd.Timestamp(last).to_period("M").to_timestamp()   # first day of last month
    monthly = duckdb.sql(f"""
        SELECT DATE_TRUNC('month', charge_date) AS month, business_unit,
               SUM(effective_cost) AS effective, SUM(billed_cost) AS billed,
               SUM(list_cost) AS list
        FROM '{SILVER}'
        WHERE charge_category = 'Usage' AND charge_date < DATE '{cutoff.date()}'
        GROUP BY 1, 2 ORDER BY 1, 2
    """).df()
    monthly["month"] = pd.to_datetime(monthly["month"])
    months = sorted(monthly["month"].unique())
    bus = sorted(monthly["business_unit"].unique())

    # Per-intervention estimates, joined to the event register.
    study = pd.read_parquet(STUDY)
    gt = pd.read_parquet(INTERVENTIONS)
    naive = study[study["method"] == "naive_before_after"].set_index("intervention_id")
    did = study[study["method"] == "did_permutation"].set_index("intervention_id")

    costs = duckdb.sql(f"""
        SELECT charge_date, resource_id, SUM(effective_cost) AS cost
        FROM '{SILVER}' WHERE charge_category = 'Usage' GROUP BY 1, 2
    """).df().pivot(index="charge_date", columns="resource_id", values="cost").fillna(0.0)
    costs.index = pd.to_datetime(costs.index)

    ev = gt.set_index("intervention_id").copy()
    ev = ev.loc[ev.index.intersection(did.index)]
    ev["run_rate"] = [
        float(costs.loc[costs.index < pd.Timestamp(r.start_date), r.resource_id].tail(84).mean())
        for r in ev.itertuples()
    ]
    ev["n_donors"] = did["n_donors"]
    ev["naive_coef"] = naive["coef"]
    ev["did_coef"] = did["coef"]
    ev["did_ci_low"] = did["ci_low"]
    ev["did_ci_high"] = did["ci_high"]
    ev["did_p"] = did["p_value"]
    ev["pt_passed"] = did["pt_passed"].astype(bool)
    ev = ev.reset_index().sort_values("start_date").reset_index(drop=True)

    # Figures the finished workbook must reproduce (checked after recalculation).
    def annual(coef):
        return -np.expm1(coef) * ev["run_rate"] * 365
    exp = {
        "naive_total": float(annual(ev["naive_coef"]).sum()),
        "did_total": float(annual(ev["did_coef"]).sum()),
        "did_pt_total": float(annual(ev["did_coef"])[ev["pt_passed"]].sum()),
        "true_total": float(annual(ev["true_effect_log"]).sum()),
        "gross_misattr": float((annual(ev["naive_coef"]) - annual(ev["true_effect_log"])).abs().sum()),
        "month1_total": float(monthly[monthly["month"] == months[0]]["effective"].sum()),
        "all_months_total": float(monthly["effective"].sum()),
        "n_events": int(len(ev)),
        "n_pt": int(ev["pt_passed"].sum()),
        "n_null": int(ev["is_null"].sum()),
    }
    return Data(monthly=monthly, months=list(months), bus=bus, events=ev,
                n_complete_months=len(months), expected=exp)


# =======================================================================================
# 10 Assumptions
# =======================================================================================


def sheet_assumptions(wb: Workbook) -> dict[str, str]:
    ws = wb.create_sheet(SHEETS["assume"])
    title(ws, "Assumptions",
          "Blue cells are inputs. Everything else in the workbook is a formula that reads "
          "them. Change one here and every sheet moves.", simulated=False)
    widths(ws, {"A": 46, "B": 16, "C": 16, "D": 16, "E": 60})
    A: dict[str, str] = {}
    r = 5

    def section(text):
        nonlocal r
        ws.cell(row=r, column=1, value=text).font = H3
        r += 1

    def inp(key, label, value, fmt, note="", key_cell=False):
        nonlocal r
        put(ws, f"A{r}", label)
        put(ws, f"B{r}", value, INPUT, fmt, KEY_FILL if key_cell else None)
        put(ws, f"E{r}", note, NOTE)
        A[key] = f"B{r}"
        r += 1

    section("Financial")
    inp("rate", "Discount rate (cost of capital)", 0.12, PCT,
        "Applied to programme cash flows. Sensitivity sheet varies it 8%–20%.", True)
    inp("horizon", "Analysis horizon (years)", 5, INT, "Fixed at 5 in the ROI layout.")
    inp("persist", "Savings persistence, year over year", 0.85, PCT,
        "Share of an attributed saving still realised the following year. Optimisations "
        "erode: resources get re-provisioned, autoscaling floors drift up.", True)
    r += 1

    section("Programme cost")
    inp("fte", "FinOps engineers (FTE)", 1.5, NUM1, "Who runs the measurement and remediation.")
    inp("loaded", "Loaded annual cost per FTE", 180_000, MONEY,
        "Salary, benefits, overhead. Change to your organisation's rate.")
    inp("tooling", "Tooling and platform, per year", 60_000, MONEY,
        "Billing export, warehouse, watsonx.ai inference, workstation.")
    inp("setup", "One-time setup (year 0)", 50_000, MONEY,
        "FOCUS export wiring, warehouse build, tagging remediation sprint.")
    inp("eng_hours", "Engineering hours per intervention", 16, NUM1,
        "Executing a rightsizing, a storage migration or a cluster consolidation is "
        "engineering time. Counted once per event, in year 0.", True)
    inp("eng_rate", "Loaded engineering rate per hour", 150, MONEY,
        "Blended senior/mid rate including overhead.")
    inp("ramp", "Year-1 ramp (share of run-rate realised in year 1)", 0.50, PCT0,
        "Interventions land through the year; a linear ramp realises half the annual "
        "run-rate in year 1 and the full run-rate from year 2.", True)
    r += 1

    section("Planning")
    inp("growth_budget", "Budget growth assumption (annual)", 0.15, PCT,
        "Budget = run-rate of the first six months, grown monthly at this rate. "
        "Illustrates the method; a real plan replaces this row.", True)
    inp("baseline_months", "Months defining the run-rate budget", 6, INT,
        "First N complete months average to the baseline. Fixed at 6 in the layout.")
    r += 1

    section("Attribution policy")
    put(ws, f"A{r}", "Credit estimates that fail the parallel-trends check?")
    put(ws, f"B{r}", "No", INPUT, None, KEY_FILL)
    put(ws, f"E{r}", "No = only estimates whose pre-trend check passed are credited "
                     "(recommended). Yes = credit every identified estimate.", NOTE)
    dv = DataValidation(type="list", formula1='"No,Yes"', allow_blank=False)
    ws.add_data_validation(dv)
    dv.add(ws[f"B{r}"])
    A["policy"] = f"B{r}"
    r += 2

    section("Scenario")
    put(ws, f"A{r}", "Active scenario")
    put(ws, f"B{r}", "Base", INPUT, None, KEY_FILL)
    put(ws, f"E{r}", "Drives ROI Analysis, Forecast and the KPI Dashboard. Scenario "
                     "Analysis shows all three side by side regardless.", NOTE)
    dv2 = DataValidation(type="list", formula1='"Base,Bull,Bear"', allow_blank=False)
    ws.add_data_validation(dv2)
    dv2.add(ws[f"B{r}"])
    A["scenario"] = f"B{r}"
    r += 2

    header(ws, r, ["Scenario driver", "Base", "Bull", "Bear", "What it means"])
    A["scen_hdr_row"] = str(r)
    r += 1
    drivers = [
        ("realization", "Effect realisation", 1.00, 1.10, 0.75, PCT0,
         "Multiplier on attributed savings. Bear: a quarter of measured savings do not "
         "survive contact with production."),
        ("cost_mult", "Programme cost multiplier", 1.00, 0.90, 1.30, MULT,
         "Bear: hiring takes longer and tooling costs more than planned."),
        ("growth", "Cloud growth (forecast)", 0.15, 0.10, 0.25, PCT0,
         "Annual growth applied in the run-rate forecast."),
        ("persist_s", "Savings persistence", 0.85, 0.90, 0.70, PCT0,
         "Overrides the financial assumption above when a scenario is active."),
    ]
    for key, label, b, u, d, fmt, note in drivers:
        put(ws, f"A{r}", label)
        for c, v in zip("BCD", (b, u, d), strict=True):
            put(ws, f"{c}{r}", v, INPUT, fmt)
        put(ws, f"E{r}", note, NOTE)
        A[f"row_{key}"] = str(r)
        r += 1
    r += 1

    section("Active scenario values (formulas)")
    hdr = int(A["scen_hdr_row"])
    for key, label, fmt in [("realization", "Effect realisation", PCT0),
                            ("cost_mult", "Programme cost multiplier", MULT),
                            ("growth", "Cloud growth (forecast)", PCT0),
                            ("persist_s", "Savings persistence", PCT0)]:
        rr = A[f"row_{key}"]
        put(ws, f"A{r}", label)
        put(ws, f"B{r}", f"=INDEX($B${rr}:$D${rr},MATCH({A['scenario']},$B${hdr}:$D${hdr},0))",
            LABEL, fmt)
        A[f"active_{key}"] = f"B{r}"
        r += 1
    r += 1
    put(ws, f"A{r}", SIM_NOTE, WARN)
    return A


# =======================================================================================
# 02 Raw Cost Data
# =======================================================================================


def sheet_raw(wb: Workbook, d: Data) -> dict:
    ws = wb.create_sheet(SHEETS["raw"])
    title(ws, "Raw cost data",
          "Monthly effective, billed and list cost by business unit, from the silver "
          "layer (Usage rows, complete months only). The pivot on the right is formulas.")
    widths(ws, {"A": 12, "B": 16, "C": 15, "D": 15, "E": 15, "F": 3})
    R: dict = {}

    # --- long table (data) -------------------------------------------------------------
    header(ws, 5, ["Month", "Business unit", "Effective cost", "Billed cost", "List cost"])
    r0 = 6
    for i, row in enumerate(d.monthly.itertuples(index=False)):
        r = r0 + i
        ws.cell(row=r, column=1, value=row.month.to_pydatetime()).number_format = MON
        ws.cell(row=r, column=2, value=row.business_unit)
        for cidx, v in zip((3, 4, 5), (row.effective, row.billed, row.list), strict=True):
            c = ws.cell(row=r, column=cidx, value=float(v))
            c.number_format = MONEY
        if i % 2:
            for cidx in range(1, 6):
                ws.cell(row=r, column=cidx).fill = BAND
    r_last = r0 + len(d.monthly) - 1
    R["long"] = (r0, r_last)
    long_month = f"$A${r0}:$A${r_last}"
    long_bu = f"$B${r0}:$B${r_last}"
    long_eff = f"$C${r0}:$C${r_last}"

    # --- pivot: month x BU, formulas -------------------------------------------------
    pc = 7   # column G
    ws.cell(row=4, column=pc, value="Effective cost by month and business unit (SUMIFS over the table)").font = H3
    header(ws, 5, ["Month", "#", *d.bus, "Total"], start_col=pc)
    ws.column_dimensions[col(pc)].width = 12
    ws.column_dimensions[col(pc + 1)].width = 5
    for j in range(len(d.bus) + 1):
        ws.column_dimensions[col(pc + 2 + j)].width = 14
    first_bu = pc + 2                      # first business-unit column
    pr0 = 6
    for i, m in enumerate(d.months):
        r = pr0 + i
        ws.cell(row=r, column=pc, value=m.to_pydatetime()).number_format = MON
        ws.cell(row=r, column=pc + 1, value=i + 1).font = NOTE   # month index, for FORECAST
        for j in range(len(d.bus)):
            c = ws.cell(row=r, column=first_bu + j,
                        value=f"=SUMIFS({long_eff},{long_month},${col(pc)}{r},{long_bu},{col(first_bu + j)}$5)")
            c.number_format = MONEY
        tc = ws.cell(row=r, column=first_bu + len(d.bus),
                     value=f"=SUM({col(first_bu)}{r}:{col(first_bu + len(d.bus) - 1)}{r})")
        tc.number_format = MONEY
        tc.font = LABEL_B
    pr_last = pr0 + len(d.months) - 1
    # totals row
    rt = pr_last + 1
    ws.cell(row=rt, column=pc, value="Total").font = LABEL_B
    for j in range(len(d.bus) + 1):
        c = ws.cell(row=rt, column=first_bu + j,
                    value=f"=SUM({col(first_bu + j)}{pr0}:{col(first_bu + j)}{pr_last})")
        c.number_format = MONEY
        c.font = LABEL_B
        c.border = TOPLINE
    R["pivot"] = {"first_row": pr0, "last_row": pr_last, "total_row": rt,
                  "month_col": col(pc), "idx_col": col(pc + 1),
                  "bu_cols": {bu: col(first_bu + j) for j, bu in enumerate(d.bus)},
                  "total_col": col(first_bu + len(d.bus))}
    ws.cell(row=rt + 2, column=pc,
            value="Effective cost is used throughout: it amortises commitments across the "
                  "period they cover. Billed cost would show prepaid resources as free.").font = NOTE
    return R


# =======================================================================================
# 03 Optimization Events
# =======================================================================================


def sheet_events(wb: Workbook, d: Data) -> dict:
    ws = wb.create_sheet(SHEETS["events"])
    title(ws, "Optimisation events",
          "Every intervention the study measured: what was done, to which resource, when, "
          "and what it was costing beforehand. The register the attribution sheet reads.")
    widths(ws, {"A": 10, "B": 10, "C": 13, "D": 24, "E": 12, "F": 10, "G": 9, "H": 16,
                "I": 17, "J": 3, "L": 26, "M": 8})
    header(ws, 5, ["Event", "Resource", "Business unit", "Action", "Action date",
                   "Trigger z", "Donors", "Pre-period daily run-rate", "Annualised run-rate"])
    r0 = 6
    for i, e in enumerate(d.events.itertuples(index=False)):
        r = r0 + i
        ws.cell(row=r, column=1, value=e.intervention_id)
        ws.cell(row=r, column=2, value=e.resource_id)
        ws.cell(row=r, column=3, value=e.business_unit)
        ws.cell(row=r, column=4, value=e.action)
        ws.cell(row=r, column=5, value=pd.Timestamp(e.start_date).to_pydatetime()).number_format = DATE
        ws.cell(row=r, column=6, value=float(e.trigger_z)).number_format = NUM3
        ws.cell(row=r, column=7, value=int(e.n_donors))
        ws.cell(row=r, column=8, value=float(e.run_rate)).number_format = MONEY
        c = ws.cell(row=r, column=9, value=f"=H{r}*365")
        c.number_format = MONEY
        if i % 2:
            for cidx in range(1, 10):
                ws.cell(row=r, column=cidx).fill = BAND
    r_last = r0 + len(d.events) - 1

    # counts by action (formulas)
    ws.cell(row=4, column=12, value="Events by action").font = H3
    header(ws, 5, ["Action", "Count", "Annualised run-rate"], start_col=12)
    ws.column_dimensions["N"].width = 18
    actions = sorted(d.events["action"].unique())
    for i, a in enumerate(actions):
        r = 6 + i
        ws.cell(row=r, column=12, value=a)
        ws.cell(row=r, column=13, value=f'=COUNTIF($D${r0}:$D${r_last},L{r})')
        c = ws.cell(row=r, column=14, value=f'=SUMIF($D${r0}:$D${r_last},L{r},$I${r0}:$I${r_last})')
        c.number_format = MONEY
    rt = 6 + len(actions)
    ws.cell(row=rt, column=12, value="Total").font = LABEL_B
    ws.cell(row=rt, column=13, value=f"=SUM(M6:M{rt - 1})").font = LABEL_B
    c = ws.cell(row=rt, column=14, value=f"=SUM(N6:N{rt - 1})")
    c.number_format = MONEY
    c.font = LABEL_B
    ws.cell(row=rt + 2, column=12,
            value="Run-rate = mean daily effective cost over the 84 days before the action, "
                  "the same window the estimator uses.").font = NOTE
    return {"first_row": r0, "last_row": r_last, "n": len(d.events)}


# =======================================================================================
# 04 Savings Attribution
# =======================================================================================


def sheet_attribution(wb: Workbook, d: Data, A: dict, EV: dict) -> dict:
    ws = wb.create_sheet(SHEETS["attr"])
    title(ws, "Savings attribution",
          "What each event is credited with, on what evidence. Before/after is what the "
          "industry reports; the identified estimate is difference-in-differences with "
          "randomisation inference; the parallel-trends check decides whether it is credible.")
    cols = ["Event", "Business unit", "Action", "Annualised run-rate",
            "Before/after effect (log)", "Before/after annual $",
            "Identified effect (log)", "CI low", "CI high", "p-value",
            "Parallel trends passed", "Credited?", "Identified annual $", "Credited annual $",
            "SIMULATED true effect (log)", "SIMULATED true annual $", "|Before/after − true|",
            "Null event?"]
    header(ws, 5, cols)
    for i in range(1, len(cols) + 1):
        ws.column_dimensions[col(i)].width = 13
    ws.column_dimensions["C"].width = 22
    for c_ in ("O", "P", "Q"):
        ws[f"{c_}5"].fill = PatternFill("solid", fgColor="C0504D")
    ws["A4"] = ("Peach columns use ground truth, which exists only because the estate is "
                "simulated. A real deployment would not have them -- that is the point of "
                "needing an estimator at all.")
    ws["A4"].font = WARN

    ev_sheet = SHEETS["events"]
    r0 = 6
    for i, e in enumerate(d.events.itertuples(index=False)):
        r = r0 + i
        er = EV["first_row"] + i     # same order as the events sheet
        ws.cell(row=r, column=1, value=f"={q(ev_sheet)}!A{er}").font = LINK
        ws.cell(row=r, column=2, value=f"={q(ev_sheet)}!C{er}").font = LINK
        ws.cell(row=r, column=3, value=f"={q(ev_sheet)}!D{er}").font = LINK
        c = ws.cell(row=r, column=4, value=f"={q(ev_sheet)}!I{er}")
        c.font = LINK
        c.number_format = MONEY
        ws.cell(row=r, column=5, value=float(e.naive_coef)).number_format = NUM3
        ws.cell(row=r, column=6, value=f"=-(EXP(E{r})-1)*D{r}").number_format = MONEY
        ws.cell(row=r, column=7, value=float(e.did_coef)).number_format = NUM3
        ws.cell(row=r, column=8, value=f"=EXP({float(e.did_ci_low)})-1").number_format = PCT
        ws.cell(row=r, column=9, value=f"=EXP({float(e.did_ci_high)})-1").number_format = PCT
        ws.cell(row=r, column=10, value=float(e.did_p)).number_format = NUM3
        ws.cell(row=r, column=11, value=bool(e.pt_passed))
        ws.cell(row=r, column=12, value=f'=IF({ref(SHEETS["assume"], A["policy"])}="Yes",TRUE,K{r})')
        ws.cell(row=r, column=13, value=f"=-(EXP(G{r})-1)*D{r}").number_format = MONEY
        ws.cell(row=r, column=14, value=f"=IF(L{r},M{r},0)").number_format = MONEY
        ws.cell(row=r, column=15, value=float(e.true_effect_log)).number_format = NUM3
        ws.cell(row=r, column=16, value=f"=-(EXP(O{r})-1)*D{r}").number_format = MONEY
        ws.cell(row=r, column=17, value=f"=ABS(F{r}-P{r})").number_format = MONEY
        ws.cell(row=r, column=18, value=bool(e.is_null))
        for cidx in (15, 16, 17):
            ws.cell(row=r, column=cidx).fill = SIM_FILL
        if i % 2:
            for cidx in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 18):
                ws.cell(row=r, column=cidx).fill = BAND
    r_last = r0 + len(d.events) - 1

    # totals
    rt = r_last + 1
    ws.cell(row=rt, column=1, value="Total").font = LABEL_B
    for c_ in ("D", "F", "M", "N", "P", "Q"):
        cell = ws[f"{c_}{rt}"]
        cell.value = f"=SUM({c_}{r0}:{c_}{r_last})"
        cell.number_format = MONEY
        cell.font = LABEL_B
        cell.border = TOPLINE
    ws[f"K{rt}"] = f"=COUNTIF(K{r0}:K{r_last},TRUE)"
    ws[f"L{rt}"] = f"=COUNTIF(L{r0}:L{r_last},TRUE)"
    ws[f"R{rt}"] = f"=COUNTIF(R{r0}:R{r_last},TRUE)"
    for c_ in ("K", "L", "R"):
        ws[f"{c_}{rt}"].font = LABEL_B
        ws[f"{c_}{rt}"].border = TOPLINE

    # summary block
    rs = rt + 3
    ws.cell(row=rs, column=1, value="Summary").font = H3
    S: dict[str, str] = {}
    rng = lambda c_: f"${c_}${r0}:${c_}${r_last}"  # noqa: E731
    lines = [
        ("n_events", "Events measured", f"=COUNTA({rng('A')})", INT),
        ("n_pt", "Passing parallel trends", f"=K{rt}", INT),
        ("n_credited", "Credited under current policy", f"=L{rt}", INT),
        ("n_null", "SIMULATED: events with zero true effect", f"=R{rt}", INT),
        ("naive_total", "Before/after would report (annual)", f"=F{rt}", MONEY),
        ("did_total", "Identified estimate, all events (annual)", f"=M{rt}", MONEY),
        ("credited_total", "Credited savings (annual)", f"=N{rt}", MONEY),
        ("true_total", "SIMULATED: true savings (annual)", f"=P{rt}", MONEY),
        ("gross_misattr", "SIMULATED: gross misattribution of before/after", f"=Q{rt}", MONEY),
        ("naive_on_null", "SIMULATED: before/after credited to zero-effect events",
         f"=SUMIF({rng('R')},TRUE,{rng('F')})", MONEY),
        ("did_on_null", "SIMULATED: identified estimate on zero-effect events",
         f"=SUMIF({rng('R')},TRUE,{rng('M')})", MONEY),
        ("credited_on_null", "SIMULATED: credited to zero-effect events (after gate)",
         f"=SUMIF({rng('R')},TRUE,{rng('N')})", MONEY),
        ("naive_fp_rate", "SIMULATED: before/after error as share of true savings",
         f"=IF(P{rt}=0,0,Q{rt}/P{rt})", PCT),
    ]
    for i, (key, label, formula, fmt) in enumerate(lines):
        r = rs + 1 + i
        put(ws, f"A{r}", label, LABEL_B if key in ("credited_total",) else LABEL)
        put(ws, f"D{r}", formula, LABEL, fmt, KEY_FILL if key == "credited_total" else None,
            bold=(key == "credited_total"))
        if "SIMULATED" in label:
            ws[f"D{r}"].fill = SIM_FILL
        S[key] = f"D{r}"
    ws.cell(row=rs + len(lines) + 2, column=1,
            value="Annual $ = −(e^effect − 1) × annualised run-rate. A log effect of −0.10 "
                  "is a 9.5% cost reduction; the conversion is exact, not approximate.").font = NOTE
    return {"first_row": r0, "last_row": r_last, "total_row": rt, "summary": S}


# =======================================================================================
# 05 Budget vs Actual
# =======================================================================================


def sheet_bva(wb: Workbook, d: Data, A: dict, RAW: dict) -> dict:
    ws = wb.create_sheet(SHEETS["bva"])
    title(ws, "Budget vs actual",
          "Budget = average of the first six complete months, grown monthly at the budget "
          "growth assumption. Actuals link to Raw Cost Data. Cost variance: under budget "
          "is favourable.")
    P = RAW["pivot"]
    raw = SHEETS["raw"]
    asm = SHEETS["assume"]
    base_n = 6
    if d.n_complete_months <= base_n + 1:
        sys.exit("not enough complete months for a budget-vs-actual view")
    months = d.months[base_n:]
    n_bu = len(d.bus)

    # monthly growth factor cell
    put(ws, "A4", "Monthly growth factor")
    put(ws, "B4", f"=(1+{ref(asm, A['growth_budget'])})^(1/12)", LABEL, "0.0000")
    ws["C4"] = "(1 + annual budget growth)^(1/12)"
    ws["C4"].font = NOTE
    gf = "$B$4"

    # baseline row: average of first 6 months per BU (formulas into the pivot)
    hdr = ["Month"]
    for bu in d.bus:
        hdr += [f"{bu} budget", f"{bu} actual", f"{bu} var"]
    hdr += ["Total budget", "Total actual", "Total variance", "Variance %", "Status"]
    header(ws, 6, hdr)
    ws.column_dimensions["A"].width = 12
    for i in range(2, len(hdr) + 1):
        ws.column_dimensions[col(i)].width = 13

    put(ws, "A5", "Baseline (avg. months 1–6)", LABEL_B)
    base_cells: dict[str, str] = {}
    for j, bu in enumerate(d.bus):
        cidx = 2 + 3 * j
        bc = P["bu_cols"][bu]
        c = ws.cell(row=5, column=cidx,
                    value=f"=AVERAGE({q(raw)}!{bc}{P['first_row']}:{bc}{P['first_row'] + base_n - 1})")
        c.number_format = MONEY
        c.font = LINK
        base_cells[bu] = f"${col(cidx)}$5"
    tb_col = 2 + 3 * n_bu           # total budget column
    ta_col, tv_col, tp_col, st_col = tb_col + 1, tb_col + 2, tb_col + 3, tb_col + 4

    r0 = 7
    for i, m in enumerate(months):
        r = r0 + i
        k = i + 1     # months after baseline
        praw = P["first_row"] + base_n + i   # pivot row of this month
        ws.cell(row=r, column=1, value=m.to_pydatetime()).number_format = MON
        for j, bu in enumerate(d.bus):
            cb, ca, cv = 2 + 3 * j, 3 + 3 * j, 4 + 3 * j
            bc = P["bu_cols"][bu]
            ws.cell(row=r, column=cb, value=f"={base_cells[bu]}*{gf}^{k}").number_format = MONEY
            c = ws.cell(row=r, column=ca, value=f"={q(raw)}!{bc}{praw}")
            c.number_format = MONEY
            c.font = LINK
            ws.cell(row=r, column=cv, value=f"={col(ca)}{r}-{col(cb)}{r}").number_format = MONEY
        ws.cell(row=r, column=tb_col,
                value="=" + "+".join(f"{col(2 + 3 * j)}{r}" for j in range(n_bu))).number_format = MONEY
        ws.cell(row=r, column=ta_col,
                value="=" + "+".join(f"{col(3 + 3 * j)}{r}" for j in range(n_bu))).number_format = MONEY
        ws.cell(row=r, column=tv_col,
                value=f"={col(ta_col)}{r}-{col(tb_col)}{r}").number_format = MONEY
        ws.cell(row=r, column=tp_col,
                value=f"=IF({col(tb_col)}{r}=0,0,{col(tv_col)}{r}/{col(tb_col)}{r})").number_format = PCT
        ws.cell(row=r, column=st_col,
                value=f'=IF({col(tp_col)}{r}>0.05,"Unfavourable",IF({col(tp_col)}{r}<-0.05,"Favourable","On plan"))')
        for cidx in (tb_col, ta_col, tv_col, tp_col):
            ws.cell(row=r, column=cidx).font = LABEL_B
    r_last = r0 + len(months) - 1
    rt = r_last + 1
    ws.cell(row=rt, column=1, value="Total").font = LABEL_B
    for cidx in list(range(2, 2 + 3 * n_bu)) + [tb_col, ta_col, tv_col]:
        c = ws.cell(row=rt, column=cidx, value=f"=SUM({col(cidx)}{r0}:{col(cidx)}{r_last})")
        c.number_format = MONEY
        c.font = LABEL_B
        c.border = TOPLINE
    c = ws.cell(row=rt, column=tp_col,
                value=f"=IF({col(tb_col)}{rt}=0,0,{col(tv_col)}{rt}/{col(tb_col)}{rt})")
    c.number_format = PCT
    c.font = LABEL_B
    c.border = TOPLINE

    # conditional formatting on variance %
    rng = f"{col(tp_col)}{r0}:{col(tp_col)}{rt}"
    ws.conditional_formatting.add(rng, CellIsRule(operator="greaterThan", formula=["0.05"],
                                                  fill=PatternFill("solid", fgColor="F8CBAD")))
    ws.conditional_formatting.add(rng, CellIsRule(operator="lessThan", formula=["-0.05"],
                                                  fill=PatternFill("solid", fgColor="C6EFCE")))
    ws.freeze_panes = "B7"
    ws.cell(row=rt + 2, column=1,
            value="Every business unit's bill rose over the period. Budget vs actual cannot "
                  "tell you whether that is growth or waste -- the unit-economics layer in "
                  "the repository does. This sheet shows the variance; the engine explains it.").font = NOTE
    return {"first_row": r0, "last_row": r_last, "total_row": rt,
            "tb_col": col(tb_col), "ta_col": col(ta_col), "tv_col": col(tv_col),
            "tp_col": col(tp_col), "months": months,
            "bu_actual_cols": {bu: col(3 + 3 * j) for j, bu in enumerate(d.bus)},
            "bu_budget_cols": {bu: col(2 + 3 * j) for j, bu in enumerate(d.bus)}}


# =======================================================================================
# 06 Forecast
# =======================================================================================


def sheet_forecast(wb: Workbook, d: Data, A: dict, RAW: dict) -> dict:
    ws = wb.create_sheet(SHEETS["fcst"])
    title(ws, "Forecast",
          "Twelve months forward. Trend = linear regression of each unit's monthly actuals "
          "on time (FORECAST). Run-rate = last three months' average grown at the active "
          "scenario's cloud growth. Two methods shown so the gap between them is visible.")
    P = RAW["pivot"]
    raw, asm = SHEETS["raw"], SHEETS["assume"]
    n = d.n_complete_months
    # known_x for FORECAST: the pivot's month-index column, same orientation as known_y
    xr = f"{q(raw)}!${P['idx_col']}${P['first_row']}:${P['idx_col']}${P['last_row']}"
    hist_idx = xr

    put(ws, "A6", "Monthly growth factor (active scenario)")
    put(ws, "B6", f"=(1+{ref(asm, A['active_growth'])})^(1/12)", LABEL, "0.0000")
    gf = "$B$6"

    hdr = ["Month", *[f"{bu} (trend)" for bu in d.bus], "Total (trend)",
           "Total (run-rate × growth)", "Gap"]
    header(ws, 8, hdr)
    ws.column_dimensions["A"].width = 12
    for i in range(2, len(hdr) + 1):
        ws.column_dimensions[col(i)].width = 14
    last_m = d.months[-1]
    r0 = 9
    # last-3-month average of total, for run-rate method
    tot_col = P["total_col"]
    l3 = (f"=AVERAGE({q(raw)}!{tot_col}{P['last_row'] - 2}:{tot_col}{P['last_row']})")
    put(ws, "A7", "Last 3 months avg. total")
    put(ws, "B7", l3, LINK, MONEY)
    for k in range(12):
        r = r0 + k
        m = (last_m + pd.DateOffset(months=k + 1)).to_pydatetime()
        ws.cell(row=r, column=1, value=m).number_format = MON
        for j, bu in enumerate(d.bus):
            bc = P["bu_cols"][bu]
            yr = f"{q(raw)}!${bc}${P['first_row']}:${bc}${P['last_row']}"
            c = ws.cell(row=r, column=2 + j, value=f"=MAX(0,FORECAST({n + k + 1},{yr},{xr}))")
            c.number_format = MONEY
        tcol = 2 + len(d.bus)
        ws.cell(row=r, column=tcol, value=f"=SUM(B{r}:{col(1 + len(d.bus))}{r})").number_format = MONEY
        ws.cell(row=r, column=tcol).font = LABEL_B
        ws.cell(row=r, column=tcol + 1, value=f"=$B$7*{gf}^{k + 1}").number_format = MONEY
        ws.cell(row=r, column=tcol + 2, value=f"={col(tcol + 1)}{r}-{col(tcol)}{r}").number_format = MONEY
    r_last = r0 + 11
    rt = r_last + 1
    ws.cell(row=rt, column=1, value="12-month total").font = LABEL_B
    tcol = 2 + len(d.bus)
    for cidx in list(range(2, tcol + 3)):
        c = ws.cell(row=rt, column=cidx, value=f"=SUM({col(cidx)}{r0}:{col(cidx)}{r_last})")
        c.number_format = MONEY
        c.font = LABEL_B
        c.border = TOPLINE
    ws.cell(row=rt + 2, column=1,
            value="A linear trend extrapolates the whole history; the run-rate method trusts "
                  "only the last quarter. When they disagree by more than ~10%, the recent "
                  "quarter contains something the trend has not caught up with.").font = NOTE
    return {"first_row": r0, "last_row": r_last, "total_row": rt,
            "trend_total_col": col(tcol), "runrate_total_col": col(tcol + 1), "helper": hist_idx}


# =======================================================================================
# 07 ROI Analysis
# =======================================================================================


def sheet_roi(wb: Workbook, A: dict, S: dict) -> dict:
    ws = wb.create_sheet(SHEETS["roi"])
    title(ws, "ROI analysis",
          "Programme economics under the active scenario. Savings are the credited annual "
          "figure from Savings Attribution, scaled by effect realisation and decayed by "
          "persistence. Costs are the programme inputs scaled by the cost multiplier.")
    widths(ws, {"A": 44, "B": 15, "C": 15, "D": 15, "E": 15, "F": 15, "G": 15, "H": 40})
    asm, attr = SHEETS["assume"], SHEETS["attr"]
    R: dict[str, str] = {}
    r = 5
    ws.cell(row=r, column=1, value="Inputs (links)").font = H3
    r += 1
    inputs = [
        ("credited", "Credited annual savings (measurement)", ref(attr, S["credited_total"]), MONEY),
        ("realization", "Effect realisation (scenario)", ref(asm, A["active_realization"]), PCT0),
        ("persist", "Savings persistence (scenario)", ref(asm, A["active_persist_s"]), PCT0),
        ("cost_mult", "Programme cost multiplier (scenario)", ref(asm, A["active_cost_mult"]), MULT),
        ("rate", "Discount rate", ref(asm, A["rate"]), PCT),
        ("run_cost", "Annual programme run cost",
         f"=({ref(asm, A['fte'])}*{ref(asm, A['loaded'])}+{ref(asm, A['tooling'])})*{ref(asm, A['active_cost_mult'])}", MONEY),
        ("setup", "One-time setup (year 0)",
         f"={ref(asm, A['setup'])}*{ref(asm, A['active_cost_mult'])}", MONEY),
        ("eng", "Intervention engineering, one-time (year 0)",
         f"={ref(attr, S['n_events'])}*{ref(asm, A['eng_hours'])}*{ref(asm, A['eng_rate'])}*{ref(asm, A['active_cost_mult'])}", MONEY),
        ("ramp", "Year-1 ramp", ref(asm, A["ramp"]), PCT0),
    ]
    for key, label, formula, fmt in inputs:
        put(ws, f"A{r}", label)
        f = formula if formula.startswith("=") else f"={formula}"
        put(ws, f"B{r}", f, LINK, fmt)
        R[key] = f"$B${r}"
        r += 1
    r += 1

    ws.cell(row=r, column=1, value="Cash flows").font = H3
    r += 1
    header(ws, r, ["", "Year 0", "Year 1", "Year 2", "Year 3", "Year 4", "Year 5", "Note"])
    r += 1
    rows = {}
    labels = [("sav", "Realised savings"), ("cost", "Programme cost"), ("net", "Net cash flow"),
              ("cum", "Cumulative net cash flow"), ("df", "Discount factor"), ("pv", "Present value of net")]
    for key, label in labels:
        put(ws, f"A{r}", label, LABEL_B if key in ("net", "pv") else LABEL)
        rows[key] = r
        r += 1
    # year 0
    ws[f"B{rows['sav']}"] = "=0"          # no savings before the work is done
    ws[f"B{rows['cost']}"] = f"=-({R['setup']}+{R['eng']})"
    for t in range(1, 6):
        c = col(2 + t)
        shape = R["ramp"] if t == 1 else f"{R['persist']}^{t - 1}"
        ws[f"{c}{rows['sav']}"] = f"={R['credited']}*{R['realization']}*{shape}"
        ws[f"{c}{rows['cost']}"] = f"=-{R['run_cost']}"
    for t in range(0, 6):
        c = col(2 + t)
        ws[f"{c}{rows['net']}"] = f"={c}{rows['sav']}+{c}{rows['cost']}"
        prev = col(1 + t)
        ws[f"{c}{rows['cum']}"] = (f"={c}{rows['net']}" if t == 0
                                   else f"={prev}{rows['cum']}+{c}{rows['net']}")
        ws[f"{c}{rows['df']}"] = f"=1/(1+{R['rate']})^{t}"
        ws[f"{c}{rows['pv']}"] = f"={c}{rows['net']}*{c}{rows['df']}"
        for key in ("sav", "cost", "net", "cum", "pv"):
            ws[f"{c}{rows[key]}"].number_format = MONEY
        ws[f"{c}{rows['df']}"].number_format = "0.0000"
        ws[f"{c}{rows['net']}"].font = LABEL_B
        ws[f"{c}{rows['pv']}"].font = LABEL_B
    ws[f"H{rows['sav']}"] = "credited × realisation × ramp (yr 1) or persistence^(t−1) (yr 2+)"
    ws[f"H{rows['cost']}"] = "setup + intervention engineering in year 0; run cost yrs 1–5; × cost multiplier"
    for k in ("sav", "cost"):
        ws[f"H{rows[k]}"].font = NOTE
    r += 1

    ws.cell(row=r, column=1, value="Results").font = H3
    r += 1
    net = f"B{rows['net']}:G{rows['net']}"
    pv_ben = f"SUMPRODUCT(C{rows['sav']}:G{rows['sav']},C{rows['df']}:G{rows['df']})"
    pv_cost = f"(-B{rows['cost']}-SUMPRODUCT(C{rows['cost']}:G{rows['cost']},C{rows['df']}:G{rows['df']}))"
    results = [
        ("npv", "Net present value (5 years)", f"=SUM(B{rows['pv']}:G{rows['pv']})", MONEY, True),
        ("pv_ben", "Present value of savings", f"={pv_ben}", MONEY, False),
        ("pv_cost", "Present value of programme cost", f"={pv_cost}", MONEY, False),
        ("bcr", "Benefit-to-cost ratio", f"=IF({pv_cost}=0,0,{pv_ben}/{pv_cost})", MULT, False),
        ("roi", "Return on investment (PV basis)", f"=IF({pv_cost}=0,0,({pv_ben}-{pv_cost})/{pv_cost})", PCT0, True),
        ("irr", "Internal rate of return", f'=IFERROR(IRR({net}),"n/a")', PCT, False),
        ("payback", "Payback (months)", None, NUM1, True),
        ("yr1_roi", "Year-1 ROI (net year 1 ÷ year-0 outlay + run)",
         f"=IF(({R['setup']}+{R['eng']}+{R['run_cost']})=0,0,D{rows['net']}/({R['setup']}+{R['eng']}+{R['run_cost']}))", PCT0, False),
    ]
    for key, label, formula, fmt, key_cell in results:
        put(ws, f"A{r}", label, LABEL_B if key_cell else LABEL)
        if key == "payback":
            # months until cumulative net turns positive, linear within the year it does
            cum = {t: f"{col(2 + t)}{rows['cum']}" for t in range(6)}
            netc = {t: f"{col(2 + t)}{rows['net']}" for t in range(6)}
            expr = '"beyond 5 yrs"'
            for t in range(5, 0, -1):
                expr = (f"IF({cum[t]}>=0,12*({t}-1)+12*(-{cum[t - 1]})/{netc[t]},{expr})")
            formula = f"=IF({cum[0]}>=0,0,{expr})"
        put(ws, f"B{r}", formula, LABEL, fmt, KEY_FILL if key_cell else None, bold=key_cell)
        R[key] = f"$B${r}"
        r += 1
    r += 1

    ws.cell(row=r, column=1, value="What the before/after number would have said").font = H3
    r += 1
    put(ws, f"A{r}", "Before/after reported annual savings")
    put(ws, f"B{r}", f"={ref(attr, S['naive_total'])}", LINK, MONEY)
    R["naive"] = f"$B${r}"
    r += 1
    put(ws, f"A{r}", "Credited savings (identified, gated)")
    put(ws, f"B{r}", f"={R['credited']}", LINK, MONEY)
    r += 1
    put(ws, f"A{r}", "Difference the method makes to the headline")
    put(ws, f"B{r}", f"={R['credited']}-{R['naive']}", LABEL, MONEY, bold=True)
    R["method_delta"] = f"$B${r}"
    r += 1
    put(ws, f"A{r}", "SIMULATED: gross misattribution inside the before/after total")
    put(ws, f"B{r}", f"={ref(attr, S['gross_misattr'])}", LINK, MONEY, SIM_FILL)
    R["gross_misattr"] = f"$B${r}"
    r += 2
    ws.cell(row=r, column=1,
            value="The aggregate can look close while every individual attribution is wrong: "
                  "errors in both directions net out. Gross misattribution is the sum of the "
                  "absolute errors, and it is what a budget owner actually experiences.").font = NOTE
    R["rows"] = rows
    return R


# =======================================================================================
# 08 Scenario Analysis
# =======================================================================================


def sheet_scenarios(wb: Workbook, A: dict, S: dict) -> dict:
    ws = wb.create_sheet(SHEETS["scen"])
    title(ws, "Scenario analysis",
          "Base, Bull and Bear computed side by side from the driver table on Assumptions. "
          "Independent of the active-scenario selector, so all three are always visible.")
    widths(ws, {"A": 40, "B": 16, "C": 16, "D": 16, "E": 44})
    asm, attr = SHEETS["assume"], SHEETS["attr"]
    header(ws, 5, ["", "Base", "Bull", "Bear", "How it is computed"])
    r = 6
    put(ws, f"A{r}", "Drivers", H3)
    r += 1
    drv = {}
    for key, label, fmt in [("realization", "Effect realisation", PCT0),
                            ("cost_mult", "Programme cost multiplier", MULT),
                            ("growth", "Cloud growth (forecast)", PCT0),
                            ("persist_s", "Savings persistence", PCT0)]:
        put(ws, f"A{r}", label)
        for c_ in "BCD":
            put(ws, f"{c_}{r}", f"={q(asm)}!{c_}{A['row_' + key]}", LINK, fmt)
        drv[key] = r
        r += 1
    r += 1
    put(ws, f"A{r}", "Constants", H3)
    r += 1
    put(ws, f"A{r}", "Credited annual savings")
    for c_ in "BCD":
        put(ws, f"{c_}{r}", f"={ref(attr, S['credited_total'])}", LINK, MONEY)
    cred = r
    r += 1
    put(ws, f"A{r}", "Discount rate")
    for c_ in "BCD":
        put(ws, f"{c_}{r}", f"={ref(asm, A['rate'])}", LINK, PCT)
    rate = r
    r += 1
    put(ws, f"A{r}", "Annual run cost (before multiplier)")
    for c_ in "BCD":
        put(ws, f"{c_}{r}", f"={ref(asm, A['fte'])}*{ref(asm, A['loaded'])}+{ref(asm, A['tooling'])}", LINK, MONEY)
    runc = r
    r += 1
    put(ws, f"A{r}", "Setup (before multiplier)")
    for c_ in "BCD":
        put(ws, f"{c_}{r}", f"={ref(asm, A['setup'])}", LINK, MONEY)
    setup = r
    r += 1
    put(ws, f"A{r}", "Intervention engineering (before multiplier)")
    for c_ in "BCD":
        put(ws, f"{c_}{r}", f"={ref(attr, S['n_events'])}*{ref(asm, A['eng_hours'])}*{ref(asm, A['eng_rate'])}", LINK, MONEY)
    eng = r
    r += 1
    put(ws, f"A{r}", "Year-1 ramp")
    for c_ in "BCD":
        put(ws, f"{c_}{r}", f"={ref(asm, A['ramp'])}", LINK, PCT0)
    ramp = r
    r += 2

    put(ws, f"A{r}", "Results", H3)
    r += 1
    out = {}
    # annuity factors via array constants; SUMPRODUCT evaluates arrays in Excel and LibreOffice
    T = "{1,2,3,4,5}"
    T2 = "{2,3,4,5}"
    for key, label, fmt, note in [
        ("y1", "Year-1 realised savings", MONEY, "credited × realisation × ramp"),
        ("pvb", "PV of savings, 5 years", MONEY, "credited·real·[ramp/(1+r) + Σ₂₋₅ persist^(t−1)/(1+r)^t]"),
        ("pvc", "PV of programme cost", MONEY, "(setup + engineering)·mult + Σ run·mult / (1+r)^t"),
        ("npv", "NPV", MONEY, "PV savings − PV cost"),
        ("roi", "ROI (PV basis)", PCT0, "(PV savings − PV cost) / PV cost"),
        ("bcr", "Benefit-to-cost ratio", MULT, "PV savings / PV cost"),
        ("pay", "Simple payback (months)", NUM1, "12 × year-0 outlay / (year-1 savings − run cost), if within year 1"),
    ]:
        put(ws, f"A{r}", label, LABEL_B if key in ("npv", "roi") else LABEL)
        for c_ in "BCD":
            real, cm, pers = f"{c_}{drv['realization']}", f"{c_}{drv['cost_mult']}", f"{c_}{drv['persist_s']}"
            cr, rt_, rc, su = f"{c_}{cred}", f"{c_}{rate}", f"{c_}{runc}", f"{c_}{setup}"
            en, rp = f"{c_}{eng}", f"{c_}{ramp}"
            pvb = f"({cr}*{real}*({rp}/(1+{rt_})+SUMPRODUCT({pers}^({T2}-1)/(1+{rt_})^{T2})))"
            pvc = f"(({su}+{en})*{cm}+{rc}*{cm}*SUMPRODUCT(1/(1+{rt_})^{T}))"
            f = {
                "y1": f"={cr}*{real}*{rp}",
                "pvb": f"={pvb}",
                "pvc": f"={pvc}",
                "npv": f"={pvb}-{pvc}",
                "roi": f"=IF({pvc}=0,0,({pvb}-{pvc})/{pvc})",
                "bcr": f"=IF({pvc}=0,0,{pvb}/{pvc})",
                "pay": f'=IF(({cr}*{real}*{rp}-{rc}*{cm})<=0,"beyond yr 1",12*({su}+{en})*{cm}/({cr}*{real}*{rp}-{rc}*{cm}))',
            }[key]
            put(ws, f"{c_}{r}", f, LABEL, fmt, KEY_FILL if key == "npv" else None, bold=(key in ("npv", "roi")))
        put(ws, f"E{r}", note, NOTE)
        out[key] = r
        r += 1
    r += 1
    ws.cell(row=r, column=1,
            value="Bear is not a stress test of the measurement -- the estimator's error is on "
                  "the Savings Attribution sheet. Bear is a stress test of execution: what if "
                  "a quarter of measured savings never reach production, hiring runs 30% over, "
                  "and savings decay faster.").font = NOTE
    return {"rows": out, "drv": drv}


# =======================================================================================
# 09 Sensitivity Analysis
# =======================================================================================


def sheet_sensitivity(wb: Workbook, A: dict, S: dict) -> dict:
    ws = wb.create_sheet(SHEETS["sens"])
    title(ws, "Sensitivity analysis",
          "Five-year NPV as two inputs move together, everything else at the active "
          "scenario. Each cell is the full NPV formula, so the grid recalculates.")
    widths(ws, {"A": 26})
    asm, attr = SHEETS["assume"], SHEETS["attr"]
    T = "{1,2,3,4,5}"
    T2 = "{2,3,4,5}"
    cred = ref(attr, S["credited_total"])
    rate = ref(asm, A["rate"])
    runc = f"({ref(asm, A['fte'])}*{ref(asm, A['loaded'])}+{ref(asm, A['tooling'])})"
    outlay = f"({ref(asm, A['setup'])}+{ref(attr, S['n_events'])}*{ref(asm, A['eng_hours'])}*{ref(asm, A['eng_rate'])})"
    ramp = ref(asm, A["ramp"])
    pers_active = ref(asm, A["active_persist_s"])
    real_active = ref(asm, A["active_realization"])
    cm_active = ref(asm, A["active_cost_mult"])

    def npv(real, cm, pers, rt_):
        pvb = f"({cred}*{real}*({ramp}/(1+{rt_})+SUMPRODUCT({pers}^({T2}-1)/(1+{rt_})^{T2})))"
        pvc = f"({outlay}*{cm}+{runc}*{cm}*SUMPRODUCT(1/(1+{rt_})^{T}))"
        return f"={pvb}-{pvc}"

    # --- grid 1: realisation (rows) x cost multiplier (cols) ---------------------------
    r = 5
    ws.cell(row=r, column=1, value="NPV: effect realisation (rows) × programme cost multiplier (columns)").font = H3
    r += 1
    reals = [0.50, 0.625, 0.75, 0.875, 1.00, 1.125, 1.25]
    mults = [0.8, 1.0, 1.2, 1.5, 2.0]
    put(ws, f"A{r}", "realisation ↓ / cost × →", NOTE)
    for j, m in enumerate(mults):
        put(ws, f"{col(2 + j)}{r}", m, INPUT, MULT)
        ws.column_dimensions[col(2 + j)].width = 14
    hdr1 = r
    r += 1
    g1_first = r
    for re_ in reals:
        put(ws, f"A{r}", re_, INPUT, PCT0)
        for j in range(len(mults)):
            c = ws[f"{col(2 + j)}{r}"]
            c.value = npv(f"$A{r}", f"{col(2 + j)}${hdr1}", pers_active, rate)
            c.number_format = MONEY
        r += 1
    g1_last = r - 1
    ws.conditional_formatting.add(f"B{g1_first}:{col(1 + len(mults))}{g1_last}",
                                  CellIsRule(operator="lessThan", formula=["0"],
                                             fill=PatternFill("solid", fgColor="F8CBAD")))
    r += 1

    # --- grid 2: discount rate (rows) x persistence (cols) --------------------------------
    ws.cell(row=r, column=1, value="NPV: discount rate (rows) × savings persistence (columns)").font = H3
    r += 1
    rates = [0.08, 0.10, 0.12, 0.15, 0.20]
    perss = [0.60, 0.70, 0.80, 0.85, 0.90, 1.00]
    put(ws, f"A{r}", "rate ↓ / persistence →", NOTE)
    for j, p in enumerate(perss):
        put(ws, f"{col(2 + j)}{r}", p, INPUT, PCT0)
        ws.column_dimensions[col(2 + j)].width = 14
    hdr2 = r
    r += 1
    g2_first = r
    for rt_ in rates:
        put(ws, f"A{r}", rt_, INPUT, PCT)
        for j in range(len(perss)):
            c = ws[f"{col(2 + j)}{r}"]
            c.value = npv(real_active, cm_active, f"{col(2 + j)}${hdr2}", f"$A{r}")
            c.number_format = MONEY
        r += 1
    g2_last = r - 1
    ws.conditional_formatting.add(f"B{g2_first}:{col(1 + len(perss))}{g2_last}",
                                  CellIsRule(operator="lessThan", formula=["0"],
                                             fill=PatternFill("solid", fgColor="F8CBAD")))
    r += 1
    # consistency anchor: the (100%, 1.0x) cell must equal the ROI sheet's NPV when Base is active
    put(ws, f"A{r}", "Check: grid 1 at (100%, 1.00x) − ROI NPV, Base scenario", NOTE)
    anchor = f"{col(2 + mults.index(1.0))}{g1_first + reals.index(1.00)}"
    return {"grid1": (g1_first, g1_last), "grid2": (g2_first, g2_last), "anchor": anchor,
            "check_row": r}


# =======================================================================================
# 11 KPI Dashboard
# =======================================================================================


def sheet_kpi(wb: Workbook, d: Data, A: dict, S: dict, R: dict, BVA: dict, RAW: dict) -> dict:
    ws = wb.create_sheet(SHEETS["kpi"])
    title(ws, "KPI dashboard",
          "Everything here is a formula into the other sheets. Change an assumption and "
          "the dashboard changes.")
    attr, roi, bva, asm, raw = SHEETS["attr"], SHEETS["roi"], SHEETS["bva"], SHEETS["assume"], SHEETS["raw"]
    P = RAW["pivot"]
    for c_ in "ABCDEFGH":
        ws.column_dimensions[c_].width = 20

    tiles = [
        ("Annualised cloud run-rate", f"=AVERAGE({q(raw)}!{P['total_col']}{P['last_row'] - 11}:{P['total_col']}{P['last_row']})*12", MONEY,
         "last 12 complete months, annualised"),
        ("Events measured", f"={ref(attr, S['n_events'])}", INT, "interventions with a usable control group"),
        ("Passing parallel trends", f"={ref(attr, S['n_pt'])}", INT, "estimates credible enough to credit"),
        ("Credited annual savings", f"={ref(attr, S['credited_total'])}", MONEY, "identified, gated by parallel trends"),
        ("Before/after would report", f"={ref(attr, S['naive_total'])}", MONEY, "the industry's number"),
        ("Programme NPV (5 yr)", f"={ref(roi, R['npv'])}", MONEY, "active scenario"),
        ("ROI (PV basis)", f"={ref(roi, R['roi'])}", PCT0, "active scenario"),
        ("Payback", f"={ref(roi, R['payback'])}", NUM1, "months"),
        ("Budget variance, period", f"={ref(bva, BVA['tp_col'] + str(BVA['total_row']))}", PCT, "actual vs run-rate budget"),
        ("SIMULATED: gross misattribution", f"={ref(attr, S['gross_misattr'])}", MONEY, "|before/after − true|, summed"),
        ("Active scenario", f"={ref(asm, A['scenario'])}", None, ""),
        ("Attribution policy", f'=IF({ref(asm, A["policy"])}="Yes","all estimates","parallel-trends gate")', None, ""),
    ]
    r = 5
    for i, (label, formula, fmt, note) in enumerate(tiles):
        cidx = 1 + 2 * (i % 4)
        rr = r + 4 * (i // 4)
        cl = col(cidx)
        put(ws, f"{cl}{rr}", label, NOTE, fill=TILE_FILL)
        ws[f"{col(cidx + 1)}{rr}"].fill = TILE_FILL
        put(ws, f"{cl}{rr + 1}", formula, BIG, fmt, TILE_FILL)
        ws[f"{col(cidx + 1)}{rr + 1}"].fill = TILE_FILL
        put(ws, f"{cl}{rr + 2}", note, NOTE, fill=TILE_FILL)
        ws[f"{col(cidx + 1)}{rr + 2}"].fill = TILE_FILL
        if "SIMULATED" in label:
            for k in range(3):
                ws[f"{cl}{rr + k}"].fill = SIM_FILL
                ws[f"{col(cidx + 1)}{rr + k}"].fill = SIM_FILL
    r = 5 + 4 * 3 + 1

    # --- chart data blocks (formulas) then charts ----------------------------------------
    ws.cell(row=r, column=1, value="Chart data (formulas)").font = H3
    r += 1
    # 1. savings comparison
    put(ws, f"A{r}", "Measure", LABEL_B)
    put(ws, f"B{r}", "Annual $", LABEL_B)
    cmp_hdr = r
    for i, (label, f) in enumerate([
        ("Before/after", f"={ref(attr, S['naive_total'])}"),
        ("Identified, all", f"={ref(attr, S['did_total'])}"),
        ("Credited (gated)", f"={ref(attr, S['credited_total'])}"),
        ("SIMULATED true", f"={ref(attr, S['true_total'])}"),
    ]):
        put(ws, f"A{r + 1 + i}", label)
        put(ws, f"B{r + 1 + i}", f, LINK, MONEY)
    ch1 = BarChart()
    ch1.type = "col"
    ch1.title = "Annual savings: three ways of counting"
    ch1.y_axis.title = "$ per year"
    ch1.add_data(Reference(ws, min_col=2, min_row=cmp_hdr, max_row=cmp_hdr + 4), titles_from_data=True)
    ch1.set_categories(Reference(ws, min_col=1, min_row=cmp_hdr + 1, max_row=cmp_hdr + 4))
    ch1.height, ch1.width = 7.5, 14
    ch1.legend = None
    ch1.y_axis.scaling.min = 0          # a bar chart that does not start at zero lies
    ws.add_chart(ch1, f"D{cmp_hdr}")
    r += 7

    # 2. budget vs actual, total by month
    put(ws, f"A{r}", "Month", LABEL_B)
    put(ws, f"B{r}", "Budget", LABEL_B)
    put(ws, f"C{r}", "Actual", LABEL_B)
    bva_hdr = r
    for i in range(BVA["last_row"] - BVA["first_row"] + 1):
        br = BVA["first_row"] + i
        rr = r + 1 + i
        c = ws[f"A{rr}"]
        c.value = f"={q(bva)}!A{br}"
        c.number_format = MON
        c.font = LINK
        put(ws, f"B{rr}", f"={q(bva)}!{BVA['tb_col']}{br}", LINK, MONEY)
        put(ws, f"C{rr}", f"={q(bva)}!{BVA['ta_col']}{br}", LINK, MONEY)
    n_m = BVA["last_row"] - BVA["first_row"] + 1
    ch2 = LineChart()
    ch2.title = "Total spend: budget vs actual"
    ch2.y_axis.title = "$ per month"
    ch2.add_data(Reference(ws, min_col=2, max_col=3, min_row=bva_hdr, max_row=bva_hdr + n_m), titles_from_data=True)
    ch2.set_categories(Reference(ws, min_col=1, min_row=bva_hdr + 1, max_row=bva_hdr + n_m))
    ch2.height, ch2.width = 7.5, 14
    ch2.y_axis.scaling.min = 0
    ws.add_chart(ch2, f"D{bva_hdr}")
    r += n_m + 3

    # 3. variance by BU over the period
    put(ws, f"A{r}", "Business unit", LABEL_B)
    put(ws, f"B{r}", "Budget", LABEL_B)
    put(ws, f"C{r}", "Actual", LABEL_B)
    bu_hdr = r
    for i, bu in enumerate(d.bus):
        rr = r + 1 + i
        put(ws, f"A{rr}", bu)
        put(ws, f"B{rr}", f"={q(bva)}!{BVA['bu_budget_cols'][bu]}{BVA['total_row']}", LINK, MONEY)
        put(ws, f"C{rr}", f"={q(bva)}!{BVA['bu_actual_cols'][bu]}{BVA['total_row']}", LINK, MONEY)
    ch3 = BarChart()
    ch3.type = "col"
    ch3.title = "Budget vs actual by business unit (period total)"
    ch3.add_data(Reference(ws, min_col=2, max_col=3, min_row=bu_hdr, max_row=bu_hdr + len(d.bus)), titles_from_data=True)
    ch3.set_categories(Reference(ws, min_col=1, min_row=bu_hdr + 1, max_row=bu_hdr + len(d.bus)))
    ch3.height, ch3.width = 7.5, 14
    ch3.y_axis.scaling.min = 0
    ws.add_chart(ch3, f"D{bu_hdr}")
    return {}


# =======================================================================================
# 01 Executive Summary
# =======================================================================================


def sheet_exec(wb: Workbook, d: Data, A: dict, S: dict, R: dict, BVA: dict):
    ws = wb.create_sheet(SHEETS["exec"])
    title(ws, "CostProof — finance model",
          "The output of a causal cost-measurement engine, delivered as an FP&A workbook.")
    widths(ws, {"A": 50, "B": 18, "C": 60})
    attr, roi, bva, asm = SHEETS["attr"], SHEETS["roi"], SHEETS["bva"], SHEETS["assume"]
    r = 5
    ws.cell(row=r, column=1, value="The problem").font = H3
    r += 1
    for line in [
        "Every cloud cost tool reports what was spent. None can prove what an optimisation saved,",
        "because teams act on spikes and spikes revert on their own. Before/after comparison",
        "credits the team for gravity. This workbook shows what changes when the saving is",
        "measured against a control group instead.",
    ]:
        ws.cell(row=r, column=1, value=line).font = LABEL
        r += 1
    r += 1
    ws.cell(row=r, column=1, value="Two workbooks, two questions").font = H3
    r += 1
    for line in [
        "costproof-business-case.xlsx asks: should an organisation buy the measurement layer?",
        "Its NPV counts only decision quality -- effort not wasted on fixes that did nothing.",
        "This workbook asks: what did the optimisation programme itself deliver, measured",
        "properly, and what is that worth? Its savings are the cloud savings; its costs are the",
        "engineering to achieve them plus the team that measures them. Different question,",
        "different NPV. Neither is the other one restated.",
    ]:
        ws.cell(row=r, column=1, value=line).font = LABEL
        r += 1
    r += 1
    ws.cell(row=r, column=1, value="Headline figures (formulas; active scenario)").font = H3
    r += 1
    header(ws, r, ["Measure", "Value", "Where it comes from"])
    r += 1
    lines = [
        ("Events measured", f"={ref(attr, S['n_events'])}", INT, "Savings Attribution"),
        ("Passing the parallel-trends check", f"={ref(attr, S['n_pt'])}", INT, "Savings Attribution"),
        ("Before/after would report, per year", f"={ref(attr, S['naive_total'])}", MONEY, "Savings Attribution — the industry's number"),
        ("Credited savings, per year", f"={ref(attr, S['credited_total'])}", MONEY, "Savings Attribution — identified, gated"),
        ("Difference the method makes", f"={ref(roi, R['method_delta'])}", MONEY, "ROI Analysis"),
        ("Programme NPV, five years", f"={ref(roi, R['npv'])}", MONEY, "ROI Analysis"),
        ("Benefit-to-cost ratio", f"={ref(roi, R['bcr'])}", MULT, "ROI Analysis"),
        ("Payback, months", f"={ref(roi, R['payback'])}", NUM1, "ROI Analysis"),
        ("Budget variance over the period", f"={ref(bva, BVA['tp_col'] + str(BVA['total_row']))}", PCT, "Budget vs Actual"),
        ("SIMULATED: gross misattribution in before/after", f"={ref(attr, S['gross_misattr'])}", MONEY, "Savings Attribution — needs ground truth"),
        ("Active scenario / attribution policy", f'={ref(asm, A["scenario"])}&" / "&IF({ref(asm, A["policy"])}="Yes","all","PT gate")', None, "Assumptions"),
    ]
    for label, f, fmt, src in lines:
        put(ws, f"A{r}", label, LABEL_B if "Credited" in label or "NPV" in label else LABEL)
        put(ws, f"B{r}", f, LINK_B if "Credited" in label or "NPV" in label else LINK, fmt,
            SIM_FILL if "SIMULATED" in label else None)
        put(ws, f"C{r}", src, NOTE)
        r += 1
    r += 1
    ws.cell(row=r, column=1, value="How to read this workbook").font = H3
    r += 1
    for line in [
        "Blue cells are inputs; black cells are formulas; green cells link to another sheet.",
        "Only estimates that pass the parallel-trends check are credited, unless you flip the",
        "policy switch on Assumptions. Bear/Bull/Base are execution scenarios, not measurement",
        "scenarios: the estimator's own error is quantified on Savings Attribution.",
        "Peach cells use simulated ground truth. They are the reason the method can be",
        "validated here and the reason it would be needed on real data, where they do not exist.",
    ]:
        ws.cell(row=r, column=1, value=line).font = LABEL
        r += 1
    r += 1
    ws.cell(row=r, column=1, value="Sheets").font = H3
    r += 1
    for name, what in [
        (SHEETS["raw"], "monthly cost by business unit; the pivot is formulas"),
        (SHEETS["events"], "the intervention register"),
        (SHEETS["attr"], "per-event estimates, the gate, the credited total"),
        (SHEETS["bva"], "run-rate budget vs actual, by unit and month"),
        (SHEETS["fcst"], "twelve months forward, two methods"),
        (SHEETS["roi"], "programme cash flows, NPV, ROI, IRR, payback"),
        (SHEETS["scen"], "Base / Bull / Bear side by side"),
        (SHEETS["sens"], "NPV grids over four drivers"),
        (SHEETS["assume"], "every input, in one place"),
        (SHEETS["kpi"], "tiles and charts"),
    ]:
        ws.cell(row=r, column=1, value=name).font = LABEL_B
        ws.cell(row=r, column=3, value=what).font = NOTE
        r += 1
    r += 1
    ws.cell(row=r, column=1,
            value="Generated by scripts/build_finance_model.py from the pipeline's own outputs. "
                  "Regenerate after `costproof study`; nothing here is typed in.").font = NOTE


# =======================================================================================
# Build
# =======================================================================================


def build(out: Path = OUT) -> Path:
    d = load()
    wb = Workbook()
    wb.remove(wb.active)

    A = sheet_assumptions(wb)
    RAW = sheet_raw(wb, d)
    EV = sheet_events(wb, d)
    AT = sheet_attribution(wb, d, A, EV)
    S = AT["summary"]
    BVA = sheet_bva(wb, d, A, RAW)
    sheet_forecast(wb, d, A, RAW)
    R = sheet_roi(wb, A, S)
    sheet_scenarios(wb, A, S)
    SENS = sheet_sensitivity(wb, A, S)
    sheet_kpi(wb, d, A, S, R, BVA, RAW)
    sheet_exec(wb, d, A, S, R, BVA)

    # consistency cell on the sensitivity sheet (needs the ROI address)
    ws = wb[SHEETS["sens"]]
    ws[f"B{SENS['check_row']}"] = f"={SENS['anchor']}-{ref(SHEETS['roi'], R['npv'])}"
    ws[f"B{SENS['check_row']}"].number_format = MONEY
    ws[f"B{SENS['check_row']}"].font = NOTE

    # order the sheets 01..11
    order = [SHEETS[k] for k in ("exec", "raw", "events", "attr", "bva", "fcst", "roi",
                                 "scen", "sens", "assume", "kpi")]
    wb._sheets = [wb[n] for n in order]  # noqa: SLF001 -- openpyxl has no public reorder
    wb.active = 0

    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)

    n_formulas = sum(1 for w in wb.worksheets for row in w.iter_rows()
                     for c in row if isinstance(c.value, str) and c.value.startswith("="))
    shown = out.relative_to(ROOT) if out.is_relative_to(ROOT) else out
    print(f"wrote {shown}: {len(wb.worksheets)} sheets, {n_formulas:,} formulas")
    print("expected after recalculation:")
    for k, v in d.expected.items():
        print(f"  {k:18s} {v:>14,.0f}" if isinstance(v, float) else f"  {k:18s} {v:>14}")
    return out


if __name__ == "__main__":
    build()
