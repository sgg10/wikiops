"""Private, provider-agnostic filesystem safety helpers.

Root resolution, lexical path validation and symlink-aware confinement for
providers that persist documents under a directory. This module is internal
(not re-exported from ``wikiops.providers``), uses only the standard library
plus the host ``ConfigurationError``, and knows nothing about any provider:
errors are raised without a namespace and the calling provider attaches its own
through :meth:`FsError.with_namespace`.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import re
import secrets
import stat
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

    def __reduce__(self) -> tuple[Callable[..., FsError], tuple[object, ...]]:
        # Exceptions pickle through ``cls(*self.args)``, which cannot rebuild
        # this keyword-only signature; rebuild from the structured fields.
        return (
            _rebuild_fs_error,
            (self.code, self.summary, self.path, self.root, self.hint, self.namespace),
        )

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


def _rebuild_fs_error(
    code: str,
    summary: str,
    path: str | None,
    root: Path | None,
    hint: str | None,
    namespace: str | None,
) -> FsError:
    return FsError(code, summary, path=path, root=root, hint=hint, namespace=namespace)


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
    ancestor is probed explicitly. The probe stops at the filesystem anchor: a
    nonexistent drive or unreachable UNC share root is its own parent, so
    ``FileNotFoundError`` is raised instead of looping forever.
    """
    real = path.resolve(strict=False)
    probe = real
    while not os.path.lexists(probe):
        if probe.parent == probe:
            raise FileNotFoundError(
                errno.ENOENT, "No existing ancestor directory", str(real)
            )
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
    base = cwd if cwd is not None else Path.cwd()
    context = f"configured='{raw}', cwd='{base}'"
    try:
        resolved = _resolve_real(base / Path(raw.strip()).expanduser())
    except FileNotFoundError as exc:
        raise FsError(
            "settings.root_missing",
            f"Root directory does not exist ({context})",
            hint="create the directory or correct 'root'",
        ) from exc
    except (OSError, RuntimeError) as exc:
        raise FsError(
            "path.unresolvable",
            f"Root directory could not be resolved (configured='{raw}')",
            hint="fix the symlink loop or unreadable directory in 'root'",
        ) from exc
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


# ---------------------------------------------------------------------------
# Directory creation and atomic writes
# ---------------------------------------------------------------------------

_IO_HINT = "check the permissions and the free space of the target directory"


def _io_error(exc: OSError, action: str, *, path: str, root: Path) -> FsError:
    detail = exc.strerror or str(exc)
    return FsError(
        "io.error",
        f"{action} ({type(exc).__name__}: {detail})",
        path=path,
        root=root,
        hint=_IO_HINT,
    )


def make_dirs_within_root(root_real: Path, rel_dir: PurePosixPath) -> Path:
    """Create ``rel_dir`` under the root one component at a time; return its real path.

    Never uses ``parents=True``: every component, created or pre-existing, is
    re-resolved and must stay inside the root (``path.symlink_escape``) and be a
    directory (``path.parent_not_directory``). A directory that appears
    concurrently falls through to the same checks. Directories created before a
    later failure are left in place (empty, inside the root).
    """
    parts = rel_dir.parts
    if not parts:
        return root_real
    validate_relative_path(rel_dir.as_posix())
    current = root_real
    for index, part in enumerate(parts):
        candidate = current / part
        try:
            os.mkdir(candidate, 0o777)
        except FileExistsError:
            pass
        except OSError as exc:
            raise _io_error(
                exc,
                "Directory could not be created",
                path="/".join(parts[: index + 1]),
                root=root_real,
            ) from exc
        current = ensure_within_root(root_real, candidate)
        if not current.is_dir():
            raise FsError(
                "path.parent_not_directory",
                "A path segment exists but is not a directory",
                path="/".join(parts[: index + 1]),
                root=root_real,
                hint="move or remove the file, or choose a location whose parents are directories",
            )
    return current


