"""Annex -> the report's HTML.

A pure function of the stored annex: no clock, no database, no request. A
re-render of a report from last quarter reproduces that quarter's document.

Autoescaping is on and is the whole escaping story. Section 3 embeds redacted
argument snapshots — arbitrary operator text — and they are escaped by the
same rule as every other value, at the point of interpolation, rather than by
a helper each call site has to remember to reach for.

The filters below are all presentation: how a timestamp reads, what an unset
field looks like, which tint a decision mode gets. None of them change what
the annex says.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

_HERE = Path(__file__).parent
TEMPLATE_NAME = "report.html.j2"
STYLESHEET = _HERE / "report.css"

EM_DASH = "—"

# Section numbers, names and the articles each one answers to, for the
# contents table. The articles come from the annex — they are part of what the
# report asserts — so only the running order lives here.
_CONTENTS = [
    (1, "AI system inventory", "inventory"),
    (2, "Controls in place", "controls"),
    (3, "Activity record", "activity"),
    (4, "Coverage statement", "coverage"),
]

# The counter row, in reading order. Keys are the annex's; the labels are the
# document's.
COUNTER_LABELS = [
    ("decisions", "Authorisation decisions"),
    # Not "denied by policy": a secret guard's refusal is a DENY row too,
    # and is counted again in the tile below. Two tiles a reader could add
    # together is exactly the misleading figure Art. 99(5) is about.
    ("denials", "Denied (policy or guard)"),
    ("approvals_required", "Approval required"),
    ("guard_refusals", "Refused by a secret guard"),
    ("ban_enforcements", "Ban enforcements"),
    ("model_calls", "Model calls"),
    ("model_call_error_rate", "Model call error rate"),
    ("distinct_models", "Distinct model identifiers"),
]

# Decision modes and outcomes share a vocabulary; anything unrecognised falls
# back to the neutral pill rather than being dropped.
_MODE_CLASS = {
    "allow": "allow",
    "deny": "deny",
    "guard_denied": "deny",
    "approval": "approval",
    "needs_approval": "approval",
}


def _dash(value: Any) -> str:
    """An unset field, as an em-dash.

    A field the operator never filled is absent, not empty: printing ``None``
    would read as a recorded value of that name, and a blank cell reads as an
    oversight in the document rather than in the data.
    """
    if value is None or value == "" or value == []:
        return EM_DASH
    return str(value)


def _dt(value: Any) -> str:
    """An annex timestamp as ``YYYY-MM-DD HH:MM:SS``, in UTC.

    The annex stores ISO-8601; the offset is dropped because every timestamp
    it holds is already UTC and the column is narrow. A value that does not
    parse is shown as it was stored rather than swallowed — this is an
    evidence document, so an unexpected format is information.
    """
    if value is None or value == "":
        return EM_DASH
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.isoformat()
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def _num(value: Any) -> str:
    """A count with thin thousands separators, or an em-dash if unset."""
    if value is None or value == "":
        return EM_DASH
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, int):
        return f"{value:,}".replace(",", " ")
    return f"{value:,.2f}".replace(",", " ")


def _pct(value: Any) -> str:
    """A rate stored as a fraction, as a percentage."""
    if value is None or not isinstance(value, (int, float)) or isinstance(value, bool):
        return EM_DASH
    return f"{value * 100:.1f} %"


def _label(value: Any) -> str:
    """``needs_approval`` -> ``Needs approval``: a stored enum, as a word."""
    if value is None or value == "":
        return EM_DASH
    return str(value).replace("_", " ").capitalize()


def _mode_class(value: Any) -> str:
    return _MODE_CLASS.get(str(value), "neutral")


def _args(value: Any) -> str:
    """A recorded argument snapshot, as one compact JSON line.

    ``sort_keys`` so the same snapshot reads the same way in two renders, and
    ``ensure_ascii=False`` so an accented value stays itself instead of
    becoming an escape the reader has to decode.
    """
    if value is None:
        return EM_DASH
    if isinstance(value, str):
        return value
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(", ", ": ")
    )


def _strip_index(value: Any) -> str:
    """Drop a leading ``1. `` from a verification step.

    The annex numbers the steps in their text; the document puts them in an
    ``<ol>``, and two numbers on one line reads as a mistake.
    """
    text = str(value)
    head, sep, tail = text.partition(". ")
    return tail if sep and head.strip().isdigit() else text


def _short_hash(value: Any) -> str:
    """A long digest as ``head…tail``, for a column that only identifies it.

    The inventory row needs enough of a bundle hash to tell two bundles apart
    at a glance; five wrapped lines of hex crowd out the fields a reader came
    for. Nothing is lost — section 1.1 prints every hash in full, and the
    annex holds them all. A hash short enough to fit is left alone.
    """
    if value is None or value == "":
        return EM_DASH
    text = str(value)
    if len(text) <= 20:
        return text
    return f"{text[:8]}\u2026{text[-8:]}"


def _matrix_roles(matrix: dict) -> list[str]:
    """The matrix's column order.

    Taken from a row's own cells when there is a row, not from ``roles``: the
    two are built together by the assembler, and reading the grid through the
    keys it is actually stored under means a role list that has drifted shows
    up as an em-dash in one cell rather than as a failed render.
    """
    rows = matrix.get("rows") or []
    if rows:
        return list(rows[0].get("cells") or {})
    return list(matrix.get("roles") or [])


# Codepoints the document's own fonts are known to carry, plus the few the
# template's own chrome uses. Everything else becomes a visible marker: Pango
# draws a missing glyph as .notdef and says nothing, so without this a report
# would quietly drop text the signed annex holds — an argument snapshot in
# Chinese, a name in Devanagari. The annex keeps the real character; the
# document says unambiguously which one it was.
#
# ASCII, Latin-1 Supplement, and markup whitespace, which is the range both
# Liberation and DejaVu cover without gaps. Coverage above that is patchy even
# inside Latin Extended-B, so the marker takes over there.
_REPRESENTABLE = (
    frozenset(range(0x20, 0x7F))
    | frozenset(range(0xA0, 0x100))
    | {0x09, 0x0A, 0x0D}
    | {ord(char) for char in "\u2014\u2026\u2192\u202f"}  # — … → thin space
)


def _mark_unrepresentable(html: str) -> str:
    """Spell out any codepoint the document's fonts cannot draw.

    Applied once to the finished HTML rather than per value: there is no call
    site to forget, and the markup itself is ASCII, so nothing but text is
    touched.
    """
    return "".join(
        char if ord(char) in _REPRESENTABLE else f"<U+{ord(char):04X}>" for char in html
    )


def build_environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(_HERE),
        # HTML autoescaping, on for the template this module loads and for any
        # other .html/.j2 that ever joins it.
        autoescape=select_autoescape(
            enabled_extensions=("html", "j2"), default_for_string=True
        ),
        # A key the annex does not carry is a failed render, not a blank cell.
        # A compliance document that quietly omits a field it claims to show
        # is worse than one that does not render.
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(
        dash=_dash,
        dt=_dt,
        num=_num,
        pct=_pct,
        label=_label,
        mode_class=_mode_class,
        args=_args,
        short_hash=_short_hash,
        strip_index=_strip_index,
        matrix_roles=_matrix_roles,
    )
    return env


def build_html(annex: dict) -> str:
    """The report's HTML for one stored annex."""
    env = build_environment()
    contents = [
        (number, name, annex[key]["articles"]) for number, name, key in _CONTENTS
    ]
    contents.append((5, "Signature and verification", [EM_DASH]))
    return _mark_unrepresentable(
        env.get_template(TEMPLATE_NAME).render(
            cover=annex["cover"],
            inventory=annex["inventory"],
            controls=annex["controls"],
            activity=annex["activity"],
            coverage=annex["coverage"],
            signature=annex["signature"],
            contents=contents,
            counter_labels=COUNTER_LABELS,
        )
    )
