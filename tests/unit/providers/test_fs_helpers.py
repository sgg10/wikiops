from __future__ import annotations

import errno
import hashlib
import os
import pickle
import re
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath

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


@pytest.mark.parametrize(
    "error",
    [
        FsError(
            "path.symlink_escape",
            "Escapes root",
            path="docs/a.md",
            root=Path("/r"),
            hint="remove the link",
            namespace="local_files",
        ),
        FsError("path.traversal", "Escapes root"),
    ],
    ids=["namespaced", "un-namespaced"],
)
def test_fs_error_survives_a_pickle_round_trip(error: FsError) -> None:
    restored = pickle.loads(pickle.dumps(error))

    assert isinstance(restored, FsError)
    assert restored is not error
    assert (restored.code, restored.summary, restored.path) == (
        error.code,
        error.summary,
        error.path,
    )
    assert (restored.root, restored.hint, restored.namespace) == (
        error.root,
        error.hint,
        error.namespace,
    )
    assert str(restored) == str(error)


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


# ---------------------------------------------------------------------------
# Ancestor probe terminates when no ancestor exists (e.g. Windows drive/UNC root)
# ---------------------------------------------------------------------------

_PROBE_LIMIT = 500


@pytest.fixture
def nothing_exists(monkeypatch: pytest.MonkeyPatch):
    """Make ``os.path.lexists`` report False for every path, bounded.

    Emulates an anchor (drive or UNC share root) that does not exist: the
    ancestor probe reaches a path whose parent is itself. A regression fails
    with ``AssertionError`` after ``_PROBE_LIMIT`` calls instead of hanging.
    """
    calls = {"count": 0}

    def lexists(_path: object) -> bool:
        calls["count"] += 1
        assert calls["count"] <= _PROBE_LIMIT, "ancestor probe did not terminate"
        return False

    with monkeypatch.context() as scoped:
        scoped.setattr(os.path, "lexists", lexists)
        yield calls


def test_ensure_within_root_reports_unresolvable_when_no_ancestor_exists(
    root: Path, nothing_exists: dict[str, int]
) -> None:
    with pytest.raises(FsError) as excinfo:
        _fs.ensure_within_root(root, root / "docs" / "a.md")

    error = excinfo.value
    assert error.code == "path.unresolvable"
    assert error.path == "docs/a.md"
    assert "Hint: " in str(error)
    assert isinstance(error.__cause__, OSError)
    assert 0 < nothing_exists["count"] <= _PROBE_LIMIT


def test_resolve_root_reports_missing_root_when_no_ancestor_exists(
    tmp_path: Path, nothing_exists: dict[str, int]
) -> None:
    with pytest.raises(FsError) as excinfo:
        _fs.resolve_root("wiki", cwd=tmp_path)

    error = excinfo.value
    assert error.code == "settings.root_missing"
    assert "configured='wiki'" in str(error)
    assert f"cwd='{tmp_path}'" in str(error)
    assert "Hint: " in str(error)
    assert 0 < nothing_exists["count"] <= _PROBE_LIMIT


# ---------------------------------------------------------------------------
# make_dirs_within_root (FS-5)
# ---------------------------------------------------------------------------


def _dirs(root: Path, relative: str) -> Path:
    return _fs.make_dirs_within_root(root, PurePosixPath(relative))


def test_make_dirs_within_root_creates_nested_directories_stepwise(root: Path) -> None:
    result = _dirs(root, "a/b/c")

    assert result == root / "a" / "b" / "c"
    assert (root / "a" / "b" / "c").is_dir()


def test_make_dirs_within_root_is_idempotent_for_existing_directories(root: Path) -> None:
    (root / "docs" / "guide").mkdir(parents=True)
    (root / "docs" / "keep.md").write_text("keep")

    assert _dirs(root, "docs/guide") == root / "docs" / "guide"
    assert _dirs(root, "docs/guide/deeper") == root / "docs" / "guide" / "deeper"
    assert (root / "docs" / "keep.md").read_text() == "keep"


def test_make_dirs_within_root_returns_root_for_an_empty_relative_dir(root: Path) -> None:
    assert _fs.make_dirs_within_root(root, PurePosixPath()) == root


