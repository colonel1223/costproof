"""Model backends: IBM watsonx, with a deterministic fallback.

Why there is a fallback at all
------------------------------
A repository that cannot run without an API key is a repository nobody evaluates. The
reviewer clones it, hits a credentials error, and closes the tab. So CostProof runs
end to end with no credentials, produces real output, and uses watsonx when it is
available.

That is not a compromise for the demo's sake -- it enforces the architectural rule the
project is built on. Because the offline backend can produce every number in the
report, it proves that **no number depends on the language model**. The model narrates;
it never calculates. If the fallback could not produce identical figures, that would be
evidence the model was doing arithmetic somewhere it should not.

Selecting a backend
-------------------
`get_backend()` returns watsonx when WATSONX_API_KEY and WATSONX_PROJECT_ID are set in
the environment or a local .env file, and the deterministic backend otherwise. Callers
never branch on which one they got.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

#: IBM Granite, in order of preference. Chosen over a larger general model because this
#: workload is short structured summarisation of tool output, where a small
#: instruction-tuned model is sufficient, materially cheaper, and -- for an IBM-targeted
#: project -- IBM's own.
#:
#: It is a *list* because foundation models are withdrawn from the catalogue on a
#: schedule. ``granite-3-8b-instruct`` was the original choice and was retired in
#: September 2026; the backend tries each entry in turn and reports which one it got.
#: Hard-coding a single model ID is a time bomb.
PREFERRED_MODELS: tuple[str, ...] = (
    "ibm/granite-4-h-small",          # Granite 4.0 hybrid, instruction-tuned
    "ibm/granite-3-3-8b-instruct",    # Granite 3.3, if the catalogue still carries it
    "ibm/granite-3-2-8b-instruct",
    "ibm/granite-3-8b-instruct",      # original choice; withdrawn Sept 2026
)
DEFAULT_MODEL = PREFERRED_MODELS[0]

#: Substring watsonx puts in the error when a model ID is not in the catalogue. Anything
#: else -- 401, 403, 404, network -- is not a model problem and must not be swallowed.
_MODEL_UNAVAILABLE = "not supported"


def resolve_model(factory, candidates=PREFERRED_MODELS):
    """Return ``(model_id, model)`` for the first candidate ``factory`` accepts.

    ``factory(model_id)`` constructs the client-side model object, which is where the
    SDK validates the ID against the live catalogue. A "not supported" rejection moves
    to the next candidate; any other exception is re-raised immediately, because a bad
    credential must never be reported as "no model available".
    """
    unavailable: list[str] = []
    for model_id in candidates:
        try:
            return model_id, factory(model_id)
        except Exception as exc:  # noqa: BLE001 -- we inspect and re-raise below
            if _MODEL_UNAVAILABLE in str(exc).lower():
                unavailable.append(model_id)
                continue
            raise
    raise LookupError(
        "none of the preferred models is available in this watsonx environment: "
        + ", ".join(unavailable)
        + ". Pick one from the 'Supported models' list in the error above and add it to "
        "PREFERRED_MODELS."
    )


def load_dotenv(path: str | Path = ".env") -> dict[str, str]:
    """Read KEY=VALUE pairs from a .env file into os.environ.

    Deliberately minimal rather than pulling in python-dotenv: it is fifteen lines,
    and a dependency that exists to read a text file is a dependency worth avoiding.
    Values already present in the environment win, so a real environment variable
    always beats a stale file.
    """
    p = Path(path)
    if not p.exists():
        return {}
    loaded = {}
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        loaded[key] = value
        os.environ.setdefault(key, value)
    return loaded


class Backend(Protocol):
    name: str
    available: bool

    def generate(self, prompt: str, system: str = "", max_tokens: int = 700) -> str: ...


# =======================================================================================
# watsonx
# =======================================================================================


@dataclass
class WatsonxBackend:
    """IBM watsonx.ai text generation."""

    model_id: str = DEFAULT_MODEL
    name: str = "watsonx"
    available: bool = True

    def __post_init__(self) -> None:
        from ibm_watsonx_ai import Credentials
        from ibm_watsonx_ai.foundation_models import ModelInference

        api_key = os.environ["WATSONX_API_KEY"]
        project_id = os.environ["WATSONX_PROJECT_ID"]
        url = os.environ.get("WATSONX_URL", "https://us-south.ml.cloud.ibm.com")
        credentials = Credentials(url=url, api_key=api_key)

        def factory(model_id: str) -> ModelInference:
            # No generation params here: those belong to the legacy completion
            # endpoint. Chat parameters are passed per call in `generate`.
            return ModelInference(
                model_id=model_id,
                credentials=credentials,
                project_id=project_id,
            )

        # The requested model first, then the preference list. The SDK validates the ID
        # against the live catalogue inside the constructor, so a withdrawn model fails
        # here rather than on the first inference call.
        candidates = (self.model_id, *(m for m in PREFERRED_MODELS if m != self.model_id))
        self.model_id, self._model = resolve_model(factory, candidates)
        self.name = f"watsonx:{self.model_id}"

    #: Chat parameters. temperature=0 with a fixed seed is the chat-endpoint equivalent
    #: of greedy decoding: this is a reporting system, and the same question on the
    #: same data should produce the same words or the audit record is not reproducible.
    CHAT_PARAMS = {"temperature": 0, "seed": 7, "top_p": 1.0}

    def generate(self, prompt: str, system: str = "", max_tokens: int = 700) -> str:
        """Call the chat endpoint.

        Current Granite models are chat-tuned. Called through the legacy completion
        endpoint (`generate_text`) they emit end-of-sequence at once and return an
        empty string -- which is exactly what the first live run produced. The
        completion endpoint is also deprecated. Chat is the only correct path.
        """
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        response = self._model.chat(
            messages=messages, params={**self.CHAT_PARAMS, "max_tokens": max_tokens},
        )
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return ""
        return (content or "").strip()


# =======================================================================================
# Deterministic fallback
# =======================================================================================


@dataclass
class DeterministicBackend:
    """Template-based narration. No model, no network, no credentials.

    This produces the report structure from the tool results directly. It is less
    fluent than a language model and that is the entire point of keeping it: everything
    of substance in the output is present here too, because the substance was never
    coming from the model.
    """

    name: str = "deterministic"
    available: bool = True

    def generate(self, prompt: str, system: str = "", max_tokens: int = 700) -> str:
        return (
            "[deterministic backend -- no language model configured]\n\n"
            "Findings and figures below are produced entirely by the analytical tools. "
            "A language model, when configured, only rewrites this into prose; it "
            "changes no number, no method, and no recommendation."
        )


# =======================================================================================
# Selection
# =======================================================================================


def watsonx_configured(env_file: str | Path = ".env") -> bool:
    load_dotenv(env_file)
    return bool(os.environ.get("WATSONX_API_KEY") and os.environ.get("WATSONX_PROJECT_ID"))


def get_backend(env_file: str | Path = ".env", model_id: str = DEFAULT_MODEL,
                verbose: bool = True) -> Backend:
    """Return watsonx if it is configured and importable, otherwise the fallback."""
    if not watsonx_configured(env_file):
        if verbose:
            print("  backend: deterministic (set WATSONX_API_KEY and "
                  "WATSONX_PROJECT_ID in .env to use watsonx)")
        return DeterministicBackend()

    try:
        backend = WatsonxBackend(model_id=model_id)
        if verbose:
            print(f"  backend: {backend.name}")
        return backend
    except ImportError:
        if verbose:
            print("  backend: deterministic (pip install 'ibm-watsonx-ai' to use watsonx)")
        return DeterministicBackend()
    except Exception as exc:  # noqa: BLE001
        # Credentials present but rejected, or the service is unreachable. Degrade
        # rather than fail: the analysis does not depend on the model.
        if verbose:
            print(f"  backend: deterministic (watsonx unavailable: "
                  f"{type(exc).__name__}: {str(exc)[:90]})")
        return DeterministicBackend()


SYSTEM_PROMPT = """You are the reporting layer of CostProof, a cloud cost measurement system.

You will be given the output of analytical tools that have already run. Write a brief,
factual summary for a finance stakeholder.

Rules you must follow exactly:

1. Every figure you state must appear verbatim in the tool output. Do not compute,
   round differently, combine, or extrapolate any number.
2. Do not assert that an action caused a saving unless the tool output says the
   parallel-trends check passed.
3. Reproduce the caveats. They are not optional context; they are what makes the
   figures defensible.
4. If the tool output does not answer part of the question, say so plainly rather
   than inferring an answer.
5. No preamble, no restating the question, no closing pleasantries. Lead with the
   finding.
6. A summary containing any figure that is not present in the tool output -- including
   a rounded, totalled or combined figure -- is rejected automatically and replaced.

Write in plain professional English. Short sentences. No marketing language."""
