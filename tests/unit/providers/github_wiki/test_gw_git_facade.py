"""Unit tests for the ``Git`` facade of ``github_wiki`` (hermetic, ``FakeGitRunner``).

Covers GW-A3 (credentials only on network commands), GW-A10 (hooks disabled on
every network command), GW-S12 (no force push, porcelain), GW-S14 (bounded,
classified failures) and the repository-selection guards (cwd authority, base
environment, literal pathspecs).
"""

from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tests.support.fake_git_runner import FakeGitRunner, RecordedCall
from wikiops.providers.github_wiki.auth import EnvTokenStrategy
from wikiops.providers.github_wiki.errors import GithubWikiError
from wikiops.providers.github_wiki import git as git_module
from wikiops.providers.github_wiki.git import Git
from wikiops.providers.github_wiki.ports import LOCALE_PIN, CommandResult, GitTransport

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


def test_the_locale_pin_cannot_be_overridden_by_a_transport_or_a_caller(
    runner: FakeGitRunner, workdir: Path
) -> None:
    class LocaleOverridingStrategy(CountingStrategy):
        def transport(self) -> GitTransport:
            base = super().transport()
            return GitTransport(
                base.remote_url,
                {**base.env_overrides, "LC_ALL": "de_DE.UTF-8", "LANGUAGE": "de"},
                base.secrets,
                base.label,
            )

    git = Git(runner, LocaleOverridingStrategy(), workdir=workdir, timeout=TIMEOUT)

    git.fetch("master")
    git.local("status", env={"LC_ALL": "fr_FR.UTF-8", "LANGUAGE": "fr", "GIT_AUTHOR_NAME": "me"})

    assert [(call.env_overrides["LC_ALL"], call.env_overrides["LANGUAGE"]) for call in runner.calls] == [
        ("C", ""),
        ("C", ""),
    ]
    assert runner.calls[1].env_overrides["GIT_AUTHOR_NAME"] == "me"  # other overrides still apply


def test_the_facade_pins_exactly_the_shared_locale_definition(
    git: Git, runner: FakeGitRunner
) -> None:
    run_every_command(git)

    for call in runner.calls:
        assert {name: call.env_overrides[name] for name in LOCALE_PIN} == dict(LOCALE_PIN)


def test_a_local_command_needs_a_subcommand(git: Git, runner: FakeGitRunner) -> None:
    with pytest.raises(ValueError, match="subcommand"):
        git.local()

    assert runner.calls == []


