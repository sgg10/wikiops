from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from wikiops.core.exceptions import ConfigurationError
from wikiops.providers import _fs
from wikiops.providers._fs import FsError

# ---------------------------------------------------------------------------
# FsError (FS-8)
# ---------------------------------------------------------------------------


def test_fs_error_renders_all_segments_without_namespace() -> None:
    error = FsError(
        "path.traversal",
        "Path escapes the root",
        path="../x.md",
        root=Path("/data/root"),
        hint="remove '..' segments",
    )

    assert str(error) == (
        "[path.traversal] Path escapes the root. "
        "path='../x.md' root='/data/root'. "
        "Hint: remove '..' segments."
    )
    assert error.code == "path.traversal"
    assert error.summary == "Path escapes the root"
    assert error.path == "../x.md"
    assert error.root == Path("/data/root")
    assert error.hint == "remove '..' segments"
    assert error.namespace is None


def test_fs_error_renders_namespace_prefix() -> None:
    error = FsError("io.error", "Write failed", namespace="local_files")

    assert str(error) == "[local_files:io.error] Write failed."


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({}, "[document.not_found] Missing."),
        ({"path": "a.md"}, "[document.not_found] Missing. path='a.md'."),
        (
            {"root": Path("/r")},
            "[document.not_found] Missing. root='/r'.",
        ),
        (
            {"hint": "create it"},
            "[document.not_found] Missing. Hint: create it.",
        ),
        (
            {"path": "a.md", "hint": "create it"},
            "[document.not_found] Missing. path='a.md'. Hint: create it.",
        ),
    ],
)
def test_fs_error_omits_absent_segments(kwargs: dict[str, object], expected: str) -> None:
    assert str(FsError("document.not_found", "Missing", **kwargs)) == expected  # type: ignore[arg-type]


def test_fs_error_does_not_double_trailing_periods() -> None:
    error = FsError("io.error", "Write failed.", hint="retry later.")

    assert str(error) == "[io.error] Write failed. Hint: retry later."


def test_fs_error_escapes_control_characters_in_path() -> None:
    error = FsError("path.invalid_chars", "Bad path", path="a\x00b.md")

    assert "\x00" not in str(error)
    assert "path='a\\x00b.md'" in str(error)


def test_with_namespace_returns_new_instance_preserving_fields() -> None:
    original = FsError(
        "path.symlink_escape",
        "Escapes root",
        path="docs/a.md",
        root=Path("/r"),
        hint="remove the link",
    )

    namespaced = original.with_namespace("local_files")

    assert namespaced is not original
    assert isinstance(namespaced, FsError)
    assert (namespaced.code, namespaced.summary, namespaced.path) == (
        original.code,
        original.summary,
        original.path,
    )
    assert (namespaced.root, namespaced.hint) == (original.root, original.hint)
    assert namespaced.namespace == "local_files"
    assert str(namespaced).startswith("[local_files:path.symlink_escape] Escapes root.")


def test_with_namespace_leaves_original_untouched() -> None:
    original = FsError("path.traversal", "Escapes root")

    original.with_namespace("github_wiki")

    assert original.namespace is None
    assert str(original) == "[path.traversal] Escapes root."


def test_with_namespace_can_replace_an_existing_namespace() -> None:
    first = FsError("io.error", "Failed", namespace="a")

    assert str(first.with_namespace("b")) == "[b:io.error] Failed."


def test_fs_error_is_a_configuration_error() -> None:
    error = FsError("io.error", "Failed")

    assert isinstance(error, ConfigurationError)
    with pytest.raises(ConfigurationError, match=r"\[io\.error\] Failed\."):
        raise error


# ---------------------------------------------------------------------------
# Provider independence (FS-9)
# ---------------------------------------------------------------------------


def test_fs_module_imports_standalone_without_provider_or_sdk_modules() -> None:
    code = (
        "import sys\n"
        "import wikiops.providers._fs\n"
        "loaded = sorted(m for m in sys.modules if m.split('.')[0] in {'wikiops', 'wikiops_sdk'})\n"
        "print('\\n'.join(loaded))\n"
    )
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}

    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )

    loaded = set(completed.stdout.split())
    assert "wikiops.providers._fs" in loaded
    assert not [m for m in loaded if m.startswith("wikiops.providers.local_files")]
    assert not [m for m in loaded if m.startswith("wikiops_sdk")]
    assert loaded <= {
        "wikiops",
        "wikiops.core",
        "wikiops.core.exceptions",
        "wikiops.providers",
        "wikiops.providers._fs",
    }


def test_fs_module_exposes_no_provider_identity() -> None:
    assert not hasattr(_fs, "ERROR_NAMESPACE")


