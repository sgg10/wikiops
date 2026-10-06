"""``local_files`` layout policy: which document paths the provider accepts.

Pure functions only (no filesystem access). Filesystem safety lives in the
provider-agnostic ``wikiops.providers._fs`` module; this module holds the rules
that belong to the ``local_files`` provider itself.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable
from urllib.parse import quote, unquote

from wikiops.providers._fs import FsError

_MARKDOWN_SUFFIX = ".md"
_FOREIGN_MARKDOWN_SUFFIXES = frozenset({".markdown", ".mdx", ".txt"})
_INDEX_STEMS = frozenset({"readme", "index"})
_TITLE_FORBIDDEN_CHARS = frozenset('/\\:*?"<>|')
_WHITESPACE_RUN = re.compile(r"\s+")
_TITLE_HINT = (
    "use a plain title without '/', '\\', control characters or : * ? \" < > |, "
    "that does not start with '.'; or set an explicit ref on the operation"
)


def _suggest_markdown_name(relative: str) -> str:
    """Return ``relative`` with a ``.md`` suffix.

    The final component gets ``.md`` appended, except that a final extension of
    ``.markdown``, ``.mdx`` or ``.txt`` (any case) is replaced by ``.md``.
    """
    stem, extension = posixpath.splitext(relative)
    if extension.lower() in _FOREIGN_MARKDOWN_SUFFIXES:
        return f"{stem}{_MARKDOWN_SUFFIX}"
    return f"{relative}{_MARKDOWN_SUFFIX}"


def require_markdown_path(relative: str) -> str:
    """Return ``relative`` unchanged when it names a Markdown file.

    Only a ``.md`` suffix (case-insensitive, casing preserved) is accepted.
    Anything else raises an un-namespaced ``FsError("path.not_markdown")`` whose
    hint carries a corrected name; the name is never applied automatically.
    """
    if relative.lower().endswith(_MARKDOWN_SUFFIX):
        return relative
    raise FsError(
        "path.not_markdown",
        "Document path must end in '.md'",
        path=relative,
        hint=f"use '{_suggest_markdown_name(relative)}' instead",
    )


def _title_error(title: str, reason: str) -> FsError:
    return FsError(
        "title.invalid",
        f"Title {title!r} cannot be used as a file name ({reason})",
        hint=_TITLE_HINT,
    )


def _title_problem(title: str) -> str | None:
    """Return why ``title`` is unusable as a file name, or ``None`` when it is fine."""
    if not title:
        return "it is empty"
    if title in (".", ".."):
        return f"'{title}' is a reserved name"
    if title.startswith("."):
        return "it starts with '.'"
    if any(ord(char) < 32 or ord(char) == 127 for char in title):
        return "it contains a control character"
    forbidden = sorted(_TITLE_FORBIDDEN_CHARS.intersection(title))
    if forbidden:
        return f"it contains the character '{forbidden[0]}'"
    return None


def filename_for_title(title: str) -> str:
    """Return the file name derived from ``title`` (D7).

    The title is trimmed, every whitespace run becomes a single ``-``, case and
    Unicode are preserved and ``.md`` is appended unless the result already ends
    in ``.md`` (case-insensitive). Unusable titles raise an un-namespaced
    ``FsError("title.invalid")``.
    """
    trimmed = title.strip()
    problem = _title_problem(trimmed)
    if problem is not None:
        raise _title_error(title, problem)
    name = _WHITESPACE_RUN.sub("-", trimmed)
    if name.lower().endswith(_MARKDOWN_SUFFIX):
        return name
    return f"{name}{_MARKDOWN_SUFFIX}"


def default_document_path(*, title: str, parent_path: str | None) -> str:
    """Return the root-relative path for a document whose ``ref`` was omitted.

    Fallback only: an explicit ``ref`` always wins and is written verbatim.
    Without a parent the file sits at the root. A ``README.md`` / ``index.md``
    parent (case-insensitive) yields a sibling; any other parent yields
    ``<parent-dir>/<parent-stem>/<file>``. Pure function, no filesystem access.
    """
    filename = filename_for_title(title)
    if parent_path is None:
        return filename
    parent_dir, parent_name = posixpath.split(parent_path)
    parent_stem = posixpath.splitext(parent_name)[0]
    if parent_stem.lower() in _INDEX_STEMS:
        return posixpath.join(parent_dir, filename)
    return posixpath.join(parent_dir, parent_stem, filename)


# Positions after an issued reference that end it: the destination closes, a
# title or attribute follows, or a fragment / query string starts.
_MARKDOWN_DESTINATION_END = r"(?=[)\s#?])"
_HTML_VALUE_END = r"(?=[\"'#?])"


def _relative_link(issued: str, target_rel: str) -> str:
    """Return the document-relative, percent-encoded link for an issued reference."""
    asset_rel = unquote(issued.removeprefix("/"))
    start = posixpath.dirname(target_rel) or "."
    return quote(posixpath.relpath(asset_rel, start), safe="/")


def relativize_issued_links(
    content: str, issued: Iterable[str], target_rel: str
) -> str:
    """Rewrite the asset references this provider issued into relative links.

    ``issued`` holds root-anchored references such as ``/assets/x--h.png``; each
    one is replaced, in Markdown link destinations and in quoted HTML ``src`` /
    ``href`` values, by the link from the directory of ``target_rel`` to the
    asset. A following ``#fragment`` or ``?query`` is preserved. Anything that
    was not issued, including user-authored ``/assets/...`` links, is left
    untouched. Pure function, no filesystem access.
    """
    for reference in set(issued):
        link = _relative_link(reference, target_rel)
        escaped = re.escape(reference)
        markdown = re.compile(rf"(?P<prefix>\]\(){escaped}{_MARKDOWN_DESTINATION_END}")
        html = re.compile(
            rf"(?P<prefix>\b(?:src|href)\s*=\s*[\"']){escaped}{_HTML_VALUE_END}",
            re.IGNORECASE,
        )
        content = markdown.sub(lambda match: f"{match.group('prefix')}{link}", content)
        content = html.sub(lambda match: f"{match.group('prefix')}{link}", content)
    return content
