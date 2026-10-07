"""The publisher: stage, commit and optionally push exactly what wikiops wrote (GW-S8..S13).

``Publisher.publish`` runs inside the workdir lock, after the backend wrote its
files, and turns them into at most one commit and at most one push:

* Exact staging. The stage set is the written paths plus the pending paths that
  still hash like the manifest recorded (wikiops' own earlier leftovers). It is
  staged with ``git --literal-pathspecs add -- <paths>`` and committed with
  ``git --literal-pathspecs commit -- <paths>`` (``--only`` semantics), so a
  foreign staged, ignored or untracked path can never ride along. There is no
  ``add -A``, ``add .`` or ``commit -a`` and no pathless form anywhere.
* Idempotent. ``git status`` decides, before anything is staged, whether a stage path
  differs from ``HEAD``: when none does there is nothing to stage, commit or render, so
  no commit is made and the commit template is never needed.
* Identity per invocation. ``git`` mode adds nothing (git's own user or
  ``commit.identity_missing``); ``bot`` and ``custom`` pass ``GIT_AUTHOR_*`` and
  ``GIT_COMMITTER_*`` in the environment of the one command, so the clone's git
  configuration is never touched. Local commands carry no credentials and keep
  the user's hooks.
* Nothing staged on failure. The commit message is rendered before the first ``add``, so a
  template error (``config.invalid_message``) leaves no staged path behind.
* Honest manifest. Written paths are recorded before anything can fail; committed
  paths are removed; a failed commit leaves them pending with the staged state
  untouched for inspection.
* Push only through the git facade: ``push --porcelain origin
  HEAD:refs/heads/<branch>`` with hooks disabled and a per-call credential, never
  forced, never retried, never preceded by a rebase.

Failures are returned in ``PublishOutcome.error`` (a coded ``GithubWikiError`` that
carries the workdir and, once a commit exists, its short sha) instead of raised,
so the caller can map them onto every operation of the apply and keep the local
commit. Problems with the manifest itself (``workdir.manifest_corrupt``) are raised.
"""

from __future__ import annotations

import string
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from wikiops.providers.github_wiki.classifier import classify
from wikiops.providers.github_wiki.errors import GithubWikiError
from wikiops.providers.github_wiki.git import Git
from wikiops.providers.github_wiki.lock import WorkdirLock
from wikiops.providers.github_wiki.manifest import PendingManifest, parse_status
from wikiops.providers.github_wiki.redaction import Redactor
from wikiops.providers.github_wiki.settings import CommitSettings

_SHA_LENGTH = 7
_WRITTEN_BUT_NOT_COMMITTED = "written but not committed"
# Codes that keep their own name when the commit step fails (never recoded as commit.failed).
_KEPT_COMMIT_CODES = frozenset({"commit.identity_missing", "config.invalid_message"})

PathOrigin = Literal["written", "pending"]


@dataclass(frozen=True)
class PathResult:
    """One staged path: written by this apply or carried over from the manifest."""

    path: str
    origin: PathOrigin
    committed: bool


@dataclass(frozen=True)
class PublishOutcome:
    """What ``Publisher.publish`` did.

    ``sha`` is the commit just made, or ``HEAD`` when only earlier commits were
    pushed; ``None`` when nothing was committed or pushed. ``error`` is set when
    the commit or the push failed (``committed`` tells whether a local commit
    exists and is retained).
    """

    workdir: Path
    branch: str
    sha: str | None
    committed: bool
    pushed: bool
    paths: tuple[PathResult, ...]
    error: GithubWikiError | None = None

    @property
    def note(self) -> str:
        """The suffix for APPLIED operations; empty when nothing was committed or pushed."""
        if self.error is not None or self.sha is None:
            return ""
        short = self.sha[:_SHA_LENGTH]
        if self.pushed:
            return f"committed {short}, pushed to {self.branch}"
        return f"committed locally at {short} in '{self.workdir}'; not pushed"


