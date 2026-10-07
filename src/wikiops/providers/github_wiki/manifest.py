"""The pending manifest: which dirty paths in the clone are wikiops' own.

wikiops records every path it wrote but has not committed, with the hash of the
bytes it wrote, in ``<git dir>/wikiops/pending.json`` (inside ``.git``: never
versioned, never part of the tree). The manifest is kept in BOTH
``allow_auto_commit`` modes. Before any write the dirty paths reported by
``git status`` are classified:

* PENDING (own): in the manifest AND the current bytes hash to the recorded
  value. They may be written over and ride the next commit.
* FOREIGN: every other dirty path, including a manifest path that was edited,
  deleted or replaced after wikiops wrote it. Any foreign path refuses the run
  with ``workdir.dirty``.

The schema is ``{"version": 1, "paths": {"<path>": "sha256:<hex>"}}``. A manifest
that cannot be read or fails validation is ``workdir.manifest_corrupt``; it is
never repaired or guessed at. Every operation reads the file fresh and writes it
atomically, so the instance holds no state and callers serialize through the
workdir lock.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from wikiops.providers._fs import FsError, atomic_write_bytes, content_version, ensure_within_root
from wikiops.providers.github_wiki.errors import GithubWikiError
from wikiops.providers.github_wiki.text import has_control_characters
from wikiops.providers.github_wiki.workdir import ensure_state_directory

MANIFEST_VERSION = 1
MAX_REPORTED_PATHS = 20
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_DRIVE_PREFIX = re.compile(r"[A-Za-z]:")


# -- git status ---------------------------------------------------------------


def _malformed_status(entry: str) -> NoReturn:
    raise GithubWikiError(
        "sync.git_failed",
        "git status output could not be parsed",
        context={"op": "status", "entry": entry[:60]},
        hint="re-run; if it persists inspect 'git status' in the workdir",
    )


@dataclass(frozen=True)
class StatusEntry:
    """One path of ``git status --porcelain=v1 -z``, with its two-letter ``XY`` status.

    The origin of a rename or copy is an entry of its own and carries the same status.
    """

    status: str
    path: str

    @property
    def untracked(self) -> bool:
        return self.status == "??"

    @property
    def collapsed_directory(self) -> bool:
        """A nested repository, which git reports as ``dir/`` instead of listing its files."""
        return self.path.endswith("/")


def parse_status_entries(text: str) -> tuple[StatusEntry, ...]:
    """Return every entry named by ``git status --porcelain=v1 -z`` output.

    This is the one parser of porcelain output in the provider; ``parse_status`` is its
    path-only view. Entries are ``XY <path>`` separated by NUL; a rename or copy (``R``/``C``
    in either status column) is followed by an extra NUL-separated field holding the
    origin, and both paths are returned. Paths are taken verbatim (``-z`` never quotes them).

    A trailing ``/`` marks a collapsed untracked directory (git reports a nested
    repository that way even with ``--untracked-files=all``). It is kept as given:
    a directory is never one of wikiops' page paths, so it classifies as foreign.
    Any other entry kind ending in ``/`` is not something git emits and is
    rejected as malformed.
    """
    fields = text.split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    entries: list[StatusEntry] = []
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if len(entry) < 4 or entry[2] != " ":
            _malformed_status(entry)
        status = entry[:2]
        path = entry[3:]
        if path.endswith("/") and status != "??":
            _malformed_status(entry)
        entries.append(StatusEntry(status, path))
        if "R" in status or "C" in status:
            if index >= len(fields) or not fields[index]:
                _malformed_status(entry)
            entries.append(StatusEntry(status, fields[index]))
            index += 1
    return tuple(entries)


def parse_status(text: str) -> tuple[str, ...]:
    """Return every path named by ``git status --porcelain=v1 -z`` output (see ``parse_status_entries``)."""
    return tuple(entry.path for entry in parse_status_entries(text))


# -- manifest -----------------------------------------------------------------


@dataclass(frozen=True)
class Classification:
    """Dirty paths split into wikiops' own (pending) and everything else (foreign), sorted."""

    pending: tuple[str, ...]
    foreign: tuple[str, ...]


def _is_safe_relative(path: str) -> bool:
    """A repository-relative POSIX path that stays inside the tree and avoids ``.git``."""
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or has_control_characters(path)
        or _DRIVE_PREFIX.match(path)
    ):
        return False
    return all(part not in {"", ".", ".."} and part.casefold() != ".git" for part in path.split("/"))


