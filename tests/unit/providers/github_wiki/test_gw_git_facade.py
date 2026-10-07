"""Unit tests for the ``Git`` facade of ``github_wiki`` (hermetic, ``FakeGitRunner``).

Covers GW-A3 (credentials only on network commands), GW-A10 (hooks disabled on
every network command), GW-S12 (no force push, porcelain), GW-S14 (bounded,
classified failures) and the repository-selection guards (cwd authority, base
environment, literal pathspecs).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tests.support.fake_git_runner import FakeGitRunner, RecordedCall
from wikiops.providers.github_wiki.auth import EnvTokenStrategy
from wikiops.providers.github_wiki.errors import GithubWikiError
from wikiops.providers.github_wiki.git import Git
from wikiops.providers.github_wiki.ports import CommandResult, GitTransport

TOKEN = "ghp_facade_SECRET_1"
REMOTE = "https://github.com/acme/platform.wiki.git"
TIMEOUT = 42.0

BASE_UNSET = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_COMMON_DIR",
    "GIT_PREFIX",
)


@dataclass
class CountingStrategy:
    """Credential strategy double: a fresh transport per call, counted."""

    label: str = "env:T"
    remote_url: str = REMOTE
    secrets: tuple[str, ...] = (TOKEN,)
    issued: int = field(default=0)

    def check_offline(self) -> None:
        return None

    def transport(self) -> GitTransport:
        self.issued += 1
        overrides = {
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {TOKEN}",
            "GIT_CONFIG_KEY_1": "credential.helper",
            "GIT_CONFIG_VALUE_1": "",
            "GIT_ASKPASS": None,
        }
        return GitTransport(self.remote_url, overrides, self.secrets, self.label)


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    path = tmp_path / "cache" / "wiki"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def runner() -> FakeGitRunner:
    fake = FakeGitRunner()
    fake.script(["git"])  # every git command succeeds unless a test scripts it otherwise
    return fake


@pytest.fixture
def strategy() -> CountingStrategy:
    return CountingStrategy()


@pytest.fixture
def git(runner: FakeGitRunner, strategy: CountingStrategy, workdir: Path) -> Git:
    return Git(runner, strategy, workdir=workdir, timeout=TIMEOUT)


def run_every_command(git: Git) -> None:
    git.local("status", "--porcelain=v1")
    git.local("add", paths=["Home.md"])
    git.ls_remote()
    git.clone("master")
    git.fetch("master")
    git.push("master")


def subcommand(argv: tuple[str, ...]) -> str:
    """The git subcommand of ``argv``, skipping global options added by the facade."""
    rest = list(argv[1:])
    while rest and rest[0] in {"-c", "--literal-pathspecs"}:
        rest = rest[2:] if rest[0] == "-c" else rest[1:]
    return rest[0]


def hooks_path(argv: tuple[str, ...]) -> str | None:
    values = [part for part in argv if part.startswith("core.hooksPath=")]
    return values[0].removeprefix("core.hooksPath=") if values else None


def test_the_facade_exposes_the_workdir_it_serves(git: Git, workdir: Path) -> None:
    assert git.workdir == workdir


# -- base environment on EVERY command ----------------------------------------------


def test_every_command_unsets_inherited_repository_selectors_and_pins_the_locale(
    git: Git, runner: FakeGitRunner, workdir: Path
) -> None:
    run_every_command(git)

    assert len(runner.calls) == 6
    for call in runner.calls:
        for name in BASE_UNSET:
            assert name in call.env_overrides, (call.argv, name)
            assert call.env_overrides[name] is None, (call.argv, name)
        assert call.env_overrides["LC_ALL"] == "C"
        assert call.env_overrides["LANGUAGE"] == ""
        assert call.env_overrides["GIT_CEILING_DIRECTORIES"] == str(workdir.parent)


def test_every_command_is_bounded_by_the_configured_timeout(git: Git, runner: FakeGitRunner) -> None:
    run_every_command(git)

    assert {call.timeout for call in runner.calls} == {TIMEOUT}


def test_every_command_is_a_plain_git_argv_without_dash_c_directory(
    git: Git, runner: FakeGitRunner
) -> None:
    run_every_command(git)

    for call in runner.calls:
        assert call.argv[0] == "git"
        assert "-C" not in call.argv


# -- cwd authority ---------------------------------------------------------------------


def test_cwd_is_the_workdir_except_for_ls_remote_and_clone(
    git: Git, runner: FakeGitRunner, workdir: Path
) -> None:
    run_every_command(git)

    cwd_by_op = {subcommand(call.argv): call.cwd for call in runner.calls}
    assert cwd_by_op == {
        "status": workdir,
        "add": workdir,
        "ls-remote": workdir.parent,
        "clone": workdir.parent,
        "fetch": workdir,
        "push": workdir,
    }


# -- network commands: shape, hooks, credentials -------------------------------------------


def test_network_commands_have_the_exact_argv_with_hooks_disabled_first(
    git: Git, runner: FakeGitRunner, workdir: Path
) -> None:
    git.ls_remote()
    git.clone("master")
    git.fetch("master")
    git.push("master")

    shapes = []
    for call in runner.calls:
        assert call.argv[:2] == ("git", "-c")
        assert call.argv[2].startswith("core.hooksPath=")
        shapes.append(call.argv[3:])
    assert shapes == [
        ("ls-remote", "--symref", REMOTE, "HEAD", "refs/heads/*"),
        ("clone", "--no-tags", "--origin", "origin", "--branch", "master", "--", REMOTE, str(workdir)),
        ("fetch", "--no-tags", "origin", "+refs/heads/master:refs/remotes/origin/master"),
        ("push", "--porcelain", "origin", "HEAD:refs/heads/master"),
    ]


def test_only_network_commands_carry_the_hooks_override(git: Git, runner: FakeGitRunner) -> None:
    run_every_command(git)

    with_hooks = {subcommand(call.argv) for call in runner.calls if hooks_path(call.argv)}
    without_hooks = [call for call in runner.calls if hooks_path(call.argv) is None]
    assert with_hooks == {"ls-remote", "clone", "fetch", "push"}
    assert {subcommand(call.argv) for call in without_hooks} == {"status", "add"}
    assert not any("-c" in call.argv for call in without_hooks)


def test_network_commands_carry_the_transport_and_drop_inherited_config_parameters(
    git: Git, runner: FakeGitRunner
) -> None:
    git.ls_remote()
    git.clone("master")
    git.fetch("master")
    git.push("master")

    for call in runner.calls:
        assert call.env_overrides["GIT_CONFIG_COUNT"] == "2"
        assert call.env_overrides["GIT_CONFIG_VALUE_0"] == f"AUTHORIZATION: basic {TOKEN}"
        assert call.env_overrides["GIT_CONFIG_PARAMETERS"] is None
        assert call.env_overrides["GIT_ASKPASS"] is None


def test_local_commands_carry_no_credentials_and_no_config_overrides(
    git: Git, runner: FakeGitRunner, strategy: CountingStrategy
) -> None:
    git.ls_remote()  # a network command first: its transport must not leak
    git.local("status", "--porcelain=v1")
    git.local("add", paths=["Home.md"])
    git.local("commit", "-m", "msg", paths=["Home.md"], op="commit")

    local_calls = runner.calls[1:]
    assert len(local_calls) == 3
    for call in local_calls:
        assert not [name for name in call.env_overrides if name.startswith("GIT_CONFIG")]
        assert "GIT_ASKPASS" not in call.env_overrides
        assert TOKEN not in repr(call.env_overrides) + repr(call.argv)
    assert strategy.issued == 1


def test_each_network_command_requests_its_own_transport(
    git: Git, runner: FakeGitRunner, strategy: CountingStrategy
) -> None:
    git.fetch("master")
    git.fetch("master")
    git.push("master")

    assert strategy.issued == 3
    assert len(runner.calls) == 3


def test_local_commands_never_request_a_transport(git: Git, strategy: CountingStrategy) -> None:
    git.local("status")
    git.local("rev-parse", "--verify", "HEAD", check=False)

    assert strategy.issued == 0


def test_a_credential_failure_stops_the_network_command_before_git_runs(
    runner: FakeGitRunner, workdir: Path
) -> None:
    strategy = EnvTokenStrategy("github.com", "acme/platform", "T", environ={})
    git = Git(runner, strategy, workdir=workdir, timeout=TIMEOUT)

    with pytest.raises(GithubWikiError) as caught:
        git.fetch("master")

    assert caught.value.code == "auth.env_missing"
    assert runner.calls == []


# -- the empty hooks directory ------------------------------------------------------------


def test_hooks_directory_is_an_empty_temporary_directory_removed_after_each_command(
    strategy: CountingStrategy, workdir: Path
) -> None:
    seen: list[tuple[str, bool, list[str]]] = []

    def respond(call: RecordedCall) -> CommandResult:
        directory = hooks_path(call.argv)
        assert directory is not None
        seen.append((directory, os.path.isdir(directory), os.listdir(directory)))
        return CommandResult(call.argv, 0, "", "")

    runner = FakeGitRunner()
    runner.script_with(["git"], respond)
    git = Git(runner, strategy, workdir=workdir, timeout=TIMEOUT)

    git.fetch("master")
    git.fetch("master")

    assert [(is_dir, entries) for _, is_dir, entries in seen] == [(True, []), (True, [])]
    assert seen[0][0] != seen[1][0]
    assert Path(seen[0][0]).name.startswith("wikiops-nohooks-")
    assert not any(os.path.exists(path) for path, _, _ in seen)
    assert Path(seen[0][0]).parent != workdir


@pytest.mark.parametrize(
    "script",
    [
        {"returncode": 1, "stderr": "fatal: boom"},
        {"returncode": 0, "timed_out": True},
    ],
    ids=["failure", "timeout"],
)
def test_hooks_directory_is_removed_even_when_the_command_fails(
    strategy: CountingStrategy, workdir: Path, script: dict
) -> None:
    runner = FakeGitRunner()
    runner.script(["git"], **script)
    git = Git(runner, strategy, workdir=workdir, timeout=TIMEOUT)

    with pytest.raises(GithubWikiError):
        git.fetch("master")

    directory = hooks_path(runner.calls[0].argv)
    assert directory is not None and not os.path.exists(directory)


# -- literal pathspecs -----------------------------------------------------------------


def test_paths_come_after_a_double_dash_with_literal_pathspecs(git: Git, runner: FakeGitRunner) -> None:
    git.local("add", paths=["a*b.md", ":(top)x.md", "-weird.md"])

    assert runner.argvs == [
        ("git", "--literal-pathspecs", "add", "--", "a*b.md", ":(top)x.md", "-weird.md")
    ]


def test_commands_without_paths_keep_a_plain_argv(git: Git, runner: FakeGitRunner) -> None:
    git.local("status", "--porcelain=v1", "-z")

    assert runner.argvs == [("git", "status", "--porcelain=v1", "-z")]


@pytest.mark.parametrize("subcommand_name", ["add", "commit"])
def test_empty_paths_are_refused_because_a_pathless_command_acts_on_everything(
    git: Git, runner: FakeGitRunner, subcommand_name: str
) -> None:
    with pytest.raises(ValueError, match="paths must not be empty"):
        git.local(subcommand_name, paths=[])

    assert runner.calls == []


# -- push is never forced ---------------------------------------------------------------


@pytest.mark.parametrize("branch", ["master", "feature/docs"])
def test_push_names_the_explicit_ref_and_never_forces(
    git: Git, runner: FakeGitRunner, branch: str
) -> None:
    git.push(branch)

    argv = runner.argvs[0]
    assert argv[3:] == ("push", "--porcelain", "origin", f"HEAD:refs/heads/{branch}")
    assert not {"--force", "-f", "--force-with-lease", "--mirror", "--delete"} & set(argv)
    assert not any(part.startswith("--force") or part.startswith("+") for part in argv[3:])


# -- successful results are returned ------------------------------------------------------


def test_a_successful_command_returns_its_result_unchanged(workdir: Path, strategy: CountingStrategy) -> None:
    runner = FakeGitRunner()
    runner.script(["git"], stdout="ref: refs/heads/master\tHEAD\n")
    git = Git(runner, strategy, workdir=workdir, timeout=TIMEOUT)

    result = git.ls_remote()

    assert result.stdout == "ref: refs/heads/master\tHEAD\n"
    assert result.returncode == 0


def test_local_extra_environment_is_applied_on_top_of_the_base(
    git: Git, runner: FakeGitRunner
) -> None:
    git.local("commit", "-m", "msg", env={"GIT_AUTHOR_NAME": "wikiops"}, op="commit")

    overrides = runner.calls[0].env_overrides
    assert overrides["GIT_AUTHOR_NAME"] == "wikiops"
    assert overrides["LC_ALL"] == "C"


def test_local_stdin_reaches_the_runner(git: Git, runner: FakeGitRunner) -> None:
    git.local("hash-object", "--stdin", stdin="payload")

    assert runner.calls[0].stdin == "payload"


# -- failures: classification, redaction, timeouts ---------------------------------------


def failing(strategy: CountingStrategy, workdir: Path, **script: object) -> tuple[Git, FakeGitRunner]:
    runner = FakeGitRunner()
    runner.script(["git"], **script)
    return Git(runner, strategy, workdir=workdir, timeout=TIMEOUT), runner


@pytest.mark.parametrize(
    ("stderr", "code"),
    [
        ("remote: Repository not found.\nfatal: repository 'x' not found", "wiki.not_initialized"),
        ("fatal: Authentication failed for 'https://github.com/'", "auth.rejected"),
        ("fatal: unable to access: Could not resolve host: github.com", "network.unreachable"),
        ("Host key verification failed.", "auth.ssh_host_key"),
        ("fatal: something odd", "sync.git_failed"),
    ],
)
def test_network_failures_are_classified_with_remote_and_auth_context(
    strategy: CountingStrategy, workdir: Path, stderr: str, code: str
) -> None:
    git, _ = failing(strategy, workdir, returncode=128, stderr=stderr)

    with pytest.raises(GithubWikiError) as caught:
        git.ls_remote()

    assert caught.value.code == code
    assert caught.value.context["op"] == "ls-remote"
    assert caught.value.context["remote"] == REMOTE
    assert caught.value.context["auth"] == "env:T"
    assert caught.value.context["workdir"] == str(workdir)


def test_failure_messages_never_carry_the_token_or_its_header(
    strategy: CountingStrategy, workdir: Path
) -> None:
    stderr = (
        f"fatal: Authentication failed\nAuthorization: basic {TOKEN}\n"
        f"remote url https://x-access-token:{TOKEN}@github.com/acme/platform.wiki.git"
    )
    git, _ = failing(strategy, workdir, returncode=128, stderr=stderr)

    with pytest.raises(GithubWikiError) as caught:
        git.fetch("master")

    rendered = str(caught.value) + repr(caught.value.context)
    assert TOKEN not in rendered
    assert "***" in rendered


@pytest.mark.parametrize("method", ["ls_remote", "fetch", "clone"])
def test_a_timed_out_network_command_is_sync_timeout_naming_the_operation(
    strategy: CountingStrategy, workdir: Path, method: str
) -> None:
    git, _ = failing(strategy, workdir, returncode=-9, timed_out=True)
    call = getattr(git, method)

    with pytest.raises(GithubWikiError) as caught:
        call() if method == "ls_remote" else call("master")

    operation = method.replace("_", "-")
    assert caught.value.code == "sync.timeout"
    assert operation in str(caught.value)


def test_a_timed_out_push_is_push_failed_with_the_timeout_cause(
    strategy: CountingStrategy, workdir: Path
) -> None:
    git, _ = failing(strategy, workdir, returncode=-9, timed_out=True)

    with pytest.raises(GithubWikiError) as caught:
        git.push("master")

    assert caught.value.code == "push.failed"
    assert "timed out" in str(caught.value)


def test_a_rejected_push_is_read_from_the_porcelain_output(
    strategy: CountingStrategy, workdir: Path
) -> None:
    porcelain = "To https://github.com/acme/platform.wiki.git\n!\tHEAD:refs/heads/master\t[rejected] (fetch first)\nDone\n"
    git, _ = failing(strategy, workdir, returncode=1, stdout=porcelain, stderr="hint: localized text")

    with pytest.raises(GithubWikiError) as caught:
        git.push("master", context={"sha": "abc1234"})

    assert caught.value.code == "push.rejected"
    assert caught.value.context["sha"] == "abc1234"
    assert caught.value.context["workdir"] == str(workdir)


def test_local_failures_are_classified_and_redacted_without_a_transport(
    strategy: CountingStrategy, workdir: Path
) -> None:
    git, _ = failing(strategy, workdir, returncode=1, stderr="Author identity unknown\nfatal: unable to auto-detect email address")

    with pytest.raises(GithubWikiError) as caught:
        git.local("commit", "-m", "msg", op="commit")

    assert caught.value.code == "commit.identity_missing"
    assert caught.value.context["workdir"] == str(workdir)
    assert "remote" not in caught.value.context
    assert strategy.issued == 0


def test_local_failure_defaults_the_operation_to_the_subcommand(
    strategy: CountingStrategy, workdir: Path
) -> None:
    git, _ = failing(strategy, workdir, returncode=1, stderr="fatal: weird")

    with pytest.raises(GithubWikiError) as caught:
        git.local("rev-parse", "--show-toplevel")

    assert caught.value.code == "sync.git_failed"
    assert caught.value.context["op"] == "rev-parse"


def test_local_failure_with_literal_pathspecs_still_names_the_subcommand(
    strategy: CountingStrategy, workdir: Path
) -> None:
    git, _ = failing(strategy, workdir, returncode=1, stderr="fatal: weird")

    with pytest.raises(GithubWikiError) as caught:
        git.local("add", paths=["x.md"])

    assert caught.value.context["op"] == "add"


def test_check_false_returns_the_failed_result_for_the_caller_to_interpret(
    strategy: CountingStrategy, workdir: Path
) -> None:
    git, _ = failing(strategy, workdir, returncode=1, stderr="fatal: Needed a single revision")

    result = git.local("rev-parse", "--verify", "HEAD", check=False)

    assert result.returncode == 1
    assert "single revision" in result.stderr


def test_a_timed_out_local_command_is_a_failure_even_when_its_exit_code_is_zero(
    strategy: CountingStrategy, workdir: Path
) -> None:
    git, _ = failing(strategy, workdir, returncode=0, timed_out=True)

    with pytest.raises(GithubWikiError) as caught:
        git.local("status")

    assert caught.value.code == "sync.timeout"
