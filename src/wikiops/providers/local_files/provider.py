"""``local_files`` provider: Markdown documents under a configured root directory.

Settings, ref resolution, existence checks, document reads and writes, asset
storage with document-relative links, and the host ``describe_target`` hook.
Every filesystem decision goes through the provider-agnostic
``wikiops.providers._fs`` helpers, whose un-namespaced ``FsError`` instances are
namespaced here, at the provider boundary.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Optional
from urllib.parse import quote

from pydantic import ConfigDict, Field

from wikiops.providers import _fs
from wikiops.providers._fs import FsError
from wikiops.providers.local_files._layout import (
    default_document_path,
    relativize_issued_links,
    require_markdown_path,
)
from wikiops_sdk.contracts import ProviderSettings
from wikiops_sdk.domain import (
    AppliedOperationResult,
    Asset,
    AssetRef,
    AssetRefKind,
    ApplyResult,
    ChangeSet,
    CreateChildDocumentOperation,
    CreateDocumentOperation,
    Document,
    DocumentRef,
    DocumentVersion,
    ExecutionContext,
    OperationStatus,
    ProviderCapability,
    PutAssetOperation,
    RefKind,
    UpdateDocumentOperation,
)

ERROR_NAMESPACE = "local_files"

_PATH_REFS_HINT = "set locator.path to a root-relative POSIX path such as 'docs/a.md'"
_DEFAULT_MEDIA_TYPE = "application/octet-stream"
_CreateOperation = CreateDocumentOperation | CreateChildDocumentOperation
_TOKEN_PREFIX = len("sha256:") + 12


class LocalFilesProviderSettings(ProviderSettings):
    """Settings for the ``local_files`` provider; unknown keys are rejected."""

    model_config = ConfigDict(extra="forbid")

    root: str = Field(
        ...,
        min_length=1,
        description="Root directory; relative values resolve against the working directory and '~' is expanded.",
    )
    assets_dir: str = Field(
        "assets", description="Root-relative directory where assets are stored."
    )
    overwrite_existing: bool = Field(
        False,
        description="Allow create operations to overwrite existing files whose content differs.",
    )


def _error(
    code: str,
    summary: str,
    *,
    path: str | None = None,
    root: Path | None = None,
    hint: str | None = None,
) -> FsError:
    """Build a provider-owned error already carrying the ``local_files`` namespace."""
    return FsError(
        code, summary, path=path, root=root, hint=hint, namespace=ERROR_NAMESPACE
    )


@contextlib.contextmanager
def _namespaced() -> Iterator[None]:
    """Re-raise un-namespaced helper errors with the ``local_files`` namespace."""
    try:
        yield
    except FsError as exc:
        if exc.namespace is not None:
            raise
        raise exc.with_namespace(ERROR_NAMESPACE) from exc


class LocalFilesProvider:
    """Document provider backed by Markdown files below a root directory."""

    provider_id = ERROR_NAMESPACE

    def __init__(self, settings: LocalFilesProviderSettings) -> None:
        self.settings = settings
        self._root_real: Path | None = None
        self._cwd: Path | None = None
        self._assets_real: Path | None = None
        self._issued_asset_links: set[str] = set()

    # -- resolved settings -------------------------------------------------

    def _ensure_resolved(self) -> Path:
        """Resolve the root and assets directory once; never writes."""
        if self._root_real is None:
            cwd = Path.cwd()
            with _namespaced():
                root_real = _fs.resolve_root(self.settings.root, cwd=cwd)
            self._assets_real = self._resolve_assets_dir(root_real)
            self._cwd = cwd
            self._root_real = root_real
        return self._root_real

    def _resolve_assets_dir(self, root_real: Path) -> Path:
        try:
            return _fs.resolve_within_root(root_real, self.settings.assets_dir)
        except FsError as exc:
            raise _error(
                "settings.assets_dir_invalid",
                f"Setting 'assets_dir' is invalid ({exc.code}: {exc.summary})",
                path=self.settings.assets_dir,
                root=root_real,
                hint=exc.hint,
            ) from exc

    # -- DocumentProvider --------------------------------------------------

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
            ProviderCapability.HIERARCHICAL_PAGES,
            ProviderCapability.VERSION_CHECK,
        }

    def validate_settings(self) -> None:
        self._ensure_resolved()

    def resolve_ref(
        self, ref: DocumentRef, ctx: Optional[ExecutionContext] = None
    ) -> DocumentRef:
        self._locate(ref)
        if not ref.provider:
            return ref.model_copy(update={"provider": self.settings.provider_name})
        return ref

    def exists(self, ref: DocumentRef) -> bool:
        _, real = self._locate(ref)
        return real.is_file()

    def get_document(self, ref: DocumentRef) -> Document:
        relative, real = self._locate(ref)
        root_real = self._ensure_resolved()
        data = self._read_existing(relative, real)
        if data is None:
            raise _error(
                "document.not_found",
                f"Document does not exist (absolute='{real}', cwd='{self._cwd}')",
                path=relative,
                root=root_real,
                hint="create the document first or remove the ref from the plugin's required refs",
            )
        with _namespaced():
            content = _fs.decode_text(data, path=relative)
        version = _fs.content_version(data)
        return Document(
            ref=ref,
            title=PurePosixPath(relative).stem,
            content=content,
            version=DocumentVersion(token=version, etag=version),
            metadata={
                "path": relative,
                "absolute_path": str(real),
                "size_bytes": len(data),
            },
        )

    def _read_existing(self, relative: str, real: Path) -> bytes | None:
        """Return the bytes of the file at ``real``, or ``None`` when it is missing."""
        root_real = self._ensure_resolved()
        if not real.exists():
            return None
        if not real.is_file():
            raise _error(
                "path.not_a_file",
                "Document path is not a regular file",
                path=relative,
                root=root_real,
                hint="point the ref at a Markdown file, not a directory",
            )
        try:
            return real.read_bytes()
        except OSError as exc:
            raise _error(
                "io.error",
                f"Could not read the document ({type(exc).__name__}: {exc})",
                path=relative,
                root=root_real,
                hint="check the permissions of the file",
            ) from exc

    def build_link(self, ref: DocumentRef) -> Optional[str]:
        _, real = self._locate(ref)
        return real.as_uri()

    def put_asset(self, operation: PutAssetOperation, content: bytes) -> Asset:
        """Store ``content`` under a content-hashed name inside ``assets_dir``.

        Identical bytes already stored are not rewritten; different bytes at the
        hashed name are refused instead of being reused.
        """
        root_real = self._ensure_resolved()
        with _namespaced():
            name = _fs.hashed_asset_name(operation.name, content)
        relative = str(PurePosixPath(self.settings.assets_dir) / name)
        with _namespaced():
            real = _fs.resolve_within_root(root_real, relative)
        existing = self._read_existing(relative, real)
        if existing is None:
            self._write(real, content, root_real)
        elif existing != content:
            raise _error(
                "conflict.asset_mismatch",
                "An asset with this hashed name already exists with different content",
                path=relative,
                root=root_real,
                hint="delete or rename the stored file, then upload the asset again",
            )
        version = _fs.content_version(content)
        return Asset(
            ref=AssetRef(
                provider=self.settings.provider_name,
                kind=AssetRefKind.PATH,
                locator={"path": relative},
            ),
            name=name,
            media_type=operation.media_type or _DEFAULT_MEDIA_TYPE,
            size_bytes=len(content),
            version=DocumentVersion(token=version, etag=version),
            metadata={
                "path": relative,
                "absolute_path": str(real),
                "unchanged": existing is not None,
            },
        )

    def build_asset_reference(self, ref: AssetRef) -> str:
        """Return the root-anchored reference for ``ref`` and remember it as issued.

        ``apply_changes`` turns exactly the references issued here into links
        relative to each document.
        """
        if ref.kind is not AssetRefKind.PATH:
            raise _error(
                "ref.unsupported_kind",
                f"local_files requires path asset refs with locator.path (got kind '{ref.kind.value}')",
                hint=_PATH_REFS_HINT,
            )
        path = ref.locator.get("path")
        if not path:
            raise _error(
                "ref.missing_path",
                "local_files requires path asset refs with locator.path (locator.path is missing)",
                hint=_PATH_REFS_HINT,
            )
        reference = "/" + quote(path, safe="/")
        self._issued_asset_links.add(reference)
        return reference

    def apply_changes(self, changeset: ChangeSet) -> ApplyResult:
        """Apply operations one by one; a failure never stops the later operations."""
        return ApplyResult(
            provider_name=self.settings.provider_name,
            results=[self._apply_operation(op) for op in changeset.operations],
        )

    # -- Host hook ---------------------------------------------------------

    def describe_target(self) -> str:
        """Describe where this provider reads and writes, for the plan note."""
        root_real = self._ensure_resolved()
        configured = self.settings.root.strip()
        parts = [f"root='{root_real}'"]
        if configured.startswith("~") or not Path(configured).is_absolute():
            parts.append(f"configured='{self.settings.root}'")
        parts.append(f"cwd='{self._cwd}'")
        parts.append(f"assets_dir='{self._assets_real}'")
        return " ".join(parts)

    # -- apply_changes -----------------------------------------------------

    def _apply_operation(self, operation: Any) -> AppliedOperationResult:
        """Apply one operation; every error becomes a FAILED result, never a raise."""
        ref: DocumentRef | None = None
        try:
            if isinstance(operation, (CreateDocumentOperation, CreateChildDocumentOperation)):
                ref = self._target_for(operation)
                return self._apply_create(operation, ref)
            if isinstance(operation, UpdateDocumentOperation):
                ref = operation.ref
                return self._apply_update(operation)
            return self._unsupported(operation)
        except FsError as exc:
            namespaced = exc if exc.namespace else exc.with_namespace(ERROR_NAMESPACE)
            return self._result(
                operation, OperationStatus.FAILED, str(namespaced), ref=ref
            )
        except Exception as exc:  # noqa: BLE001 - one operation must never abort the batch
            unexpected = _error(
                "io.error",
                f"Unexpected error while applying the operation ({type(exc).__name__}: {exc})",
                path=self._relative_of(ref),
                root=self._root_real,
                hint="check the permissions and state of the target, then re-run",
            )
            return self._result(
                operation, OperationStatus.FAILED, str(unexpected), ref=ref
            )

    def _unsupported(self, operation: Any) -> AppliedOperationResult:
        message = _error(
            "op.unsupported",
            f"Unsupported operation type: {type(operation).__name__}",
            hint="local_files applies create_document, create_child_document and update_document",
        )
        return self._result(operation, OperationStatus.SKIPPED, str(message))

    @staticmethod
    def _relative_of(ref: DocumentRef | None) -> str | None:
        """Return the locator path of ``ref`` for error context, when it has one."""
        path = None if ref is None else ref.locator.get("path")
        return path if isinstance(path, str) else None

    def _result(
        self,
        operation: Any,
        status: OperationStatus,
        message: str,
        *,
        ref: DocumentRef | None = None,
        data: bytes | None = None,
    ) -> AppliedOperationResult:
        version = None
        if data is not None:
            token = _fs.content_version(data)
            version = DocumentVersion(token=token, etag=token)
        return AppliedOperationResult(
            operation_id=operation.operation_id,
            status=status,
            message=message,
            resolved_ref=ref,
            resulting_version=version,
        )

    def _target_for(self, operation: _CreateOperation) -> DocumentRef:
        """Return the target of a create: an explicit ``ref`` wins, else derive it.

        An explicit ``ref`` is used verbatim and ``parent_ref`` / the title are
        ignored. Derivation from the title and the optional parent is only the
        fallback for operations that carry no ``ref``.
        """
        if operation.ref is not None:
            return operation.ref
        parent_path = None
        if operation.parent_ref is not None:
            parent_path, _ = self._locate(operation.parent_ref)
        title = (
            operation.child_title
            if isinstance(operation, CreateChildDocumentOperation)
            else operation.title
        )
        with _namespaced():
            path = default_document_path(title=title, parent_path=parent_path)
        return DocumentRef(
            provider=self.settings.provider_name,
            kind=RefKind.PATH,
            locator={"path": path},
        )

    def _apply_create(
        self, operation: _CreateOperation, ref: DocumentRef
    ) -> AppliedOperationResult:
        content = (
            operation.child_content
            if isinstance(operation, CreateChildDocumentOperation)
            else operation.content
        )
        relative, real = self._locate(ref)
        root_real = self._ensure_resolved()
        data = self._encode(self._relativize(content, relative), relative, root_real)
        existing = self._read_existing(relative, real)
        if existing is None:
            self._write(real, data, root_real)
            return self._result(
                operation,
                OperationStatus.APPLIED,
                f"Document created at '{real}'",
                ref=ref,
                data=data,
            )
        if existing == data:
            return self._unchanged(operation, ref, relative, root_real, data)
        if not self.settings.overwrite_existing:
            raise _error(
                "conflict.exists",
                "Document already exists with different content",
                path=relative,
                root=root_real,
                hint=(
                    "use update_document, or set 'overwrite_existing: true' on "
                    f"provider '{self.settings.provider_name}'"
                ),
            )
        self._write(real, data, root_real)
        overwritten = _error(
            "applied.overwritten",
            f"Existing document overwritten at '{real}'",
            path=relative,
            root=root_real,
        )
        return self._result(
            operation, OperationStatus.APPLIED, str(overwritten), ref=ref, data=data
        )

    def _apply_update(self, operation: UpdateDocumentOperation) -> AppliedOperationResult:
        """Update in order: validate, missing, identical, version, write."""
        relative, real = self._locate(operation.ref)
        root_real = self._ensure_resolved()
        data = self._encode(
            self._relativize(operation.new_content, relative), relative, root_real
        )
        existing = self._read_existing(relative, real)
        if existing is None:
            raise _error(
                "document.not_found",
                "Document does not exist, so it cannot be updated",
                path=relative,
                root=root_real,
                hint="use create_document to create it first",
            )
        if existing == data:
            return self._unchanged(operation, operation.ref, relative, root_real, data)
        expected = self._expected_token(operation.expected_version)
        current = _fs.content_version(existing)
        if expected is not None and expected != current:
            raise _error(
                "conflict.version_mismatch",
                "Document changed since it was planned "
                f"(expected '{expected[:_TOKEN_PREFIX]}', current '{current[:_TOKEN_PREFIX]}')",
                path=relative,
                root=root_real,
                hint="re-run the plan to refresh the expected version, then apply again",
            )
        self._write(real, data, root_real)
        return self._result(
            operation,
            OperationStatus.APPLIED,
            f"Document updated at '{real}'",
            ref=operation.ref,
            data=data,
        )

    def _relativize(self, content: str, relative: str) -> str:
        """Turn the asset references issued by this provider into links relative to ``relative``."""
        return relativize_issued_links(content, self._issued_asset_links, relative)

    @staticmethod
    def _encode(content: str, relative: str, root_real: Path) -> bytes:
        """Encode ``content`` as UTF-8, reporting unencodable text accurately."""
        try:
            return content.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise _error(
                "document.encode_error",
                f"Content is not valid UTF-8-encodable ({exc.reason} at character {exc.start})",
                path=relative,
                root=root_real,
                hint="remove or replace unpaired surrogate characters in the content",
            ) from exc

    @staticmethod
    def _expected_token(version: DocumentVersion | None) -> str | None:
        """Return the version the plugin expects: the token, else the etag, else none."""
        if version is None:
            return None
        return version.token or version.etag or None

    def _unchanged(
        self,
        operation: Any,
        ref: DocumentRef,
        relative: str,
        root_real: Path,
        data: bytes,
    ) -> AppliedOperationResult:
        message = _error(
            "noop.unchanged",
            "Document already has this content; nothing was written",
            path=relative,
            root=root_real,
        )
        return self._result(
            operation, OperationStatus.SKIPPED, str(message), ref=ref, data=data
        )

    @staticmethod
    def _write(real: Path, data: bytes, root_real: Path) -> None:
        with _namespaced():
            _fs.atomic_write_bytes(real, data, root_real=root_real)

    # -- Path resolution ---------------------------------------------------

    def _locate(self, ref: DocumentRef) -> tuple[str, Path]:
        """Validate ``ref`` and return its root-relative path and real path."""
        relative = self._path_of(ref)
        root_real = self._ensure_resolved()
        with _namespaced():
            _fs.validate_relative_path(relative)
            require_markdown_path(relative)
            return relative, _fs.resolve_within_root(root_real, relative)

    @staticmethod
    def _path_of(ref: DocumentRef) -> str:
        if ref.kind != RefKind.PATH:
            raise _error(
                "ref.unsupported_kind",
                f"local_files requires path refs with locator.path (got kind '{ref.kind.value}')",
                hint=_PATH_REFS_HINT,
            )
        path = ref.locator.get("path")
        if path is None:
            raise _error(
                "ref.missing_path",
                "local_files requires path refs with locator.path (locator.path is missing)",
                hint=_PATH_REFS_HINT,
            )
        return path


class LocalFilesProviderFactory:
    """Factory for ``local_files`` providers."""

    provider_id = LocalFilesProvider.provider_id
    settings_model = LocalFilesProviderSettings

    def create(
        self, settings: LocalFilesProviderSettings | Dict[str, Any]
    ) -> LocalFilesProvider:
        typed_settings = (
            settings
            if isinstance(settings, LocalFilesProviderSettings)
            else self.settings_model.model_validate(settings)
        )
        return LocalFilesProvider(typed_settings)
