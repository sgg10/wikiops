from __future__ import annotations

from typing import Any

import pytest
from pydantic import Field

from wikiops.core.exceptions import ConfigurationError
from wikiops.core.provider_manager import ProviderManager, TargetDescribingProvider
from wikiops_sdk.compat import ProviderAPIIncompatibleError
from wikiops_sdk.contracts import ProviderSettings
from wikiops_sdk.domain import (
    ApplyResult,
    Asset,
    AssetRef,
    AssetRefKind,
    ChangeSet,
    Document,
    DocumentRef,
    ProviderCapability,
    PutAssetOperation,
)


def _patch_entry_points(monkeypatch: pytest.MonkeyPatch, entry_points: list[Any]) -> None:
    monkeypatch.setattr(
        "wikiops.core.provider_manager.entry_points",
        lambda **_: entry_points,
    )


class DemoProviderSettings(ProviderSettings):
    endpoint: str = Field(default="https://example.test")


class DemoProvider:
    provider_id = "demo-provider"

    def __init__(self, settings: DemoProviderSettings) -> None:
        self.settings = settings
        self.validated = False

    def capabilities(self) -> set[ProviderCapability]:
        return {ProviderCapability.READ_DOCUMENT}

    def validate_settings(self) -> None:
        self.validated = True

    def resolve_ref(self, ref: DocumentRef, ctx=None) -> DocumentRef:
        return ref

    def exists(self, ref: DocumentRef) -> bool:
        return True

    def get_document(self, ref: DocumentRef) -> Document:
        return Document(ref=ref, title="Example")

    def build_link(self, ref: DocumentRef) -> str | None:
        return None

    def put_asset(self, operation: PutAssetOperation, content: bytes) -> Asset:
        return Asset(
            ref=AssetRef(
                provider=self.settings.provider_name,
                kind=AssetRefKind.PATH,
                locator={"path": f"/.assets/{operation.name or 'asset.bin'}"},
            ),
            name=operation.name or "asset.bin",
            media_type=operation.media_type or "application/octet-stream",
            size_bytes=len(content),
        )

    def build_asset_reference(self, ref: AssetRef) -> str:
        return ref.locator["path"]

    def apply_changes(self, changeset: ChangeSet) -> ApplyResult:
        return ApplyResult(provider_name=self.settings.provider_name)


class DemoProviderFactory:
    provider_id = "demo-provider"
    settings_model = DemoProviderSettings

    def create(self, settings: DemoProviderSettings) -> DemoProvider:
        return DemoProvider(settings)


class LegacyProviderFactory:
    provider_id = "legacy-provider"

    def create(self, settings) -> DemoProvider:
        return DemoProvider(DemoProviderSettings.model_validate(settings))


class DuplicateProviderFactory(DemoProviderFactory):
    provider_id = "demo-provider"


class InvalidSettingsModelFactory(DemoProviderFactory):
    provider_id = "invalid-settings"
    settings_model = object


class NoSettingsProvider(DemoProvider):
    def __init__(self) -> None:
        self.validated = False


class MissingSettingsLegacyFactory:
    provider_id = "missing-settings"

    def create(self, settings) -> NoSettingsProvider:
        return NoSettingsProvider()


class InvalidSettingsProvider(DemoProvider):
    def __init__(self) -> None:
        self.settings = {"provider_name": "invalid"}
        self.validated = False


class InvalidSettingsLegacyFactory:
    provider_id = "invalid-provider-settings"

    def create(self, settings) -> InvalidSettingsProvider:
        return InvalidSettingsProvider()


class BrokenProviderFactory(DemoProviderFactory):
    provider_id = "broken-provider"

    def create(self, settings: DemoProviderSettings):
        return object()


class ExplodingProviderFactory(DemoProviderFactory):
    provider_id = "exploding-provider"

    def create(self, settings: DemoProviderSettings):
        raise RuntimeError("boom")


class BrokenEntryPoint:
    name = "broken-provider"

    def load(self):
        raise RuntimeError("boom")


def test_create_validates_provider_settings_and_contract(
    monkeypatch: pytest.MonkeyPatch,
    entry_point_factory,
) -> None:
    manager = ProviderManager()
    entry_point = entry_point_factory("demo-provider", DemoProviderFactory)
    _patch_entry_points(monkeypatch, [entry_point])

    provider = manager.create("demo-provider", {"provider_name": "demo"})

    assert provider.validated is True
    assert provider.settings.provider_name == "demo"


def test_create_supports_legacy_factories_with_provider_settings(
    monkeypatch: pytest.MonkeyPatch,
    entry_point_factory,
) -> None:
    manager = ProviderManager()
    entry_point = entry_point_factory("legacy-provider", LegacyProviderFactory)
    _patch_entry_points(monkeypatch, [entry_point])

    provider = manager.create("legacy-provider", {"provider_name": "legacy"})

    assert provider.validated is True
    assert provider.settings.provider_name == "legacy"


def test_load_rejects_duplicate_provider_ids(
    monkeypatch: pytest.MonkeyPatch,
    entry_point_factory,
) -> None:
    manager = ProviderManager()
    entry_points = [
        entry_point_factory("demo-provider", DemoProviderFactory),
        entry_point_factory("duplicate-provider", DuplicateProviderFactory),
    ]
    _patch_entry_points(monkeypatch, entry_points)

    with pytest.raises(ConfigurationError, match="Duplicate provider ID"):
        manager.load()