def test_make_dirs_within_root_rejects_non_canonical_input_before_any_mkdir(
    root: Path, outside: Path
) -> None:
    with pytest.raises(FsError) as excinfo:
        _dirs(root, "../outside/leak")

    assert excinfo.value.code == "path.traversal"
    assert not (outside / "leak").exists()


def test_make_dirs_within_root_refuses_a_regular_file_component(root: Path) -> None:
    (root / "docs").mkdir()
    (root / "docs" / "examples").write_text("i am a file")

    with pytest.raises(FsError) as excinfo:
        _dirs(root, "docs/examples/deep")

    error = excinfo.value
    assert error.code == "path.parent_not_directory"
    assert error.path == "docs/examples"
    assert error.root == root
    assert "Hint: " in str(error)
    assert (root / "docs" / "examples").read_text() == "i am a file"
    assert not (root / "docs" / "examples" / "deep").exists()


def test_make_dirs_within_root_refuses_a_file_as_the_last_component(root: Path) -> None:
    (root / "notes").write_text("x")

    with pytest.raises(FsError) as excinfo:
        _dirs(root, "notes")

    assert excinfo.value.code == "path.parent_not_directory"
    assert excinfo.value.path == "notes"


def test_make_dirs_within_root_refuses_existing_symlink_to_outside_directory(
    root: Path, outside: Path, make_symlink
) -> None:
    (root / "docs").mkdir()
    make_symlink(root / "docs" / "link", outside)

    with pytest.raises(FsError) as excinfo:
        _dirs(root, "docs/link/sub")

    assert excinfo.value.code == "path.symlink_escape"
    assert list(outside.iterdir()) == []


def test_make_dirs_within_root_follows_in_root_symlinked_directory(
    root: Path, make_symlink
) -> None:
    (root / "real").mkdir()
    make_symlink(root / "alias", root / "real")

    result = _dirs(root, "alias/sub")

    assert result == root / "real" / "sub"
    assert (root / "real" / "sub").is_dir()


