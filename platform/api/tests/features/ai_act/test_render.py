"""Annex -> HTML -> PDF.

Two levels, deliberately. The HTML tests are cheap and say what the document
contains; the PDF tests are slower and say how it is set — that a value fits
its column, that a table's header repeats where it breaks, that the running
footer is on every page. A layout defect is invisible to an assertion about
HTML, so the pages are read back out of the PDF.

There is no escaping suite here. The LaTeX renderer this replaced needed one,
because it had a hand-written escaper that every call site had to remember to
use; Jinja autoescaping applies at interpolation and has no call sites to
forget. What survives is the one test that matters either way: an annex whose
every string leaf is hostile still renders, and reads back as itself.
"""

from __future__ import annotations

import re
from html import unescape

import pytest
from pypdf import PdfReader

from hexgate_api.features.ai_act.render import (
    PDF_VARIANT,
    PdfRenderError,
    build_html,
    render_annex_pdf,
    render_pdf,
)
from hexgate_api.features.ai_act.render.html import (
    EM_DASH,
    _dt,
    _label,
    _strip_index,
)
from tests.features.ai_act.annex_fixture import sample_annex

# HTML metacharacters, plus the TeX ones the previous renderer had to escape —
# kept in the corpus because an operator's text does not know which renderer is
# downstream of it, and a future engine change must not quietly start eating
# them again.
HOSTILE = "x<script>&\"'\\{}$%_~^[]#y"


def pdf_pages(pdf: bytes, tmp_path) -> list[str]:
    path = tmp_path / "out.pdf"
    path.write_bytes(pdf)
    return [page.extract_text() for page in PdfReader(path).pages]


def pdf_text(pdf: bytes, tmp_path) -> str:
    """The whole document as one string, with wrapping undone.

    Line breaks are the renderer's decision, not the annex's: a value that was
    wrapped inside its column is still that value.
    """
    return re.sub(r"\s+", " ", " ".join(pdf_pages(pdf, tmp_path)))


def hostilise(value):
    """Replace every string leaf of an annex with metacharacter-laden text.

    Walking the structure rather than listing fields is deliberate: a field
    added to the annex later is covered without anyone remembering to add it
    here.
    """
    if isinstance(value, dict):
        return {key: hostilise(item) for key, item in value.items()}
    if isinstance(value, list):
        return [hostilise(item) for item in value]
    if isinstance(value, str):
        return HOSTILE
    return value


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------


def test_build_html_happy_path() -> None:
    html = build_html(sample_annex())

    assert html.startswith("<!doctype html>")
    # The five sections, each numbered and carrying the articles the annex
    # says it answers to.
    for number, name in (
        (1, "AI system inventory"),
        (2, "Controls in place"),
        (3, "Activity record"),
        (4, "Coverage statement"),
        (5, "Signature and verification"),
    ):
        assert f'<span class="n">{number}</span>{name}' in html
    assert "Art. 26(12)" in html
    # The scope disclaimer is the one thing that must survive into every
    # generated document.
    assert "not a statutory filing" in html.lower()


def test_when_a_value_carries_markup_then_it_is_escaped_not_interpreted() -> None:
    html = build_html(hostilise(sample_annex()))

    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_when_an_annex_key_is_absent_then_the_render_fails_loudly() -> None:
    annex = sample_annex()
    del annex["coverage"]["gaps"]

    with pytest.raises(Exception, match="gaps"):
        build_html(annex)


def test_when_a_field_is_unset_then_it_reads_as_an_em_dash() -> None:
    html = build_html(sample_annex())

    # triage_bot's entry is the golden file's incomplete one.
    assert EM_DASH in html
    assert ">None<" not in html


def test_when_an_inventory_entry_is_incomplete_then_its_matrix_is_annex_only() -> None:
    html = build_html(sample_annex())

    assert "carried in the signed annex only" in html
    # The complete agent's matrix is still there.
    assert "read_file" in html


