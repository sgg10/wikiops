"""Self-tests of ``FakeGitRunner``: the hermetic stand-in for ``GitRunner``."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.support.fake_git_runner import FakeGitRunner, RecordedCall, UnscriptedCallError
from wikiops.providers.github_wiki.ports import CommandResult, GitRunner


def call(
    runner: FakeGitRunner,
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str | None] | None = None,
    timeout: float = 30.0,
    stdin: str | None = None,
) -> CommandResult:
    return runner.run(argv, cwd=cwd, env_overrides=env or {}, timeout=timeout, stdin=stdin)


def test_the_fake_satisfies_the_git_runner_port() -> None:
    assert isinstance(FakeGitRunner(), GitRunner)


# -- scripted responses ---------------------------------------------------------


def test_a_scripted_prefix_answers_with_the_scripted_result() -> None:
    runner = FakeGitRunner()
    runner.script(["git", "status"], stdout="clean\n", stderr="warn\n", returncode=0)

    result = call(runner, ["git", "status", "--porcelain"])

    assert result == CommandResult(
        argv=("git", "status", "--porcelain"),
        returncode=0,
        stdout="clean\n",
        stderr="warn\n",
        timed_out=False,
    )


def test_a_scripted_failure_and_a_timeout_are_reproduced() -> None:
    runner = FakeGitRunner()
    runner.script(["git", "fetch"], returncode=128, stderr="fatal: boom")
    runner.script(["git", "push"], returncode=-9, timed_out=True)

    assert call(runner, ["git", "fetch"]).returncode == 128
    assert call(runner, ["git", "fetch"]).stderr == "fatal: boom"
    assert call(runner, ["git", "push"]).timed_out is True


def test_the_longest_matching_prefix_wins_regardless_of_registration_order() -> None:
    runner = FakeGitRunner()
    runner.script(["git"], stdout="generic")
    runner.script(["git", "rev-parse", "--verify"], stdout="specific")

    assert call(runner, ["git", "rev-parse", "--verify", "HEAD"]).stdout == "specific"
    assert call(runner, ["git", "rev-parse", "--show-toplevel"]).stdout == "generic"


def test_a_prefix_must_match_whole_argv_elements_not_substrings() -> None:
    runner = FakeGitRunner()
    runner.script(["git", "push"], stdout="pushed")

    with pytest.raises(UnscriptedCallError):
        call(runner, ["git", "pushed-by-mistake"])


def test_a_prefix_longer_than_the_argv_never_matches() -> None:
    runner = FakeGitRunner()
    runner.script(["git", "status", "--porcelain"], stdout="x")

    with pytest.raises(UnscriptedCallError):
        call(runner, ["git", "status"])


def test_limited_responses_are_consumed_in_order_then_the_next_rule_answers() -> None:
    runner = FakeGitRunner()
    runner.script(["git", "fetch"], returncode=128, stderr="first", times=1)
    runner.script(["git", "fetch"], returncode=0, stdout="second", times=1)
    runner.script(["git", "fetch"], stdout="forever")

    outputs = [call(runner, ["git", "fetch"]) for _ in range(4)]

    assert [(r.returncode, r.stderr or r.stdout) for r in outputs] == [
        (128, "first"),
        (0, "second"),
        (0, "forever"),
        (0, "forever"),
    ]


def test_an_exhausted_limited_response_leaves_the_call_unscripted() -> None:
    runner = FakeGitRunner()
    runner.script(["git", "fetch"], times=1)

    call(runner, ["git", "fetch"])

    with pytest.raises(UnscriptedCallError):
        call(runner, ["git", "fetch"])


def test_a_dynamic_responder_computes_the_result_from_the_recorded_call() -> None:
    runner = FakeGitRunner()
    runner.script_with(
        ["gh", "auth", "token"],
        lambda recorded: CommandResult(
            recorded.argv, 0, f"token-for-{recorded.argv[-1]}\n", ""
        ),
    )

    first = call(runner, ["gh", "auth", "token", "alice"])
    second = call(runner, ["gh", "auth", "token", "bob"])

    assert (first.stdout, second.stdout) == ("token-for-alice\n", "token-for-bob\n")


# -- unscripted calls -------------------------------------------------------------


def test_an_unscripted_call_raises_naming_the_argv_and_is_still_recorded() -> None:
    runner = FakeGitRunner()
    runner.script(["git", "status"])

    with pytest.raises(UnscriptedCallError, match=r"git.*clone.*url"):
        call(runner, ["git", "clone", "url"])

    assert [recorded.argv for recorded in runner.calls] == [("git", "clone", "url")]


def test_the_unscripted_error_is_an_assertion_error() -> None:
    with pytest.raises(AssertionError):
        call(FakeGitRunner(), ["git", "anything"])


# -- recording -----------------------------------------------------------------------


def test_every_call_is_recorded_with_argv_cwd_overrides_timeout_and_stdin(
    tmp_path: Path,
) -> None:
    runner = FakeGitRunner()
    runner.script(["git"])

    call(
        runner,
        ["git", "fetch", "origin"],
        cwd=tmp_path,
        env={"GIT_DIR": None, "LC_ALL": "C"},
        timeout=12.5,
        stdin="payload",
    )
    call(runner, ["git", "status"])

    assert runner.calls == [
        RecordedCall(
            argv=("git", "fetch", "origin"),
            cwd=tmp_path,
            env_overrides={"GIT_DIR": None, "LC_ALL": "C"},
            timeout=12.5,
            stdin="payload",
        ),
        RecordedCall(
            argv=("git", "status"), cwd=None, env_overrides={}, timeout=30.0, stdin=None
        ),
    ]


def test_recorded_overrides_are_a_snapshot_not_a_live_reference() -> None:
    runner = FakeGitRunner()
    runner.script(["git"])
    env: dict[str, str | None] = {"A": "1"}

    call(runner, ["git", "status"], env=env)
    env["A"] = "changed"

    assert runner.calls[0].env_overrides == {"A": "1"}


def test_calls_matching_filters_the_recording_by_prefix() -> None:
    runner = FakeGitRunner()
    runner.script(["git"])
    for argv in (["git", "fetch"], ["git", "status"], ["git", "fetch", "--tags"]):
        call(runner, argv)

    assert [c.argv for c in runner.calls_matching(["git", "fetch"])] == [
        ("git", "fetch"),
        ("git", "fetch", "--tags"),
    ]
    assert runner.calls_matching(["git", "push"]) == []


def test_argvs_lists_every_recorded_argv_in_order() -> None:
    runner = FakeGitRunner()
    runner.script(["git"])
    call(runner, ["git", "a"])
    call(runner, ["git", "b"])

    assert runner.argvs == [("git", "a"), ("git", "b")]


def test_a_fresh_fake_has_recorded_nothing() -> None:
    runner = FakeGitRunner()

    assert runner.calls == []
    assert runner.argvs == []


# -- hermetic ----------------------------------------------------------------------------


def test_the_fake_never_spawns_a_process(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("a process was spawned")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    runner = FakeGitRunner()
    runner.script(["git", "fetch"], stdout="fake")

    assert call(runner, ["git", "fetch"]).stdout == "fake"
