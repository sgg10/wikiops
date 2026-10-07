"""The sync state machine: get one clone of the wiki ready, safely (GW-S2..S7, S13, S15).

``WikiSync.ensure_ready`` runs at most once per instance and leaves the workdir
holding a clone of exactly the configured wiki, on the resolved branch, free of
foreign changes and fast-forwarded to the remote. It follows these rules:

* Identity first. An existing directory must be the root of a complete clone
  (``rev-parse --show-toplevel`` equals the workdir, ``HEAD`` resolves) whose
  ``origin`` is the credential-free remote derived from the settings, compared
  transport-aware (an SSH origin with an HTTPS profile is a mismatch). Anything
  else is refused untouched; ``origin`` is never rewritten and nothing is deleted.
* The branch comes from the remote's HEAD symref (``ls-remote --symref``), never
  an assumed ``main``/``master``, unless ``branch`` overrides it. A checked-out
  branch other than the resolved one is ``sync.branch_mismatch``; there is no
  checkout, switch or reset anywhere in this module.
* The first clone happens before the workdir lock (the lock lives inside the
  clone). From then on the lock is held for the whole sequence and released at
  its end, also on failure. State files are located through
  ``rev-parse --absolute-git-dir`` so linked worktrees and gitfiles work.
* Foreign dirty paths (everything not recorded in the pending manifest with a
  matching hash) refuse the run with ``workdir.dirty`` before any fetch.
* Sync only fetches and fast-forwards. A non-fast-forwardable history is
  ``sync.diverged`` (no rebase, no merge commit, no data loss); local commits
  ahead of the remote are kept.
* An offline plan (``sync_on_plan: false``) reads the existing clone without a
  single network command, and fails with ``sync.no_local_clone`` when there is
  none, or with ``sync.branch_not_found`` when the branch it would read (override,
  ``origin/HEAD`` or the checked-out fallback) has no remote-tracking ref in the
  clone. Apply always syncs.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from wikiops.providers.github_wiki.classifier import classify, stderr_tail
from wikiops.providers.github_wiki.errors import GithubWikiError
from wikiops.providers.github_wiki.git import Git
from wikiops.providers.github_wiki.lock import WorkdirLock
from wikiops.providers.github_wiki.manifest import PendingManifest, parse_status
from wikiops.providers.github_wiki.ports import CommandResult, CredentialStrategy
from wikiops.providers.github_wiki.redaction import Redactor
from wikiops.providers.github_wiki.workdir import lock_path, manifest_path, prepare_parent

SyncPurpose = Literal["plan", "apply"]

_SHA_LENGTH = 7
_SYMREF_HEAD = re.compile(r"ref: refs/heads/(?P<branch>.+)\tHEAD")
_BRANCH_LINE = re.compile(r"[0-9a-f]{40,64}\trefs/heads/(?P<branch>.+)")
_ORIGIN_HEAD_PREFIX = "refs/remotes/origin/"
_SCP_LIKE = re.compile(r"(?:[^@/:]+@)?(?P<host>[^:/]{2,}):(?P<path>.+)")
_SSH_SCHEMES = {"ssh", "git+ssh", "ssh+git"}
_NOT_SET = "(not set)"
_DETACHED = "(detached HEAD)"


@dataclass(frozen=True)
class SyncState:
    """What a finished sync established about the clone."""

    workdir: Path
    branch: str
    head_sha: str
    offline: bool
    unpushed: int
    pending: int


def _short(sha: str) -> str:
    return sha[:_SHA_LENGTH]


def _parse_ls_remote(stdout: str) -> tuple[str | None, tuple[str, ...]]:
    """The branch HEAD points at (if any) and every branch, from ``ls-remote --symref``."""
    symref: str | None = None
    heads: list[str] = []
    for line in stdout.splitlines():
        symref_match = _SYMREF_HEAD.fullmatch(line)
        if symref_match:
            symref = symref_match["branch"]
            continue
        branch_match = _BRANCH_LINE.fullmatch(line)
        if branch_match:
            heads.append(branch_match["branch"])
    return symref, tuple(heads)


def _trim(path: str) -> str:
    return path.rstrip("/").removesuffix(".git")


def _remote_identity(url: str) -> tuple[str, str, str]:
    """Transport-aware identity of a remote: ``(transport, host, path)``.

    Host and path are case-insensitive for the web transports (GitHub names are);
    ``file`` remotes keep their case. User-info, a trailing slash and a missing
    ``.git`` suffix do not change which wiki a remote names. The ssh URL form and
    the scp-like form are the same transport.
    """
    text = url.strip()
    if "://" in text:
        parts = urlsplit(text)
        scheme = parts.scheme.lower()
        if scheme == "file":
            return ("file", "", _trim(parts.path))
        host = (parts.hostname or "").lower() + (f":{parts.port}" if parts.port else "")
        return ("ssh" if scheme in _SSH_SCHEMES else scheme, host, _trim(parts.path).lstrip("/").lower())
    scp = _SCP_LIKE.fullmatch(text)
    if scp:
        return ("ssh", scp["host"].lower(), _trim(scp["path"]).lstrip("/").lower())
    return ("file", "", _trim(text))


class WikiSync:
    """Prepares one clone for plan or apply; one instance per provider instance."""

    def __init__(
        self,
        git: Git,
        credentials: CredentialStrategy,
        *,
        branch: str | None = None,
        sync_on_plan: bool = True,
        lock_factory: Callable[..., WorkdirLock] = WorkdirLock,
    ) -> None:
        self._git = git
        self._credentials = credentials
        self._branch_override = branch
        self._sync_on_plan = sync_on_plan
        self._lock_factory = lock_factory
        self._state: SyncState | None = None
        self._lock: WorkdirLock | None = None
        self._manifest: PendingManifest | None = None

    # -- results ---------------------------------------------------------------

    @property
    def state(self) -> SyncState | None:
        """The state of the last successful sync, or ``None`` before the first."""
        return self._state

    @property
    def lock(self) -> WorkdirLock:
        """The workdir lock of the synced clone (available after ``ensure_ready``)."""
        if self._lock is None:
            raise RuntimeError("the workdir is not synced yet: call ensure_ready() first")
        return self._lock

    @property
    def manifest(self) -> PendingManifest:
        """The pending manifest of the synced clone (available after ``ensure_ready``)."""
        if self._manifest is None:
            raise RuntimeError("the workdir is not synced yet: call ensure_ready() first")
        return self._manifest

    # -- entry point -------------------------------------------------------------

    def ensure_ready(self, purpose: SyncPurpose) -> SyncState:
        """Sync once per instance and return the resulting state.

        A cached offline result is replaced by a real sync when apply asks for
        it (apply always syncs); a failed attempt is never cached.
        """
        offline = purpose == "plan" and not self._sync_on_plan
        if self._state is not None and (offline or not self._state.offline):
            return self._state
        state = self._offline_sync() if offline else self._online_sync()
        self._state = state
        return state

    def peek(self) -> SyncState | None:
        """Describe an existing clone without changing anything (for ``describe_target``).

        Returns the cached state after a sync. Otherwise, when the workdir already
        holds a clone, verifies its identity and reads the branch, the unpushed
        count and the pending paths with local read-only git commands: no network,
        no credential, no lock and no write, and the instance is not marked as
        synced. ``None`` when there is no clone yet (nothing is run) or when no
        branch can be named (detached HEAD without an override or ``origin/HEAD``).
        """
        if self._state is not None:
            return self._state
        if not (self._git.workdir / ".git").exists():
            return None
        self._verify_identity()
        self._open_state_files()
        branch = self._branch_override or self._origin_head() or self._current_branch()
        if branch is None:
            return None
        self._require_tracking_ref(branch)
        return self._snapshot(branch, offline=True)

    # -- the two paths -----------------------------------------------------------

    def _offline_sync(self) -> SyncState:
        workdir = self._git.workdir
        if self._needs_clone():
            raise GithubWikiError(
                "sync.no_local_clone",
                "sync_on_plan is false and the workdir has no local clone to read",
                context={"workdir": str(workdir)},
            )
        self._verify_identity()
        self._open_state_files()
        with self.lock.hold("plan"):
            branch = self._require_checked_out(
                self._branch_override or self._origin_head() or self._current_branch()
            )
            self._require_tracking_ref(branch)
            self._require_no_foreign_changes()
            return self._snapshot(branch, offline=True)

    def _online_sync(self) -> SyncState:
        workdir = self._git.workdir
        needs_clone = self._needs_clone()
        if not needs_clone:
            self._verify_identity()  # before any network command or credential use
        prepare_parent(workdir)
        branch = self._remote_branch()
        if needs_clone:
            self._git.clone(branch)
        self._open_state_files()
        with self.lock.hold("sync"):
            self._require_checked_out(branch)
            self._require_no_foreign_changes()
            self._git.fetch(branch)
            self._fast_forward(branch)
            return self._snapshot(branch, offline=False)

    # -- workdir identity ----------------------------------------------------------

    def _needs_clone(self) -> bool:
        """Whether the workdir is missing or an empty directory (nothing there to verify)."""
        workdir = self._git.workdir
        if not workdir.exists():
            return True
        if not workdir.is_dir():
            raise GithubWikiError(
                "workdir.unusable",
                "The workdir exists but is not a directory",
                context={"workdir": str(workdir)},
            )
        return not any(workdir.iterdir())

    def _probe(self, *args: str) -> CommandResult:
        """A local git command whose failure the caller interprets; a timeout still raises."""
        result = self._git.local(*args, check=False)
        if result.timed_out:
            raise classify(
                args[0], result, redactor=Redactor(), context={"workdir": str(self._git.workdir)}
            )
        return result

    def _not_a_clone(self, reason: str) -> GithubWikiError:
        return GithubWikiError(
            "sync.workdir_not_clone",
            f"The workdir is not a complete clone of its own ({reason})",
            context={"workdir": str(self._git.workdir)},
        )

    def _verify_identity(self) -> None:
        """The directory is the root of a complete clone of THIS wiki, else refuse untouched."""
        workdir = self._git.workdir
        top = self._probe("rev-parse", "--show-toplevel")
        if top.returncode != 0:
            raise self._not_a_clone("it is not a git repository")
        if os.path.realpath(top.stdout.strip()) != os.path.realpath(workdir):
            raise self._not_a_clone("its repository root is another directory")
        if self._probe("rev-parse", "--verify", "HEAD").returncode != 0:
            raise self._not_a_clone("it has no commit, so a clone was interrupted")
        origin = self._probe("config", "--get", "remote.origin.url")
        actual = origin.stdout.strip() if origin.returncode == 0 else ""
        expected = self._credentials.remote_url
        if _remote_identity(actual) != _remote_identity(expected):
            raise GithubWikiError(
                "sync.remote_mismatch",
                "The existing clone's origin is not the wiki this profile points at",
                context={
                    "workdir": str(workdir),
                    "expected": expected,
                    "origin": Redactor().redact(actual) or _NOT_SET,
                },
            )

    def _open_state_files(self) -> None:
        """Locate the manifest and the lock inside the git directory git reports."""
        workdir = self._git.workdir
        git_dir = Path(self._git.local("rev-parse", "--absolute-git-dir").stdout.strip())
        self._manifest = PendingManifest(manifest_path(git_dir), workdir=workdir)
        self._lock = self._lock_factory(lock_path(git_dir), workdir=workdir)

    # -- branch -----------------------------------------------------------------------

    def _remote_branch(self) -> str:
        """Resolve the branch from the remote: the override if it exists, else HEAD's target."""
        symref, heads = _parse_ls_remote(self._git.ls_remote().stdout)
        available = ", ".join(heads) or "(none)"
        if self._branch_override is not None:
            if self._branch_override in heads:
                return self._branch_override
            raise GithubWikiError(
                "sync.branch_not_found",
                f"Branch '{self._branch_override}' does not exist on the remote",
                context={"available": available},
            )
        if symref is None or symref not in heads:
            raise GithubWikiError(
                "sync.branch_not_found",
                "The remote has no HEAD branch to detect the wiki branch from",
                context={"available": available},
            )
        return symref

    def _origin_head(self) -> str | None:
        result = self._probe("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
        target = result.stdout.strip()
        if result.returncode != 0 or not target.startswith(_ORIGIN_HEAD_PREFIX):
            return None
        return target.removeprefix(_ORIGIN_HEAD_PREFIX)

    def _current_branch(self) -> str | None:
        result = self._probe("symbolic-ref", "--quiet", "--short", "HEAD")
        branch = result.stdout.strip()
        return branch if result.returncode == 0 and branch else None

    def _require_checked_out(self, expected: str | None) -> str:
        """Return ``expected``; refuse when the clone is on another branch or detached.

        Never switches: the user decides which branch the workdir lives on.
        """
        actual = self._current_branch()
        if expected is None or actual != expected:
            raise GithubWikiError(
                "sync.branch_mismatch",
                "The clone is not on the branch the wiki resolves to",
                context={
                    "workdir": str(self._git.workdir),
                    "expected": expected or "unknown",
                    "actual": actual or _DETACHED,
                },
            )
        return expected

    def _require_tracking_ref(self, branch: str) -> None:
        """Refuse a branch the clone never fetched (no ``refs/remotes/origin/<branch>``).

        Only the offline paths need it: they read the clone without fetching, so a
        branch chosen from the override or the checked-out fallback may name nothing
        the clone knows about, and every number computed against it would be fiction.
        """
        reference = f"{_ORIGIN_HEAD_PREFIX}{branch}"
        if self._probe("rev-parse", "--verify", "--quiet", reference).returncode == 0:
            return
        listing = self._probe("for-each-ref", "--format=%(refname:lstrip=3)", "refs/remotes/origin")
        known = sorted(name for name in listing.stdout.split() if name != "HEAD")
        raise GithubWikiError(
            "sync.branch_not_found",
            f"Branch '{branch}' has no remote-tracking ref in the local clone, so an offline plan cannot read it",
            context={"workdir": str(self._git.workdir), "available": ", ".join(known) or "(none)"},
            hint="run once with sync_on_plan: true (or apply) so the clone fetches the branch, or set 'branch' to one the clone has fetched",
        )

    # -- dirty policy, fetch, fast-forward -----------------------------------------------

    def _require_no_foreign_changes(self) -> None:
        """Refuse foreign dirty paths; forget manifest entries that are no longer dirty."""
        status = self._git.local("status", "--porcelain=v1", "-z", "--untracked-files=all")
        dirty = parse_status(status.stdout)
        self.manifest.check(dirty)
        self.manifest.prune(dirty)

    def _fast_forward(self, branch: str) -> None:
        result = self._git.local("merge", "--ff-only", f"origin/{branch}", check=False)
        if result.timed_out or result.returncode != 0:
            raise self._merge_error(result, branch)

    def _merge_error(self, result: CommandResult, branch: str) -> GithubWikiError:
        workdir = str(self._git.workdir)
        if result.timed_out:
            return classify("merge", result, redactor=Redactor(), context={"workdir": workdir})
        text = f"{result.stderr}\n{result.stdout}".lower()
        if "would be overwritten" in text:
            return GithubWikiError(
                "workdir.dirty",
                "git refused to fast-forward because local changes would be overwritten",
                context={"workdir": workdir, "reason": stderr_tail(result.stderr, Redactor())},
            )
        if "not possible to fast-forward" in text:
            head = self._probe("rev-parse", "--verify", "HEAD")
            local = _short(head.stdout.strip()) if head.returncode == 0 else "unknown"
            return GithubWikiError(
                "sync.diverged",
                "The local history and the remote branch have diverged",
                context={"workdir": workdir, "branch": branch, "local": local},
            )
        return classify("merge", result, redactor=Redactor(), context={"workdir": workdir})

    # -- result ---------------------------------------------------------------------------

    def _snapshot(self, branch: str, *, offline: bool) -> SyncState:
        head = self._git.local("rev-parse", "--verify", "HEAD").stdout.strip()
        count = self._git.local("rev-list", "--count", f"origin/{branch}..HEAD").stdout.strip()
        try:
            unpushed = int(count)
        except ValueError as exc:
            raise GithubWikiError(
                "sync.git_failed",
                "git rev-list returned an unexpected count",
                context={"op": "rev-list", "workdir": str(self._git.workdir)},
            ) from exc
        return SyncState(
            workdir=self._git.workdir,
            branch=branch,
            head_sha=head,
            offline=offline,
            unpushed=unpushed,
            pending=len(self.manifest),
        )
