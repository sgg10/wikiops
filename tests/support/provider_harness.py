"""Wiring shared by the provider tests: a ``GithubWikiProvider`` over fakes only.

``build_provider`` assembles the real provider with the real credential strategy
(so labels, remotes and per-call transports are the production ones) over a
``FakeGitRunner`` answered by a ``FakeWikiGit`` model, and a backend resolver that
creates the real ``local_files`` backend or the independent ``fake_files`` one
rooted at the workdir. ``CountingResolver`` records when the backend was created
and whether the workdir already existed then, which is how tests prove the
backend is lazy and comes after the sync. Nothing here starts a process or
touches the network.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wikiops_sdk.contracts import DocumentProvider

from tests.support.fake_file_backend import FakeFileBackendFactory, manager_with
from tests.support.fake_git_runner import FakeGitRunner
from tests.support.fake_wiki_git import FakeWikiGit, subcommand_and_args
from wikiops.providers.github_wiki.auth import build_strategy
from wikiops.providers.github_wiki.backend import EntryPointBackendResolver
from wikiops.providers.github_wiki.ports import BackendResolver, CredentialStrategy
from wikiops.providers.github_wiki.provider import GithubWikiProvider
from wikiops.providers.github_wiki.settings import (
    GithubWikiProviderSettings,
    LocalBackendSettings,
    parse_settings,
)
from wikiops.providers.local_files.provider import LocalFilesProviderFactory

TOKEN = "ghp_provider_SECRET_9"


def fake_which(name: str) -> str:
    return f"/usr/bin/{name}"


@dataclass
class CountingResolver:
    """``BackendResolver`` wrapper recording every offline check and creation."""

    inner: BackendResolver
    offline_checks: int = 0
    created: list[tuple[Path, str, bool]] = field(default_factory=list)  # root, provider_name, root existed

    def check_offline(self, backend: LocalBackendSettings) -> None:
        self.offline_checks += 1
        self.inner.check_offline(backend)

    def create(
        self, backend: LocalBackendSettings, *, root: Path, provider_name: str
    ) -> DocumentProvider:
        self.created.append((root, provider_name, root.is_dir()))
        return self.inner.create(backend, root=root, provider_name=provider_name)


def real_resolver() -> EntryPointBackendResolver:
    """Resolver over exactly ``local_files`` and ``fake_files`` (no entry points)."""
    return EntryPointBackendResolver(manager_with(LocalFilesProviderFactory(), FakeFileBackendFactory()))


@dataclass
class ProviderHarness:
    provider: GithubWikiProvider
    settings: GithubWikiProviderSettings
    workdir: Path
    runner: FakeGitRunner
    fake: FakeWikiGit
    resolver: CountingResolver
    raw: dict[str, Any] = field(default_factory=dict)
    credentials: CredentialStrategy | None = None
    which: Callable[[str], str | None] = fake_which
    cache_dir: Path | None = None

    def rebuild(self, **overrides: Any) -> GithubWikiProvider:
        """A NEW provider over the same runner, clone and credentials with changed raw settings.

        Models the user editing the configuration between two runs (for example
        switching ``allow_auto_commit``): nothing but the settings differs.
        """
        assert self.credentials is not None
        return GithubWikiProvider(
            parse_settings({**self.raw, **overrides}),
            runner=self.runner,
            credentials=self.credentials,
            backends=self.resolver,
            which=self.which,
            cache_dir=self.cache_dir,
        )

    def git_subcommands(self) -> list[str]:
        return [
            subcommand_and_args(call.argv)[0] for call in self.runner.calls if call.argv[0] == "git"
        ]

    def network_calls(self) -> list[str]:
        """Subcommands that ran with git hooks disabled: exactly the network commands."""
        return [
            subcommand_and_args(call.argv)[0]
            for call in self.runner.calls
            if call.argv[0] == "git" and any(part.startswith("core.hooksPath=") for part in call.argv)
        ]


def build_provider(
    tmp_path: Path,
    *,
    cloned: bool = False,
    workdir: Path | None = None,
    settings: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
    gh_token: str | None = None,
    cache_dir: Path | None = None,
    which: Callable[[str], str | None] = fake_which,
    backends: BackendResolver | None = None,
    **model: Any,
) -> ProviderHarness:
    """A provider whose workdir is ``tmp_path/cache/wiki`` unless settings name another.

    ``settings`` overrides the raw provider settings (``workdir`` included),
    ``environ`` is what the env strategy sees, ``which`` resolves executables,
    ``gh_token`` is what a scripted ``gh auth token`` prints, ``backends``
    replaces the resolver that creates the inner backend, and ``model``
    configures the ``FakeWikiGit`` (``track_files=True`` lets it see the files
    a real backend writes).
    """
    raw: dict[str, Any] = {
        "provider_name": "docs",
        "repository": "acme/platform",
        "workdir": str(workdir or tmp_path / "cache" / "wiki"),
    }
    raw.update(settings or {})
    parsed = parse_settings(raw)
    target = Path(parsed.workdir).resolve() if parsed.workdir else _default_workdir(tmp_path, parsed, cache_dir)

    runner = FakeGitRunner()
    if gh_token is not None:
        runner.script(["gh", "auth", "token"], stdout=f"{gh_token}\n")
    credentials = build_strategy(parsed, runner=runner, environ=dict(environ or {}), which=which)
    fake = FakeWikiGit(workdir=target, remote_url=credentials.remote_url, **model).attach(runner)
    if cloned:
        target.mkdir(parents=True, exist_ok=True)
        fake.install_clone()

    resolver = CountingResolver(backends or real_resolver())
    provider = GithubWikiProvider(
        parsed,
        runner=runner,
        credentials=credentials,
        backends=resolver,
        which=which,
        cache_dir=cache_dir,
    )
    return ProviderHarness(
        provider, parsed, target, runner, fake, resolver, raw, credentials, which, cache_dir
    )


def _default_workdir(
    tmp_path: Path, parsed: GithubWikiProviderSettings, cache_dir: Path | None
) -> Path:
    from wikiops.providers.github_wiki.workdir import resolve_workdir

    return resolve_workdir(
        configured=None,
        host=parsed.host,
        repository=parsed.repository,
        provider_name=parsed.provider_name,
        cwd=tmp_path,
        cache_dir=cache_dir,
    )
