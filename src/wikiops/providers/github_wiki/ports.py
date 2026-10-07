"""Ports of the ``github_wiki`` provider.

Everything that reaches outside the process goes through one of these seams:
``GitRunner`` executes git (and ``gh``), ``CredentialStrategy`` yields the
per-command ``GitTransport``, and ``BackendResolver`` builds the local file
backend. Adapters implement them; unit tests substitute fakes, so no test needs
a real git process, credential or backend registry.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from wikiops.providers.github_wiki.settings import LocalBackendSettings
from wikiops_sdk.contracts import DocumentProvider


@dataclass(frozen=True)
class CommandResult:
    """Outcome of one executed command; ``timed_out`` means it was killed."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


@runtime_checkable
class GitRunner(Protocol):
    """Runs one command without a shell and without ever prompting.

    ``env_overrides`` is applied on top of the inherited environment: a string
    sets the variable, ``None`` unsets it. ``cwd`` is the only working-directory
    authority (callers never pass ``-C``). A command that exceeds ``timeout``
    seconds is killed and reported with ``timed_out=True``.
    """

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None,
        env_overrides: Mapping[str, str | None],
        timeout: float,
        stdin: str | None = None,
    ) -> CommandResult: ...


@dataclass(frozen=True)
class GitTransport:
    """What one network command needs: the remote, its credentials, its secrets.

    ``secrets`` lists every value that must be masked in text leaving the
    provider; ``label`` names the credential source (never its value).
    """

    remote_url: str
    env_overrides: Mapping[str, str | None]
    secrets: tuple[str, ...]
    label: str


class CredentialStrategy(Protocol):
    """Supplies credentials for exactly one auth mode, fresh for every command.

    ``remote_url`` is the credential-free remote of the mode, derived from the
    settings alone: reading it never runs a command or touches a credential.
    """

    label: str
    remote_url: str

    def check_offline(self) -> None:
        """Fail with an ``auth.*`` / ``config.*`` error when the mode cannot work."""
        ...

    def transport(self) -> GitTransport:
        """Build the transport for ONE network command; never cached."""
        ...


class BackendResolver(Protocol):
    """Creates the local file backend that stores pages and assets in the workdir."""

    def check_offline(self, backend: LocalBackendSettings) -> None:
        """Validate the backend selection without touching the filesystem."""
        ...

    def create(
        self, backend: LocalBackendSettings, *, root: Path, provider_name: str
    ) -> DocumentProvider:
        """Instantiate the backend rooted at ``root`` (the clone's workdir)."""
        ...
