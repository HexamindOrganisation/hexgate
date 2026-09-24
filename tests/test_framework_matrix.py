"""Tests for the version-matrix driver's tier classification (``scripts/``).

The driver itself is not importable as a package, so it is loaded by path. Only
the pure reporting logic is covered here — nothing in this module runs uv, pytest
or the network.

The tier map is the load-bearing part: it decides which probe failures condemn a
framework version. A fragment landing in the wrong tier is silent, so these pin
the boundaries rather than the mapping's contents.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_DRIVER = Path(__file__).resolve().parents[1] / "scripts" / "framework_matrix.py"


def _load_driver() -> ModuleType:
    spec = importlib.util.spec_from_file_location("framework_matrix", _DRIVER)
    if spec is None or spec.loader is None:  # pragma: no cover — path is fixed
        pytest.fail(f"could not load the matrix driver from {_DRIVER}")
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: the driver's @dataclass resolves its annotations
    # through ``sys.modules[cls.__module__]``, which is absent for a path import.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def driver() -> ModuleType:
    return _load_driver()


def test_the_skills_probe_gets_the_experimental_tier(driver: ModuleType) -> None:
    assert driver._tier_of("test_skills_surface") == driver.EXPERIMENTAL_TIER


def test_a_skills_enforcement_probe_still_classifies_as_tier_1(
    driver: ModuleType,
) -> None:
    """The narrow fragment earns its narrowness: only the surface probe is Tier 3.

    ``_TIER_BY_FRAGMENT`` is scanned in insertion order and ``skills_surface`` sits
    first, so a broader ``skills`` fragment would swallow every later skills probe
    and quietly exempt real enforcement failures from the cell verdict."""
    assert driver._tier_of("test_skills_deny_path") == 1


def test_an_experimental_failure_does_not_condemn_the_cell(driver: ModuleType) -> None:
    """The point of the tier: churn in an ``@experimental`` upstream surface must
    not read as "this ADK version is unusable" while the deterministic seam passes."""
    tiers = {
        0: driver.TIER_PASS,
        1: driver.TIER_PASS,
        driver.EXPERIMENTAL_TIER: driver.TIER_FAIL,
    }

    assert driver.classify(tiers) == "OK"


def test_a_tier_0_failure_still_condemns_the_cell(driver: ModuleType) -> None:
    """The other half: the exemption is scoped to Tier 3 and nothing else."""
    assert driver.classify({0: driver.TIER_FAIL, 1: driver.TIER_PASS}) == "UNUSABLE"


def test_every_mapped_tier_has_a_bucket_in_parse_junit(driver: ModuleType) -> None:
    """``parse_junit`` used to hardcode its buckets, so adding a tier to the
    fragment map raised ``KeyError`` mid-run instead of reporting it."""
    cases = "".join(
        f'<testcase name="{name}"/>'
        for name in ("test_contract", "test_skills_surface")
    )
    path = Path(driver.JUNIT_DIR) / "tier-bucket-probe.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"<testsuite>{cases}</testsuite>")
    try:
        tiers, unclassified = driver.parse_junit(path)
    finally:
        path.unlink()

    assert not unclassified
    assert tiers[0] == driver.TIER_PASS
    assert tiers[driver.EXPERIMENTAL_TIER] == driver.TIER_PASS


def test_the_rendered_table_carries_the_experimental_column(driver: ModuleType) -> None:
    result = driver.CellResult(
        framework="google-adk",
        version="1.32.0",
        tiers={
            0: driver.TIER_PASS,
            1: driver.TIER_PASS,
            2: driver.TIER_SKIP,
            driver.EXPERIMENTAL_TIER: driver.TIER_FAIL,
        },
        status="OK",
    )

    table = driver.render_table([result])

    assert "| T3 |" in table
    assert "| google-adk | 1.32.0 | ✓ | ✓ | – | ✗ | OK |" in table
