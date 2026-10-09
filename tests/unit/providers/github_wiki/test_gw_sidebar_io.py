"""Unit tests for the local scans of the sidebar IO layer of ``github_wiki`` (``sidebar_io.py``).

``root_pages`` lists the pages the sidebar links to (SB5: underscore pages, non-markdown
files and files git ignores are not pages; a deleted page is gone) and ``plan_action`` says
what the next apply would do with ``_Sidebar.md`` from the clone alone (SB14: never raises,
no git command, a bounded read). Both run over a real ``tmp_path`` clone; git itself is the
stateful ``FakeWikiGit`` model.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from tests.support.publisher_harness import PublisherHarness, build_publisher
from wikiops.providers.github_wiki import sidebar_io
from wikiops.providers.github_wiki.errors import GithubWikiError
from wikiops.providers.github_wiki.layout import SIDEBAR_PAGE
from wikiops.providers.github_wiki.sidebar import MARKER

BOM = chr(0xFEFF)


def scene(tmp_path: Path, files: dict[str, str | bytes] | None = None, **model: Any) -> PublisherHarness:
    """A clone (``.git`` present, answered by ``FakeWikiGit``) holding ``files``."""
    harness = build_publisher(tmp_path, **model)
    for name, content in (files or {}).items():
        data = content.encode() if isinstance(content, str) else content
        (harness.workdir / name).write_bytes(data)
    return harness


# -- root_pages ------------------------------------------------------------------------------------------


def test_root_pages_lists_the_markdown_files_and_excludes_underscore_pages(tmp_path: Path) -> None:
    harness = scene(
        tmp_path,
        {
            "Home.md": "# Home\n",
            "Setup.md": "# Setup\n",
            "_Footer.md": "footer\n",
            SIDEBAR_PAGE: MARKER + "\n",
            "notes.txt": "not a page\n",
            "image.png": b"\x89PNG",
        },
    )

    assert sidebar_io.root_pages(harness.git) == ("Home", "Setup")


def test_root_pages_is_sorted_by_exact_name_so_the_result_is_deterministic(tmp_path: Path) -> None:
    harness = scene(tmp_path, {"beta.md": "b", "Alpha.md": "a", "Home.md": "h", "zeta.md": "z"})

    assert sidebar_io.root_pages(harness.git) == ("Alpha", "Home", "beta", "zeta")


def test_root_pages_matches_the_markdown_suffix_case_insensitively(tmp_path: Path) -> None:
    harness = scene(tmp_path, {"Upper.MD": "u", "Mixed.Md": "m", "Plain.md": "p", "Home.markdown": "x"})

    assert sidebar_io.root_pages(harness.git) == ("Mixed", "Plain", "Upper")


def test_root_pages_drops_a_directory_named_like_a_page(tmp_path: Path) -> None:
    harness = scene(tmp_path, {"Home.md": "h"})
    (harness.workdir / "x.md").mkdir()
    (harness.workdir / "x.md" / "inner.md").write_bytes(b"inside")

    assert sidebar_io.root_pages(harness.git) == ("Home",)


def test_root_pages_drops_symlinks_even_to_a_page_inside_the_clone(tmp_path: Path) -> None:
    harness = scene(tmp_path, {"Home.md": "h"})
    (harness.workdir / "alias.md").symlink_to("Home.md")
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"outside")
    (harness.workdir / "escape.md").symlink_to(outside)
    (harness.workdir / "dangling.md").symlink_to("missing.md")

    assert sidebar_io.root_pages(harness.git) == ("Home",)


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("bad" + chr(1) + "name.md", id="control-char"),
        pytest.param("tab" + chr(9) + "name.md", id="tab"),
        pytest.param(" leading.md", id="leading-space"),
        pytest.param("trailing .md", id="trailing-space"),
        pytest.param("del" + chr(0x7F) + "name.md", id="delete-char"),
        pytest.param(".md", id="blank-stem"),
    ],
)
def test_root_pages_drops_names_the_page_policy_rejects(tmp_path: Path, name: str) -> None:
    harness = scene(tmp_path, {"Home.md": "h"})
    (harness.workdir / name).write_bytes(b"x")

    assert sidebar_io.root_pages(harness.git) == ("Home",)


def test_root_pages_keeps_unusual_but_valid_names(tmp_path: Path) -> None:
    harness = scene(tmp_path, {"Getting-Started.md": "g", "Spaces in name.md": "s", "caf" + chr(0xE9) + ".md": "c"})

    assert sidebar_io.root_pages(harness.git) == ("Getting-Started", "Spaces in name", "caf" + chr(0xE9))


def test_root_pages_drops_untracked_files_git_ignores_and_keeps_tracked_ones(tmp_path: Path) -> None:
    harness = scene(
        tmp_path,
        {"Home.md": "h", "Secret.md": "s", "Tracked.md": "t"},
        ignored={"Secret.md", "Tracked.md"},
        committed_files={"Tracked.md": b"t"},
    )

    pages = sidebar_io.root_pages(harness.git)

    assert pages == ("Home", "Tracked")
    ignore_calls = [call for call in harness.runner.calls if "check-ignore" in call.argv]
    assert len(ignore_calls) == 1
    assert ignore_calls[0].stdin == "Home.md\0Secret.md\0Tracked.md\0"


def test_root_pages_asks_git_about_the_page_names_only_never_about_excluded_files(tmp_path: Path) -> None:
    harness = scene(tmp_path, {"Home.md": "h", "_Footer.md": "f", "notes.txt": "n"})

    sidebar_io.root_pages(harness.git)

    ignore_calls = [call for call in harness.runner.calls if "check-ignore" in call.argv]
    assert [call.stdin for call in ignore_calls] == ["Home.md\0"]


def test_root_pages_runs_no_git_command_when_there_is_no_candidate(tmp_path: Path) -> None:
    harness = scene(tmp_path, {"_Footer.md": "f", "notes.txt": "n"})

    assert sidebar_io.root_pages(harness.git) == ()
    assert harness.runner.calls == []


class _FakeEntry:
    """What ``os.scandir`` yields, for a name that exists once per exact spelling."""

    def __init__(self, name: str) -> None:
        self.name = name

    def is_file(self, *, follow_symlinks: bool = True) -> bool:
        assert follow_symlinks is False, "a symlink must never be taken for a page"
        return True


class _FakeScan:
    def __init__(self, names: list[str]) -> None:
        self._entries = [_FakeEntry(name) for name in names]

    def __enter__(self) -> list[_FakeEntry]:
        return self._entries

    def __exit__(self, *exc: object) -> None:
        return None


def test_root_pages_keeps_the_first_exact_name_of_a_duplicate_stem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``A.md`` and ``A.MD`` can only coexist on a case-sensitive file system: model the scan."""
    harness = scene(tmp_path)
    monkeypatch.setattr(sidebar_io.os, "scandir", lambda path: _FakeScan(["Z.md", "A.md", "A.MD", "b.md"]))

    pages = sidebar_io.root_pages(harness.git)

    assert pages == ("A", "Z", "b")
    ignore_calls = [call for call in harness.runner.calls if "check-ignore" in call.argv]
    assert [call.stdin for call in ignore_calls] == ["A.MD\0Z.md\0b.md\0"]


