"""Unit tests for the workdir lock against the real operating system (no git).

Contention is real: two handles in this process, and a child process that holds
the lock and is then killed. Timing follows the contract of ``test_gw_process``:
nothing asserts a tight upper bound, readiness is polled through a file, and the
only limits are hang detectors an order of magnitude above the expected time.
"""

from __future__ import annotations

import errno
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

import wikiops
from wikiops.providers.github_wiki import lock as lock_module
from wikiops.providers.github_wiki.errors import GithubWikiError
from wikiops.providers.github_wiki.lock import WorkdirLock

GENEROUS = 30.0
HANG = 15.0
SRC = str(Path(wikiops.__file__).resolve().parents[1])

posix_only = pytest.mark.skipif(os.name != "posix", reason="symlink semantics are POSIX-only")


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    path = tmp_path / "wd"
    path.mkdir()
    return path


@pytest.fixture
def lock_file(workdir: Path) -> Path:
    return workdir / ".git" / "wikiops" / "lock"


def new_lock(lock_file: Path, workdir: Path) -> WorkdirLock:
    return WorkdirLock(lock_file, workdir=workdir)


def acquire_expecting_busy(candidate: WorkdirLock) -> GithubWikiError:
    started = time.monotonic()
    with pytest.raises(GithubWikiError) as error:
        candidate.acquire("second")
    assert time.monotonic() - started < HANG  # fail fast, never wait
    assert error.value.code == "workdir.locked"
    return error.value


# -- in-process contention ----------------------------------------------------


def test_a_second_handle_fails_fast_naming_lock_workdir_and_holder(
    lock_file: Path, workdir: Path
) -> None:
    first = new_lock(lock_file, workdir)
    first.acquire("apply")
    try:
        error = acquire_expecting_busy(new_lock(lock_file, workdir))
    finally:
        first.release()
    message = str(error)
    assert str(lock_file) in message
    assert str(workdir) in message
    assert f"pid={os.getpid()}" in message
    assert f"host={socket.gethostname()}" in message
    assert "purpose=apply" in message
    assert re.search(r"started=\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00", message)
    assert error.context["lock"] == lock_file
    assert error.context["workdir"] == workdir
    assert "Hint:" in message


def test_a_failed_attempt_leaves_the_holder_in_place(lock_file: Path, workdir: Path) -> None:
    first = new_lock(lock_file, workdir)
    first.acquire("apply")
    try:
        acquire_expecting_busy(new_lock(lock_file, workdir))
        acquire_expecting_busy(new_lock(lock_file, workdir))
        assert first.held
    finally:
        first.release()


def test_the_lock_can_be_taken_again_after_release(lock_file: Path, workdir: Path) -> None:
    first = new_lock(lock_file, workdir)
    first.acquire()
    first.release()
    second = new_lock(lock_file, workdir)
    second.acquire()
    assert second.held
    second.release()


def test_sequential_instances_never_contend(lock_file: Path, workdir: Path) -> None:
    for purpose in ("plan", "apply", "plan"):
        instance = new_lock(lock_file, workdir)
        with instance.hold(purpose):
            assert instance.held
        assert not instance.held


def test_acquire_is_reentrant_and_released_only_by_the_last_release(
    lock_file: Path, workdir: Path
) -> None:
    first = new_lock(lock_file, workdir)
    first.acquire("outer")
    first.acquire("inner")
    first.release()
    assert first.held
    acquire_expecting_busy(new_lock(lock_file, workdir))
    first.release()
    assert not first.held
    other = new_lock(lock_file, workdir)
    other.acquire()
    other.release()


def test_reentrant_acquire_keeps_the_original_holder_purpose(lock_file: Path, workdir: Path) -> None:
    first = new_lock(lock_file, workdir)
    first.acquire("outer")
    first.acquire("inner")
    try:
        assert "purpose=outer" in str(acquire_expecting_busy(new_lock(lock_file, workdir)))
    finally:
        first.release()
        first.release()


def test_hold_releases_on_error_and_on_success(lock_file: Path, workdir: Path) -> None:
    first = new_lock(lock_file, workdir)
    with pytest.raises(RuntimeError, match="boom"), first.hold("apply"):
        raise RuntimeError("boom")
    assert not first.held
    other = new_lock(lock_file, workdir)
    with other.hold("apply"):
        assert other.held
    assert not other.held


