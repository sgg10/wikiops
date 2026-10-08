"""Undo a backend write the provider then refused or that failed half way (S12.F1, S13).

The backend writes first and reports afterwards. When the provider rejects what it
reported (a page ref the policy refuses, a forbidden link, an asset reference a wiki
cannot link, a manifest that cannot record it), or the backend itself raises after it
started writing, the files it produced would stay in the clone and the NEXT run would stop
on them as ``workdir.dirty``: our own orphan blocking the user.

``roll_back`` removes exactly that, and nothing else. The workdir lock is advisory, so a
user (or another tool) can create or edit files while the backend runs; deleting "whatever
is new" would destroy their work. A path is therefore rolled back only when BOTH hold:

* it appeared since ``take_snapshot``, taken right before the backend was called (a path
  that was already dirty then, pending or not, is never touched), and
* the caller says the operation owns it (``owns``: the page reference of a rejected
  operation, the name of an asset), and it is not in ``keep`` (what was accepted).

A tracked file is restored from HEAD, an untracked one is deleted. Every other new path is
left alone and reported as foreign. ``roll_back`` never raises: what it could not undo is
in the returned ``RollbackResult``, so the caller can say so instead of hiding it.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path

from wikiops.providers.github_wiki.errors import GithubWikiError
from wikiops.providers.github_wiki.git import Git
from wikiops.providers.github_wiki.manifest import StatusEntry, parse_status_entries

_STATUS = ("status", "--porcelain=v1", "-z", "--untracked-files=all")


@dataclass(frozen=True)
class Snapshot:
    """The dirty paths of the clone at one moment."""

    paths: frozenset[str]


def _entries(git: Git) -> tuple[StatusEntry, ...]:
    return parse_status_entries(git.local(*_STATUS).stdout)


def take_snapshot(git: Git) -> Snapshot:
    """Record which paths are dirty now; raises the coded error when git cannot say."""
    return Snapshot(frozenset(entry.path for entry in _entries(git)))


def _quoted(paths: Collection[str]) -> str:
    return ", ".join(f"'{path}'" for path in paths)


@dataclass(frozen=True)
class RollbackResult:
    """What the rollback could not do, in the terms the user needs to finish it by hand.

    ``leftovers``: paths of the operation that could not be restored or deleted.
    ``foreign``: new paths the operation does not own (or a collapsed directory), left alone.
    ``status_error``: why the clone status could not be read; nothing was rolled back then.
    """

    leftovers: tuple[str, ...] = ()
    foreign: tuple[str, ...] = ()
    status_error: str | None = None

    @property
    def clean(self) -> bool:
        return not (self.leftovers or self.foreign or self.status_error)

    @property
    def note(self) -> str:
        """One sentence per problem, ready to append to a failure message; empty when clean."""
        parts: list[str] = []
        if self.status_error:
            parts.append(
                f"the clone status could not be read ({self.status_error}), so nothing was "
                "rolled back: inspect the clone before the next apply"
            )
        if self.leftovers:
            parts.append(
                f"the clone still holds {_quoted(self.leftovers)}: "
                "restore or delete it by hand before the next apply"
            )
        if self.foreign:
            parts.append(
                f"left untouched: {_quoted(self.foreign)} appeared in the clone during this "
                "operation and is not part of it"
            )
        return "; ".join(parts)


class RollbackIncomplete(Exception):
    """A write failed with an error that cannot carry a note, and the rollback left something.

    ``cause`` is the original error (also chained as ``__cause__``); ``note`` says what the
    clone still holds. Only raised when there is something to say: a clean rollback re-raises
    the original error untouched.
    """

    def __init__(self, cause: Exception, note: str) -> None:
        super().__init__(f"{type(cause).__name__}: {cause}; {note}")
        self.cause = cause
        self.note = note


def annotate(error: Exception, result: RollbackResult) -> Exception:
    """The error to raise once the rollback ran: ``error`` itself when nothing is left to say.

    A coded error keeps its code and gets the note in its summary; any other error is
    wrapped in ``RollbackIncomplete`` so its type and message are not lost.
    """
    if result.clean:
        return error
    if isinstance(error, GithubWikiError):
        return GithubWikiError(
            error.code, f"{error.summary}; {result.note}", context=error.context, hint=error.hint
        )
    return RollbackIncomplete(error, result.note)


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


def roll_back(
    git: Git,
    *,
    before: Snapshot,
    owns: Callable[[str], bool],
    keep: Collection[str] = (),
) -> RollbackResult:
    """Undo what the operation left in the clone since ``before``; report the rest.

    ``owns(path)`` says whether the operation is the one that wrote ``path``; ``keep`` holds
    the paths it legitimately wrote (accepted work), which are neither undone nor reported.
    A collapsed untracked directory (a nested repository, which git does not expand) is
    never owned: deleting a tree we cannot enumerate is not ours to do, so it is reported
    as foreign.
    """
    try:
        entries = _entries(git)
    except GithubWikiError as error:
        return RollbackResult(status_error=error.summary)
    spared = set(keep)
    appeared = [
        entry for entry in entries if entry.path not in before.paths and entry.path not in spared
    ]
    ours = [entry for entry in appeared if not entry.collapsed_directory and owns(entry.path)]
    foreign = sorted(entry.path for entry in appeared if entry not in ours)
    tracked = [entry.path for entry in ours if not entry.untracked]
    left: list[str] = []
    if tracked and not _restore(git, tracked):
        left += tracked
    left += [
        entry.path for entry in ours if entry.untracked and not _delete(git.workdir, entry.path)
    ]
    return RollbackResult(leftovers=tuple(sorted(left)), foreign=tuple(foreign))
