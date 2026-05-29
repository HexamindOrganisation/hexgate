"""ClickHouse client + reachability probe for the platform API.

A single process-wide ``Client`` is enough — clickhouse-connect manages
its own HTTP connection pool internally (via urllib3) and the resulting
client is thread-safe, so the FastAPI handlers share it via the
``get_clickhouse`` dependency rather than each constructing their own.
"""
from __future__ import annotations

import logging
from functools import lru_cache

import clickhouse_connect
from clickhouse_connect.driver.client import Client

from settings import get_settings

_log = logging.getLogger(__name__)


@lru_cache
def get_clickhouse() -> Client:
    """Return the process-wide ClickHouse client, configured from settings."""
    s = get_settings()
    return clickhouse_connect.get_client(
        host=s.clickhouse_host,
        port=s.clickhouse_port,
        username=s.clickhouse_user,
        password=s.clickhouse_password,
        database=s.clickhouse_database,
        secure=s.clickhouse_secure,
        compress=True,
        connect_timeout=5,
        send_receive_timeout=30,
    )


def ping() -> bool:
    """Cheap reachability probe used by the /health endpoint.

    Returns False on any failure (connection refused, timeout, auth) —
    callers only care about reachable-vs-not, not the reason.
    """
    try:
        return bool(get_clickhouse().ping())
    except Exception as exc:
        _log.warning("ClickHouse ping failed: %s", exc)
        return False
