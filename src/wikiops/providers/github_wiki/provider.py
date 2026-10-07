"""``GithubWikiProvider``: wiki policy and git lifecycle around a local file backend.

The provider owns what is specific to a GitHub wiki: the flat page namespace
and link policy (``layout``), keeping the workdir a clean clone of the right
branch (``sync``), and the credential-free description of where it works. All
file I/O is delegated to an inner ``DocumentProvider`` that a ``BackendResolver``
creates lazily, rooted at the workdir, only after the first sync succeeded.

Read side (GW-P6..P8, P9, P10, P11): offline construction and validation, the
static capability set, ``resolve_ref``, ``exists``, ``get_document``, link and
asset-reference building, and ``describe_target``. Write side (GW-P8, P9, S7, S9,
S11, S12): ``put_asset`` and ``apply_changes``, which run under the workdir lock,
re-check the clone for foreign changes, delegate the file I/O to the backend, and
hand exactly the paths the backend reported to the ``Publisher`` (stage, one commit,
optional push). The class is a complete ``DocumentProvider`` now; the provider
stays unregistered until the entry point lands in a later slice.

State is per instance: the sync, the workdir and the backend belong to this
object only, so two profiles never share anything (GW-P13). Page references are
checked against the page policy before any command runs, so a plan with a bad
reference fails early and offline.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from wikiops_sdk.contracts import DocumentProvider
from wikiops_sdk.domain import (
    AppliedOperationResult,
    ApplyResult,
    Asset,
    AssetRef,
    ChangeSet,
    Document,
    DocumentRef,
    ExecutionContext,
    OperationStatus,
    ProviderCapability,
    PutAssetOperation,
)

from wikiops.providers.github_wiki import layout, rollback, writes
from wikiops.providers.github_wiki.errors import GithubWikiError, render_message
from wikiops.providers.github_wiki.git import Git
from wikiops.providers.github_wiki.lock import WorkdirLock
from wikiops.providers.github_wiki.ports import BackendResolver, CredentialStrategy, GitRunner
from wikiops.providers.github_wiki.publisher import Publisher
from wikiops.providers.github_wiki.settings import GithubWikiProviderSettings
from wikiops.providers.github_wiki.sync import SyncPurpose, WikiSync
from wikiops.providers.github_wiki.text import escape_unsafe_characters
from wikiops.providers.github_wiki.workdir import resolve_workdir

PROVIDER_ID = "github_wiki"

# Advertised before any backend exists (the orchestrator asks for capabilities
# before it resolves a single ref), so the set is static. It never includes
# HIERARCHICAL_PAGES: wiki pages are flat.
_CAPABILITIES = frozenset(
    {
        ProviderCapability.READ_DOCUMENT,
        ProviderCapability.CHECK_EXISTS,
        ProviderCapability.CREATE_DOCUMENT,
        ProviderCapability.UPDATE_DOCUMENT,
        ProviderCapability.CREATE_CHILD_DOCUMENT,
        ProviderCapability.BUILD_LINK,
        ProviderCapability.PUT_ASSET,
        ProviderCapability.RESOLVE_BY_PATH,
        ProviderCapability.VERSION_CHECK,
    }
)

_STALE_PLAN_SUMMARY = "Plan read the local clone without fetching; content may be stale"
_FETCH_HEAD = "FETCH_HEAD"


def _flag(value: bool) -> str:
    return "true" if value else "false"


def _quoted(value: object) -> str:
    """Single-quote ``value`` so the note stays parseable whatever the text holds.

    The POSIX idiom: an embedded apostrophe closes the quote, is escaped and
    reopens it (``it's`` becomes ``'it'\\''s'``), so ``shlex.split`` decodes the
    exact text and the next field can never be swallowed. Control and invisible
    characters are made visible first, which keeps the note on one line.
    """
    return "'" + escape_unsafe_characters(str(value)).replace("'", "'\\''") + "'"


def _last_fetch(workdir: Path) -> str | None:
    """When git last fetched into the clone (``FETCH_HEAD`` mtime, UTC ISO), if known."""
    try:
        modified = (workdir / ".git" / _FETCH_HEAD).stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(modified, timezone.utc).isoformat(timespec="seconds")


class GithubWikiProvider:
    """Document provider for a GitHub wiki cloned into a local working directory."""

    provider_id = PROVIDER_ID

    def __init__(
        self,
        settings: GithubWikiProviderSettings,
        *,
        runner: GitRunner,
        credentials: CredentialStrategy,
        backends: BackendResolver,
        which: Callable[[str], Optional[str]] = shutil.which,
        cache_dir: Path | None = None,
        lock_factory: Callable[..., WorkdirLock] = WorkdirLock,
    ) -> None:
        """Store the collaborators; nothing runs and nothing is created here."""
        self.settings = settings
        self._runner = runner
        self._credentials = credentials
        self._backends = backends
        self._which = which
        self._cache_dir = cache_dir
        self._lock_factory = lock_factory
        self._workdir: Path | None = None
        self._git: Git | None = None
        self._sync: WikiSync | None = None
        self._backend: DocumentProvider | None = None

    # -- lazily built collaborators ---------------------------------------------------

    def _resolved_workdir(self) -> Path:
        """The absolute workdir, resolved once; creates nothing, runs nothing."""
        if self._workdir is None:
            self._workdir = resolve_workdir(
                configured=self.settings.workdir,
                host=self.settings.host,
                repository=self.settings.repository,
                provider_name=self.settings.provider_name,
                cache_dir=self._cache_dir,
            )
        return self._workdir

    def _git_facade(self) -> Git:
        if self._git is None:
            self._git = Git(
                self._runner,
                self._credentials,
                workdir=self._resolved_workdir(),
                timeout=self.settings.git_timeout_seconds,
            )
        return self._git

    def _wiki_sync(self) -> WikiSync:
        if self._sync is None:
            self._sync = WikiSync(
                self._git_facade(),
                self._credentials,
                branch=self.settings.branch,
                sync_on_plan=self.settings.sync_on_plan,
                lock_factory=self._lock_factory,
            )
        return self._sync

    def _ready(self, purpose: SyncPurpose) -> DocumentProvider:
        """Sync the clone (once per instance), then return the backend, creating it on first use."""
        self._wiki_sync().ensure_ready(purpose)
        if self._backend is None:
            self._backend = self._backends.create(
                self.settings.local_backend,
                root=self._resolved_workdir(),
                provider_name=self.settings.provider_name,
            )
        return self._backend

    # -- DocumentProvider: configuration -----------------------------------------------

    def capabilities(self) -> set[ProviderCapability]:
        return set(_CAPABILITIES)

    def validate_settings(self) -> None:
        """Offline checks only: git on PATH, the credential mode, the backend selection."""
        if self._which("git") is None:
            raise GithubWikiError("config.git_unavailable", "git was not found on PATH")
        self._credentials.check_offline()
        self._backends.check_offline(self.settings.local_backend)

    # -- DocumentProvider: reads -------------------------------------------------------

    def resolve_ref(
        self, ref: DocumentRef, ctx: Optional[ExecutionContext] = None
    ) -> DocumentRef:
        layout.validate_page_ref(ref)
        return self._ready("plan").resolve_ref(ref, ctx)

    def exists(self, ref: DocumentRef) -> bool:
        layout.validate_page_ref(ref)
        return self._ready("plan").exists(ref)

    def get_document(self, ref: DocumentRef) -> Document:
        layout.validate_page_ref(ref)
        return self._ready("plan").get_document(ref)

    # -- DocumentProvider: links -------------------------------------------------------

    def build_link(self, ref: DocumentRef) -> Optional[str]:
        return layout.build_link(
            ref, host=self.settings.host, repository=self.settings.repository
        )

    def build_asset_reference(self, ref: AssetRef) -> str:
        return layout.build_asset_reference(ref)

    # -- DocumentProvider: writes ------------------------------------------------------

    def put_asset(self, operation: PutAssetOperation, content: bytes) -> Asset:
        """Store one asset under the workdir lock and leave it pending (never committed here).

        Errors propagate: the apply engine reports the asset as failed. The
        reference the backend returns must be a canonical root-relative path
        (``asset.ref_unsupported`` otherwise) because that is all a wiki page can
        link to; only then is the path recorded, with its hash, in the manifest so
        the next committing apply includes it.
        """
        backend = self._ready("apply")
        sync = self._wiki_sync()
        with sync.lock.hold("apply"):
            sync.recheck()
            pending = set(sync.manifest.entries())
            asset = backend.put_asset(operation, content)
            try:
                layout.build_asset_reference(asset.ref)
                sync.manifest.record([asset.ref.locator["path"]])
            except Exception:
                # The backend already wrote the file: do not leave our own orphan behind.
                rollback.roll_back(self._git_facade(), before=pending, keep=())
                raise
        return asset

    def apply_changes(self, changeset: ChangeSet) -> ApplyResult:
        """Write the pages through the backend, then commit and push what it reported.

        Never raises: every failure (sync, lock, dirty clone, backend, commit, push)
        becomes a FAILED result, so the apply engine always gets one result per
        operation. Operations that break the page policy fail on their own and are not
        delegated; the rest go to the backend in ONE call and their results, messages
        included, pass through unchanged. Only paths the backend reports in the result
        fields count as written.
        """
        operations = list(changeset.operations)
        if not operations:
            return self._apply_result([])
        try:
            results = self._write(changeset, operations)
        except Exception as error:  # noqa: BLE001 - an apply reports failures, it never raises
            results = writes.fail_all(operations, error)
        return self._apply_result(results)

    def _apply_result(self, results: list[AppliedOperationResult]) -> ApplyResult:
        return ApplyResult(provider_name=self.settings.provider_name, results=results)

    def _write(self, changeset: ChangeSet, operations: list[Any]) -> list[AppliedOperationResult]:
        backend = self._ready("apply")
        sync = self._wiki_sync()
        with sync.lock.hold("apply"):
            sync.recheck()
            prepared = writes.prepare(operations, provider_name=self.settings.provider_name)
            if not prepared.delegated:
                return writes.in_order(operations, prepared.failures)
            pending = set(sync.manifest.entries())
            reply = backend.apply_changes(
                changeset.model_copy(update={"operations": list(prepared.delegated)})
            )
            settled = self._accepted(sync, prepared.delegated, reply, pending)
            results = writes.in_order(operations, prepared.failures, settled.results)
            if all(item.status is OperationStatus.FAILED for item in results):
                return results  # a failed apply leaves the history as it was
            return self._publish(sync, changeset.plugin_id, settled, results)

    def _accepted(
        self, sync: WikiSync, delegated: tuple[Any, ...], reply: ApplyResult, pending: set[str]
    ) -> writes.Settled:
        """Validate what the backend reported and make the clone match it.

        What the provider refuses is rolled back (it is not wikiops' pending work and would
        block the next run); what it accepts is recorded before anything else can fail, so
        it is never a foreign change at the next run.
        """
        git = self._git_facade()
        try:
            settled = writes.settle(delegated, reply)
            if settled.rejected:
                leftovers = rollback.roll_back(git, before=pending, keep=settled.written)
                settled = writes.note_leftovers(settled, leftovers)
            sync.manifest.record(settled.written)
        except Exception:
            rollback.roll_back(git, before=pending, keep=())
            raise
        return settled

    def _publish(
        self,
        sync: WikiSync,
        plugin_id: str,
        settled: writes.Settled,
        results: list[AppliedOperationResult],
    ) -> list[AppliedOperationResult]:
        """Commit (and push) per the settings and put the outcome on the results."""
        settings = self.settings
        if not settings.allow_auto_commit:
            note = f"written to '{self._resolved_workdir()}', not committed (allow_auto_commit=false)"
            return writes.annotate(results, note=note)
        state = sync.state
        assert state is not None  # _ready("apply") synced
        publisher = Publisher(
            self._git_facade(),
            sync.manifest,
            sync.lock,
            commit=settings.commit,
            branch=state.branch,
            provider_name=settings.provider_name,
            push=settings.allow_auto_push,
        )
        outcome = publisher.publish(
            settled.written,
            plugin_id=plugin_id,
            page_count=len(settled.pages),
            unpushed=sync.unpushed_commits() if settings.allow_auto_push else 0,
        )
        return writes.annotate(results, note=outcome.note, error=outcome.error)

    # -- host hook ---------------------------------------------------------------------

    def describe_target(self) -> str:
        """Describe where this provider works, for the plan note (GW-P11, gap G2).

        Never touches the network, never issues a credential and never creates
        anything. Before a clone exists it runs no command at all; with a clone
        it reads the cached sync state or, failing that, peeks at the clone with
        local read-only git commands. The auth label names the mode, never a
        token or an environment value.
        """
        workdir = self._resolved_workdir()
        state = self._wiki_sync().peek()
        parts = [
            f"remote={_quoted(self._credentials.remote_url)}",
            f"workdir={_quoted(workdir)}",
            f"branch={state.branch if state else 'auto'}",
            f"auth={self._credentials.label}",
            f"auto_commit={_flag(self.settings.allow_auto_commit)}",
            f"auto_push={_flag(self.settings.allow_auto_push)}",
            f"sync_on_plan={_flag(self.settings.sync_on_plan)}",
            f"backend={self.settings.local_backend.type}",
        ]
        if state is not None:
            parts.append(f"pending_paths={state.pending}")
            parts.append(f"unpushed_commits={state.unpushed}")
        if not self.settings.sync_on_plan:
            parts.append(
                render_message(
                    "sync.stale_plan",
                    _STALE_PLAN_SUMMARY,
                    context={"last_sync": _last_fetch(workdir)},
                )
            )
        return " ".join(parts)
