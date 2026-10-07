"""Unit tests for the pending manifest: persistence, status parsing, classification.

No git here: the dirty-path set is built from literal ``git status --porcelain=v1
-z`` text, and the workdir is a plain directory below ``tmp_path``.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from wikiops.providers._fs import content_version
from wikiops.providers.github_wiki.errors import GithubWikiError
from wikiops.providers.github_wiki.manifest import (
    MAX_REPORTED_PATHS,
    Classification,
    PendingManifest,
    parse_status,
)
from wikiops.providers.github_wiki.workdir import manifest_path

posix_only = pytest.mark.skipif(os.name != "posix", reason="symlinks need POSIX")


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    path = tmp_path / "wd"
    (path / ".git").mkdir(parents=True)
    return path


@pytest.fixture
def manifest(workdir: Path) -> PendingManifest:
    return PendingManifest(manifest_path(workdir / ".git"), workdir=workdir)


def write(workdir: Path, name: str, content: bytes = b"content") -> str:
    target = workdir / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return name


def on_disk(workdir: Path) -> dict[str, object]:
    return json.loads((workdir / ".git" / "wikiops" / "pending.json").read_text())


def seed(workdir: Path, document: object) -> Path:
    path = manifest_path(workdir / ".git")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document if isinstance(document, str) else json.dumps(document))
    return path


def sha(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


# -- status parsing -----------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "paths"),
    [
        (" M Home.md\0", ("Home.md",)),
        ("?? new.md\0 M Home.md\0A  added.md\0D  gone.md\0", ("new.md", "Home.md", "added.md", "gone.md")),
        ("MM both.md\0", ("both.md",)),
        ("UU conflicted.md\0AA both-added.md\0", ("conflicted.md", "both-added.md")),
        ("?? assets/a1.png\0", ("assets/a1.png",)),
        ("?? with space.md\0", ("with space.md",)),
        ("?? line\nbreak.md\0", ("line\nbreak.md",)),
        ("?? Página ñandú.md\0", ("Página ñandú.md",)),
        (" M no-terminator.md", ("no-terminator.md",)),
        ("", ()),
    ],
)
def test_status_entries_yield_their_paths(text: str, paths: tuple[str, ...]) -> None:
    assert parse_status(text) == paths


@pytest.mark.parametrize(
    ("text", "paths"),
    [
        ("R  new.md\0old.md\0", ("new.md", "old.md")),
        ("C  copy.md\0source.md\0", ("copy.md", "source.md")),
        ("RM new.md\0old.md\0 M other.md\0", ("new.md", "old.md", "other.md")),
        (" R renamed-in-worktree.md\0original.md\0", ("renamed-in-worktree.md", "original.md")),
    ],
)
def test_renames_and_copies_yield_both_paths(text: str, paths: tuple[str, ...]) -> None:
    assert parse_status(text) == paths


@pytest.mark.parametrize(
    "text",
    [
        "M\0",  # too short
        "XY\0",
        "M  \0",  # empty path
        "MXfoo.md\0",  # no separator space
        "R  only-new.md\0",  # rename without its origin
        "R  new.md\0\0",  # rename with an empty origin
    ],
)
def test_malformed_status_is_a_coded_git_failure(text: str) -> None:
    with pytest.raises(GithubWikiError) as error:
        parse_status(text)
    assert error.value.code == "sync.git_failed"


@pytest.mark.parametrize(
    ("text", "paths"),
    [
        ("?? nested-repo/\0", ("nested-repo/",)),
        ("?? Home.md\0?? vendor/clone/\0 M other.md\0", ("Home.md", "vendor/clone/", "other.md")),
    ],
)
def test_a_collapsed_untracked_directory_keeps_its_trailing_slash(
    text: str, paths: tuple[str, ...]
) -> None:
    # git collapses a nested repository even under --untracked-files=all; the entry
    # stays recognisable as a directory instead of posing as a page path.
    assert parse_status(text) == paths


@pytest.mark.parametrize(
    "text",
    [
        " M dir/\0",  # only an untracked entry can be a collapsed directory
        "A  dir/\0",
        "R  new/\0old.md\0",
    ],
)
def test_a_trailing_slash_on_anything_but_an_untracked_entry_is_a_coded_git_failure(text: str) -> None:
    with pytest.raises(GithubWikiError) as error:
        parse_status(text)
    assert error.value.code == "sync.git_failed"


def test_a_collapsed_directory_is_always_foreign_even_when_the_manifest_lists_files_below_it(
    workdir: Path, manifest: PendingManifest
) -> None:
    write(workdir, "nested-repo/Page.md", b"mine")
    manifest.record(["nested-repo/Page.md"])
    dirty = parse_status("?? nested-repo/\0")
    result = manifest.classify(dirty)
    assert result == Classification(pending=(), foreign=("nested-repo/",))
    with pytest.raises(GithubWikiError) as error:
        manifest.check(dirty)
    assert error.value.code == "workdir.dirty"
    assert error.value.context["paths"] == "nested-repo/"


# -- recording ----------------------------------------------------------------


def test_record_stores_the_hash_of_the_bytes_read_back_from_the_workdir(
    workdir: Path, manifest: PendingManifest
) -> None:
    write(workdir, "Home.md", b"# Home\n")
    write(workdir, "assets/a1.png", b"\x89PNG")
    manifest.record(["Home.md", "assets/a1.png"])
    assert on_disk(workdir) == {
        "version": 1,
        "paths": {"Home.md": sha(b"# Home\n"), "assets/a1.png": sha(b"\x89PNG")},
    }
    assert on_disk(workdir)["paths"]["Home.md"] == content_version(b"# Home\n")  # type: ignore[index]


def test_record_overwrites_the_hash_of_a_path_written_again(
    workdir: Path, manifest: PendingManifest
) -> None:
    write(workdir, "Home.md", b"one")
    manifest.record(["Home.md"])
    write(workdir, "Home.md", b"two")
    manifest.record(["Home.md"])
    assert manifest.entries() == {"Home.md": sha(b"two")}


def test_record_keeps_earlier_entries(workdir: Path, manifest: PendingManifest) -> None:
    write(workdir, "A.md", b"a")
    write(workdir, "B.md", b"b")
    manifest.record(["A.md"])
    manifest.record(["B.md"])
    assert manifest.entries() == {"A.md": sha(b"a"), "B.md": sha(b"b")}
    assert len(manifest) == 2


def test_record_creates_the_state_directory_and_leaves_no_temporary_file(
    workdir: Path, manifest: PendingManifest
) -> None:
    write(workdir, "Home.md")
    assert not (workdir / ".git" / "wikiops").exists()
    manifest.record(["Home.md"])
    assert sorted(entry.name for entry in (workdir / ".git" / "wikiops").iterdir()) == ["pending.json"]


def test_the_written_manifest_is_deterministic_and_newline_terminated(
    workdir: Path, manifest: PendingManifest
) -> None:
    write(workdir, "B.md", b"b")
    write(workdir, "A.md", b"a")
    manifest.record(["B.md", "A.md"])
    text = (workdir / ".git" / "wikiops" / "pending.json").read_text()
    assert text.endswith("\n")
    assert text.index('"A.md"') < text.index('"B.md"')


def test_record_of_a_missing_file_is_refused_and_writes_nothing(
    workdir: Path, manifest: PendingManifest
) -> None:
    with pytest.raises(GithubWikiError) as error:
        manifest.record(["Ghost.md"])
    assert error.value.code == "workdir.unusable"
    assert "Ghost.md" in str(error.value)
    assert not (workdir / ".git" / "wikiops").exists()


def test_record_without_a_git_directory_refuses_and_creates_no_stray_directories(tmp_path: Path) -> None:
    bare = tmp_path / "not-a-clone"
    bare.mkdir()
    (bare / "Home.md").write_bytes(b"page")
    stray = PendingManifest(manifest_path(bare / ".git"), workdir=bare)
    with pytest.raises(GithubWikiError) as error:
        stray.record(["Home.md"])
    assert error.value.code == "sync.workdir_not_clone"
    assert error.value.context["workdir"] == bare
    assert sorted(entry.name for entry in bare.iterdir()) == ["Home.md"]  # no .git/wikiops trees


def test_record_below_a_missing_nested_git_directory_creates_none_of_the_parents(tmp_path: Path) -> None:
    bare = tmp_path / "wd"
    (bare / "sub").mkdir(parents=True)
    (bare / "Home.md").write_bytes(b"page")
    stray = PendingManifest(manifest_path(bare / "sub" / "gitdir"), workdir=bare)
    with pytest.raises(GithubWikiError) as error:
        stray.record(["Home.md"])
    assert error.value.code == "sync.workdir_not_clone"
    assert list((bare / "sub").iterdir()) == []


def test_record_is_all_or_nothing(workdir: Path, manifest: PendingManifest) -> None:
    write(workdir, "Real.md")
    with pytest.raises(GithubWikiError):
        manifest.record(["Real.md", "Ghost.md"])
    assert manifest.entries() == {}


@posix_only
def test_record_refuses_a_symlink_and_a_path_through_an_escaping_symlink(
    workdir: Path, tmp_path: Path, manifest: PendingManifest
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("secret")
    (workdir / "link.md").symlink_to(outside / "secret.md")
    (workdir / "dirlink").symlink_to(outside, target_is_directory=True)
    for path in ("link.md", "dirlink/secret.md"):
        with pytest.raises(GithubWikiError) as error:
            manifest.record([path])
        assert error.value.code == "workdir.unusable"


@pytest.mark.parametrize(
    "path",
    ["", "/abs.md", "../up.md", "a/../b.md", "a//b.md", "a/./b.md", "a\\b.md", "C:/x.md",
     ".git/config", "sub/.GIT/x", "bad\x00.md", "bad\n.md"],
)
def test_record_rejects_unsafe_relative_paths_as_a_programming_error(
    manifest: PendingManifest, path: str
) -> None:
    with pytest.raises(ValueError, match="relative"):
        manifest.record([path])


@posix_only
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores permission bits")
def test_a_failing_manifest_write_is_a_coded_error(workdir: Path, manifest: PendingManifest) -> None:
    state = workdir / ".git" / "wikiops"
    state.mkdir()
    state.chmod(0o500)
    write(workdir, "Home.md")
    try:
        with pytest.raises(GithubWikiError) as error:
            manifest.record(["Home.md"])
    finally:
        state.chmod(0o700)
    assert error.value.code == "workdir.unusable"
    assert not (state / "pending.json").exists()


# -- loading ------------------------------------------------------------------


def test_a_missing_manifest_is_empty_and_is_not_created_by_reading(
    workdir: Path, manifest: PendingManifest
) -> None:
    assert manifest.entries() == {}
    assert len(manifest) == 0
    assert not (workdir / ".git" / "wikiops").exists()


VALID_HASH = "sha256:" + "ab" * 32


@pytest.mark.parametrize(
    "document",
    [
        "{not json",
        "",
        "[]",
        '"text"',
        "null",
        {"paths": {}},
        {"version": 2, "paths": {}},
        {"version": "1", "paths": {}},
        {"version": True, "paths": {}},
        {"version": 1},
        {"version": 1, "paths": []},
        {"version": 1, "paths": {"A.md": 5}},
        {"version": 1, "paths": {"A.md": "sha1:" + "ab" * 20}},
        {"version": 1, "paths": {"A.md": "sha256:XYZ"}},
        {"version": 1, "paths": {"A.md": "sha256:" + "AB" * 32}},  # uppercase hex
        {"version": 1, "paths": {"A.md": VALID_HASH + "00"}},
        {"version": 1, "paths": {"/abs.md": VALID_HASH}},
        {"version": 1, "paths": {"../up.md": VALID_HASH}},
        {"version": 1, "paths": {".git/config": VALID_HASH}},
        {"version": 1, "paths": {"": VALID_HASH}},
        '{"version": 1, "paths": {}} trailing',
        "[" * 100000,
    ],
)
def test_an_invalid_manifest_is_corrupt_and_names_the_file(
    workdir: Path, manifest: PendingManifest, document: object
) -> None:
    path = seed(workdir, document)
    with pytest.raises(GithubWikiError) as error:
        manifest.entries()
    assert error.value.code == "workdir.manifest_corrupt"
    assert str(path) in str(error.value)
    assert "Hint:" in str(error.value)


def test_a_manifest_that_is_not_utf8_is_corrupt(workdir: Path, manifest: PendingManifest) -> None:
    path = seed(workdir, "{}")
    path.write_bytes(b'{"version": 1, "paths": {"\xff\xfe": "x"}}')
    with pytest.raises(GithubWikiError) as error:
        manifest.entries()
    assert error.value.code == "workdir.manifest_corrupt"


def test_a_manifest_that_is_a_directory_is_corrupt(workdir: Path, manifest: PendingManifest) -> None:
    manifest_path(workdir / ".git").mkdir(parents=True)
    with pytest.raises(GithubWikiError) as error:
        manifest.entries()
    assert error.value.code == "workdir.manifest_corrupt"


def test_an_oversized_manifest_is_corrupt_without_being_parsed(
    workdir: Path, manifest: PendingManifest
) -> None:
    path = seed(workdir, "")
    path.write_bytes(b" " * (5 * 1024 * 1024))
    with pytest.raises(GithubWikiError) as error:
        manifest.entries()
    assert error.value.code == "workdir.manifest_corrupt"
    assert "large" in error.value.summary


@pytest.mark.parametrize("operation", ["classify", "check", "record", "discard", "prune", "len"])
def test_every_operation_reports_a_corrupt_manifest(
    workdir: Path, manifest: PendingManifest, operation: str
) -> None:
    seed(workdir, "{broken")
    write(workdir, "Home.md")
    calls = {
        "classify": lambda: manifest.classify(["Home.md"]),
        "check": lambda: manifest.check(["Home.md"]),
        "record": lambda: manifest.record(["Home.md"]),
        "discard": lambda: manifest.discard(["Home.md"]),
        "prune": lambda: manifest.prune(["Home.md"]),
        "len": lambda: len(manifest),
    }
    with pytest.raises(GithubWikiError) as error:
        calls[operation]()
    assert error.value.code == "workdir.manifest_corrupt"
    assert (workdir / ".git" / "wikiops" / "pending.json").read_text() == "{broken"  # untouched


# -- classification -----------------------------------------------------------


def test_a_dirty_manifest_path_with_the_recorded_hash_is_pending(
    workdir: Path, manifest: PendingManifest
) -> None:
    write(workdir, "Home.md", b"mine")
    manifest.record(["Home.md"])
    assert manifest.classify(["Home.md"]) == Classification(pending=("Home.md",), foreign=())


def test_a_pending_path_edited_by_the_user_is_foreign(
    workdir: Path, manifest: PendingManifest
) -> None:
    write(workdir, "Home.md", b"mine")
    manifest.record(["Home.md"])
    write(workdir, "Home.md", b"edited by the user")
    assert manifest.classify(["Home.md"]) == Classification(pending=(), foreign=("Home.md",))


def test_a_dirty_path_outside_the_manifest_is_foreign(
    workdir: Path, manifest: PendingManifest
) -> None:
    write(workdir, "notes.txt")
    assert manifest.classify(["notes.txt"]) == Classification(pending=(), foreign=("notes.txt",))


def test_a_deleted_pending_path_is_foreign(workdir: Path, manifest: PendingManifest) -> None:
    write(workdir, "Home.md", b"mine")
    manifest.record(["Home.md"])
    (workdir / "Home.md").unlink()
    assert manifest.classify(["Home.md"]) == Classification(pending=(), foreign=("Home.md",))


def test_a_pending_path_replaced_by_a_directory_is_foreign(
    workdir: Path, manifest: PendingManifest
) -> None:
    write(workdir, "Home.md", b"mine")
    manifest.record(["Home.md"])
    (workdir / "Home.md").unlink()
    (workdir / "Home.md").mkdir()
    assert manifest.classify(["Home.md"]).foreign == ("Home.md",)


@posix_only
def test_a_pending_path_replaced_by_a_symlink_is_foreign_even_with_equal_content(
    workdir: Path, manifest: PendingManifest
) -> None:
    write(workdir, "Home.md", b"mine")
    write(workdir, "Elsewhere.md", b"mine")
    manifest.record(["Home.md"])
    (workdir / "Home.md").unlink()
    (workdir / "Home.md").symlink_to("Elsewhere.md")
    assert manifest.classify(["Home.md"]).foreign == ("Home.md",)


def test_pending_and_foreign_paths_are_split_and_sorted(
    workdir: Path, manifest: PendingManifest
) -> None:
    write(workdir, "Home.md", b"mine")
    write(workdir, "assets/a1.png", b"png")
    manifest.record(["Home.md", "assets/a1.png"])
    write(workdir, "Other.md")
    write(workdir, "Another.md")
    result = manifest.classify(["Other.md", "Home.md", "Another.md", "assets/a1.png", "Home.md"])
    assert result.pending == ("Home.md", "assets/a1.png")
    assert result.foreign == ("Another.md", "Other.md")


def test_manifest_entries_that_are_not_dirty_are_ignored_by_classification(
    workdir: Path, manifest: PendingManifest
) -> None:
    write(workdir, "Home.md", b"mine")
    manifest.record(["Home.md"])
    assert manifest.classify([]) == Classification(pending=(), foreign=())


def test_classification_never_modifies_the_manifest(
    workdir: Path, manifest: PendingManifest
) -> None:
    write(workdir, "Home.md", b"mine")
    manifest.record(["Home.md"])
    before = (workdir / ".git" / "wikiops" / "pending.json").read_bytes()
    manifest.classify(["Home.md", "Other.md"])
    manifest.classify([])
    assert (workdir / ".git" / "wikiops" / "pending.json").read_bytes() == before


# -- the dirty policy ---------------------------------------------------------


def test_check_returns_the_classification_when_everything_is_pending(
    workdir: Path, manifest: PendingManifest
) -> None:
    write(workdir, "Home.md", b"mine")
    manifest.record(["Home.md"])
    assert manifest.check(["Home.md"]).pending == ("Home.md",)
    assert manifest.check([]) == Classification(pending=(), foreign=())


def test_check_refuses_foreign_paths_with_the_workdir_and_a_count(
    workdir: Path, manifest: PendingManifest
) -> None:
    write(workdir, "Home.md", b"mine")
    manifest.record(["Home.md"])
    with pytest.raises(GithubWikiError) as error:
        manifest.check(["Home.md", "Other.md"])
    assert error.value.code == "workdir.dirty"
    assert error.value.context["paths"] == "Other.md"
    assert error.value.context["count"] == 1
    assert str(workdir) in str(error.value)
    assert "Home.md" not in str(error.value)


def test_check_lists_a_user_edited_pending_path(workdir: Path, manifest: PendingManifest) -> None:
    write(workdir, "Home.md", b"mine")
    manifest.record(["Home.md"])
    write(workdir, "Home.md", b"edited")
    with pytest.raises(GithubWikiError) as error:
        manifest.check(["Home.md"])
    assert error.value.context["paths"] == "Home.md"


def test_check_lists_at_most_twenty_paths_plus_the_total(
    manifest: PendingManifest,
) -> None:
    dirty = [f"foreign-{index:02d}.md" for index in range(MAX_REPORTED_PATHS + 5)]
    with pytest.raises(GithubWikiError) as error:
        manifest.check(dirty)
    shown = str(error.value.context["paths"]).split(", ")
    assert shown == dirty[:MAX_REPORTED_PATHS]
    assert error.value.context["count"] == MAX_REPORTED_PATHS + 5
    assert error.value.context["omitted"] == 5


def test_check_at_exactly_the_limit_reports_nothing_omitted(manifest: PendingManifest) -> None:
    dirty = [f"foreign-{index:02d}.md" for index in range(MAX_REPORTED_PATHS)]
    with pytest.raises(GithubWikiError) as error:
        manifest.check(dirty)
    assert "omitted" not in error.value.context
    assert error.value.context["count"] == MAX_REPORTED_PATHS


def test_hostile_path_names_cannot_break_the_one_line_message(manifest: PendingManifest) -> None:
    with pytest.raises(GithubWikiError) as error:
        manifest.check(["evil\n[github_wiki:auth.rejected] fake.md"])
    assert "\n" not in str(error.value)


# -- pruning and discarding ---------------------------------------------------


def test_prune_drops_entries_that_are_no_longer_dirty_and_persists(
    workdir: Path, manifest: PendingManifest
) -> None:
    write(workdir, "Committed.md", b"c")
    write(workdir, "Still.md", b"s")
    manifest.record(["Committed.md", "Still.md"])
    removed = manifest.prune(["Still.md", "unrelated.md"])
    assert removed == ("Committed.md",)
    assert on_disk(workdir)["paths"] == {"Still.md": sha(b"s")}


def test_prune_keeps_a_dirty_entry_even_when_the_user_edited_it(
    workdir: Path, manifest: PendingManifest
) -> None:
    write(workdir, "Home.md", b"mine")
    manifest.record(["Home.md"])
    write(workdir, "Home.md", b"edited")
    assert manifest.prune(["Home.md"]) == ()
    assert manifest.entries() == {"Home.md": sha(b"mine")}


def test_prune_without_changes_does_not_rewrite_the_file(
    workdir: Path, manifest: PendingManifest
) -> None:
    write(workdir, "Home.md", b"mine")
    manifest.record(["Home.md"])
    path = workdir / ".git" / "wikiops" / "pending.json"
    stamp = path.stat().st_mtime_ns
    assert manifest.prune(["Home.md"]) == ()
    assert path.stat().st_mtime_ns == stamp


def test_prune_of_a_missing_manifest_creates_nothing(workdir: Path, manifest: PendingManifest) -> None:
    assert manifest.prune(["Home.md"]) == ()
    assert not (workdir / ".git" / "wikiops").exists()


def test_prune_can_empty_the_manifest(workdir: Path, manifest: PendingManifest) -> None:
    write(workdir, "Home.md")
    manifest.record(["Home.md"])
    assert manifest.prune([]) == ("Home.md",)
    assert manifest.entries() == {}
    assert on_disk(workdir) == {"version": 1, "paths": {}}


def test_discard_removes_committed_paths_and_ignores_unknown_ones(
    workdir: Path, manifest: PendingManifest
) -> None:
    write(workdir, "A.md", b"a")
    write(workdir, "B.md", b"b")
    manifest.record(["A.md", "B.md"])
    manifest.discard(["A.md", "Never-recorded.md"])
    assert manifest.entries() == {"B.md": sha(b"b")}


def test_discard_reads_a_one_shot_iterable_once_and_forgets_every_path_in_it(
    workdir: Path, manifest: PendingManifest
) -> None:
    for name in ("A.md", "B.md", "C.md"):
        write(workdir, name, name.encode())
    manifest.record(["A.md", "B.md", "C.md"])
    manifest.discard(name for name in ("A.md", "B.md"))
    assert manifest.entries() == {"C.md": sha(b"C.md")}


def test_discard_accepts_a_generator_that_names_only_unknown_paths(
    workdir: Path, manifest: PendingManifest
) -> None:
    write(workdir, "A.md")
    manifest.record(["A.md"])
    manifest.discard(name for name in ("Other.md", "More.md"))
    assert list(manifest.entries()) == ["A.md"]


def test_discard_of_nothing_known_does_not_rewrite_the_file(
    workdir: Path, manifest: PendingManifest
) -> None:
    write(workdir, "A.md")
    manifest.record(["A.md"])
    path = workdir / ".git" / "wikiops" / "pending.json"
    stamp = path.stat().st_mtime_ns
    manifest.discard(["Other.md"])
    assert path.stat().st_mtime_ns == stamp


def test_a_second_instance_sees_what_the_first_recorded(workdir: Path) -> None:
    first = PendingManifest(manifest_path(workdir / ".git"), workdir=workdir)
    second = PendingManifest(manifest_path(workdir / ".git"), workdir=workdir)
    write(workdir, "Home.md", b"mine")
    first.record(["Home.md"])
    assert second.entries() == {"Home.md": sha(b"mine")}
    assert second.classify(["Home.md"]).pending == ("Home.md",)


def test_the_manifest_exposes_its_path(workdir: Path, manifest: PendingManifest) -> None:
    assert manifest.path == manifest_path(workdir / ".git")


def test_recording_nothing_writes_nothing(workdir: Path, manifest: PendingManifest) -> None:
    manifest.record([])
    assert not (workdir / ".git" / "wikiops").exists()


def test_a_manifest_below_a_regular_file_is_reported_as_unreadable(
    workdir: Path, manifest: PendingManifest
) -> None:
    (workdir / ".git" / "wikiops").write_text("a file where the directory should be")
    with pytest.raises(GithubWikiError) as error:
        manifest.entries()
    assert error.value.code == "workdir.manifest_corrupt"
    assert "unreadable" in error.value.summary


@posix_only
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores permission bits")
def test_a_manifest_that_cannot_be_read_is_corrupt(workdir: Path, manifest: PendingManifest) -> None:
    path = seed(workdir, {"version": 1, "paths": {}})
    path.chmod(0o000)
    try:
        with pytest.raises(GithubWikiError) as error:
            manifest.entries()
    finally:
        path.chmod(0o600)
    assert error.value.code == "workdir.manifest_corrupt"
    assert "unreadable" in error.value.summary