def render_commit_message(
    template: str, *, plugin_id: str, provider_name: str, page_count: int
) -> str:
    """Render the commit template without shell or ``str.format`` interpretation.

    Only the three placeholders exist; their values are substituted literally
    (braces, quotes, ``$(...)`` and the like stay as they are) and NUL
    characters, which cannot be passed to a process, are dropped.
    """
    values = {
        "plugin_id": plugin_id,
        "provider_name": provider_name,
        "page_count": str(page_count),
    }
    parts: list[str] = []
    for literal, field, _format_spec, _conversion in string.Formatter().parse(template):
        parts.append(literal)
        if field is None:
            continue
        if field not in values:
            raise GithubWikiError(
                "config.invalid_message",
                f"commit.message uses the unknown placeholder '{{{field}}}'",
                context={"message": template},
            )
        parts.append(values[field])
    return "".join(parts).replace("\x00", "")


def _identity_environment(commit: CommitSettings) -> dict[str, str | None]:
    identity = commit.identity
    if identity.mode == "git":
        return {}
    return {
        "GIT_AUTHOR_NAME": identity.name,
        "GIT_AUTHOR_EMAIL": identity.email,
        "GIT_COMMITTER_NAME": identity.name,
        "GIT_COMMITTER_EMAIL": identity.email,
    }


