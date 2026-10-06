"""``local_files`` layout policy: which document paths the provider accepts.

Pure functions only (no filesystem access). Filesystem safety lives in the
provider-agnostic ``wikiops.providers._fs`` module; this module holds the rules
that belong to the ``local_files`` provider itself.
"""

from __future__ import annotations

import posixpath

from wikiops.providers._fs import FsError

_MARKDOWN_SUFFIX = ".md"
_FOREIGN_MARKDOWN_SUFFIXES = frozenset({".markdown", ".mdx", ".txt"})


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
