"""Wiring shared by the sidebar IO tests: a ``SidebarWriter`` over fakes only.

``build_sidebar`` creates the real ``Git`` facade, ``PendingManifest`` and workdir lock
of the publisher harness (a clone already exists, answered by ``FakeWikiGit``), and a
``ScriptedBackend`` over the independent ``FakeFileBackend`` rooted at the workdir, so a
test can both let the backend really write ``_Sidebar.md`` and replace what it reports.
Nothing here starts a process or touches the network.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wikiops_sdk.domain import ApplyResult, ChangeSet

from tests.support.fake_file_backend import FakeFileBackend, FakeFileBackendSettings
from tests.support.fake_git_runner import FakeGitRunner
from tests.support.fake_wiki_git import FakeWikiGit
from tests.support.publisher_harness import PublisherHarness, build_publisher
from tests.support.write_ops import ScriptedBackend, change_set, create
from wikiops.providers.github_wiki.git import Git
from wikiops.providers.github_wiki.layout import SIDEBAR_PAGE
from wikiops.providers.github_wiki.manifest import PendingManifest
from wikiops.providers.github_wiki.sidebar_io import SidebarWriter


@dataclass
class SidebarHarness:
    publisher: PublisherHarness
    backend: ScriptedBackend
    changeset: ChangeSet

    @property
    def workdir(self) -> Path:
        return self.publisher.workdir

    @property
    def runner(self) -> FakeGitRunner:
        return self.publisher.runner

    @property
    def fake(self) -> FakeWikiGit:
        return self.publisher.fake

    @property
    def git(self) -> Git:
        return self.publisher.git

    @property
    def manifest(self) -> PendingManifest:
        return self.publisher.manifest

    def writer(self) -> SidebarWriter:
        return SidebarWriter(self.git, self.backend, self.manifest, self.changeset)

    # -- the workdir -------------------------------------------------------------

    def pages(self, *names: str) -> None:
        """Write plain pages (``Home`` -> ``Home.md``) in the workdir."""
        for name in names:
            (self.workdir / f"{name}.md").write_bytes(f"# {name}\n".encode())

    def seed(self, content: str | bytes, *, committed: bool = False, pending: bool = False) -> bytes:
        """Put a ``_Sidebar.md`` in the workdir: committed (tracked) or pending (recorded)."""
        data = content.encode() if isinstance(content, str) else content
        if pending:
            self.publisher.write(SIDEBAR_PAGE, "", record=False)
            (self.workdir / SIDEBAR_PAGE).write_bytes(data)
            self.manifest.record([SIDEBAR_PAGE])
        else:
            (self.workdir / SIDEBAR_PAGE).write_bytes(data)
        if committed:
            self.fake.committed_files[SIDEBAR_PAGE] = data
        return data

    def sidebar(self) -> bytes | None:
        target = self.workdir / SIDEBAR_PAGE
        return target.read_bytes() if target.is_file() else None

    def check_ignore_calls(self) -> list[Any]:
        return self.runner.calls_matching(("git", "--literal-pathspecs", "check-ignore")) or [
            call for call in self.runner.calls if "check-ignore" in call.argv
        ]


def build_sidebar(tmp_path: Path, **model: Any) -> SidebarHarness:
    """A writer harness whose clone holds no page yet; ``model`` configures the ``FakeWikiGit``."""
    publisher = build_publisher(tmp_path, **model)
    backend = ScriptedBackend(
        FakeFileBackend(FakeFileBackendSettings(root=str(publisher.workdir), provider_name="docs"))
    )
    # The change set of the surrounding apply: the sidebar write must replace its operations.
    return SidebarHarness(publisher, backend, change_set(create("Home.md", "# Home\n")))


def only_result(results: list[Any]) -> ApplyResult:
    """An ``ApplyResult`` holding exactly ``results``."""
    return ApplyResult(provider_name="docs", results=results)