def test_a_local_command_with_only_paths_still_needs_a_subcommand(
    git: Git, runner: FakeGitRunner
) -> None:
    with pytest.raises(ValueError, match="subcommand"):
        git.local(paths=["Home.md"])

    assert runner.calls == []


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
    clone_destination = runner.calls[1].argv[-1]  # a sibling staging directory, see below
    assert shapes == [
        ("ls-remote", "--symref", REMOTE, "HEAD", "refs/heads/*"),
        ("clone", "--no-tags", "--origin", "origin", "--branch", "master", "--", REMOTE, clone_destination),
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


# -- clone atomicity: a failed or timed-out clone never leaves a partial workdir --------


def clone_into_destination(files: dict[str, str], *, returncode: int = 0, timed_out: bool = False):
    """Responder that behaves like git: it writes into the clone destination, then answers."""

    def respond(call: RecordedCall) -> CommandResult:
        destination = Path(call.argv[-1])
        assert destination.is_dir() and not any(destination.iterdir())  # git needs an empty dir
        for name, content in files.items():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        return CommandResult(call.argv, returncode, "", "fatal: boom" if returncode else "", timed_out)

    return respond


def clone_git(responder, strategy: CountingStrategy, workdir: Path) -> tuple[Git, FakeGitRunner]:
    runner = FakeGitRunner()
    runner.script_with(["git"], responder)
    return Git(runner, strategy, workdir=workdir, timeout=TIMEOUT), runner


def test_clone_runs_in_a_sibling_staging_directory_and_publishes_it_atomically(
    strategy: CountingStrategy, tmp_path: Path
) -> None:
    workdir = tmp_path / "cache" / "wiki"
    workdir.parent.mkdir()
    git, runner = clone_git(clone_into_destination({".git/HEAD": "ref", "Home.md": "# Home"}), strategy, workdir)

    git.clone("master")

    destination = Path(runner.calls[0].argv[-1])
    assert destination != workdir
    assert destination.parent == workdir.parent  # same filesystem: the rename is atomic
    assert (workdir / "Home.md").read_text() == "# Home"
    assert (workdir / ".git" / "HEAD").is_file()
    assert [path.name for path in workdir.parent.iterdir()] == ["wiki"]  # staging is gone


def test_clone_replaces_an_existing_empty_workdir(strategy: CountingStrategy, tmp_path: Path) -> None:
    workdir = tmp_path / "cache" / "wiki"
    workdir.mkdir(parents=True)
    git, _ = clone_git(clone_into_destination({"Home.md": "# Home"}), strategy, workdir)

    git.clone("master")

    assert [path.name for path in workdir.iterdir()] == ["Home.md"]
    assert [path.name for path in workdir.parent.iterdir()] == ["wiki"]


@pytest.mark.parametrize(
    ("returncode", "timed_out", "code"),
    [(128, False, "sync.git_failed"), (0, True, "sync.timeout")],
    ids=["failure", "timeout"],
)
def test_a_failed_or_timed_out_clone_leaves_no_workdir_and_no_staging_directory(
    strategy: CountingStrategy, tmp_path: Path, returncode: int, timed_out: bool, code: str
) -> None:
    workdir = tmp_path / "cache" / "wiki"
    workdir.parent.mkdir()
    partial = {".git/objects/pack/partial": "half a pack", "Home.md": "# half"}
    git, _ = clone_git(
        clone_into_destination(partial, returncode=returncode, timed_out=timed_out), strategy, workdir
    )

    with pytest.raises(GithubWikiError) as caught:
        git.clone("master")

    assert caught.value.code == code
    assert not workdir.exists()
    assert list(workdir.parent.iterdir()) == []


def test_a_failed_clone_keeps_an_existing_empty_workdir_untouched(
    strategy: CountingStrategy, tmp_path: Path
) -> None:
    workdir = tmp_path / "cache" / "wiki"
    workdir.mkdir(parents=True)
    git, _ = clone_git(clone_into_destination({"Home.md": "x"}, returncode=128), strategy, workdir)

    with pytest.raises(GithubWikiError):
        git.clone("master")

    assert workdir.is_dir() and list(workdir.iterdir()) == []
    assert [path.name for path in workdir.parent.iterdir()] == ["wiki"]


def test_cloning_into_a_non_empty_workdir_is_refused_before_any_network_command(
    strategy: CountingStrategy, tmp_path: Path
) -> None:
    workdir = tmp_path / "cache" / "wiki"
    workdir.mkdir(parents=True)
    (workdir / "mine.txt").write_text("keep")
    git, runner = clone_git(clone_into_destination({}), strategy, workdir)

    with pytest.raises(GithubWikiError) as caught:
        git.clone("master")

    assert caught.value.code == "sync.workdir_not_clone"
    assert runner.calls == []
    assert strategy.issued == 0
    assert (workdir / "mine.txt").read_text() == "keep"
    assert [path.name for path in workdir.parent.iterdir()] == ["wiki"]


def test_a_workdir_that_fills_up_during_the_clone_is_not_overwritten(
    strategy: CountingStrategy, tmp_path: Path
) -> None:
    workdir = tmp_path / "cache" / "wiki"
    workdir.parent.mkdir()
    cloned = clone_into_destination({"Home.md": "# remote"})

    def respond(call: RecordedCall) -> CommandResult:
        result = cloned(call)
        workdir.mkdir()  # someone else created the directory in the meantime
        (workdir / "mine.txt").write_text("keep")
        return result

    git, _ = clone_git(respond, strategy, workdir)

    with pytest.raises(GithubWikiError) as caught:
        git.clone("master")

    assert caught.value.code == "sync.workdir_not_clone"
    assert [path.name for path in workdir.iterdir()] == ["mine.txt"]
    assert [path.name for path in workdir.parent.iterdir()] == ["wiki"]


def test_a_missing_parent_directory_is_workdir_unusable_and_runs_nothing(
    strategy: CountingStrategy, tmp_path: Path
) -> None:
    workdir = tmp_path / "no-such-parent" / "wiki"
    git, runner = clone_git(clone_into_destination({}), strategy, workdir)

    with pytest.raises(GithubWikiError) as caught:
        git.clone("master")

    assert caught.value.code == "workdir.unusable"
    assert runner.calls == []
    assert not workdir.parent.exists()


def test_each_clone_uses_its_own_staging_directory(strategy: CountingStrategy, tmp_path: Path) -> None:
    destinations: list[str] = []
    inner = clone_into_destination({"Home.md": "x"})

    def respond(call: RecordedCall) -> CommandResult:
        destinations.append(call.argv[-1])
        return inner(call)

    for name in ("one", "two"):
        workdir = tmp_path / "cache" / name
        workdir.parent.mkdir(exist_ok=True)
        git, _ = clone_git(respond, strategy, workdir)
        git.clone("master")

    assert len(set(destinations)) == 2


# -- clone staging: deterministic names, stale leftovers cleaned, symlinks refused ------

STALE = 3600.0 * 24  # far older than the grace period before a staging directory counts as abandoned

posix_only = pytest.mark.skipif(os.name != "posix", reason="directory locks are POSIX-only")


def staging_directory(parent: Path, workdir_name: str, token: str = "0123456789ab", *, age: float = STALE) -> Path:
    """A leftover ``.<name>.clone-<token>`` directory with content, ``age`` seconds old."""
    path = parent / f".{workdir_name}.clone-{token}"
    (path / ".git").mkdir(parents=True)
    (path / "Home.md").write_text("half a clone")
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))
    return path


