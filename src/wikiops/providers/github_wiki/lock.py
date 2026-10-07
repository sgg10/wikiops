"""The workdir lock: one state-changing sequence per clone at a time.

The lock is an operating-system advisory lock on ``<git dir>/wikiops/lock``
(``fcntl.flock`` with ``LOCK_NB`` on POSIX, ``msvcrt.locking`` with ``LK_NBLCK`` on
Windows). It never waits: if another live run holds it, ``acquire`` fails at once
with ``workdir.locked``. Because the OS drops the lock when its holder dies, a
file left behind by a killed process can never block a later run, so there is no
stale-lock repair and the file is never deleted (which would race with another
run opening it).

The holder identity (pid, host, start time, purpose) is written into the file
while the lock is held so a refused run can name who holds it; the record is
purely informational and is cleared on release.

Reentrancy is per instance: nested ``acquire`` calls share one OS lock through a
counter. Two instances (even in one process) are independent handles and do
contend; callers hold the lock per sequence and release it at the end, so a plan
instance followed by an apply instance never overlap.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import sys
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from wikiops.providers.github_wiki.errors import GithubWikiError

_HOLDER_READ_LIMIT = 4096
_OPEN_FLAGS = (
    os.O_RDWR
    | os.O_CREAT
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_BINARY", 0)
)


def _try_lock(descriptor: int) -> bool:
    """Take the exclusive lock without waiting; ``False`` when another handle holds it."""
    if sys.platform == "win32":  # pragma: no cover - Windows branch, exercised on Windows only
        import errno
        import msvcrt

        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EDEADLK, errno.EAGAIN):
                return False
            raise
        return True
    import fcntl

    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _describe_holder(raw: bytes) -> str | None:
    """Render a holder record as ``pid=.. host=.. started=.. purpose=..``, or ``None``."""
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(record, dict):
        return None
    pid, host, started, purpose = (record.get(key) for key in ("pid", "host", "started_at", "purpose"))
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or not all(isinstance(value, str) for value in (host, started, purpose))
    ):
        return None
    parts = [f"pid={pid}", f"host={host}", f"started={started}"]
    if purpose:
        parts.append(f"purpose={purpose}")
    return " ".join(parts)


class WorkdirLock:
    """Exclusive, fail-fast, reentrant lock on one clone."""

    def __init__(self, path: Path, *, workdir: Path) -> None:
        self._path = path
        self._workdir = workdir
        self._descriptor: int | None = None
        self._depth = 0

    @property
    def path(self) -> Path:
        return self._path

    @property
    def held(self) -> bool:
        return self._depth > 0

    def _unusable(self, action: str, exc: OSError) -> GithubWikiError:
        return GithubWikiError(
            "workdir.unusable",
            f"The workdir lock could not be {action} ({type(exc).__name__}: {exc.strerror or exc})",
            context={"lock": self._path, "workdir": self._workdir},
            hint="check the permissions of the .git directory in the workdir",
        )

    def _open(self) -> int:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            return os.open(self._path, _OPEN_FLAGS, 0o600)
        except OSError as exc:
            raise self._unusable("opened", exc) from exc

    def _read_holder(self, descriptor: int) -> str | None:
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            return _describe_holder(os.read(descriptor, _HOLDER_READ_LIMIT))
        except OSError:
            return None

    def _write_holder(self, descriptor: int, purpose: str) -> None:
        record = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "purpose": purpose,
        }
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.write(descriptor, json.dumps(record).encode("utf-8"))

    def acquire(self, purpose: str = "") -> None:
        """Take the lock, or nest inside the hold this instance already has.

        Raises ``workdir.locked`` immediately when another handle holds it.
        """
        if self._depth:
            self._depth += 1
            return
        descriptor = self._open()
        try:
            acquired = _try_lock(descriptor)
        except OSError as exc:
            os.close(descriptor)
            raise self._unusable("taken", exc) from exc
        if not acquired:
            holder = self._read_holder(descriptor)
            os.close(descriptor)
            context: dict[str, object] = {"lock": self._path, "workdir": self._workdir}
            if holder is not None:
                context["holder"] = holder
            raise GithubWikiError(
                "workdir.locked", "The workdir is locked by another wikiops run", context=context
            )
        try:
            self._write_holder(descriptor, purpose)
        except OSError as exc:
            os.close(descriptor)
            raise self._unusable("written", exc) from exc
        self._descriptor = descriptor
        self._depth = 1

    def release(self) -> None:
        """Undo one ``acquire``; the OS lock is dropped by the last one."""
        if not self._depth or self._descriptor is None:
            raise RuntimeError("the workdir lock is not held")
        self._depth -= 1
        if self._depth:
            return
        descriptor, self._descriptor = self._descriptor, None
        with contextlib.suppress(OSError):
            os.ftruncate(descriptor, 0)
        os.close(descriptor)

    @contextlib.contextmanager
    def hold(self, purpose: str = "") -> Iterator[None]:
        """Hold the lock for the duration of the block."""
        self.acquire(purpose)
        try:
            yield
        finally:
            self.release()
