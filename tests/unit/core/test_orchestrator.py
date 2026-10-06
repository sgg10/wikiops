from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import BaseModel, Field

from wikiops.core.config_loader import AppConfig, ProfileDefinition, ProviderDefinition
from wikiops.core.exceptions import ConfigurationError, ProviderCompatibilityError
from wikiops.core.orchestrator import DefaultDocumentationOrchestrator
from wikiops_sdk.contracts import PluginConfigModel, PluginInputModel, PluginManifest
from wikiops_sdk.domain import (
    ApplyResult,
    AssetPathBase,
    ChangeSet,
    Document,
    DocumentRef,
    NoteMessage,
    OperationStatus,
    PluginResourceAssetSource,
    PutAssetOperation,
    ProviderCapability,
    UpdateDocumentOperation,
)


class DemoPluginConfig(PluginConfigModel):
    greeting: str = Field(default="hello")


class AssetPolicyPluginConfig(PluginConfigModel):
    asset_policy: dict[str, object] = Field(default_factory=dict)


class DemoPluginInput(PluginInputModel):
    title: str


class PartialInput(PluginInputModel):
    title: str | None = None
    description: str | None = None
    execution_date: str | None = None
    execution_time: str | None = None
    sources: list[str] | None = None
    components: list[str] | None = None
    developer: str | None = None


class InfoPatch(BaseModel):
    frequency: str | None = None
    description: str | None = None
    execution_date: str | None = None
    execution_time: str | None = None
    sources: list[str] | None = None
    components: list[str] | None = None
    developer: str | None = None


class PatchInput(BaseModel):
    info: InfoPatch | None = None


class NestedPartialInput(PluginInputModel):
    patch: PatchInput


class RowUpdate(BaseModel):
    key: str
    description: str | None = None
    domain: str | None = None
    category: str | None = None
    partitioning: str | None = None


class RowsPatch(BaseModel):
    update: list[RowUpdate] = Field(default_factory=list)


class RowsInputPatch(BaseModel):
    rows: RowsPatch | None = None


class ListPartialInput(PluginInputModel):
    patch: RowsInputPatch


class RecordingPlugin:
    manifest = PluginManifest.for_current_api(
        plugin_id="demo.plugin",
        display_name="Demo Plugin",
        version="1.0.0",
        description="Plugin used for orchestrator tests.",
    )

    def __init__(self) -> None:
        self.received_ctx = None

    def get_config_model(self):
        return DemoPluginConfig

    def get_input_model(self):
        return DemoPluginInput

    def required_ref_aliases(self, plugin_config, input_data):
        assert plugin_config.greeting
        assert input_data.title
        return {"inventory"}

    def plan(self, ctx):
        self.received_ctx = ctx
        return ChangeSet(
            plugin_id=self.manifest.plugin_id,
            operations=[
                UpdateDocumentOperation(
                    ref=ctx.refs["inventory"],
                    new_content="# Planned\n",
                )
            ],
        )


class GenericRecordingPlugin(RecordingPlugin):
    def __init__(self, input_model: type[PluginInputModel]) -> None:
        super().__init__()
        self._input_model = input_model

    def get_input_model(self):
        return self._input_model

    def required_ref_aliases(self, plugin_config, input_data):
        return {"inventory"}


class CapabilityHungryPlugin(RecordingPlugin):
    manifest = PluginManifest.for_current_api(
        plugin_id="hungry.plugin",
        display_name="Hungry Plugin",
        version="1.0.0",
        description="Plugin requiring update capability.",
        required_capabilities={ProviderCapability.UPDATE_DOCUMENT},
    )


class AssetPlugin(RecordingPlugin):
    def get_config_model(self):
        return AssetPolicyPluginConfig

    def required_ref_aliases(self, plugin_config, input_data):
        return {"inventory"}

    def plan(self, ctx):
        self.received_ctx = ctx
        return ChangeSet(
            plugin_id=self.manifest.plugin_id,
            operations=[
                PutAssetOperation(
                    asset_key="logo",
                    source=PluginResourceAssetSource(relative_path="resources/logo.png"),
                )
            ],
        )


