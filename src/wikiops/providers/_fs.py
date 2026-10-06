"""Private, provider-agnostic filesystem safety helpers.

Root resolution, lexical path validation and symlink-aware confinement for
providers that persist documents under a directory. This module is internal
(not re-exported from ``wikiops.providers``), uses only the standard library
plus the host ``ConfigurationError``, and knows nothing about any provider:
errors are raised without a namespace and the calling provider attaches its own
through :meth:`FsError.with_namespace`.
"""

from __future__ import annotations

import errno
import os
import re
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Union

from wikiops.core.exceptions import ConfigurationError


def _display(value: str) -> str:
    """Return ``value`` with non-printable characters escaped for messages."""
    return "".join(
        char if char.isprintable() else char.encode("unicode_escape").decode()
        for char in value
    )


class FsError(ConfigurationError):
    """Typed, actionable filesystem error with a machine-readable code.

    The message shape is
    ``[<namespace>:<code>] <summary>. path='<rel>' root='<abs>'. Hint: <action>.``
    where the namespace prefix and the ``path``/``root``/``Hint`` segments are
    omitted when not set.
    """

    def __init__(
        self,
        code: str,
        summary: str,
        *,
        path: str | None = None,
        root: Path | None = None,
        hint: str | None = None,
        namespace: str | None = None,
    ) -> None:
        self.code = code
        self.summary = summary
        self.path = path
        self.root = root
        self.hint = hint
        self.namespace = namespace
        super().__init__(self._render())

    def _render(self) -> str:
        tag = f"{self.namespace}:{self.code}" if self.namespace else self.code
        parts = [f"[{tag}] {self.summary.rstrip('.')}."]
        context = []
        if self.path is not None:
            context.append(f"path='{_display(self.path)}'")
        if self.root is not None:
            context.append(f"root='{self.root}'")
        if context:
            parts.append(" ".join(context) + ".")
        if self.hint is not None:
            parts.append(f"Hint: {self.hint.rstrip('.')}.")
        return " ".join(parts)

    def with_namespace(self, namespace: str) -> FsError:
        """Return a copy of this error carrying ``namespace``; ``self`` is unchanged."""
        return FsError(
            self.code,
            self.summary,
            path=self.path,
            root=self.root,
            hint=self.hint,
            namespace=namespace,
        )


# ---------------------------------------------------------------------------
# Lexical validation of root-relative paths
# ---------------------------------------------------------------------------

_Text = Union[str, Callable[[str], str]]

_EXAMPLE_PATH = "docs/guide/a.md"
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:[/\\]")
_RESERVED_CHARS = frozenset(':*?"<>|')
_RESERVED_SEGMENT = ".git"


def _canonical(value: str) -> str:
    return "/".join(part for part in value.split("/") if part not in ("", "."))


def _invalid_char_reason(value: str) -> str | None:
    if "\\" in value:
        return "a backslash"
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return "a control character"
    reserved = sorted(_RESERVED_CHARS.intersection(value))
    return f"the reserved character '{reserved[0]}'" if reserved else None


def _has_segment(value: str, predicate: Callable[[str], bool]) -> bool:
    return any(predicate(part) for part in value.split("/"))


# (code, predicate, summary, hint); evaluated in order, first match wins. A
# summary/hint is either text or a callable receiving the offending value.
_PATH_RULES: tuple[tuple[str, Callable[[str], bool], _Text, _Text], ...] = (
    (
        "path.absolute",
        lambda v: v.startswith("/") or bool(_DRIVE_PREFIX.match(v)),
        "Path is absolute",
        f"paths are relative to the root; use e.g. '{_EXAMPLE_PATH}'",
    ),
    (
        "path.empty",
        lambda v: not v.strip() or not _canonical(v),
        "Path is empty",
        f"pass a path relative to the root, e.g. '{_EXAMPLE_PATH}'",
    ),
    (
        "path.invalid_chars",
        lambda v: _invalid_char_reason(v) is not None,
        lambda v: f"Path contains {_invalid_char_reason(v)}",
        "use '/' as separator and avoid control characters and : * ? \" < > |",
    ),
    (
        "path.traversal",
        lambda v: _has_segment(v, lambda part: part == ".."),
        "Path contains a '..' segment",
        "remove '..' segments; paths cannot leave the root",
    ),
    (
        "path.non_canonical",
        lambda v: _canonical(v) != v,
        "Path is not in canonical form",
        lambda v: f"use the canonical form '{_canonical(v)}'",
    ),
    (
        "path.reserved",
        lambda v: _has_segment(v, lambda part: part.casefold() == _RESERVED_SEGMENT),
        "Path contains a reserved '.git' segment",
        "'.git' directories are not accessible; choose another location",
    ),
)


