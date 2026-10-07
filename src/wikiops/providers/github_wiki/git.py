"""The ``Git`` facade: every git command of the provider, in two safe flavors.

Local commands (status, add, commit, merge, rev-parse, ...) run with the user's
own git configuration and never receive credentials. Network commands
(``ls-remote``, ``clone``, ``fetch``, ``push``) get a fresh per-command
transport from the credential strategy, run with hooks disabled through an
empty temporary ``core.hooksPath`` (so no hook can observe a credential) and
with any inherited ``GIT_CONFIG_PARAMETERS`` dropped so no user ``-c`` entry can
override the hooks path or the credential entries.

Every command runs with the repository-selection variables unset, the locale
pinned to ``C`` (stable stderr for the classifier) and ``GIT_CEILING_DIRECTORIES``
at the workdir's parent, so git can never wander into an enclosing repository.
``cwd`` is the only working-directory authority; ``-C`` is never used. Failures
are classified into coded errors with a redacted stderr tail.

A clone is staged in a hidden sibling directory (``.<workdir>.clone-<token>``)
and renamed onto the workdir only after git succeeded: a failure or timeout
never leaves a partial workdir behind (which a later run would have to classify
as a broken clone). The running clone holds an advisory lock on its staging
directory, so a killed process (which cannot clean up) leaves a directory that
the next clone of the same workdir recognizes as abandoned and removes: only
names of exactly that shape, only real directories, only when nobody holds the
lock and the directory is older than a grace period.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import shutil
import stat
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from wikiops.providers.github_wiki.classifier import classify
from wikiops.providers.github_wiki.errors import GithubWikiError
from wikiops.providers.github_wiki.ports import (
    CommandResult,
    CredentialStrategy,
    LOCALE_PIN,
    GitRunner,
)
from wikiops.providers.github_wiki.redaction import Redactor

_UNSET_VARIABLES = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_COMMON_DIR",
    "GIT_PREFIX",
)
_HOOKS_DIRECTORY_PREFIX = "wikiops-nohooks-"
_STAGING_TOKEN_BYTES = 6
# An unlocked staging directory younger than this may belong to a clone that has
# created it but not locked it yet; only older ones are treated as abandoned.
_STALE_STAGING_SECONDS = 600.0


def _try_lock_directory(path: Path) -> int | None:
    """Take an exclusive advisory lock on the directory ``path`` without waiting.

    Returns the open descriptor (closing it drops the lock), or ``None`` when
    another holder has it, the directory cannot be opened (never followed through
    a symlink) or the platform or file system cannot lock directories. ``None``
    therefore never proves a directory is free, only that it was not locked here.
    """
    if os.name != "posix":  # pragma: no cover - directory locks are POSIX-only
        return None
    import fcntl

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return None
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(descriptor)
        return None
    return descriptor


def _base_environment(workdir: Path) -> dict[str, str | None]:
    environment: dict[str, str | None] = dict.fromkeys(_UNSET_VARIABLES)
    environment.update(LOCALE_PIN)
    environment["GIT_CEILING_DIRECTORIES"] = str(workdir.parent)
    return environment


class Git:
    """Runs git for one clone through a ``GitRunner`` and a ``CredentialStrategy``."""

    def __init__(
        self,
        runner: GitRunner,
        credentials: CredentialStrategy,
        *,
        workdir: Path,
        timeout: float,
    ) -> None:
        self._runner = runner
        self._credentials = credentials
        self._workdir = workdir
        self._timeout = timeout
        self._base = _base_environment(workdir)

    @property
    def workdir(self) -> Path:
        return self._workdir

    # -- local commands ------------------------------------------------------

    def local(
        self,
        *args: str,
        paths: Sequence[str] | None = None,
        op: str | None = None,
        env: Mapping[str, str | None] | None = None,
        stdin: str | None = None,
        check: bool = True,
        context: Mapping[str, object] | None = None,
    ) -> CommandResult:
        """Run a local git command in the workdir; it never carries credentials.

        ``paths`` become literal pathspecs after ``--``. With ``check`` a failed
        command raises the classified error for ``op`` (default: the
        subcommand); without it the caller interprets the result.
        """
        if not args:
            raise ValueError("a git subcommand is required")
        argv: tuple[str, ...]
        if paths is None:
            argv = ("git", *args)
        elif not paths:
            raise ValueError("paths must not be empty: a pathless command acts on everything")
        else:
            argv = ("git", "--literal-pathspecs", *args, "--", *paths)
        result = self._runner.run(
            argv,
            cwd=self._workdir,
            env_overrides={**self._base, **(env or {}), **LOCALE_PIN},
            timeout=self._timeout,
            stdin=stdin,
        )
        if check and _failed(result):
            raise classify(
                op or args[0],
                result,
                redactor=Redactor(),
                context={"workdir": str(self._workdir), **(context or {})},
            )
        return result

    def ignored_paths(self, paths: Sequence[str]) -> tuple[str, ...]:
        """Which of ``paths`` an ignore rule (``.gitignore``, ``info/exclude``, ...) hides.

        Names are literal (read from stdin, NUL-separated, so no pathspec magic and no
        quoting applies) and the answer keeps the order given. A path git already tracks
        is never ignored (no ``--no-index``: a clean, tracked page that happens to match a
        pattern is an ordinary page); only untracked paths can be hidden, which is exactly
        what ``git status`` omits and ``git add`` refuses.
        """
        if not paths:
            return ()
        result = self.local(
            "check-ignore", "--stdin", "-z", stdin="".join(f"{path}\0" for path in paths), check=False
        )
        if result.timed_out or result.returncode not in (0, 1):  # 1: none of them is ignored
            raise classify(
                "check-ignore",
                result,
                redactor=Redactor(),
                context={"workdir": str(self._workdir)},
            )
        found = {entry for entry in result.stdout.split("\0") if entry}
        return tuple(path for path in paths if path in found)

    # -- network commands ----------------------------------------------------

    def ls_remote(self, *, context: Mapping[str, object] | None = None) -> CommandResult:
        """``ls-remote --symref <url> HEAD refs/heads/*`` (from the workdir's parent)."""
        return self._network(
            "ls-remote",
            lambda remote: ("--symref", remote, "HEAD", "refs/heads/*"),
            cwd=self._workdir.parent,
            context=context,
        )

    def clone(self, branch: str, *, context: Mapping[str, object] | None = None) -> CommandResult:
        """Clone ``branch`` into the workdir (its parent must exist).

        git clones into a fresh sibling directory (same filesystem), which is
        renamed onto the workdir once the clone succeeded; on any failure the
        staging directory is removed and the workdir is left as it was. A
        workdir that is not an empty directory is refused before any network
        command runs (``sync.workdir_not_clone``).
        """
        self._require_clone_target()
        self._remove_abandoned_staging()
        staging = self._workdir.parent / f"{self._staging_prefix()}{secrets.token_hex(_STAGING_TOKEN_BYTES)}"
        try:
            staging.mkdir()
        except OSError as exc:
            raise GithubWikiError(
                "workdir.unusable",
                "Cannot create a staging directory next to the workdir for the clone",
                context={"workdir": str(self._workdir), "reason": exc.strerror or str(exc)},
            ) from exc
        lock = _try_lock_directory(staging)
        try:
            result = self._network(
                "clone",
                lambda remote: (
                    "--no-tags",
                    "--origin",
                    "origin",
                    "--branch",
                    branch,
                    "--",
                    remote,
                    str(staging),
                ),
                cwd=self._workdir.parent,
                context=context,
            )
            self._publish_clone(staging)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        finally:
            if lock is not None:
                os.close(lock)
        return result

    def _staging_prefix(self) -> str:
        return f".{self._workdir.name}.clone-"

    def _remove_abandoned_staging(self) -> None:
        """Remove staging directories that crashed clones of this workdir left behind.

        Best effort and conservative: anything that is not provably abandoned
        (another name, a symlink, a file, a recent directory, a held lock) stays.
        """
        pattern = re.compile(re.escape(self._staging_prefix()) + f"[0-9a-f]{{{2 * _STAGING_TOKEN_BYTES}}}")
        try:
            siblings = list(self._workdir.parent.iterdir())
        except OSError:
            return
        now = time.time()
        for entry in siblings:
            if not pattern.fullmatch(entry.name):
                continue
            try:
                info = entry.lstat()
            except OSError:
                continue
            if not stat.S_ISDIR(info.st_mode) or now - info.st_mtime < _STALE_STAGING_SECONDS:
                continue
            lock = _try_lock_directory(entry)
            if lock is None:
                continue
            try:
                shutil.rmtree(entry, ignore_errors=True)
            finally:
                os.close(lock)

    def _require_clone_target(self) -> None:
        """The workdir must be missing or an empty real directory; nothing is ever overwritten."""
        if self._workdir.is_symlink():
            raise self._symlinked_workdir()
        if self._workdir.is_dir() and not any(self._workdir.iterdir()):
            return
        if not self._workdir.exists():
            return
        raise self._not_a_clone_target()

    def _symlinked_workdir(self) -> GithubWikiError:
        return GithubWikiError(
            "workdir.unusable",
            "The workdir is a symbolic link; a clone is never published through a link",
            context={"workdir": str(self._workdir)},
            hint="set workdir to the real directory path instead of a symbolic link",
        )

    def _not_a_clone_target(self) -> GithubWikiError:
        return GithubWikiError(
            "sync.workdir_not_clone",
            "The workdir exists and is not an empty directory, so it cannot be cloned into",
            context={"workdir": str(self._workdir)},
        )

    def _publish_clone(self, staging: Path) -> None:
        """Rename the finished clone onto the workdir; a workdir that filled up wins."""
        if os.name != "posix":  # pragma: no cover - Windows cannot rename onto an existing directory
            with contextlib.suppress(OSError):
                self._workdir.rmdir()
        if self._workdir.is_symlink():
            raise self._symlinked_workdir()
        try:
            os.replace(staging, self._workdir)
        except OSError as exc:
            raise self._not_a_clone_target() from exc

    def fetch(self, branch: str, *, context: Mapping[str, object] | None = None) -> CommandResult:
        """Fetch ``branch`` into its remote-tracking ref; tags are never fetched."""
        return self._network(
            "fetch",
            lambda _remote: (
                "--no-tags",
                "origin",
                f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
            ),
            cwd=self._workdir,
            context=context,
        )

    def push(self, branch: str, *, context: Mapping[str, object] | None = None) -> CommandResult:
        """Push ``HEAD`` to ``refs/heads/<branch>``: explicit ref, porcelain, never forced."""
        return self._network(
            "push",
            lambda _remote: ("--porcelain", "origin", f"HEAD:refs/heads/{branch}"),
            cwd=self._workdir,
            context=context,
        )

    def _network(
        self,
        op: str,
        build_args: Callable[[str], Sequence[str]],
        *,
        cwd: Path,
        context: Mapping[str, object] | None,
    ) -> CommandResult:
        transport = self._credentials.transport()
        redactor = Redactor(transport.secrets)
        environment = {
            **self._base,
            **transport.env_overrides,
            **LOCALE_PIN,
            "GIT_CONFIG_PARAMETERS": None,
        }
        with tempfile.TemporaryDirectory(prefix=_HOOKS_DIRECTORY_PREFIX) as hooks_directory:
            argv = (
                "git",
                "-c",
                f"core.hooksPath={hooks_directory}",
                op,
                *build_args(transport.remote_url),
            )
            result = self._runner.run(
                argv, cwd=cwd, env_overrides=environment, timeout=self._timeout
            )
        if _failed(result):
            raise classify(
                op,
                result,
                redactor=redactor,
                context={
                    "workdir": str(self._workdir),
                    "remote": transport.remote_url,
                    "auth": transport.label,
                    **(context or {}),
                },
            )
        return result


def _failed(result: CommandResult) -> bool:
    return result.timed_out or result.returncode != 0
