"""Undo a backend write the provider then refused (S12.F1).

The backend writes first and reports afterwards. When the provider rejects what it
reported (a page ref the policy refuses, a forbidden link, an asset reference a wiki
cannot link, a manifest that cannot record it), the files the backend produced would
stay in the clone, and the NEXT run would stop on them as ``workdir.dirty``: our own
orphan blocking the user.

``roll_back`` removes exactly that. It runs under the workdir lock, after the clone was
checked clean (apart from wikiops' own pending work), so every dirty path that is not
pending, and not something the caller wants to keep, was produced by this operation: a
tracked file is restored from HEAD, an untracked one is deleted. Pending paths are never
touched, whatever happened to them. It never raises: what it could not undo is returned,
so the caller can say so instead of hiding it.
"""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path

from wikiops.providers.github_wiki.errors import GithubWikiError
from wikiops.providers.github_wiki.git import Git
from wikiops.providers.github_wiki.manifest import parse_status

_UNTRACKED = "?? "
STATUS_UNREADABLE = "(the clone status could not be read)"


def _untracked(status: str) -> set[str]:
    return {entry[len(_UNTRACKED) :] for entry in status.split("\0") if entry.startswith(_UNTRACKED)}


def _delete(workdir: Path, path: str) -> bool:
    """Delete one untracked file; never follows a symlinked directory out of the workdir."""
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts:
        return False
    target = workdir / relative
    try:
        if target.parent.resolve() != (workdir.resolve() / relative.parent):
            return False
        target.unlink()
    except OSError:
        return False
    return True


def _restore(git: Git, paths: list[str]) -> bool:
    try:
        git.local("checkout", "HEAD", paths=paths)
    except GithubWikiError:
        return False
    return True


def roll_back(git: Git, *, before: Collection[str], keep: Collection[str]) -> tuple[str, ...]:
    """Undo every change that appeared since ``before``, except ``keep``; return what is left.

    ``before`` is the set of paths that were already pending when the operation began,
    ``keep`` the paths the operation legitimately wrote. The returned paths (sorted) could
    not be restored or deleted; ``(STATUS_UNREADABLE,)`` when the clone status itself failed.
    """
    try:
        status = git.local("status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
        dirty = parse_status(status)
    except GithubWikiError:
        return (STATUS_UNREADABLE,)
    spared = set(before) | set(keep)
    orphans = [path for path in dirty if path not in spared]
    untracked = _untracked(status)
    left: list[str] = []
    tracked = [path for path in orphans if path not in untracked]
    if tracked and not _restore(git, tracked):
        left += tracked
    left += [
        path
        for path in orphans
        if path in untracked and (path.endswith("/") or not _delete(git.workdir, path))
    ]
    return tuple(sorted(left))
