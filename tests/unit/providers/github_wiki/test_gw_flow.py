"""Flow tests: a plugin plans and applies through the host onto ``github_wiki`` (GW-P1, GW-P14).

The orchestrator creates the provider through the entry-point factory, the real
``local_files`` backend writes into the clone, and git is the stateful ``FakeWikiGit``
model behind an injected runner (nothing starts a process, nothing touches the network).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from wikiops_sdk.contracts import PluginConfigModel, PluginInputModel, PluginManifest
from wikiops_sdk.domain import (
    ChangeSet,
    CreateDocumentOperation,
    DocumentRef,
    OperationStatus,
    PluginResourceAssetSource,
    ProviderCapability,
    PutAssetOperation,
    RefKind,
)

from tests.support.fake_git_runner import FakeGitRunner
from tests.support.fake_wiki_git import FakeWikiGit
from wikiops.core.orchestrator import DefaultDocumentationOrchestrator
from wikiops.providers.github_wiki import GithubWikiProviderFactory
from wikiops.providers.local_files import LocalFilesProviderFactory

PROFILE = "default"
PLUGIN_ID = "flow.plugin"
LOGO_BYTES = b"\x89PNG-flow-logo"
REMOTE = "https://github.com/acme/platform.wiki.git"


class _Config(PluginConfigModel):
    pass


class _Input(PluginInputModel):
    pass


class _Resources:
    def has(self, relative_path: str) -> bool:
        return relative_path == "logo.png"

    def read_text(self, relative_path: str, encoding: str = "utf-8") -> str:
        return self.read_bytes(relative_path).decode(encoding)

    def read_bytes(self, relative_path: str) -> bytes:
        return LOGO_BYTES


def _ref(path: str) -> DocumentRef:
    return DocumentRef(provider="docs", kind=RefKind.PATH, locator={"path": path})


class _FlowPlugin:
    """Plans a logo upload, a root page and a page without ``ref``; ``nested`` adds a nested one."""

    manifest = PluginManifest.for_current_api(
        plugin_id=PLUGIN_ID,
        display_name="Flow Plugin",
        version="1.0.0",
        description="Plugin used for github_wiki flow tests.",
        required_capabilities={ProviderCapability.CREATE_DOCUMENT, ProviderCapability.PUT_ASSET},
    )
    resources = _Resources()

    def __init__(self, nested: bool = False) -> None:
        self._nested = nested

    def get_config_model(self) -> type[PluginConfigModel]:
        return _Config

    def get_input_model(self) -> type[PluginInputModel]:
        return _Input

    def required_ref_aliases(self, plugin_config: Any, input_data: Any) -> set[str]:
        return set()

    def plan(self, ctx: Any) -> ChangeSet:
        body = "![logo](asset://logo)\n"
        operations: list[Any] = [
            PutAssetOperation(
                asset_key="logo",
                source=PluginResourceAssetSource(relative_path="logo.png"),
                name="logo.png",
            ),
            CreateDocumentOperation(title="Home", content=body, ref=_ref("Home.md")),
            CreateDocumentOperation(title="Release Notes", content=body),
        ]
        if self._nested:
            operations.append(
                CreateDocumentOperation(title="Setup", content=body, ref=_ref("guides/setup.md"))
            )
        return ChangeSet(plugin_id=PLUGIN_ID, operations=operations)


@pytest.fixture
def wiki(tmp_path: Path) -> SimpleNamespace:
    workdir = tmp_path / "cache" / "wiki"
    workdir.mkdir(parents=True)
    runner = FakeGitRunner()
    model = FakeWikiGit(workdir=workdir, remote_url=REMOTE, track_files=True).attach(runner)
    model.install_clone()
    return SimpleNamespace(workdir=workdir, runner=runner, model=model)


@pytest.fixture
def config_path(tmp_path: Path, wiki: SimpleNamespace) -> Path:
    path = tmp_path / "wikiops.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "docs": {
                        "type": "github_wiki",
                        "repository": "acme/platform",
                        "workdir": str(wiki.workdir),
                    }
                },
                "profiles": {PROFILE: {"provider": "docs"}},
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def orchestrator(
    monkeypatch: pytest.MonkeyPatch, entry_point_factory: Any, wiki: SimpleNamespace
) -> DefaultDocumentationOrchestrator:
    class WikiFactory(GithubWikiProviderFactory):
        def __init__(self) -> None:
            super().__init__(runner=wiki.runner)

    entry_points = [
        entry_point_factory("github_wiki", WikiFactory),
        entry_point_factory("local_files", LocalFilesProviderFactory),
    ]
    monkeypatch.setattr("wikiops.core.provider_manager.entry_points", lambda **_: entry_points)
    flow = DefaultDocumentationOrchestrator()
    flow.plugin_manager = SimpleNamespace(get=lambda _plugin_id: _FlowPlugin())
    return flow


def _apply(orchestrator: DefaultDocumentationOrchestrator, config_path: Path):
    return orchestrator.apply_from_file(str(config_path), PROFILE, PLUGIN_ID, {})


def test_plan_shows_the_provider_target_and_writes_nothing(
    orchestrator: DefaultDocumentationOrchestrator, config_path: Path, wiki: SimpleNamespace
) -> None:
    _, _, change_set, _ = orchestrator.plan_from_file(str(config_path), PROFILE, PLUGIN_ID, {})

    note = change_set.notes[0]
    assert note.code == "provider_target"
    assert f"remote='{REMOTE}'" in note.message and f"workdir='{wiki.workdir}'" in note.message
    assert "auto_commit=true" in note.message and "auto_push=false" in note.message
    assert [item for item in wiki.workdir.iterdir() if item.name != ".git"] == []
    assert wiki.model.commits == []


def test_apply_writes_flat_pages_with_document_relative_assets_and_one_local_commit(
    orchestrator: DefaultDocumentationOrchestrator, config_path: Path, wiki: SimpleNamespace
) -> None:
    _, result, _ = _apply(orchestrator, config_path)

    assert [item.status for item in result.results] == [OperationStatus.APPLIED] * 3
    (stored,) = sorted((wiki.workdir / "assets").iterdir())
    link = f"assets/{stored.name}"
    assert stored.read_bytes() == LOGO_BYTES
    assert (wiki.workdir / "Home.md").read_text(encoding="utf-8") == f"![logo]({link})\n"
    assert (wiki.workdir / "Release-Notes.md").read_text(encoding="utf-8") == f"![logo]({link})\n"
    assert len(wiki.model.commits) == 1
    assert set(wiki.model.commits[0].paths) == {"Home.md", "Release-Notes.md", link}
    assert wiki.model.pushes == []  # allow_auto_push defaults to false
    assert all("committed locally" in (item.message or "") for item in result.results[1:])


def test_a_nested_page_fails_on_its_own_and_the_flat_ones_are_committed(
    orchestrator: DefaultDocumentationOrchestrator, config_path: Path, wiki: SimpleNamespace
) -> None:
    orchestrator.plugin_manager = SimpleNamespace(get=lambda _plugin_id: _FlowPlugin(nested=True))

    _, result, _ = _apply(orchestrator, config_path)

    assert [item.status for item in result.results] == [OperationStatus.APPLIED] * 3 + [
        OperationStatus.FAILED
    ]
    assert (result.results[3].message or "").startswith("[github_wiki:path.nested_not_supported]")
    assert not (wiki.workdir / "guides").exists()
    assert "guides/setup.md" not in wiki.model.commits[0].paths
