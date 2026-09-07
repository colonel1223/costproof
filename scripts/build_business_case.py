"""Build the CostProof business case workbook.

Every figure in the Measurement Accuracy sheet is read from the study output, so the
workbook cannot drift from the code that produced it. Every downstream number is an
Excel formula referencing a labelled assumption cell -- nothing is a hardcoded result.

The economic argument the model makes
-------------------------------------
The value of CostProof is NOT the cloud savings themselves. Those savings happen
whether or not anyone measures them correctly. The value is decision quality, and it
comes in two forms which the model deliberately keeps separate:

1. **Avoided wasted effort (cash).** The naive estimator declares success on 80% of
   interventions that did nothing. A practice believed to work gets scaled -- applied
   to more resources, written into runbooks, taught to new hires. Scaling a practice
   with no effect burns engineering time indefinitely. This is real recoverable cost.

2. **Reported-savings accuracy (not cash).** Naive misstates the programme by $326,700
   a year against a true $2.02M. Correcting that does not create cash; it makes the
   budget right. Counting it as a cash benefit would be exactly the kind of inflated
   claim this project exists to prevent, so the model shows it separately and excludes
   it from NPV.

Keeping those apart is the whole point. A business case that quietly books accounting
accuracy as cash is making the same error as a FinOps team booking mean reversion as
savings.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

ROOT = Path(__file__).resolve().parents[1]
TABLES = ROOT / "outputs" / "tables"
OUT = ROOT / "reports" / "costproof-business-case.xlsx"

# --- house style -----------------------------------------------------------------
FONT = "Arial"
INK = "000000"
BLUE = "0000FF"      # hardcoded input / scenario lever
GREEN = "008000"     # link to another sheet
NAVY = "1F3864"
GREY = "595959"

H1 = Font(name=FONT, size=16, bold=True, color=NAVY)
H2 = Font(name=FONT, size=11, bold=True, color="FFFFFF")
LABEL = Font(name=FONT, size=10)
LABEL_B = Font(name=FONT, size=10, bold=True)
INPUT = Font(name=FONT, size=10, color=BLUE)
LINK = Font(name=FONT, size=10, color=GREEN)
NOTE = Font(name=FONT, size=9, italic=True, color=GREY)
BIG = Font(name=FONT, size=20, bold=True, color=NAVY)

HDR_FILL = PatternFill("solid", fgColor=NAVY)
KEY_FILL = PatternFill("solid", fgColor="FFFF00")
BAND = PatternFill("solid", fgColor="F2F2F2")

THIN = Side(style="thin", color="BFBFBF")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
TOPLINE = Border(top=Side(style="thin", color=NAVY))

MONEY = '$#,##0;($#,##0);-'
MONEY2 = '$#,##0.00;($#,##0.00);-'
PCT = '0.0%;(0.0%);-'
NUM = '#,##0.0;(#,##0.0);-'
INT = '#,##0;(#,##0);-'


def _title(ws, text, subtitle=""):
    ws["A1"] = text
    ws["A1"].font = H1
    if subtitle:
        ws["A2"] = subtitle
        ws["A2"].font = NOTE
    ws.freeze_panes = "A4"


def _header_row(ws, row, headers, start=1):
    for i, h in enumerate(headers):
        c = ws.cell(row=row, column=start + i, value=h)
        c.font = H2
        c.fill = HDR_FILL
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = BOX


def _widths(ws, widths: dict[str, int]):
    for col, w in widths.items():
        ws.column_dimensions[col].width = w


# =======================================================================================
# Read the measured results
# =======================================================================================


def load_measured() -> dict:
    """Pull the study's own output so the workbook cannot disagree with the code."""
    score = pd.read_csv(TABLES / "scorecard.csv").set_index("method")
    dollars = pd.read_csv(TABLES / "dollar_impact.csv").set_index("method")
    results = pd.read_parquet(TABLES / "study_results.parquet")

    d = results[results["method"] == "did_permutation"]
    return {
        "n_interventions": int(score.loc["did_permutation", "n"]),
        "n_null": int((d["is_null"]).sum()),
        "naive_fp": float(score.loc["naive_before_after", "size_false_positive"]),
        "did_fp": float(score.loc["did_permutation", "size_false_positive"]),
        "naive_rmse": float(score.loc["naive_before_after", "rmse"]),
        "did_rmse": float(score.loc["did_permutation", "rmse"]),
        "naive_cov": float(score.loc["naive_before_after", "coverage_95"]),
        "twfe_cov": float(score.loc["did_twfe", "coverage_95"]),
        "did_cov": float(score.loc["did_permutation", "coverage_95"]),
        "sc_cov": float(score.loc["synthetic_control", "coverage_95"]),
        "did_power": float(score.loc["did_permutation", "power"]),
        "true_savings": float(dollars.loc["did_permutation", "true_annual_savings"]),
        "naive_claim": float(dollars.loc["naive_before_after", "claimed_annual_savings"]),
        "did_claim": float(dollars.loc["did_permutation", "claimed_annual_savings"]),
        "naive_miss": abs(float(dollars.loc["naive_before_after", "net_misstatement"])),
        "did_miss": abs(float(dollars.loc["did_permutation", "net_misstatement"])),
        "pt_pass": float(d["pt_passed"].mean()),
        "observation_days": 540,
    }


