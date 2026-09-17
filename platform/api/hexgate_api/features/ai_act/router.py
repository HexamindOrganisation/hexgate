"""AI Act evidence report endpoints — generate, list history, download annex.

All three are dashboard reads/writes gated on org membership through the
project path parameter, like the audit summary endpoint. The annex download
serves the exact signed bytes from the row, never a re-serialization: a
round-trip through a JSON encoder could reorder or reformat and break the
digest for no visible reason.
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
from hexgate_api.deps.org import require_org_member
from hexgate_api.features.ai_act.service import (
    InvalidPeriod,
    ReportNotFound,
    annex_filename,
    emails_for_user_ids,
    generate_report,
    get_report,
    list_reports,
)
from hexgate_api.models import AiActReport, Project, User
from hexgate_api.schemas import (
    AiActReportCreate,
    AiActReportRead,
    AiActReportSummary,
)

_log = logging.getLogger(__name__)

router = APIRouter()


def _summary(report: AiActReport, *, email: str | None) -> AiActReportSummary:
    return AiActReportSummary(
        id=report.id,
        project_id=report.project_id,
        period_start=report.period_start,
        period_end=report.period_end,
        generated_at=report.generated_at,
        generated_by_user_id=report.generated_by_user_id,
        generated_by_email=email,
        annex_sha256=report.annex_sha256,
        annex_bytes=len(report.annex_json.encode("utf-8")),
        annex_filename=annex_filename(report),
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
    user: User = Depends(require_org_member),
    session: AsyncSession = Depends(get_session),
    clickhouse_client=Depends(require_clickhouse),
) -> AiActReportRead:
    """Generate, sign and store one report for the project over a period.

    Synchronous by design in v0 (see the service docstring). A body is
    optional: no body at all is the same request as an empty one, which means
    the full retention window.
    """
    # require_org_member has already 404'd an unknown project, so this is a
    # fetch rather than a check — but it is still a fetch that can miss, and a
    # 404 beats an AttributeError deeper in the assembler.
    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    request = body or AiActReportCreate()
    try:
        report = await generate_report(
            session,
            clickhouse_client,
            project=project,
            requested_by=user,
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

    summary = _summary(report, email=user.email)
    return AiActReportRead(**summary.model_dump(), annex=json.loads(report.annex_json))


@router.get(
    "/projects/{project_id}/ai-act/reports",
    response_model=list[AiActReportSummary],
    dependencies=[Depends(require_org_member)],
    tags=["ai_act"],
)
async def api_list_ai_act_reports(
    project_id: str, session: AsyncSession = Depends(get_session)
) -> list[AiActReportSummary]:
    """Past reports for the project, newest first."""
    rows = await list_reports(session, project_id)
    emails = await emails_for_user_ids(session, {r.generated_by_user_id for r in rows})
    return [_summary(r, email=emails.get(r.generated_by_user_id)) for r in rows]


@router.get(
    "/projects/{project_id}/ai-act/reports/{rpt_id}/annex",
    dependencies=[Depends(require_org_member)],
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
            "Content-Disposition": (f'attachment; filename="{annex_filename(report)}"'),
            "X-Hexgate-Annex-Sha256": report.annex_sha256,
            "X-Hexgate-Signature": base64.b64encode(report.signature).decode("ascii"),
            "X-Hexgate-Signing-Kid": report.signing_kid,
        },
    )
