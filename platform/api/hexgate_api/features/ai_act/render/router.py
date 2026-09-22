"""``GET /v1/projects/{id}/ai-act/reports/{rpt_id}.pdf`` — render a stored report.

The PDF is a rendering of the annex, not a second artifact: the signature
covers the annex bytes, and this route reads nothing else.

It gates on ``require_project_admin`` for the same reason the three routes in
the sibling router do, and it has to: the document prints section 3.4 from the
annex's ban-enforcement rows — who was blocked and the operator's free-text
reason — so a laxer gate here would serve past a boundary the JSON route
draws, and ``.pdf`` would be the way around it.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.core.db import get_session
from hexgate_api.deps.project import require_project_admin
from hexgate_api.features.ai_act.render import PdfRenderError, render_annex_pdf_async
from hexgate_api.features.ai_act.service import ReportNotFound, get_report

router = APIRouter()


@router.get(
    "/projects/{project_id}/ai-act/reports/{rpt_id}.pdf",
    dependencies=[Depends(require_project_admin)],
    response_class=Response,
    tags=["ai_act"],
)
async def api_report_pdf(
    project_id: str,
    rpt_id: str,
    session: AsyncSession = Depends(get_session),
) -> Response:
    try:
        report = await get_report(session, project_id=project_id, report_id=rpt_id)
    except ReportNotFound as exc:
        raise HTTPException(status_code=404, detail="report not found") from exc

    try:
        pdf = await render_annex_pdf_async(json.loads(report.annex_json))
    except PdfRenderError as exc:
        # 502, not 500: the annex is stored and signed already, so the report
        # itself is intact and the render is retryable. The renderer's own
        # message goes back with it — without it a render failure is
        # undiagnosable from the outside.
        raise HTTPException(
            status_code=502,
            detail={"error": str(exc), "renderer": exc.detail},
        ) from exc

    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            # The stored row's id, not the path parameter: a report id that
            # matched nothing has already 404'd, so what reaches the header is
            # always a minted id rather than whatever the caller typed.
            "Content-Disposition": f'attachment; filename="{report.id}.pdf"',
        },
    )