def names(directory: Path) -> set[str]:
    return {path.name for path in directory.iterdir()}


def clone_ok(workdir: Path, strategy: CountingStrategy) -> tuple[Git, FakeGitRunner]:
    return clone_git(clone_into_destination({"Home.md": "# Home"}), strategy, workdir)


def test_the_staging_directory_has_a_deterministic_prefix_and_a_random_token(
    strategy: CountingStrategy, tmp_path: Path
) -> None:
    workdir = tmp_path / "cache" / "wiki"
    workdir.parent.mkdir()
    git, runner = clone_ok(workdir, strategy)

    git.clone("master")

    assert re.fullmatch(r"\.wiki\.clone-[0-9a-f]{12}", Path(runner.calls[0].argv[-1]).name)


def test_a_stale_staging_directory_of_this_workdir_is_removed_by_the_next_clone(
    strategy: CountingStrategy, tmp_path: Path
) -> None:
    workdir = tmp_path / "cache" / "wiki"
    workdir.parent.mkdir()
    leftovers = [
        staging_directory(workdir.parent, "wiki", "0123456789ab"),
        staging_directory(workdir.parent, "wiki", "ffffffffffff"),
    ]
    git, _ = clone_ok(workdir, strategy)

    git.clone("master")

    assert all(not leftover.exists() for leftover in leftovers)
    assert names(workdir.parent) == {"wiki"}
    assert (workdir / "Home.md").read_text() == "# Home"


def test_cleanup_only_touches_directories_with_this_workdirs_exact_staging_name(
    strategy: CountingStrategy, tmp_path: Path
) -> None:
    parent = tmp_path / "cache"
    workdir = parent / "wiki"
    parent.mkdir()
    bystanders = [
        staging_directory(parent, "other"),  # another workdir's staging
        staging_directory(parent, "wiki.clone-x"),  # a longer workdir name sharing our prefix
        staging_directory(parent, "wiki", "not-a-token"),  # not our token shape
        staging_directory(parent, "wiki", "0123456789ABCDEF"[:12]),  # upper-case hex is not ours
        staging_directory(parent, "wiki", "0123456789abc"),  # 13 characters
        staging_directory(parent, "wiki-backup", "0123456789ab"),
    ]
    (parent / "notes.txt").write_text("mine")
    git, _ = clone_ok(workdir, strategy)

    git.clone("master")

    assert all(path.is_dir() and (path / "Home.md").is_file() for path in bystanders)
    assert (parent / "notes.txt").read_text() == "mine"


def test_cleanup_keeps_a_recent_staging_directory_that_may_still_be_in_use(
    strategy: CountingStrategy, tmp_path: Path
) -> None:
    workdir = tmp_path / "cache" / "wiki"
    workdir.parent.mkdir()
    fresh = staging_directory(workdir.parent, "wiki", age=1.0)
    git, _ = clone_ok(workdir, strategy)

    git.clone("master")

    assert (fresh / "Home.md").is_file()


