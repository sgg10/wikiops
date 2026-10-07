"""Unit tests for workdir resolution: explicit paths, per-profile cache paths, usability.

Nothing here touches the real user cache or home directory: every test passes an
explicit ``home``/``cache_dir``/``environ`` or works below ``tmp_path``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from wikiops.providers.github_wiki import workdir as workdir_module
from wikiops.providers.github_wiki.errors import GithubWikiError
from wikiops.providers.github_wiki.workdir import (
    cache_base,
    default_workdir,
    lock_path,
    manifest_path,
    prepare_parent,
    profile_key,
    resolve_workdir,
    state_directory,
)

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX permission semantics")
not_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores permission bits"
)


def resolve(tmp_path: Path, **overrides: object) -> Path:
    """Resolve with every external input pinned below ``tmp_path``."""
    options: dict[str, object] = {
        "configured": None,
        "host": "github.com",
        "repository": "acme/platform",
        "provider_name": "docs",
        "cwd": tmp_path / "cwd",
        "cache_dir": tmp_path / "cache",
        "home": tmp_path / "home",
    }
    options.update(overrides)
    (tmp_path / "cwd").mkdir(exist_ok=True)
    return resolve_workdir(**options)  # type: ignore[arg-type]


def code_of(error: pytest.ExceptionInfo[GithubWikiError]) -> str:
    return error.value.code


# -- explicit workdir ---------------------------------------------------------


def test_explicit_absolute_workdir_is_used_as_given(tmp_path: Path) -> None:
    target = tmp_path / "wiki-checkout"
    assert resolve(tmp_path, configured=str(target)) == target.resolve()


def test_relative_workdir_resolves_against_the_working_directory(tmp_path: Path) -> None:
    result = resolve(tmp_path, configured="./.wiki-checkout")
    assert result == (tmp_path / "cwd" / ".wiki-checkout").resolve()
    assert result.is_absolute()


def test_a_different_working_directory_gives_a_different_result(tmp_path: Path) -> None:
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert resolve(tmp_path, configured="w", cwd=other) == (other / "w").resolve()
    assert resolve(tmp_path, configured="w") == (tmp_path / "cwd" / "w").resolve()


def test_tilde_is_expanded_against_home(tmp_path: Path) -> None:
    result = resolve(tmp_path, configured="~/wikis/one")
    assert result == (tmp_path / "home" / "wikis" / "one").resolve()


def test_symlinked_workdir_is_resolved_to_its_real_path(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    assert resolve(tmp_path, configured=str(link / "wiki")) == (real / "wiki").resolve()


def test_dot_segments_are_normalised(tmp_path: Path) -> None:
    result = resolve(tmp_path, configured=str(tmp_path / "a" / ".." / "b"))
    assert result == (tmp_path / "b").resolve()


@pytest.mark.parametrize("blank", ["", " ", "\t", "\n"])
def test_blank_workdir_is_refused_instead_of_meaning_the_working_directory(
    tmp_path: Path, blank: str
) -> None:
    with pytest.raises(GithubWikiError) as error:
        resolve(tmp_path, configured=blank)
    assert error.value.code == "workdir.unusable"
    assert "empty" in error.value.summary


def test_unknown_user_tilde_is_refused(tmp_path: Path) -> None:
    with pytest.raises(GithubWikiError) as error:
        resolve(tmp_path, configured="~no-such-user-wikiops/wiki")
    assert code_of(error) == "workdir.unusable"


def test_the_home_directory_itself_is_refused(tmp_path: Path) -> None:
    (tmp_path / "home").mkdir()
    with pytest.raises(GithubWikiError) as error:
        resolve(tmp_path, configured="~")
    assert code_of(error) == "workdir.unusable"
    assert "home" in error.value.summary


def test_a_directory_inside_home_is_accepted(tmp_path: Path) -> None:
    (tmp_path / "home").mkdir()
    assert resolve(tmp_path, configured="~/wiki") == (tmp_path / "home" / "wiki").resolve()


def test_the_filesystem_root_is_refused(tmp_path: Path) -> None:
    root = Path(tmp_path.anchor)
    with pytest.raises(GithubWikiError) as error:
        resolve(tmp_path, configured=str(root))
    assert code_of(error) == "workdir.unusable"


# -- default (cache) workdir --------------------------------------------------


def test_default_path_has_one_segment_per_part_in_g3_order(tmp_path: Path) -> None:
    result = resolve(tmp_path)
    expected = tmp_path / "cache" / "wikiops" / "github_wiki" / "github.com" / "acme" / "platform" / "p-docs"
    assert result == expected.resolve()


def test_host_owner_and_repo_are_lowercased(tmp_path: Path) -> None:
    result = resolve(tmp_path, host="GitHub.Example.IO", repository="ACME/Platform-Docs")
    assert result.parts[-4:] == ("github.example.io", "acme", "platform-docs", "p-docs")


def test_owner_and_repo_are_never_joined_so_dashes_cannot_collide(tmp_path: Path) -> None:
    first = resolve(tmp_path, repository="a-b/c")
    second = resolve(tmp_path, repository="a/b-c")
    assert first != second
    assert first.parts[-3:-1] == ("a-b", "c")
    assert second.parts[-3:-1] == ("a", "b-c")


def test_each_profile_gets_its_own_directory_under_the_same_repository(tmp_path: Path) -> None:
    docs_a = resolve(tmp_path, provider_name="docs-a")
    docs_b = resolve(tmp_path, provider_name="docs-b")
    assert docs_a.parent == docs_b.parent
    assert docs_a.name == "p-docs-a"
    assert docs_b.name == "p-docs-b"


def test_profiles_differing_only_by_case_get_distinct_directories(tmp_path: Path) -> None:
    upper = resolve(tmp_path, provider_name="Docs")
    lower = resolve(tmp_path, provider_name="docs")
    assert upper.name == "p-%44ocs"
    assert lower.name == "p-docs"
    assert upper.name.casefold() != lower.name.casefold()


@pytest.mark.parametrize(
    "unsafe",
    [
        "a/b",
        "..",
        "a/../b",
        "a\\b",
        "a\x00b",
    ],
)
def test_unsafe_host_owner_or_repo_segments_never_reach_the_path(
    tmp_path: Path, unsafe: str
) -> None:
    for options in (
        {"host": unsafe},
        {"repository": f"{unsafe}/repo"},
        {"repository": f"owner/{unsafe}"},
    ):
        with pytest.raises(GithubWikiError) as error:
            resolve(tmp_path, **options)
        assert error.value.code == "workdir.unusable"


def test_repository_without_owner_is_refused(tmp_path: Path) -> None:
    with pytest.raises(GithubWikiError) as error:
        resolve(tmp_path, repository="platform")
    assert code_of(error) == "workdir.unusable"


# -- cache base per operating system -----------------------------------------


def test_linux_prefers_an_absolute_xdg_cache_home(tmp_path: Path) -> None:
    xdg = tmp_path / "xdg"
    base = cache_base(platform="linux", environ={"XDG_CACHE_HOME": str(xdg)}, home=tmp_path / "h")
    assert base == xdg


@pytest.mark.parametrize("value", [None, "", "relative/cache"])
def test_linux_falls_back_to_dot_cache_when_xdg_is_unset_empty_or_relative(
    tmp_path: Path, value: str | None
) -> None:
    environ = {} if value is None else {"XDG_CACHE_HOME": value}
    assert cache_base(platform="linux", environ=environ, home=tmp_path / "h") == tmp_path / "h" / ".cache"


def test_macos_uses_library_caches_and_ignores_xdg(tmp_path: Path) -> None:
    base = cache_base(
        platform="darwin", environ={"XDG_CACHE_HOME": str(tmp_path / "x")}, home=tmp_path / "h"
    )
    assert base == tmp_path / "h" / "Library" / "Caches"


def test_windows_uses_localappdata(tmp_path: Path) -> None:
    local = tmp_path / "local"
    base = cache_base(platform="win32", environ={"LOCALAPPDATA": str(local)}, home=tmp_path / "h")
    assert base == local


@pytest.mark.parametrize("value", [None, "", "relative"])
def test_windows_without_a_usable_localappdata_falls_back_under_home(
    tmp_path: Path, value: str | None
) -> None:
    environ = {} if value is None else {"LOCALAPPDATA": value}
    base = cache_base(platform="win32", environ=environ, home=tmp_path / "h")
    assert base == tmp_path / "h" / "AppData" / "Local"


def test_a_relative_home_cannot_become_a_cache_base() -> None:
    with pytest.raises(GithubWikiError) as error:
        cache_base(platform="linux", environ={}, home=Path("relative-home"))
    assert error.value.code == "workdir.unusable"


def test_default_workdir_uses_the_injected_cache_directory_over_the_platform(
    tmp_path: Path,
) -> None:
    result = default_workdir(
        host="github.com",
        repository="acme/platform",
        provider_name="docs",
        cache_dir=tmp_path / "forced",
        platform="darwin",
        environ={},
        home=tmp_path / "h",
    )
    assert result == tmp_path / "forced" / "wikiops" / "github_wiki" / "github.com" / "acme" / "platform" / "p-docs"


def test_default_workdir_falls_back_to_the_platform_cache_without_an_override(
    tmp_path: Path,
) -> None:
    result = default_workdir(
        host="github.com",
        repository="acme/platform",
        provider_name="docs",
        platform="linux",
        environ={"XDG_CACHE_HOME": str(tmp_path / "xdg")},
        home=tmp_path / "h",
    )
    assert result.parts[: len((tmp_path / "xdg").parts)] == (tmp_path / "xdg").parts
    assert result.name == "p-docs"


def test_a_relative_cache_override_is_refused(tmp_path: Path) -> None:
    with pytest.raises(GithubWikiError) as error:
        default_workdir(
            host="github.com",
            repository="acme/platform",
            provider_name="docs",
            cache_dir=Path("relative-cache"),
        )
    assert error.value.code == "workdir.unusable"


# -- profile key --------------------------------------------------------------


def test_key_keeps_lowercase_digits_dash_and_underscore() -> None:
    assert profile_key("docs-a_1") == "p-docs-a_1"


def test_key_escapes_uppercase_and_other_characters_per_utf8_byte() -> None:
    assert profile_key("Docs") == "p-%44ocs"
    assert profile_key("a.b") == "p-a%2Eb"
    assert profile_key("a/b") == "p-a%2Fb"
    assert profile_key("100%") == "p-100%25"
    assert profile_key("é") == "p-%C3%A9"


@pytest.mark.parametrize("name", [".", "..", "/", "%", "", " ", "con", "NUL", "aux.txt", "日本語"])
def test_key_is_a_safe_single_path_component(name: str) -> None:
    key = profile_key(name)
    assert key.startswith("p-")
    assert all(char.isascii() and (char.isalnum() or char in "%-_~") for char in key)
    assert key not in {".", ".."}
    assert "/" not in key and "\\" not in key
    assert key.split(".")[0].upper() not in {"CON", "PRN", "AUX", "NUL", "COM1", "LPT1"}


def test_key_is_injective_even_when_compared_case_insensitively() -> None:
    names = [
        "Docs", "docs", "DOCS", "dOcS", ".", "..", "/", "\\", "%", "%25", "%44ocs",
        "%4a", "J", "j", "é", "É", "日本", " ", "", "a.b", "a-b", "a_b", "a b",
        "p-docs", "P-docs",
    ]
    keys = [profile_key(name) for name in names]
    assert len(set(keys)) == len(names)
    assert len({key.casefold() for key in keys}) == len(names)


def test_key_of_a_very_long_name_stays_within_a_filename_and_stays_distinct() -> None:
    first = profile_key("é" * 200)
    second = profile_key("é" * 199 + "e")
    assert len(first.encode()) <= 200
    assert first != second
    assert profile_key("é" * 200) == first  # deterministic


# -- usability ----------------------------------------------------------------


def test_a_workdir_that_is_a_regular_file_is_unusable(tmp_path: Path) -> None:
    target = tmp_path / "wiki"
    target.write_text("not a directory")
    with pytest.raises(GithubWikiError) as error:
        resolve(tmp_path, configured=str(target))
    assert error.value.code == "workdir.unusable"
    assert str(target.resolve()) in str(error.value)
    assert target.read_text() == "not a directory"


def test_a_workdir_below_a_regular_file_is_not_creatable(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    with pytest.raises(GithubWikiError) as error:
        resolve(tmp_path, configured=str(blocker / "wiki"))
    assert code_of(error) == "workdir.unusable"


@posix_only
@not_root
def test_a_read_only_existing_workdir_is_unusable(tmp_path: Path) -> None:
    target = tmp_path / "wiki"
    target.mkdir()
    target.chmod(0o500)
    try:
        with pytest.raises(GithubWikiError) as error:
            resolve(tmp_path, configured=str(target))
        assert code_of(error) == "workdir.unusable"
    finally:
        target.chmod(0o700)


@posix_only
@not_root
def test_a_missing_workdir_below_a_read_only_directory_is_not_creatable(tmp_path: Path) -> None:
    parent = tmp_path / "locked"
    parent.mkdir()
    parent.chmod(0o500)
    try:
        with pytest.raises(GithubWikiError) as error:
            resolve(tmp_path, configured=str(parent / "deep" / "wiki"))
        assert code_of(error) == "workdir.unusable"
    finally:
        parent.chmod(0o700)


@posix_only
def test_a_symlink_loop_is_unusable_and_not_an_unhandled_error(tmp_path: Path) -> None:
    first, second = tmp_path / "one", tmp_path / "two"
    first.symlink_to(second)
    second.symlink_to(first)
    with pytest.raises(GithubWikiError) as error:
        resolve(tmp_path, configured=str(first / "wiki"))
    assert code_of(error) == "workdir.unusable"


def test_a_writable_existing_directory_and_a_creatable_missing_path_are_usable(
    tmp_path: Path,
) -> None:
    existing = tmp_path / "existing"
    existing.mkdir()
    assert resolve(tmp_path, configured=str(existing)) == existing.resolve()
    assert resolve(tmp_path, configured=str(tmp_path / "a" / "b" / "c")) == (
        tmp_path / "a" / "b" / "c"
    ).resolve()
    assert not (tmp_path / "a").exists()  # resolution never creates anything


def test_resolution_creates_nothing_for_the_default_path(tmp_path: Path) -> None:
    resolve(tmp_path)
    assert not (tmp_path / "cache").exists()


# -- parent preparation -------------------------------------------------------


def test_prepare_parent_creates_the_missing_parents_but_not_the_workdir(tmp_path: Path) -> None:
    workdir = tmp_path / "a" / "b" / "wiki"
    prepare_parent(workdir)
    assert (tmp_path / "a" / "b").is_dir()
    assert not workdir.exists()


def test_prepare_parent_is_idempotent_and_keeps_existing_content(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "keep.txt").write_text("keep")
    prepare_parent(parent / "wiki")
    prepare_parent(parent / "wiki")
    assert (parent / "keep.txt").read_text() == "keep"


def test_prepare_parent_fails_coded_when_a_file_is_in_the_way(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    with pytest.raises(GithubWikiError) as error:
        prepare_parent(blocker / "wiki")
    assert error.value.code == "workdir.unusable"
    assert blocker.read_text() == "x"


# -- state file locations -----------------------------------------------------


def test_state_files_live_in_a_wikiops_directory_inside_the_git_directory(tmp_path: Path) -> None:
    git_dir = tmp_path / ".git"
    assert state_directory(git_dir) == git_dir / "wikiops"
    assert manifest_path(git_dir) == git_dir / "wikiops" / "pending.json"
    assert lock_path(git_dir) == git_dir / "wikiops" / "lock"


# -- process defaults ---------------------------------------------------------


def test_the_running_platform_and_environment_are_used_when_nothing_is_injected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "process-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setattr("sys.platform", "linux")
    assert cache_base() == home / ".cache"


def test_the_process_working_directory_and_home_are_the_defaults_of_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "process-home"
    work = tmp_path / "process-cwd"
    home.mkdir()
    work.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.chdir(work)
    relative = resolve_workdir(
        configured="wiki", host="github.com", repository="a/b", provider_name="p"
    )
    assert relative == (work / "wiki").resolve()
    tilde = resolve_workdir(
        configured="~/wiki", host="github.com", repository="a/b", provider_name="p"
    )
    assert tilde == (home / "wiki").resolve()


def test_a_cache_workdir_through_a_symlink_loop_is_unusable(tmp_path: Path) -> None:
    first, second = tmp_path / "loop-a", tmp_path / "loop-b"
    first.symlink_to(second)
    second.symlink_to(first)
    with pytest.raises(GithubWikiError) as error:
        resolve(tmp_path, cache_dir=first)
    assert code_of(error) == "workdir.unusable"


def test_a_path_without_any_existing_ancestor_is_unusable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workdir_module.os.path, "lexists", lambda _path: False)
    with pytest.raises(GithubWikiError) as error:
        workdir_module._check_usable(tmp_path / "nowhere")
    assert code_of(error) == "workdir.unusable"
    assert "ancestor" in error.value.summary


@posix_only
def test_a_path_that_cannot_be_inspected_is_unusable(tmp_path: Path) -> None:
    first, second = tmp_path / "one", tmp_path / "two"
    first.symlink_to(second)
    second.symlink_to(first)
    with pytest.raises(GithubWikiError) as error:
        workdir_module._check_usable(first)
    assert code_of(error) == "workdir.unusable"
    assert "cannot be inspected" in error.value.summary


def test_a_long_name_is_cut_on_an_escape_boundary(tmp_path: Path) -> None:
    # 33 two-byte characters = 198 escaped characters; the cut at 100 lands inside an escape
    for length in (33, 34, 35, 60):
        key = profile_key("é" * length)
        prefix = key[len("p-") : key.index("~")]
        assert prefix.count("%") * 3 == len(prefix)  # whole %XX triples only