# ---------------------------------------------------------------------------
# validate_relative_path (FS-2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["a.md", "docs/guide/a.md", "docs/v1.2/notes.md", "Ñandú/Árbol de ideas.md", ".github/a.md"],
)
def test_validate_relative_path_accepts_canonical_paths_unchanged(value: str) -> None:
    result = _fs.validate_relative_path(value)

    assert str(result) == value
    assert result.as_posix() == value


def _rejection(value: str) -> FsError:
    with pytest.raises(FsError) as excinfo:
        _fs.validate_relative_path(value)
    return excinfo.value


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("", "path.empty"),
        ("   ", "path.empty"),
        (".", "path.empty"),
        ("./", "path.empty"),
        ("/etc/passwd", "path.absolute"),
        ("//server/share/a.md", "path.absolute"),
        ("C:/x", "path.absolute"),
        ("c:\\x", "path.absolute"),
        ("../x.md", "path.traversal"),
        ("docs/../../secret.md", "path.traversal"),
        ("docs/..", "path.traversal"),
        ("docs\\a.md", "path.invalid_chars"),
        ("a\x00b.md", "path.invalid_chars"),
        ("a\nb.md", "path.invalid_chars"),
        ("a\x7fb.md", "path.invalid_chars"),
        ("docs/a:b.md", "path.invalid_chars"),
        ("docs/a*.md", "path.invalid_chars"),
        ("docs/a?.md", "path.invalid_chars"),
        ('docs/a".md', "path.invalid_chars"),
        ("docs/<a>.md", "path.invalid_chars"),
        ("docs/a|b.md", "path.invalid_chars"),
        ("./docs//guide/./a.md", "path.non_canonical"),
        ("./a.md", "path.non_canonical"),
        ("docs//a.md", "path.non_canonical"),
        ("docs/./a.md", "path.non_canonical"),
        ("docs/a/", "path.non_canonical"),
        (".git/config.md", "path.reserved"),
        ("docs/.GIT/x.md", "path.reserved"),
        ("docs/.Git", "path.reserved"),
    ],
)
def test_validate_relative_path_rejects_with_distinct_code_and_hint(
    value: str, code: str
) -> None:
    error = _rejection(value)

    assert error.code == code
    assert error.hint
    assert str(error).startswith(f"[{code}] ")
    assert "Hint: " in str(error)
    assert error.namespace is None


def test_non_canonical_hint_contains_the_canonical_form() -> None:
    error = _rejection("./docs//guide/./a.md")

    assert "docs/guide/a.md" in str(error.hint)
    assert error.path == "./docs//guide/./a.md"


def test_non_canonical_trailing_slash_hint_drops_the_slash() -> None:
    assert "'docs/a'" in str(_rejection("docs/a/").hint)


def test_absolute_error_states_paths_are_relative_and_gives_an_example() -> None:
    message = str(_rejection("/etc/passwd"))

    assert "relative to the root" in message
    assert "docs/guide/a.md" in message


@pytest.mark.parametrize(
    ("value", "needle"),
    [
        ("docs\\a.md", "backslash"),
        ("a\x00b.md", "control character"),
        ("docs/a?.md", "'?'"),
    ],
)
def test_invalid_chars_names_the_offending_character_class(value: str, needle: str) -> None:
    assert needle in _rejection(value).summary


def test_validate_relative_path_does_not_touch_the_filesystem(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()

    assert str(_fs.validate_relative_path("docs/missing/a.md")) == "docs/missing/a.md"


# ---------------------------------------------------------------------------
# resolve_root (FS-1)
# ---------------------------------------------------------------------------


@pytest.fixture
def symlinks() -> None:
    """Skip the requesting test when the platform cannot create symlinks."""
    if not hasattr(os, "symlink"):
        pytest.skip("os.symlink is not available on this platform")


@pytest.fixture
def make_symlink(tmp_path: Path, symlinks: None):
    """Return a factory creating symlinks, skipping when the OS refuses them."""

    def _make(link: Path, target: Path) -> Path:
        try:
            link.symlink_to(target, target_is_directory=target.is_dir())
        except (OSError, NotImplementedError):
            pytest.skip("symlinks cannot be created here")
        return link

    return _make


def test_resolve_root_resolves_relative_value_against_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "docs").mkdir()
    monkeypatch.chdir(tmp_path)

    assert _fs.resolve_root("docs") == (tmp_path / "docs").resolve()


def test_resolve_root_accepts_explicit_cwd_override(tmp_path: Path) -> None:
    (tmp_path / "wiki").mkdir()

    assert _fs.resolve_root("wiki", cwd=tmp_path) == (tmp_path / "wiki").resolve()


