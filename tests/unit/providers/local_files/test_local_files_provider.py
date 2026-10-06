"""Unit tests for the ``local_files`` provider read side (settings, refs, reads)."""

from __future__ import annotations

import hashlib
import os
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
    AssetRef,
    AssetRefKind,
    ChangeSet,
    DocumentRef,
    ProviderCapability,
    PutAssetOperation,
    RefKind,
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


def test_write_side_is_not_implemented_yet(root: Path) -> None:
    provider = _provider(root)
    asset_ref = AssetRef(
        provider=PROVIDER_NAME, kind=AssetRefKind.PATH, locator={"path": "a.png"}
    )

    with pytest.raises(NotImplementedError):
        provider.apply_changes(ChangeSet(plugin_id="demo.plugin"))
    with pytest.raises(NotImplementedError):
        provider.build_asset_reference(asset_ref)
    with pytest.raises(NotImplementedError):
        provider.put_asset(
            PutAssetOperation.model_construct(name="a.png"),
            b"x",
        )