def test_root_pages_drops_a_page_deleted_since_the_last_listing(tmp_path: Path) -> None:
    harness = scene(tmp_path, {"Home.md": "h", "Old.md": "o"})
    assert sidebar_io.root_pages(harness.git) == ("Home", "Old")

    (harness.workdir / "Old.md").unlink()

    assert sidebar_io.root_pages(harness.git) == ("Home",)


def test_root_pages_lets_a_git_failure_through_for_the_writer_to_report(tmp_path: Path) -> None:
    harness = scene(tmp_path, {"Home.md": "h"}, fail={"check-ignore": (128, "fatal: not a git repository")})

    with pytest.raises(GithubWikiError):
        sidebar_io.root_pages(harness.git)


# -- plan_action -----------------------------------------------------------------------------------------


def test_plan_action_is_create_without_a_clone_and_runs_no_git_command(tmp_path: Path) -> None:
    harness = scene(tmp_path, {SIDEBAR_PAGE: "hand written, but there is no clone yet\n"})
    shutil.rmtree(harness.workdir / ".git")

    assert sidebar_io.plan_action(harness.workdir) == "create"
    assert harness.runner.calls == []


def test_plan_action_is_create_when_the_workdir_does_not_exist(tmp_path: Path) -> None:
    assert sidebar_io.plan_action(tmp_path / "missing") == "create"


def test_plan_action_is_create_when_the_sidebar_is_absent(tmp_path: Path) -> None:
    harness = scene(tmp_path, {"Home.md": "h"})

    assert sidebar_io.plan_action(harness.workdir) == "create"
    assert harness.runner.calls == []


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(MARKER + "\n- [Home](Home)\n", id="marker-and-body"),
        pytest.param(MARKER, id="marker-only-no-newline"),
        pytest.param(MARKER + "\r\nbody\r\n", id="crlf-first-line"),
        pytest.param(MARKER + "\n" + "caf" + chr(0xE9) + "\n", id="multibyte-text-after-the-marker"),
        pytest.param(MARKER + "\n" + chr(0x20AC) * 4, id="multibyte-char-straddling-the-read-limit"),
    ],
)
def test_plan_action_is_regenerate_for_a_managed_sidebar(tmp_path: Path, content: str) -> None:
    harness = scene(tmp_path, {SIDEBAR_PAGE: content})

    assert sidebar_io.plan_action(harness.workdir) == "regenerate"
    assert harness.runner.calls == []


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("# My own sidebar\n- [Home](Home)\n", id="hand-written"),
        pytest.param(" " + MARKER + "\n", id="leading-space"),
        pytest.param(MARKER + " extra\n", id="trailing-text"),
        pytest.param(BOM + MARKER + "\n", id="bom"),
        pytest.param("\n" + MARKER + "\n", id="marker-not-on-the-first-line"),
        pytest.param(MARKER + "\r\r\n", id="two-carriage-returns"),
        pytest.param("", id="empty-file"),
    ],
)
def test_plan_action_is_skipped_unmanaged_for_an_unmarked_sidebar(tmp_path: Path, content: str) -> None:
    harness = scene(tmp_path, {SIDEBAR_PAGE: content})

    assert sidebar_io.plan_action(harness.workdir) == "skipped-unmanaged"


