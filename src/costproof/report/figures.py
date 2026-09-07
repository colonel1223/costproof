"""Figures for the README and the executive report.

Two rules, both about honesty rather than aesthetics:

1. Every figure is regenerated from the study outputs. No figure is ever hand-edited,
   so nothing in the repo can drift away from the numbers the code produces.
2. Diagnostics that could embarrass the project are plotted as prominently as the
   headline result. The event-study plot exists precisely so a reader can check whether
   parallel trends holds before believing anything else.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

# Colour-blind safe, consistent across every figure.
INK = "#1c1c1c"
MUTED = "#8a8a8a"
GRID = "#e4e4e4"
TREAT = "#0f62fe"   # blue: the treated unit / identified estimate
CTRL = "#a8a8a8"    # grey: controls
BAD = "#da1e28"     # red: the invalid estimator
GOOD = "#0e6027"    # green: passes


def _style(ax, title: str = "", xlabel: str = "", ylabel: str = ""):
    ax.set_facecolor("white")
    ax.grid(True, color=GRID, linewidth=0.7, zorder=0)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(MUTED)
    if title:
        ax.set_title(title, fontsize=11.5, color=INK, loc="left", pad=10, weight="bold")
    ax.set_xlabel(xlabel, fontsize=9.5, color=INK)
    ax.set_ylabel(ylabel, fontsize=9.5, color=INK)
    ax.tick_params(colors=MUTED, labelsize=8.5)
    return ax


def event_study_plot(es: pd.DataFrame, out: Path, title: str = "") -> Path:
    """Event-study coefficients with 95% intervals.

    The pre-treatment coefficients are the test. If they sit on zero, parallel trends is
    not contradicted by the data. If they trend, the design is invalid and no post-period
    estimate from it should be believed.
    """
    fig, ax = plt.subplots(figsize=(8.2, 4.2), dpi=160)
    _style(ax, title or "Event study: effect by week relative to the action",
           "days relative to intervention", "log-point change vs. control")

    pre, post = es[es["is_pre"]], es[~es["is_pre"]]
    for part, colour, label in ((pre, MUTED, "pre-treatment (should be zero)"),
                                (post, TREAT, "post-treatment")):
        ax.errorbar(part["rel_day_start"], part["coef"],
                    yerr=[part["coef"] - part["ci_low"], part["ci_high"] - part["coef"]],
                    fmt="o", ms=4.5, lw=1.3, capsize=2.6, color=colour, label=label, zorder=3)

    ax.axhline(0, color=INK, lw=1.0, zorder=2)
    ax.axvline(0, color=BAD, lw=1.1, ls="--", zorder=2)
    ax.text(0.6, ax.get_ylim()[1] * 0.92, "action", color=BAD, fontsize=8.5)
    ax.legend(frameon=False, fontsize=8.5, loc="lower left")
    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return out


def synthetic_control_plot(detail: dict, out: Path, title: str = "") -> Path:
    """Treated unit against its synthetic counterfactual, plus the gap."""
    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(8.2, 5.6), dpi=160, sharex=True,
        gridspec_kw={"height_ratios": [2.1, 1]},
    )
    d = detail["rel_day"]
    _style(ax1, title or "Treated resource vs. synthetic control", "", "log daily cost")
    ax1.plot(d, detail["treated"], color=TREAT, lw=1.7, label="treated resource", zorder=3)
    ax1.plot(d, detail["synthetic"], color=INK, lw=1.4, ls="--",
             label="synthetic control", zorder=3)
    ax1.axvline(0, color=BAD, lw=1.1, ls="--", zorder=2)
    ax1.legend(frameon=False, fontsize=8.5, loc="upper left")

    _style(ax2, "", "days relative to intervention", "gap (log points)")
    ax2.plot(d, detail["gap"], color=TREAT, lw=1.4, zorder=3)
    ax2.axhline(0, color=INK, lw=1.0, zorder=2)
    ax2.axvline(0, color=BAD, lw=1.1, ls="--", zorder=2)
    ax2.fill_between(d, detail["gap"], 0, where=(d >= 0), color=TREAT, alpha=0.16, zorder=2)
    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return out


def estimator_scorecard_plot(score: pd.DataFrame, out: Path) -> Path:
    """The headline result: accuracy, interval coverage, and false-positive rate."""
    labels = {
        "naive_before_after": "naive\nbefore/after",
        "did_twfe": "DiD\ncluster SE",
        "did_permutation": "DiD\nrandomization",
        "synthetic_control": "synthetic\ncontrol",
    }
    s = score.copy()
    s["label"] = s["method"].map(labels).fillna(s["method"])

    fig, axes = plt.subplots(1, 3, figsize=(11.4, 3.9), dpi=160)
    x = np.arange(len(s))

    _style(axes[0], "RMSE vs. known truth", "", "log points")
    axes[0].bar(x, s["rmse"], color=[BAD if "naive" in m else TREAT for m in s["method"]],
                zorder=3, width=0.62)
    axes[0].set_xticks(x); axes[0].set_xticklabels(s["label"], fontsize=8)

    _style(axes[1], "95% interval coverage", "", "share containing truth")
    cols = [GOOD if c >= 0.80 else BAD for c in s["coverage_95"]]
    axes[1].bar(x, s["coverage_95"], color=cols, zorder=3, width=0.62)
    axes[1].axhline(0.95, color=INK, ls="--", lw=1.1, zorder=4)
    axes[1].text(len(s) - 0.45, 0.965, "nominal 95%", fontsize=7.5, color=INK, ha="right")
    axes[1].set_ylim(0, 1.08)
    axes[1].set_xticks(x); axes[1].set_xticklabels(s["label"], fontsize=8)

    _style(axes[2], "False positives on null interventions", "", "share declared significant")
    cols = [GOOD if v <= 0.15 else BAD for v in s["size_false_positive"]]
    axes[2].bar(x, s["size_false_positive"], color=cols, zorder=3, width=0.62)
    for xi, v in zip(x, s["size_false_positive"]):
        axes[2].text(xi, v + 0.02, f"{v:.0%}", ha="center", fontsize=8, color=INK)
    axes[2].axhline(0.05, color=INK, ls="--", lw=1.1, zorder=4)
    axes[2].text(len(s) - 0.45, 0.075, "nominal 5%", fontsize=7.5, color=INK, ha="right")
    axes[2].set_ylim(0, 1.02)
    axes[2].set_xticks(x); axes[2].set_xticklabels(s["label"], fontsize=8)

    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return out


def error_distribution_plot(results: pd.DataFrame, out: Path) -> Path:
    """Estimation error by method. Width is what matters, not just centring."""
    order = ["naive_before_after", "did_twfe", "did_permutation", "synthetic_control"]
    labels = ["naive\nbefore/after", "DiD\ncluster SE", "DiD\nrandomization",
              "synthetic\ncontrol"]
    data = [results.loc[results["method"] == m, "error"].dropna().to_numpy() for m in order]
    keep = [(d, lab) for d, lab in zip(data, labels) if len(d)]

    fig, ax = plt.subplots(figsize=(7.6, 4.0), dpi=160)
    _style(ax, "Estimation error against known truth",
           "", "estimate minus true effect (log points)")
    bp = ax.boxplot([d for d, _ in keep], patch_artist=True, widths=0.5,
                    medianprops={"color": INK, "linewidth": 1.4},
                    flierprops={"marker": ".", "markersize": 3, "markerfacecolor": MUTED,
                                "markeredgecolor": MUTED})
    for i, patch in enumerate(bp["boxes"]):
        patch.set_facecolor(BAD if "naive" in order[i] else TREAT)
        patch.set_alpha(0.75)
        patch.set_edgecolor(INK)
    ax.axhline(0, color=GOOD, lw=1.3, ls="--", zorder=4)
    ax.set_xticklabels([lab for _, lab in keep], fontsize=8.5)
    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return out
