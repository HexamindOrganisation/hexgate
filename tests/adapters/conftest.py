"""Shared fixture for the (opt-in) adapter integration test suite — one
live platform + ingest pipeline, common to all four frameworks.

Two distinct hops, easy to conflate: policy is fetched over HTTP from the
platform API (HEXGATE_API_URL), while audit/usage spans are exported over
OTLP to the Collector (HEXGATE_OTLP_ENDPOINT) and reach ClickHouse only
via Redpanda and the span-enricher job. Both must be up; see
.claude/skills/integration-tests.

Centralizes what would otherwise be copy-pasted per adapter: reading
HEXGATE_API_KEY/HEXGATE_API_URL/ClickHouse creds, pinning the OTLP
endpoint, skipping cleanly when no key is configured, and querying the
audit tables.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

import httpx
import pytest

from hexgate.cloud.client import _parse_project_from_key


@dataclass(frozen=True)
class HexgatePlatformEnv:
    api_key: str
    platform_url: str
    clickhouse_url: str
    clickhouse_user: str
    clickhouse_password: str
    # Dashboard login for the cookie-authed read endpoints. Empty when the
    # HEXGATE_SMOKE_* pair is unset; only the endpoint-reading tests need it,
    # and they skip rather than fail (see ``dashboard_login``).
    email: str = ""
    password: str = ""
    # The project the api_key was minted for — the path segment every
    # project-scoped read endpoint takes. Carried on the envelope of the key
    # itself, so the tests need no second source of truth for it.
    project_id: str = ""

    def clickhouse_query(self, query: str, **params: str) -> str:
        """Parameterized query via ClickHouse's HTTP interface — never
        string-interpolate test-controlled values into SQL."""
        response = httpx.get(
            self.clickhouse_url,
            params={"query": query, **{f"param_{k}": v for k, v in params.items()}},
            auth=(self.clickhouse_user, self.clickhouse_password),
            timeout=5,
        )
        response.raise_for_status()
        return response.text.strip()

    def policy_decision_outcome(
        self, agent_name: str, session_id: str, tool_name: str
    ) -> str | None:
        """The most recent policy_decision outcome for this run, or None
        while the row hasn't landed yet."""
        text = self.clickhouse_query(
            "SELECT outcome FROM hexgate_audit.policy_decision "
            "WHERE agent_name = {agent_name:String} "
            "AND session_id = {session_id:String} "
            "AND tool_name = {tool_name:String} "
            "ORDER BY occurred_at DESC LIMIT 1",
            agent_name=agent_name,
            session_id=session_id,
            tool_name=tool_name,
        )
        return text or None

    def llm_invocation_count(self, agent_name: str, session_id: str) -> int:
        text = self.clickhouse_query(
            "SELECT count() FROM hexgate_audit.llm_invocation "
            "WHERE agent_name = {agent_name:String} "
            "AND session_id = {session_id:String}",
            agent_name=agent_name,
            session_id=session_id,
        )
        return int(text)

    def llm_message_rows(self, agent_name: str, session_id: str) -> list[dict]:
        """Every llm_message row of this run, in transcript order.

        Ordered as the endpoint orders it, minus the ``event_id`` tiebreak
        that only matters for rows sharing a timestamp and a seq:
        ``message_seq`` counts inside one ``turn_key`` and restarts for an
        agent reached by a handoff, so time is what orders across lists.
        JSONEachRow because a row carries embedded JSON in three columns —
        TSV would need un-escaping by hand.
        """
        text = self.clickhouse_query(
            "SELECT turn_key, message_seq, resynced, truncated, model, "
            "input_messages, output_messages, system_instructions "
            "FROM hexgate_audit.llm_message "
            "WHERE agent_name = {agent_name:String} "
            "AND session_id = {session_id:String} "
            "ORDER BY occurred_at, message_seq FORMAT JSONEachRow",
            agent_name=agent_name,
            session_id=session_id,
        )
        return [json.loads(line) for line in text.splitlines() if line]

    def llm_messages_via_api(
        self, *, session_id: str = "", run_id: str | None = None
    ) -> list[dict]:
        """One transcript read back through the product's own endpoint.

        Cookie-authed, not key-authed: this is a dashboard read
        (``require_org_member``), so it needs a login rather than the
        ``fty_live_…`` the SDK exports with. Same credentials
        ``platform/scripts/otlp_smoke.py`` reads, so one pair serves both —
        and :func:`require_dashboard_login` skips the test when they are
        absent.

        ``session_id`` is always sent, blank included: it is the second
        column of the storage sort key, so pinning it lets the scan stop at
        ``limit + offset`` rows instead of reading every session in the
        project. The endpoint documents that convention on the parameter.
        """
        with httpx.Client(base_url=self.platform_url, timeout=15) as client:
            login = client.post(
                "/v1/auth/cookie/login",
                data={"username": self.email, "password": self.password},
            )
            login.raise_for_status()
            params: dict[str, str] = {"session_id": session_id}
            if run_id is not None:
                params["run_id"] = run_id
            response = client.get(
                f"/v1/projects/{self.project_id}/audit/llm-messages", params=params
            )
            response.raise_for_status()
            return response.json()["rows"]


