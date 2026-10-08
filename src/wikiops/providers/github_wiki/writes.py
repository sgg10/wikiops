"""Pure helpers of ``GithubWikiProvider.apply_changes`` (GW-P8, P9, P10, S9, S11, S12).

Nothing here touches git, the filesystem or the lock: the provider orchestrates, these
functions decide. They cover the three places where the provider must not trust or
forward blindly:

* ``prepare`` applies the flat page policy to every operation before the backend sees
  it, derives root references for ref-less creates and keeps the failures out of the
  delegated batch.
* ``settle`` learns what was written ONLY from the SDK result fields (``resolved_ref``
  and ``resolved_asset_ref`` of APPLIED results), re-validates those paths and guards
  the reference string the backend reports. Files the backend wrote without reporting
  them are never part of the written set, so they can never be staged.
* ``fail_ignored`` and ``ignored_error`` turn a write that git ignores into a loud failure.
* ``annotate`` maps what publishing did (a note, or a coded error) onto the results.
* ``page_targets``, ``asset_owner`` and ``rollback_owner`` say which paths an operation (a
  page write or an asset upload, whether sent alone or inside a change set) is the one to
  write, which is all the rollback may touch if it has to undo that operation.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from wikiops_sdk.domain import (
    AppliedOperationResult,
    ApplyResult,
    CreateChildDocumentOperation,
    CreateDocumentOperation,
    DocumentRef,
    OperationStatus,
    PutAssetOperation,
    UpdateDocumentOperation,
)

from wikiops.providers._fs import FsError, hashed_asset_name
from wikiops.providers.github_wiki import layout
from wikiops.providers.github_wiki.errors import GithubWikiError
from wikiops.providers.github_wiki.redaction import Redactor
from wikiops.providers.github_wiki.rollback import RollbackIncomplete, RollbackResult

_NO_RESULT = "The backend returned no result for this operation"


@dataclass(frozen=True)
class Prepared:
    """Operations that passed the page policy and the failures of those that did not."""

    delegated: tuple[Any, ...]
    failures: dict[str, AppliedOperationResult]


@dataclass(frozen=True)
class Settled:
    """Backend results after validation and the repo-relative paths they report as written."""

    results: dict[str, AppliedOperationResult]
    pages: tuple[str, ...]
    assets: tuple[str, ...]
    rejected: tuple[str, ...] = ()  # ids of operations the backend applied and the provider refused

    @property
    def written(self) -> tuple[str, ...]:
        return (*self.pages, *self.assets)


def failed(
    operation_id: str, message: str, *, ref: DocumentRef | None = None
) -> AppliedOperationResult:
    return AppliedOperationResult(
        operation_id=operation_id,
        status=OperationStatus.FAILED,
        message=message,
        resolved_ref=ref,
    )


def _document_ref(operation: Any) -> DocumentRef | None:
    """The page reference an operation carries, if it is a document operation."""
    ref = getattr(operation, "ref", None)
    return ref if isinstance(ref, DocumentRef) else None


def failure_text(error: BaseException) -> str:
    """The message of an unexpected error: coded ones as they are, others redacted.

    A ``RollbackIncomplete`` is its cause's text followed by what the rollback left behind.
    """
    if isinstance(error, GithubWikiError):
        return str(error)
    if isinstance(error, RollbackIncomplete):
        return with_note(failure_text(error.cause), error.note)
    detail = Redactor().redact(f"{type(error).__name__}: {error}")
    return f"Unexpected error while applying the operations ({detail})"


def fail_all(operations: Iterable[Any], error: BaseException) -> list[AppliedOperationResult]:
    """One FAILED result per operation, in order, all carrying the same error text."""
    message = failure_text(error)
    return [failed(op.operation_id, message, ref=_document_ref(op)) for op in operations]


def page_targets(operations: Iterable[Any]) -> frozenset[str]:
    """The page names the document operations write (those with a valid reference).

    The reference the operation CARRIES, never the one the backend reports: the report is
    what the provider may refuse, so it cannot decide which file to roll back.
    """
    targets: set[str] = set()
    for operation in operations:
        ref = _document_ref(operation)
        if ref is None:
            continue
        try:
            targets.add(layout.validate_page_ref(ref))
        except GithubWikiError:
            continue
    return frozenset(targets)


def _hash_pattern(name: object) -> re.Pattern[str] | None:
    """The file name any content of the asset ``name`` is stored under, hash left open.

    ``None`` when the backend would refuse the name before writing anything.
    """
    probe = hashlib.sha256(b"").hexdigest()[:16]
    try:
        prefix, _, suffix = hashed_asset_name(name if isinstance(name, str) else None, b"").rpartition(probe)
    except FsError:
        return None
    return re.compile(rf"{re.escape(prefix)}[0-9a-f]{{{len(probe)}}}{re.escape(suffix)}")


def asset_owner(
    operation: Any, content: bytes | None, *, reported: object = None
) -> Callable[[str], bool]:
    """Whether a path is the file this asset upload writes.

    Backends store an asset under the content-hashed name of its file name; the path the
    backend reported (when it did) counts too. Without ``content`` (an upload sent inside a
    change set carries only a source) any hash of that name counts. Anything else that
    appeared meanwhile is not this upload's.
    """
    name = getattr(operation, "name", None)
    if content is None:
        pattern = _hash_pattern(name)
        stored = None if pattern is None else pattern.fullmatch
    else:
        try:
            exact = hashed_asset_name(name, content)
        except FsError:
            exact = None  # the backend refuses such an asset before it writes anything
        stored = None if exact is None else exact.__eq__

    def owns(path: str) -> bool:
        return path == reported or (stored is not None and bool(stored(PurePosixPath(path).name)))

    return owns


def _reported_asset_path(operation: Any, reply: ApplyResult | None) -> str | None:
    """The asset path the backend reported for ``operation``, if it reported one."""
    if reply is None:
        return None
    for item in reply.results:
        if item.operation_id == operation.operation_id and item.resolved_asset_ref is not None:
            return item.resolved_asset_ref.locator.get("path")
    return None


def rollback_owner(
    operations: Iterable[Any], reply: ApplyResult | None = None
) -> Callable[[str], bool]:
    """Whether a path is a file one of ``operations`` writes (pages and assets alike).

    The rollback of a change set may touch only these: the pages the operations carry, the
    content-hashed names of their asset uploads and the asset path the backend reported for
    an upload (``reply``). Anything else that appeared meanwhile is not theirs.
    """
    delegated = list(operations)
    pages = page_targets(delegated)
    uploads = [
        asset_owner(op, None, reported=_reported_asset_path(op, reply))
        for op in delegated
        if isinstance(op, PutAssetOperation)
    ]
    return lambda path: path in pages or any(owns(path) for owns in uploads)


# -- before the backend -------------------------------------------------------------------


def _checked(operation: Any, provider_name: str) -> Any:
    """The operation to delegate (with a derived root ref when it had none) or a coded error."""
    if isinstance(operation, (CreateDocumentOperation, CreateChildDocumentOperation)):
        if operation.ref is not None:
            layout.validate_page_ref(operation.ref)
            return operation
        title = (
            operation.child_title
            if isinstance(operation, CreateChildDocumentOperation)
            else operation.title
        )
        derived = layout.derive_root_ref(title, provider=provider_name)
        return operation.model_copy(update={"ref": derived})
    if isinstance(operation, UpdateDocumentOperation):
        layout.validate_page_ref(operation.ref)
    return operation


def prepare(operations: Sequence[Any], *, provider_name: str) -> Prepared:
    """Apply the page policy per operation: failures are kept apart and never delegated."""
    delegated: list[Any] = []
    failures: dict[str, AppliedOperationResult] = {}
    for operation in operations:
        try:
            delegated.append(_checked(operation, provider_name))
        except GithubWikiError as error:
            failures[operation.operation_id] = failed(
                operation.operation_id, str(error), ref=_document_ref(operation)
            )
    return Prepared(tuple(delegated), failures)


# -- after the backend --------------------------------------------------------------------


def _reported_paths(result: AppliedOperationResult) -> tuple[list[str], list[str]]:
    """Pages and assets an APPLIED result reports as written, validated; coded error otherwise."""
    if result.status is not OperationStatus.APPLIED:
        return [], []
    if result.resolved_asset_reference is not None:
        layout.guard_link(result.resolved_asset_reference)
    if result.resolved_ref is None:
        raise GithubWikiError(
            "ref.missing_path",
            "The backend applied the operation but reported no page reference",
            hint="a backend must return resolved_ref for every created or updated page",
        )
    pages = [layout.validate_page_ref(result.resolved_ref)]
    assets: list[str] = []
    if result.resolved_asset_ref is not None:
        layout.build_asset_reference(result.resolved_asset_ref)
        assets.append(result.resolved_asset_ref.locator["path"])
    return pages, assets


def settle(delegated: Sequence[Any], reply: ApplyResult) -> Settled:
    """Pair every delegated operation with its backend result and collect the written paths."""
    by_id = {item.operation_id: item for item in reply.results}
    results: dict[str, AppliedOperationResult] = {}
    pages: list[str] = []
    assets: list[str] = []
    rejected: list[str] = []
    for operation in delegated:
        result = by_id.get(operation.operation_id)
        if result is None:
            results[operation.operation_id] = failed(
                operation.operation_id, _NO_RESULT, ref=_document_ref(operation)
            )
            continue
        try:
            written_pages, written_assets = _reported_paths(result)
        except GithubWikiError as error:
            results[operation.operation_id] = result.model_copy(
                update={"status": OperationStatus.FAILED, "message": str(error)}
            )
            rejected.append(operation.operation_id)
            continue
        results[operation.operation_id] = result
        pages += written_pages
        assets += written_assets
    return Settled(results, tuple(pages), tuple(assets), tuple(rejected))


def note_rollback(settled: Settled, result: RollbackResult) -> Settled:
    """Tell, on every rejected operation, what the rollback left in the clone."""
    if result.clean:
        return settled
    results = dict(settled.results)
    for operation_id in settled.rejected:
        item = results[operation_id]
        results[operation_id] = item.model_copy(
            update={"message": with_note(item.message, result.note)}
        )
    return Settled(results, settled.pages, settled.assets, settled.rejected)


def in_order(
    operations: Iterable[Any], *sources: dict[str, AppliedOperationResult]
) -> list[AppliedOperationResult]:
    """The result of every operation, in the order planned, from the first source that has it."""
    ordered: list[AppliedOperationResult] = []
    for operation in operations:
        for source in sources:
            if operation.operation_id in source:
                ordered.append(source[operation.operation_id])
                break
    return ordered


# -- after publishing ---------------------------------------------------------------------


def with_note(message: str | None, note: str) -> str:
    """Append ``note`` to a backend message without breaking its own sentence."""
    base = (message or "").rstrip()
    if not base:
        return note
    return f"{base} {note}" if base.endswith(".") else f"{base}; {note}"


def ignored_error(paths: Iterable[str], *, workdir: object) -> GithubWikiError:
    """``commit.failed`` for paths the backend wrote and an ignore rule hides from git.

    ``git status`` never lists them, so they would be neither staged nor committed: that
    must be said, never skipped silently.
    """
    named = ", ".join(f"'{path}'" for path in paths)
    return GithubWikiError(
        "commit.failed",
        f"{named} was written but is ignored by git (.gitignore or .git/info/exclude), "
        "so it was not committed",
        context={"workdir": str(workdir)},
        hint="remove the ignore rule or rename the page",
    )


def _written_paths(item: AppliedOperationResult) -> set[str]:
    """The repo-relative paths an APPLIED result reports (already validated by ``settle``)."""
    if item.status is not OperationStatus.APPLIED:
        return set()
    paths: set[str] = set()
    if item.resolved_ref is not None:
        paths.add(layout.validate_page_ref(item.resolved_ref))
    if item.resolved_asset_ref is not None:
        paths.add(item.resolved_asset_ref.locator["path"])
    return paths


def fail_ignored(
    results: list[AppliedOperationResult], ignored: Sequence[str], *, workdir: object
) -> list[AppliedOperationResult]:
    """Fail every APPLIED result that wrote an ignored path; the others stay as they are."""
    if not ignored:
        return results
    hidden = set(ignored)
    failed_results: list[AppliedOperationResult] = []
    for item in results:
        mine = sorted(_written_paths(item) & hidden)
        if mine:
            item = item.model_copy(
                update={
                    "status": OperationStatus.FAILED,
                    "message": str(ignored_error(mine, workdir=workdir)),
                }
            )
        failed_results.append(item)
    return failed_results


def annotate(
    results: list[AppliedOperationResult], *, note: str = "", error: GithubWikiError | None = None
) -> list[AppliedOperationResult]:
    """Put the publishing outcome on the results.

    A note goes on every APPLIED result. An error fails every operation that was written
    in this apply; when nothing was written (every operation was a no-op), the error is
    about earlier work the apply was carrying (a push of older commits, a retried
    commit), so it fails the no-op results instead and is never swallowed.
    """
    if error is None:
        if not note:
            return results
        return [
            item.model_copy(update={"message": with_note(item.message, note)})
            if item.status is OperationStatus.APPLIED
            else item
            for item in results
        ]
    carried = (
        OperationStatus.APPLIED
        if any(item.status is OperationStatus.APPLIED for item in results)
        else OperationStatus.SKIPPED
    )
    return [
        item.model_copy(update={"status": OperationStatus.FAILED, "message": str(error)})
        if item.status is carried
        else item
        for item in results
    ]
