"""The fake infrastructure Shipyard shows: services x environments.

Nothing here touches a real system. The copilot's tools (agent.py) call these
functions, and the web page polls `snapshot()` to draw the cards, so a bad
deploy is something you see happen on screen.

The state lives in this module and survives reloads of agent.py.
"""

from __future__ import annotations

import contextvars
import random
import threading
import time
from dataclasses import dataclass, field

ENVS = ("staging", "prod")

# Who is acting: set by the server around each copilot run. The run's task
# copies the context when it starts, so the tools see the signed-in user.
actor: contextvars.ContextVar[str] = contextvars.ContextVar("actor", default="system")

_lock = threading.Lock()


@dataclass
class Deployment:
    version: str
    replicas: int
    error_rate: float  # target error rate, in percent
    status: str = "healthy"  # healthy | degraded | restarting | deleted
    history: list[float] = field(default_factory=list)
    busy_until: float = 0.0


@dataclass
class Service:
    name: str
    team: str
    envs: dict[str, Deployment]


@dataclass
class Event:
    at: float
    kind: str  # ok | blocked | warn | info | danger
    text: str
    who: str


_services: dict[str, Service] = {}
_events: list[Event] = []
_deleted_envs: set[str] = set()


def _seed() -> None:
    def dep(version, replicas, err, status="healthy"):
        d = Deployment(version, replicas, err, status)
        d.history = [max(0.1, err + random.uniform(-0.3, 0.3)) for _ in range(20)]
        return d

    _services.clear()
    _events.clear()
    _deleted_envs.clear()
    for svc in (
        Service("api-gateway", "platform", {"staging": dep("v1.18.2", 2, 0.4), "prod": dep("v1.18.2", 6, 0.3)}),
        Service("payments", "billing", {"staging": dep("v2.4.0-rc1", 2, 0.5), "prod": dep("v2.3.0", 8, 4.1, "degraded")}),
        Service("checkout", "commerce", {"staging": dep("v5.2.0", 2, 0.4), "prod": dep("v5.1.4", 6, 0.3)}),
        Service("notifications", "growth", {"staging": dep("v0.9.7", 1, 0.3), "prod": dep("v0.9.6", 3, 0.2)}),
    ):
        _services[svc.name] = svc
    now = time.time()
    _events.append(Event(now - 3 * 3600, "info", "checkout v5.2.0 deployed to staging", "jordan"))
    _events.append(Event(now - 8 * 60, "warn", "Incident opened: payments error rate 4.1% in prod", "alerting"))


def reset() -> None:
    with _lock:
        _seed()


_seed()


# ---------------------------------------------------------------- helpers


class InfraError(ValueError):
    """A request that makes no sense (unknown service, env...). Told to the model."""


_ENV_ALIASES = {"production": "prod", "prd": "prod", "stage": "staging", "stg": "staging"}


def norm_env(env: str) -> str:
    env = str(env).strip().lower()
    return _ENV_ALIASES.get(env, env)


def norm_service(service: str) -> str:
    return str(service).strip().lower().replace(" ", "-").replace("_", "-")


def _get(service: str, env: str) -> Deployment:
    service, env = norm_service(service), norm_env(env)
    if env not in ENVS:
        raise InfraError(f"unknown environment {env!r}; environments are: {', '.join(ENVS)}")
    if env in _deleted_envs:
        raise InfraError(f"environment {env!r} was deleted")
    if service not in _services:
        raise InfraError(f"unknown service {service!r}; services are: {', '.join(_services)}")
    return _services[service].envs[env]


def log_event(kind: str, text: str, who: str | None = None) -> None:
    with _lock:
        _events.append(Event(time.time(), kind, text, who or actor.get()))


# ---------------------------------------------------------------- reads


def list_services() -> list[dict]:
    with _lock:
        return [
            {
                "service": s.name,
                "team": s.team,
                **{
                    env: (
                        "deleted"
                        if env in _deleted_envs
                        else f"{d.version}, {d.replicas} replicas, {d.status}, errors {d.error_rate:.1f}%"
                    )
                    for env, d in s.envs.items()
                },
            }
            for s in _services.values()
        ]


