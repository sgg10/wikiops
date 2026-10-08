"""Page, reference, asset and link policy of the ``github_wiki`` provider.

Pure functions only (no filesystem, process or network access). A GitHub wiki
is a flat namespace of root-level Markdown pages, so this module decides which
page references the provider accepts, derives root references from titles,
builds document-relative asset references and wiki page URLs, and guards every
link wikiops emits against forms that break on private wikis (root-anchored
paths and raw-content URLs).

Plugin-authored page content is never scanned or rewritten here: no function
in this module receives page content.
"""

from __future__ import annotations

import posixpath
import re
from typing import NamedTuple
from urllib.parse import quote

from wikiops_sdk.domain import AssetRef, AssetRefKind, DocumentRef, RefKind

from wikiops.providers._fs import FsError, validate_relative_path
from wikiops.providers.github_wiki.errors import GithubWikiError
from wikiops.providers.github_wiki.settings import (
    CodedValueError,
    check_host,
    check_repository,
)
from wikiops.providers.github_wiki.text import CONTROL_CHARACTERS, has_control_characters

_MARKDOWN_SUFFIX = ".md"
_FOREIGN_MARKDOWN_SUFFIXES = frozenset({".markdown", ".mdx", ".txt"})
_RESERVED_SEGMENT = ".git"
_SEPARATORS = re.compile(r"[/\\]")
_WHITESPACE_RUN = re.compile(r"\s+")
_TITLE_FORBIDDEN_CHARS = frozenset('/\\:*?"<>|')
_RAW_CONTENT_URL = re.compile(
    r"https?://raw\.githubusercontent\.com(?:[:/?#]|$)", re.IGNORECASE
)


# ---------------------------------------------------------------------------
# Page references
# ---------------------------------------------------------------------------


def _suggest_markdown_name(name: str) -> str:
    """Return ``name`` with a ``.md`` suffix (a foreign Markdown extension is replaced)."""
    stem, extension = posixpath.splitext(name)
    if extension.lower() in _FOREIGN_MARKDOWN_SUFFIXES:
        return f"{stem}{_MARKDOWN_SUFFIX}"
    return f"{name}{_MARKDOWN_SUFFIX}"


def _flattened_name(path: str) -> str:
    """Return ``path`` as a flat ``.md`` file name: separators become ``-``.

    Empty, ``.`` and ``..`` segments are dropped. Returns an empty string when
    nothing usable remains.
    """
    parts = [part for part in _SEPARATORS.split(path) if part not in ("", ".", "..")]
    if not parts:
        return ""
    flat = "-".join(parts)
    return flat if flat.lower().endswith(_MARKDOWN_SUFFIX) else _suggest_markdown_name(flat)


def _nested_error(path: str) -> GithubWikiError:
    flat = _flattened_name(path)
    hint = (
        f"use the flat page name '{flat}' (path separators replaced by '-'); "
        "wikiops never flattens names automatically"
        if flat
        else "use a flat page name; replace path separators with '-'"
    )
    return GithubWikiError(
        "path.nested_not_supported",
        "Wiki pages live at the wiki root; page paths cannot contain separators",
        context={"path": path},
        hint=hint,
    )


class _NameProblem(NamedTuple):
    """Why a flat page name is unusable and the action that fixes it."""

    reason: str
    fix: str


_NAME_EXAMPLE = "'Home.md'"


def _unsafe_name_problem(path: str, *, has_suffix: bool) -> _NameProblem | None:
    """Return why the flat page name ``path`` is unsafe, or ``None`` when it is fine.

    Control characters (including NUL) and leading or trailing whitespace are
    rejected: they cannot be told apart on screen, trip git and file systems
    (Windows silently trims them) and would make two names look identical.
    Whitespace right before the ``.md`` suffix counts as trailing whitespace of
    the stem.
    """
    if has_control_characters(path):
        return _NameProblem("it contains control characters", "remove the control characters")
    stem = path[: -len(_MARKDOWN_SUFFIX)] if has_suffix else path
    if path != path.strip() or stem != stem.strip():
        return _NameProblem(
            "it starts or ends with whitespace", "remove the leading and trailing whitespace"
        )
    return None


