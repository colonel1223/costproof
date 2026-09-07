"""Verify watsonx credentials before running anything that depends on them.

Credential problems in this stack are hard to diagnose because the error messages point
at the wrong thing. The three failures below account for nearly all of them, and each
produces a message that does not name its real cause:

* **No Watson Machine Learning service associated with the project.** The key
  authenticates, the project resolves, and inference returns a permission error that
  never mentions the association. Fixed in the project's Manage tab, not in IAM.
* **Region mismatch.** A key from a Dallas account against the Frankfurt endpoint
  fails as though the credentials were wrong. They are not; they are in the wrong place.
* **Project ID vs space ID.** Both are UUIDs, both look plausible, and passing one
  where the other belongs fails with a not-found rather than a type error.

This runs each stage separately so the output names which one broke.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from costproof.agent import llm

CHECK = "✅"
CROSS = "❌"
WARN = "⚠️"

REGION_HINTS = {
    "us-south": "Dallas", "eu-de": "Frankfurt", "eu-gb": "London",
    "jp-tok": "Tokyo", "ca-tor": "Toronto", "au-syd": "Sydney",
}


def _mask(value: str, keep: int = 4) -> str:
    """Show enough of a secret to confirm which one it is, never enough to use it."""
    if not value:
        return "(empty)"
    return f"{value[:keep]}{'*' * max(0, len(value) - keep * 2)}{value[-keep:]}" \
        if len(value) > keep * 2 else "*" * len(value)


def run(env_file: str | Path = ".env", verbose: bool = True) -> dict:
    """Check credentials stage by stage. Returns a dict of results."""
    results: dict = {"stages": [], "ok": False}

    def stage(name: str, ok: bool, detail: str = "", fix: str = "") -> bool:
        results["stages"].append({"stage": name, "ok": ok, "detail": detail, "fix": fix})
        if verbose:
            print(f"  {CHECK if ok else CROSS} {name}")
            if detail:
                print(f"       {detail}")
            if not ok and fix:
                print(f"       fix: {fix}")
        return ok

    if verbose:
        print("\nwatsonx connection check\n")

    # --- 1. env file ------------------------------------------------------------
    loaded = llm.load_dotenv(env_file)
    if not stage(
        "Environment file found",
        Path(env_file).exists(),
        f"read {len(loaded)} variable(s) from {env_file}",
        f"create {env_file} with WATSONX_API_KEY, WATSONX_PROJECT_ID and WATSONX_URL",
    ):
        return results

    # --- 2. variables present ---------------------------------------------------
    api_key = os.environ.get("WATSONX_API_KEY", "")
    project_id = os.environ.get("WATSONX_PROJECT_ID", "")
    url = os.environ.get("WATSONX_URL", "")

    if not stage("WATSONX_API_KEY set", bool(api_key), _mask(api_key),
                 "cloud.ibm.com -> Manage -> Access (IAM) -> API keys -> Create"):
        return results

    # Placeholder text is a real and common failure -- the .env gets created from a
    # template and never filled in, and every downstream error is then misleading.
    if any(t in api_key.lower() for t in ("paste", "your-", "xxx", "<")):
        stage("API key looks real", False, f"value is {api_key!r}",
              "replace the placeholder with the key you copied from IBM Cloud")
        return results

    ok_project = stage(
        "WATSONX_PROJECT_ID set", bool(project_id), _mask(project_id, 8),
        "dataplatform.cloud.ibm.com -> your project -> Manage -> General -> Project ID",
    )
    if not ok_project:
        return results

    if len(project_id) != 36 or project_id.count("-") != 4:
        stage("Project ID is a UUID", False, f"got {len(project_id)} chars",
              "copy the Project ID from the Manage tab, not the project name or URL slug")
        return results

    region = next((r for r in REGION_HINTS if r in url), None)
    stage("WATSONX_URL set", bool(url),
          f"{url}  ({REGION_HINTS.get(region, 'unrecognised region')})",
          "must match the region your watsonx instance was provisioned in")

    # --- 3. SDK installed --------------------------------------------------------
    try:
        from ibm_watsonx_ai import Credentials  # noqa: F401
        from ibm_watsonx_ai.foundation_models import ModelInference  # noqa: F401
        stage("ibm-watsonx-ai installed", True)
    except ImportError as exc:
        stage("ibm-watsonx-ai installed", False, str(exc),
              "pip install ibm-watsonx-ai")
        return results

    # --- 4. authenticate + one real call ------------------------------------------
    try:
        t0 = time.perf_counter()
        backend = llm.WatsonxBackend()
        text = backend.generate(
            "Reply with exactly the word: connected",
            system="You are a connection test. Reply with one word.",
            max_tokens=8,
        )
        elapsed = (time.perf_counter() - t0) * 1000
        stage("Inference call succeeded", True,
              f"model {backend.model_id}, {elapsed:.0f}ms, replied {text[:40]!r}")
        results["ok"] = True
        results["model"] = backend.model_id
        results["latency_ms"] = round(elapsed, 1)
        results["reply"] = text[:200]
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        fix = "check the API key and project ID"
        low = message.lower()
        if "not_authorized" in low or "403" in low or "forbidden" in low:
            fix = ("associate a Watson Machine Learning service with the project: "
                   "project -> Manage -> Services & integrations -> Associate service. "
                   "This is the most common cause and the error never mentions it.")
        elif "not found" in low or "404" in low:
            fix = ("the project ID may be a SPACE id, or the region in WATSONX_URL may "
                   "not match where the project lives")
        elif "unauthor" in low or "401" in low:
            fix = "the API key is wrong or was revoked; create a new one in IAM"
        stage("Inference call succeeded", False,
              f"{type(exc).__name__}: {message[:170]}", fix)

    if verbose:
        print()
        print(f"  {CHECK} watsonx is configured and reachable." if results["ok"]
              else f"  {WARN} watsonx is not usable yet. CostProof still runs -- "
                   "the deterministic backend produces identical figures.")
        print()
    return results
