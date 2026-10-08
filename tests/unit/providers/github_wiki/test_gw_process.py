"""Unit tests for ``SubprocessGitRunner`` against real subprocesses (no git).

Timing contract of these tests: a "timeout" test never asserts a tight upper
bound (that would depend on machine load) and never depends on how fast the
interpreter starts. Children run ``python -S`` (no site import, ~20 ms start),
the timeout that must expire is ``SHORT`` (50x that), and the only upper bounds
are hang detectors an order of magnitude above the expected duration.

A test that inspects what a child did before the timeout (a pid, partial
output) runs it through ``run_until_ready``: the child announces readiness in a
marker file, and if a loaded machine killed it before that, the run is repeated
with a longer timeout. The normal case costs one run of ``SHORT``; slowness
makes the test patient instead of flaky.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from contextlib import suppress
from pathlib import Path

import pytest

from wikiops.providers.github_wiki import process
from wikiops.providers.github_wiki.ports import LOCALE_PIN, CommandResult, GitRunner
from wikiops.providers.github_wiki.process import SubprocessGitRunner

PY = sys.executable
GENEROUS = 30.0
SHORT = 1.0  # a timeout that is meant to expire
HANG = 15.0  # no run below may take this long: it would be a hang, not slowness

posix_only = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")


def python(code: str, *args: str) -> list[str]:
    return [PY, "-S", "-c", code, *args]


def run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str | None] | None = None,
    timeout: float = GENEROUS,
    stdin: str | None = None,
) -> CommandResult:
    return SubprocessGitRunner().run(
        argv, cwd=cwd, env_overrides=env or {}, timeout=timeout, stdin=stdin
    )


def test_the_runner_satisfies_the_git_runner_port() -> None:
    assert isinstance(SubprocessGitRunner(), GitRunner)


# -- result shape ------------------------------------------------------------


def test_stdout_stderr_and_returncode_are_captured_and_argv_is_recorded() -> None:
    argv = python(
        "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"
    )

    result = run(argv)

    assert result == CommandResult(
        argv=tuple(argv), returncode=3, stdout="out\n", stderr="err\n", timed_out=False
    )


def test_a_successful_command_has_returncode_zero() -> None:
    assert run(python("print('ok')")).returncode == 0


def test_cwd_is_the_working_directory_of_the_child(tmp_path: Path) -> None:
    result = run(python("import os; print(os.getcwd())"), cwd=tmp_path)

    assert Path(result.stdout.strip()).resolve() == tmp_path.resolve()


def test_large_output_on_both_streams_does_not_deadlock() -> None:
    code = (
        "import sys; sys.stdout.write('o' * 1_000_000); "
        "sys.stderr.write('e' * 1_000_000)"
    )

    result = run(python(code))

    assert (len(result.stdout), len(result.stderr)) == (1_000_000, 1_000_000)


def test_non_utf8_output_is_decoded_with_replacement_characters() -> None:
    code = "import sys; sys.stdout.buffer.write(b'ok \\xff\\xfe end'); sys.stderr.buffer.write(b'\\xc3')"

    result = run(python(code))

    assert result.stdout == "ok �� end"
    assert result.stderr == "�"


# -- no shell ----------------------------------------------------------------


def test_arguments_are_passed_literally_without_a_shell(tmp_path: Path) -> None:
    marker = tmp_path / "pwned"
    hostile = f"; touch {marker}; echo $HOME `id` | cat"

    result = run(python("import sys; print(sys.argv[1])", hostile))

    assert result.stdout.strip() == hostile
    assert not marker.exists()


# -- environment -------------------------------------------------------------


def test_env_overrides_set_and_replace_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WK_REPLACED", "old")
    code = "import os; print(os.environ['WK_NEW'], os.environ['WK_REPLACED'])"

    result = run(python(code), env={"WK_NEW": "n", "WK_REPLACED": "new"})

    assert result.stdout.strip() == "n new"


def test_a_none_override_removes_an_inherited_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WK_INHERITED", "yes")
    code = "import os; print(os.environ.get('WK_INHERITED', 'unset'))"

    assert run(python(code)).stdout.strip() == "yes"
    assert run(python(code), env={"WK_INHERITED": None}).stdout.strip() == "unset"


def test_unsetting_a_variable_that_is_not_there_is_harmless() -> None:
    code = "import os; print(os.environ.get('WK_NEVER_SET', 'unset'))"

    assert run(python(code), env={"WK_NEVER_SET": None}).stdout.strip() == "unset"


def test_the_inherited_environment_reaches_the_child(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WK_PASSED_THROUGH", "visible")

    result = run(python("import os; print(os.environ['WK_PASSED_THROUGH'])"))

    assert result.stdout.strip() == "visible"


def test_git_terminal_prompt_is_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GIT_TERMINAL_PROMPT", raising=False)
    code = "import os; print(os.environ.get('GIT_TERMINAL_PROMPT'))"

    assert run(python(code)).stdout.strip() == "0"


def test_the_baseline_wins_over_the_ambient_environment_but_not_over_an_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "1")
    code = "import os; print(os.environ.get('GIT_TERMINAL_PROMPT'))"

    assert run(python(code)).stdout.strip() == "0"
    assert run(python(code), env={"GIT_TERMINAL_PROMPT": "1"}).stdout.strip() == "1"


def test_the_locale_is_pinned_so_tool_output_is_always_english(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    monkeypatch.setenv("LANGUAGE", "de:fr")
    code = "import os; print(os.environ.get('LC_ALL'), repr(os.environ.get('LANGUAGE')))"

    assert run(python(code)).stdout.strip() == "C ''"


def test_the_locale_baseline_applies_when_the_ambient_environment_sets_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LANGUAGE", raising=False)
    code = "import os; print(os.environ.get('LC_ALL'), repr(os.environ.get('LANGUAGE')))"

    assert run(python(code)).stdout.strip() == "C ''"


def test_an_explicit_override_cannot_change_the_pinned_locale() -> None:
    code = "import os; print(os.environ.get('LC_ALL'), repr(os.environ.get('LANGUAGE')))"

    result = run(python(code), env={"LC_ALL": "de_DE.UTF-8", "LANGUAGE": "de"})

    assert result.stdout.strip() == "C ''"


def test_the_runner_pins_exactly_the_shared_locale_definition() -> None:
    code = (
        "import os, json; "
        "print(json.dumps({k: os.environ.get(k) for k in ('LC_ALL', 'LANGUAGE')}))"
    )

    observed = json.loads(run(python(code)).stdout)

    assert observed == dict(LOCALE_PIN)


def test_the_process_environment_of_the_caller_is_never_modified() -> None:
    before = dict(os.environ)

    run(python("pass"), env={"WK_LEAK": "x", "PATH": None})

    assert dict(os.environ) == before


# -- stdin -------------------------------------------------------------------


def test_stdin_is_closed_by_default_so_a_prompting_child_cannot_hang() -> None:
    result = run(python("import sys; print(repr(sys.stdin.read()))"), timeout=10)

    assert result.stdout.strip() == "''"
    assert result.timed_out is False


def test_stdin_text_is_delivered_when_given() -> None:
    result = run(python("import sys; print(sys.stdin.read().upper())"), stdin="héllo\nworld")

    assert result.stdout == "HÉLLO\nWORLD\n"


# -- timeout -----------------------------------------------------------------


def wait_for_file(path: Path, *, limit: float = GENEROUS) -> str:
    """The text of ``path`` once a child wrote it (polling, never a fixed sleep)."""
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if path.exists() and path.read_text():
            return path.read_text()
        time.sleep(0.02)
    raise AssertionError(f"{path.name} was never written")


def run_until_ready(code: str, marker: Path, *, attempts: int = 4) -> CommandResult:
    """Run ``code`` until it times out AFTER writing ``marker`` (its readiness signal).

    The timeout starts at ``SHORT`` and grows fourfold when the child was killed
    before it became ready, so a slow start is retried rather than reported.
    """
    timeout = SHORT
    for _ in range(attempts):
        marker.unlink(missing_ok=True)
        result = run(python(code), timeout=timeout)
        if marker.exists() and marker.read_text():
            return result
        timeout *= 4
    raise AssertionError(f"{marker.name} was never written, even with a {timeout / 4:.0f} s timeout")


def test_a_command_over_its_timeout_is_killed_and_reported(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    code = (
        "import time; print('started', flush=True); "
        f"open({str(ready)!r}, 'w').write('ready'); time.sleep(60)"
    )
    started = time.monotonic()

    result = run_until_ready(code, ready)

    assert result.timed_out is True
    assert result.returncode != 0
    assert "started" in result.stdout
    assert time.monotonic() - started < HANG


def test_a_command_that_finishes_in_time_is_not_flagged() -> None:
    assert run(python("print('fast')"), timeout=GENEROUS).timed_out is False


@posix_only
def test_a_timeout_leaves_no_orphaned_grandchild(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    code = (
        "import subprocess, sys, time; "
        "p = subprocess.Popen([sys.executable, '-S', '-c', 'import time; time.sleep(60)']); "
        f"open({str(pid_file)!r}, 'w').write(str(p.pid)); "
        "time.sleep(60)"
    )

    started = time.monotonic()
    result = run_until_ready(code, pid_file)
    elapsed = time.monotonic() - started

    assert result.timed_out is True
    # A grandchild that kept the pipes open would block the runner until it exited.
    assert elapsed < HANG
    grandchild = int(wait_for_file(pid_file))
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        os.kill(grandchild, 9)
        pytest.fail("grandchild process survived the timeout")


@posix_only
def test_a_daemonized_descendant_holding_the_pipes_cannot_stall_the_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The descendant leaves the killed process group (setsid) but keeps the
    # inherited pipes open for 30 s: an unbounded drain would wait for it.
    monkeypatch.setattr(process, "_DRAIN_SECONDS", 0.3)
    pid_file = tmp_path / "daemon.pid"
    code = (
        "import subprocess, sys, time; "
        "d = subprocess.Popen([sys.executable, '-S', '-c', 'import time; time.sleep(30)'], "
        "start_new_session=True); "
        "print('partial output', flush=True); "
        f"open({str(pid_file)!r}, 'w').write(str(d.pid)); "
        "time.sleep(60)"
    )

    started = time.monotonic()
    try:
        result = run_until_ready(code, pid_file)
        elapsed = time.monotonic() - started
    finally:
        with suppress(FileNotFoundError, ProcessLookupError, ValueError):
            os.kill(int(pid_file.read_text()), signal.SIGKILL)

    assert result.timed_out is True
    assert result.returncode != 0
    assert "partial output" in result.stdout  # what was read before the bound is kept
    assert elapsed < 10  # far below the 30 s the daemon holds the pipes


@posix_only
def test_the_drain_after_a_timeout_still_collects_output_of_the_killed_tree(
    tmp_path: Path,
) -> None:
    ready = tmp_path / "ready"
    code = (
        "import sys, time; "
        "sys.stdout.write('o' * 200_000); sys.stdout.flush(); "
        "sys.stderr.write('e' * 200_000); sys.stderr.flush(); "
        f"open({str(ready)!r}, 'w').write('ready'); "
        "time.sleep(60)"
    )

    result = run_until_ready(code, ready)

    assert result.timed_out is True
    assert (len(result.stdout), len(result.stderr)) == (200_000, 200_000)


# -- failure to start --------------------------------------------------------


def test_a_missing_executable_is_command_not_found() -> None:
    argv = ["wikiops-no-such-binary-xyz", "--version"]

    result = run(argv)

    assert result.returncode == 127
    assert result.argv == tuple(argv)
    assert result.stdout == ""
    assert "wikiops-no-such-binary-xyz: command not found" in result.stderr
    assert "working directory" not in result.stderr
    assert result.timed_out is False


@pytest.mark.parametrize("kind", ["missing", "a-file"])
def test_an_unusable_working_directory_is_reported_as_such_not_as_a_missing_command(
    tmp_path: Path, kind: str
) -> None:
    cwd = tmp_path / "does-not-exist"
    if kind == "a-file":
        cwd.write_text("not a directory")

    result = run(python("pass"), cwd=cwd)

    assert result.returncode == 126
    assert "working directory" in result.stderr
    assert str(cwd) in result.stderr
    assert "command not found" not in result.stderr


def test_an_unusable_working_directory_wins_over_a_missing_command(tmp_path: Path) -> None:
    result = run(["wikiops-no-such-binary-xyz"], cwd=tmp_path / "does-not-exist")

    assert result.returncode == 126
    assert "working directory" in result.stderr


@posix_only
def test_an_executable_that_cannot_be_run_is_a_permission_problem(tmp_path: Path) -> None:
    tool = tmp_path / "tool"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o644)

    result = run([str(tool)])

    assert result.returncode == 126
    assert f"{tool}: cannot execute" in result.stderr
    assert "command not found" not in result.stderr
    assert "working directory" not in result.stderr


def test_start_failures_never_raise_and_keep_the_other_fields_empty(tmp_path: Path) -> None:
    results = [
        run(["wikiops-no-such-binary-xyz"]),
        run(python("pass"), cwd=tmp_path / "missing"),
    ]

    assert [(r.stdout, r.timed_out) for r in results] == [("", False), ("", False)]
    assert [r.returncode for r in results] == [127, 126]
