"""Invariants of the OTel span wire contract (hexgate.tracing.semconv)."""

from __future__ import annotations

from hexgate.tracing import semconv

_SCOPES = {"SCOPE_AUDIT", "SCOPE_USAGE", "SCOPE_BANS", "SCOPE_MESSAGES"}
_GEN_AI = {
    "GEN_AI_REQUEST_MODEL",
    "GEN_AI_USAGE_INPUT_TOKENS",
    "GEN_AI_USAGE_OUTPUT_TOKENS",
    "GEN_AI_INPUT_MESSAGES",
    "GEN_AI_OUTPUT_MESSAGES",
    "GEN_AI_SYSTEM_INSTRUCTIONS",
}


def _constants() -> dict[str, str]:
    return {
        name: value
        for name, value in vars(semconv).items()
        if not name.startswith("_") and isinstance(value, str)
    }


def test_when_all_values_are_collected_then_none_collide() -> None:
    """Two constants sharing a wire string would silently merge two fields."""
    values = list(_constants().values())
    assert len(values) == len(set(values))


def test_when_names_are_grouped_then_prefixes_match_their_namespace() -> None:
    """Scopes name the emitting library; attributes name the shared vocabulary."""
    for name, value in _constants().items():
        if name in _SCOPES:
            assert value.startswith("hexgate."), name
        elif name in _GEN_AI:
            assert value.startswith("gen_ai."), name
        else:
            assert value.startswith("sec_ai."), name


def test_when_gen_ai_names_are_read_then_they_match_the_official_semconv() -> None:
    """Inlined literals (not imported — see module docstring) must track the
    official gen_ai convention verbatim."""
    assert semconv.GEN_AI_REQUEST_MODEL == "gen_ai.request.model"
    assert semconv.GEN_AI_USAGE_INPUT_TOKENS == "gen_ai.usage.input_tokens"
    assert semconv.GEN_AI_USAGE_OUTPUT_TOKENS == "gen_ai.usage.output_tokens"
    assert semconv.GEN_AI_INPUT_MESSAGES == "gen_ai.input.messages"
    assert semconv.GEN_AI_OUTPUT_MESSAGES == "gen_ai.output.messages"
    assert semconv.GEN_AI_SYSTEM_INSTRUCTIONS == "gen_ai.system_instructions"


def test_when_the_message_scope_is_read_then_it_names_the_emitting_library() -> None:
    """The scope selects the event type platform-side; the content attributes
    stay OTel's so a foreign backend reads standard GenAI fields."""
    assert semconv.SCOPE_MESSAGES == "hexgate.messages"
    for name in ("MESSAGE_SEQ", "TURN_KEY", "RESYNCED", "TRUNCATED"):
        assert getattr(semconv, name).startswith("sec_ai."), name