def test_make_dirs_within_root_falls_through_when_directory_appears_concurrently(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_mkdir = os.mkdir

    def racing_mkdir(path, mode=0o777, **kwargs):  # noqa: ANN001, ANN003, ANN202
        real_mkdir(path, mode, **kwargs)
        raise FileExistsError(errno.EEXIST, "File exists", str(path))

    monkeypatch.setattr(os, "mkdir", racing_mkdir)

    assert _dirs(root, "a/b") == root / "a" / "b"
    assert (root / "a" / "b").is_dir()


def test_make_dirs_within_root_refuses_a_component_swapped_for_an_outside_symlink(
    root: Path, outside: Path, make_symlink, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_mkdir = os.mkdir

    def swapping_mkdir(path, mode=0o777, **kwargs):  # noqa: ANN001, ANN003, ANN202
        real_mkdir(path, mode, **kwargs)
        if Path(path).name == "swapped":
            Path(path).rmdir()
            make_symlink(Path(path), outside)

    monkeypatch.setattr(os, "mkdir", swapping_mkdir)

    with pytest.raises(FsError) as excinfo:
        _dirs(root, "swapped/inner")

    assert excinfo.value.code == "path.symlink_escape"
    assert list(outside.iterdir()) == []


def test_make_dirs_within_root_maps_os_errors_to_io_error(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(path, mode=0o777, **kwargs):  # noqa: ANN001, ANN003, ANN202
        raise PermissionError(errno.EACCES, "Permission denied", str(path))

    monkeypatch.setattr(os, "mkdir", denied)

    with pytest.raises(FsError) as excinfo:
        _dirs(root, "docs")

    error = excinfo.value
    assert error.code == "io.error"
    assert str(error).startswith("[io.error] ")
    assert "PermissionError" in error.summary
    assert error.path == "docs"
    assert "Hint: " in str(error)
    assert isinstance(error.__cause__, PermissionError)


# ---------------------------------------------------------------------------
# atomic_write_bytes (FS-5)
# ---------------------------------------------------------------------------

posix_modes = pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")


def _write(root: Path, relative: str, data: bytes) -> None:
    _fs.atomic_write_bytes(root.joinpath(*relative.split("/")), data, root_real=root)


def _names(directory: Path) -> list[str]:
    return sorted(entry.name for entry in directory.iterdir())


def test_atomic_write_bytes_creates_a_new_file_with_parents(root: Path) -> None:
    _write(root, "a/b/c.md", b"x")

    assert (root / "a" / "b" / "c.md").read_bytes() == b"x"
    assert _names(root / "a" / "b") == ["c.md"]


def test_atomic_write_bytes_writes_a_file_directly_under_the_root(root: Path) -> None:
    _write(root, "top.md", b"top")

    assert _names(root) == ["top.md"]
    assert (root / "top.md").read_bytes() == b"top"


def test_atomic_write_bytes_replaces_existing_content_and_leaves_no_temp_files(
    root: Path,
) -> None:
    (root / "a.md").write_bytes(b"old")

    _write(root, "a.md", b"new")

    assert (root / "a.md").read_bytes() == b"new"
    assert _names(root) == ["a.md"]


@pytest.mark.parametrize(
    "data",
    [b"a\r\nb\r\n", b"\xef\xbb\xbfhi\r\n", b"mixed\r\nlf\nand\rcr", b""],
    ids=["crlf", "bom", "mixed-newlines", "empty"],
)
def test_atomic_write_bytes_round_trips_bytes_exactly(root: Path, data: bytes) -> None:
    _write(root, "doc.md", data)

    assert (root / "doc.md").read_bytes() == data


@posix_modes
def test_atomic_write_bytes_preserves_the_mode_of_an_existing_file(root: Path) -> None:
    target = root / "a.md"
    target.write_bytes(b"old")
    target.chmod(0o640)

    _write(root, "a.md", b"new")

    assert stat.S_IMODE(target.stat().st_mode) == 0o640


@posix_modes
@pytest.mark.parametrize(("umask", "expected"), [(0o022, 0o644), (0o077, 0o600)])
def test_atomic_write_bytes_gives_new_files_umask_derived_permissions(
    root: Path, umask: int, expected: int
) -> None:
    previous = os.umask(umask)
    try:
        _write(root, "new.md", b"x")
    finally:
        os.umask(previous)

    assert stat.S_IMODE((root / "new.md").stat().st_mode) == expected


def test_atomic_write_bytes_writes_through_an_in_root_symlinked_file(
    root: Path, make_symlink
) -> None:
    (root / "real.md").write_bytes(b"old")
    link = make_symlink(root / "link.md", root / "real.md")

    _fs.atomic_write_bytes(_fs.resolve_within_root(root, "link.md"), b"new", root_real=root)

    assert (root / "real.md").read_bytes() == b"new"
    assert link.is_symlink()
    assert _names(root) == ["link.md", "real.md"]


def test_atomic_write_bytes_refuses_a_directory_target(root: Path) -> None:
    (root / "docs").mkdir()
    (root / "docs" / "keep.md").write_bytes(b"keep")

    with pytest.raises(FsError) as excinfo:
        _write(root, "docs", b"x")

    error = excinfo.value
    assert error.code == "path.not_a_file"
    assert error.path == "docs"
    assert "Hint: " in str(error)
    assert _names(root / "docs") == ["keep.md"]


def test_atomic_write_bytes_refuses_a_file_blocking_the_parent_directory(root: Path) -> None:
    (root / "docs").mkdir()
    (root / "docs" / "examples").write_bytes(b"i am a file")

    with pytest.raises(FsError) as excinfo:
        _write(root, "docs/examples/deep/foo.md", b"x")

    assert excinfo.value.code == "path.parent_not_directory"
    assert excinfo.value.path == "docs/examples"
    assert (root / "docs" / "examples").read_bytes() == b"i am a file"
    assert _names(root / "docs") == ["examples"]


def test_atomic_write_bytes_refuses_a_target_outside_the_root(root: Path, outside: Path) -> None:
    with pytest.raises(FsError) as excinfo:
        _fs.atomic_write_bytes(outside / "x.md", b"x", root_real=root)

    assert excinfo.value.code == "path.symlink_escape"
    assert list(outside.iterdir()) == []


def test_atomic_write_bytes_keeps_the_original_and_cleans_up_when_replace_fails(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (root / "a.md").write_bytes(b"old")

    def failing_replace(src, dst, **kwargs):  # noqa: ANN001, ANN003, ANN202
        raise OSError(errno.EIO, "replace failed")

    monkeypatch.setattr(os, "replace", failing_replace)

    with pytest.raises(FsError) as excinfo:
        _write(root, "a.md", b"new")

    assert excinfo.value.code == "io.error"
    assert isinstance(excinfo.value.__cause__, OSError)
    assert (root / "a.md").read_bytes() == b"old"
    assert _names(root) == ["a.md"]


def test_atomic_write_bytes_removes_the_temp_file_when_the_data_write_fails(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_fsync(fd):  # noqa: ANN001, ANN202
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(os, "fsync", failing_fsync)

    with pytest.raises(FsError) as excinfo:
        _write(root, "new.md", b"x")

    assert excinfo.value.code == "io.error"
    assert "disk full" in excinfo.value.summary
    assert _names(root) == []


def test_atomic_write_bytes_tolerates_a_directory_fsync_failure(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_open = os.open

    def open_refusing_directories(path, flags, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        if Path(path).is_dir():
            raise PermissionError(errno.EACCES, "cannot open directory")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", open_refusing_directories)

    _write(root, "a.md", b"x")

    assert (root / "a.md").read_bytes() == b"x"


def test_atomic_write_bytes_maps_permission_errors_to_io_error(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_open = os.open

    def deny_creation(path, flags, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        if flags & os.O_EXCL:
            raise PermissionError(errno.EACCES, "Permission denied")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", deny_creation)

    with pytest.raises(FsError) as excinfo:
        _write(root, "docs/a.md", b"x")

    error = excinfo.value
    assert error.code == "io.error"
    assert str(error).startswith("[io.error] ")
    assert "PermissionError" in error.summary
    assert error.path == "docs/a.md"
    assert error.root == root
    assert "Hint: " in str(error)
    assert isinstance(error.__cause__, PermissionError)


def _after_first_fsync(monkeypatch: pytest.MonkeyPatch, action) -> None:  # noqa: ANN001
    """Run ``action`` once, right after the temp file data is flushed."""
    real_fsync = os.fsync
    state = {"done": False}

    def fsync(fd):  # noqa: ANN001, ANN202
        real_fsync(fd)
        if not state["done"]:
            state["done"] = True
            action()

    monkeypatch.setattr(os, "fsync", fsync)


def test_atomic_write_bytes_refuses_a_target_swapped_for_an_outside_symlink(
    root: Path, outside: Path, make_symlink, monkeypatch: pytest.MonkeyPatch
) -> None:
    (root / "docs").mkdir()
    target = root / "docs" / "a.md"
    target.write_bytes(b"old")

    def swap() -> None:
        target.unlink()
        make_symlink(target, outside / "stolen.md")

    _after_first_fsync(monkeypatch, swap)

    with pytest.raises(FsError) as excinfo:
        _write(root, "docs/a.md", b"new")

    assert excinfo.value.code == "path.symlink_escape"
    assert list(outside.iterdir()) == []
    assert _names(root / "docs") == ["a.md"]
    assert target.is_symlink()


def test_atomic_write_bytes_refuses_a_parent_swapped_for_an_outside_symlink(
    root: Path, outside: Path, make_symlink, monkeypatch: pytest.MonkeyPatch
) -> None:
    (root / "docs").mkdir()

    def swap() -> None:
        (root / "docs").rename(root / "docs-moved")
        make_symlink(root / "docs", outside)

    _after_first_fsync(monkeypatch, swap)

    with pytest.raises(FsError) as excinfo:
        _write(root, "docs/a.md", b"new")

    assert excinfo.value.code == "path.symlink_escape"
    assert list(outside.iterdir()) == []


# ---------------------------------------------------------------------------
# decode_text / content_version (FS-6)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"hello", "hello"),
        ("Ñandú ✓".encode(), "Ñandú ✓"),
        (b"a\r\nb\r\n", "a\r\nb\r\n"),
        (b"a\rb", "a\rb"),
    ],
    ids=["ascii", "unicode", "crlf-untranslated", "lone-cr-untranslated"],
)
def test_decode_text_decodes_strict_utf8_without_newline_translation(
    data: bytes, expected: str
) -> None:
    assert _fs.decode_text(data, path="docs/a.md") == expected


def test_decode_text_keeps_the_bom_as_a_leading_character() -> None:
    text = _fs.decode_text(b"\xef\xbb\xbfhi", path="docs/a.md")

    assert text == "﻿hi"
    assert text.startswith("﻿")


def test_decode_text_names_the_file_when_bytes_are_not_utf8() -> None:
    with pytest.raises(FsError) as excinfo:
        _fs.decode_text(b"ok\xff\xfe", path="docs/bad.md")

    error = excinfo.value
    assert error.code == "document.decode_error"
    assert error.path == "docs/bad.md"
    assert "utf-8" in error.summary
    assert "Hint: " in str(error)
    assert str(error).startswith("[document.decode_error] ")
    assert isinstance(error.__cause__, UnicodeDecodeError)


def test_decode_text_honours_an_explicit_encoding() -> None:
    assert _fs.decode_text("ñ".encode("latin-1"), path="a.md", encoding="latin-1") == "ñ"


def test_content_version_is_the_prefixed_lowercase_sha256_digest() -> None:
    assert _fs.content_version(b"hello") == (
        "sha256:2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )


@pytest.mark.parametrize("data", [b"", b"a", b"a\r\nb", b"\xef\xbb\xbfhi"])
def test_content_version_matches_hashlib_for_any_bytes(data: bytes) -> None:
    assert _fs.content_version(data) == f"sha256:{hashlib.sha256(data).hexdigest()}"


def test_content_version_differs_for_different_bytes() -> None:
    assert _fs.content_version(b"a\n") != _fs.content_version(b"a\r\n")


# ---------------------------------------------------------------------------
# hashed_asset_name (FS-7)
# ---------------------------------------------------------------------------


def test_hashed_asset_name_is_deterministic_and_embeds_the_content_hash() -> None:
    first = _fs.hashed_asset_name("My Diagram.png", b"abc")
    second = _fs.hashed_asset_name("My Diagram.png", b"abc")

    assert first == second
    assert re.fullmatch(r"My-Diagram--[0-9a-f]{16}\.png", first)
    assert first == "My-Diagram--ba7816bf8f01cfea.png"


def test_hashed_asset_name_changes_when_the_bytes_change() -> None:
    assert _fs.hashed_asset_name("a.png", b"abc") != _fs.hashed_asset_name("a.png", b"abd")


@pytest.mark.parametrize(
    ("filename", "stem_and_suffix"),
    [
        ("diagram.PNG", "diagram--{h}.PNG"),
        ("My Diagram.final.png", "My-Diagram.final--{h}.png"),
        ("a   b!!c.png", "a-b-c--{h}.png"),
        ("--edge--.png", "edge--{h}.png"),
        ("###.png", "asset--{h}.png"),
        ("README", "README--{h}"),
        ("Diagrama ñandú.png", "Diagrama-and--{h}.png"),
        ("a.p g", "a--{h}.p-g"),
        (".png", "png--{h}"),
    ],
)
def test_hashed_asset_name_sanitizes_the_stem_and_preserves_the_suffix(
    filename: str, stem_and_suffix: str
) -> None:
    digest = hashlib.sha256(b"abc").hexdigest()[:16]

    assert _fs.hashed_asset_name(filename, b"abc") == stem_and_suffix.format(h=digest)


@pytest.mark.parametrize("filename", [None, "", "   "])
def test_hashed_asset_name_requires_a_name(filename: str | None) -> None:
    with pytest.raises(FsError) as excinfo:
        _fs.hashed_asset_name(filename, b"abc")

    assert excinfo.value.code == "asset.missing_name"
    assert "Hint: " in str(excinfo.value)
    assert str(excinfo.value).startswith("[asset.missing_name] ")


@pytest.mark.parametrize(
    "filename", ["../x.png", "a/b.png", "a\\b.png", "/abs.png", "..", "x..png"]
)
def test_hashed_asset_name_rejects_names_with_separators_or_dot_dot(filename: str) -> None:
    with pytest.raises(FsError) as excinfo:
        _fs.hashed_asset_name(filename, b"abc")

    error = excinfo.value
    assert error.code == "asset.invalid_name"
    assert error.path == filename
    assert "Hint: " in str(error)
    assert str(error).startswith("[asset.invalid_name] ")