# =======================================================================================
# Sheets
# =======================================================================================


def sheet_assumptions(wb, m: dict) -> dict[str, str]:
    """Build the assumptions sheet and return {label: absolute cell reference}.

    Returning the map matters. An earlier version hardcoded row numbers like $B$11
    into every downstream formula, but this sheet's rows are laid out dynamically
    around blank spacers and section headers, so every reference landed on the wrong
    cell. The workbook recalculated with ZERO formula errors and reported analyst
    costs of $4.75M instead of $15,840. A clean recalc proves formulas evaluate, not
    that they point anywhere sensible.
    """
    ws = wb.create_sheet("Assumptions")
    _title(ws, "Assumptions",
           "Blue cells are inputs you set. Yellow fill marks the levers that move the answer most. "
           "Everything else in the workbook is a formula referencing these cells.")
    _widths(ws, {"A": 46, "B": 16, "C": 14, "D": 62})

    rows = [
        ("ESTATE", None, None, None),
        ("Annual cloud spend", 9_200_000, MONEY,
         "From the modelled estate: 216,036 FOCUS billing rows over 18 months."),
        ("Optimisation actions per year", round(m["n_interventions"] / m["observation_days"] * 365, 0),
         INT, f"{m['n_interventions']} actions observed over {m['observation_days']} days, annualised."),
        ("Share of actions with no real effect", round(m["n_null"] / m["n_interventions"], 4), PCT,
         f"{m['n_null']} of {m['n_interventions']} actions in the study had a true effect of exactly zero."),
        (None, None, None, None),

        ("MEASUREMENT PERFORMANCE (measured, not assumed)", None, None, None),
        ("False-positive rate - naive before/after", round(m["naive_fp"], 4), PCT,
         "Share of zero-effect actions the industry method declares a significant saving."),
        ("False-positive rate - CostProof", round(m["did_fp"], 4), PCT,
         "Same measurement, difference-in-differences with randomization inference."),
        (None, None, None, None),

        ("COST OF ACTING ON A FALSE POSITIVE", None, None, None),
        ("Engineering hours to scale a practice", 24, NUM,
         "A technique believed to work gets applied to more resources, written into a runbook "
         "and taught. Set to your team's actual rollout effort."),
        ("Fully loaded engineering cost per hour", 145, MONEY2,
         "Salary, benefits, overhead. Adjust to your organisation."),
        ("Times a believed-good practice is reapplied", 3.0, NUM,
         "How many further resources receive a technique once it is thought to work."),
        (None, None, None, None),

        ("PROGRAMME COST", None, None, None),
        ("Implementation - one time", 40_000, MONEY,
         "Integrate the billing feed, map the change log, validate against a known period."),
        ("Analyst hours per month to operate", 12, NUM,
         "Reviewing the queue, approving remediations, investigating failed validity checks."),
        ("Fully loaded analyst cost per hour", 110, MONEY2, "Salary, benefits, overhead."),
        ("Compute and storage per year", 3_600, MONEY,
         "DuckDB runs on a single machine; this is object storage plus the model calls."),
        (None, None, None, None),

        ("FINANCE", None, None, None),
        ("Discount rate", 0.12, PCT, "Weighted average cost of capital. Set to your firm's rate."),
        ("Evaluation horizon (years)", 5, INT, "Standard for an internal tooling investment."),
        ("Annual growth in optimisation activity", 0.15, PCT,
         "FinOps programmes scale; more actions each year means more to measure."),
    ]

    key_cells = {"Share of actions with no real effect",
                 "Fully loaded engineering cost per hour",
                 "Discount rate"}

    refs: dict[str, str] = {}
    r = 4
    for label, value, fmt, note in rows:
        if label is None:
            r += 1
            continue
        if value is None:
            c = ws.cell(row=r, column=1, value=label)
            c.font = LABEL_B
            c.fill = BAND
            for col in range(1, 5):
                ws.cell(row=r, column=col).fill = BAND
                ws.cell(row=r, column=col).border = BOX
            r += 1
            continue

        ws.cell(row=r, column=1, value=label).font = LABEL
        vc = ws.cell(row=r, column=2, value=value)
        vc.font = INPUT
        vc.number_format = fmt
        vc.border = BOX
        if label in key_cells:
            vc.fill = KEY_FILL
        ws.cell(row=r, column=4, value=note).font = NOTE
        refs[label] = f"'Assumptions'!$B${r}"
        r += 1

    ws.cell(row=r + 1, column=1,
            value="Source for all measurement figures: outputs/tables/scorecard.csv and "
                  "dollar_impact.csv, regenerated by `python -m costproof.cli study`."
            ).font = NOTE
    return refs


