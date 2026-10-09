"""The `usage:` policy block: what `agent_usage.*` reads when the snapshot is missing.

Agent-wide like the guard stance (R-GUARD-006/007): every role must resolve to one
`on_unavailable`, checked when the policy set loads. Absent means `allow` (G3). A
`deny` is signed into the bundle manifest, and a usage-free policy's dump and
manifest bytes must not move. These tests pin the model shape, placement,
inheritance, cross-role agreement, the serializer drop and the bundle carriage.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from hexgate.runtime.agent_usage import OnUnavailable
from hexgate.security.bundle import PolicyBundle, build_signed_bundle
from hexgate.security.models import AgentPolicy, UsagePolicy
from hexgate.security.policy_set import (
    PolicySetError,
    load_policy_set,
    load_policy_set_from_dict,
)
from hexgate.security.rego import compile_to_rego

DENY_BLOCK = {"on_unavailable": "deny"}
ALLOW_BLOCK = {"on_unavailable": "allow"}
TOOLS = {"send_email": {"mode": "allow"}}


WASM_STUB = b"\x00asm"


def _manifest_bytes(payload: dict) -> bytes:
    return build_signed_bundle(
        yaml.safe_dump(payload), compile_wasm=False
    ).manifest_bytes


def _read_back(manifest_bytes: bytes) -> PolicyBundle:
    """The bundle as the SDK receives it: parsed from the served manifest bytes."""
    return PolicyBundle.from_parts(wasm_bytes=WASM_STUB, manifest_bytes=manifest_bytes)


def _bundle_from(payload: dict) -> PolicyBundle:
    return _read_back(_manifest_bytes(payload))


def _bundle_with_manifest_usage(section: object) -> PolicyBundle:
    manifest = json.loads(_manifest_bytes({"tools": TOOLS}))
    return _read_back(json.dumps({**manifest, "usage": section}).encode("utf-8"))


# --- UsagePolicy shape --------------------------------------------------------


def test_usage_policy_requires_on_unavailable() -> None:
    """A block states an intent; there is no implied default."""
    with pytest.raises(ValidationError):
        UsagePolicy.model_validate({})


def test_usage_policy_rejects_unknown_key() -> None:
    with pytest.raises(ValidationError):
        UsagePolicy.model_validate({"on_unavailable": "deny", "max_staleness": 5})


def test_usage_policy_rejects_unknown_value() -> None:
    with pytest.raises(ValidationError):
        UsagePolicy.model_validate({"on_unavailable": "warn"})


def test_usage_policy_validates_deny() -> None:
    assert UsagePolicy.model_validate(DENY_BLOCK).on_unavailable is OnUnavailable.DENY


# --- placement ----------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"tools": {"send_email": {"mode": "allow", "usage": DENY_BLOCK}}},
        {"default_policy": {"mode": "deny", "usage": DENY_BLOCK}},
        {"admission": {"mode": "allow", "usage": DENY_BLOCK}},
    ],
    ids=["tool", "default_policy", "admission"],
)
def test_usage_rejected_below_the_policy(payload: dict) -> None:
    with pytest.raises(ValidationError):
        AgentPolicy.model_validate(payload)


def test_file_level_usage_beside_roles_is_rejected() -> None:
    with pytest.raises(PolicySetError, match="move them under a role"):
        load_policy_set_from_dict(
            {"usage": DENY_BLOCK, "roles": {"default": {"tools": TOOLS}}}
        )


# --- reader -------------------------------------------------------------------


def test_absent_usage_reads_allow() -> None:
    """G3: a policy that says nothing fails open."""
    assert AgentPolicy().usage_on_unavailable() is OnUnavailable.ALLOW


def test_flat_policy_reads_its_block() -> None:
    ps = load_policy_set_from_dict({"usage": DENY_BLOCK, "tools": TOOLS})
    assert ps.usage_on_unavailable() is OnUnavailable.DENY


# --- inheritance --------------------------------------------------------------


def test_mixin_deny_reaches_a_silent_child() -> None:
    ps = load_policy_set_from_dict(
        {
            "roles": {
                "base": {"is_mixin": True, "usage": DENY_BLOCK},
                "default": {"inherits": ["base"]},
            }
        }
    )
    assert ps.usage_on_unavailable() is OnUnavailable.DENY


def test_child_allow_overrides_a_mixin_deny() -> None:
    ps = load_policy_set_from_dict(
        {
            "roles": {
                "base": {"is_mixin": True, "usage": DENY_BLOCK},
                "default": {"inherits": ["base"], "usage": ALLOW_BLOCK},
            }
        }
    )
    assert ps.usage_on_unavailable() is OnUnavailable.ALLOW


def test_later_silent_mixin_does_not_null_an_earlier_deny() -> None:
    """The `admission` rule: only a parent that sets the block overwrites it."""
    ps = load_policy_set_from_dict(
        {
            "roles": {
                "strict": {"is_mixin": True, "usage": DENY_BLOCK},
                "tools_only": {"is_mixin": True, "tools": TOOLS},
                "default": {"inherits": ["strict", "tools_only"]},
            }
        }
    )
    assert ps.usage_on_unavailable() is OnUnavailable.DENY


# --- cross-role agreement -----------------------------------------------------


def test_roles_that_agree_load() -> None:
    ps = load_policy_set_from_dict(
        {
            "roles": {
                "base": {"is_mixin": True, "usage": DENY_BLOCK},
                "support": {"inherits": ["base"]},
                "admin": {"inherits": ["base"]},
            }
        }
    )
    assert ps.usage_on_unavailable() is OnUnavailable.DENY


def test_deny_versus_silent_role_raises_naming_both() -> None:
    with pytest.raises(PolicySetError, match="same value across all roles") as exc:
        load_policy_set_from_dict(
            {
                "roles": {
                    "support": {"usage": DENY_BLOCK},
                    "admin": {"tools": TOOLS},
                }
            }
        )
    assert "support=deny" in str(exc.value)
    assert "admin=allow" in str(exc.value)


def test_deny_versus_allow_raises() -> None:
    with pytest.raises(PolicySetError, match="same value across all roles"):
        load_policy_set_from_dict(
            {
                "roles": {
                    "support": {"usage": DENY_BLOCK},
                    "admin": {"usage": ALLOW_BLOCK},
                }
            }
        )


def test_directory_shape_agrees_with_inline_roles(tmp_path: Path) -> None:
    """A `policies/` directory with no `default.yaml` aliases a role as default, and
    reads the same fail mode as the inline-roles shape."""
    roles = {
        "base": {"is_mixin": True, "usage": DENY_BLOCK},
        "support": {"inherits": ["base"], "tools": TOOLS},
        "admin": {"inherits": ["base"]},
    }
    policies = tmp_path / "policies"
    policies.mkdir()
    for name, spec in roles.items():
        (policies / f"{name}.yaml").write_text(yaml.safe_dump(spec))

    from_dir = load_policy_set(policies)
    inline = load_policy_set_from_dict({"roles": roles})

    assert from_dir.aliased_default is not None
    assert from_dir.usage_on_unavailable() is OnUnavailable.DENY
    assert inline.usage_on_unavailable() is from_dir.usage_on_unavailable()


# --- serializer ---------------------------------------------------------------


def test_usage_free_dump_has_no_usage_key() -> None:
    dumped = AgentPolicy.model_validate({"tools": TOOLS}).model_dump(mode="json")
    assert "usage" not in dumped


def test_deny_dump_carries_the_block_as_plain_yaml() -> None:
    dumped = AgentPolicy.model_validate(
        {"usage": DENY_BLOCK, "tools": TOOLS}
    ).model_dump(mode="json")
    assert dumped["usage"] == {"on_unavailable": "deny"}
    # PyYAML's safe dumper rejects str subclasses, so the StrEnum must dump plain.
    assert yaml.safe_load(yaml.safe_dump(dumped))["usage"] == DENY_BLOCK


# --- bundle -------------------------------------------------------------------


def test_bundle_carries_and_reads_deny() -> None:
    bundle = _bundle_from({"usage": DENY_BLOCK, "tools": TOOLS})
    assert bundle.manifest["usage"] == {"on_unavailable": "deny"}
    assert bundle.usage_on_unavailable() is OnUnavailable.DENY


@pytest.mark.parametrize(
    "payload",
    [{"tools": TOOLS}, {"usage": ALLOW_BLOCK, "tools": TOOLS}],
    ids=["silent", "explicit-allow"],
)
def test_bundle_omits_usage_at_the_default(payload: dict) -> None:
    bundle = _bundle_from(payload)
    assert "usage" not in bundle.manifest
    assert bundle.usage_on_unavailable() is OnUnavailable.ALLOW


def test_usage_free_manifest_bytes_carry_no_usage_key() -> None:
    assert "usage" not in json.loads(_manifest_bytes({"tools": TOOLS}))


@pytest.mark.parametrize(
    "section",
    [{"on_unavailable": "later"}, "deny"],
    ids=["unknown-value", "non-dict"],
)
def test_unreadable_manifest_usage_reads_allow(section: object) -> None:
    """A newer builder's mode must not turn a platform blip into an outage."""
    bundle = _bundle_with_manifest_usage(section)
    assert bundle.usage_on_unavailable() is OnUnavailable.ALLOW


def test_unknown_manifest_value_warns(caplog: pytest.LogCaptureFixture) -> None:
    bundle = _bundle_with_manifest_usage({"on_unavailable": "later"})
    with caplog.at_level(logging.WARNING, logger="hexgate.security.bundle"):
        bundle.usage_on_unavailable()
    assert "does not know" in caplog.text


# --- rego ---------------------------------------------------------------------


def test_usage_block_does_not_reach_the_rego() -> None:
    """`usage` is read beside the engine, never lowered into effective_tools."""
    source_hash = "0" * 64
    without = compile_to_rego({"tools": TOOLS}, source_hash=source_hash)
    with_deny = compile_to_rego(
        {"usage": DENY_BLOCK, "tools": TOOLS}, source_hash=source_hash
    )
    assert with_deny == without
