"""``GithubWikiProvider``: wiki policy and git lifecycle around a local file backend.

The provider owns what is specific to a GitHub wiki: the flat page namespace
and link policy (``layout``), keeping the workdir a clean clone of the right
branch (``sync``), and the credential-free description of where it works. All
file I/O is delegated to an inner ``DocumentProvider`` that a ``BackendResolver``
creates lazily, rooted at the workdir, only after the first sync succeeded.

This module is the read side (GW-P6..P8 reads, P9, P10, P11): offline
construction and validation, the static capability set, ``resolve_ref``,
``exists``, ``get_document``, link and asset-reference building, and
``describe_target``. The write side (``put_asset``, ``apply_changes``) joins in
a later slice, so the class is not yet a complete ``DocumentProvider`` and the
provider stays unregistered.

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
from typing import Optional

from wikiops_sdk.contracts import DocumentProvider
from wikiops_sdk.domain import (
    AssetRef,
    Document,
    DocumentRef,
    ExecutionContext,
    ProviderCapability,
)

from wikiops.providers.github_wiki import layout
from wikiops.providers.github_wiki.errors import GithubWikiError, render_message
from wikiops.providers.github_wiki.git import Git
from wikiops.providers.github_wiki.lock import WorkdirLock
from wikiops.providers.github_wiki.ports import BackendResolver, CredentialStrategy, GitRunner
from wikiops.providers.github_wiki.settings import GithubWikiProviderSettings
from wikiops.providers.github_wiki.sync import SyncPurpose, WikiSync
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

    def _wiki_sync(self) -> WikiSync:
        if self._sync is None:
            git = Git(
                self._runner,
                self._credentials,
                workdir=self._resolved_workdir(),
                timeout=self.settings.git_timeout_seconds,
            )
            self._sync = WikiSync(
                git,
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
            f"remote='{self._credentials.remote_url}'",
            f"workdir='{workdir}'",
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