def _resolve_text(text: _Text, value: str) -> str:
    return text(value) if callable(text) else text


def validate_relative_path(relative: str) -> PurePosixPath:
    """Return ``relative`` as a ``PurePosixPath`` if it is a canonical root-relative path.

    The value is never normalized: non-canonical input is rejected with the
    canonical form in the hint so callers keep exact control of what is written.
    """
    for code, matches, summary, hint in _PATH_RULES:
        if matches(relative):
            raise FsError(
                code,
                _resolve_text(summary, relative),
                path=relative,
                hint=_resolve_text(hint, relative),
            )
    return PurePosixPath(relative)


# ---------------------------------------------------------------------------
# Root resolution
# ---------------------------------------------------------------------------


def _resolve_real(path: Path) -> Path:
    """Resolve ``path`` through every existing symlink; missing tails are kept.

    Raises ``RuntimeError``/``OSError`` for symlink loops on every supported
    interpreter: before Python 3.13 ``Path.resolve`` raises on loops itself,
    afterwards it silently returns the unresolved tail, so the nearest existing
    ancestor is probed explicitly.
    """
    real = path.resolve(strict=False)
    probe = real
    while not os.path.lexists(probe):
        probe = probe.parent
    try:
        os.stat(probe)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise
    return real


def resolve_root(raw: str, *, cwd: Path | None = None) -> Path:
    """Return the fully resolved, existing root directory for ``raw``.

    A leading ``~`` is expanded and relative values resolve against ``cwd``
    (the process working directory by default). Empty values are rejected and
    never fall back to the working directory.
    """
    if not raw.strip():
        raise FsError(
            "settings.root_missing",
            "Root directory is not configured (empty value)",
            hint="set 'root' to an existing directory",
        )
    try:
        base = cwd if cwd is not None else Path.cwd()
        resolved = _resolve_real(base / Path(raw.strip()).expanduser())
    except (OSError, RuntimeError) as exc:
        raise FsError(
            "path.unresolvable",
            f"Root directory could not be resolved (configured='{raw}')",
            hint="fix the symlink loop or unreadable directory in 'root'",
        ) from exc
    context = f"configured='{raw}', cwd='{base}'"
    if not resolved.exists():
        raise FsError(
            "settings.root_missing",
            f"Root directory does not exist ({context})",
            root=resolved,
            hint="create the directory or correct 'root'",
        )
    if not resolved.is_dir():
        raise FsError(
            "settings.root_not_directory",
            f"Root path is not a directory ({context})",
            root=resolved,
            hint="point 'root' to a directory, not a file",
        )
    return resolved


# ---------------------------------------------------------------------------
# Confined resolution
# ---------------------------------------------------------------------------


def _relative_display(root_real: Path, candidate: Path) -> str:
    try:
        return candidate.relative_to(root_real).as_posix()
    except ValueError:
        return str(candidate)


def ensure_within_root(root_real: Path, candidate: Path) -> Path:
    """Re-resolve ``candidate`` and return its real path if it stays inside the root.

    Every existing symlink in the chain is followed, including a dangling final
    link. Call it again right before a write (and for each directory created or
    traversed) so a swap after the first resolution is still caught.
    """
    shown = _relative_display(root_real, candidate)
    try:
        real = _resolve_real(candidate)
    except (OSError, RuntimeError) as exc:
        raise FsError(
            "path.unresolvable",
            "Path could not be resolved (symlink loop or unreadable component)",
            path=shown,
            root=root_real,
            hint="remove the symlink loop or fix the permissions of the path",
        ) from exc
    if real == root_real:
        raise FsError(
            "path.unresolvable",
            "Path resolves to the root directory itself",
            path=shown,
            root=root_real,
            hint="choose a path that points inside the root, not the root itself",
        )
    if not real.is_relative_to(root_real):
        raise FsError(
            "path.symlink_escape",
            f"Path resolves outside the root (real target='{real}')",
            path=shown,
            root=root_real,
            hint="symlinks are only allowed when they point inside the root; "
            "remove the link or retarget it inside the root",
        )
    return real


def resolve_within_root(root_real: Path, relative: str) -> Path:
    """Validate ``relative`` (FS-2) and return its real absolute path inside the root."""
    return ensure_within_root(root_real, root_real.joinpath(*validate_relative_path(relative).parts))
