"""Unit tests for ``SubprocessGitRunner`` against real subprocesses (no git)."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from wikiops.providers.github_wiki.ports import CommandResult, GitRunner
from wikiops.providers.github_wiki.process import SubprocessGitRunner

PY = sys.executable
GENEROUS = 30.0

posix_only = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")


def python(code: str, *args: str) -> list[str]:
    return [PY, "-c", code, *args]


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


def test_a_command_over_its_timeout_is_killed_and_reported() -> None:
    started = time.monotonic()

    result = run(python("import time; print('started', flush=True); time.sleep(60)"), timeout=1.0)

    assert result.timed_out is True
    assert result.returncode != 0
    assert "started" in result.stdout
    assert time.monotonic() - started < 20


def test_a_command_that_finishes_in_time_is_not_flagged() -> None:
    assert run(python("print('fast')"), timeout=GENEROUS).timed_out is False


@posix_only
def test_a_timeout_leaves_no_orphaned_grandchild(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    code = (
        "import subprocess, sys, time; "
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f"open({str(pid_file)!r}, 'w').write(str(p.pid)); "
        "time.sleep(60)"
    )

    started = time.monotonic()
    result = run(python(code), timeout=2.0)

    assert result.timed_out is True
    # A grandchild that kept the pipes open would block the runner until it exited.
    assert time.monotonic() - started < 20
    grandchild = int(pid_file.read_text())
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


# -- failure to start --------------------------------------------------------


def test_a_missing_executable_is_a_failed_result_not_an_exception() -> None:
    argv = ["wikiops-no-such-binary-xyz", "--version"]

    result = run(argv)

    assert result.returncode == 127
    assert result.argv == tuple(argv)
    assert result.stdout == ""
    assert "wikiops-no-such-binary-xyz" in result.stderr
    assert result.timed_out is False


def test_a_missing_working_directory_is_a_failed_result_not_an_exception(
    tmp_path: Path,
) -> None:
    result = run(python("pass"), cwd=tmp_path / "does-not-exist")

    assert result.returncode == 127
    assert "does-not-exist" in result.stderr
