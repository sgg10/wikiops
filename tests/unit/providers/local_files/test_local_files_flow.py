"""Flow tests: a plugin plans and applies through the host onto ``local_files``."""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from wikiops.core.orchestrator import DefaultDocumentationOrchestrator
from wikiops.core.provider_manager import ProviderManager, TargetDescribingProvider
from wikiops.providers.azure_devops import AzureDevOpsWikiProviderFactory
from wikiops.providers.azure_devops.provider import AzureDevOpsWikiProvider
from wikiops.providers.local_files import LocalFilesProviderFactory
from wikiops_sdk.contracts import PluginConfigModel, PluginInputModel, PluginManifest
from wikiops_sdk.domain import (
    ApplyResult,
    ChangeSet,
    CreateDocumentOperation,
    DocumentRef,
    OperationStatus,
    PluginResourceAssetSource,
    ProviderCapability,
    PutAssetOperation,
    RefKind,
    UpdateDocumentOperation,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
ENTRY_POINT = "local_files"
ENTRY_POINT_TARGET = "wikiops.providers.local_files:LocalFilesProviderFactory"
PROFILE = "default"
PLUGIN_ID = "flow.plugin"
LOGO_BYTES = b"\x89PNG-flow-logo"


class _Config(PluginConfigModel):
    pass


class _Input(PluginInputModel):
    title: str = "Overview"


class _Resources:
    """Plugin resources: one logo image."""

    def has(self, relative_path: str) -> bool:
        return relative_path == "logo.png"

    def read_text(self, relative_path: str, encoding: str = "utf-8") -> str:
        return self.read_bytes(relative_path).decode(encoding)

    def read_bytes(self, relative_path: str) -> bytes:
        assert relative_path == "logo.png"
        return LOGO_BYTES


def _ref(path: str) -> DocumentRef:
    return DocumentRef(provider="docs", kind=RefKind.PATH, locator={"path": path})


class _FlowPlugin:
    """Plans a logo upload and three documents at three depths, one without ``ref``."""

    manifest = PluginManifest.for_current_api(
        plugin_id=PLUGIN_ID,
        display_name="Flow Plugin",
        version="1.0.0",
        description="Plugin used for local_files flow tests.",
        required_capabilities={
            ProviderCapability.CREATE_DOCUMENT,
            ProviderCapability.PUT_ASSET,
        },
    )
    resources = _Resources()

    def get_config_model(self) -> type[PluginConfigModel]:
        return _Config

    def get_input_model(self) -> type[PluginInputModel]:
        return _Input

    def required_ref_aliases(self, plugin_config: Any, input_data: Any) -> set[str]:
        return set()

    def plan(self, ctx: Any) -> ChangeSet:
        body = "![logo](asset://logo)\n"
        return ChangeSet(
            plugin_id=PLUGIN_ID,
            operations=[
                PutAssetOperation(
                    asset_key="logo",
                    source=PluginResourceAssetSource(relative_path="logo.png"),
                    name="logo.png",
                ),
                CreateDocumentOperation(
                    title="Setup", content=body, ref=_ref("docs/guide/Setup.md")
                ),
                CreateDocumentOperation(
                    title="Home", content=body, ref=_ref("README.md")
                ),
                CreateDocumentOperation(
                    title=ctx.input_data.get("title", "Overview"), content=body
                ),
            ],
        )


@pytest.fixture
def root(tmp_path: Path) -> Path:
    real = tmp_path.resolve() / "wiki"
    real.mkdir()
    return real


@pytest.fixture
def config_path(tmp_path: Path, root: Path) -> Path:
    path = tmp_path / "wikiops.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "providers": {"docs": {"type": "local_files", "root": str(root)}},
                "profiles": {PROFILE: {"provider": "docs"}},
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def orchestrator(
    monkeypatch: pytest.MonkeyPatch, entry_point_factory
) -> DefaultDocumentationOrchestrator:
    entry_points = [
        entry_point_factory("azure_devops_wiki", AzureDevOpsWikiProviderFactory),
        entry_point_factory("local_files", LocalFilesProviderFactory),
    ]
    monkeypatch.setattr(
        "wikiops.core.provider_manager.entry_points", lambda **_: entry_points
    )
    flow = DefaultDocumentationOrchestrator()
    flow.plugin_manager = SimpleNamespace(get=lambda _plugin_id: _FlowPlugin())
    return flow


def _plan(orchestrator: DefaultDocumentationOrchestrator, config_path: Path):
    return orchestrator.plan_from_file(str(config_path), PROFILE, PLUGIN_ID, {})


def _apply(
    orchestrator: DefaultDocumentationOrchestrator, config_path: Path
) -> tuple[ChangeSet, ApplyResult]:
    change_set, result, _ = orchestrator.apply_from_file(
        str(config_path), PROFILE, PLUGIN_ID, {}
    )
    return change_set, result


def _logo_link(root: Path) -> str:
    (stored,) = sorted((root / "assets").iterdir())
    return f"assets/{stored.name}"


def test_the_host_lists_local_files_next_to_azure_devops_wiki(
    monkeypatch: pytest.MonkeyPatch, entry_point_factory
) -> None:
    entry_points = [
        entry_point_factory("azure_devops_wiki", AzureDevOpsWikiProviderFactory),
        entry_point_factory("local_files", LocalFilesProviderFactory),
    ]
    monkeypatch.setattr(
        "wikiops.core.provider_manager.entry_points", lambda **_: entry_points
    )

    assert ProviderManager().list_types() == ["azure_devops_wiki", "local_files"]


def test_pyproject_registers_the_local_files_entry_point() -> None:
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    header = '[project.entry-points."wikiops.providers"]'
    section = pyproject.split(header, 1)[1].split("\n[", 1)[0]

    assert f'{ENTRY_POINT} = "{ENTRY_POINT_TARGET}"' in section
    module_name, _, attribute = ENTRY_POINT_TARGET.partition(":")
    assert getattr(importlib.import_module(module_name), attribute) is (
        LocalFilesProviderFactory
    )


def test_azure_devops_wiki_does_not_gain_a_provider_target_note() -> None:
    assert not hasattr(AzureDevOpsWikiProvider, "describe_target")
    assert not isinstance(
        AzureDevOpsWikiProvider.__new__(AzureDevOpsWikiProvider), TargetDescribingProvider
    )


def test_plan_shows_the_target_and_writes_nothing(
    orchestrator: DefaultDocumentationOrchestrator, config_path: Path, root: Path
) -> None:
    _, _, change_set, _ = _plan(orchestrator, config_path)

    assert change_set.notes[0].code == "provider_target"
    assert f"root='{root}'" in change_set.notes[0].message
    assert f"assets_dir='{root / 'assets'}'" in change_set.notes[0].message
    assert list(root.iterdir()) == []


def test_apply_writes_assets_and_documents_with_document_relative_links(
    orchestrator: DefaultDocumentationOrchestrator, config_path: Path, root: Path
) -> None:
    change_set, result = _apply(orchestrator, config_path)

    assert change_set.notes[0].code == "provider_target"
    assert f"root='{root}'" in change_set.notes[0].message
    assert [r.status for r in result.results] == [OperationStatus.APPLIED] * 4
    logo = _logo_link(root)
    assert (root / logo).read_bytes() == LOGO_BYTES
    assert (root / "docs/guide/Setup.md").read_text(encoding="utf-8") == (
        f"![logo](../../{logo})\n"
    )
    assert (root / "README.md").read_text(encoding="utf-8") == f"![logo]({logo})\n"
    assert (root / "Overview.md").read_text(encoding="utf-8") == f"![logo]({logo})\n"


def test_the_document_without_a_ref_reports_where_it_was_generated(
    orchestrator: DefaultDocumentationOrchestrator, config_path: Path
) -> None:
    _, result = _apply(orchestrator, config_path)

    generated = result.results[-1]
    assert generated.resolved_ref is not None
    assert generated.resolved_ref.locator == {"path": "Overview.md"}


def test_the_asset_result_keeps_the_generic_root_anchored_reference(
    orchestrator: DefaultDocumentationOrchestrator, config_path: Path, root: Path
) -> None:
    _, result = _apply(orchestrator, config_path)

    assert result.results[0].resolved_asset_reference == f"/{_logo_link(root)}"


def test_reapplying_the_same_plan_leaves_every_document_untouched(
    orchestrator: DefaultDocumentationOrchestrator, config_path: Path, root: Path
) -> None:
    _apply(orchestrator, config_path)
    files = sorted(path for path in root.rglob("*") if path.is_file())
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files}

    _, result = _apply(orchestrator, config_path)

    assert result.results[0].status is OperationStatus.APPLIED
    assert [r.status for r in result.results[1:]] == [OperationStatus.SKIPPED] * 3
    assert all(
        (r.message or "").startswith("[local_files:noop.unchanged]")
        for r in result.results[1:]
    )
    assert sorted(path for path in root.rglob("*") if path.is_file()) == files
    assert {
        path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files
    } == before


