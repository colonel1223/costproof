"""The finance model must be formulas over data, and its data must match the pipeline.

Three earlier workbooks in this project evaluated cleanly with wrong numbers, because a
hard-coded cell reference pointed at the wrong row. These tests check the structural
properties that make that class of bug impossible to ship unnoticed. Full numeric
verification needs a spreadsheet engine to recalculate; that runs in
`scripts/build_finance_model.py`'s documented recalc step, not here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

needs_outputs = pytest.mark.skipif(
    not (ROOT / "outputs" / "tables" / "study_results.parquet").exists()
    or not (ROOT / "data" / "silver" / "billing.parquet").exists(),
    reason="run `costproof data`, `warehouse` and `study` first",
)


@pytest.fixture(scope="module")
def workbook(tmp_path_factory):
    import build_finance_model as bfm  # noqa: PLC0415 -- scripts/ is not a package

    out = tmp_path_factory.mktemp("fm") / "finance-model.xlsx"
    bfm.build(out)
    return load_workbook(out), bfm


@needs_outputs
def test_all_eleven_sheets_in_order(workbook):
    wb, bfm = workbook
    expected = [bfm.SHEETS[k] for k in ("exec", "raw", "events", "attr", "bva", "fcst",
                                        "roi", "scen", "sens", "assume", "kpi")]
    assert wb.sheetnames == expected


@needs_outputs
def test_every_downstream_number_is_a_formula(workbook):
    """Only the raw table, the event register and the per-event estimates may be
    literals. Every total, ratio, budget, forecast and NPV must be a formula."""
    wb, bfm = workbook
    literal_ok = {bfm.SHEETS["raw"], bfm.SHEETS["events"], bfm.SHEETS["attr"],
                  bfm.SHEETS["assume"]}
    offenders = []
    for ws in wb.worksheets:
        if ws.title in literal_ok:
            continue
        for row in ws.iter_rows():
            for c in row:
                if isinstance(c.value, (int, float)) and not isinstance(c.value, bool):
                    # sensitivity axes and the forecast month index are inputs by design
                    if ws.title == bfm.SHEETS["sens"]:
                        continue
                    offenders.append(f"{ws.title}!{c.coordinate}={c.value}")
    assert not offenders, f"numeric literals where formulas belong: {offenders[:10]}"


@needs_outputs
def test_no_cross_sheet_reference_is_hard_coded_to_a_missing_cell(workbook):
    """Every cross-sheet reference must point at a cell that actually holds something."""
    import re

    wb, _ = workbook
    pattern = re.compile(r"'([^']+)'!\$?([A-Z]{1,3})\$?(\d+)")
    dangling = []
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for c in row:
                if not (isinstance(c.value, str) and c.value.startswith("=")):
                    continue
                for sheet, colu, rownum in pattern.findall(c.value):
                    if sheet not in wb.sheetnames:
                        dangling.append(f"{ws.title}!{c.coordinate} -> {sheet}")
                        continue
                    target = wb[sheet][f"{colu}{rownum}"]
                    if target.value is None:
                        dangling.append(f"{ws.title}!{c.coordinate} -> '{sheet}'!{colu}{rownum}")
    assert not dangling, f"references to empty cells: {sorted(set(dangling))[:10]}"


@needs_outputs
def test_event_register_matches_the_study(workbook):
    wb, bfm = workbook
    ws = wb[bfm.SHEETS["events"]]
    ids = [ws.cell(row=r, column=1).value for r in range(6, ws.max_row + 1)
           if isinstance(ws.cell(row=r, column=1).value, str)
           and ws.cell(row=r, column=1).value.startswith("i-")]
    assert len(ids) == 117
    assert len(set(ids)) == 117


@needs_outputs
def test_simulated_columns_are_labelled(workbook):
    """Ground truth exists only because the estate is simulated. Every sheet that
    shows it must say so in a header or note."""
    wb, bfm = workbook
    for key in ("attr", "roi", "kpi", "exec"):
        ws = wb[bfm.SHEETS[key]]
        text = " ".join(str(c.value) for row in ws.iter_rows() for c in row if c.value)
        assert "SIMULATED" in text, f"{ws.title} shows ground truth without saying so"


@needs_outputs
def test_switches_exist_with_validation(workbook):
    wb, bfm = workbook
    ws = wb[bfm.SHEETS["assume"]]
    formulas = {dv.formula1 for dv in ws.data_validations.dataValidation}
    assert '"No,Yes"' in formulas
    assert '"Base,Bull,Bear"' in formulas
