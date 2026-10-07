"""Branch detection, clone and remote-failure handling of the sync layer (GW-S2, S4, S5, S14).

The branch always comes from the remote (HEAD symref via ``ls-remote --symref``,
never an assumed ``main``/``master``) unless the user overrides it; the clone
happens before the workdir lock; a checked-out branch that is not the resolved
one is a ``sync.branch_mismatch`` with no checkout ever issued; and remote
failures keep their class (not initialized, rejected credentials, unreachable,
timeout) without retries or a push-bootstrap.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.fake_wiki_git import sha_for, subcommand_and_args
from tests.support.sync_harness import REMOTE, SyncHarness, build
from wikiops.providers.github_wiki.errors import GithubWikiError

HTTPS_NOT_FOUND = "remote: Repository not found.\nfatal: repository 'https://github.com/acme/platform.wiki.git/' not found\n"
SSH_NOT_FOUND = "ERROR: Repository not found.\nfatal: Could not read from remote repository.\n"
STATE_CHANGING = {"checkout", "switch", "reset", "rebase", "pull", "branch", "push", "commit", "add"}


def failure(harness: SyncHarness, *, purpose: str = "apply", **options: object) -> GithubWikiError:
    with pytest.raises(GithubWikiError) as caught:
        harness.sync(**options).ensure_ready(purpose)  # type: ignore[arg-type]
    return caught.value


def commands(harness: SyncHarness, name: str) -> list[tuple[str, ...]]:
    return [
        call.argv for call in harness.runner.calls if subcommand_and_args(call.argv)[0] == name
    ]


# -- branch detection from the remote HEAD -------------------------------------------------


def test_ls_remote_with_symref_is_the_first_command_and_reads_head_and_branches(tmp_path: Path) -> None:
    harness = build(tmp_path)

    harness.sync().ensure_ready("apply")

    first = harness.runner.calls[0]
    command, args = subcommand_and_args(first.argv)
    assert command == "ls-remote"
    assert args == ["--symref", REMOTE, "HEAD", "refs/heads/*"]
    assert any(part.startswith("core.hooksPath=") for part in first.argv)  # a network command


@pytest.mark.parametrize("default", ["master", "main", "trunk"])
def test_the_branch_is_whatever_the_remote_head_points_at(tmp_path: Path, default: str) -> None:
    harness = build(
        tmp_path, remote={"master": ["a"], "main": ["b"], "trunk": ["c"]}, head_symref=default
    )

    state = harness.sync().ensure_ready("apply")

    clone = commands(harness, "clone")[0]
    assert clone[clone.index("--branch") + 1] == default
    assert state.branch == default


def test_a_wiki_that_uses_master_is_cloned_on_master_not_main(tmp_path: Path) -> None:
    harness = build(tmp_path, remote={"master": ["a"]}, head_symref="master")

    assert harness.sync().ensure_ready("apply").branch == "master"


def test_a_configured_branch_overrides_detection(tmp_path: Path) -> None:
    harness = build(tmp_path, remote={"master": ["a"], "main": ["b"]}, head_symref="master")

    state = harness.sync(branch="main").ensure_ready("apply")

    clone = commands(harness, "clone")[0]
    assert clone[clone.index("--branch") + 1] == "main"
    assert state.branch == "main"


@pytest.mark.parametrize(
    ("model", "override"),
    [
        ({"remote": {"master": ["a"], "docs": ["b"]}, "head_symref": "master"}, "nope"),
        ({"remote": {"master": ["a"]}, "head_symref": None}, None),
        ({"remote": {"master": ["a"]}, "head_symref": "gone"}, None),
        ({"remote": {}, "head_symref": None}, None),
    ],
    ids=["override-absent", "no-head-symref", "symref-target-missing", "empty-remote"],
)
def test_an_unresolvable_branch_is_branch_not_found_and_nothing_is_cloned(
    tmp_path: Path, model: dict, override: str | None
) -> None:
    harness = build(tmp_path, **model)

    error = failure(harness, branch=override)

    assert error.code == "sync.branch_not_found"
    assert not harness.workdir.exists()
    assert "clone" not in harness.subcommands()


def test_branch_not_found_lists_the_branches_the_remote_has(tmp_path: Path) -> None:
    harness = build(tmp_path, remote={"master": ["a"], "docs": ["b"]})

    error = failure(harness, branch="nope")

    assert "master" in error.context["available"] and "docs" in error.context["available"]
    assert "nope" in str(error)


# -- the clone ---------------------------------------------------------------------------------


def test_the_clone_has_the_exact_argv_and_no_tags(tmp_path: Path) -> None:
    harness = build(tmp_path)

    harness.sync().ensure_ready("apply")

    command, args = subcommand_and_args(commands(harness, "clone")[0])
    assert command == "clone"
    assert args[:6] == ["--no-tags", "--origin", "origin", "--branch", "master", "--"]
    assert args[6] == REMOTE
    assert Path(args[7]).parent == harness.workdir.parent  # staging sibling, published afterwards


def test_the_missing_parent_directories_are_created_before_the_first_clone(tmp_path: Path) -> None:
    workdir = tmp_path / "deep" / "er" / "wiki"
    harness = build(tmp_path, workdir=workdir)

    harness.sync().ensure_ready("apply")

    assert (workdir / ".git").is_dir()


def test_the_clone_happens_before_the_lock_is_taken(tmp_path: Path) -> None:
    harness = build(tmp_path)

    harness.sync().ensure_ready("apply")

    acquire = next(event for event in harness.lock_events if event.event == "acquire")
    assert acquire.clones_before == 1
    assert harness.subcommands().index("clone") < acquire.calls_before


def test_an_existing_clone_is_not_cloned_again(tmp_path: Path) -> None:
    harness = build(tmp_path, cloned=True)

    harness.sync().ensure_ready("apply")

    assert "clone" not in harness.subcommands()


# -- the checked-out branch --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("local_branch", "shown"), [("feature", "feature"), (None, "detached")], ids=["other-branch", "detached"]
)
def test_a_clone_on_another_branch_is_a_mismatch_and_nothing_is_switched(
    tmp_path: Path, local_branch: str | None, shown: str
) -> None:
    harness = build(tmp_path, cloned=True, local_branch=local_branch)

    error = failure(harness)

    assert error.code == "sync.branch_mismatch"
    assert shown in str(error)
    assert STATE_CHANGING.isdisjoint(harness.subcommands())
    assert harness.fake.local_branch == local_branch  # HEAD unchanged
    assert {"fetch", "merge"}.isdisjoint(harness.subcommands())


def test_the_mismatch_names_the_expected_and_the_checked_out_branch(tmp_path: Path) -> None:
    harness = build(tmp_path, cloned=True, local_branch="feature")

    error = failure(harness)

    assert error.context["expected"] == "master" and error.context["actual"] == "feature"
    assert str(harness.workdir) in str(error)


def test_a_configured_branch_that_differs_from_the_checkout_is_a_mismatch(tmp_path: Path) -> None:
    harness = build(tmp_path, cloned=True, remote={"master": ["c1"], "main": ["m1"]})

    error = failure(harness, branch="main")

    assert error.code == "sync.branch_mismatch"
    assert STATE_CHANGING.isdisjoint(harness.subcommands())


# -- remote failures keep their class ----------------------------------------------------------


@pytest.mark.parametrize("stderr", [HTTPS_NOT_FOUND, SSH_NOT_FOUND], ids=["https", "ssh"])
def test_an_uninitialized_wiki_fails_fast_without_retry_or_bootstrap(tmp_path: Path, stderr: str) -> None:
    harness = build(tmp_path)
    harness.fake.fail["ls-remote"] = (128, stderr)

    error = failure(harness)

    assert error.code == "wiki.not_initialized"
    assert harness.subcommands() == ["ls-remote"]  # one attempt: no retry, no clone, no push
    assert STATE_CHANGING.isdisjoint(harness.subcommands())


def test_the_uninitialized_message_lists_the_four_causes_and_the_auth_label(tmp_path: Path) -> None:
    harness = build(tmp_path)
    harness.fake.fail["ls-remote"] = (128, HTTPS_NOT_FOUND)

    error = failure(harness)

    text = str(error)
    for cause in ("not initialized", "feature is disabled", "no access", "owner, repo or host is wrong"):
        assert cause in text
    assert "auth='env:T'" in text


@pytest.mark.parametrize(
    ("stderr", "code"),
    [
        ("remote: Invalid username or password.\nfatal: Authentication failed for 'x'\n", "auth.rejected"),
        ("git@github.com: Permission denied (publickey).\n", "auth.rejected"),
        ("fatal: unable to access 'x': Could not resolve host: github.com\n", "network.unreachable"),
    ],
    ids=["bad-credentials", "ssh-publickey", "no-network"],
)
def test_remote_failures_keep_their_class(tmp_path: Path, stderr: str, code: str) -> None:
    harness = build(tmp_path)
    harness.fake.fail["ls-remote"] = (128, stderr)

    assert failure(harness).code == code


@pytest.mark.parametrize("operation", ["ls-remote", "clone"])
def test_a_timed_out_network_command_is_sync_timeout_naming_the_operation(
    tmp_path: Path, operation: str
) -> None:
    harness = build(tmp_path)
    harness.fake.timeout_on.add(operation)

    error = failure(harness)

    assert error.code == "sync.timeout"
    assert operation in str(error)
    assert not harness.workdir.exists()


def test_a_failed_clone_leaves_neither_workdir_nor_staging_directory(tmp_path: Path) -> None:
    harness = build(tmp_path)
    harness.fake.fail["clone"] = (128, "fatal: unable to access: Connection refused\n")

    error = failure(harness)

    assert error.code == "network.unreachable"
    assert list(harness.workdir.parent.iterdir()) == []


def test_the_token_never_appears_in_a_remote_failure(tmp_path: Path) -> None:
    harness = build(tmp_path)
    harness.fake.fail["ls-remote"] = (128, f"fatal: unable to access 'https://x-access-token:{harness.strategy.secrets[0]}@github.com/a/b.wiki.git/'\n")

    assert harness.strategy.secrets[0] not in str(failure(harness))


def test_a_failed_attempt_is_not_cached_and_the_next_call_retries(tmp_path: Path) -> None:
    harness = build(tmp_path)
    harness.fake.fail["ls-remote"] = (128, "fatal: Could not resolve host: github.com\n")
    sync = harness.sync()
    with pytest.raises(GithubWikiError):
        sync.ensure_ready("apply")
    del harness.fake.fail["ls-remote"]

    state = sync.ensure_ready("apply")

    assert state.branch == "master"
    assert state.head_sha == sha_for("c1")