def test_plan_action_is_skipped_unmanaged_for_a_directory(tmp_path: Path) -> None:
    harness = scene(tmp_path)
    (harness.workdir / SIDEBAR_PAGE).mkdir()

    assert sidebar_io.plan_action(harness.workdir) == "skipped-unmanaged"


def test_plan_action_is_skipped_unmanaged_for_a_symlink_that_escapes_the_clone(tmp_path: Path) -> None:
    harness = scene(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_bytes((MARKER + "\n").encode())
    (harness.workdir / SIDEBAR_PAGE).symlink_to(outside)

    assert sidebar_io.plan_action(harness.workdir) == "skipped-unmanaged"


def test_plan_action_never_follows_a_symlink_even_one_that_stays_inside(tmp_path: Path) -> None:
    harness = scene(tmp_path, {"Real.md": MARKER + "\n"})
    (harness.workdir / SIDEBAR_PAGE).symlink_to("Real.md")

    assert sidebar_io.plan_action(harness.workdir) == "skipped-unmanaged"


def test_plan_action_is_skipped_unmanaged_for_a_dangling_symlink(tmp_path: Path) -> None:
    harness = scene(tmp_path)
    (harness.workdir / SIDEBAR_PAGE).symlink_to("missing.md")

    assert sidebar_io.plan_action(harness.workdir) == "skipped-unmanaged"


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"\xff\xfe\x00 not text at all", id="invalid-start-byte"),
        pytest.param(MARKER.encode() + b"\xff\n", id="invalid-byte-on-the-first-line"),
        pytest.param(b"\xc3\x28" + MARKER.encode() + b"\n", id="invalid-continuation"),
    ],
)
def test_plan_action_is_skipped_unmanaged_for_undecodable_bytes(tmp_path: Path, data: bytes) -> None:
    harness = scene(tmp_path, {SIDEBAR_PAGE: data})

    assert sidebar_io.plan_action(harness.workdir) == "skipped-unmanaged"


def test_plan_action_reads_only_a_bounded_prefix_of_a_large_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = scene(tmp_path, {SIDEBAR_PAGE: MARKER + "\n" + "x" * 5_000_000})
    read: list[int] = []
    real_open = Path.open

    class Spy:
        def __init__(self, handle: Any) -> None:
            self._handle = handle

        def __enter__(self) -> Spy:
            self._handle.__enter__()
            return self

        def __exit__(self, *exc: object) -> None:
            self._handle.__exit__(*exc)

        def read(self, size: int = -1) -> bytes:
            data = self._handle.read(size)
            read.append(len(data))
            return data

    monkeypatch.setattr(Path, "open", lambda self, *a, **k: Spy(real_open(self, *a, **k)))

    assert sidebar_io.plan_action(harness.workdir) == "regenerate"
    assert read and sum(read) == len(MARKER.encode()) + 2


def test_plan_action_does_not_decode_beyond_the_first_line(tmp_path: Path) -> None:
    """Bytes far past the marker line are never looked at, so they cannot make the file unreadable."""
    harness = scene(tmp_path, {SIDEBAR_PAGE: MARKER.encode() + b"\n" + b"\xff" * 4096})

    assert sidebar_io.plan_action(harness.workdir) == "regenerate"


def test_plan_action_never_raises_when_the_file_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = scene(tmp_path, {SIDEBAR_PAGE: MARKER + "\n"})

    def refuse(self: Path, *args: Any, **kwargs: Any) -> None:
        raise PermissionError("permission denied")

    monkeypatch.setattr(Path, "open", refuse)

    assert sidebar_io.plan_action(harness.workdir) == "skipped-unmanaged"


def test_plan_action_never_raises_when_the_file_cannot_be_inspected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = scene(tmp_path, {SIDEBAR_PAGE: MARKER + "\n"})
    real_lstat = sidebar_io.os.lstat

    def refuse(path: Any, *args: Any, **kwargs: Any) -> Any:
        if Path(path).name == SIDEBAR_PAGE:
            raise PermissionError("permission denied")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(sidebar_io.os, "lstat", refuse)

    assert sidebar_io.plan_action(harness.workdir) == "skipped-unmanaged"