def test_a_differing_existing_document_fails_without_blocking_the_others(
    orchestrator: DefaultDocumentationOrchestrator, config_path: Path, root: Path
) -> None:
    (root / "README.md").write_text("hand written\n", encoding="utf-8")

    _, result = _apply(orchestrator, config_path)

    assert [r.status for r in result.results] == [
        OperationStatus.APPLIED,
        OperationStatus.APPLIED,
        OperationStatus.FAILED,
        OperationStatus.APPLIED,
    ]
    assert (result.results[2].message or "").startswith("[local_files:conflict.exists]")
    assert (root / "README.md").read_text(encoding="utf-8") == "hand written\n"


def test_a_plugin_update_of_an_existing_document_is_relativized_too(
    orchestrator: DefaultDocumentationOrchestrator, config_path: Path, root: Path
) -> None:
    (root / "docs").mkdir()
    (root / "docs" / "a.md").write_text("old\n", encoding="utf-8")

    class _UpdatePlugin(_FlowPlugin):
        def plan(self, ctx: Any) -> ChangeSet:
            planned = super().plan(ctx)
            return ChangeSet(
                plugin_id=PLUGIN_ID,
                operations=[
                    planned.operations[0],
                    UpdateDocumentOperation(
                        ref=_ref("docs/a.md"), new_content="![logo](asset://logo)\n"
                    ),
                ],
            )

    orchestrator.plugin_manager = SimpleNamespace(get=lambda _plugin_id: _UpdatePlugin())

    _, result = _apply(orchestrator, config_path)

    assert [r.status for r in result.results] == [OperationStatus.APPLIED] * 2
    assert (root / "docs" / "a.md").read_text(encoding="utf-8") == (
        f"![logo](../{_logo_link(root)})\n"
    )