def _corrected_name(path: str, *, has_suffix: bool) -> str:
    """Return a valid page name derived from the unsafe ``path``, or ``""`` when none exists.

    Runs of control characters become one ``-``, whitespace around the stem is
    dropped (inner spaces stay) and ``.md`` is appended unless present. Nothing
    is returned when no visible character is left before ``.md``.
    """
    stem = path[: -len(_MARKDOWN_SUFFIX)] if has_suffix else path
    pieces = (piece.strip() for piece in CONTROL_CHARACTERS.split(stem))
    name = "-".join(piece for piece in pieces if piece)
    if not name:
        return ""
    if has_suffix:
        name = f"{name}{_MARKDOWN_SUFFIX}"
    elif not name.lower().endswith(_MARKDOWN_SUFFIX):
        name = _suggest_markdown_name(name)
    return name if name[: -len(_MARKDOWN_SUFFIX)].strip() else ""


def _invalid_name_error(summary: str, *, path: str, hint: str) -> GithubWikiError:
    return GithubWikiError("path.invalid_name", summary, context={"path": path}, hint=hint)


def validate_page_ref(ref: DocumentRef) -> str:
    """Return the root-level ``.md`` page name of ``ref`` or raise a coded error.

    Checks run in a fixed order and the first failure wins: reference kind
    (``ref.unsupported_kind``), presence of a path (``ref.missing_path``), a
    ``.git`` component (``path.reserved``), any path separator
    (``path.nested_not_supported``), a blank stem before ``.md``
    (``path.invalid_name``), an unsafe name (``path.invalid_name``) and the
    ``.md`` suffix, case-insensitive (``path.not_markdown``). The returned name
    is the path unchanged.

    Code choices: ``ref.missing_path`` is only for a reference that carries no
    path at all (absent or blank). A name that exists but cannot be used (a
    stem that is empty or whitespace-only, control characters, or surrounding
    whitespace) is ``path.invalid_name``; its Hint states the exact problem and
    suggests a corrected name when one exists. ``path.reserved`` stays for the
    ``.git`` component.
    """
    if ref.kind is not RefKind.PATH:
        raise GithubWikiError(
            "ref.unsupported_kind",
            "github_wiki pages are addressed by path references",
            context={"kind": ref.kind.value},
        )
    path = ref.locator.get("path")
    if path is None or not path.strip():
        raise GithubWikiError(
            "ref.missing_path",
            "The page reference has no path",
            hint="set locator.path to a flat page name such as 'Home.md'",
        )
    segments = _SEPARATORS.split(path)
    if any(segment.casefold() == _RESERVED_SEGMENT for segment in segments):
        raise GithubWikiError(
            "path.reserved",
            "The path contains a reserved '.git' component",
            context={"path": path},
        )
    if len(segments) > 1:
        raise _nested_error(path)
    has_suffix = path.lower().endswith(_MARKDOWN_SUFFIX)
    if has_suffix and not path[: -len(_MARKDOWN_SUFFIX)].strip():
        raise _invalid_name_error(
            "The page name is blank: there is no visible character before '.md'",
            path=path,
            hint=(
                "use a page name with at least one visible character before '.md', "
                f"such as {_NAME_EXAMPLE}"
            ),
        )
    problem = _unsafe_name_problem(path, has_suffix=has_suffix)
    if problem is not None:
        corrected = _corrected_name(path, has_suffix=has_suffix)
        raise _invalid_name_error(
            f"The page name is not allowed: {problem.reason}",
            path=path,
            hint=(
                f"{problem.fix} and use '{corrected}'"
                if corrected
                else (
                    f"{problem.fix} and use a page name with at least one visible "
                    f"character, such as {_NAME_EXAMPLE}"
                )
            ),
        )
    if not has_suffix:
        raise GithubWikiError(
            "path.not_markdown",
            "Wiki page paths must end in '.md'",
            context={"path": path},
            hint=f"use '{_suggest_markdown_name(path)}' instead",
        )
    return path


