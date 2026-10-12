"""ClickHouse dependency: resolve the client, mapping connect failures to 503."""

import logging
from collections.abc import Callable
from typing import Final

from clickhouse_connect.driver.client import Client
from clickhouse_connect.driver.exceptions import ClickHouseError
from fastapi import HTTPException

from hexgate_api.core.clickhouse import get_clickhouse

_log = logging.getLogger(__name__)

CLICKHOUSE_RETRY_AFTER_SECONDS: Final = "5"
AUDIT_UNAVAILABLE_DETAIL: Final = "audit log temporarily unavailable"


def clickhouse_unavailable(detail: str) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail=detail,
        headers={"Retry-After": CLICKHOUSE_RETRY_AFTER_SECONDS},
    )


def _audit_unavailable() -> HTTPException:
    return clickhouse_unavailable(AUDIT_UNAVAILABLE_DETAIL)


def require_clickhouse():
    """Resolve the ClickHouse client as a dependency, mapping connect failures
    to 503 — get_clickhouse() connects eagerly, so without this the raise
    escapes dependency resolution as an uncaught 500."""
    try:
        return get_clickhouse()
    except ClickHouseError as exc:
        _log.warning("ClickHouse unreachable resolving audit client: %s", exc)
        raise _audit_unavailable()


def clickhouse_getter() -> Callable[[], Client]:
    """The client factory itself, for a route that must connect lazily: after its
    own validation, and inside its own time budget."""
    return get_clickhouse
