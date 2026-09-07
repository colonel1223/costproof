"""Build the warehouse: run the SQL layers in order.

The transformations themselves live in ``sql/`` as plain SQL, because that is the
language a data engineer or an analyst will read them in, and because DuckDB can run
them against Parquet with no loading step. This module only sequences them.

    bronze  data/bronze/focus_billing.parquet   raw FOCUS 1.2 feed, never modified
    silver  data/silver/billing.parquet         conformed; untagged spend made explicit
    gold    data/gold/unit_economics.parquet    cost per unit of business output

Each layer is rebuilt from the one below it (``OVERWRITE TRUE`` in the SQL), so a
re-run cannot double-count. That property -- idempotence -- is what makes the pipeline
safe to retry.
"""

from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[3]
SQL_DIR = ROOT / "sql"

#: Execution order. Each file declares what it reads and writes in its header comment.
LAYERS: tuple[tuple[str, str, str], ...] = (
    ("silver", "silver_billing.sql", "data/silver/billing.parquet"),
    ("gold", "gold_unit_economics.sql", "data/gold/unit_economics.parquet"),
)


def build(verbose: bool = True) -> list[Path]:
    """Run every SQL layer against the repository's data directory. Returns outputs."""
    bronze = ROOT / "data" / "bronze" / "focus_billing.parquet"
    if not bronze.exists():
        raise FileNotFoundError(
            f"{bronze.relative_to(ROOT)} not found -- run `python -m costproof.cli data` first"
        )

    outputs: list[Path] = []
    # Paths inside the SQL files are relative to the repository root, so that the same
    # file works from the CLI, from a notebook, or pasted into the DuckDB shell.
    with _working_directory(ROOT), duckdb.connect() as con:
        for name, sql_file, out_rel in LAYERS:
            out = ROOT / out_rel
            out.parent.mkdir(parents=True, exist_ok=True)
            t0 = time.perf_counter()
            con.execute((SQL_DIR / sql_file).read_text())
            rows = con.execute(f"SELECT COUNT(*) FROM '{out_rel}'").fetchone()[0]
            outputs.append(out)
            if verbose:
                print(f"  {name:7s} {sql_file:26s} -> {out_rel:36s} "
                      f"{rows:>9,} rows  {time.perf_counter() - t0:5.1f}s")
    return outputs


@contextlib.contextmanager
def _working_directory(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)
