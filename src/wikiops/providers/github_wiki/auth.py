"""Credential strategies of the ``github_wiki`` provider (GW-A2..A6, GW-A9).

Exactly one strategy serves a provider: ``env``, ``gh``, ``ssh`` or ``ambient``.
A strategy derives the credential-free remote from the settings, checks offline
that its mode can work, and builds a fresh ``GitTransport`` for every network
command: nothing is cached, so two profiles in one process never share a token.
HTTPS modes inject the token through ``GIT_CONFIG_COUNT/KEY/VALUE`` environment
entries appended after the user's own, so it never reaches argv, a URL or any
file git writes. Every transport disables git's terminal prompt, so a missing or
rejected credential fails fast instead of waiting for input.
"""

from __future__ import annotations

import base64
import os
import shlex
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path

from wikiops.providers.github_wiki.classifier import stderr_tail
from wikiops.providers.github_wiki.errors import GithubWikiError
from wikiops.providers.github_wiki.ports import GitRunner, GitTransport
from wikiops.providers.github_wiki.redaction import Redactor
from wikiops.providers.github_wiki.settings import (
    AmbientAuth,
    EnvAuth,
    GhAuth,
    GithubWikiProviderSettings,
    SshAuth,
)

_TOKEN_USER = "x-access-token"
_SSH_CONNECT_TIMEOUT_SECONDS = 15
# ``gh auth token`` reads a local credential store: it is quick or it is stuck,
# so it never gets the (much longer) budget of a network git command.
GH_TOKEN_TIMEOUT_SECONDS = 15.0
# Never prompt for credentials on a terminal, on any transport.
_NO_PROMPT = {"GIT_TERMINAL_PROMPT": "0"}
# Variables that could expose or replace a credential on a network command.
_ISOLATED_VARIABLES = (
    "GIT_ASKPASS",
    "SSH_ASKPASS",
    "GIT_CURL_VERBOSE",
    "GIT_TRACE",
    "GIT_TRACE_CURL",
    "GIT_TRACE_CURL_NO_DATA",
    "GIT_TRACE_PACKET",
    "GIT_TRACE_PACK_ACCESS",
    "GIT_TRACE_PERFORMANCE",
    "GIT_TRACE_SETUP",
    "GIT_TRACE_SHALLOW",
    "GIT_TRACE_REFS",
    "GIT_TRACE2",
    "GIT_TRACE2_EVENT",
    "GIT_TRACE2_PERF",
)
_TRACE_PREFIX = "GIT_TRACE"

Which = Callable[[str], str | None]


def _https_remote(host: str, repository: str) -> str:
    return f"https://{host}/{repository}.wiki.git"


def _ssh_remote(host: str, repository: str) -> str:
    return f"git@{host}:{repository}.wiki.git"


def _inherited_config_count(environ: Mapping[str, str]) -> int:
    raw = environ.get("GIT_CONFIG_COUNT", "")
    return int(raw) if raw.isascii() and raw.isdigit() else 0


def _isolation_overrides(environ: Mapping[str, str]) -> dict[str, str | None]:
    names = set(_ISOLATED_VARIABLES)
    names.update(name for name in environ if name.startswith(_TRACE_PREFIX))
    return dict.fromkeys(sorted(names))


def _https_transport(
    remote_url: str, label: str, host: str, token: str, environ: Mapping[str, str]
) -> GitTransport:
    """Token transport: an extra HTTPS header for ``host`` and no credential helper.

    ``http.<url>.extraheader`` is multi-valued, so the entry is preceded by an
    empty value for the same key: git clears every header collected so far,
    and another profile's ``Authorization`` header (from a config file or an
    inherited ``GIT_CONFIG_*`` entry) can never be sent alongside this token.
    """
    pair = f"{_TOKEN_USER}:{token}"
    basic = base64.b64encode(pair.encode("utf-8")).decode("ascii")
    first = _inherited_config_count(environ)
    header_key = f"http.https://{host}/.extraheader"
    overrides: dict[str, str | None] = {
        **_isolation_overrides(environ),
        **_NO_PROMPT,
        "GIT_CONFIG_COUNT": str(first + 3),
        f"GIT_CONFIG_KEY_{first}": header_key,
        f"GIT_CONFIG_VALUE_{first}": "",
        f"GIT_CONFIG_KEY_{first + 1}": header_key,
        f"GIT_CONFIG_VALUE_{first + 1}": f"AUTHORIZATION: basic {basic}",
        f"GIT_CONFIG_KEY_{first + 2}": "credential.helper",
        f"GIT_CONFIG_VALUE_{first + 2}": "",
    }
    return GitTransport(remote_url, overrides, (token, pair, basic), label)


