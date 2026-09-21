"""The findings contract: the wire shapes, the deterministic finding id, and
the DDL that stores them.

PR 1 of the findings spec ships no detector and no endpoint, so what is worth
pinning here is exactly what the later PRs build on and cannot see break: that
``finding_id`` is a function of its bucket alone (re-scoring depends on it),
and that ``migrations/0004`` and ``init/schema.sql`` say the same thing (a
hand-migrated volume and a fresh one must end up with the same tables).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from tests.core.test_migrations import CLICKHOUSE_DATABASE, split_statements

from hexgate_api.schemas import (
    AnomalySeverity,
    AuditFinding,
    AuditFindingPage,
    FindingKind,
    FindingSubject,
    finding_id,
)

# parents: [0] findings, [1] features, [2] tests, [3] api, [4] platform
_PLATFORM = Path(__file__).resolve().parents[4]
INIT_SCHEMA = _PLATFORM / "clickhouse" / "init" / "schema.sql"
MIGRATION_0004 = _PLATFORM / "clickhouse" / "migrations" / "0004_add_findings.sql"

FINDING_TABLES = (
    "audit_finding",
    "feature_first_seen",
    "agent_baseline",
    "detector_watermark",
)

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def _finding(**overrides) -> AuditFinding:
    base = {
        "finding_id": finding_id("proj_1", FindingKind.RUN_SHAPE, "run_9", "run_9"),
        "detected_at": NOW,
        "kind": FindingKind.RUN_SHAPE,
        "severity": AnomalySeverity.MEDIUM,
        "subject": FindingSubject.RUN,
        "subject_id": "run_9",
        "first_seen": NOW,
        "last_seen": NOW,
        "summary": "researcher made 41 tool calls in one run, 8x its usual 5.",
    }
    return AuditFinding(**{**base, **overrides})


# ---------------------------------------------------------------------------
# finding_id()
# ---------------------------------------------------------------------------


def test_finding_id_happy_path() -> None:
    """Same project, kind, subject and bucket — same id, every time.

    This is what makes a re-score an update rather than an append: tick 2 of a
    growing run must land on tick 1's row.
    """
    first = finding_id("proj_1", FindingKind.REPETITION, "run_9", "run_9", "search")
    second = finding_id("proj_1", FindingKind.REPETITION, "run_9", "run_9", "search")

    assert first == second
    assert isinstance(first, UUID)
    assert first.version == 5


def test_when_the_project_differs_then_the_id_differs() -> None:
    """Tenant isolation at the identity level: two projects that see the same
    run id and tool must not collapse into one finding."""
    one = finding_id("proj_1", FindingKind.REPETITION, "run_9", "run_9", "search")
    two = finding_id("proj_2", FindingKind.REPETITION, "run_9", "run_9", "search")

    assert one != two


def test_when_the_kind_differs_then_the_id_differs() -> None:
    """Two detectors reaching the same subject file two findings, not one — the
    suppression rule (PR 8) decides which is shown, not the storage key."""
    shape = finding_id("proj_1", FindingKind.RUN_SHAPE, "run_9", "run_9")
    conformance = finding_id("proj_1", FindingKind.CONFORMANCE, "run_9", "run_9")

    assert shape != conformance


def test_when_the_bucket_differs_then_the_id_differs() -> None:
    """``repetition`` buckets on (run, tool): one run repeating two tools is two
    findings."""
    search = finding_id("proj_1", FindingKind.REPETITION, "run_9", "run_9", "search")
    fetch = finding_id("proj_1", FindingKind.REPETITION, "run_9", "run_9", "fetch")

    assert search != fetch


def test_when_a_bucket_part_contains_a_separator_then_no_collision() -> None:
    """Bucket parts are joined, so a joiner a caller could supply would let two
    different buckets hash to one id — silently merging two findings into one
    row. A tool really can be named ``a|b`` or ``a:b``; none can contain the
    ASCII unit separator used here.
    """
    split = finding_id("proj_1", FindingKind.REPETITION, "run_9", "a", "b")
    joined = finding_id("proj_1", FindingKind.REPETITION, "run_9", "a|b")
    colon = finding_id("proj_1", FindingKind.REPETITION, "run_9", "a:b")

    assert len({split, joined, colon}) == 3


def test_when_the_kind_is_a_string_then_the_id_matches_the_enum() -> None:
    """``FindingKind`` is a ``StrEnum``, so a detector that passes the raw value
    (from a stored row, say) reaches the same finding as one passing the enum."""
    assert finding_id("proj_1", FindingKind.DENY_BURST, "alice", "2026-09-21T12") == (
        finding_id("proj_1", "deny_burst", "alice", "2026-09-21T12")
    )


# ---------------------------------------------------------------------------
# AuditFinding / AuditFindingPage
# ---------------------------------------------------------------------------


def test_audit_finding_happy_path() -> None:
    """A finding round-trips through JSON unchanged — the detector writes it,
    the page reads it, and ``evidence`` stays an object on both sides."""
    finding = _finding(
        severity=AnomalySeverity.HIGH,
        agent_name="researcher",
        run_id=UUID("11111111-1111-1111-1111-111111111111"),
        session_id="sess_3",
        evidence={"feature": "tool_calls", "z": 6.2, "others": []},
        suggested_control={"expression": "run.tool_calls <= 12"},
    )

    restored = AuditFinding.model_validate_json(finding.model_dump_json())

    assert restored == finding
    assert restored.evidence["z"] == 6.2
    assert restored.suggested_control["expression"] == "run.tool_calls <= 12"


def test_when_optional_fields_are_absent_then_they_default() -> None:
    """The denormalised join keys are defaulted, not nullable — except
    ``run_id``, which reads as absent rather than as the zero UUID the column
    stores for "not about one run"."""
    finding = _finding()

    assert finding.agent_name == ""
    assert finding.session_id == ""
    assert finding.run_id is None
    assert finding.evidence == {}
    assert finding.suggested_control == {}


def test_when_the_severity_is_low_then_it_validates() -> None:
    """The reason ``AnomalySeverity`` gained a third member: a detector that
    cannot yet prove its false-positive budget ships quiet, not off."""
    finding = _finding(severity="low")

    assert finding.severity is AnomalySeverity.LOW


def test_anomaly_severity_wire_values_are_unchanged() -> None:
    """Each member keeps its exact wire spelling.

    Scope, deliberately: this pins the enum, NOT the anomalies endpoint. That
    ``GET /audit/anomalies`` still returns only these two is a property of
    ``_sliding_window_anomalies``'s two-branch severity assignment, and it is
    tests/features/audit/test_audit.py that covers it — a test here asserting
    three enum members would pass however that detector changed.
    """
    assert AnomalySeverity.HIGH == "high"
    assert AnomalySeverity.MEDIUM == "medium"
    assert AnomalySeverity.LOW == "low"


def test_audit_finding_page_happy_path() -> None:
    """``total`` is the unpaginated match count, same shape as the other pages."""
    page = AuditFindingPage(rows=[_finding()], total=42, limit=50, offset=0)

    assert page.total == 42
    assert page.rows[0].kind is FindingKind.RUN_SHAPE


def test_the_five_detector_kinds_are_the_spec_s() -> None:
    """The dedup-bucket table in §II has one row per kind; a sixth member here
    would be a detector with no bucket defined."""
    assert {kind.value for kind in FindingKind} == {
        "first_seen",
        "run_shape",
        "repetition",
        "deny_burst",
        "conformance",
    }
    assert {subject.value for subject in FindingSubject} == {"user", "agent", "run"}


# ---------------------------------------------------------------------------
# The DDL
# ---------------------------------------------------------------------------


def _create_statement(sql: str, table: str) -> str:
    """The one executable ``CREATE TABLE`` for ``table``, comments stripped.

    ``split_statements`` rather than a regex up to the next ``;``: these files'
    prose comments are full of semicolons and so is at least one ``COMMENT``
    literal, and a naive split stops inside them. test_migrations.py owns that
    splitter and documents the bug it was written for — importing it is also
    what keeps the two tests from disagreeing about where a statement ends.
    """
    prefix = f"CREATE TABLE IF NOT EXISTS {CLICKHOUSE_DATABASE}.{table}"
    matches = [s for s in split_statements(sql) if s.startswith(prefix)]
    assert len(matches) == 1, (
        f"expected one CREATE TABLE for {table}, got {len(matches)}"
    )
    return matches[0]


def test_the_migration_and_the_init_schema_declare_the_same_tables() -> None:
    """0004's own header claims it. Nothing else checks it without a live
    ClickHouse: ``test_migrations`` compares the two for real, but only under
    the ``integration`` marker, so drift introduced in an ordinary edit would
    reach a stage before anyone ran that. The failure it prevents is a
    hand-migrated volume whose columns differ from a fresh one's — which the
    startup schema guard then reports as a stage that cannot boot.
    """
    init_sql = INIT_SCHEMA.read_text()
    migration_sql = MIGRATION_0004.read_text()

    for table in FINDING_TABLES:
        assert _create_statement(init_sql, table) == _create_statement(
            migration_sql, table
        ), f"{table} differs between init/schema.sql and 0004"

    # And 0004 creates these four and nothing else: a table it added but
    # init/schema.sql did not would leave fresh volumes without it.
    created = [
        statement.split()[5].split(".")[1]
        for statement in split_statements(migration_sql)
        if statement.startswith("CREATE TABLE IF NOT EXISTS")
    ]
    assert created == list(FINDING_TABLES)


def test_audit_finding_is_keyed_on_identity_alone() -> None:
    """The sorting key is the load-bearing decision of the whole table.

    ``ReplacingMergeTree`` collapses rows whose *whole* sorting key is equal, and
    ``severity`` and ``last_seen`` both move as a run grows. Either one in the
    key leaves a single long run stored as one row per tick at escalating
    severities, with the ``uuid5`` bucket doing nothing — and the page then
    shows the same run five times.
    """
    statement = _create_statement(INIT_SCHEMA.read_text(), "audit_finding")

    assert "ORDER BY (project_id, finding_id)" in statement
    assert "ENGINE = ReplacingMergeTree(detected_at)" in statement


def test_feature_first_seen_keeps_the_minimum() -> None:
    """A ``ReplacingMergeTree`` here keeps the *newest* row, which is backwards
    for a first-sighting table: the second sighting of a tool would overwrite
    the first and nothing would ever read as novel again."""
    statement = _create_statement(INIT_SCHEMA.read_text(), "feature_first_seen")

    assert "ENGINE = AggregatingMergeTree" in statement
    # Whitespace-normalised: the invariant is the aggregate, not the alignment.
    assert "first_at SimpleAggregateFunction(min, DateTime64(3, 'UTC'))" in (
        " ".join(statement.split())
    )


def test_the_deduplicating_tables_are_not_partitioned_by_time() -> None:
    """``agent_baseline`` and ``detector_watermark`` must collapse to one row per
    key forever. ClickHouse dedups within a partition, so partitioning either on
    its version column would leave every hourly rebuild — and every watermark
    write — as its own row that no merge ever reaches.
    """
    init_sql = INIT_SCHEMA.read_text()

    for table in ("agent_baseline", "detector_watermark"):
        assert "PARTITION BY" not in _create_statement(init_sql, table), table