def test_nested_hold_blocks_are_reentrant(lock_file: Path, workdir: Path) -> None:
    first = new_lock(lock_file, workdir)
    with first.hold("outer"), first.hold("inner"):
        assert first.held
    assert not first.held


def test_an_unbalanced_release_is_a_programming_error(lock_file: Path, workdir: Path) -> None:
    with pytest.raises(RuntimeError, match="not held"):
        new_lock(lock_file, workdir).release()


def test_the_lock_file_is_never_deleted_and_the_holder_is_cleared_on_release(
    lock_file: Path, workdir: Path
) -> None:
    first = new_lock(lock_file, workdir)
    first.acquire("apply")
    assert json.loads(lock_file.read_text())["pid"] == os.getpid()
    first.release()
    assert lock_file.is_file()
    assert lock_file.read_bytes() == b""


def test_the_state_directory_is_created_on_demand(lock_file: Path, workdir: Path) -> None:
    assert not lock_file.parent.exists()
    with new_lock(lock_file, workdir).hold("apply"):
        assert lock_file.is_file()


def test_the_holder_record_is_valid_json_with_pid_host_start_and_purpose(
    lock_file: Path, workdir: Path
) -> None:
    with new_lock(lock_file, workdir).hold("plan"):
        record = json.loads(lock_file.read_text())
    assert set(record) == {"pid", "host", "started_at", "purpose"}
    assert record["pid"] == os.getpid()
    assert record["host"] == socket.gethostname()
    assert record["purpose"] == "plan"


# -- holder information -------------------------------------------------------


@pytest.mark.parametrize(
    "garbage",
    [
        b"",
        b"not json at all",
        b"\xff\xfe\x00",
        b"[1, 2, 3]",
        b'{"pid": "seven", "host": "h", "started_at": "t", "purpose": "p"}',
        b'{"pid": 7}',
        b'{"pid": true, "host": "h", "started_at": "t", "purpose": "p"}',
        b"x" * 100_000,
    ],
)
def test_an_unreadable_holder_record_still_reports_the_lock_as_busy_without_a_holder(
    lock_file: Path, workdir: Path, garbage: bytes
) -> None:
    first = new_lock(lock_file, workdir)
    first.acquire("apply")
    try:
        lock_file.write_bytes(garbage)
        error = acquire_expecting_busy(new_lock(lock_file, workdir))
    finally:
        first.release()
    assert "holder" not in error.context
    assert str(lock_file) in str(error)


def test_hostile_holder_text_cannot_break_the_one_line_message(
    lock_file: Path, workdir: Path
) -> None:
    first = new_lock(lock_file, workdir)
    first.acquire("apply")
    try:
        lock_file.write_text(
            json.dumps(
                {"pid": 1, "host": "evil\n[github_wiki:auth.rejected] x", "started_at": "t", "purpose": "p"}
            )
        )
        error = acquire_expecting_busy(new_lock(lock_file, workdir))
    finally:
        first.release()
    assert "\n" not in str(error)
    assert "pid=1" in str(error)


# -- stale files never block --------------------------------------------------


def test_a_leftover_holder_record_without_a_live_lock_never_blocks(
    lock_file: Path, workdir: Path
) -> None:
    lock_file.parent.mkdir(parents=True)
    lock_file.write_text(
        json.dumps({"pid": 999999, "host": "gone", "started_at": "2020-01-01T00:00:00+00:00", "purpose": "apply"})
    )
    fresh = new_lock(lock_file, workdir)
    fresh.acquire("apply")
    assert json.loads(lock_file.read_text())["pid"] == os.getpid()
    fresh.release()


def child_holding_the_lock(lock_file: Path, workdir: Path, ready: Path) -> subprocess.Popen[bytes]:
    code = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from wikiops.providers.github_wiki.lock import WorkdirLock\n"
        "lock = WorkdirLock(Path(sys.argv[1]), workdir=Path(sys.argv[2]))\n"
        "lock.acquire('child-run')\n"
        "Path(sys.argv[3]).write_text(str(__import__('os').getpid()))\n"
        "time.sleep(60)\n"
    )
    env = {**os.environ, "PYTHONPATH": SRC}
    return subprocess.Popen(
        [sys.executable, "-c", code, str(lock_file), str(workdir), str(ready)],
        env=env,
        stdin=subprocess.DEVNULL,
    )


