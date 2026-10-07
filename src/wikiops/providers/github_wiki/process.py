"""Production ``GitRunner``: one real subprocess per call, never a shell."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from wikiops.providers.github_wiki.ports import CommandResult

# Applied to every command (git, gh): no terminal prompt, and an English
# locale so error text matches what the classifier and the user-facing hints
# expect. Per-call overrides still win.
_BASELINE_ENV = {"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", "LANGUAGE": ""}
# Shell conventions for a command that never ran.
_COMMAND_NOT_FOUND = 127
_CANNOT_RUN = 126
# After the kill, how long to keep reading the pipes. The killed tree closes
# them at once; only a descendant that left the process group (setsid, a
# daemon) can keep them open, and the runner must not wait for that.
_DRAIN_SECONDS = 2.0


def _environment(env_overrides: Mapping[str, str | None]) -> dict[str, str]:
    environment = {**os.environ, **_BASELINE_ENV}
    for name, value in env_overrides.items():
        if value is None:
            environment.pop(name, None)
        else:
            environment[name] = value
    return environment


def _kill_tree(process: subprocess.Popen[bytes]) -> None:
    """Kill the child and everything it spawned (ssh, remote helpers)."""
    if os.name == "posix":
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    else:  # pragma: no cover - Windows has no process groups to signal
        process.kill()


def _drain(process: subprocess.Popen[bytes]) -> tuple[bytes, bytes]:
    """Collect what the killed command left in its pipes, within ``_DRAIN_SECONDS``.

    A descendant that escaped the process group may hold the pipes open
    indefinitely; the output read so far is kept and the pipes are abandoned.
    """
    try:
        return process.communicate(timeout=_DRAIN_SECONDS)
    except subprocess.TimeoutExpired as exc:
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                with contextlib.suppress(OSError):
                    pipe.close()
        process.wait()  # the child itself was killed: this returns at once
        return exc.stdout or b"", exc.stderr or b""


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _start_failure(command: tuple[str, ...], cwd: Path | None, exc: OSError) -> CommandResult:
    """Describe a command that could not be started, telling the causes apart.

    ``127`` is a missing executable; ``126`` covers an executable that cannot
    be run and a working directory that cannot be entered. The message names
    the actual cause so a bad ``cwd`` is never reported as a missing ``git``.
    """
    executable = command[0]
    reason = exc.strerror or str(exc)
    filename = None if exc.filename is None else os.fsdecode(exc.filename)
    if cwd is not None and (filename == os.fsdecode(cwd) or not os.path.isdir(cwd)):
        stderr = f"{executable}: working directory '{cwd}' is unusable ({reason})"
        return CommandResult(command, _CANNOT_RUN, "", stderr)
    if isinstance(exc, FileNotFoundError):
        stderr = f"{executable}: command not found ({reason})"
        return CommandResult(command, _COMMAND_NOT_FOUND, "", stderr)
    return CommandResult(command, _CANNOT_RUN, "", f"{executable}: cannot execute ({reason})")


class SubprocessGitRunner:
    """Runs argv lists with ``shell=False`` and a non-interactive environment.

    The environment is the caller's own plus the baseline (no terminal prompt,
    ``LC_ALL=C``, empty ``LANGUAGE``) and the per-call overrides. stdin is
    closed unless text is supplied, so a command that wants to prompt fails
    instead of hanging. On timeout the whole process group is killed and the
    result carries ``timed_out=True``; the wait for its output is bounded.
    """

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None,
        env_overrides: Mapping[str, str | None],
        timeout: float,
        stdin: str | None = None,
    ) -> CommandResult:
        command = tuple(argv)
        try:
            process = subprocess.Popen(
                list(command),
                cwd=cwd,
                env=_environment(env_overrides),
                stdin=subprocess.DEVNULL if stdin is None else subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=os.name == "posix",
            )
        except OSError as exc:
            return _start_failure(command, cwd, exc)
        payload = None if stdin is None else stdin.encode("utf-8")
        timed_out = False
        try:
            stdout, stderr = process.communicate(payload, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(process)
            stdout, stderr = _drain(process)
            timed_out = True
        return CommandResult(
            command, process.returncode, _decode(stdout), _decode(stderr), timed_out
        )