def test_create_rejects_incompatible_provider_api_versions(
    monkeypatch: pytest.MonkeyPatch,
    entry_point_factory,
) -> None:
    manager = ProviderManager()
    entry_point = entry_point_factory("demo-provider", DemoProviderFactory)
    _patch_entry_points(monkeypatch, [entry_point])

    with pytest.raises(ProviderAPIIncompatibleError):
        manager.create(
            "demo-provider",
            {"provider_name": "demo", "provider_api_version": "2.0.0"},
        )


def test_list_types_returns_sorted_provider_ids(
    monkeypatch: pytest.MonkeyPatch,
    entry_point_factory,
) -> None:
    manager = ProviderManager()

    class ZedFactory(DemoProviderFactory):
        provider_id = "zed-provider"

    entry_points = [
        entry_point_factory("zed", ZedFactory),
        entry_point_factory("demo", DemoProviderFactory),
    ]
    _patch_entry_points(monkeypatch, entry_points)

    assert manager.list_types() == ["demo-provider", "zed-provider"]


def test_get_missing_provider_type_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ProviderManager()
    _patch_entry_points(monkeypatch, [])

    with pytest.raises(ConfigurationError, match="No provider factory found"):
        manager.create("missing", {"provider_name": "missing"})


def test_create_rejects_invalid_settings_model(
    monkeypatch: pytest.MonkeyPatch,
    entry_point_factory,
) -> None:
    manager = ProviderManager()
    entry_point = entry_point_factory("invalid-settings", InvalidSettingsModelFactory)
    _patch_entry_points(monkeypatch, [entry_point])

    with pytest.raises(ConfigurationError, match="settings_model must inherit"):
        manager.create("invalid-settings", {"provider_name": "demo"})


def test_create_rejects_provider_without_document_contract(
    monkeypatch: pytest.MonkeyPatch,
    entry_point_factory,
) -> None:
    manager = ProviderManager()
    entry_point = entry_point_factory("broken-provider", BrokenProviderFactory)
    _patch_entry_points(monkeypatch, [entry_point])

    with pytest.raises(ConfigurationError, match="does not create a DocumentProvider"):
        manager.create("broken-provider", {"provider_name": "demo"})


def test_create_rejects_legacy_provider_without_settings(
    monkeypatch: pytest.MonkeyPatch,
    entry_point_factory,
) -> None:
    manager = ProviderManager()
    entry_point = entry_point_factory("missing-settings", MissingSettingsLegacyFactory)
    _patch_entry_points(monkeypatch, [entry_point])

    with pytest.raises(ConfigurationError, match="must expose settings_model"):
        manager.create("missing-settings", {"provider_name": "demo"})


def test_create_rejects_provider_with_invalid_settings_type(
    monkeypatch: pytest.MonkeyPatch,
    entry_point_factory,
) -> None:
    manager = ProviderManager()
    entry_point = entry_point_factory(
        "invalid-provider-settings",
        InvalidSettingsLegacyFactory,
    )
    _patch_entry_points(monkeypatch, [entry_point])

    with pytest.raises(ConfigurationError, match="must inherit from ProviderSettings"):
        manager.create("invalid-provider-settings", {"provider_name": "demo"})


def test_load_wraps_entry_point_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = ProviderManager()
    _patch_entry_points(monkeypatch, [BrokenEntryPoint()])

    with pytest.raises(ConfigurationError, match="Failed to load provider factory"):
        manager.load()


def test_create_wraps_factory_errors(
    monkeypatch: pytest.MonkeyPatch,
    entry_point_factory,
) -> None:
    manager = ProviderManager()
    entry_point = entry_point_factory("exploding-provider", ExplodingProviderFactory)
    _patch_entry_points(monkeypatch, [entry_point])

    with pytest.raises(ConfigurationError, match="Failed to create provider"):
        manager.create("exploding-provider", {"provider_name": "demo"})


def test_load_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    entry_point_factory,
) -> None:
    calls = {"count": 0}

    def _entry_points(**_kwargs):
        calls["count"] += 1
        return [entry_point_factory("demo-provider", DemoProviderFactory)]

    monkeypatch.setattr("wikiops.core.provider_manager.entry_points", _entry_points)
    manager = ProviderManager()

    manager.load()
    manager.load()

    assert calls["count"] == 1


class TargetDescribingDemoProvider(DemoProvider):
    def describe_target(self) -> str:
        return "root='/srv/wiki'"


def test_target_describing_protocol_matches_providers_with_describe_target() -> None:
    provider = TargetDescribingDemoProvider(DemoProviderSettings(provider_name="demo"))

    assert isinstance(provider, TargetDescribingProvider)
    assert provider.describe_target() == "root='/srv/wiki'"


def test_target_describing_protocol_rejects_providers_without_describe_target() -> None:
    provider = DemoProvider(DemoProviderSettings(provider_name="demo"))

    assert not isinstance(provider, TargetDescribingProvider)
    assert not isinstance(object(), TargetDescribingProvider)