def test_when_a_sample_is_empty_then_the_table_says_so() -> None:
    annex = sample_annex()
    annex["activity"]["ban_enforcements"]["rows"] = []

    assert "No ban was enforced in this period." in build_html(annex)


def test_when_a_bundle_hash_is_absent_then_it_reads_as_an_em_dash() -> None:
    """A bundle stored before its WASM compile succeeded carries one hash and
    not the other, and the golden file has both — so the null case only shows
    up if a test makes it."""
    annex = sample_annex()
    annex["inventory"]["agents"][0]["bundle"]["wasm_hash"] = None

    html = build_html(annex)

    assert ">None<" not in html
    assert EM_DASH in html


def test_when_a_codepoint_has_no_glyph_then_it_is_spelled_out() -> None:
    """Pango draws a missing glyph as .notdef and says nothing, so a report
    would otherwise lose text the signed annex holds."""
    annex = sample_annex()
    annex["activity"]["decision_sample"]["rows"][0]["arguments"] = {
        "note": "\u4e2d\u6587"
    }

    html = build_html(annex)

    assert "<U+4E2D>" in html
    assert "\u4e2d" not in html
    # The document's own punctuation is not collateral damage.
    assert EM_DASH in html


def test_every_role_header_can_wrap_inside_its_column() -> None:
    """A role name with no space in it and a fixed table layout is how a
    header comes to print over the next column, with no warning anywhere."""
    annex = sample_annex()
    matrix = annex["controls"]["agents"][0]["authorisation_matrix"]
    long_role = "customer_support_tier_two"
    matrix["roles"] = [long_role]
    for row in matrix["rows"]:
        row["cells"] = {long_role: next(iter(row["cells"].values()))}

    html = build_html(annex)

    assert f'<th class="brk">{long_role}</th>' in html


def test_the_denials_counter_does_not_claim_to_exclude_guard_refusals() -> None:
    """A guard refusal is a DENY row, and is counted again in its own tile. A
    label that said "by policy" would invite a reader to add the two."""
    from hexgate_api.features.ai_act.render.html import COUNTER_LABELS

    labels = dict(COUNTER_LABELS)

    assert labels["denials"] == "Denied (policy or guard)"
    assert "policy" not in labels["denials"] or "guard" in labels["denials"]


def test_when_the_template_fails_then_the_route_still_gets_a_render_error() -> None:
    """A template failure is a failed render, and the route documents one
    answer for that. Outside the guard it would arrive as a bare 500."""
    from hexgate_api.features.ai_act.render import render_document

    annex = sample_annex()
    del annex["activity"]["ban_enforcements"]["total"]

    with pytest.raises(PdfRenderError) as excinfo:
        render_document(annex)

    assert "total" in excinfo.value.detail


def test_html_references_no_external_resource() -> None:
    """A render that reached the network could hang, and a document that
    depended on what a CDN served that day would not be reproducible."""
    html = build_html(sample_annex())

    assert "http://" not in html
    assert "https://" not in html.replace('lang="en"', "")
    assert "<img" not in html
    assert "@import" not in html


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


def test_render_annex_pdf_happy_path(tmp_path) -> None:
    pdf = render_annex_pdf(sample_annex())

    assert pdf.startswith(b"%PDF-")
    text = pdf_text(pdf, tmp_path)
    assert "AI Act compliance evidence report" in text
    assert "support_bot" in text


def test_pdf_declares_the_archival_variant(tmp_path) -> None:
    """PDF/A is what makes this readable years after the events it records:
    fonts embedded, nothing external, no encryption."""
    assert PDF_VARIANT == "pdf/a-2b"

    path = tmp_path / "out.pdf"
    path.write_bytes(render_annex_pdf(sample_annex()))
    reader = PdfReader(path)

    assert reader.xmp_metadata is not None
    # Every font the document uses travels with it.
    fonts = [
        font
        for page in reader.pages
        for font in (page.get("/Resources", {}).get("/Font", {}) or {}).values()
    ]
    assert fonts


