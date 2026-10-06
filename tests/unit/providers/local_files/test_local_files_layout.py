"""Unit tests for the ``local_files`` layout policy (pure, no filesystem)."""

from __future__ import annotations

import pytest

from wikiops.providers._fs import FsError
from wikiops.providers.local_files._layout import require_markdown_path


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
