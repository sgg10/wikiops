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
* ``annotate`` maps what publishing did (a note, or a coded error) onto the results.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from wikiops_sdk.domain import (
    AppliedOperationResult,
    ApplyResult,
    CreateChildDocumentOperation,
    CreateDocumentOperation,
    DocumentRef,
    OperationStatus,
    UpdateDocumentOperation,
)

from wikiops.providers.github_wiki import layout
from wikiops.providers.github_wiki.errors import GithubWikiError
from wikiops.providers.github_wiki.redaction import Redactor

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
    """The message of an unexpected error: coded ones as they are, others redacted."""
    if isinstance(error, GithubWikiError):
        return str(error)
    detail = Redactor().redact(f"{type(error).__name__}: {error}")
    return f"Unexpected error while applying the operations ({detail})"


def fail_all(operations: Iterable[Any], error: BaseException) -> list[AppliedOperationResult]:
    """One FAILED result per operation, in order, all carrying the same error text."""
    message = failure_text(error)
    return [failed(op.operation_id, message, ref=_document_ref(op)) for op in operations]


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


def note_leftovers(settled: Settled, leftovers: Sequence[str]) -> Settled:
    """Tell, on every rejected operation, which files the rollback could not undo."""
    if not leftovers:
        return settled
    named = ", ".join(f"'{path}'" for path in leftovers)
    note = f"the clone still holds {named}: restore or delete it by hand before the next apply"
    results = dict(settled.results)
    for operation_id in settled.rejected:
        item = results[operation_id]
        results[operation_id] = item.model_copy(update={"message": with_note(item.message, note)})
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