def test_resolve_root_returns_absolute_value_unchanged(tmp_path: Path) -> None:
    assert _fs.resolve_root(str(tmp_path)) == tmp_path.resolve()


def test_resolve_root_expands_tilde_from_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "wiki").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    assert _fs.resolve_root("~/wiki") == (tmp_path / "wiki").resolve()


@pytest.mark.parametrize("raw", ["", "   ", "\t\n"])
def test_resolve_root_rejects_empty_value_and_never_uses_cwd(
    raw: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(FsError) as excinfo:
        _fs.resolve_root(raw)

    assert excinfo.value.code == "settings.root_missing"
    assert "Hint: " in str(excinfo.value)
    assert excinfo.value.root is None
    assert str(tmp_path) not in str(excinfo.value)


def test_resolve_root_missing_message_has_configured_resolved_cwd_and_hint(
    tmp_path: Path,
) -> None:
    with pytest.raises(FsError) as excinfo:
        _fs.resolve_root("nope/wiki", cwd=tmp_path)

    error = excinfo.value
    message = str(error)
    assert error.code == "settings.root_missing"
    assert message.startswith("[settings.root_missing] ")
    assert "configured='nope/wiki'" in message
    assert f"cwd='{tmp_path}'" in message
    assert f"root='{(tmp_path / 'nope' / 'wiki').resolve()}'" in message
    assert "Hint: " in message
    assert not (tmp_path / "nope").exists()


def test_resolve_root_rejects_a_regular_file(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_text("x")

    with pytest.raises(FsError) as excinfo:
        _fs.resolve_root("file.txt", cwd=tmp_path)

    message = str(excinfo.value)
    assert excinfo.value.code == "settings.root_not_directory"
    assert "configured='file.txt'" in message
    assert f"cwd='{tmp_path}'" in message
    assert f"root='{(tmp_path / 'file.txt').resolve()}'" in message
    assert "Hint: " in message


def test_resolve_root_returns_real_target_of_a_symlinked_root(
    tmp_path: Path, make_symlink
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = make_symlink(tmp_path / "link", real)

    result = _fs.resolve_root(str(link))

    assert result == real.resolve()
    assert result != link


def test_resolve_root_maps_unresolvable_roots_to_typed_error(
    tmp_path: Path, make_symlink
) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    make_symlink(a, b)
    make_symlink(b, a)

    with pytest.raises(FsError) as excinfo:
        _fs.resolve_root(str(a))

    assert excinfo.value.code == "path.unresolvable"
    assert "configured=" in str(excinfo.value)
    assert "Hint: " in str(excinfo.value)


# ---------------------------------------------------------------------------
# resolve_within_root / ensure_within_root (FS-3)
# ---------------------------------------------------------------------------


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A real (symlink-free) root directory inside ``tmp_path``."""
    directory = tmp_path / "root"
    directory.mkdir()
    return directory.resolve()


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """A real directory that lives outside ``root``."""
    directory = tmp_path / "outside"
    directory.mkdir()
    return directory.resolve()


def test_resolve_within_root_returns_real_path_for_missing_nested_target(root: Path) -> None:
    assert _fs.resolve_within_root(root, "docs/a.md") == root / "docs" / "a.md"


def test_resolve_within_root_returns_existing_file(root: Path) -> None:
    (root / "docs").mkdir()
    (root / "docs" / "a.md").write_text("x")

    assert _fs.resolve_within_root(root, "docs/a.md") == root / "docs" / "a.md"


def test_resolve_within_root_validates_lexically_first(root: Path) -> None:
    with pytest.raises(FsError) as excinfo:
        _fs.resolve_within_root(root, "../x.md")

    assert excinfo.value.code == "path.traversal"


def test_resolve_within_root_allows_in_root_symlinked_directory(
    root: Path, make_symlink
) -> None:
    (root / "real").mkdir()
    (root / "real" / "a.md").write_text("x")
    make_symlink(root / "link", root / "real")

    assert _fs.resolve_within_root(root, "link/a.md") == root / "real" / "a.md"


def test_resolve_within_root_allows_in_root_symlinked_file(root: Path, make_symlink) -> None:
    (root / "real.md").write_text("x")
    make_symlink(root / "alias.md", root / "real.md")

    assert _fs.resolve_within_root(root, "alias.md") == root / "real.md"


def test_resolve_within_root_allows_dangling_symlink_to_in_root_target(
    root: Path, symlinks: None
) -> None:
    link = root / "pending.md"
    try:
        link.symlink_to(root / "future.md")
    except OSError:  # pragma: no cover - platform dependent
        pytest.skip("symlinks cannot be created here")

    assert _fs.resolve_within_root(root, "pending.md") == root / "future.md"


def test_final_component_symlink_outside_root_is_rejected(
    root: Path, outside: Path, make_symlink
) -> None:
    (outside / "x.md").write_text("secret")
    make_symlink(root / "out.md", outside / "x.md")

    with pytest.raises(FsError) as excinfo:
        _fs.resolve_within_root(root, "out.md")

    error = excinfo.value
    assert error.code == "path.symlink_escape"
    assert str(outside / "x.md") in str(error)
    assert error.path == "out.md"
    assert error.root == root
    assert "Hint: " in str(error)


def test_symlinked_parent_directory_outside_root_is_rejected(
    root: Path, outside: Path, make_symlink
) -> None:
    make_symlink(root / "dir", outside)

    with pytest.raises(FsError) as excinfo:
        _fs.resolve_within_root(root, "dir/new.md")

    assert excinfo.value.code == "path.symlink_escape"
    assert str(outside) in str(excinfo.value)
    assert not (outside / "new.md").exists()


def test_dangling_symlink_pointing_outside_root_is_rejected(
    root: Path, outside: Path, symlinks: None
) -> None:
    link = root / "dangling.md"
    try:
        link.symlink_to(outside / "missing.md")
    except OSError:  # pragma: no cover - platform dependent
        pytest.skip("symlinks cannot be created here")

    with pytest.raises(FsError) as excinfo:
        _fs.resolve_within_root(root, "dangling.md")

    assert excinfo.value.code == "path.symlink_escape"
    assert str(outside / "missing.md") in str(excinfo.value)


def test_symlink_loop_is_reported_as_unresolvable_without_raw_errors(
    root: Path, make_symlink
) -> None:
    make_symlink(root / "a", root / "b")
    make_symlink(root / "b", root / "a")

    with pytest.raises(FsError) as excinfo:
        _fs.resolve_within_root(root, "a/x.md")

    error = excinfo.value
    assert error.code == "path.unresolvable"
    assert error.path == "a/x.md"
    assert "Hint: " in str(error)
    assert isinstance(error.__cause__, (OSError, RuntimeError))


def test_path_resolving_to_the_root_itself_is_rejected(root: Path, make_symlink) -> None:
    make_symlink(root / "self", root)

    with pytest.raises(FsError) as excinfo:
        _fs.resolve_within_root(root, "self")

    assert excinfo.value.code == "path.unresolvable"
    assert "root directory itself" in excinfo.value.summary


def test_ensure_within_root_returns_the_real_candidate(root: Path) -> None:
    (root / "docs").mkdir()

    assert _fs.ensure_within_root(root, root / "docs" / "a.md") == root / "docs" / "a.md"


def test_ensure_within_root_rejects_candidates_outside_the_root(
    root: Path, outside: Path
) -> None:
    with pytest.raises(FsError) as excinfo:
        _fs.ensure_within_root(root, outside / "x.md")

    assert excinfo.value.code == "path.symlink_escape"
    assert excinfo.value.path == str(outside / "x.md")


def test_ensure_within_root_rejects_parent_swapped_for_outside_symlink(
    root: Path, outside: Path
) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("os.symlink is not available on this platform")
    (root / "docs").mkdir()
    resolved = _fs.resolve_within_root(root, "docs/a.md")
    (root / "docs").rmdir()
    try:
        (root / "docs").symlink_to(outside, target_is_directory=True)
    except OSError:  # pragma: no cover - platform dependent
        pytest.skip("symlinks cannot be created here")

    with pytest.raises(FsError) as excinfo:
        _fs.ensure_within_root(root, resolved)

    assert excinfo.value.code == "path.symlink_escape"
    assert not (outside / "a.md").exists()


@pytest.fixture
def lenient_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mimic Python >= 3.13, where non-strict ``Path.resolve`` ignores symlink loops."""
    monkeypatch.setattr(Path, "resolve", lambda self, strict=False: self)


def test_symlink_loop_is_detected_when_resolve_ignores_loops(
    root: Path, make_symlink, lenient_resolve: None
) -> None:
    make_symlink(root / "a", root / "b")
    make_symlink(root / "b", root / "a")

    with pytest.raises(FsError) as excinfo:
        _fs.ensure_within_root(root, root / "a" / "x.md")

    assert excinfo.value.code == "path.unresolvable"
    assert isinstance(excinfo.value.__cause__, OSError)


def test_dangling_in_root_symlink_is_not_mistaken_for_a_loop(
    root: Path, symlinks: None, lenient_resolve: None
) -> None:
    link = root / "pending.md"
    try:
        link.symlink_to(root / "future.md")
    except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
        pytest.skip("symlinks cannot be created here")

    assert _fs.ensure_within_root(root, link) == link
