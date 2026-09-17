"""PDF rendering of a stored AI Act evidence annex.

The annex is the signed artifact; this package is downstream of it and reads
nothing else, so a re-render of an old report reproduces the document it was
first downloaded as.
"""

from hexgate_api.features.ai_act.render.html import build_html
from hexgate_api.features.ai_act.render.pdf import (
    PDF_VARIANT,
    PdfRenderError,
    render_document,
    render_document_async,
    render_pdf,
    render_pdf_async,
)


def render_annex_pdf(annex: dict) -> bytes:
    """Render one stored annex to PDF bytes."""
    return render_document(annex)


async def render_annex_pdf_async(annex: dict) -> bytes:
    """:func:`render_annex_pdf` for a request handler: both stages run on the
    render pool, so neither holds the event loop nor a shared thread."""
    return await render_document_async(annex)


__all__ = [
    "PDF_VARIANT",
    "PdfRenderError",
    "build_html",
    "render_document",
    "render_document_async",
    "render_annex_pdf",
    "render_annex_pdf_async",
    "render_pdf",
    "render_pdf_async",
]