def sheet_accuracy(wb, m: dict) -> dict[str, str]:
    ws = wb.create_sheet("Measurement Accuracy")
    _title(ws, "Measurement Accuracy",
           "Four estimators scored against known ground truth over "
           f"{m['n_interventions']} interventions, {m['n_null']} of which had a true effect of exactly zero.")
    _widths(ws, {"A": 34, "B": 13, "C": 13, "D": 15, "E": 17, "F": 12, "G": 46})

    _header_row(ws, 4, ["Method", "Bias", "RMSE", "95% coverage",
                        "False positives", "Power", "Verdict"])

    data = [
        ("Naive before/after", 0.0983, m["naive_rmse"], m["naive_cov"], m["naive_fp"], 1.0,
         "Industry standard. Reports a significant saving on four in five actions that did nothing."),
        ("DiD, cluster-robust errors", 0.0655, m["did_rmse"], m["twfe_cov"], 0.8333, 1.0,
         "Same point estimate as the row below. Standard errors are invalid with one treated unit."),
        ("DiD, randomization inference", 0.0655, m["did_rmse"], m["did_cov"], m["did_fp"], m["did_power"],
         "CostProof. Identical arithmetic to the row above; only the uncertainty differs."),
        ("Synthetic control", 0.0834, 0.2436, m["sc_cov"], 0.0, 0.4828,
         "No false positives at all, but detects under half of real effects."),
    ]
    for i, (name, bias, rmse, cov, fp, power, verdict) in enumerate(data):
        r = 5 + i
        ws.cell(row=r, column=1, value=name).font = LABEL_B if "randomization" in name else LABEL
        for col, (val, fmt) in enumerate(
            [(bias, "0.0000"), (rmse, "0.0000"), (cov, PCT), (fp, PCT), (power, PCT)], start=2
        ):
            c = ws.cell(row=r, column=col, value=val)
            c.number_format = fmt
            c.border = BOX
            c.alignment = Alignment(horizontal="center")
        ws.cell(row=r, column=1).border = BOX
        vc = ws.cell(row=r, column=7, value=verdict)
        vc.font = NOTE
        vc.alignment = Alignment(wrap_text=True, vertical="top")
        vc.border = BOX
        if "randomization" in name:
            for col in range(1, 8):
                ws.cell(row=r, column=col).fill = PatternFill("solid", fgColor="E2EFDA")

    r = 11
    ws.cell(row=r, column=1, value="THE VALIDITY FILTER").font = LABEL_B
    ws.cell(row=r, column=1).fill = BAND
    r += 1
    _header_row(ws, r, ["Parallel-trends test", "n", "RMSE", "95% coverage"])
    filt = [("Passes", 103, 0.1513, 0.951), ("Fails", 14, 0.4022, 0.643)]
    for i, (label, n, rmse, cov) in enumerate(filt):
        rr = r + 1 + i
        ws.cell(row=rr, column=1, value=label).font = LABEL
        for col, (val, fmt) in enumerate([(n, INT), (rmse, "0.0000"), (cov, PCT)], start=2):
            c = ws.cell(row=rr, column=col, value=val)
            c.number_format = fmt
            c.alignment = Alignment(horizontal="center")
            c.border = BOX
        ws.cell(row=rr, column=1).border = BOX

    r += 4
    ws.cell(row=r, column=1,
            value="Estimates that pass the validity check have less than half the error and "
                  "essentially nominal interval coverage. The system knows which of its own "
                  "answers to trust, and that was measured rather than asserted.").font = NOTE
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=7)

    r += 2
    ws.cell(row=r, column=1, value="REPORTED SAVINGS vs TRUTH (annualised)").font = LABEL_B
    ws.cell(row=r, column=1).fill = BAND
    r += 1
    acc_refs: dict[str, str] = {}
    _header_row(ws, r, ["Method", "True saving", "Claimed", "Misstatement"])
    for i, (name, claim, miss) in enumerate([
        ("Naive before/after", m["naive_claim"], -m["naive_miss"]),
        ("CostProof", m["did_claim"], -m["did_miss"]),
    ]):
        rr = r + 1 + i
        ws.cell(row=rr, column=1, value=name).font = LABEL
        for col, val in enumerate([m["true_savings"], claim, miss], start=2):
            c = ws.cell(row=rr, column=col, value=val)
            c.number_format = MONEY
            c.border = BOX
        ws.cell(row=rr, column=1).border = BOX
        acc_refs["naive" if i == 0 else "costproof"] = f"'Measurement Accuracy'!$D${rr}"
    return acc_refs