class PendingManifest:
    """Reads and writes the pending manifest of one clone."""

    def __init__(self, path: Path, *, workdir: Path) -> None:
        self._path = path
        self._workdir = workdir

    @property
    def path(self) -> Path:
        return self._path

    # -- persistence ----------------------------------------------------------

    def _corrupt(self, reason: str) -> GithubWikiError:
        return GithubWikiError(
            "workdir.manifest_corrupt",
            f"The pending manifest is corrupt ({reason})",
            context={"manifest": self._path, "workdir": self._workdir},
        )

    def _validated(self, document: object) -> dict[str, str]:
        if not isinstance(document, dict):
            raise self._corrupt("not a JSON object")
        version = document.get("version")
        if isinstance(version, bool) or version != MANIFEST_VERSION:
            raise self._corrupt(f"unsupported version {version!r}")
        paths = document.get("paths")
        if not isinstance(paths, dict):
            raise self._corrupt("'paths' is not an object")
        for path, digest in paths.items():
            if not _is_safe_relative(path):
                raise self._corrupt("it lists an unsafe path")
            if not isinstance(digest, str) or not _HASH.fullmatch(digest):
                raise self._corrupt("it holds a malformed content hash")
        return dict(paths)

    def entries(self) -> dict[str, str]:
        """The recorded ``{path: sha256:<hex>}`` pairs; empty when no manifest exists."""
        try:
            info = os.lstat(self._path)
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise self._corrupt(f"unreadable: {exc.strerror or exc}") from exc
        if not stat.S_ISREG(info.st_mode):
            raise self._corrupt("not a regular file")
        if info.st_size > _MAX_MANIFEST_BYTES:
            raise self._corrupt("file too large")
        try:
            document = json.loads(self._path.read_bytes().decode("utf-8"))
        except OSError as exc:
            raise self._corrupt(f"unreadable: {exc.strerror or exc}") from exc
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise self._corrupt("invalid JSON") from exc
        return self._validated(document)

    def __len__(self) -> int:
        return len(self.entries())

    def _save(self, entries: dict[str, str]) -> None:
        payload = json.dumps({"version": MANIFEST_VERSION, "paths": dict(sorted(entries.items()))}, indent=2)
        try:
            directory = self._path.parent
            ensure_state_directory(directory, workdir=self._workdir)
            root = directory.resolve()
            atomic_write_bytes(root / self._path.name, f"{payload}\n".encode(), root_real=root)
        except (OSError, FsError) as exc:
            raise GithubWikiError(
                "workdir.unusable",
                f"The pending manifest could not be written ({type(exc).__name__})",
                context={"manifest": self._path, "workdir": self._workdir},
                hint="check the permissions of the .git directory in the workdir",
            ) from exc

    # -- hashing the workdir --------------------------------------------------

    def _current_hash(self, path: str) -> str | None:
        """Hash of the regular file at ``path`` in the workdir, or ``None``.

        ``None`` for anything that is not a plain regular file reachable inside
        the workdir (missing, directory, symlink, path through an escaping link).
        """
        target = self._workdir / path
        try:
            if not stat.S_ISREG(os.lstat(target).st_mode):
                return None
            ensure_within_root(self._workdir.resolve(), target)
            return content_version(target.read_bytes())
        except (OSError, FsError):
            return None

    # -- operations -----------------------------------------------------------

    def record(self, paths: Iterable[str]) -> None:
        """Record ``paths`` with the hash of the bytes currently in the workdir.

        All or nothing: a path that cannot be read back raises ``workdir.unusable``
        and the manifest is left as it was. Paths must be safe relative paths
        (a programming error otherwise).
        """
        entries = self.entries()
        updates: dict[str, str] = {}
        for path in paths:
            if not _is_safe_relative(path):
                raise ValueError(f"manifest paths must be safe relative paths, got {path!r}")
            digest = self._current_hash(path)
            if digest is None:
                raise GithubWikiError(
                    "workdir.unusable",
                    "A written path could not be read back from the workdir",
                    context={"path": path, "workdir": self._workdir},
                    hint="check that the backend wrote a regular file inside the workdir",
                )
            updates[path] = digest
        if updates:
            self._save({**entries, **updates})

    def classify(self, dirty: Iterable[str]) -> Classification:
        """Split ``dirty`` paths into pending (recorded and unchanged) and foreign."""
        entries = self.entries()
        pending: list[str] = []
        foreign: list[str] = []
        for path in sorted(set(dirty)):
            recorded = entries.get(path)
            if recorded is not None and self._current_hash(path) == recorded:
                pending.append(path)
            else:
                foreign.append(path)
        return Classification(pending=tuple(pending), foreign=tuple(foreign))

    def check(self, dirty: Iterable[str]) -> Classification:
        """Classify ``dirty`` and refuse with ``workdir.dirty`` when any path is foreign."""
        result = self.classify(dirty)
        if result.foreign:
            shown = result.foreign[:MAX_REPORTED_PATHS]
            context: dict[str, object] = {
                "workdir": self._workdir,
                "paths": ", ".join(shown),
                "count": len(result.foreign),
            }
            if len(result.foreign) > len(shown):
                context["omitted"] = len(result.foreign) - len(shown)
            raise GithubWikiError(
                "workdir.dirty",
                "The workdir has changes that wikiops did not make",
                context=context,
            )
        return result

    def prune(self, dirty: Iterable[str]) -> tuple[str, ...]:
        """Drop entries whose path is no longer dirty (committed or clean); return them."""
        entries = self.entries()
        dirty_set = set(dirty)
        kept = {path: digest for path, digest in entries.items() if path in dirty_set}
        removed = tuple(sorted(set(entries) - set(kept)))
        if removed:
            self._save(kept)
        return removed

    def discard(self, paths: Iterable[str]) -> None:
        """Forget ``paths`` (they were committed); unknown paths are ignored."""
        entries = self.entries()
        forgotten = set(paths)  # materialized once: ``paths`` may be a one-shot iterator
        kept = {path: digest for path, digest in entries.items() if path not in forgotten}
        if len(kept) != len(entries):
            self._save(kept)