def test_when_every_annex_string_is_hostile_then_it_round_trips(tmp_path) -> None:
    text = pdf_text(render_annex_pdf(hostilise(sample_annex())), tmp_path)

    assert HOSTILE in text
    # Not once, somewhere: every cell in the document is that string, so a
    # single unescaped interpolation would show up as a missing or mangled one.
    assert text.count(HOSTILE) > 20


def test_the_running_footer_is_on_every_page_after_the_cover(tmp_path) -> None:
    """The report id and the disclaimer have to be on any page an auditor
    photocopies out of the document, not only on the first."""
    pages = pdf_pages(render_annex_pdf(sample_annex()), tmp_path)

    assert len(pages) > 1
    for number, page in enumerate(pages[1:], start=2):
        flat = re.sub(r"\s+", " ", page)
        assert "rpt_000000000000" in flat
        assert "Not a statutory filing" in flat
        assert f"{number} / {len(pages)}" in flat


def test_a_table_that_breaks_across_pages_repeats_its_header(tmp_path) -> None:
    annex = sample_annex()
    # One long decision sample, so section 3.2 has to break.
    row = annex["activity"]["decision_sample"]["rows"][0]
    annex["activity"]["decision_sample"]["rows"] = [
        {**row, "event_id": f"evt-{n}", "user_id": f"u_{n}"} for n in range(90)
    ]

    pages = pdf_pages(render_annex_pdf(annex), tmp_path)
    carrying = [p for p in pages if "u_0" in p or "u_89" in p]

    assert len(carrying) >= 2, "the sample did not break across pages"
    for page in carrying:
        assert "ARGUMENTS (REDACTED)" in re.sub(r"\s+", " ", page).upper()


def test_no_section_heading_is_orphaned_at_a_page_foot(tmp_path) -> None:
    """A heading alone at the bottom of a page, with its table overleaf, is
    the classic paged-media defect. `break-after: avoid` is what stops it."""
    pages = pdf_pages(render_annex_pdf(sample_annex()), tmp_path)

    # From page two: page one ends with the contents table, whose rows look
    # exactly like headings.
    for page in pages[1:]:
        lines = [line.strip() for line in page.splitlines() if line.strip()]
        # Drop the running footer, which is always the last thing on a page.
        body = [line for line in lines if "Not a statutory filing" not in line]
        if not body:
            continue
        assert not re.match(r"^\d(\.\d)? [A-Z]", body[-1]), (
            f"heading {body[-1]!r} is the last thing on its page"
        )


def test_when_a_value_is_one_long_token_then_it_wraps_inside_its_column(
    tmp_path,
) -> None:
    annex = sample_annex()
    token = "Z" * 400
    annex["activity"]["decision_sample"]["rows"][0]["arguments"] = {"path": token}

    text = pdf_text(render_annex_pdf(annex), tmp_path)

    # An unwrappable token would run off the page and lose its tail.
    assert text.replace(" ", "").count("Z") >= 400


def test_when_the_renderer_raises_then_no_partial_pdf_comes_back(monkeypatch) -> None:
    """WeasyPrint is forgiving about markup, so the failure worth pinning is
    the library one: whatever it raises has to reach the route as a
    PdfRenderError carrying a diagnostic, not as bytes."""

    def boom(*_args, **_kwargs):
        raise OSError("pango is not installed")

    monkeypatch.setattr("hexgate_api.features.ai_act.render.pdf.HTML", boom)

    with pytest.raises(PdfRenderError) as excinfo:
        render_pdf("<html><body>x</body></html>")

    assert "pango is not installed" in excinfo.value.detail


async def test_when_a_render_overruns_then_the_caller_is_told_rather_than_left(
    monkeypatch,
) -> None:
    """The timeout bounds what the caller waits, not the work. What has to
    hold is that they get a PdfRenderError instead of sitting on a connection
    the proxy will drop."""
    import time

    from hexgate_api.features.ai_act.render import pdf as pdf_module

    monkeypatch.setattr(pdf_module, "RENDER_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(
        pdf_module, "render_pdf", lambda _html: time.sleep(1) or b"%PDF-"
    )

    with pytest.raises(PdfRenderError, match="timed out"):
        await pdf_module.render_pdf_async("<html><body>x</body></html>")


