#!/usr/bin/env Rscript
# =======================================================================================
# Cross-validate CostProof's hand-rolled DiD estimator against R.
#
# Python (src/costproof/causal/did.py) implements two-way fixed effects by demeaning:
#
#     log(cost_it) = alpha_i + lambda_t + tau * (treated_i x post_t) + e_it
#
# This script re-estimates tau for every panel the study used, three ways, none of
# which share code with the Python implementation:
#
#   1. plm::plm, model = "within", effect = "twoways"
#        The canonical R panel-econometrics package (Croissant & Millo, JSS 2008). Same
#        within transformation as the Python, different codebase, different language.
#
#   2. lm() with explicit unit and time dummies
#        Brute force. The Frisch-Waugh-Lovell theorem says this must equal (1). If it
#        does, the demeaning shortcut is algebraically sound on these panels.
#
#   3. sandwich::vcovCL(type = "HC1") on (2), clustered on resource
#        The cluster-robust standard error with the standard small-sample correction
#        G/(G-1) x (N-1)/(N-K). K counts the absorbed fixed effects, which is why (2)
#        rather than (1) is used for the SE: plm's own vcovHC counts only the slope
#        coefficients in K and so applies a slightly different correction.
#
# Usage:   Rscript R/crossvalidate.R [outputs/panels]
# Reads:   <dir>/panels.csv, <dir>/python_estimates.csv
# Writes:  <dir>/r_estimates.csv
#
# Requires: plm, sandwich, data.table   (install.packages(c("plm","sandwich","data.table")))
# =======================================================================================

suppressPackageStartupMessages({
  library(data.table)
  library(plm)
  library(sandwich)
})

args      <- commandArgs(trailingOnly = TRUE)
panel_dir <- if (length(args) >= 1) args[1] else "outputs/panels"

panels <- fread(file.path(panel_dir, "panels.csv"))
python <- fread(file.path(panel_dir, "python_estimates.csv"))
ids    <- unique(panels$intervention_id)

cat(sprintf("R %s | plm %s | sandwich %s\n",
            getRversion(), packageVersion("plm"), packageVersion("sandwich")))
cat(sprintf("%d panels, %s resource-days\n", length(ids), format(nrow(panels), big.mark = ",")))

estimate_one <- function(d) {
  # --- 1. plm within, two-way ---------------------------------------------------------
  pd  <- pdata.frame(as.data.frame(d), index = c("resource_id", "rel_day"))
  m1  <- plm(log_cost ~ treat_post, data = pd, model = "within", effect = "twoways")
  b1  <- unname(coef(m1)["treat_post"])

  # --- 2. lm with explicit dummies (FWL check) ------------------------------------------
  m2  <- lm(log_cost ~ treat_post + factor(resource_id) + factor(rel_day), data = d)
  b2  <- unname(coef(m2)["treat_post"])

  # --- 3. cluster-robust SE, HC1 small-sample correction, clustered on resource -------
  V   <- vcovCL(m2, cluster = ~resource_id, type = "HC1")
  se  <- sqrt(V["treat_post", "treat_post"])

  data.table(
    intervention_id = d$intervention_id[1],
    r_coef          = b1,          # plm within
    r_coef_lm       = b2,          # brute-force dummies
    r_se_cluster    = se,          # sandwich HC1, clustered
    r_n_obs         = nrow(d),
    r_n_units       = uniqueN(d$resource_id),
    r_within_vs_lm  = abs(b1 - b2) # FWL agreement inside R itself
  )
}

t0  <- Sys.time()
out <- rbindlist(lapply(ids, function(id) estimate_one(panels[intervention_id == id])))
elapsed <- as.numeric(difftime(Sys.time(), t0, units = "secs"))

fwrite(out, file.path(panel_dir, "r_estimates.csv"))

# --- summary against Python ------------------------------------------------------------
both <- merge(python, out, by = "intervention_id")
both[, coef_abs_diff := abs(py_coef - r_coef)]
both[, se_ratio      := py_se_cluster / r_se_cluster]

cat(sprintf("\nestimated %d panels in %.1fs\n", nrow(out), elapsed))
cat(sprintf("max |python - plm|          %.3e\n", max(both$coef_abs_diff)))
cat(sprintf("max |plm - lm dummies|      %.3e   (Frisch-Waugh-Lovell, inside R)\n",
            max(out$r_within_vs_lm)))
cat(sprintf("cluster-SE ratio py / R     min %.8f  max %.8f\n",
            min(both$se_ratio), max(both$se_ratio)))
cat(sprintf("wrote %s\n", file.path(panel_dir, "r_estimates.csv")))
