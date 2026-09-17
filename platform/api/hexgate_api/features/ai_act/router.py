"""AI Act evidence report endpoints — generate, list history, download annex.

All three gate on ``require_project_admin``, not plain org membership, because
the annex embeds up to ``BAN_ENFORCEMENT_LIMIT`` ban-enforcement rows — who was
blocked and the operator's free-text reason. Every other route serving that
data is admin-gated (``/audit/ban-enforcements`` and all of ``/bans``), and the
audit router says so in as many words, so serving it inside a document would
route around a deliberate boundary rather than inherit it. The report is an
operator artifact; the audit reads an org member already has stay where they
are.

Gating the endpoint rather than thinning the document for non-admins is the
deliberate half of that: dropping rows per caller would make the caller's role
part of what the signature covers, so one period would have two digests. One
document per period, fewer readers.

The annex download serves the exact signed bytes from the row, never a
re-serialization: a round-trip through a JSON encoder could reorder or reformat
and break the digest for no visible reason.
"""

from __future__ import annotations

import base64
import json
import logging

from clickhouse_connect.driver.exceptions import ClickHouseError
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.core.db import get_session
from hexgate_api.deps.clickhouse import _audit_unavailable, require_clickhouse
from hexgate_api.deps.project import require_project_admin
from hexgate_api.features.ai_act.service import (
    DEFAULT_HISTORY_LIMIT,
    MAX_HISTORY_LIMIT,
    InvalidPeriod,
    ReportNotFound,
    ReportSummary,
    annex_filename,
    emails_for_user_ids,
    generate_report,
    get_report,
    list_reports,
)
from hexgate_api.models import AiActReport, OrganizationMember, Project, User
from hexgate_api.schemas import (
    AiActReportCreate,
    AiActReportRead,
    AiActReportSummary,
)

_log = logging.getLogger(__name__)

router = APIRouter()


def _summary(
    report: AiActReport | ReportSummary, *, email: str | None
) -> AiActReportSummary:
    """Serialise a history row.

    Takes either the stored entity (the generate path already holds one) or the
    annex-free ``ReportSummary`` the history read returns. Every field it
    touches exists on both, and deliberately so: ``annex_bytes`` is read off
    the row rather than measured from ``annex_json``, so this cannot become the
    reason the list endpoint loads every blob again.
    """
    return AiActReportSummary(
        id=report.id,
        project_id=report.project_id,
        period_start=report.period_start,
        period_end=report.period_end,
        generated_at=report.generated_at,
        generated_by_user_id=report.generated_by_user_id,
        generated_by_email=email,
        annex_sha256=report.annex_sha256,
        annex_bytes=report.annex_bytes,
        annex_filename=annex_filename(report.id),
        signing_kid=report.signing_kid,
        signature_b64=base64.b64encode(report.signature).decode("ascii"),
    )


@router.post(
    "/projects/{project_id}/ai-act/report",
    response_model=AiActReportRead,
    status_code=201,
    tags=["ai_act"],
)
async def api_generate_ai_act_report(
    project_id: str,
    body: AiActReportCreate | None = None,
    membership: tuple[User, OrganizationMember] = Depends(require_project_admin),
    session: AsyncSession = Depends(get_session),
    clickhouse_client=Depends(require_clickhouse),
) -> AiActReportRead:
    """Generate, sign and store one report for the project over a period.

    Synchronous by design in v0 (see the service docstring). A body is
    optional: no body at all is the same request as an empty one, which means
    the full retention window.
    """
    # require_project_admin has already 404’d an unknown project, so this is a
    # fetch rather than a check — but it is still a fetch that can miss, and a
    # 404 beats an AttributeError deeper in the assembler.
    caller, _member = membership
    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    request = body or AiActReportCreate()
    try:
        report = await generate_report(
            session,
            clickhouse_client,
            project=project,
            requested_by=caller,
            period_start=request.period_start,
            period_end=request.period_end,
        )
    except InvalidPeriod as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ClickHouseError as exc:
        # Nothing is stored on this path until the annex is complete, so a
        # failed read leaves no partial report behind.
        _log.warning("ai act report generation failed reading events: %s", exc)
        raise _audit_unavailable() from exc

    summary = _summary(report, email=caller.email)
    return AiActReportRead(**summary.model_dump(), annex=json.loads(report.annex_json))


@router.get(
    "/projects/{project_id}/ai-act/reports",
    response_model=list[AiActReportSummary],
    dependencies=[Depends(require_project_admin)],
    tags=["ai_act"],
)
async def api_list_ai_act_reports(
    project_id: str,
    limit: int = DEFAULT_HISTORY_LIMIT,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
) -> list[AiActReportSummary]:
    """A page of past reports for the project, newest first."""
    # Clamped like the audit reads (features/audit/router.py), not passed
    # through: a negative LIMIT is an executor error on Postgres but is
    # ignored by SQLite, so an unclamped value 500s only in production and
    # looks fine in the dev suite.
    rows = await list_reports(
        session,
        project_id,
        limit=max(1, min(limit, MAX_HISTORY_LIMIT)),
        offset=max(0, offset),
    )
    # ``or ""`` because the actor FK nulls on account erasure; the helper
    # drops empty ids and the row then renders with no email, as intended.
    emails = await emails_for_user_ids(
        session, {r.generated_by_user_id or "" for r in rows}
    )
    return [_summary(r, email=emails.get(r.generated_by_user_id or "")) for r in rows]


@router.get(
    "/projects/{project_id}/ai-act/reports/{rpt_id}/annex",
    dependencies=[Depends(require_project_admin)],
    tags=["ai_act"],
)
async def api_download_ai_act_annex(
    project_id: str,
    rpt_id: str,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Download the exact signed annex bytes.

    The digest and signature ride in headers rather than the body: the body is
    what the signature covers, so it cannot carry them.
    """
    try:
        report = await get_report(session, project_id=project_id, report_id=rpt_id)
    except ReportNotFound as exc:
        raise HTTPException(status_code=404, detail="report not found") from exc
    return Response(
        content=report.annex_json.encode("utf-8"),
        media_type="application/json",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{annex_filename(report.id)}"'
            ),
            "X-Hexgate-Annex-Sha256": report.annex_sha256,
            "X-Hexgate-Signature": base64.b64encode(report.signature).decode("ascii"),
            "X-Hexgate-Signing-Kid": report.signing_kid,
        },
    )