class EnvTokenStrategy:
    """Token read from an environment variable at every network command."""

    def __init__(
        self,
        host: str,
        repository: str,
        variable: str,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._host = host
        self._variable = variable
        self._environ = os.environ if environ is None else environ
        self.remote_url = _https_remote(host, repository)
        self.label = f"env:{variable}"

    def _token(self) -> str:
        token = self._environ.get(self._variable, "").strip()
        if not token:
            raise GithubWikiError(
                "auth.env_missing",
                "The environment variable that holds the token is unset or empty",
                context={"variable": self._variable},
            )
        return token

    def check_offline(self) -> None:
        self._token()

    def transport(self) -> GitTransport:
        return _https_transport(
            self.remote_url, self.label, self._host, self._token(), self._environ
        )


class GhTokenStrategy:
    """Token requested from the GitHub CLI for one explicit account, per command."""

    def __init__(
        self,
        host: str,
        repository: str,
        account: str,
        *,
        runner: GitRunner,
        timeout: float = GH_TOKEN_TIMEOUT_SECONDS,
        which: Which = shutil.which,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._host = host
        self._account = account
        self._runner = runner
        self._timeout = timeout
        self._which = which
        self._environ = os.environ if environ is None else environ
        self.remote_url = _https_remote(host, repository)
        self.label = f"gh:{account}"

    def check_offline(self) -> None:
        if self._which("gh") is None:
            raise GithubWikiError(
                "auth.gh_unavailable", "The GitHub CLI (gh) is not on PATH"
            )

    def _fetch_token(self) -> str:
        result = self._runner.run(
            (
                "gh",
                "auth",
                "token",
                "--user",
                self._account,
                "--hostname",
                self._host,
            ),
            cwd=None,
            env_overrides={"GH_PROMPT_DISABLED": "1"},
            timeout=self._timeout,
        )
        token = result.stdout.strip()
        usable = bool(token) and not any(char.isspace() for char in token)
        if result.timed_out:
            reason = "timed out"
        elif result.returncode != 0:
            reason = f"exit code {result.returncode}"
        elif not token:
            reason = "empty output"
        elif not usable:
            reason = "unexpected output"
        else:
            return token
        redactor = Redactor((token,) if usable else ())
        raise GithubWikiError(
            "auth.gh_failed",
            "gh could not provide a token for the configured account",
            context={
                "account": self._account,
                "host": self._host,
                "reason": reason,
                "stderr": stderr_tail(result.stderr, redactor) or None,
            },
        )

    def transport(self) -> GitTransport:
        return _https_transport(
            self.remote_url, self.label, self._host, self._fetch_token(), self._environ
        )


class SshStrategy:
    """SSH remote; ``key_path`` optionally pins one identity file (never read)."""

    def __init__(
        self, host: str, repository: str, key_path: str | None = None
    ) -> None:
        self._configured_key = key_path
        self._key = (
            None if key_path is None else os.path.abspath(os.path.expanduser(key_path))
        )
        self.remote_url = _ssh_remote(host, repository)
        self.label = "ssh" if key_path is None else f"ssh:{key_path}"

    def check_offline(self) -> None:
        if self._key is not None and not Path(self._key).is_file():
            raise GithubWikiError(
                "config.key_path_missing",
                "The configured ssh key file does not exist",
                context={"key_path": self._configured_key},
            )

    def transport(self) -> GitTransport:
        command = f"ssh -o BatchMode=yes -o ConnectTimeout={_SSH_CONNECT_TIMEOUT_SECONDS}"
        if self._key is not None:
            command += f" -i {shlex.quote(self._key)} -o IdentitiesOnly=yes"
        overrides: dict[str, str | None] = {
            **_NO_PROMPT,
            "GIT_SSH_COMMAND": command,
            "GIT_SSH_VARIANT": "ssh",
        }
        return GitTransport(self.remote_url, overrides, (), self.label)


class AmbientStrategy:
    """No credentials added: git uses the user's own configuration, non-interactively."""

    label = "ambient"

    def __init__(self, host: str, repository: str) -> None:
        self.remote_url = _https_remote(host, repository)

    def check_offline(self) -> None:
        return None

    def transport(self) -> GitTransport:
        return GitTransport(
            self.remote_url,
            {**_NO_PROMPT, "GCM_INTERACTIVE": "never"},
            (),
            self.label,
        )


def build_strategy(
    settings: GithubWikiProviderSettings,
    *,
    runner: GitRunner,
    environ: Mapping[str, str] | None = None,
    which: Which = shutil.which,
) -> EnvTokenStrategy | GhTokenStrategy | SshStrategy | AmbientStrategy:
    """Return the strategy of ``settings.auth``; no other mode is ever consulted.

    An auth value that is none of the four modes is a coded error, never a
    silent fallback to ambient credentials (this is an explicit check, not an
    ``assert``, so it also holds under ``python -O``).
    """
    host, repository, auth = settings.host, settings.repository, settings.auth
    if isinstance(auth, EnvAuth):
        return EnvTokenStrategy(host, repository, auth.variable, environ=environ)
    if isinstance(auth, GhAuth):
        return GhTokenStrategy(
            host,
            repository,
            auth.account,
            runner=runner,
            timeout=min(settings.git_timeout_seconds, GH_TOKEN_TIMEOUT_SECONDS),
            which=which,
            environ=environ,
        )
    if isinstance(auth, SshAuth):
        return SshStrategy(host, repository, auth.key_path)
    if isinstance(auth, AmbientAuth):
        return AmbientStrategy(host, repository)
    raise GithubWikiError(
        "config.invalid",
        f"Unsupported auth mode '{getattr(auth, 'mode', type(auth).__name__)}'",
        hint="set auth.mode to one of: env | gh | ssh | ambient",
    )