def _title_problem(title: str) -> str | None:
    """Return why ``title`` is unusable as a page name, or ``None`` when it is fine."""
    if not title:
        return "it is empty"
    if title.startswith("."):
        return "it starts with '.'"
    if has_control_characters(title):
        return "it contains a control character"
    forbidden = sorted(_TITLE_FORBIDDEN_CHARS.intersection(title))
    if forbidden:
        return f"it contains the character '{forbidden[0]}'"
    return None


def derive_root_ref(title: str, *, provider: str) -> DocumentRef:
    """Return the root page reference derived from ``title`` (ref-less creates).

    The title is trimmed, every whitespace run becomes one ``-``, case and
    Unicode are preserved and ``.md`` is appended unless already present
    (case-insensitive). The result is always a flat root page: titles that
    would need a separator, start with ``.`` or hold control or reserved
    characters raise ``title.invalid`` instead of nesting.
    """
    name = _WHITESPACE_RUN.sub("-", title.strip())
    problem = _title_problem(name)
    if problem is not None:
        raise GithubWikiError(
            "title.invalid",
            f"Title cannot be used as a wiki page name ({problem})",
            context={"title": title},
        )
    if not name.lower().endswith(_MARKDOWN_SUFFIX):
        name = f"{name}{_MARKDOWN_SUFFIX}"
    return DocumentRef(
        provider=provider,
        kind=RefKind.PATH,
        locator={"path": name},
        title_hint=title,
    )


# ---------------------------------------------------------------------------
# Links and asset references
# ---------------------------------------------------------------------------


def guard_link(link: str) -> str:
    """Return ``link`` unchanged if it is a form that works on private wikis.

    A link starting with ``/`` raises ``link.root_anchored`` and a
    ``raw.githubusercontent.com`` URL raises ``link.raw_url``. Only links
    wikiops itself emits are passed through this guard.
    """
    candidate = link.lstrip()
    if candidate.startswith("/"):
        raise GithubWikiError(
            "link.root_anchored",
            "A generated link starts with '/' and breaks on private wikis",
            context={"link": link},
        )
    if _RAW_CONTENT_URL.match(candidate):
        raise GithubWikiError(
            "link.raw_url",
            "A generated link points at raw.githubusercontent.com and breaks on private wikis",
            context={"link": link},
        )
    return link


def build_asset_reference(ref: AssetRef) -> str:
    """Return the document-relative reference for an asset stored in the wiki.

    Only path asset references with a canonical root-relative path are
    expressible (``asset.ref_unsupported`` otherwise). The result is the
    percent-encoded path, e.g. ``assets/<hash>.png``: no leading ``/`` and no
    URL, so it resolves from every root page.
    """
    if ref.kind is not AssetRefKind.PATH:
        raise GithubWikiError(
            "asset.ref_unsupported",
            "Only path asset references can be linked from a wiki page",
            context={"kind": ref.kind.value},
        )
    path = ref.locator.get("path")
    if path is None:
        raise GithubWikiError(
            "asset.ref_unsupported",
            "The asset reference has no path",
            hint="provide locator.path with a root-relative asset path",
        )
    try:
        validate_relative_path(path)
    except FsError as exc:
        raise GithubWikiError(
            "asset.ref_unsupported",
            f"The asset path is not a canonical root-relative path ({exc.summary})",
            context={"path": path},
        ) from exc
    return guard_link(quote(path, safe="/"))


def build_link(ref: DocumentRef, *, host: str, repository: str) -> str:
    """Return the GitHub wiki URL of the page ``ref``: ``https://<host>/<owner>/<repo>/wiki/<stem>``.

    The page policy (:func:`validate_page_ref`) applies first, so only flat
    Markdown pages get a link; ``host`` and ``repository`` then pass the same
    shape rules as the settings (``config.invalid_host`` and
    ``config.invalid_repository``). The page stem is percent-encoded and the
    ``host`` is honored for GitHub Enterprise.
    """
    stem = validate_page_ref(ref)[: -len(_MARKDOWN_SUFFIX)]
    try:
        check_host(host)
        check_repository(repository)
    except CodedValueError as exc:
        raise GithubWikiError(
            exc.code, exc.summary, context=exc.context, hint=exc.hint
        ) from exc
    return guard_link(f"https://{host}/{repository}/wiki/{quote(stem, safe='')}")