class Publisher:
    """Stages, commits and pushes for one clone; the caller holds the workdir lock."""

    def __init__(
        self,
        git: Git,
        manifest: PendingManifest,
        lock: WorkdirLock,
        *,
        commit: CommitSettings,
        branch: str,
        provider_name: str,
        push: bool,
    ) -> None:
        self._git = git
        self._manifest = manifest
        self._lock = lock
        self._commit = commit
        self._branch = branch
        self._provider_name = provider_name
        self._push = push

    def publish(
        self,
        written: Iterable[str],
        *,
        plugin_id: str,
        page_count: int,
        unpushed: int = 0,
    ) -> PublishOutcome:
        """Commit the written and pending paths, then push when enabled and needed.

        ``unpushed`` is the number of local commits the remote does not have yet
        (the sync state): with push enabled they are pushed even when this apply
        committed nothing new.
        """
        if not self._lock.held:
            raise RuntimeError("publishing needs the workdir lock: call publish() inside lock.hold()")
        written_paths = sorted(set(written))
        self._manifest.record(written_paths)
        try:
            dirty = parse_status(
                self._git.local("status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
            )
        except GithubWikiError as error:
            return self._outcome(
                self._stage_set(written_paths, ()), committed=False, error=self._commit_failure(error)
            )
        self._manifest.prune(dirty)
        dirty_paths = set(dirty)
        pending = self._manifest.classify(dirty).pending
        stage = self._stage_set(written_paths, pending)
        sha: str | None = None
        committed = False
        if stage:
            paths = [item.path for item in stage]
            try:
                committed = self._commit_if_dirty(paths, dirty_paths, plugin_id, page_count)
            except GithubWikiError as error:
                return self._outcome(stage, committed=False, error=self._commit_failure(error))
            # Committed now, or identical to HEAD already: either way nothing stays pending.
            self._manifest.discard(paths)
            if committed:
                try:
                    sha = self._head()
                except GithubWikiError as error:
                    return self._outcome(
                        stage, committed=True, error=self._head_failure(error, committed=True)
                    )
        if not self._push or not (committed or unpushed > 0):
            return self._outcome(stage, committed=committed, sha=sha)
        try:
            sha = sha or self._head()
        except GithubWikiError as error:
            return self._outcome(
                stage, committed=committed, error=self._head_failure(error, committed=committed)
            )
        try:
            self._git.push(self._branch, context={"sha": sha[:_SHA_LENGTH]})
        except GithubWikiError as error:
            return self._outcome(
                stage, committed=committed, sha=sha, error=self._push_failure(error, sha)
            )
        return self._outcome(stage, committed=committed, sha=sha, pushed=True)

    # -- steps ----------------------------------------------------------------------

    @staticmethod
    def _stage_set(written: list[str], pending: Iterable[str]) -> tuple[PathResult, ...]:
        origins: dict[str, PathOrigin] = {path: "pending" for path in pending}
        origins.update({path: "written" for path in written})
        return tuple(PathResult(path, origins[path], False) for path in sorted(origins))

    def _commit_if_dirty(
        self, paths: list[str], dirty: set[str], plugin_id: str, page_count: int
    ) -> bool:
        """Commit ``paths`` when git status says one of them differs; ``False`` when none does.

        One rule: ``git status`` decides, before anything is staged. With no dirty path there is
        nothing to stage, to commit or to render, so an apply that rewrote pages identical to
        HEAD never fails on the commit template. With a dirty path the message is rendered
        FIRST, so a template error leaves nothing staged; then the paths are staged and, unless
        git finds them identical to HEAD after all, committed in one commit.
        """
        if dirty.isdisjoint(paths):
            return False
        message = render_commit_message(
            self._commit.message,
            plugin_id=plugin_id,
            provider_name=self._provider_name,
            page_count=page_count,
        )
        self._git.local("add", paths=paths, op="add")
        staged = self._git.local("diff", "--cached", "--quiet", paths=paths, check=False)
        if staged.timed_out or staged.returncode not in (0, 1):
            raise classify(
                "diff",
                staged,
                redactor=Redactor(),
                context={"workdir": str(self._git.workdir)},
            )
        if staged.returncode == 0:
            return False
        self._git.local(
            "commit", "-m", message, paths=paths, op="commit", env=_identity_environment(self._commit)
        )
        return True

    def _head(self) -> str:
        return self._git.local("rev-parse", "--verify", "HEAD").stdout.strip()

    # -- results ---------------------------------------------------------------------

    def _outcome(
        self,
        stage: tuple[PathResult, ...],
        *,
        committed: bool,
        sha: str | None = None,
        pushed: bool = False,
        error: GithubWikiError | None = None,
    ) -> PublishOutcome:
        return PublishOutcome(
            workdir=self._git.workdir,
            branch=self._branch,
            sha=sha,
            committed=committed,
            pushed=pushed,
            paths=tuple(PathResult(item.path, item.origin, committed) for item in stage),
            error=error,
        )

    def _commit_failure(self, error: GithubWikiError) -> GithubWikiError:
        """The failure as ``commit.failed`` (or its own specific code): nothing was committed."""
        code = error.code if error.code in _KEPT_COMMIT_CODES else "commit.failed"
        return GithubWikiError(
            code,
            f"{error.summary}: {_WRITTEN_BUT_NOT_COMMITTED}",
            context={**error.context, "workdir": str(self._git.workdir)},
            hint=error.hint if error.code == code else None,
        )

    def _head_failure(self, error: GithubWikiError, *, committed: bool) -> GithubWikiError:
        """HEAD could not be read, so nothing is pushed.

        After a commit of this run the commit exists and only its sha is unknown; without one
        (an apply that only carries earlier unpushed commits) nothing was committed here.
        """
        workdir = self._git.workdir
        summary = (
            f"committed locally in '{workdir}' but its sha could not be read (not pushed): {error.summary}"
            if committed
            else f"unpushed local commits in '{workdir}' were not pushed because HEAD could not be read: {error.summary}"
        )
        return GithubWikiError(
            error.code,
            summary,
            context={**error.context, "workdir": str(self._git.workdir)},
            hint=error.hint,
        )

    def _push_failure(self, error: GithubWikiError, sha: str) -> GithubWikiError:
        """The push failure of the same code, saying where the retained local commit is."""
        short = sha[:_SHA_LENGTH]
        where = f"committed locally at {short} in '{self._git.workdir}'"
        summary = (
            f"{where}, push rejected"
            if error.code == "push.rejected"
            else f"{where}, push failed: {error.summary}"
        )
        return GithubWikiError(
            error.code,
            summary,
            context={**error.context, "sha": short, "workdir": str(self._git.workdir)},
            hint=error.hint,
        )
