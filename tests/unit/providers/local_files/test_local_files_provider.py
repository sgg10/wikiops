"""Unit tests for the ``local_files`` provider read side (settings, refs, reads)."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from wikiops.core.exceptions import ConfigurationError
from wikiops.core.provider_manager import ProviderManager, TargetDescribingProvider
from wikiops.providers._fs import FsError
from wikiops.providers.local_files import LocalFilesProviderFactory
from wikiops.providers.local_files.provider import (
    ERROR_NAMESPACE,
    LocalFilesProvider,
    LocalFilesProviderSettings,
    _namespaced,
)
from wikiops_sdk.contracts import DocumentProvider
from wikiops_sdk.domain import (
    AppliedOperationResult,
    ApplyResult,
    AssetRef,
    AssetRefKind,
    ChangeSet,
    CreateChildDocumentOperation,
    CreateDocumentOperation,
    DocumentRef,
    DocumentVersion,
    OperationStatus,
    ProviderCapability,
    PutAssetOperation,
    RefKind,
    UpdateDocumentOperation,
)

PROVIDER_NAME = "docs"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """Return an existing, fully resolved root directory."""
    real = tmp_path.resolve() / "root"
    real.mkdir()
    return real


@pytest.fixture
def symlinks() -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("os.symlink is not available on this platform")


@pytest.fixture
def make_symlink(symlinks: None):
    def _make(link: Path, target: Path) -> Path:
        try:
            link.symlink_to(target, target_is_directory=target.is_dir())
        except (OSError, NotImplementedError):
            pytest.skip("symlinks cannot be created here")
        return link

    return _make


def _settings(root: Path | str, **extra: Any) -> dict[str, Any]:
    return {"provider_name": PROVIDER_NAME, "root": str(root), **extra}


def _provider(root: Path | str, **extra: Any) -> LocalFilesProvider:
    return LocalFilesProviderFactory().create(_settings(root, **extra))


def _ref(path: str | None, **overrides: Any) -> DocumentRef:
    locator = {} if path is None else {"path": path}
    fields: dict[str, Any] = {
        "provider": PROVIDER_NAME,
        "kind": RefKind.PATH,
        "locator": locator,
    }
    fields.update(overrides)
    return DocumentRef(**fields)


def _write(root: Path, relative: str, data: bytes) -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_settings_apply_defaults(root: Path) -> None:
    settings = LocalFilesProviderSettings.model_validate(_settings(root))

    assert settings.root == str(root)
    assert settings.assets_dir == "assets"
    assert settings.overwrite_existing is False


def test_settings_accept_explicit_values(root: Path) -> None:
    settings = LocalFilesProviderSettings.model_validate(
        _settings(root, assets_dir="docs/img", overwrite_existing=True)
    )

    assert settings.assets_dir == "docs/img"
    assert settings.overwrite_existing is True


def test_settings_require_root() -> None:
    with pytest.raises(ValidationError) as excinfo:
        LocalFilesProviderSettings.model_validate({"provider_name": PROVIDER_NAME})

    assert [error["loc"] for error in excinfo.value.errors()] == [("root",)]


def test_settings_reject_empty_root() -> None:
    with pytest.raises(ValidationError) as excinfo:
        LocalFilesProviderSettings.model_validate(_settings(""))

    assert [error["loc"] for error in excinfo.value.errors()] == [("root",)]


@pytest.mark.parametrize(
    "unknown",
    [{"overwrite_exising": True}, {"asset_link_prefix": "/"}],
)
def test_settings_reject_unknown_keys_naming_them(
    root: Path, unknown: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError) as excinfo:
        LocalFilesProviderSettings.model_validate(_settings(root, **unknown))

    (key,) = unknown
    assert [error["loc"] for error in excinfo.value.errors()] == [(key,)]
    assert key in str(excinfo.value)


# ---------------------------------------------------------------------------
# Factory and host integration
# ---------------------------------------------------------------------------


def test_factory_exposes_identity_and_settings_model() -> None:
    factory = LocalFilesProviderFactory()

    assert factory.provider_id == "local_files"
    assert factory.settings_model is LocalFilesProviderSettings
    assert ERROR_NAMESPACE == "local_files"


def test_factory_creates_provider_from_dict(root: Path) -> None:
    provider = LocalFilesProviderFactory().create(_settings(root))

    assert provider.provider_id == "local_files"
    assert isinstance(provider, DocumentProvider)
    assert isinstance(provider, TargetDescribingProvider)
    assert provider.settings.root == str(root)
    assert provider.settings.provider_name == PROVIDER_NAME


def test_factory_creates_provider_from_typed_settings(root: Path) -> None:
    typed = LocalFilesProviderSettings(provider_name="typed", root=str(root))

    provider = LocalFilesProviderFactory().create(typed)

    assert provider.settings is typed
    assert isinstance(provider, DocumentProvider)


def test_factory_does_not_touch_the_filesystem(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"

    provider = LocalFilesProviderFactory().create(_settings(missing))

    assert provider.settings.root == str(missing)
    assert not missing.exists()


def test_provider_manager_creates_and_validates_the_provider(
    monkeypatch: pytest.MonkeyPatch, entry_point_factory, root: Path
) -> None:
    monkeypatch.setattr(
        "wikiops.core.provider_manager.entry_points",
        lambda **_: [entry_point_factory("local_files", LocalFilesProviderFactory)],
    )

    provider = ProviderManager().create("local_files", _settings(root))

    assert isinstance(provider, LocalFilesProvider)
    assert f"root='{root}'" in provider.describe_target()


def test_provider_manager_reports_missing_root_with_namespaced_code(
    monkeypatch: pytest.MonkeyPatch, entry_point_factory, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "wikiops.core.provider_manager.entry_points",
        lambda **_: [entry_point_factory("local_files", LocalFilesProviderFactory)],
    )
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ConfigurationError) as excinfo:
        ProviderManager().create("local_files", _settings("missing-root"))

    message = str(excinfo.value)
    assert message.startswith("[local_files:settings.root_missing]")
    assert "configured='missing-root'" in message
    assert f"root='{Path.cwd() / 'missing-root'}'" in message
    assert f"cwd='{Path.cwd()}'" in message
    assert "Hint:" in message
    assert not (tmp_path / "missing-root").exists()


# ---------------------------------------------------------------------------
# Namespacing boundary
# ---------------------------------------------------------------------------


def test_namespaced_attaches_the_namespace_to_helper_errors_keeping_the_cause() -> None:
    helper_error = FsError("path.traversal", "Escapes", path="../x.md", hint="fix it")

    with pytest.raises(FsError) as excinfo:
        with _namespaced():
            raise helper_error

    error = excinfo.value
    assert error is not helper_error
    assert error.__cause__ is helper_error
    assert error.namespace == "local_files"
    assert str(error) == "[local_files:path.traversal] Escapes. path='../x.md'. Hint: fix it."
    assert helper_error.namespace is None


def test_namespaced_leaves_already_namespaced_errors_untouched() -> None:
    owned = FsError("conflict.exists", "Exists", namespace="local_files")

    with pytest.raises(FsError) as excinfo:
        with _namespaced():
            raise owned

    assert excinfo.value is owned


def test_namespaced_does_not_swallow_other_exceptions() -> None:
    with pytest.raises(KeyError):
        with _namespaced():
            raise KeyError("boom")


# ---------------------------------------------------------------------------
# validate_settings
# ---------------------------------------------------------------------------


def test_validate_settings_performs_no_writes(root: Path) -> None:
    _write(root, "docs/a.md", b"x")
    before = sorted(path.relative_to(root) for path in root.rglob("*"))

    _provider(root).validate_settings()

    assert sorted(path.relative_to(root) for path in root.rglob("*")) == before
    assert not (root / "assets").exists()


def test_validate_settings_rejects_a_root_that_is_a_file(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("x")

    with pytest.raises(FsError) as excinfo:
        _provider(target).validate_settings()

    assert str(excinfo.value).startswith("[local_files:settings.root_not_directory]")


def test_validate_settings_never_falls_back_to_cwd_for_blank_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(FsError) as excinfo:
        _provider("   ").validate_settings()

    assert str(excinfo.value).startswith("[local_files:settings.root_missing]")


@pytest.mark.parametrize(
    ("assets_dir", "cause"),
    [
        ("../outside", "path.traversal"),
        ("/abs", "path.absolute"),
        ("assets/../../x", "path.traversal"),
        (".git/assets", "path.reserved"),
    ],
)
def test_validate_settings_rejects_invalid_assets_dir_naming_the_cause(
    root: Path, assets_dir: str, cause: str
) -> None:
    with pytest.raises(FsError) as excinfo:
        _provider(root, assets_dir=assets_dir).validate_settings()

    error = excinfo.value
    assert str(error).startswith("[local_files:settings.assets_dir_invalid]")
    assert error.code == "settings.assets_dir_invalid"
    assert error.namespace == "local_files"
    assert cause in str(error)
    assert "Hint:" in str(error)


def test_validate_settings_rejects_assets_dir_symlink_escaping_the_root(
    root: Path, tmp_path: Path, make_symlink
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    make_symlink(root / "assets", outside)

    with pytest.raises(FsError) as excinfo:
        _provider(root).validate_settings()

    assert str(excinfo.value).startswith("[local_files:settings.assets_dir_invalid]")
    assert "path.symlink_escape" in str(excinfo.value)


def test_validate_settings_accepts_missing_nested_assets_dir_without_creating_it(
    root: Path,
) -> None:
    _provider(root, assets_dir="docs/img").validate_settings()

    assert not (root / "docs").exists()


# ---------------------------------------------------------------------------
# resolve_ref
# ---------------------------------------------------------------------------


def test_resolve_ref_returns_the_same_ref_when_provider_is_set(root: Path) -> None:
    ref = _ref("docs/README.md")

    resolved = _provider(root).resolve_ref(ref)

    assert resolved is ref
    assert resolved.locator == {"path": "docs/README.md"}
    assert resolved.provider == PROVIDER_NAME


def test_resolve_ref_fills_an_empty_provider_without_mutating_the_input(
    root: Path,
) -> None:
    ref = _ref("docs/README.md", provider="")

    resolved = _provider(root).resolve_ref(ref)

    assert resolved.provider == PROVIDER_NAME
    assert resolved.locator == {"path": "docs/README.md"}
    assert ref.provider == ""


def test_resolve_ref_keeps_a_foreign_provider_name(root: Path) -> None:
    ref = _ref("a.md", provider="other")

    assert _provider(root).resolve_ref(ref).provider == "other"


def test_resolve_ref_accepts_an_uppercase_markdown_extension(root: Path) -> None:
    ref = _ref("docs/README.MD")

    assert _provider(root).resolve_ref(ref).locator == {"path": "docs/README.MD"}


def test_resolve_ref_rejects_alias_refs(root: Path) -> None:
    ref = DocumentRef(provider=PROVIDER_NAME, kind=RefKind.ALIAS, alias="inventory")

    with pytest.raises(FsError) as excinfo:
        _provider(root).resolve_ref(ref)

    message = str(excinfo.value)
    assert message.startswith("[local_files:ref.unsupported_kind]")
    assert "local_files requires path refs with locator.path" in message


def test_resolve_ref_rejects_path_refs_without_a_path(root: Path) -> None:
    with pytest.raises(FsError) as excinfo:
        _provider(root).resolve_ref(_ref(None))

    message = str(excinfo.value)
    assert message.startswith("[local_files:ref.missing_path]")
    assert "local_files requires path refs with locator.path" in message


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("../x.md", "path.traversal"),
        ("docs/../../secret.md", "path.traversal"),
        ("/etc/passwd.md", "path.absolute"),
        ("", "path.empty"),
        (".git/config.md", "path.reserved"),
        (".git/config", "path.reserved"),
        ("docs//a.md", "path.non_canonical"),
    ],
)
def test_resolve_ref_namespaces_helper_errors(
    root: Path, path: str, code: str
) -> None:
    with pytest.raises(FsError) as excinfo:
        _provider(root).resolve_ref(_ref(path))

    assert excinfo.value.code == code
    assert excinfo.value.namespace == "local_files"
    assert str(excinfo.value).startswith(f"[local_files:{code}]")


@pytest.mark.parametrize(
    ("path", "suggestion"),
    [
        ("docs/README", "docs/README.md"),
        ("docs/v1.2", "docs/v1.2.md"),
        ("docs/notes.markdown", "docs/notes.md"),
        ("docs/a.txt", "docs/a.md"),
        ("pyproject.toml", "pyproject.toml.md"),
        (".github/workflows/ci.yml", ".github/workflows/ci.yml.md"),
    ],
)
def test_resolve_ref_enforces_the_markdown_rule_with_a_hint(
    root: Path, path: str, suggestion: str
) -> None:
    with pytest.raises(FsError) as excinfo:
        _provider(root).resolve_ref(_ref(path))

    error = excinfo.value
    assert str(error).startswith("[local_files:path.not_markdown]")
    assert error.hint is not None and suggestion in error.hint
    assert not (root / path).exists()


def test_resolve_ref_rejects_an_escaping_symlink_at_plan_time(
    root: Path, tmp_path: Path, make_symlink
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    make_symlink(root / "link", outside)

    with pytest.raises(FsError) as excinfo:
        _provider(root).resolve_ref(_ref("link/x.md"))

    assert str(excinfo.value).startswith("[local_files:path.symlink_escape]")


def test_resolve_ref_allows_an_in_root_symlink(root: Path, make_symlink) -> None:
    (root / "real").mkdir()
    make_symlink(root / "alias", root / "real")
    ref = _ref("alias/x.md")

    assert _provider(root).resolve_ref(ref) is ref


# ---------------------------------------------------------------------------
# exists
# ---------------------------------------------------------------------------


def test_exists_is_true_for_a_file_and_false_for_a_missing_one(root: Path) -> None:
    _write(root, "a.md", b"# A")
    provider = _provider(root)

    assert provider.exists(_ref("a.md")) is True
    assert provider.exists(_ref("b.md")) is False


def test_exists_is_false_for_a_directory(root: Path) -> None:
    (root / "docs.md").mkdir()

    assert _provider(root).exists(_ref("docs.md")) is False


def test_exists_is_false_when_a_parent_component_is_a_file(root: Path) -> None:
    _write(root, "a.md", b"# A")

    assert _provider(root).exists(_ref("a.md/b.md")) is False


def test_exists_raises_instead_of_returning_false_on_escape(
    root: Path, tmp_path: Path, make_symlink
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.md").write_text("secret")
    make_symlink(root / "out.md", outside / "x.md")

    with pytest.raises(FsError) as excinfo:
        _provider(root).exists(_ref("out.md"))

    assert str(excinfo.value).startswith("[local_files:path.symlink_escape]")


@pytest.mark.parametrize("path", ["README", "../x.md"])
def test_exists_raises_on_markdown_and_traversal_violations(
    root: Path, path: str
) -> None:
    with pytest.raises(FsError):
        _provider(root).exists(_ref(path))


# ---------------------------------------------------------------------------
# get_document
# ---------------------------------------------------------------------------


def test_get_document_returns_exact_content_title_version_and_metadata(
    root: Path,
) -> None:
    data = b"# Hi\r\n"
    target = _write(root, "docs/a.md", data)
    ref = _ref("docs/a.md")

    document = _provider(root).get_document(ref)

    digest = f"sha256:{hashlib.sha256(data).hexdigest()}"
    assert document.ref is ref
    assert document.content == "# Hi\r\n"
    assert document.title == "a"
    assert document.version is not None
    assert document.version.token == digest
    assert document.version.etag == digest
    assert document.metadata == {
        "path": "docs/a.md",
        "absolute_path": str(target.resolve()),
        "size_bytes": 6,
    }


def test_get_document_keeps_bom_and_returns_stem_for_uppercase_extension(
    root: Path,
) -> None:
    _write(root, "Notes.MD", b"\xef\xbb\xbf# Title\n")

    document = _provider(root).get_document(_ref("Notes.MD"))

    assert document.content == "﻿# Title\n"
    assert document.title == "Notes"


def test_get_document_missing_file_reports_actionable_not_found(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(root)

    with pytest.raises(FsError) as excinfo:
        _provider(root).get_document(_ref("nope.md"))

    message = str(excinfo.value)
    assert message.startswith("[local_files:document.not_found]")
    assert "path='nope.md'" in message
    assert f"root='{root}'" in message
    assert f"cwd='{Path.cwd()}'" in message
    assert "Hint:" in message
    assert not (root / "nope.md").exists()


def test_get_document_invalid_utf8_names_the_file(root: Path) -> None:
    _write(root, "bad.md", b"\xff\xfe")

    with pytest.raises(FsError) as excinfo:
        _provider(root).get_document(_ref("bad.md"))

    message = str(excinfo.value)
    assert message.startswith("[local_files:document.decode_error]")
    assert "path='bad.md'" in message


def test_get_document_directory_is_not_a_file(root: Path) -> None:
    (root / "docs.md").mkdir()

    with pytest.raises(FsError) as excinfo:
        _provider(root).get_document(_ref("docs.md"))

    assert str(excinfo.value).startswith("[local_files:path.not_a_file]")


def test_get_document_read_failure_becomes_a_namespaced_io_error(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(root, "a.md", b"x")

    def _deny(self: Path) -> bytes:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "read_bytes", _deny)

    with pytest.raises(FsError) as excinfo:
        _provider(root).get_document(_ref("a.md"))

    message = str(excinfo.value)
    assert message.startswith("[local_files:io.error]")
    assert "PermissionError" in message
    assert "path='a.md'" in message
    assert "Hint:" in message


def test_get_document_rejects_escaping_symlink_without_reading(
    root: Path, tmp_path: Path, make_symlink
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.md").write_text("secret")
    make_symlink(root / "out.md", outside / "x.md")

    with pytest.raises(FsError) as excinfo:
        _provider(root).get_document(_ref("out.md"))

    assert str(excinfo.value).startswith("[local_files:path.symlink_escape]")
    assert "secret" not in str(excinfo.value)


def test_get_document_reads_through_an_in_root_symlink(
    root: Path, make_symlink
) -> None:
    _write(root, "real/a.md", b"# Real")
    make_symlink(root / "alias", root / "real")

    document = _provider(root).get_document(_ref("alias/a.md"))

    assert document.content == "# Real"
    assert document.metadata["path"] == "alias/a.md"
    assert document.metadata["absolute_path"] == str(root / "real" / "a.md")


# ---------------------------------------------------------------------------
# build_link
# ---------------------------------------------------------------------------


def test_build_link_returns_the_file_uri_of_the_real_path(root: Path) -> None:
    _write(root, "docs/a.md", b"x")

    link = _provider(root).build_link(_ref("docs/a.md"))

    assert link == Path(root / "docs" / "a.md").resolve().as_uri()
    assert link.startswith("file://")


def test_build_link_does_not_require_the_document_to_exist(root: Path) -> None:
    link = _provider(root).build_link(_ref("docs/future.md"))

    assert link == Path(root / "docs" / "future.md").resolve().as_uri()
    assert not (root / "docs").exists()


@pytest.mark.parametrize(
    ("path", "code"),
    [("docs/a", "path.not_markdown"), ("../a.md", "path.traversal")],
)
def test_build_link_enforces_markdown_and_confinement(
    root: Path, path: str, code: str
) -> None:
    with pytest.raises(FsError) as excinfo:
        _provider(root).build_link(_ref(path))

    assert excinfo.value.code == code


# ---------------------------------------------------------------------------
# describe_target and capabilities
# ---------------------------------------------------------------------------


def test_describe_target_for_an_absolute_root_has_no_configured_segment(
    root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)

    description = _provider(root).describe_target()

    assert description == (
        f"root='{root}' cwd='{Path.cwd()}' assets_dir='{root / 'assets'}'"
    )
    assert "configured=" not in description


def test_describe_target_for_a_relative_root_shows_the_configured_value(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (root / "docs").mkdir()
    monkeypatch.chdir(root)

    description = _provider("docs").describe_target()

    assert f"root='{root / 'docs'}'" in description
    assert "configured='docs'" in description
    assert f"cwd='{Path.cwd()}'" in description
    assert f"assets_dir='{root / 'docs' / 'assets'}'" in description


def test_describe_target_for_a_tilde_root_shows_the_configured_value(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = root / "home"
    (home / "wiki").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    description = _provider("~/wiki").describe_target()

    assert f"root='{home / 'wiki'}'" in description
    assert "configured='~/wiki'" in description


def test_describe_target_reflects_a_custom_assets_dir(root: Path) -> None:
    description = _provider(root, assets_dir="docs/img").describe_target()

    assert f"assets_dir='{root / 'docs' / 'img'}'" in description


def test_describe_target_shows_different_roots_for_different_working_dirs(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("one", "two"):
        (root / name / "wiki").mkdir(parents=True)

    monkeypatch.chdir(root / "one")
    first = _provider("wiki").describe_target()
    monkeypatch.chdir(root / "two")
    second = _provider("wiki").describe_target()

    assert f"root='{root / 'one' / 'wiki'}'" in first
    assert f"root='{root / 'two' / 'wiki'}'" in second


def test_describe_target_resolves_the_root_once_per_provider(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (root / "docs").mkdir()
    (root / "other").mkdir()
    monkeypatch.chdir(root)
    provider = _provider("docs")
    provider.validate_settings()

    monkeypatch.chdir(root / "other")

    assert f"root='{root / 'docs'}'" in provider.describe_target()
    assert f"cwd='{root}'" in provider.describe_target()


def test_capabilities_expose_only_the_implemented_read_side(root: Path) -> None:
    assert _provider(root).capabilities() == {
        ProviderCapability.READ_DOCUMENT,
        ProviderCapability.CHECK_EXISTS,
        ProviderCapability.BUILD_LINK,
        ProviderCapability.RESOLVE_BY_PATH,
    }


def test_asset_side_is_not_implemented_yet(root: Path) -> None:
    provider = _provider(root)
    asset_ref = AssetRef(
        provider=PROVIDER_NAME, kind=AssetRefKind.PATH, locator={"path": "a.png"}
    )

    with pytest.raises(NotImplementedError):
        provider.build_asset_reference(asset_ref)
    with pytest.raises(NotImplementedError):
        provider.put_asset(
            PutAssetOperation.model_construct(name="a.png"),
            b"x",
        )


# ---------------------------------------------------------------------------
# apply_changes: create_document and create_child_document (LF-8, LF-9)
# ---------------------------------------------------------------------------


def _sha(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _create(content: str = "x", title: str = "Doc", **fields: Any) -> CreateDocumentOperation:
    return CreateDocumentOperation(title=title, content=content, **fields)


def _child(
    parent: str = "docs/guide.md",
    content: str = "x",
    title: str = "Child",
    **fields: Any,
) -> CreateChildDocumentOperation:
    return CreateChildDocumentOperation(
        parent_ref=_ref(parent), child_title=title, child_content=content, **fields
    )


def _apply(provider: LocalFilesProvider, *operations: Any) -> ApplyResult:
    return provider.apply_changes(
        ChangeSet.model_construct(
            plugin_id="demo.plugin", operations=list(operations), warnings=[], notes=[]
        )
    )


def _apply_one(provider: LocalFilesProvider, operation: Any) -> AppliedOperationResult:
    result = _apply(provider, operation)
    assert len(result.results) == 1
    return result.results[0]


def _tree(base: Path) -> list[str]:
    return sorted(str(path.relative_to(base)) for path in base.rglob("*"))


def test_create_at_an_explicit_ref_writes_the_file_and_reports_it(root: Path) -> None:
    operation = _create("x", ref=_ref("docs/a.md"))

    result = _apply_one(_provider(root), operation)

    assert (root / "docs" / "a.md").read_bytes() == b"x"
    assert result.operation_id == operation.operation_id
    assert result.status is OperationStatus.APPLIED
    assert result.resolved_ref is not None
    assert result.resolved_ref.locator == {"path": "docs/a.md"}
    assert result.resulting_version == DocumentVersion(token=_sha(b"x"), etag=_sha(b"x"))
    assert str(root / "docs" / "a.md") in (result.message or "")
    assert "created" in (result.message or "")


def test_create_writes_exact_bytes_including_crlf_and_bom(root: Path) -> None:
    content = "\ufeffline1\r\nline2\r\n"

    _apply_one(_provider(root), _create(content, ref=_ref("a.md")))

    assert (root / "a.md").read_bytes() == content.encode("utf-8")


def test_explicit_ref_wins_over_parent_ref_and_title(root: Path) -> None:
    operation = _create(
        "x",
        title="Other",
        ref=_ref("docs/examples/foo.md"),
        parent_ref=_ref("docs/guide.md"),
    )

    result = _apply_one(_provider(root), operation)

    assert result.status is OperationStatus.APPLIED
    assert _tree(root) == ["docs", "docs/examples", "docs/examples/foo.md"]
    assert (root / "docs/examples/foo.md").read_bytes() == b"x"


@pytest.mark.parametrize(
    "parent",
    ["docs/missing-parent.md", "../escape", "docs/not-markdown", "/abs.md"],
)
def test_explicit_ref_ignores_an_absent_or_invalid_parent_ref(
    root: Path, parent: str
) -> None:
    operation = _create("x", ref=_ref("docs/ok.md"), parent_ref=_ref(parent))

    result = _apply_one(_provider(root), operation)

    assert result.status is OperationStatus.APPLIED
    assert (root / "docs" / "ok.md").read_bytes() == b"x"


def test_explicit_ref_ignores_an_invalid_title(root: Path) -> None:
    result = _apply_one(_provider(root), _create("x", title="a/b", ref=_ref("docs/ok.md")))

    assert result.status is OperationStatus.APPLIED
    assert (root / "docs" / "ok.md").read_bytes() == b"x"


def test_create_without_ref_and_parent_writes_the_titled_file_at_the_root(
    root: Path,
) -> None:
    operation = _create("x", title="Architecture Overview")

    result = _apply_one(_provider(root), operation)

    assert result.status is OperationStatus.APPLIED
    assert (root / "Architecture-Overview.md").read_bytes() == b"x"
    assert result.resolved_ref is not None
    assert result.resolved_ref.locator == {"path": "Architecture-Overview.md"}
    assert result.resolved_ref.kind is RefKind.PATH
    assert result.resolved_ref.provider == PROVIDER_NAME
    assert "op.missing_target" not in (result.message or "")
    assert str(root / "Architecture-Overview.md") in (result.message or "")
    assert "created" in (result.message or "")


@pytest.mark.parametrize("title", ["a/b", "", "..", ".hidden", "a:b"])
def test_create_without_ref_rejects_an_invalid_title(root: Path, title: str) -> None:
    result = _apply_one(_provider(root), _create("x", title=title))

    assert result.status is OperationStatus.FAILED
    assert (result.message or "").startswith("[local_files:title.invalid]")
    assert "Hint:" in (result.message or "")
    assert _tree(root) == []


def test_create_without_ref_derives_from_the_parent_ref(root: Path) -> None:
    operation = _create("x", title="Setup", parent_ref=_ref("docs/guide.md"))

    result = _apply_one(_provider(root), operation)

    assert result.status is OperationStatus.APPLIED
    assert (root / "docs/guide/Setup.md").read_bytes() == b"x"
    assert result.resolved_ref is not None
    assert result.resolved_ref.locator == {"path": "docs/guide/Setup.md"}


def test_create_without_ref_namespaces_an_invalid_parent_ref(root: Path) -> None:
    operation = _create("x", title="Setup", parent_ref=_ref("../escape.md"))

    result = _apply_one(_provider(root), operation)

    assert result.status is OperationStatus.FAILED
    assert (result.message or "").startswith("[local_files:path.traversal]")
    assert _tree(root) == []


@pytest.mark.parametrize(
    ("parent", "title", "expected"),
    [
        ("docs/guide.md", "Setup", "docs/guide/Setup.md"),
        ("docs/README.md", "Setup", "docs/Setup.md"),
        ("docs/INDEX.md", "Setup", "docs/Setup.md"),
        ("README.md", "Setup", "Setup.md"),
        ("docs/guide.md", "Architecture Overview Ñandú", "docs/guide/Architecture-Overview-Ñandú.md"),
    ],
)
def test_create_child_derives_the_target_from_the_parent(
    root: Path, parent: str, title: str, expected: str
) -> None:
    result = _apply_one(_provider(root), _child(parent, "body", title))

    assert result.status is OperationStatus.APPLIED
    assert (root / expected).read_bytes() == b"body"
    assert result.resolved_ref is not None
    assert result.resolved_ref.locator == {"path": expected}
    assert str(root / expected) in (result.message or "")
    assert "created" in (result.message or "")


def test_create_child_explicit_ref_wins_over_parent_and_title(root: Path) -> None:
    operation = _child(
        "docs/README.md", "x", "Setup", ref=_ref("docs/examples/deep/foo.md")
    )

    result = _apply_one(_provider(root), operation)

    assert result.status is OperationStatus.APPLIED
    assert (root / "docs/examples/deep/foo.md").read_bytes() == b"x"
    assert _tree(root) == [
        "docs",
        "docs/examples",
        "docs/examples/deep",
        "docs/examples/deep/foo.md",
    ]


@pytest.mark.parametrize("parent", ["docs/nope.md", "../escape.md"])
def test_create_child_explicit_ref_ignores_an_unusable_parent(
    root: Path, parent: str
) -> None:
    result = _apply_one(_provider(root), _child(parent, "x", "a/b", ref=_ref("docs/ok.md")))

    assert result.status is OperationStatus.APPLIED
    assert (root / "docs" / "ok.md").read_bytes() == b"x"


@pytest.mark.parametrize("title", ["a/b", "..", ".hidden", "a:b", ""])
def test_create_child_without_ref_rejects_an_invalid_title(
    root: Path, title: str
) -> None:
    result = _apply_one(_provider(root), _child(title=title))

    assert result.status is OperationStatus.FAILED
    assert (result.message or "").startswith("[local_files:title.invalid]")
    assert _tree(root) == []


@pytest.mark.parametrize(
    "operation_factory",
    [
        lambda: _create("new", ref=_ref("docs/a.md")),
        lambda: _child("docs/guide.md", "new", "a", ref=_ref("docs/a.md")),
    ],
    ids=["create_document", "create_child_document"],
)
def test_create_on_an_existing_file_is_refused_by_default(
    root: Path, operation_factory
) -> None:
    target = _write(root, "docs/a.md", b"old")

    result = _apply_one(_provider(root), operation_factory())

    assert result.status is OperationStatus.FAILED
    message = result.message or ""
    assert message.startswith("[local_files:conflict.exists]")
    assert "overwrite_existing" in message
    assert "update_document" in message
    assert "path='docs/a.md'" in message
    assert result.resolved_ref is not None
    assert result.resolved_ref.locator == {"path": "docs/a.md"}
    assert target.read_bytes() == b"old"


def test_create_without_ref_hits_the_conflict_policy_at_the_root(root: Path) -> None:
    _write(root, "Setup.md", b"old")
    provider = _provider(root)

    conflict = _apply_one(provider, _create("new", title="Setup"))
    identical = _apply_one(provider, _create("old", title="Setup"))

    assert conflict.status is OperationStatus.FAILED
    assert (conflict.message or "").startswith("[local_files:conflict.exists]")
    assert conflict.resolved_ref is not None
    assert conflict.resolved_ref.locator == {"path": "Setup.md"}
    assert identical.status is OperationStatus.SKIPPED
    assert (root / "Setup.md").read_bytes() == b"old"


def test_derived_child_conflict_fails_with_the_derived_ref(root: Path) -> None:
    _write(root, "docs/guide/Setup.md", b"old")

    result = _apply_one(_provider(root), _child("docs/guide.md", "new", "Setup"))

    assert result.status is OperationStatus.FAILED
    assert (result.message or "").startswith("[local_files:conflict.exists]")
    assert result.resolved_ref is not None
    assert result.resolved_ref.locator == {"path": "docs/guide/Setup.md"}


def test_identical_create_is_skipped_without_touching_the_file(root: Path) -> None:
    target = _write(root, "docs/a.md", b"same")
    os.utime(target, (1_000_000, 1_000_000))

    result = _apply_one(_provider(root), _create("same", ref=_ref("docs/a.md")))

    assert result.status is OperationStatus.SKIPPED
    assert (result.message or "").startswith("[local_files:noop.unchanged]")
    assert "Hint:" not in (result.message or "")
    assert result.resolved_ref is not None
    assert result.resolved_ref.locator == {"path": "docs/a.md"}
    assert result.resulting_version == DocumentVersion(
        token=_sha(b"same"), etag=_sha(b"same")
    )
    assert target.stat().st_mtime == 1_000_000


def test_overwrite_existing_replaces_differing_content(root: Path) -> None:
    target = _write(root, "docs/a.md", b"old")

    result = _apply_one(
        _provider(root, overwrite_existing=True), _create("new", ref=_ref("docs/a.md"))
    )

    assert result.status is OperationStatus.APPLIED
    assert (result.message or "").startswith("[local_files:applied.overwritten]")
    assert str(target) in (result.message or "")
    assert result.resulting_version is not None
    assert result.resulting_version.token == _sha(b"new")
    assert target.read_bytes() == b"new"


def test_overwrite_existing_with_identical_content_does_not_rewrite(root: Path) -> None:
    target = _write(root, "docs/a.md", b"same")
    os.utime(target, (1_000_000, 1_000_000))

    result = _apply_one(
        _provider(root, overwrite_existing=True), _create("same", ref=_ref("docs/a.md"))
    )

    assert result.status is OperationStatus.SKIPPED
    assert (result.message or "").startswith("[local_files:noop.unchanged]")
    assert target.stat().st_mtime == 1_000_000


def test_create_over_a_directory_is_not_a_file(root: Path) -> None:
    (root / "docs" / "a.md").mkdir(parents=True)

    result = _apply_one(_provider(root), _create("x", ref=_ref("docs/a.md")))

    assert result.status is OperationStatus.FAILED
    assert (result.message or "").startswith("[local_files:path.not_a_file]")
    assert result.resolved_ref is not None
    assert (root / "docs" / "a.md").is_dir()


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("docs/../../x.md", "path.traversal"),
        ("/etc/x.md", "path.absolute"),
        ("docs//x.md", "path.non_canonical"),
        (".git/x.md", "path.reserved"),
    ],
)
def test_create_revalidates_plugin_built_refs_lexically(
    root: Path, path: str, code: str
) -> None:
    result = _apply_one(_provider(root), _create("x", ref=_ref(path)))

    assert result.status is OperationStatus.FAILED
    assert (result.message or "").startswith(f"[local_files:{code}]")
    assert _tree(root) == []


def test_create_through_an_escaping_symlink_directory_is_refused(
    root: Path, tmp_path: Path, make_symlink
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "docs").mkdir()
    make_symlink(root / "docs" / "link", outside)

    result = _apply_one(_provider(root), _create("x", ref=_ref("docs/link/x.md")))

    assert result.status is OperationStatus.FAILED
    assert (result.message or "").startswith("[local_files:path.symlink_escape]")
    assert _tree(outside) == []


@pytest.mark.parametrize(
    ("blocker", "path"),
    [
        ("docs/examples", "docs/examples/deep/foo.md"),
        ("docs/guide", "docs/guide/Setup.md"),
    ],
)
def test_a_file_blocking_a_parent_directory_is_reported_by_name(
    root: Path, blocker: str, path: str
) -> None:
    blocker_file = _write(root, blocker, b"i am a file")

    result = _apply_one(_provider(root), _create("x", ref=_ref(path)))

    assert result.status is OperationStatus.FAILED
    message = result.message or ""
    assert message.startswith("[local_files:path.parent_not_directory]")
    assert f"path='{blocker}'" in message
    assert result.resolved_ref is not None
    assert result.resolved_ref.locator == {"path": path}
    assert blocker_file.read_bytes() == b"i am a file"


def test_a_derived_child_blocked_by_a_parent_file_fails_by_name(root: Path) -> None:
    _write(root, "docs/guide", b"file")

    result = _apply_one(_provider(root), _child("docs/guide.md", "x", "Setup"))

    assert result.status is OperationStatus.FAILED
    assert (result.message or "").startswith("[local_files:path.parent_not_directory]")
    assert "path='docs/guide'" in (result.message or "")
    assert (root / "docs" / "guide").read_bytes() == b"file"


@pytest.mark.parametrize(
    ("path", "hint"),
    [("docs/foo", "docs/foo.md"), ("docs/v1.2", "docs/v1.2.md"), ("notes.txt", "notes.md")],
)
def test_create_without_markdown_extension_fails_with_a_corrected_name(
    root: Path, path: str, hint: str
) -> None:
    result = _apply_one(_provider(root), _create("x", ref=_ref(path)))

    assert result.status is OperationStatus.FAILED
    assert (result.message or "").startswith("[local_files:path.not_markdown]")
    assert hint in (result.message or "")
    assert _tree(root) == []


def test_a_failed_nested_create_leaves_only_empty_in_root_directories(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*args: Any, **kwargs: Any) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "replace", _boom)

    result = _apply_one(_provider(root), _create("x", ref=_ref("docs/deep/foo.md")))

    assert result.status is OperationStatus.FAILED
    assert (result.message or "").startswith("[local_files:io.error]")
    assert _tree(root) == ["docs", "docs/deep"]


def test_create_writes_through_an_in_root_symlinked_directory(
    root: Path, make_symlink
) -> None:
    (root / "real").mkdir()
    make_symlink(root / "alias", root / "real")

    result = _apply_one(_provider(root), _create("x", ref=_ref("alias/a.md")))

    assert result.status is OperationStatus.APPLIED
    assert (root / "real" / "a.md").read_bytes() == b"x"


# ---------------------------------------------------------------------------
# apply_changes: update_document (LF-10)
# ---------------------------------------------------------------------------


def _update(
    path: str = "a.md",
    content: str = "new",
    expected: DocumentVersion | None = None,
) -> UpdateDocumentOperation:
    return UpdateDocumentOperation(
        ref=_ref(path), new_content=content, expected_version=expected
    )


def test_update_with_a_matching_token_applies_and_reports_the_new_version(
    root: Path,
) -> None:
    target = _write(root, "a.md", b"old")
    operation = _update("a.md", "new", DocumentVersion(token=_sha(b"old")))

    result = _apply_one(_provider(root), operation)

    assert result.status is OperationStatus.APPLIED
    assert target.read_bytes() == b"new"
    assert result.resolved_ref is not None
    assert result.resolved_ref.locator == {"path": "a.md"}
    assert result.resulting_version == DocumentVersion(
        token=_sha(b"new"), etag=_sha(b"new")
    )
    assert str(target) in (result.message or "")
    assert "updated" in (result.message or "")


def test_update_accepts_the_version_through_the_etag_alone(root: Path) -> None:
    target = _write(root, "a.md", b"old")

    result = _apply_one(
        _provider(root), _update(expected=DocumentVersion(etag=_sha(b"old")))
    )

    assert result.status is OperationStatus.APPLIED
    assert target.read_bytes() == b"new"


def test_update_prefers_the_token_over_the_etag(root: Path) -> None:
    target = _write(root, "a.md", b"old")
    expected = DocumentVersion(token=_sha(b"old"), etag=_sha(b"something else"))

    result = _apply_one(_provider(root), _update(expected=expected))

    assert result.status is OperationStatus.APPLIED
    assert target.read_bytes() == b"new"


@pytest.mark.parametrize(
    "expected",
    [
        DocumentVersion(token=_sha(b"stale")),
        DocumentVersion(etag=_sha(b"stale")),
    ],
    ids=["token", "etag"],
)
def test_update_with_a_stale_version_fails_without_writing(
    root: Path, expected: DocumentVersion
) -> None:
    target = _write(root, "a.md", b"changed after planning")

    result = _apply_one(_provider(root), _update(expected=expected))

    assert result.status is OperationStatus.FAILED
    message = result.message or ""
    assert message.startswith("[local_files:conflict.version_mismatch]")
    assert (expected.token or expected.etag or "")[:19] in message
    assert _sha(b"changed after planning")[:19] in message
    assert "path='a.md'" in message
    assert "Hint:" in message
    assert result.resolved_ref is not None
    assert target.read_bytes() == b"changed after planning"


def test_update_without_an_expected_version_skips_the_version_check(
    root: Path,
) -> None:
    target = _write(root, "a.md", b"old")

    result = _apply_one(_provider(root), _update(expected=None))

    assert result.status is OperationStatus.APPLIED
    assert target.read_bytes() == b"new"


def test_update_with_an_empty_expected_version_skips_the_version_check(
    root: Path,
) -> None:
    target = _write(root, "a.md", b"old")

    result = _apply_one(_provider(root), _update(expected=DocumentVersion()))

    assert result.status is OperationStatus.APPLIED
    assert target.read_bytes() == b"new"


def test_update_of_a_missing_file_fails_and_creates_nothing(root: Path) -> None:
    result = _apply_one(_provider(root), _update("docs/a.md"))

    assert result.status is OperationStatus.FAILED
    message = result.message or ""
    assert message.startswith("[local_files:document.not_found]")
    assert "create_document" in message
    assert result.resolved_ref is not None
    assert _tree(root) == []


def test_update_with_identical_content_is_skipped_even_with_a_stale_token(
    root: Path,
) -> None:
    target = _write(root, "a.md", b"same")
    os.utime(target, (1_000_000, 1_000_000))
    stale = DocumentVersion(token=_sha(b"stale"))

    result = _apply_one(_provider(root), _update("a.md", "same", stale))

    assert result.status is OperationStatus.SKIPPED
    assert (result.message or "").startswith("[local_files:noop.unchanged]")
    assert result.resulting_version == DocumentVersion(
        token=_sha(b"same"), etag=_sha(b"same")
    )
    assert target.stat().st_mtime == 1_000_000


def test_update_through_an_in_root_symlink_keeps_the_link(
    root: Path, make_symlink
) -> None:
    real = _write(root, "real.md", b"old")
    make_symlink(root / "a.md", real)

    result = _apply_one(_provider(root), _update("a.md", "new"))

    assert result.status is OperationStatus.APPLIED
    assert real.read_bytes() == b"new"
    assert (root / "a.md").is_symlink()


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("docs/../x.md", "path.traversal"),
        ("docs/x", "path.not_markdown"),
        ("/etc/x.md", "path.absolute"),
    ],
)
def test_update_revalidates_the_ref_before_anything_else(
    root: Path, path: str, code: str
) -> None:
    result = _apply_one(_provider(root), _update(path))

    assert result.status is OperationStatus.FAILED
    assert (result.message or "").startswith(f"[local_files:{code}]")
    assert _tree(root) == []


def test_update_of_a_directory_is_not_a_file(root: Path) -> None:
    (root / "a.md").mkdir()

    result = _apply_one(_provider(root), _update("a.md"))

    assert result.status is OperationStatus.FAILED
    assert (result.message or "").startswith("[local_files:path.not_a_file]")


def test_update_through_an_escaping_symlink_is_refused(
    root: Path, tmp_path: Path, make_symlink
) -> None:
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"secret")
    make_symlink(root / "a.md", outside)

    result = _apply_one(_provider(root), _update("a.md"))

    assert result.status is OperationStatus.FAILED
    assert (result.message or "").startswith("[local_files:path.symlink_escape]")
    assert outside.read_bytes() == b"secret"


def test_update_applies_the_version_check_after_the_identical_check_but_before_writing(
    root: Path,
) -> None:
    _write(root, "a.md", b"old")
    provider = _provider(root)

    stale = _apply_one(provider, _update("a.md", "new", DocumentVersion(token=_sha(b"x"))))
    fresh = _apply_one(
        provider, _update("a.md", "new", DocumentVersion(token=_sha(b"old")))
    )

    assert stale.status is OperationStatus.FAILED
    assert fresh.status is OperationStatus.APPLIED
    assert (root / "a.md").read_bytes() == b"new"


# ---------------------------------------------------------------------------
# apply_changes: batch semantics and result reporting (LF-13)
# ---------------------------------------------------------------------------

_MESSAGE_SHAPE = re.compile(
    r"^\[(?P<ns>[a-z_]+):(?P<code>[a-z_]+(\.[a-z_]+)*)\]"
)


class _UnknownOperation:
    """Stand-in for an operation type the provider does not understand."""

    operation_id = "unknown-op"


def test_a_mixed_batch_reports_every_operation_in_input_order(root: Path) -> None:
    first = _create("one", ref=_ref("docs/one.md"))
    missing_update = _update("docs/missing.md")
    last = _create("two", ref=_ref("docs/two.md"))

    result = _apply(_provider(root), first, missing_update, last)

    assert [r.operation_id for r in result.results] == [
        first.operation_id,
        missing_update.operation_id,
        last.operation_id,
    ]
    assert [r.status for r in result.results] == [
        OperationStatus.APPLIED,
        OperationStatus.FAILED,
        OperationStatus.APPLIED,
    ]
    assert (root / "docs" / "one.md").read_bytes() == b"one"
    assert (root / "docs" / "two.md").read_bytes() == b"two"
    assert result.has_failures() is True


def test_a_batch_without_failures_does_not_report_failures(root: Path) -> None:
    result = _apply(
        _provider(root),
        _create("one", ref=_ref("one.md")),
        _create("one", ref=_ref("one.md")),
    )

    assert [r.status for r in result.results] == [
        OperationStatus.APPLIED,
        OperationStatus.SKIPPED,
    ]
    assert result.has_failures() is False


def test_apply_reports_the_configured_provider_name(root: Path) -> None:
    result = _apply(_provider(root), _create("x", ref=_ref("a.md")))

    assert result.provider_name == PROVIDER_NAME


def test_an_empty_batch_returns_an_empty_result(root: Path) -> None:
    result = _apply(_provider(root))

    assert result.provider_name == PROVIDER_NAME
    assert result.results == []
    assert result.has_failures() is False


def test_unsupported_operation_types_are_skipped_not_failed(root: Path) -> None:
    ok = _create("x", ref=_ref("a.md"))

    result = _apply(_provider(root), _UnknownOperation(), ok)

    unsupported, applied = result.results
    assert unsupported.operation_id == "unknown-op"
    assert unsupported.status is OperationStatus.SKIPPED
    message = unsupported.message or ""
    assert message.startswith("[local_files:op.unsupported]")
    assert "Unsupported operation type: _UnknownOperation" in message
    assert "Hint:" in message
    assert applied.status is OperationStatus.APPLIED


def test_asset_operations_are_not_handled_by_apply_changes(root: Path) -> None:
    operation = PutAssetOperation.model_construct(
        operation_id="asset-op", asset_key="logo", name="a.png"
    )

    result = _apply_one(_provider(root), operation)

    assert result.status is OperationStatus.SKIPPED
    assert "Unsupported operation type: PutAssetOperation" in (result.message or "")


def test_a_permission_error_from_the_writer_fails_only_that_operation(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wikiops.providers import _fs

    real_write = _fs.atomic_write_bytes

    def _flaky(target: Path, data: bytes, *, root_real: Path) -> None:
        if target.name == "boom.md":
            raise PermissionError(13, "Permission denied")
        real_write(target, data, root_real=root_real)

    monkeypatch.setattr(_fs, "atomic_write_bytes", _flaky)
    before = _create("a", ref=_ref("before.md"))
    boom = _create("b", ref=_ref("docs/boom.md"))
    after = _create("c", ref=_ref("after.md"))

    result = _apply(_provider(root), before, boom, after)

    assert [r.status for r in result.results] == [
        OperationStatus.APPLIED,
        OperationStatus.FAILED,
        OperationStatus.APPLIED,
    ]
    failed = result.results[1]
    message = failed.message or ""
    assert message.startswith("[local_files:io.error]")
    assert "PermissionError" in message
    assert "path='docs/boom.md'" in message
    assert f"root='{root}'" in message
    assert "Hint:" in message
    assert failed.resolved_ref is not None
    assert failed.resolved_ref.locator == {"path": "docs/boom.md"}
    assert (root / "after.md").read_bytes() == b"c"


def test_an_unexpected_exception_is_captured_as_a_failed_io_error(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wikiops.providers import _fs

    def _explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("surprise")

    monkeypatch.setattr(_fs, "atomic_write_bytes", _explode)

    result = _apply(
        _provider(root),
        _create("x", ref=_ref("a.md")),
        _create("y", ref=_ref("b.md")),
    )

    assert [r.status for r in result.results] == [
        OperationStatus.FAILED,
        OperationStatus.FAILED,
    ]
    message = result.results[0].message or ""
    assert message.startswith("[local_files:io.error]")
    assert "RuntimeError: surprise" in message
    assert "Hint:" in message
    assert result.results[0].resolved_ref is not None


def test_failed_messages_match_the_machine_readable_shape(root: Path) -> None:
    _write(root, "docs/a.md", b"old")

    result = _apply_one(_provider(root), _create("new", ref=_ref("docs/a.md")))

    message = result.message or ""
    match = _MESSAGE_SHAPE.match(message)
    assert match is not None
    assert match.group("ns") == "local_files"
    assert match.group("code") == "conflict.exists"
    assert "path='docs/a.md'" in message
    assert f"root='{root}'" in message
    assert "Hint:" in message


@pytest.mark.parametrize(
    ("operation_factory", "code"),
    [
        (lambda: _create("x", ref=_ref("../x.md")), "path.traversal"),
        (lambda: _update("../x.md"), "path.traversal"),
        (lambda: _create("x", title="a/b"), "title.invalid"),
        (lambda: _update("docs/missing.md"), "document.not_found"),
    ],
)
def test_helper_and_policy_errors_surface_with_the_provider_namespace(
    root: Path, operation_factory, code: str
) -> None:
    result = _apply_one(_provider(root), operation_factory())

    match = _MESSAGE_SHAPE.match(result.message or "")
    assert match is not None
    assert (match.group("ns"), match.group("code")) == ("local_files", code)


def test_informational_messages_use_the_same_prefix(root: Path) -> None:
    _write(root, "a.md", b"same")

    skipped = _apply_one(_provider(root), _create("same", ref=_ref("a.md")))
    overwritten = _apply_one(
        _provider(root, overwrite_existing=True), _create("other", ref=_ref("a.md"))
    )

    for result, code in ((skipped, "noop.unchanged"), (overwritten, "applied.overwritten")):
        match = _MESSAGE_SHAPE.match(result.message or "")
        assert match is not None
        assert (match.group("ns"), match.group("code")) == ("local_files", code)


def test_apply_results_do_not_leave_temp_files_behind(root: Path) -> None:
    _apply(
        _provider(root),
        _create("x", ref=_ref("docs/a.md")),
        _create("y", ref=_ref("docs/a.md")),
        _update("docs/a.md", "z"),
    )

    assert _tree(root) == ["docs", "docs/a.md"]


def test_an_unexpected_error_before_the_target_is_known_has_no_path_or_ref(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _explode(self: LocalFilesProvider, operation: Any) -> DocumentRef:
        raise RuntimeError("surprise")

    monkeypatch.setattr(LocalFilesProvider, "_target_for", _explode)

    result = _apply_one(_provider(root), _create("x", ref=_ref("a.md")))

    assert result.status is OperationStatus.FAILED
    message = result.message or ""
    assert message.startswith("[local_files:io.error]")
    assert "RuntimeError: surprise" in message
    assert "path=" not in message
    assert result.resolved_ref is None


@pytest.mark.parametrize(
    ("ref", "code"),
    [
        (_ref("a.md", kind=RefKind.ALIAS), "ref.unsupported_kind"),
        (_ref(None), "ref.missing_path"),
    ],
)
def test_create_rejects_refs_the_provider_cannot_interpret(
    root: Path, ref: DocumentRef, code: str
) -> None:
    result = _apply_one(_provider(root), _create("x", ref=ref))

    assert result.status is OperationStatus.FAILED
    assert (result.message or "").startswith(f"[local_files:{code}]")
    assert result.resolved_ref == ref
    assert _tree(root) == []
