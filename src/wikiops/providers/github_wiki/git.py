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

A clone is staged in a hidden sibling directory and renamed onto the workdir
only after git succeeded: a failure or timeout never leaves a partial workdir
behind (which a later run would have to classify as a broken clone).
"""

from __future__ import annotations

import contextlib
import os
import secrets
import shutil
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from wikiops.providers.github_wiki.classifier import classify
from wikiops.providers.github_wiki.errors import GithubWikiError
from wikiops.providers.github_wiki.ports import (
    CommandResult,
    CredentialStrategy,
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


# Applied last on every command: a transport or caller override can never
# change the locale the classifier's patterns depend on.
_LOCALE_PIN: dict[str, str | None] = {"LC_ALL": "C", "LANGUAGE": ""}


def _base_environment(workdir: Path) -> dict[str, str | None]:
    environment: dict[str, str | None] = dict.fromkeys(_UNSET_VARIABLES)
    environment.update(_LOCALE_PIN)
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
            env_overrides={**self._base, **(env or {}), **_LOCALE_PIN},
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
        staging = self._workdir.parent / f".{self._workdir.name}.clone-{secrets.token_hex(6)}"
        try:
            staging.mkdir()
        except OSError as exc:
            raise GithubWikiError(
                "workdir.unusable",
                "Cannot create a staging directory next to the workdir for the clone",
                context={"workdir": str(self._workdir), "reason": exc.strerror or str(exc)},
            ) from exc
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
        return result

    def _require_clone_target(self) -> None:
        """The workdir must be missing or an empty directory; nothing is ever overwritten."""
        if self._workdir.is_dir() and not any(self._workdir.iterdir()):
            return
        if not self._workdir.exists() and not self._workdir.is_symlink():
            return
        raise self._not_a_clone_target()

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
            **_LOCALE_PIN,
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