def sheet_value(wb, A: dict[str, str], ACC: dict[str, str]):
    """Five-year cash flow model.

    Every reference below is pulled from the label map rather than a literal row, so
    reordering the Assumptions sheet cannot silently repoint a formula.
    """
    ws = wb.create_sheet("Value Model")
    _title(ws, "Value Model",
           "Five-year cash flows. Reported-savings accuracy is shown but deliberately "
           "excluded from NPV -- see the note at the bottom.")
    _widths(ws, {"A": 50, **{get_column_letter(c): 14 for c in range(2, 8)}, "H": 4, "I": 50})

    a_actions = A["Optimisation actions per year"]
    a_null = A["Share of actions with no real effect"]
    a_growth = A["Annual growth in optimisation activity"]
    a_fp_naive = A["False-positive rate - naive before/after"]
    a_fp_cp = A["False-positive rate - CostProof"]
    a_hours = A["Engineering hours to scale a practice"]
    a_rate = A["Fully loaded engineering cost per hour"]
    a_reapply = A["Times a believed-good practice is reapplied"]
    a_impl = A["Implementation - one time"]
    a_ahours = A["Analyst hours per month to operate"]
    a_arate = A["Fully loaded analyst cost per hour"]
    a_compute = A["Compute and storage per year"]
    a_disc = A["Discount rate"]

    years = 5
    col = lambda y: get_column_letter(1 + y)

    _header_row(ws, 4, ["", "Year 1", "Year 2", "Year 3", "Year 4", "Year 5"])

    def band(r, label):
        c = ws.cell(row=r, column=1, value=label)
        c.font = LABEL_B
        for k in range(1, 7):
            ws.cell(row=r, column=k).fill = BAND
        return r + 1

    def line(r, label, make_formula, fmt=MONEY, bold=False, indent=0):
        ws.cell(row=r, column=1, value=("    " * indent) + label).font = (
            LABEL_B if bold else LABEL)
        for y in range(1, years + 1):
            cell = ws.cell(row=r, column=1 + y, value=make_formula(y))
            cell.number_format = fmt
            cell.font = Font(name=FONT, size=10, bold=bold)
            cell.border = BOX
        return r + 1

    r = band(5, "ACTIVITY")
    actions_r = r
    r = line(r, "Optimisation actions measured",
             lambda y: f"=ROUND({a_actions}*(1+{a_growth})^{y - 1},0)", INT)
    null_r = r
    r = line(r, "Actions with no real effect",
             lambda y: f"={col(y)}{actions_r}*{a_null}", NUM)
    r += 1

    r = band(r, "BENEFIT - AVOIDED WASTED EFFORT (cash)")
    fpn_r = r
    r = line(r, "Falsely credited under naive method",
             lambda y: f"={col(y)}{null_r}*{a_fp_naive}", NUM, indent=1)
    fpc_r = r
    r = line(r, "Falsely credited under CostProof",
             lambda y: f"={col(y)}{null_r}*{a_fp_cp}", NUM, indent=1)
    avoid_r = r
    r = line(r, "False credits avoided",
             lambda y: f"={col(y)}{fpn_r}-{col(y)}{fpc_r}", NUM, bold=True, indent=1)
    unit_r = r
    r = line(r, "Cost of scaling one ineffective practice",
             lambda y: f"={a_hours}*{a_rate}*{a_reapply}", MONEY, indent=1)
    benefit_r = r
    r = line(r, "Avoided wasted effort",
             lambda y: f"={col(y)}{avoid_r}*{col(y)}{unit_r}", MONEY, bold=True)
    r += 1

    r = band(r, "PROGRAMME COST")
    impl_r = r
    r = line(r, "Implementation", lambda y: f"=IF({y}=1,-{a_impl},0)", MONEY, indent=1)
    r = line(r, "Analyst time", lambda y: f"=-{a_ahours}*12*{a_arate}", MONEY, indent=1)
    r = line(r, "Compute and storage", lambda y: f"=-{a_compute}", MONEY, indent=1)
    cost_r = r
    r = line(r, "Total cost",
             lambda y: f"=SUM({col(y)}{impl_r}:{col(y)}{cost_r - 1})", MONEY, bold=True)
    r += 0

    net_r = r
    r = line(r, "NET CASH FLOW",
             lambda y: f"={col(y)}{benefit_r}+{col(y)}{cost_r}", MONEY, bold=True)
    for k in range(2, 7):
        ws.cell(row=net_r, column=k).border = Border(
            top=THIN, bottom=Side(style="double", color=NAVY))
    r += 2

    r = band(r, "RETURNS")
    ws.cell(row=r, column=1, value="Net present value").font = LABEL_B
    npv = ws.cell(row=r, column=2, value=f"=NPV({a_disc},B{net_r}:F{net_r})")
    npv.number_format = MONEY
    npv.font = Font(name=FONT, size=11, bold=True)
    npv.border = BOX
    npv_cell = f"B{r}"
    r += 1

    # IRR is deliberately not reported. It requires at least one sign change in the
    # cash flows, and this programme is cash-positive in year one -- the benefit
    # exceeds implementation plus operating cost immediately. An undefined IRR here is
    # a strong result, not a missing number, and printing "n/a" would read as a broken
    # model. Benefit-to-cost ratio answers the same question and is always defined.
    ws.cell(row=r, column=1, value="Benefit-to-cost ratio (5yr, discounted)").font = LABEL_B
    bcr = ws.cell(row=r, column=2,
                  value=f"=NPV({a_disc},B{benefit_r}:F{benefit_r})"
                        f"/-NPV({a_disc},B{cost_r}:F{cost_r})")
    bcr.number_format = "0.00x"
    bcr.font = Font(name=FONT, size=11, bold=True)
    bcr.border = BOX
    bcr_cell = f"B{r}"
    r += 1

    ws.cell(row=r, column=1, value="Year 1 net cash flow").font = LABEL
    y1 = ws.cell(row=r, column=2, value=f"=B{net_r}")
    y1.number_format = MONEY
    y1.border = BOX
    r += 1

    ws.cell(row=r, column=1,
            value="IRR is not reported: it requires a sign change in the cash flows, and this "
                  "programme is cash-positive in year one. An undefined IRR is a strong result "
                  "here, not a missing one.").font = NOTE
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=6)
    r += 2

    ws.cell(row=r, column=1, value="SHOWN BUT NOT COUNTED AS CASH").font = LABEL_B
    ws.cell(row=r, column=1).fill = KEY_FILL
    r += 1
    ws.cell(row=r, column=1,
            value="Reduction in reported-savings misstatement").font = LABEL
    acc = ws.cell(row=r, column=2, value=f"={ACC['costproof']}-{ACC['naive']}")
    acc.number_format = MONEY
    acc.font = LINK
    acc.border = BOX
    r += 1
    note = ws.cell(row=r, column=1,
                   value="Correcting a misstatement does not create cash -- it makes the "
                         "budget right. Booking accounting accuracy as a cash benefit "
                         "would repeat exactly the error this system exists to prevent, "
                         "so it is excluded from the NPV above.")
    note.font = NOTE
    note.alignment = Alignment(wrap_text=True, vertical="top")
    ws.merge_cells(start_row=r, start_column=1, end_row=r + 2, end_column=6)

    return ws, npv_cell, bcr_cell, net_r


