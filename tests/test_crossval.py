"""The hand-rolled DiD must agree with R's plm to machine precision.

This is the strongest correctness claim the project makes about its estimator, and it
is checked by code that shares nothing with the estimator: a different language, a
different OLS routine, a different author. The test skips -- it does not pass -- when R
or its packages are missing, so a green run means the comparison actually happened.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from costproof.causal import crossval as CV
from costproof.simulate.generator import SimConfig, generate


def _r_ready() -> bool:
    if shutil.which("Rscript") is None:
        return False
    probe = subprocess.run(
        ["Rscript", "--vanilla", "-e",
         "suppressPackageStartupMessages({library(plm); library(sandwich); "
         "library(data.table)})"],
        capture_output=True, text=True,
    )
    return probe.returncode == 0


needs_r = pytest.mark.skipif(
    not _r_ready(),
    reason='R with plm, sandwich and data.table is required: '
           'install.packages(c("plm","sandwich","data.table"))',
)


def test_export_reproduces_the_study_panels(tmp_path):
    """Every exported panel must carry exactly the columns R expects, and Python's
    estimate for each must be finite."""
    est = generate(SimConfig(n_resources=90, n_waste_events=32, n_interventions=26))
    py = CV.export_panels(est, out_dir=tmp_path, verbose=False)
    assert len(py) > 0
    assert (tmp_path / "panels.csv").exists()
    header = (tmp_path / "panels.csv").read_text().splitlines()[0].split(",")
    assert header == CV.PANEL_COLUMNS
    assert py["py_coef"].notna().all()
    assert py["py_se_cluster"].gt(0).all()


@needs_r
def test_python_twfe_matches_r_plm_to_machine_precision(tmp_path):
    est = generate(SimConfig(n_resources=90, n_waste_events=32, n_interventions=26))
    py = CV.export_panels(est, out_dir=tmp_path, verbose=False)
    assert CV.run_r(panel_dir=tmp_path) is not None
    both = CV.compare(panel_dir=tmp_path)

    assert len(both) == len(py), "R must return an estimate for every panel"
    assert both["agrees"].all(), CV.report(both)
    # 1e-10 is far below the 1e-8 tolerance: this is "same number", not "close".
    assert both["coef_abs_diff"].max() < 1e-10
    # Same small-sample convention on both sides -> identical clustered SEs.
    assert (both["se_ratio"] - 1).abs().max() < 1e-8