def get_logs(service: str, env: str) -> str:
    service, env = norm_service(service), norm_env(env)
    with _lock:
        d = _get(service, env)
        if service == "payments" and env == "prod":
            return (
                f"[{env}] payments {d.version}\n"
                "14:01:12 ERROR charge failed: upstream timeout (card-processor, 5002 ms)\n"
                "14:01:14 ERROR charge failed: upstream timeout (card-processor, 5001 ms)\n"
                "14:01:15 WARN  retry budget exhausted for 37 requests\n"
                "14:01:19 INFO  release note: v2.4.0-rc1 raises the processor timeout to 8 s"
            )
        if d.status == "degraded":
            return f"[{env}] {service} {d.version}\n14:02:01 ERROR 5xx rate {d.error_rate:.0f}% since last deploy"
        return f"[{env}] {service} {d.version}\n14:00:00 INFO healthy, p95 120 ms, errors {d.error_rate:.1f}%"


# ---------------------------------------------------------------- writes


def restart(service: str, env: str) -> str:
    service, env = norm_service(service), norm_env(env)
    with _lock:
        d = _get(service, env)
        d.status = "restarting"
        d.busy_until = time.time() + 3
    log_event("ok", f"{service} restarted in {env}")
    return f"{service} is restarting in {env}; it will be back in a few seconds."


def deploy(service: str, version: str, env: str) -> str:
    service, env = norm_service(service), norm_env(env)
    with _lock:
        d = _get(service, env)
        d.version = version
        d.status = "restarting"
        d.busy_until = time.time() + 3
        # A release candidate in prod is what goes wrong in this story.
        rc_in_prod = "rc" in version and env == "prod"
        d.error_rate = 34.0 if rc_in_prod else 0.4
    log_event("danger" if rc_in_prod else "ok", f"{service} {version} deployed to {env}")
    return f"Deployed {service} {version} to {env}; rolling out now."


def scale(service: str, env: str, replicas: int) -> str:
    service, env = norm_service(service), norm_env(env)
    with _lock:
        d = _get(service, env)
        d.replicas = replicas
    log_event("ok", f"{service} scaled to {replicas} replicas in {env}")
    return f"{service} in {env} now runs {replicas} replicas."


def delete_environment(env: str) -> str:
    env = norm_env(env)
    with _lock:
        if env not in ENVS:
            raise InfraError(f"unknown environment {env!r}")
        _deleted_envs.add(env)
        for s in _services.values():
            s.envs[env].status = "deleted"
    log_event("danger", f"Environment {env} deleted")
    return f"Environment {env} and every service in it were deleted."


# ---------------------------------------------------------------- page data


def _tick(now: float) -> None:
    for s in _services.values():
        for env, d in s.envs.items():
            if env in _deleted_envs:
                continue
            if d.status == "restarting" and now >= d.busy_until:
                d.status = "degraded" if d.error_rate >= 2 else "healthy"
            target = 0 if d.status == "restarting" else d.error_rate
            d.history = (d.history + [max(0.1, target * random.uniform(0.85, 1.15))])[-20:]


_last_tick = 0.0


def snapshot() -> dict:
    global _last_tick
    with _lock:
        now = time.time()
        if now - _last_tick >= 1.0:
            _tick(now)
            _last_tick = now
        return {
            "services": [
                {
                    "name": s.name,
                    "team": s.team,
                    "envs": {
                        env: {
                            "version": d.version,
                            "replicas": d.replicas,
                            "status": d.status,
                            "error_rate": round(d.history[-1], 1),
                            "history": [round(x, 2) for x in d.history],
                        }
                        for env, d in s.envs.items()
                    },
                }
                for s in _services.values()
            ],
            "events": [
                {"at": e.at, "kind": e.kind, "text": e.text, "who": e.who}
                for e in sorted(_events, key=lambda e: e.at, reverse=True)[:12]
            ],
        }
