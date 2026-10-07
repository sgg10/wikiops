"""Production ``GitRunner``: one real subprocess per call, never a shell."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from wikiops.providers.github_wiki.ports import CommandResult

_BASELINE_ENV = {"GIT_TERMINAL_PROMPT": "0"}
_COMMAND_NOT_FOUND = 127


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


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


class SubprocessGitRunner:
    """Runs argv lists with ``shell=False`` and a non-interactive environment.

    The environment is the caller's own plus ``GIT_TERMINAL_PROMPT=0`` and the
    per-call overrides. stdin is closed unless text is supplied, so a command
    that wants to prompt fails instead of hanging. On timeout the whole process
    group is killed and the result carries ``timed_out=True``.
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
            return CommandResult(command, _COMMAND_NOT_FOUND, "", f"{command[0]}: {exc}")
        payload = None if stdin is None else stdin.encode("utf-8")
        timed_out = False
        try:
            stdout, stderr = process.communicate(payload, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(process)
            stdout, stderr = process.communicate()
            timed_out = True
        return CommandResult(
            command, process.returncode, _decode(stdout), _decode(stderr), timed_out
        )