def test_cleanup_never_follows_or_removes_a_symlink_or_file_with_a_staging_name(
    strategy: CountingStrategy, tmp_path: Path
) -> None:
    parent = tmp_path / "cache"
    workdir = parent / "wiki"
    parent.mkdir()
    target = tmp_path / "precious"
    target.mkdir()
    (target / "keep.txt").write_text("keep")
    link = parent / ".wiki.clone-0123456789ab"
    link.symlink_to(target, target_is_directory=True)
    plain_file = parent / ".wiki.clone-ffffffffffff"
    plain_file.write_text("a file, not a directory")
    git, _ = clone_ok(workdir, strategy)

    git.clone("master")

    assert (target / "keep.txt").read_text() == "keep"
    assert link.is_symlink()
    assert plain_file.read_text() == "a file, not a directory"


@posix_only
def test_cleanup_never_removes_a_staging_directory_another_run_holds_locked_until_it_ends(
    strategy: CountingStrategy, tmp_path: Path
) -> None:
    import fcntl

    workdir = tmp_path / "cache" / "wiki"
    workdir.parent.mkdir()
    busy = staging_directory(workdir.parent, "wiki")  # old, but a live clone holds it
    holder = os.open(busy, os.O_RDONLY)
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    git, _ = clone_ok(workdir, strategy)
    try:
        git.clone("master")
        assert (busy / "Home.md").is_file()  # held: not stale, however old
    finally:
        os.close(holder)  # the holder ended or was killed: the OS drops its lock
    shutil.rmtree(workdir)

    git.clone("master")

    assert names(workdir.parent) == {"wiki"}  # now abandoned, so cleaned


@posix_only
def test_the_staging_directory_of_a_running_clone_is_locked_and_released_afterwards(
    strategy: CountingStrategy, tmp_path: Path
) -> None:
    import fcntl

    workdir = tmp_path / "cache" / "wiki"
    workdir.parent.mkdir()
    inner = clone_into_destination({"Home.md": "# Home"})
    observed: list[bool] = []

    def respond(call: RecordedCall) -> CommandResult:
        probe = os.open(call.argv[-1], os.O_RDONLY)
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            observed.append(False)  # we could lock it: nobody held it
        except BlockingIOError:
            observed.append(True)  # held by the running clone
        finally:
            os.close(probe)
        return inner(call)

    git, _ = clone_git(respond, strategy, workdir)

    git.clone("master")

    assert observed == [True]
    probe = os.open(workdir, os.O_RDONLY)  # the descriptor was released: no leaked lock
    try:
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(probe)


@posix_only
def test_the_directory_lock_helper_reports_free_held_and_unlockable_paths(tmp_path: Path) -> None:
    free = tmp_path / "free"
    free.mkdir()
    a_file = tmp_path / "file"
    a_file.write_text("x")
    link = tmp_path / "link"
    link.symlink_to(free, target_is_directory=True)

    descriptor = git_module._try_lock_directory(free)
    assert descriptor is not None
    try:
        assert git_module._try_lock_directory(free) is None  # held by the first descriptor
    finally:
        os.close(descriptor)
    again = git_module._try_lock_directory(free)
    assert again is not None  # released with the descriptor
    os.close(again)
    assert git_module._try_lock_directory(a_file) is None  # not a directory
    assert git_module._try_lock_directory(link) is None  # a symlink is never followed
    assert git_module._try_lock_directory(tmp_path / "missing") is None


