"""Unit tests for the ``local_files`` layout policy (pure, no filesystem)."""

from __future__ import annotations

import pytest

from wikiops.providers._fs import FsError
from wikiops.providers.local_files._layout import (
    default_document_path,
    filename_for_title,
    require_markdown_path,
)


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "docs/guide/Setup.md",
        "docs/README.MD",
        "docs/Notes.Md",
        "docs/v1.2.md",
        "docs/ñandú.md",
    ],
)
def test_require_markdown_path_returns_markdown_paths_unchanged(path: str) -> None:
    assert require_markdown_path(path) == path


@pytest.mark.parametrize(
    ("path", "suggestion"),
    [
        ("docs/README", "docs/README.md"),
        ("docs/v1.2", "docs/v1.2.md"),
        ("Notes", "Notes.md"),
        ("docs/notes.markdown", "docs/notes.md"),
        ("docs/guide.mdx", "docs/guide.md"),
        ("docs/a.txt", "docs/a.md"),
        ("docs/A.TXT", "docs/A.md"),
        ("docs/Notes.MARKDOWN", "docs/Notes.md"),
        ("pyproject.toml", "pyproject.toml.md"),
        (".github/workflows/ci.yml", ".github/workflows/ci.yml.md"),
        ("docs/archive.md.bak", "docs/archive.md.bak.md"),
    ],
)
def test_require_markdown_path_rejects_other_names_with_corrected_hint(
    path: str, suggestion: str
) -> None:
    with pytest.raises(FsError) as excinfo:
        require_markdown_path(path)

    error = excinfo.value
    assert error.code == "path.not_markdown"
    assert error.namespace is None
    assert error.path == path
    assert error.hint is not None and f"'{suggestion}'" in error.hint
    assert str(error).startswith("[path.not_markdown] ")
    assert f"path='{path}'" in str(error)
    assert "Hint:" in str(error)


def test_require_markdown_path_never_appends_an_extension_silently() -> None:
    # The corrected name is only a hint; the function must raise, not return it.
    with pytest.raises(FsError, match=r"path\.not_markdown"):
        require_markdown_path("docs/README")


# ---------------------------------------------------------------------------
# Title naming (D7) and fallback derivation (D8)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Setup", "Setup.md"),
        ("  Data   Product  ", "Data-Product.md"),
        ("Architecture Overview Ñandú", "Architecture-Overview-Ñandú.md"),
        ("Setup.md", "Setup.md"),
        ("Setup.MD", "Setup.MD"),
        ("v1.2 Notes", "v1.2-Notes.md"),
        ("a b", "a-b.md"),
        ("ALL CAPS Title", "ALL-CAPS-Title.md"),
    ],
)
def test_filename_for_title_trims_collapses_and_keeps_case_and_unicode(
    title: str, expected: str
) -> None:
    assert filename_for_title(title) == expected


@pytest.mark.parametrize(
    "title",
    [
        "",
        "   ",
        ".",
        "..",
        ".hidden",
        "a/b",
        "a\\b",
        "a\x00b",
        "a\tb",
        "a\nb",
        "a\x7fb",
        "a:b",
        "a*b",
        "a?b",
        'a"b',
        "a<b",
        "a>b",
        "a|b",
    ],
)
def test_filename_for_title_rejects_invalid_titles_with_a_hint(title: str) -> None:
    with pytest.raises(FsError) as excinfo:
        filename_for_title(title)

    error = excinfo.value
    assert error.code == "title.invalid"
    assert error.namespace is None
    assert error.hint
    assert str(error).startswith("[title.invalid] ")
    assert "Hint:" in str(error)


def test_filename_for_title_error_reports_the_offending_title() -> None:
    with pytest.raises(FsError) as excinfo:
        filename_for_title("a:b")

    assert "a:b" in excinfo.value.summary
    assert "':'" in excinfo.value.summary


@pytest.mark.parametrize(
    ("title", "parent_path", "expected"),
    [
        ("Architecture Overview", None, "Architecture-Overview.md"),
        ("Setup", "README.md", "Setup.md"),
        ("Setup", "index.md", "Setup.md"),
        ("Setup", "INDEX.md", "Setup.md"),
        ("Setup", "docs/README.md", "docs/Setup.md"),
        ("Setup", "docs/INDEX.MD", "docs/Setup.md"),
        ("Setup", "docs/guide/index.md", "docs/guide/Setup.md"),
        ("Setup", "docs/guide.md", "docs/guide/Setup.md"),
        ("Setup", "guide.md", "guide/Setup.md"),
        ("Setup", "docs/Guide.MD", "docs/Guide/Setup.md"),
        ("Architecture Overview Ñandú", "docs/guide.md", "docs/guide/Architecture-Overview-Ñandú.md"),
        ("Setup.md", "docs/guide.md", "docs/guide/Setup.md"),
        ("Setup", "docs/readme-first.md", "docs/readme-first/Setup.md"),
    ],
)
def test_default_document_path_derives_the_fallback_location(
    title: str, parent_path: str | None, expected: str
) -> None:
    assert default_document_path(title=title, parent_path=parent_path) == expected


def test_default_document_path_propagates_invalid_titles() -> None:
    with pytest.raises(FsError) as excinfo:
        default_document_path(title="a/b", parent_path="docs/guide.md")

    assert excinfo.value.code == "title.invalid"