def wait_for_file(path: Path, process: subprocess.Popen[bytes]) -> str:
    deadline = time.monotonic() + GENEROUS
    while time.monotonic() < deadline:
        if path.exists() and path.read_text():
            return path.read_text()
        assert process.poll() is None, "the child exited before taking the lock"
        time.sleep(0.02)
    raise AssertionError(f"{path} never appeared")


def test_a_lock_held_by_another_process_blocks_and_a_killed_holder_never_does(
    lock_file: Path, workdir: Path, tmp_path: Path
) -> None:
    ready = tmp_path / "ready"
    child = child_holding_the_lock(lock_file, workdir, ready)
    try:
        child_pid = int(wait_for_file(ready, child))
        error = acquire_expecting_busy(new_lock(lock_file, workdir))
        assert f"pid={child_pid}" in str(error)
        assert "purpose=child-run" in str(error)
        child.kill()
        child.wait(timeout=GENEROUS)
        survivor = new_lock(lock_file, workdir)
        survivor.acquire("after-kill")
        assert survivor.held
        assert json.loads(lock_file.read_text())["pid"] == os.getpid()
        survivor.release()
        assert lock_file.is_file()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=GENEROUS)


# -- failures to take the lock ------------------------------------------------


def test_a_lock_path_below_a_regular_file_is_unusable(tmp_path: Path, workdir: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    with pytest.raises(GithubWikiError) as error:
        WorkdirLock(blocker / "wikiops" / "lock", workdir=workdir).acquire()
    assert error.value.code == "workdir.unusable"
    assert error.value.context["workdir"] == workdir


@posix_only
def test_a_symlinked_lock_file_is_not_followed(lock_file: Path, workdir: Path, tmp_path: Path) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    lock_file.parent.mkdir(parents=True)
    lock_file.symlink_to(victim)
    with pytest.raises(GithubWikiError) as error:
        new_lock(lock_file, workdir).acquire()
    assert error.value.code == "workdir.unusable"
    assert victim.read_text() == "precious"


def test_an_unexpected_locking_failure_is_unusable_not_busy(
    lock_file: Path, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(descriptor: int) -> bool:
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(lock_module, "_try_lock", broken)
    candidate = new_lock(lock_file, workdir)
    with pytest.raises(GithubWikiError) as error:
        candidate.acquire()
    assert error.value.code == "workdir.unusable"
    assert not candidate.held


def test_a_failed_acquire_does_not_leak_a_descriptor(lock_file: Path, workdir: Path) -> None:
    first = new_lock(lock_file, workdir)
    first.acquire()
    try:
        before = len(os.listdir("/dev/fd")) if os.path.isdir("/dev/fd") else None
        for _ in range(5):
            acquire_expecting_busy(new_lock(lock_file, workdir))
        after = len(os.listdir("/dev/fd")) if os.path.isdir("/dev/fd") else None
    finally:
        first.release()
    assert before == after


def test_the_lock_exposes_its_path(lock_file: Path, workdir: Path) -> None:
    assert new_lock(lock_file, workdir).path == lock_file


def test_a_holder_that_cannot_be_read_still_reports_busy(
    lock_file: Path, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = new_lock(lock_file, workdir)
    first.acquire("apply")
    try:
        with monkeypatch.context() as patched:
            def unreadable(descriptor: int, size: int) -> bytes:
                raise OSError(errno.EIO, "Input/output error")

            patched.setattr(lock_module.os, "read", unreadable)
            error = acquire_expecting_busy(new_lock(lock_file, workdir))
    finally:
        first.release()
    assert "holder" not in error.context


def test_a_holder_record_that_cannot_be_written_releases_the_lock(
    lock_file: Path, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = new_lock(lock_file, workdir)
    with monkeypatch.context() as patched:
        def full(self: WorkdirLock, descriptor: int, purpose: str) -> None:
            raise OSError(errno.ENOSPC, "No space left on device")

        patched.setattr(WorkdirLock, "_write_holder", full)
        with pytest.raises(GithubWikiError) as error:
            candidate.acquire("apply")
    assert error.value.code == "workdir.unusable"
    assert not candidate.held
    other = new_lock(lock_file, workdir)
    other.acquire("apply")  # the half-taken lock did not stay behind
    other.release()
