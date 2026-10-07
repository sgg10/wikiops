"""Wiring shared by the publisher tests: a ``Publisher`` over ``FakeWikiGit``.

``build_publisher`` creates the real ``Git`` facade over a ``FakeGitRunner``
answered by a ``FakeWikiGit`` model (a clone already exists), a real
``PendingManifest`` and a real ``WorkdirLock`` inside the workdir's ``.git``
directory, and the ``Publisher`` configured from ``CommitSettings``. ``write``
puts a page on disk and marks it dirty in the model, as an apply does;
``publish`` runs inside the workdir lock like the provider will.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tests.support.fake_git_runner import FakeGitRunner
from tests.support.fake_wiki_git import FakeWikiGit, subcommand_and_args
from tests.support.sync_harness import REMOTE, StubStrategy, TIMEOUT
from wikiops.providers.github_wiki.git import Git
from wikiops.providers.github_wiki.lock import WorkdirLock
from wikiops.providers.github_wiki.manifest import PendingManifest
from wikiops.providers.github_wiki.publisher import PublishOutcome, Publisher
from wikiops.providers.github_wiki.settings import CommitSettings
from wikiops.providers.github_wiki.workdir import lock_path, manifest_path


@dataclass
class PublisherHarness:
    workdir: Path
    runner: FakeGitRunner
    fake: FakeWikiGit
    strategy: StubStrategy
    git: Git
    manifest: PendingManifest
    lock: WorkdirLock
    publisher: Publisher

    # -- the workdir -------------------------------------------------------------

    def write(self, path: str, content: str = "# page\n", *, record: bool = False) -> None:
        """Write ``path`` in the workdir and mark it dirty (untracked) in the model."""
        target = self.workdir / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        entry = f"?? {path}"
        if entry not in self.fake.dirty:
            self.fake.dirty.append(entry)
        if record:
            self.manifest.record([path])

    def foreign(self, path: str, status: str = "??") -> None:
        """A dirty path wikiops did not write (not in the manifest)."""
        (self.workdir / path).write_text("foreign\n")
        self.fake.dirty.append(f"{status} {path}")

    # -- running -----------------------------------------------------------------

    def publish(self, written: list[str], **options: Any) -> PublishOutcome:
        options.setdefault("plugin_id", "azure-docs")
        options.setdefault("page_count", len(written))
        options.setdefault("unpushed", 0)
        with self.lock.hold("apply"):
            return self.publisher.publish(written, **options)

    # -- inspection ----------------------------------------------------------------

    def subcommands(self) -> list[str]:
        return [subcommand_and_args(call.argv)[0] for call in self.runner.calls]

    def argvs(self, subcommand: str) -> list[tuple[str, ...]]:
        return [
            call.argv
            for call in self.runner.calls
            if subcommand_and_args(call.argv)[0] == subcommand
        ]


def build_publisher(
    tmp_path: Path,
    *,
    commit: dict[str, Any] | None = None,
    push: bool = False,
    branch: str = "master",
    provider_name: str = "wiki-docs",
    **model: Any,
) -> PublisherHarness:
    workdir = tmp_path / "cache" / "wiki"
    workdir.mkdir(parents=True)
    runner = FakeGitRunner()
    fake = FakeWikiGit(workdir=workdir, remote_url=REMOTE, **model).attach(runner)
    fake.install_clone()
    strategy = StubStrategy(remote_url=REMOTE)
    git = Git(runner, strategy, workdir=workdir, timeout=TIMEOUT)
    manifest = PendingManifest(manifest_path(fake.git_dir), workdir=workdir)
    lock = WorkdirLock(lock_path(fake.git_dir), workdir=workdir)
    publisher = Publisher(
        git,
        manifest,
        lock,
        commit=CommitSettings.model_validate(commit or {}),
        branch=branch,
        provider_name=provider_name,
        push=push,
    )
    return PublisherHarness(workdir, runner, fake, strategy, git, manifest, lock, publisher)
