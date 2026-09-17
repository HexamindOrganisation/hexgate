"""Turn the report's HTML into a PDF with WeasyPrint.

WeasyPrint is an in-process library, not a subprocess, but it is CPU-bound
for the length of a render, so the same two constraints the tectonic driver
had still apply and for the same reasons:

*Off the event loop.* A render that ran inline would stall every other
in-flight request on this worker for its whole duration.

*Out of the shared executor.* ``asyncio.to_thread`` dispatches to the loop's
default pool, which every other blocking call in this API shares — the whole
audit dashboard, the LLM summary, OPA bundle compiles — and which is only
``min(32, cpu+4)`` threads wide. Something that holds a thread for seconds
does not belong in there: a few concurrent report downloads would queue the
dashboard behind them, quietly, with ``/health`` still green. Renders get
their own small pool instead, so a burst of them waits on itself and nothing
else in the API notices.

What this does *not* give us, and the subprocess renderer it replaces did: a
render that has started cannot be stopped. A Python thread running library
code is uninterruptible, so the timeout below bounds how long the *caller*
waits, not how long the work runs. The blast radius of that is the pool — two
threads — and it is bounded on the other side too, because the assembler caps
every sample the annex carries, so there is no annex size a project can reach
that turns one render into minutes of layout.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from weasyprint import CSS, HTML

from hexgate_api.features.ai_act.render.html import STYLESHEET, build_html

_log = logging.getLogger(__name__)

# PDF/A-2b. "A" is the archival profile — fonts embedded, no external
# resources, no encryption, XMP metadata — which is what a document meant to
# be readable years after the events it records needs. "2" rather than "1"
# for the modern feature set, and rather than "3": 3's one addition over 2 is
# that it permits an arbitrary embedded file, and this document embeds none.
# (Attaching the signed annex would be the reason to move to 3, and is the
# only reason to.) "b" is basic conformance; "a" additionally requires a full
# tagged structure tree, which this document does not carry.
PDF_VARIANT = "pdf/a-2b"

# A render of this document is well under a second. The cap is what the caller
# waits before being told the render failed, rather than being left on a
# connection the box proxy will drop at 60s anyway. Keeping it under that
# proxy timeout is the point: the caller gets a 502 it can act on instead of a
# 504 it cannot.
RENDER_TIMEOUT_SECONDS = 45

_render_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ai-act-render")


class PdfRenderError(RuntimeError):
    """A render that produced no PDF. ``detail`` is the renderer's own message.

    The annex is stored and signed before any render happens, so a failure
    here loses nothing and the request is retryable.
    """

    def __init__(self, message: str, *, detail: str) -> None:
        super().__init__(message)
        self.detail = detail


def render_pdf(html: str) -> bytes:
    """Compile the report's HTML to PDF bytes.

    ``base_url=None``: the document references no external resource, and
    leaving the base URL unset means a stray ``src`` or ``@import`` that ever
    appeared in the template could not be resolved into a network fetch.

    The bytes are deterministic. WeasyPrint writes ``/CreationDate`` only from
    the document's own ``dcterms.created`` metadata, which this template does
    not set, so it stamps no clock of its own and two renders of one annex are
    byte-identical.
    """
    try:
        document = HTML(string=html, base_url=None)
        return document.write_pdf(
            stylesheets=[CSS(filename=str(STYLESHEET))],
            pdf_variant=PDF_VARIANT,
        )
    except Exception as exc:  # WeasyPrint raises a variety of library errors
        _log.error("ai-act report render failed: %s", exc, exc_info=True)
        raise PdfRenderError(
            "the report could not be rendered", detail=f"{type(exc).__name__}: {exc}"
        ) from exc


def render_document(annex: dict) -> bytes:
    """One stored annex, all the way to PDF bytes.

    Both stages sit inside the guard: a template that raises is as much a
    failed render as a layout that does, and the route documents one answer
    for a failed render. Building the HTML outside it would reach the caller
    as a bare 500 instead.
    """
    try:
        html = build_html(annex)
    except Exception as exc:
        _log.error("ai-act report HTML build failed: %s", exc, exc_info=True)
        raise PdfRenderError(
            "the report could not be rendered", detail=f"{type(exc).__name__}: {exc}"
        ) from exc
    return render_pdf(html)


async def render_pdf_async(html: str) -> bytes:
    """:func:`render_pdf` on the render pool, off the event loop."""
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(_render_pool, render_pdf, html),
            timeout=RENDER_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        raise PdfRenderError(
            "the report render timed out",
            detail=f"no PDF within {RENDER_TIMEOUT_SECONDS}s",
        ) from exc


async def render_document_async(annex: dict) -> bytes:
    """:func:`render_document` on the render pool, off the event loop."""
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(_render_pool, render_document, annex),
            timeout=RENDER_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        raise PdfRenderError(
            "the report render timed out",
            detail=f"no PDF within {RENDER_TIMEOUT_SECONDS}s",
        ) from exc
