"""Save a checked policy change: every file, its record and the agents'
bundles, in one commit or not at all.

:func:`apply_change` runs, under the project's lock:

1. ``layout``: the project has a stored ``policy.yaml``, else
   :class:`~service.NoEntryFileError`.
2. base hashes: each file's ``base_hash`` is its stored ``text_hash`` (null for a
   file that must not exist yet), else :class:`~service.StaleBaseError`.
3. the checks of :func:`~apply_checks.run_checks`.
4. the effect: the per-role diff of the saved project against the drafted one,
   taken before anything is written, since afterwards both sides are the same.
5. the file rows and the change row, flushed, not committed.
6. the bundles, rebuilt from those rows without a commit. A project whose
   bundles cannot be built rolls everything back: the check ``bundle`` fails,
   and agents keep enforcing what is still stored.
7. one commit.

A failed check is an answer, ``applied: false``, with nothing written; a stale
base or a missing entry file is raised, for the caller to answer.
"""

from __future__ import annotations

from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.core.ids import new_id
from hexgate_api.core.locks import project_lock
from hexgate_api.features.policy_modules import service
from hexgate_api.features.policy_modules.apply_checks import (
    Check,
    ChecksResult,
    run_checks,
)
from hexgate_api.features.policy_modules.diff import diff_files
from hexgate_api.models import AssistantChange, PolicyFile
from hexgate_api.schemas import (
    PolicyApplyFileOut,
    PolicyApplyRequest,
    PolicyApplyResponse,
    PolicyDiffResponse,
)


async def apply_change(
    session: AsyncSession,
    project_id: str,
    request: PolicyApplyRequest,
    actor: str,
    client_id: str | None,
) -> PolicyApplyResponse:
    """Check ``request``'s files and, when every check passes, save them with
    an ``assistant_change`` row and the rebuilt bundles in one commit.
    ``actor`` is the user saving; ``client_id`` the OAuth client, recorded on
    the change row.

    A change refused at the bundle step, or one that raises while writing, is
    rolled back on ``session``, which expires every object loaded in it: read
    what the caller needs (the user's id) before calling."""
    async with project_lock(project_id):
        rows = {row.name: row for row in await service.list_files(session, project_id)}
        _check_layout(rows)
        _check_bases(rows, request)
        result = await run_checks(session, project_id, request.files, request.samples)
        if not result.passed:
            failed = next(c.id for c in result.checks if c.status == "fail")
            bundle = Check(id="bundle", status="skipped", detail=f"{failed} failed")
            return _refused(result, bundle)
        return await _save(session, project_id, request, rows, result, actor, client_id)


def _check_layout(rows: dict[str, PolicyFile]) -> None:
    if service.ENTRY_FILE not in rows:
        raise service.NoEntryFileError(
            f"this project has no stored {service.ENTRY_FILE!r}"
        )


def _check_bases(rows: dict[str, PolicyFile], request: PolicyApplyRequest) -> None:
    """Raise :class:`~service.StaleBaseError` naming every file whose stored
    ``text_hash`` (None when absent) differs from its ``base_hash``."""
    stale = [f.name for f in request.files if _stored_hash(rows, f.name) != f.base_hash]
    if stale:
        raise service.StaleBaseError(stale)


def _stored_hash(rows: dict[str, PolicyFile], name: str) -> str | None:
    row = rows.get(name)
    return None if row is None else service.text_hash(row.content)


async def _save(
    session: AsyncSession,
    project_id: str,
    request: PolicyApplyRequest,
    rows: dict[str, PolicyFile],
    result: ChecksResult,
    actor: str,
    client_id: str | None,
) -> PolicyApplyResponse:
    """Steps 4 to 7. Any failure on the way rolls the session back, so a row
    written here never outlives a bundle that was not built."""
    # diff_project's body, on the rows already read under the lock.
    saved = {name: row.content for name, row in rows.items()}
    effect = diff_files(saved, service.overlay_drafts(saved, request.files))
    files = [_file_record(rows, f.name, f.content) for f in request.files]
    change = AssistantChange(
        id=new_id(AssistantChange),
        project_id=project_id,
        session_id=request.session_id,
        created_by_user_id=actor,
        client_id=client_id,
        summary=request.summary,
        files=files,
        samples=[s.model_dump(mode="json") for s in request.samples],
        checks=[],
        boundary_changes=_boundary_dicts(result),
        effect=PolicyDiffResponse(**effect).model_dump(mode="json"),
    )
    try:
        for f in request.files:
            service.stage_file(
                session,
                rows.get(f.name),
                project_id=project_id,
                name=f.name,
                content=f.content,
                actor_user_id=actor,
                change_id=change.id,
            )
        session.add(change)
        await session.flush()
        bundle = await _rebuild_bundles(session, project_id)
        if bundle.status == "fail":
            await session.rollback()
            return _refused(result, bundle)
        checks = [*result.checks, bundle]
        change.checks = [c.model_dump(mode="json") for c in checks]
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    return PolicyApplyResponse(
        applied=True,
        change_id=change.id,
        checks=change.checks,
        boundary_changes=change.boundary_changes,
        files=[_file_out(f) for f in files],
    )


async def _rebuild_bundles(session: AsyncSession, project_id: str) -> Check:
    """Build every agent's bundle from the flushed rows, uncommitted. Fails
    when any agent's bundle cannot be built, including when the build raises:
    an unbuilt bundle is never reported as a pass."""
    built = await service.rebuild_bundles(session, project_id, commit=False)
    if built is None:
        detail = "the agents' bundles could not be built; nothing was saved"
        return Check(id="bundle", status="fail", detail=detail)
    return Check(id="bundle", status="pass", detail=f"{built} agent bundle(s) built")


def _refused(result: ChecksResult, bundle: Check) -> PolicyApplyResponse:
    return PolicyApplyResponse(
        applied=False,
        checks=[c.model_dump(mode="json") for c in [*result.checks, bundle]],
        boundary_changes=_boundary_dicts(result),
    )


def _boundary_dicts(result: ChecksResult) -> list[dict]:
    return [change.model_dump(mode="json") for change in result.boundary_changes]


def _file_record(rows: dict[str, PolicyFile], name: str, content: str) -> dict:
    """One file of the change row: its text and ``text_hash`` on each side."""
    row = rows.get(name)
    return {
        "name": name,
        "before_content": None if row is None else row.content,
        "before_hash": _stored_hash(rows, name),
        "after_content": content,
        "after_hash": service.text_hash(content),
    }


def _file_out(record: dict) -> PolicyApplyFileOut:
    return PolicyApplyFileOut.model_validate(record)