def test_two_renders_of_one_annex_are_byte_identical() -> None:
    """The document is a pure function of the annex — no clock, no request —
    so re-rendering a report from last quarter reproduces it exactly.

    Bytes, not text: a stamped ``/CreationDate`` would leave the words
    unchanged and the file different, which is the version of this regression
    that a text comparison would wave through.
    """
    annex = sample_annex()

    assert render_annex_pdf(annex) == render_annex_pdf(annex)


# Annex fields the document deliberately does not print, and why. Anything
# not listed here has to appear, so a field the assembler adds later fails
# this test instead of quietly going missing — which is how the matrix's
# `any_other_tool` row and its two agent-gate statements were dropped for a
# day without a single test noticing.
NOT_PRINTED = {
    ".cover.spec_version": "shown as the eyebrow's report-spec version, reformatted",
    ".cover.organization.id": "the name identifies the operator; the id is plumbing",
    ".cover.requested_by.user_id": "the email identifies the requester",
    ".inventory.agents[].agent_id": "the agent name identifies it; the id is plumbing",
    ".inventory.agents[].recorded_by.user_id": "the email identifies the recorder",
    ".activity.decision_sample.rows[].event_id": "a join key, not evidence a reader acts on",
    ".activity.decision_sample.rows[].received_at": "occurred_at is the time shown; the pair is in the annex",
    ".activity.decision_sample.rows[].agent_version_id": "the agent name and version are in section 1",
    ".activity.decision_sample.rows[].session_id": "runs are not grouped yet — section 4, gap 3",
    ".activity.decision_sample.rows[].error_type": "surfaced as the outcome pill and the violation line",
    ".activity.approval_required_sample.rows[].event_id": "as above",
    ".activity.approval_required_sample.rows[].received_at": "as above",
    ".activity.approval_required_sample.rows[].agent_version_id": "as above",
    ".activity.approval_required_sample.rows[].session_id": "as above",
    ".activity.approval_required_sample.rows[].error_type": "as above",
    ".activity.ban_enforcements.rows[].event_id": "as above",
    ".activity.ban_enforcements.rows[].received_at": "as above",
    ".activity.ban_enforcements.rows[].session_id": "as above",
    ".coverage.gaps[].id": "a stable key for the gap, not prose for a reader",
}


def _normalise(text: str) -> str:
    """Tags out, entities back, whitespace collapsed — so a value that the
    layout wrapped across two lines still reads as itself."""
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", text)))


def test_every_annex_string_reaches_the_document() -> None:
    """No annex field is silently dropped.

    The assembler and the renderer move independently, and a field added to
    one is invisible to the other until someone reads a page and notices a
    paragraph is missing. Walking the annex and demanding each value appear
    turns that into a failing test. Deliberate omissions are listed in
    NOT_PRINTED, with a reason each.
    """
    annex = sample_annex()
    haystack = _normalise(build_html(annex))

    missing: list[str] = []

    def walk(value, path: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                walk(item, f"{path}.{key}")
        elif isinstance(value, list):
            for item in value:
                walk(item, f"{path}[]")
        elif isinstance(value, str) and value.strip():
            if re.sub(r"\[\]$", "", path) in NOT_PRINTED or path in NOT_PRINTED:
                return
            # A value the document reformats on purpose counts as present in
            # any of the forms a filter in html.py can produce.
            forms = {value, _dt(value), _label(value), _strip_index(value)}
            if not any(_normalise(form) in haystack for form in forms):
                missing.append(f"{path} = {value[:70]!r}")

    walk(annex, "")

    assert not missing, "annex values absent from the document:\n" + "\n".join(missing)