class LocalAssetPlugin(AssetPlugin):
    def plan(self, ctx):
        self.received_ctx = ctx
        return ChangeSet(
            plugin_id=self.manifest.plugin_id,
            operations=[
                PutAssetOperation(
                    asset_key="logo",
                    source={
                        "kind": "local_file",
                        "path": "./logo.png",
                        "relative_to": AssetPathBase.INPUT_DIR,
                    },
                )
            ],
        )


class MissingAssetReferencePlugin(RecordingPlugin):
    def plan(self, ctx):
        self.received_ctx = ctx
        return ChangeSet(
            plugin_id=self.manifest.plugin_id,
            operations=[
                UpdateDocumentOperation(
                    ref=ctx.refs["inventory"],
                    new_content="![Logo](asset://missing)",
                )
            ],
        )


class DemoProvider:
    provider_id = "demo-provider"

    def __init__(
        self,
        capabilities: set[ProviderCapability],
        document: Document,
        resolved_ref: DocumentRef | None = None,
    ) -> None:
        self._capabilities = capabilities
        self.document = document
        self.resolved_ref = resolved_ref
        self.resolve_calls: list[DocumentRef] = []
        self.validate_calls = 0

    def capabilities(self) -> set[ProviderCapability]:
        return self._capabilities

    def validate_settings(self) -> None:
        self.validate_calls += 1

    def resolve_ref(self, ref: DocumentRef, ctx=None) -> DocumentRef:
        self.resolve_calls.append(ref)
        return self.resolved_ref or ref

    def exists(self, ref: DocumentRef) -> bool:
        return True

    def get_document(self, ref: DocumentRef) -> Document:
        return self.document

    def build_link(self, ref: DocumentRef) -> str | None:
        return None

    def apply_changes(self, changeset: ChangeSet) -> ApplyResult:
        return ApplyResult(provider_name="default")


