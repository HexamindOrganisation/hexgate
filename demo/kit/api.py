"""A tiny client for the local platform API, signed in through /v1/demo-login.

/v1/demo-login only exists when the API runs with HEXGATE_DEMO=1, which only
`kit.platform` sets, on localhost.
"""

from __future__ import annotations

import http.cookiejar
import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from kit.paths import API


class ApiError(RuntimeError):
    pass


class Api:
    def __init__(self, base: str = API):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )
        self.call("GET", "/v1/demo-login", raw=True)

    def call(
        self, method: str, path: str, body=None, raw: bool = False, ok404: bool = False
    ):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("content-type", "application/json")
        try:
            with self.opener.open(req, timeout=30) as r:
                payload = r.read()
        except urllib.error.HTTPError as e:
            if ok404 and e.code == 404:
                return None
            detail = e.read().decode(errors="replace")[:400]
            raise ApiError(f"{method} {path} → {e.code}: {detail}") from None
        if raw or not payload:
            return payload
        return json.loads(payload)

    # ------------------------------------------------------------ projects

    def org_id(self) -> str:
        orgs = self.call("GET", "/v1/orgs")
        if not orgs:
            raise ApiError("no organization on this platform")
        return orgs[0]["id"]

    def ensure_project(self, name: str) -> str:
        org = self.org_id()
        for p in self.call("GET", f"/v1/orgs/{org}/projects"):
            if p["name"] == name:
                return p["id"]
        return self.call("POST", f"/v1/orgs/{org}/projects", {"name": name})["id"]

    def mint_key(self, project: str, name: str) -> str:
        return self.call(
            "POST", f"/v1/projects/{project}/tokens", {"name": name, "env": "live"}
        )["full"]

    # ------------------------------------------------------------ policy files

    def policy_files(self, project: str) -> list[str]:
        return [
            f["name"] for f in self.call("GET", f"/v1/projects/{project}/policy-files")
        ]

    def put_policy_file(self, project: str, name: str, content: str) -> None:
        path = urllib.parse.quote(name)
        self.call(
            "PUT", f"/v1/projects/{project}/policy-files/{path}", {"content": content}
        )

    def delete_policy_files(self, project: str) -> list[str]:
        # policy.yaml first: a file can't go while something imports it.
        names = sorted(self.policy_files(project), key=lambda n: n != "policy.yaml")
        for n in names:
            self.call(
                "DELETE",
                f"/v1/projects/{project}/policy-files/{urllib.parse.quote(n)}",
                ok404=True,
            )
        return names


def psql(sql: str) -> None:
    """Run SQL on the local Postgres container (there is no API to delete an agent)."""
    subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            "hexgate-postgres",
            "psql",
            "-q",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "hexgate",
            "-d",
            "hexgate",
        ],
        input=sql,
        text=True,
        check=True,
        capture_output=True,
    )


def clickhouse(query: str) -> None:
    subprocess.run(
        ["docker", "exec", "hexgate-clickhouse", "clickhouse-client", "-q", query],
        check=True,
        capture_output=True,
        text=True,
    )


def forget_agent(project: str, agent: str) -> None:
    """Delete an agent's registration and its audit rows (back to 'never registered')."""
    psql(f"""
BEGIN;
CREATE TEMP TABLE gone AS SELECT id FROM agent WHERE name = '{agent}' AND project_id = '{project}';
DELETE FROM tool  WHERE agent_version_id IN (SELECT id FROM agent_version WHERE agent_id IN (SELECT id FROM gone));
DELETE FROM skill WHERE agent_version_id IN (SELECT id FROM agent_version WHERE agent_id IN (SELECT id FROM gone));
DELETE FROM agent_version        WHERE agent_id IN (SELECT id FROM gone);
DELETE FROM agent_classification WHERE agent_id IN (SELECT id FROM gone);
DELETE FROM agent                WHERE id IN (SELECT id FROM gone);
COMMIT;
""")
    for table in (
        "policy_decision",
        "llm_message",
        "agent_run",
        "llm_invocation",
        "ban_enforcement",
        "usage_minute",
    ):
        try:
            clickhouse(
                f"ALTER TABLE hexgate_audit.{table} DELETE WHERE agent_name = '{agent}' "
                f"AND project_id = '{project}' SETTINGS mutations_sync = 1"
            )
        except subprocess.CalledProcessError:
            # Older schemas have no project_id column on some tables.
            clickhouse(
                f"ALTER TABLE hexgate_audit.{table} DELETE WHERE agent_name = '{agent}' SETTINGS mutations_sync = 1"
            )
