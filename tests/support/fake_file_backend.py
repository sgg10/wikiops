"""Test-only local backend for ``github_wiki``: files below a root directory.

``FakeFileBackend`` is a second, independent implementation of the local backend
contract (``tests/contract``). It shares no code with ``local_files`` and keeps
its own, deliberately different choices (it issues a document-relative asset
reference directly where ``local_files`` issues a root-anchored one and rewrites
it), so a suite that passes for both only requires behavior, not an
implementation. ``manager_with`` registers factories in a ``ProviderManager``
without entry points, which is how tests make the fake selectable as
``local_backend.type``.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

from pydantic import ConfigDict, Field
from wikiops_sdk.contracts import ProviderSettings
from wikiops_sdk.domain import (
    AppliedOperationResult,
    ApplyResult,
    Asset,
    AssetRef,
    AssetRefKind,
    ChangeSet,
    CreateChildDocumentOperation,
    CreateDocumentOperation,
    Document,
    DocumentRef,
    DocumentVersion,
    OperationStatus,
    ProviderCapability,
    PutAssetOperation,
    RefKind,
    UpdateDocumentOperation,
)

from wikiops.core.exceptions import ConfigurationError
from wikiops.core.provider_manager import ProviderFactory, ProviderManager

PROVIDER_ID = "fake_files"
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
_UNSAFE_ASSET_STEM = re.compile(r"[^A-Za-z0-9._-]+")


class FakeBackendError(ConfigurationError):
    """Coded failure of the fake backend: ``[fake_files:<code>] <summary>. path='<p>'``."""

    def __init__(self, code: str, summary: str, *, path: str | None = None) -> None:
        self.code = code
        suffix = "" if path is None else f" path='{path}'."
        super().__init__(f"[{PROVIDER_ID}:{code}] {summary}.{suffix}")


class FakeFileBackendSettings(ProviderSettings):
    """Settings of the fake backend; unknown keys are rejected like ``local_files``."""

    model_config = ConfigDict(extra="forbid")

    root: str = Field(..., min_length=1)
    assets_dir: str = "assets"


def _canonical_relative(value: str, *, what: str) -> str:
    """Return ``value`` if it is a canonical root-relative POSIX path, else raise."""
    if not value.strip():
        raise FakeBackendError("path.empty", f"The {what} path is empty", path=value)
    if value.startswith("/") or _DRIVE_PREFIX.match(value):
        raise FakeBackendError("path.absolute", f"The {what} path is absolute", path=value)
    if "\\" in value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise FakeBackendError(
            "path.invalid_chars", f"The {what} path has a backslash or control character", path=value
        )
    segments = value.split("/")
    if any(segment in ("", ".") for segment in segments):
        raise FakeBackendError("path.non_canonical", f"The {what} path is not canonical", path=value)
    if ".." in segments:
        raise FakeBackendError("path.traversal", f"The {what} path leaves the root", path=value)
    if any(segment.casefold() == ".git" for segment in segments):
        raise FakeBackendError("path.reserved", f"The {what} path touches '.git'", path=value)
    return value


def _version(data: bytes) -> DocumentVersion:
    digest = f"sha256:{hashlib.sha256(data).hexdigest()}"
    return DocumentVersion(token=digest, etag=digest)


class FakeFileBackend:
    """A ``DocumentProvider`` over Markdown files in one directory."""

    provider_id = PROVIDER_ID

    def __init__(self, settings: FakeFileBackendSettings) -> None:
        self.settings = settings
        self._root = Path(settings.root).expanduser()

    # -- DocumentProvider ------------------------------------------------------

    def capabilities(self) -> set[ProviderCapability]:
        return {
            ProviderCapability.READ_DOCUMENT,
            ProviderCapability.CHECK_EXISTS,
            ProviderCapability.CREATE_DOCUMENT,
            ProviderCapability.UPDATE_DOCUMENT,
            ProviderCapability.CREATE_CHILD_DOCUMENT,
            ProviderCapability.BUILD_LINK,
            ProviderCapability.PUT_ASSET,
            ProviderCapability.RESOLVE_BY_PATH,
            ProviderCapability.VERSION_CHECK,
        }

    def validate_settings(self) -> None:
        if not self._root.is_dir():
            raise FakeBackendError("root.missing", "The root is not an existing directory", path=str(self._root))
        _canonical_relative(self.settings.assets_dir, what="assets_dir")

    def resolve_ref(self, ref: DocumentRef, ctx: Any = None) -> DocumentRef:
        self._locate(ref)
        return ref if ref.provider else ref.model_copy(update={"provider": self.settings.provider_name})

    def exists(self, ref: DocumentRef) -> bool:
        return self._locate(ref)[1].is_file()

    def get_document(self, ref: DocumentRef) -> Document:
        relative, target = self._locate(ref)
        if not target.is_file():
            raise FakeBackendError("document.not_found", "The document does not exist", path=relative)
        data = target.read_bytes()
        return Document(
            ref=ref,
            title=PurePosixPath(relative).stem,
            content=data.decode("utf-8"),
            version=_version(data),
            metadata={"path": relative},
        )

    def build_link(self, ref: DocumentRef) -> str | None:
        return self._locate(ref)[1].resolve().as_uri()

    def put_asset(self, operation: PutAssetOperation, content: bytes) -> Asset:
        name = operation.name or ""
        if not name or "/" in name or "\\" in name or ".." in name:
            raise FakeBackendError("asset.invalid_name", "The asset name must be a plain file name", path=name)
        suffix = PurePosixPath(name).suffix
        stem = _UNSAFE_ASSET_STEM.sub("-", name[: len(name) - len(suffix)]).strip("-.") or "asset"
        stored = f"{stem}--{hashlib.sha256(content).hexdigest()[:16]}{suffix}"
        relative = f"{_canonical_relative(self.settings.assets_dir, what='assets_dir')}/{stored}"
        target = self._root / relative
        if not target.is_file():
            self._write(target, content)
        return Asset(
            ref=AssetRef(
                provider=self.settings.provider_name,
                kind=AssetRefKind.PATH,
                locator={"path": relative},
            ),
            name=stored,
            media_type=operation.media_type or "application/octet-stream",
            size_bytes=len(content),
            version=_version(content),
        )

    def build_asset_reference(self, ref: AssetRef) -> str:
        """Document-relative already (valid from every root page); nothing is rewritten later."""
        return quote(ref.locator["path"], safe="/")

    def apply_changes(self, changeset: ChangeSet) -> ApplyResult:
        results = [self._apply_one(operation) for operation in changeset.operations]
        return ApplyResult(provider_name=self.settings.provider_name, results=results)

    # -- internals -------------------------------------------------------------

    def _locate(self, ref: DocumentRef) -> tuple[str, Path]:
        if ref.kind is not RefKind.PATH:
            raise FakeBackendError("ref.unsupported_kind", f"Only path refs are supported, got '{ref.kind.value}'")
        path = ref.locator.get("path")
        if path is None:
            raise FakeBackendError("ref.missing_path", "The path ref has no path")
        relative = _canonical_relative(path, what="document")
        if not relative.lower().endswith(".md"):
            raise FakeBackendError("path.not_markdown", "Document paths must end in '.md'", path=relative)
        return relative, self._root / relative

    def _write(self, target: Path, data: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=".fake-")
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
            os.replace(temporary, target)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    def _apply_one(self, operation: Any) -> AppliedOperationResult:
        try:
            if isinstance(operation, CreateDocumentOperation):
                return self._create(operation, operation.ref, operation.content)
            if isinstance(operation, CreateChildDocumentOperation):
                return self._create(operation, operation.ref, operation.child_content)
            if isinstance(operation, UpdateDocumentOperation):
                return self._update(operation)
        except Exception as exc:  # noqa: BLE001 - a failing operation never aborts the batch
            return self._result(operation, OperationStatus.FAILED, str(exc))
        return self._result(operation, OperationStatus.SKIPPED, "Unsupported operation")

    def _result(
        self,
        operation: Any,
        status: OperationStatus,
        message: str,
        *,
        ref: DocumentRef | None = None,
        data: bytes | None = None,
    ) -> AppliedOperationResult:
        return AppliedOperationResult(
            operation_id=operation.operation_id,
            status=status,
            message=message,
            resolved_ref=ref,
            resulting_version=None if data is None else _version(data),
        )

    def _create(self, operation: Any, ref: DocumentRef | None, content: str) -> AppliedOperationResult:
        if ref is None:
            raise FakeBackendError("ref.missing", "The fake backend needs an explicit ref to create a page")
        relative, target = self._locate(ref)
        data = content.encode("utf-8")
        if not target.exists():
            self._write(target, data)
            return self._result(operation, OperationStatus.APPLIED, f"created '{relative}'", ref=ref, data=data)
        if target.read_bytes() == data:
            return self._result(operation, OperationStatus.SKIPPED, "unchanged", ref=ref, data=data)
        raise FakeBackendError("conflict.exists", "The document exists with different content", path=relative)

    def _update(self, operation: UpdateDocumentOperation) -> AppliedOperationResult:
        relative, target = self._locate(operation.ref)
        if not target.is_file():
            raise FakeBackendError("document.not_found", "The document to update does not exist", path=relative)
        data = operation.new_content.encode("utf-8")
        current = target.read_bytes()
        if current == data:
            return self._result(operation, OperationStatus.SKIPPED, "unchanged", ref=operation.ref, data=data)
        expected = operation.expected_version
        expected_token = None if expected is None else (expected.token or expected.etag)
        if expected_token is not None and expected_token != _version(current).token:
            raise FakeBackendError("conflict.version_mismatch", "The document changed since it was planned", path=relative)
        self._write(target, data)
        return self._result(operation, OperationStatus.APPLIED, f"updated '{relative}'", ref=operation.ref, data=data)


class FakeFileBackendFactory:
    """Factory of the fake backend, registered as ``fake_files`` by ``manager_with``."""

    provider_id = PROVIDER_ID
    settings_model = FakeFileBackendSettings

    def create(self, settings: FakeFileBackendSettings | dict[str, Any]) -> FakeFileBackend:
        typed = (
            settings
            if isinstance(settings, FakeFileBackendSettings)
            else FakeFileBackendSettings.model_validate(settings)
        )
        return FakeFileBackend(typed)


def manager_with(*factories: ProviderFactory) -> ProviderManager:
    """A ``ProviderManager`` that knows exactly ``factories``, without entry points.

    The only place that touches the manager's private state, so tests and
    production code never depend on how it stores factories.
    """
    manager = ProviderManager()
    registered: Sequence[ProviderFactory] = factories
    manager._factories = {factory.provider_id: factory for factory in registered}  # noqa: SLF001
    manager._loaded = True  # noqa: SLF001
    return manager