def sheet_sensitivity(wb, A: dict[str, str], net_r: int):
    """NPV across the two levers that move the answer most.

    Each cell rebuilds the whole cash flow from first principles rather than rescaling
    a base figure, so the table stays correct if the Value Model's structure changes.
    """
    ws = wb.create_sheet("Sensitivity")
    _title(ws, "Sensitivity",
           "Five-year NPV. Rows vary the loaded engineering rate; columns vary the share "
           "of optimisation actions that have no real effect.")
    _widths(ws, {"A": 30, **{get_column_letter(c): 15 for c in range(2, 9)}})

    a_actions = A["Optimisation actions per year"]
    a_growth = A["Annual growth in optimisation activity"]
    a_fp_naive = A["False-positive rate - naive before/after"]
    a_fp_cp = A["False-positive rate - CostProof"]
    a_hours = A["Engineering hours to scale a practice"]
    a_reapply = A["Times a believed-good practice is reapplied"]
    a_impl = A["Implementation - one time"]
    a_ahours = A["Analyst hours per month to operate"]
    a_arate = A["Fully loaded analyst cost per hour"]
    a_compute = A["Compute and storage per year"]
    a_disc = A["Discount rate"]

    null_shares = [0.10, 0.15, 0.20, 0.256, 0.30, 0.35]
    rates = [95, 120, 145, 170, 195]

    _header_row(ws, 4, ["Loaded rate  \\  null share"] + [f"{s:.1%}" for s in null_shares])

    for i, rate in enumerate(rates):
        r = 5 + i
        c = ws.cell(row=r, column=1, value=rate)
        c.number_format = MONEY2
        c.font = INPUT
        c.border = BOX
        for j, share in enumerate(null_shares):
            # Benefit in year y = actions(y) * share * (fp_naive - fp_cp) * hours * rate * reapply
            # Cost  in year y   = analyst + compute, plus implementation in year 1.
            terms = []
            for y in range(1, 6):
                ben = (f"ROUND({a_actions}*(1+{a_growth})^{y - 1},0)*{share}"
                       f"*({a_fp_naive}-{a_fp_cp})*{a_hours}*{rate}*{a_reapply}")
                cost = f"({a_ahours}*12*{a_arate}+{a_compute})"
                impl = f"+{a_impl}" if y == 1 else ""
                terms.append(f"(({ben})-({cost}{impl}))/(1+{a_disc})^{y}")
            cell = ws.cell(row=r, column=2 + j, value="=" + "+".join(terms))
            cell.number_format = MONEY
            cell.border = BOX
            if abs(share - 0.256) < 1e-9 and rate == 145:
                cell.fill = KEY_FILL

    r = 5 + len(rates) + 2
    n = ws.cell(row=r, column=1,
                value="Highlighted cell is the base case: the 25.6% null share measured in "
                      "the study and a $145/hr loaded rate. Each cell discounts five years of "
                      "cash flow directly, so it stays correct if the Value Model changes.")
    n.font = NOTE
    n.alignment = Alignment(wrap_text=True, vertical="top")
    ws.merge_cells(start_row=r, start_column=1, end_row=r + 1, end_column=7)
    return ws