def test_when_directories_cannot_be_locked_the_clone_works_and_nothing_is_cleaned(
    strategy: CountingStrategy, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(git_module, "_try_lock_directory", lambda path: None)
    workdir = tmp_path / "cache" / "wiki"
    workdir.parent.mkdir()
    leftover = staging_directory(workdir.parent, "wiki")  # cannot be proven abandoned
    git, _ = clone_ok(workdir, strategy)

    git.clone("master")

    assert (workdir / "Home.md").read_text() == "# Home"
    assert (leftover / "Home.md").is_file()


def test_a_staging_directory_that_vanishes_during_cleanup_is_skipped(
    strategy: CountingStrategy, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = tmp_path / "cache" / "wiki"
    workdir.parent.mkdir()
    staging_directory(workdir.parent, "wiki")
    real_lstat = Path.lstat

    def vanishing(self: Path, *args: object, **kwargs: object):
        if self.name.startswith(".wiki.clone-"):
            raise FileNotFoundError(self.name)
        return real_lstat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", vanishing)
    git, _ = clone_ok(workdir, strategy)

    git.clone("master")

    assert (workdir / "Home.md").read_text() == "# Home"


def test_a_refused_clone_target_cleans_nothing(strategy: CountingStrategy, tmp_path: Path) -> None:
    workdir = tmp_path / "cache" / "wiki"
    workdir.mkdir(parents=True)
    (workdir / "mine.txt").write_text("keep")
    leftover = staging_directory(workdir.parent, "wiki")
    git, runner = clone_ok(workdir, strategy)

    with pytest.raises(GithubWikiError) as caught:
        git.clone("master")

    assert caught.value.code == "sync.workdir_not_clone"
    assert (leftover / "Home.md").is_file()
    assert runner.calls == []


@pytest.mark.parametrize("target", ["empty-directory", "non-empty-directory", "dangling"])
def test_a_workdir_that_is_a_symlink_is_unusable_before_any_clone(
    strategy: CountingStrategy, tmp_path: Path, target: str
) -> None:
    parent = tmp_path / "cache"
    parent.mkdir()
    real = tmp_path / "real"
    if target != "dangling":
        real.mkdir()
    if target == "non-empty-directory":
        (real / "mine.txt").write_text("keep")
    workdir = parent / "wiki"
    workdir.symlink_to(real, target_is_directory=True)
    git, runner = clone_ok(workdir, strategy)

    with pytest.raises(GithubWikiError) as caught:
        git.clone("master")

    assert caught.value.code == "workdir.unusable"
    assert "symbolic link" in str(caught.value)
    assert runner.calls == []
    assert strategy.issued == 0
    assert workdir.is_symlink()
    assert names(parent) == {"wiki"}
    if target != "dangling":
        assert names(real) == ({"mine.txt"} if target == "non-empty-directory" else set())


def test_a_workdir_swapped_for_a_symlink_during_the_clone_is_unusable_and_staging_removed(
    strategy: CountingStrategy, tmp_path: Path
) -> None:
    parent = tmp_path / "cache"
    parent.mkdir()
    workdir = parent / "wiki"
    real = tmp_path / "real"
    real.mkdir()
    inner = clone_into_destination({"Home.md": "# remote"})

    def respond(call: RecordedCall) -> CommandResult:
        result = inner(call)
        workdir.symlink_to(real, target_is_directory=True)  # swapped after the pre-clone check
        return result

    git, _ = clone_git(respond, strategy, workdir)

    with pytest.raises(GithubWikiError) as caught:
        git.clone("master")

    assert caught.value.code == "workdir.unusable"
    assert list(real.iterdir()) == []  # nothing was published through the link
    assert names(parent) == {"wiki"}


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


def test_ignored_paths_come_back_in_the_order_asked_and_names_travel_on_stdin(
    tmp_path: Path, strategy: CountingStrategy
) -> None:
    git, runner = failing(strategy, tmp_path, returncode=0, stdout="b.md\0a.md\0")

    assert git.ignored_paths(["a.md", "x[ab].md", "b.md"]) == ("a.md", "b.md")
    assert runner.calls[0].argv == ("git", "check-ignore", "--stdin", "-z")
    assert runner.calls[0].stdin == "a.md\0x[ab].md\0b.md\0"


def test_no_ignored_path_is_exit_one_and_no_names_run_no_command(
    tmp_path: Path, strategy: CountingStrategy
) -> None:
    git, runner = failing(strategy, tmp_path, returncode=1)

    assert git.ignored_paths(["a.md"]) == ()
    assert git.ignored_paths([]) == ()
    assert len(runner.calls) == 1


@pytest.mark.parametrize("script", [{"returncode": 128, "stderr": "fatal: bad\n"}, {"timed_out": True}])
def test_a_check_ignore_that_cannot_answer_is_a_coded_error(
    tmp_path: Path, strategy: CountingStrategy, script: dict[str, object]
) -> None:
    git, _runner = failing(strategy, tmp_path, **script)

    with pytest.raises(GithubWikiError):
        git.ignored_paths(["a.md"])


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
