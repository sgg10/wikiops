"""Builders and doubles for the write-side tests of ``GithubWikiProvider``.

Small constructors for the SDK operations a plugin plans (``create``, ``update``,
``child``, ``asset``), a ``ScriptedBackend`` that wraps a real backend and can
replace what it reports (to prove the provider trusts nothing the backend says),
and ``ScriptedResolver`` that hands such a backend to the provider.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from wikiops_sdk.contracts import DocumentProvider
from wikiops_sdk.domain import (
    AppliedOperationResult,
    ApplyResult,
    Asset,
    AssetRef,
    AssetRefKind,
    ChangeSet,
    CreateChildDocumentOperation,
    CreateDocumentOperation,
    DocumentRef,
    LocalFileAssetSource,
    OperationStatus,
    PutAssetOperation,
    RefKind,
    UpdateDocumentOperation,
)

from tests.support.provider_harness import real_resolver
from wikiops.providers.github_wiki.ports import BackendResolver
from wikiops.providers.github_wiki.settings import LocalBackendSettings

PLUGIN = "azure-docs"


def ref(path: str) -> DocumentRef:
    return DocumentRef(provider="", kind=RefKind.PATH, locator={"path": path})


def create(path: str | None, content: str = "# page\n", *, title: str = "Title") -> CreateDocumentOperation:
    return CreateDocumentOperation(ref=None if path is None else ref(path), title=title, content=content)


def update(path: str, content: str) -> UpdateDocumentOperation:
    return UpdateDocumentOperation(ref=ref(path), new_content=content)


def child(
    title: str, content: str = "# child\n", *, path: str | None = None, parent: str = "Home.md"
) -> CreateChildDocumentOperation:
    return CreateChildDocumentOperation(
        parent_ref=ref(parent), ref=None if path is None else ref(path), child_title=title, child_content=content
    )


def asset(name: str = "logo.png", key: str = "logo") -> PutAssetOperation:
    return PutAssetOperation(asset_key=key, source=LocalFileAssetSource(path=f"/ignored/{name}"), name=name)


def change_set(*operations: Any, plugin_id: str = PLUGIN) -> ChangeSet:
    return ChangeSet(plugin_id=plugin_id, operations=list(operations))


def asset_ref(path: str) -> AssetRef:
    return AssetRef(provider="docs", kind=AssetRefKind.PATH, locator={"path": path})


def result_for(result: ApplyResult, operation: Any) -> AppliedOperationResult:
    return next(item for item in result.results if item.operation_id == operation.operation_id)


class ScriptedBackend:
    """Delegates to ``inner`` unless a hook replaces the answer.

    ``on_apply(changeset, real_result) -> ApplyResult`` post-processes a real apply;
    ``on_asset(operation, content, real_asset) -> Asset`` post-processes a real put_asset;
    ``apply_error`` makes ``apply_changes`` raise. ``applied`` records every changeset.
    """

    def __init__(self, inner: DocumentProvider) -> None:
        self._inner = inner
        self.provider_id = inner.provider_id
        self.settings = getattr(inner, "settings", None)
        self.applied: list[ChangeSet] = []
        self.assets: list[PutAssetOperation] = []
        self.on_apply: Callable[[ChangeSet, ApplyResult], ApplyResult] | None = None
        self.on_asset: Callable[[PutAssetOperation, bytes, Asset], Asset] | None = None
        self.apply_error: Exception | None = None
        self.asset_error: Exception | None = None
        self.before: Callable[[], None] | None = None  # runs when a write starts (lock probes)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def capabilities(self):  # noqa: ANN201
        return self._inner.capabilities()

    def apply_changes(self, changeset: ChangeSet) -> ApplyResult:
        self.applied.append(changeset)
        if self.before:
            self.before()
        if self.apply_error is not None:
            raise self.apply_error
        real = self._inner.apply_changes(changeset)
        return self.on_apply(changeset, real) if self.on_apply else real

    def put_asset(self, operation: PutAssetOperation, content: bytes) -> Asset:
        self.assets.append(operation)
        if self.before:
            self.before()
        if self.asset_error is not None:
            raise self.asset_error
        real = self._inner.put_asset(operation, content)
        return self.on_asset(operation, content, real) if self.on_asset else real


class ScriptedResolver:
    """``BackendResolver`` whose backend is wrapped in a ``ScriptedBackend``."""

    def __init__(self, inner: BackendResolver | None = None) -> None:
        self._inner = inner or real_resolver()
        self.backend: ScriptedBackend | None = None

    def check_offline(self, backend: LocalBackendSettings) -> None:
        self._inner.check_offline(backend)

    def create(self, backend: LocalBackendSettings, *, root: Path, provider_name: str) -> DocumentProvider:
        self.backend = ScriptedBackend(self._inner.create(backend, root=root, provider_name=provider_name))
        return self.backend  # type: ignore[return-value]


def failed(operation_id: str, message: str = "boom") -> AppliedOperationResult:
    return AppliedOperationResult(operation_id=operation_id, status=OperationStatus.FAILED, message=message)