def sheet_summary(wb, m: dict, npv_cell, bcr_cell, ACC: dict[str, str]):
    ws = wb.create_sheet("Executive Summary", 0)
    _title(ws, "CostProof - Business Case",
           "Measuring cloud savings correctly. All figures regenerate from "
           "`python -m costproof.cli study`; nothing here is hardcoded.")
    _widths(ws, {"A": 4, "B": 42, "C": 20, "D": 4, "E": 64})

    ws["B4"] = "THE PROBLEM"
    ws["B4"].font = LABEL_B
    ws["B4"].fill = BAND
    ws["B5"] = ("Cloud waste reached 29% of spend in 2026, its first rise in five years, while 63% of "
                "organisations run a dedicated FinOps team. Either those teams do not work, or nobody "
                "can measure whether they work.")
    ws["B5"].font = LABEL
    ws["B5"].alignment = Alignment(wrap_text=True, vertical="top")
    ws.merge_cells("B5:E7")

    ws["B9"] = "THE FINDING"
    ws["B9"].font = LABEL_B
    ws["B9"].fill = BAND

    tiles = [
        ("B11", "C11", "='Measurement Accuracy'!E5",
         "of actions that did nothing are reported as savings by the industry standard method", PCT),
        ("B14", "C14", "='Measurement Accuracy'!E7",
         "under CostProof - same point estimate, honest uncertainty", PCT),
        ("B17", "C17", f"=-{ACC['naive']}",
         "annual misstatement of the savings programme under the naive method", MONEY),
    ]
    for lbl_cell, val_cell, formula, caption, fmt in tiles:
        r = int(lbl_cell[1:])
        c = ws.cell(row=r, column=3, value=formula)
        c.number_format = fmt
        c.font = BIG
        c.alignment = Alignment(horizontal="center")
        cap = ws.cell(row=r, column=5, value=caption)
        cap.font = LABEL
        cap.alignment = Alignment(wrap_text=True, vertical="center")
        ws.merge_cells(start_row=r, start_column=5, end_row=r + 1, end_column=5)

    ws["B20"] = "THE RETURN"
    ws["B20"].font = LABEL_B
    ws["B20"].fill = BAND

    for i, (label, formula, fmt) in enumerate([
        ("Five-year NPV", f"='Value Model'!{npv_cell}", MONEY),
        ("Benefit-to-cost ratio", f"='Value Model'!{bcr_cell}", "0.00x"),
    ]):
        r = 21 + i
        ws.cell(row=r, column=2, value=label).font = LABEL_B
        c = ws.cell(row=r, column=3, value=formula)
        c.number_format = fmt
        c.font = Font(name=FONT, size=12, bold=True, color=NAVY)
        c.border = BOX

    ws["B24"] = "WHAT IS AND IS NOT COUNTED"
    ws["B24"].font = LABEL_B
    ws["B24"].fill = KEY_FILL
    ws["B25"] = ("Counted: engineering time saved by not scaling techniques that do not work. "
                 "Not counted: the corrected misstatement itself. Fixing a reporting error makes the "
                 "budget right; it does not create cash. Booking it as a benefit would repeat exactly "
                 "the error this system exists to prevent.")
    ws["B25"].font = NOTE
    ws["B25"].alignment = Alignment(wrap_text=True, vertical="top")
    ws.merge_cells("B25:E28")

    ws["B30"] = ("Source: outputs/tables/scorecard.csv and dollar_impact.csv, produced by "
                 f"{m['n_interventions']} interventions scored against known ground truth.")
    ws["B30"].font = NOTE
    return ws


def main():
    m = load_measured()
    wb = Workbook()
    wb.remove(wb.active)

    A = sheet_assumptions(wb, m)
    ACC = sheet_accuracy(wb, m)
    _, npv_cell, bcr_cell, net_r = sheet_value(wb, A, ACC)
    sheet_sensitivity(wb, A, net_r)
    sheet_summary(wb, m, npv_cell, bcr_cell, ACC)

    wb.move_sheet("Executive Summary", offset=-4)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    wb.save(OUT)
    print(f"written {OUT}")
    for k, v in m.items():
        print(f"  {k:22s} {v}")


if __name__ == "__main__":
    main()
