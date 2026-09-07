"""Model resolution against a changing catalogue.

watsonx withdraws foundation models on a schedule -- ``ibm/granite-3-8b-instruct`` went
in September 2026 after this project had hard-coded it. These tests pin the behaviour
that replaced the hard-code: try the preferred models in order, skip the withdrawn
ones, and never let a credential failure masquerade as "no model available".

No SDK, no network, no credentials: ``resolve_model`` takes a factory callable, so a
fake factory stands in for the watsonx client.
"""

from __future__ import annotations

import pytest

from costproof.agent import llm

WITHDRAWN = "Model '{}' is not supported for this environment. Supported models: [...]"


def _catalogue(*available: str):
    """A fake ModelInference constructor backed by a fixed catalogue."""
    calls: list[str] = []

    def factory(model_id: str):
        calls.append(model_id)
        if model_id not in available:
            raise RuntimeError(WITHDRAWN.format(model_id))
        return f"<model {model_id}>"

    factory.calls = calls
    return factory


def test_first_preferred_model_is_used_when_available():
    factory = _catalogue("ibm/granite-4-h-small")
    model_id, model = llm.resolve_model(factory, ("ibm/granite-4-h-small", "ibm/other"))
    assert model_id == "ibm/granite-4-h-small"
    assert model == "<model ibm/granite-4-h-small>"
    assert factory.calls == ["ibm/granite-4-h-small"]


def test_withdrawn_models_are_skipped_in_order():
    factory = _catalogue("ibm/granite-3-8b-instruct")
    model_id, _ = llm.resolve_model(
        factory, ("ibm/granite-4-h-small", "ibm/granite-3-3-8b-instruct",
                  "ibm/granite-3-8b-instruct"),
    )
    assert model_id == "ibm/granite-3-8b-instruct"
    assert factory.calls == ["ibm/granite-4-h-small", "ibm/granite-3-3-8b-instruct",
                             "ibm/granite-3-8b-instruct"]


def test_nothing_available_names_every_candidate():
    factory = _catalogue()
    with pytest.raises(LookupError) as exc:
        llm.resolve_model(factory, ("ibm/a", "ibm/b"))
    assert "ibm/a" in str(exc.value) and "ibm/b" in str(exc.value)
    assert "PREFERRED_MODELS" in str(exc.value)


def test_credential_failure_is_not_mistaken_for_a_missing_model():
    """A 401 on the first candidate must surface immediately, not fall through the
    list and end as 'no model available' -- that would send the user hunting for a
    model ID when the problem is their key."""
    def factory(model_id: str):
        raise RuntimeError("401 Unauthorized: invalid API key")

    with pytest.raises(RuntimeError, match="401"):
        llm.resolve_model(factory, ("ibm/a", "ibm/b"))


def test_default_model_is_head_of_the_preference_list():
    assert llm.PREFERRED_MODELS[0] == llm.DEFAULT_MODEL
    assert "ibm/granite-3-8b-instruct" in llm.PREFERRED_MODELS  # kept as last resort


def test_generate_uses_the_chat_endpoint_and_reads_the_documented_shape():
    """Granite 4 is chat-tuned: the legacy completion endpoint returns an empty string
    (the first live run) and is deprecated. `generate` must call `.chat()` with a
    system + user message pair and read choices[0].message.content."""
    calls = {}

    class FakeModel:
        def chat(self, messages, params=None, **_):
            calls["messages"] = messages
            calls["params"] = params
            return {"choices": [{"message": {"role": "assistant", "content": "  connected  "}}]}

    backend = object.__new__(llm.WatsonxBackend)   # skip __post_init__ (needs SDK + creds)
    backend._model = FakeModel()
    out = backend.generate("Reply with one word.", system="You are a test.", max_tokens=8)

    assert out == "connected"
    assert calls["messages"][0] == {"role": "system", "content": "You are a test."}
    assert calls["messages"][1] == {"role": "user", "content": "Reply with one word."}
    assert calls["params"]["temperature"] == 0        # deterministic reporting
    assert calls["params"]["max_tokens"] == 8


def test_generate_returns_empty_string_on_malformed_response():
    class FakeModel:
        def chat(self, messages, params=None, **_):
            return {"choices": []}

    backend = object.__new__(llm.WatsonxBackend)
    backend._model = FakeModel()
    assert backend.generate("x") == ""   # the guard, not an exception, handles this