class TargetDescribingDemoProvider(DemoProvider):
    """Provider that opts into the host-local ``describe_target`` hook."""

    def __init__(self, *args, target: str = "root='/srv/wiki'", **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.target = target

    def describe_target(self) -> str:
        return self.target


class NotePlugin(RecordingPlugin):
    """Plugin that emits its own notes, which must stay after ``provider_target``."""

    def plan(self, ctx):
        change_set = super().plan(ctx)
        change_set.notes.extend(
            [
                NoteMessage(code="plugin_note_a", message="first plugin note"),
                NoteMessage(code="plugin_note_b", message="second plugin note"),
            ]
        )
        return change_set


def _build_config(ref: DocumentRef, plugins: dict[str, dict[str, object]]) -> AppConfig:
    return AppConfig(
        providers={
            "default": ProviderDefinition(
                type="demo-provider",
                settings={"provider_name": "default"},
            )
        },
        profiles={
            "default": ProfileDefinition(
                provider="default",
                refs={"inventory": ref},
                plugins=plugins,
            )
        },
    )


def _plan_with_plugin(
    monkeypatch: pytest.MonkeyPatch,
    doc_ref_factory,
    document_factory,
    plugin: RecordingPlugin,
    raw_input: dict[str, object],
) -> tuple[AppConfig, object, ChangeSet]:
    ref = doc_ref_factory(provider="default", path="/inventory")
    resolved_ref = doc_ref_factory(provider="default", path="/resolved")
    document = document_factory(ref=resolved_ref, title="Inventory", content="# Current\n")
    config = _build_config(ref, {})
    provider = DemoProvider(
        {ProviderCapability.READ_DOCUMENT},
        document,
        resolved_ref=resolved_ref,
    )
    orchestrator = DefaultDocumentationOrchestrator()
    monkeypatch.setattr(
        "wikiops.core.orchestrator.uuid4",
        lambda: UUID("11111111-1111-1111-1111-111111111111"),
    )
    orchestrator.provider_manager = SimpleNamespace(create=lambda *_args: provider)
    orchestrator.plugin_manager = SimpleNamespace(get=lambda _plugin_id: plugin)
    orchestrator.reference_resolver = SimpleNamespace(
        resolve_alias=lambda profile, alias: profile.refs[alias]
    )
    orchestrator.document_loader = SimpleNamespace(
        load=lambda _provider, refs: {
            "inventory": document_factory(
                ref=refs["inventory"], title="Inventory", content="# Current\n"
            )
        }
    )

    return orchestrator._plan_internal(
        config,
        "default",
        plugin.manifest.plugin_id,
        raw_input,
    )


def test_init_validates_python_runtime_compatibility(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {"count": 0}

    monkeypatch.setattr(
        "wikiops.core.orchestrator.ensure_python_compatible",
        lambda: called.__setitem__("count", called["count"] + 1),
    )

    DefaultDocumentationOrchestrator()

    assert called["count"] == 1


def test_plan_raises_when_profile_is_missing() -> None:
    orchestrator = DefaultDocumentationOrchestrator()
    config = AppConfig()

    with pytest.raises(ConfigurationError, match="Profile 'missing' not found"):
        orchestrator._plan_internal(config, "missing", "demo.plugin", {"title": "Example"})


def test_plan_raises_when_provider_definition_is_missing() -> None:
    orchestrator = DefaultDocumentationOrchestrator()
    ref = DocumentRef(provider="default", kind="path", locator={"path": "/docs"})
    config = AppConfig(
        profiles={
            "default": ProfileDefinition(provider="default", refs={"inventory": ref})
        }
    )

    with pytest.raises(ConfigurationError, match="Provider 'default'.*does not exist"):
        orchestrator._plan_internal(config, "default", "demo.plugin", {"title": "Example"})


def test_plan_raises_when_provider_capabilities_are_missing(
    monkeypatch: pytest.MonkeyPatch,
    doc_ref_factory,
    document_factory,
) -> None:
    ref = doc_ref_factory(provider="default", path="/docs")
    config = _build_config(ref, {"hungry.plugin": {}})
    provider = DemoProvider(set(), document_factory(ref=ref))
    plugin = CapabilityHungryPlugin()
    orchestrator = DefaultDocumentationOrchestrator()
    orchestrator.provider_manager = SimpleNamespace(create=lambda *_args: provider)
    orchestrator.plugin_manager = SimpleNamespace(get=lambda _plugin_id: plugin)

    with pytest.raises(ProviderCompatibilityError, match="Missing capabilities"):
        orchestrator._plan_internal(config, "default", "hungry.plugin", {"title": "Example"})


@pytest.mark.parametrize(
    ("plugins", "plugin_id", "expected_greeting"),
    [
        ({"demo.plugin": {"greeting": "from-manifest"}}, "entrypoint.plugin", "from-manifest"),
        ({"entrypoint.plugin": {"greeting": "from-entrypoint"}}, "entrypoint.plugin", "from-entrypoint"),
    ],
)
def test_plan_internal_builds_context_and_uses_plugin_config_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
    doc_ref_factory,
    document_factory,
    plugins: dict[str, dict[str, object]],
    plugin_id: str,
    expected_greeting: str,
) -> None:
    ref = doc_ref_factory(provider="default", path="/inventory")
    resolved_ref = doc_ref_factory(provider="default", path="/resolved")
    document = document_factory(ref=resolved_ref, title="Inventory", content="# Current\n")
    config = _build_config(ref, plugins)
    provider = DemoProvider(
        {ProviderCapability.READ_DOCUMENT},
        document,
        resolved_ref=resolved_ref,
    )
    plugin = RecordingPlugin()
    orchestrator = DefaultDocumentationOrchestrator()
    monkeypatch.setattr(
        "wikiops.core.orchestrator.uuid4",
        lambda: UUID("11111111-1111-1111-1111-111111111111"),
    )
    orchestrator.provider_manager = SimpleNamespace(create=lambda *_args: provider)
    orchestrator.plugin_manager = SimpleNamespace(get=lambda _plugin_id: plugin)
    orchestrator.reference_resolver = SimpleNamespace(
        resolve_alias=lambda profile, alias: profile.refs[alias]
    )
    orchestrator.document_loader = SimpleNamespace(
        load=lambda _provider, refs: {"inventory": document_factory(ref=refs["inventory"], title="Inventory", content="# Current\n")}
    )

    returned_config, ctx, change_set = orchestrator._plan_internal(
        config,
        "default",
        plugin_id,
        {"title": "Example"},
    )

    assert returned_config is config
    assert ctx.run_id == "11111111-1111-1111-1111-111111111111"
    assert ctx.profile_name == "default"
    assert ctx.provider_name == "default"
    assert ctx.refs == {"inventory": resolved_ref}
    assert ctx.plugin_config == {"greeting": expected_greeting}
    assert ctx.input_data == {"title": "Example"}
    assert ctx.runtime_vars == {
        "plugin_id": "demo.plugin",
        "provider_id": "demo-provider",
    }
    assert provider.resolve_calls == [ref]
    assert plugin.received_ctx == ctx
    assert change_set.plugin_id == "demo.plugin"
    assert len(change_set.operations) == 1


def test_plan_internal_preserves_omitted_optional_fields_in_input_data(
    monkeypatch: pytest.MonkeyPatch,
    doc_ref_factory,
    document_factory,
) -> None:
    plugin = GenericRecordingPlugin(PartialInput)

    _, ctx, _ = _plan_with_plugin(
        monkeypatch,
        doc_ref_factory,
        document_factory,
        plugin,
        {"title": "Example"},
    )

    assert ctx.input_data == {"title": "Example"}
    assert plugin.received_ctx.input_data == {"title": "Example"}


def test_plan_internal_preserves_partial_nested_objects_in_input_data(
    monkeypatch: pytest.MonkeyPatch,
    doc_ref_factory,
    document_factory,
) -> None:
    plugin = GenericRecordingPlugin(NestedPartialInput)

    _, ctx, _ = _plan_with_plugin(
        monkeypatch,
        doc_ref_factory,
        document_factory,
        plugin,
        {"patch": {"info": {"frequency": "Semanal"}}},
    )

    assert ctx.input_data == {"patch": {"info": {"frequency": "Semanal"}}}
    assert plugin.received_ctx.input_data == {
        "patch": {"info": {"frequency": "Semanal"}}
    }


def test_plan_internal_preserves_partial_objects_inside_lists_in_input_data(
    monkeypatch: pytest.MonkeyPatch,
    doc_ref_factory,
    document_factory,
) -> None:
    plugin = GenericRecordingPlugin(ListPartialInput)

    _, ctx, _ = _plan_with_plugin(
        monkeypatch,
        doc_ref_factory,
        document_factory,
        plugin,
        {
            "patch": {
                "rows": {
                    "update": [
                        {"key": "A", "description": "nuevo texto"},
                    ]
                }
            }
        },
    )

    assert ctx.input_data == {
        "patch": {
            "rows": {
                "update": [
                    {"key": "A", "description": "nuevo texto"},
                ]
            }
        }
    }
    assert plugin.received_ctx.input_data == ctx.input_data


def test_plan_internal_preserves_explicit_nulls_in_input_data(
    monkeypatch: pytest.MonkeyPatch,
    doc_ref_factory,
    document_factory,
) -> None:
    plugin = GenericRecordingPlugin(NestedPartialInput)

    _, ctx, _ = _plan_with_plugin(
        monkeypatch,
        doc_ref_factory,
        document_factory,
        plugin,
        {"patch": {"info": {"description": None}}},
    )

    assert ctx.input_data == {"patch": {"info": {"description": None}}}
    assert plugin.received_ctx.input_data == {
        "patch": {"info": {"description": None}}
    }


def test_plan_internal_rejects_asset_operations_without_provider_support(
    monkeypatch: pytest.MonkeyPatch,
    doc_ref_factory,
    document_factory,
) -> None:
    plugin = AssetPlugin()
    ref = doc_ref_factory(provider="default", path="/inventory")
    config = _build_config(ref, {"demo.plugin": {}})
    provider = DemoProvider({ProviderCapability.READ_DOCUMENT}, document_factory(ref=ref))
    orchestrator = DefaultDocumentationOrchestrator()
    orchestrator.provider_manager = SimpleNamespace(create=lambda *_args: provider)
    orchestrator.plugin_manager = SimpleNamespace(get=lambda _plugin_id: plugin)
    orchestrator.reference_resolver = SimpleNamespace(
        resolve_alias=lambda profile, alias: profile.refs[alias]
    )
    orchestrator.document_loader = SimpleNamespace(
        load=lambda _provider, refs: {"inventory": document_factory(ref=refs["inventory"]) }
    )

    with pytest.raises(ProviderCompatibilityError, match="does not support asset uploads"):
        orchestrator._plan_internal(config, "default", plugin.manifest.plugin_id, {"title": "Example"})


def test_plan_internal_warns_when_local_asset_roots_are_deactivated(
    monkeypatch: pytest.MonkeyPatch,
    doc_ref_factory,
    document_factory,
    tmp_path,
) -> None:
    plugin = LocalAssetPlugin()
    ref = doc_ref_factory(provider="default", path="/inventory")
    (tmp_path / "logo.png").write_bytes(b"PNG")
    config = _build_config(
        ref,
        {
            "demo.plugin": {
                "asset_policy": {
                    "deactivate_allowed_asset_roots": True,
                }
            }
        },
    )
    provider = DemoProvider(
        {ProviderCapability.READ_DOCUMENT, ProviderCapability.PUT_ASSET},
        document_factory(ref=ref),
    )
    orchestrator = DefaultDocumentationOrchestrator()
    orchestrator.provider_manager = SimpleNamespace(create=lambda *_args: provider)
    orchestrator.plugin_manager = SimpleNamespace(get=lambda _plugin_id: plugin)
    orchestrator.reference_resolver = SimpleNamespace(
        resolve_alias=lambda profile, alias: profile.refs[alias]
    )
    orchestrator.document_loader = SimpleNamespace(
        load=lambda _provider, refs: {"inventory": document_factory(ref=refs["inventory"]) }
    )

    _, ctx, change_set = orchestrator._plan_internal(
        config,
        "default",
        plugin.manifest.plugin_id,
        {"title": "Example"},
        config_path=str(tmp_path / "wikiops.yaml"),
        input_path=str(tmp_path / "input.yaml"),
    )

    assert ctx.runtime_vars["config_dir"] == str(tmp_path)
    assert ctx.runtime_vars["input_dir"] == str(tmp_path)
    assert change_set.warnings[0].code == "asset_policy_unrestricted_local_files"


def test_plan_internal_rejects_local_asset_sources_outside_allowed_roots(
    monkeypatch: pytest.MonkeyPatch,
    doc_ref_factory,
    document_factory,
    tmp_path,
) -> None:
    plugin = LocalAssetPlugin()
    ref = doc_ref_factory(provider="default", path="/inventory")
    asset_file = tmp_path / "logo.png"
    asset_file.write_bytes(b"PNG")
    config = _build_config(
        ref,
        {
            "demo.plugin": {
                "asset_policy": {
                    "allowed_asset_roots": ["./trusted-assets"],
                }
            }
        },
    )
    provider = DemoProvider(
        {ProviderCapability.READ_DOCUMENT, ProviderCapability.PUT_ASSET},
        document_factory(ref=ref),
    )
    orchestrator = DefaultDocumentationOrchestrator()
    orchestrator.provider_manager = SimpleNamespace(create=lambda *_args: provider)
    orchestrator.plugin_manager = SimpleNamespace(get=lambda _plugin_id: plugin)
    orchestrator.reference_resolver = SimpleNamespace(
        resolve_alias=lambda profile, alias: profile.refs[alias]
    )
    orchestrator.document_loader = SimpleNamespace(
        load=lambda _provider, refs: {"inventory": document_factory(ref=refs["inventory"])}
    )

    with pytest.raises(ConfigurationError, match="outside the configured allowed_asset_roots"):
        orchestrator._plan_internal(
            config,
            "default",
            plugin.manifest.plugin_id,
            {"title": "Example"},
            config_path=str(tmp_path / "wikiops.yaml"),
            input_path=str(tmp_path / "input.yaml"),
        )


def test_plan_internal_rejects_missing_asset_references(
    monkeypatch: pytest.MonkeyPatch,
    doc_ref_factory,
    document_factory,
) -> None:
    plugin = MissingAssetReferencePlugin()
    ref = doc_ref_factory(provider="default", path="/inventory")
    resolved_ref = doc_ref_factory(provider="default", path="/resolved")
    config = _build_config(ref, {})
    provider = DemoProvider(
        {ProviderCapability.READ_DOCUMENT},
        document_factory(ref=resolved_ref),
        resolved_ref=resolved_ref,
    )
    orchestrator = DefaultDocumentationOrchestrator()
    orchestrator.provider_manager = SimpleNamespace(create=lambda *_args: provider)
    orchestrator.plugin_manager = SimpleNamespace(get=lambda _plugin_id: plugin)
    orchestrator.reference_resolver = SimpleNamespace(
        resolve_alias=lambda profile, alias: profile.refs[alias]
    )
    orchestrator.document_loader = SimpleNamespace(
        load=lambda _provider, refs: {"inventory": document_factory(ref=refs["inventory"]) }
    )

    with pytest.raises(ConfigurationError, match="asset keys that are not uploaded"):
        orchestrator._plan_internal(config, "default", plugin.manifest.plugin_id, {"title": "Example"})


def test_plan_and_apply_raise_without_config_file() -> None:
    orchestrator = DefaultDocumentationOrchestrator()

    with pytest.raises(NotImplementedError):
        orchestrator.plan("default", "demo.plugin", {"title": "Example"})

    with pytest.raises(NotImplementedError):
        orchestrator.apply("default", "demo.plugin", {"title": "Example"})


def test_plan_from_file_loads_config_and_renders_diff(
    app_config_factory,
    changeset_factory,
    document_factory,
) -> None:
    orchestrator = DefaultDocumentationOrchestrator()
    config = app_config_factory()
    ctx = SimpleNamespace(documents={"inventory": document_factory()})
    change_set = changeset_factory()
    orchestrator.config_loader = SimpleNamespace(load=lambda _path: config)
    orchestrator._plan_internal = lambda *_args, **_kwargs: (config, ctx, change_set)
    orchestrator.diff_engine = SimpleNamespace(
        render=lambda documents, planned: f"diff:{len(documents)}:{planned.plugin_id}"
    )

    result = orchestrator.plan_from_file(
        "config.yaml",
        "default",
        "demo.plugin",
        {"title": "Example"},
    )

    assert result == (config, ctx, change_set, "diff:1:demo.plugin")


def test_apply_from_file_reuses_plan_and_applies_changes(
    app_config_factory,
    changeset_factory,
    apply_result_factory,
) -> None:
    orchestrator = DefaultDocumentationOrchestrator()
    config = app_config_factory()
    ctx = object()
    change_set = changeset_factory()
    apply_result = apply_result_factory(statuses=[OperationStatus.APPLIED])
    provider = object()
    plugin = SimpleNamespace(resources=object())
    orchestrator.plan_from_file = lambda **_kwargs: (config, ctx, change_set, "diff")
    orchestrator.provider_manager = SimpleNamespace(create=lambda *_args: provider)
    orchestrator.plugin_manager = SimpleNamespace(get=lambda _plugin_id: plugin)
    orchestrator.apply_engine = SimpleNamespace(
        apply=lambda candidate, planned, execution_ctx, resources: apply_result
        if candidate is provider
        and planned is change_set
        and execution_ctx is ctx
        and resources is plugin.resources
        else None
    )

    result = orchestrator.apply_from_file(
        "config.yaml",
        "default",
        "demo.plugin",
        {"title": "Example"},
    )

    assert result == (change_set, apply_result, "diff")


def _wire_orchestrator(
    provider,
    plugin: RecordingPlugin,
    document_factory,
) -> DefaultDocumentationOrchestrator:
    orchestrator = DefaultDocumentationOrchestrator()
    orchestrator.provider_manager = SimpleNamespace(create=lambda *_args: provider)
    orchestrator.plugin_manager = SimpleNamespace(get=lambda _plugin_id: plugin)
    orchestrator.reference_resolver = SimpleNamespace(
        resolve_alias=lambda profile, alias: profile.refs[alias]
    )
    orchestrator.document_loader = SimpleNamespace(
        load=lambda _provider, refs: {
            "inventory": document_factory(ref=refs["inventory"])
        }
    )
    return orchestrator


def test_plan_internal_prepends_provider_target_note(
    doc_ref_factory,
    document_factory,
) -> None:
    ref = doc_ref_factory(provider="default", path="/inventory")
    provider = TargetDescribingDemoProvider(
        {ProviderCapability.READ_DOCUMENT},
        document_factory(ref=ref),
        target="root='/work/repo/docs' cwd='/work/repo'",
    )
    plugin = RecordingPlugin()
    orchestrator = _wire_orchestrator(provider, plugin, document_factory)

    _, _, change_set = orchestrator._plan_internal(
        _build_config(ref, {}),
        "default",
        plugin.manifest.plugin_id,
        {"title": "Example"},
    )

    assert [note.code for note in change_set.notes] == ["provider_target"]
    assert change_set.notes[0].message == (
        "Provider 'default' (demo-provider) target: "
        "root='/work/repo/docs' cwd='/work/repo'"
    )


def test_plan_internal_keeps_plugin_notes_after_provider_target(
    doc_ref_factory,
    document_factory,
) -> None:
    ref = doc_ref_factory(provider="default", path="/inventory")
    provider = TargetDescribingDemoProvider(
        {ProviderCapability.READ_DOCUMENT},
        document_factory(ref=ref),
    )
    plugin = NotePlugin()
    orchestrator = _wire_orchestrator(provider, plugin, document_factory)

    _, _, change_set = orchestrator._plan_internal(
        _build_config(ref, {}),
        "default",
        plugin.manifest.plugin_id,
        {"title": "Example"},
    )

    assert [note.code for note in change_set.notes] == [
        "provider_target",
        "plugin_note_a",
        "plugin_note_b",
    ]
    assert change_set.notes[1].message == "first plugin note"


def test_plan_internal_adds_no_note_for_provider_without_describe_target(
    doc_ref_factory,
    document_factory,
) -> None:
    ref = doc_ref_factory(provider="default", path="/inventory")
    provider = DemoProvider(
        {ProviderCapability.READ_DOCUMENT},
        document_factory(ref=ref),
    )
    plugin = NotePlugin()
    orchestrator = _wire_orchestrator(provider, plugin, document_factory)

    _, _, change_set = orchestrator._plan_internal(
        _build_config(ref, {}),
        "default",
        plugin.manifest.plugin_id,
        {"title": "Example"},
    )

    assert [note.code for note in change_set.notes] == [
        "plugin_note_a",
        "plugin_note_b",
    ]


def test_apply_from_file_applies_change_set_carrying_provider_target_note(
    doc_ref_factory,
    document_factory,
    apply_result_factory,
) -> None:
    ref = doc_ref_factory(provider="default", path="/inventory")
    provider = TargetDescribingDemoProvider(
        {ProviderCapability.READ_DOCUMENT},
        document_factory(ref=ref),
        target="root='/srv/wiki'",
    )
    plugin = NotePlugin()
    plugin.resources = None
    orchestrator = _wire_orchestrator(provider, plugin, document_factory)
    orchestrator.config_loader = SimpleNamespace(
        load=lambda _path: _build_config(ref, {})
    )
    applied: list[ChangeSet] = []

    def _apply(candidate, planned, execution_ctx, resources):
        applied.append(planned)
        return apply_result_factory(statuses=[OperationStatus.APPLIED])

    orchestrator.apply_engine = SimpleNamespace(apply=_apply)

    change_set, _, _ = orchestrator.apply_from_file(
        "config.yaml",
        "default",
        plugin.manifest.plugin_id,
        {"title": "Example"},
    )

    expected = [note.code for note in change_set.notes]
    assert expected == ["provider_target", "plugin_note_a", "plugin_note_b"]
    assert change_set.notes[0].message == (
        "Provider 'default' (demo-provider) target: root='/srv/wiki'"
    )
    assert applied == [change_set]