@pytest.fixture
def hexgate_platform_env(monkeypatch: pytest.MonkeyPatch) -> HexgatePlatformEnv:
    """Skip cleanly if HEXGATE_API_KEY isn't set; otherwise pin
    HEXGATE_API_KEY/HEXGATE_API_URL in os.environ for this test only —
    monkeypatch reverts them afterward, so a value set here can't leak
    into another test the way a bare `os.environ[...] = ...` could — and
    hand back a small client for polling the audit tables.
    """
    api_key = os.environ.get("HEXGATE_API_KEY")
    if not api_key:
        pytest.skip("HEXGATE_API_KEY not set; mint a token via the dashboard")
    platform_url = os.environ.get("HEXGATE_API_URL", "http://localhost:8000").rstrip(
        "/"
    )
    monkeypatch.setenv("HEXGATE_API_KEY", api_key)
    monkeypatch.setenv("HEXGATE_API_URL", platform_url)
    # Spans go to the Collector's OTLP/HTTP receiver, NOT to the control
    # plane. Leaving this unset lets resolve_otlp_endpoint() fall back to
    # <api url>/v1/traces, and the platform API serves no such route — every
    # export then dies with a 405 the SDK only logs, surfacing here as a
    # bare "row never landed in ClickHouse".
    monkeypatch.setenv(
        "HEXGATE_OTLP_ENDPOINT",
        os.environ.get("HEXGATE_OTLP_ENDPOINT", "http://localhost:4318/v1/traces"),
    )
    # Upstream's default is 5s, which would eat most of poll_until's budget
    # before the Collector's own 5s batch timeout even starts. A documented
    # OTel env var, read when the sender builds its BatchSpanProcessor on
    # first emit — this fixture runs first, so the sender picks it up.
    monkeypatch.setenv("OTEL_BSP_SCHEDULE_DELAY", "500")
    return HexgatePlatformEnv(
        api_key=api_key,
        platform_url=platform_url,
        clickhouse_url=os.environ.get(
            "HEXGATE_CLICKHOUSE_URL", "http://localhost:8124"
        ),
        clickhouse_user=os.environ.get("HEXGATE_CLICKHOUSE_USER", "hexgate"),
        clickhouse_password=os.environ.get(
            "HEXGATE_CLICKHOUSE_PASSWORD", "hexgate-dev-password"
        ),
        # Same names platform/scripts/otlp_smoke.py reads, so one dashboard
        # login serves the smoke run and the endpoint-reading tests here.
        email=os.environ.get("HEXGATE_SMOKE_EMAIL", ""),
        password=os.environ.get("HEXGATE_SMOKE_PASSWORD", ""),
        project_id=_parse_project_from_key(api_key) or "",
    )
