"""Every copy of the OPA pin agrees with the platform API image's.

The API image's OPA compiles and signs production policy bundles, and CI runs
the SDK and platform tests against its own OPA install, so the two must be the
same binary. Dependabot cannot bump an ``ADD <url>`` or a ``curl`` line, so
each OPA bump edits every copy by hand: the API Dockerfile is the source of
truth, and this fails on any copy left behind.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# `ADD --checksum=sha256:<sha> \` then the download URL on the next line.
_DOCKERFILE_PIN = re.compile(
    r"ADD --checksum=sha256:(?P<sha>[0-9a-f]{64}) \\\n\s+"
    r"https://openpolicyagent\.org/downloads/(?P<version>v[0-9.]+)/opa_linux_(?P<arch>amd64|arm64)_static"
)
_WORKFLOW_URL = re.compile(r"releases/download/(v[0-9.]+)/opa_linux_amd64_static")
_WORKFLOW_SHA = re.compile(r'echo "([0-9a-f]{64})  opa" \| sha256sum -c -')


def _dockerfile_pins(path: str) -> dict[str, tuple[str, str]]:
    text = (ROOT / path).read_text()
    return {m["arch"]: (m["version"], m["sha"]) for m in _DOCKERFILE_PIN.finditer(text)}


def test_api_image_pins_opa_for_both_architectures() -> None:
    assert set(_dockerfile_pins("platform/api/Dockerfile")) == {"amd64", "arm64"}


def test_evals_sandbox_pins_the_same_opa_as_the_api_image() -> None:
    assert _dockerfile_pins("evals/policy_writing/Dockerfile") == _dockerfile_pins(
        "platform/api/Dockerfile"
    )


def test_ci_installs_and_verifies_the_same_opa_as_the_api_image() -> None:
    version, sha = _dockerfile_pins("platform/api/Dockerfile")["amd64"]
    for workflow in (".github/workflows/tests.yml", ".github/workflows/release.yml"):
        text = (ROOT / workflow).read_text()
        versions = _WORKFLOW_URL.findall(text)
        shas = _WORKFLOW_SHA.findall(text)
        assert versions, f"{workflow}: no OPA download found"
        # Every download is followed by its own checksum check.
        assert len(shas) == len(versions), (
            f"{workflow}: an OPA download is not checksummed"
        )
        assert set(versions) == {version}, workflow
        assert set(shas) == {sha}, workflow