_TEMP_SUFFIX = ".wikiops.tmp"
_NEW_FILE_FLAGS = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)


def _fsync_directory(directory: Path) -> None:
    """Flush the directory entry to disk; best effort (not every platform allows it)."""
    try:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass


def atomic_write_bytes(target_real: Path, data: bytes, *, root_real: Path) -> None:
    """Atomically write ``data`` to ``target_real`` inside the root.

    The target is confined first, its parent is created through
    :func:`make_dirs_within_root`, the bytes go to a temp file in the same
    directory and are published with ``os.replace``. Updates keep the existing
    permission bits, new files follow the umask. On any failure the original
    file is untouched and no temp file remains. The caller encodes text, so no
    newline translation happens here.
    """
    target = ensure_within_root(root_real, target_real)
    shown = _relative_display(root_real, target)
    if os.path.lexists(target) and not target.is_file():
        raise FsError(
            "path.not_a_file",
            "Target exists and is not a regular file",
            path=shown,
            root=root_real,
            hint="choose a file path, or remove the directory or special file in the way",
        )
    directory = make_dirs_within_root(
        root_real, PurePosixPath(target.parent.relative_to(root_real).as_posix())
    )
    temp = directory / f".{target.name}.{secrets.token_hex(4)}{_TEMP_SUFFIX}"
    try:
        existing_mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else None
        descriptor = os.open(temp, _NEW_FILE_FLAGS, 0o666)
        with os.fdopen(descriptor, "wb") as handle:
            if existing_mode is not None:
                os.chmod(temp, existing_mode)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        ensure_within_root(root_real, target)
        os.replace(temp, target)
        _fsync_directory(directory)
    except OSError as exc:
        raise _io_error(exc, "File could not be written", path=shown, root=root_real) from exc
    finally:
        with contextlib.suppress(OSError):
            os.unlink(temp)


# ---------------------------------------------------------------------------
# Decoding, versioning and asset naming
# ---------------------------------------------------------------------------

_ASSET_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_ASSET_HASH_LENGTH = 16
_DEFAULT_ASSET_STEM = "asset"


def decode_text(data: bytes, *, path: str, encoding: str = "utf-8") -> str:
    """Decode ``data`` strictly; no newline translation and no BOM stripping."""
    try:
        return data.decode(encoding)
    except UnicodeDecodeError as exc:
        raise FsError(
            "document.decode_error",
            f"File is not valid {encoding} text ({exc.reason} at byte {exc.start})",
            path=path,
            hint=f"re-save the file as {encoding} or remove the invalid bytes",
        ) from exc


def content_version(data: bytes) -> str:
    """Return the version token ``sha256:<hex>`` for ``data``."""
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def hashed_asset_name(filename: str | None, data: bytes) -> str:
    """Return ``<safe-stem>--<sha256[:16]><suffix>`` for an asset ``filename``.

    The suffix is preserved, stem characters outside ``[A-Za-z0-9._-]`` become
    ``-`` (runs collapsed, edges stripped, empty stem -> ``asset``). The name
    depends only on the filename and the bytes, so it is deterministic.
    """
    if filename is None or not filename.strip():
        raise FsError(
            "asset.missing_name",
            "Asset has no file name",
            hint="give the asset a file name such as 'diagram.png'",
        )
    if "/" in filename or "\\" in filename or ".." in filename:
        raise FsError(
            "asset.invalid_name",
            "Asset name must be a plain file name without path separators or '..'",
            path=filename,
            hint="pass only the file name, e.g. 'diagram.png', not a path",
        )
    suffix = PurePosixPath(filename).suffix
    stem = filename[: len(filename) - len(suffix)]
    safe_stem = _ASSET_UNSAFE.sub("-", stem).strip("-.") or _DEFAULT_ASSET_STEM
    digest = hashlib.sha256(data).hexdigest()[:_ASSET_HASH_LENGTH]
    return f"{safe_stem}--{digest}{_ASSET_UNSAFE.sub('-', suffix)}"
