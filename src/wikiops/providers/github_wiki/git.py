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
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from wikiops.providers.github_wiki.classifier import classify
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


def _base_environment(workdir: Path) -> dict[str, str | None]:
    environment: dict[str, str | None] = dict.fromkeys(_UNSET_VARIABLES)
    environment.update(
        LC_ALL="C",
        LANGUAGE="",
        GIT_CEILING_DIRECTORIES=str(workdir.parent),
    )
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
            env_overrides={**self._base, **(env or {})},
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
        """Clone ``branch`` into the workdir (from its parent, which must exist)."""
        return self._network(
            "clone",
            lambda remote: (
                "--no-tags",
                "--origin",
                "origin",
                "--branch",
                branch,
                "--",
                remote,
                str(self._workdir),
            ),
            cwd=self._workdir.parent,
            context=context,
        )

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
