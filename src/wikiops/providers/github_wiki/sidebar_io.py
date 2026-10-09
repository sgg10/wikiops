"""Local IO of the managed sidebar of ``github_wiki`` (SB4, SB5, SB10-SB12, SB14).

``sidebar.py`` is pure; this module is where the sidebar meets the clone and the backend,
always through the layers that already exist and never over the network:

* ``root_pages`` scans the clone for the pages the sidebar links to.
* ``plan_action`` tells, from the clone alone, what the next apply would do with
  ``_Sidebar.md`` (a plan note, so it never raises and never runs a git command).
* ``SidebarWriter`` regenerates the file: it reads the previous one through the backend,
  leaves an unmarked one alone, renders, writes through the backend (never by itself) and
  records the file in the pending manifest. It never raises: a failure becomes a
  ``sidebar.write_failed`` warning after the file is put back exactly as it was.

Nothing here imports the provider: the provider wires these pieces in.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from wikiops_sdk.contracts import DocumentProvider
from wikiops_sdk.domain import (
    ApplyResult,
    ChangeSet,
    CreateDocumentOperation,
    DocumentRef,
    OperationStatus,
    RefKind,
    UpdateDocumentOperation,
)

from wikiops.providers import _fs
from wikiops.providers.github_wiki import layout, rollback, sidebar, writes
from wikiops.providers.github_wiki.errors import GithubWikiError, render_message
from wikiops.providers.github_wiki.git import Git
from wikiops.providers.github_wiki.manifest import PendingManifest
from wikiops.providers.github_wiki.redaction import Redactor

SidebarAction = Literal["create", "regenerate", "skipped-unmanaged"]

_MARKDOWN_SUFFIX = ".md"
_EXCLUDED_PREFIX = "_"
# The marker line plus the longest line ending that still counts as that line (``\r\n``):
# the first line cannot be the marker if it does not end within this many bytes.
_HEAD_BYTES = len(sidebar.MARKER.encode("ascii")) + 2
# The id of the one operation the writer sends; its result never reaches an ``ApplyResult``.
_OPERATION_ID = "wikiops-sidebar"
_TITLE = "_Sidebar"
_IGNORED_BEFORE_WRITE = (
    f"The sidebar was not written: '{layout.SIDEBAR_PAGE}' is ignored by git "
    "(.gitignore or .git/info/exclude), so it would never be committed"
)
_IGNORED_AFTER_PUBLISH = (
    f"The sidebar was not committed: '{layout.SIDEBAR_PAGE}' is ignored by git "
    "(.gitignore or .git/info/exclude)"
)


def _is_page_file(name: str) -> bool:
    """Whether a root file name is a page the sidebar may link to (SB5, G6)."""
    if not name.lower().endswith(_MARKDOWN_SUFFIX) or name.startswith(_EXCLUDED_PREFIX):
        return False
    try:
        layout.validate_page_ref(DocumentRef(provider="", kind=RefKind.PATH, locator={"path": name}))
    except GithubWikiError:
        return False
    return True


def root_pages(git: Git) -> tuple[str, ...]:
    """The page stems of the root of the clone, in exact-name order, as the sidebar lists them.

    A page is a regular ``*.md`` file (any letter case) directly in the root whose name does
    not start with ``_`` and is accepted by the page policy: directories and symbolic links
    (a link could lead out of the clone or nowhere) and names with control characters or
    stray whitespace are not pages. Of several names with one stem (``A.md``, ``A.MD``) the
    first in exact-name order wins. Untracked files git ignores are dropped last, with one
    local ``check-ignore`` (a tracked file is never ignored): they will not be published, so
    linking them would leave a dead link. May raise the git error of that command.
    """
    with os.scandir(git.workdir) as entries:
        names = sorted(
            entry.name
            for entry in entries
            if entry.is_file(follow_symlinks=False) and _is_page_file(entry.name)
        )
    first_by_stem: dict[str, str] = {}
    for name in names:
        first_by_stem.setdefault(sidebar.page_name(name), name)
    ignored = set(git.ignored_paths(tuple(first_by_stem.values())))
    return tuple(stem for stem, name in first_by_stem.items() if name not in ignored)


def _plan_action(workdir: Path) -> SidebarAction:
    if not os.path.lexists(workdir / ".git"):
        return "create"
    try:
        mode = os.lstat(workdir / layout.SIDEBAR_PAGE).st_mode
    except FileNotFoundError:
        return "create"
    if not stat.S_ISREG(mode):  # a directory, or a link: never read through, never written
        return "skipped-unmanaged"
    target = _fs.resolve_within_root(workdir.resolve(), layout.SIDEBAR_PAGE)
    with target.open("rb") as handle:
        head = handle.read(_HEAD_BYTES).split(b"\n", 1)[0]
    try:
        first_line = head.decode("utf-8")
    except UnicodeDecodeError:
        return "skipped-unmanaged"
    return "regenerate" if sidebar.classify(first_line) == "managed" else "skipped-unmanaged"


def plan_action(workdir: Path) -> SidebarAction:
    """What the next apply would do with ``_Sidebar.md``, from the local clone only (SB14).

    No clone yet or no ``_Sidebar.md``: ``create``. A managed file: ``regenerate``. Anything
    else the apply would not touch is ``skipped-unmanaged``: an unmarked file, and, because
    the apply could not write it safely either (G3), a directory, a symbolic link, bytes
    that are not text or a file that cannot be read. At most the marker line is read. Never
    raises and never runs git, so a plan stays offline and cheap.
    """
    try:
        return _plan_action(workdir)
    except (OSError, _fs.FsError, ValueError):
        return "skipped-unmanaged"


@dataclass(frozen=True)
class SidebarReport:
    """What one regeneration did, as warnings the provider attaches to a result (SB13).

    ``generated``: ``(SIDEBAR_PAGE,)`` only when the file was written AND recorded in the
    manifest, so exactly then it joins the paths the publisher stages. ``hint_warnings``:
    one ``sidebar.invalid_hint`` text per ignored hint, in operation order, always kept.
    ``status_warning``: ``sidebar.unmanaged_exists`` or ``sidebar.write_failed`` text.
    """

    generated: tuple[str, ...] = ()
    hint_warnings: tuple[str, ...] = ()
    status_warning: str | None = None


class _Rejected(Exception):
    """The backend answered, but not with a sidebar that was written; the text says why."""


def _write_failed(summary: str, restore: rollback.RollbackResult | None = None) -> str:
    """The one ``sidebar.write_failed`` text: why, plus what the clone still holds, if anything."""
    note = restore.note if restore is not None else ""
    return render_message("sidebar.write_failed", writes.with_note(summary, note) if note else summary)


def _unmanaged_warning() -> str:
    return render_message(
        "sidebar.unmanaged_exists",
        f"The existing '{layout.SIDEBAR_PAGE}' has no managed marker, so it was left as it is",
    )


def _failure_summary(error: Exception) -> str:
    detail = str(error) if isinstance(error, _Rejected) else writes.failure_text(error)
    return f"The sidebar was not written: {detail}"


def _sidebar_ref() -> DocumentRef:
    return DocumentRef(provider="", kind=RefKind.PATH, locator={"path": layout.SIDEBAR_PAGE})


class SidebarWriter:
    """Regenerates the managed ``_Sidebar.md`` of one apply, through the configured backend.

    The write is a separate backend call carrying a copy of the apply's change set with the
    one sidebar operation, so the page results stay untouched and the sidebar never appears in
    an ``ApplyResult``. ``regenerate`` and ``restore_after_publish`` never raise.
    """

    def __init__(
        self, git: Git, backend: DocumentProvider, manifest: PendingManifest, changeset: ChangeSet
    ) -> None:
        self._git = git
        self._backend = backend
        self._manifest = manifest
        self._changeset = changeset
        self._previous: bytes | None = None
        self._ours: bytes | None = None
        self._was_recorded = False

    # -- regenerate ---------------------------------------------------------------

    def regenerate(self, sources: Sequence[sidebar.HintSource]) -> SidebarReport:
        """Bring ``_Sidebar.md`` up to date with the pages of the clone and the ``sources`` hints.

        Invalid hints are reported whatever else happens (A6). An unmarked sidebar is never
        parsed, written or staged (SB4); a sidebar git ignores, a read or write error and a
        link the guard refuses are ``sidebar.write_failed`` (SB12). When the rendered bytes
        equal the file nothing is written (SB11).
        """
        warnings: tuple[str, ...] = ()
        try:
            patches, rejected = sidebar.collect(sources)
            warnings = tuple(sidebar.render_hint_warning(item) for item in rejected)
            return self._regenerate(patches, warnings)
        except Exception as error:  # noqa: BLE001 - the contract: a sidebar problem never fails the apply
            return SidebarReport((), warnings, _write_failed(_failure_summary(error)))

    def _regenerate(
        self, patches: Sequence[tuple[str, sidebar.HintPatch]], warnings: tuple[str, ...]
    ) -> SidebarReport:
        refusal = self._unusable_target()
        if refusal is not None:
            return SidebarReport((), warnings, _write_failed(refusal))
        ref = _sidebar_ref()
        document = self._backend.get_document(ref) if self._backend.exists(ref) else None
        if document is not None and sidebar.classify(document.content) == "unmanaged":
            return SidebarReport((), warnings, _unmanaged_warning())
        if self._git.ignored_paths([layout.SIDEBAR_PAGE]):
            return SidebarReport((), warnings, _write_failed(_IGNORED_BEFORE_WRITE))
        previous = {} if document is None else sidebar.parse(document.content)
        rendered = sidebar.render(root_pages(self._git), sidebar.merge(previous, patches))
        if document is not None and rendered == document.content:
            return SidebarReport((), warnings, None)
        operation: CreateDocumentOperation | UpdateDocumentOperation
        if document is None:
            operation = CreateDocumentOperation(
                operation_id=_OPERATION_ID, ref=ref, title=_TITLE, content=rendered
            )
        else:
            operation = UpdateDocumentOperation(
                operation_id=_OPERATION_ID,
                ref=ref,
                new_content=rendered,
                expected_version=document.version,
            )
        previous_bytes = None if document is None else document.content.encode("utf-8")
        return self._write(operation, previous_bytes, rendered.encode("utf-8"), warnings)

    def _unusable_target(self) -> str | None:
        """Why ``_Sidebar.md`` cannot be written at all, or ``None`` (absent or a regular file)."""
        try:
            mode = os.lstat(self._git.workdir / layout.SIDEBAR_PAGE).st_mode
        except FileNotFoundError:
            return None
        if stat.S_ISREG(mode):
            return None
        return (
            f"The sidebar was not written: '{layout.SIDEBAR_PAGE}' is not a regular file "
            "(a directory or a symbolic link), so it was left as it is"
        )

    def _write(
        self,
        operation: CreateDocumentOperation | UpdateDocumentOperation,
        previous: bytes | None,
        ours: bytes,
        warnings: tuple[str, ...],
    ) -> SidebarReport:
        self._previous, self._ours = previous, ours
        try:
            self._was_recorded = layout.SIDEBAR_PAGE in self._manifest.entries()
            reply = self._backend.apply_changes(
                self._changeset.model_copy(update={"operations": [operation]})
            )
            if not self._verified(reply):
                return SidebarReport((), warnings, None)
            self._manifest.record([layout.SIDEBAR_PAGE])
        except Exception as error:  # noqa: BLE001 - whatever failed, the file goes back as it was
            restore = rollback.restore_file(
                self._git.workdir, layout.SIDEBAR_PAGE, previous=previous, ours=ours
            )
            return SidebarReport((), warnings, _write_failed(_failure_summary(error), restore))
        return SidebarReport((layout.SIDEBAR_PAGE,), warnings, None)

    @staticmethod
    def _verified(reply: ApplyResult) -> bool:
        """Whether the backend reports the sidebar as written; ``False`` for an identical file.

        Trusts only the sidebar operation's own result: its status and the reference it
        resolved (which must be ``_Sidebar.md``, whatever the backend decided to touch).
        """
        result = next((item for item in reply.results if item.operation_id == _OPERATION_ID), None)
        if result is None:
            raise _Rejected("the backend did not report the sidebar operation")
        if result.status is OperationStatus.SKIPPED:
            return False
        if result.status is not OperationStatus.APPLIED:
            reason = Redactor().redact(result.message or "no reason given")
            raise _Rejected(f"the backend could not write it ({reason})")
        if result.resolved_ref is None or layout.validate_page_ref(result.resolved_ref) != layout.SIDEBAR_PAGE:
            raise _Rejected("the backend reported a page other than the sidebar")
        return True

    # -- the post-publish fallback ---------------------------------------------------

    def restore_after_publish(self) -> str:
        """Undo the write after git refused to commit it (an ignore rule appeared late); never raises.

        The pre-write check cannot see a rule that appears between it and the publish. The
        file goes back exactly as it was (only if it still holds what this writer wrote, like
        any restore) and the manifest entry the write added is dropped, or put back to the
        previous bytes when the file was already pending. Returns the ``sidebar.write_failed``
        text for the provider to attach instead of a status.
        """
        restore = None
        manifest_note = ""
        if self._ours is not None:
            restore = rollback.restore_file(
                self._git.workdir, layout.SIDEBAR_PAGE, previous=self._previous, ours=self._ours
            )
            self._ours = None
            if restore.clean:
                manifest_note = self._reconcile_manifest()
        summary = _IGNORED_AFTER_PUBLISH
        return _write_failed(writes.with_note(summary, manifest_note) if manifest_note else summary, restore)

    def _reconcile_manifest(self) -> str:
        """Make the manifest match the restored file; the note says what could not be done."""
        try:
            if self._was_recorded:
                self._manifest.record([layout.SIDEBAR_PAGE])
            else:
                self._manifest.discard([layout.SIDEBAR_PAGE])
        except Exception as error:  # noqa: BLE001 - never raises; the next run reports a stale entry
            return f"the pending manifest could not be updated ({writes.failure_text(error)})"
        return ""
