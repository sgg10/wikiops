"""Dirty-workdir policy, offline plans, fetch / fast-forward and sync state (GW-S2, S6, S7, S13, S15).

Foreign changes refuse the run at plan time and again under the lock; the
wikiops-own pending paths (manifest + matching hash) are allowed. Sync only ever
fetches and fast-forwards: a divergence is reported and never resolved, a git
refusal over local changes is ``workdir.dirty``, local-ahead history is kept.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support.fake_wiki_git import sha_for, subcommand_and_args
from tests.support.sync_harness import SyncHarness, build
from wikiops.providers.github_wiki.errors import GithubWikiError
from wikiops.providers.github_wiki.lock import WorkdirLock
from wikiops.providers.github_wiki.manifest import PendingManifest
from wikiops.providers.github_wiki.workdir import lock_path, manifest_path

REWRITING = {"rebase", "reset", "checkout", "switch", "pull", "commit", "add", "push", "stash"}


def failure(harness: SyncHarness, *, purpose: str = "apply", **options: object) -> GithubWikiError:
    with pytest.raises(GithubWikiError) as caught:
        harness.sync(**options).ensure_ready(purpose)  # type: ignore[arg-type]
    return caught.value


def manifest_of(harness: SyncHarness) -> PendingManifest:
    return PendingManifest(manifest_path(harness.fake.git_dir), workdir=harness.workdir)


def write_pending(harness: SyncHarness, path: str, content: str = "# mine") -> None:
    """A page wikiops wrote earlier: on disk, dirty in git, recorded in the manifest."""
    (harness.workdir / path).write_text(content)
    manifest_of(harness).record([path])


@pytest.fixture
def clone(tmp_path: Path) -> SyncHarness:
    return build(tmp_path, cloned=True)


# -- foreign dirty paths ---------------------------------------------------------------------


@pytest.mark.parametrize("purpose", ["plan", "apply"])
def test_a_foreign_dirty_path_refuses_the_run_at_plan_and_apply_time(
    clone: SyncHarness, purpose: str
) -> None:
    clone.fake.dirty = ["?? notes.txt"]

    error = failure(clone, purpose=purpose)

    assert error.code == "workdir.dirty"
    assert error.context["paths"] == "notes.txt"
    assert {"fetch", "merge"}.isdisjoint(clone.subcommands())  # nothing is synced over foreign work


def test_dirty_detection_lists_untracked_files_individually(clone: SyncHarness) -> None:
    clone.sync().ensure_ready("apply")

    status = [subcommand_and_args(c.argv)[1] for c in clone.runner.calls if subcommand_and_args(c.argv)[0] == "status"]
    assert status == [["--porcelain=v1", "-z", "--untracked-files=all"]]


def test_the_lock_is_released_after_a_dirty_refusal(clone: SyncHarness) -> None:
    clone.fake.dirty = ["?? notes.txt"]
    failure(clone)

    other = WorkdirLock(lock_path(clone.fake.git_dir), workdir=clone.workdir)
    other.acquire("probe")  # would raise workdir.locked if the failed sync leaked its lock
    other.release()
    assert [event.event for event in clone.lock_events] == ["acquire", "release"]


def test_a_corrupt_manifest_is_reported_before_anything_is_synced(clone: SyncHarness) -> None:
    path = manifest_path(clone.fake.git_dir)
    path.parent.mkdir(parents=True)
    path.write_text("{ this is not json")

    error = failure(clone)

    assert error.code == "workdir.manifest_corrupt"
    assert str(path) in str(error)
    assert {"fetch", "merge"}.isdisjoint(clone.subcommands())


def test_wikiops_own_pending_paths_are_allowed_and_counted(clone: SyncHarness) -> None:
    write_pending(clone, "Home.md")
    clone.fake.dirty = [" M Home.md"]

    state = clone.sync().ensure_ready("apply")

    assert state.pending == 1
    assert "fetch" in clone.subcommands()  # sync went ahead


def test_a_pending_path_the_user_edited_afterwards_is_foreign(clone: SyncHarness) -> None:
    write_pending(clone, "Home.md")
    (clone.workdir / "Home.md").write_text("# edited by hand")
    clone.fake.dirty = [" M Home.md"]

    error = failure(clone)

    assert error.code == "workdir.dirty"
    assert error.context["paths"] == "Home.md"


def test_only_the_foreign_paths_are_listed_next_to_pending_ones(clone: SyncHarness) -> None:
    write_pending(clone, "Home.md")
    clone.fake.dirty = [" M Home.md", " M Other.md"]

    error = failure(clone)

    assert error.context["paths"] == "Other.md" and error.context["count"] == 1


def test_manifest_entries_that_are_no_longer_dirty_are_pruned(clone: SyncHarness) -> None:
    write_pending(clone, "Home.md")
    clone.fake.dirty = []  # the user committed it

    state = clone.sync().ensure_ready("apply")

    assert state.pending == 0
    assert manifest_of(clone).entries() == {}


# -- offline plan ----------------------------------------------------------------------------


def test_an_offline_plan_issues_no_network_command_and_asks_for_no_credentials(clone: SyncHarness) -> None:
    state = clone.sync(sync_on_plan=False).ensure_ready("plan")

    assert state.offline is True
    assert clone.network_calls() == []
    assert {"ls-remote", "clone", "fetch", "push"}.isdisjoint(clone.subcommands())
    assert clone.strategy.issued == 0
    assert state.branch == "master" and state.head_sha == sha_for("c1")


def test_an_offline_plan_takes_the_branch_from_the_clones_remote_head(tmp_path: Path) -> None:
    harness = build(tmp_path, cloned=True, local_branch="docs", origin_head="docs")

    assert harness.sync(sync_on_plan=False).ensure_ready("plan").branch == "docs"


def test_an_offline_plan_falls_back_to_the_checked_out_branch_without_a_remote_head(tmp_path: Path) -> None:
    harness = build(tmp_path, cloned=True, local_branch="wiki", origin_head=None)

    assert harness.sync(sync_on_plan=False).ensure_ready("plan").branch == "wiki"


def test_an_offline_plan_honors_the_branch_override_and_the_mismatch_rule(tmp_path: Path) -> None:
    harness = build(tmp_path, cloned=True)

    error = failure(harness, purpose="plan", sync_on_plan=False, branch="main")

    assert error.code == "sync.branch_mismatch"
    assert harness.network_calls() == []


def test_an_offline_plan_still_refuses_foreign_dirty_paths(clone: SyncHarness) -> None:
    clone.fake.dirty = ["?? scratch.md"]

    assert failure(clone, purpose="plan", sync_on_plan=False).code == "workdir.dirty"


@pytest.mark.parametrize("state", ["missing", "empty"])
def test_an_offline_plan_without_a_clone_is_no_local_clone(tmp_path: Path, state: str) -> None:
    harness = build(tmp_path)
    if state == "empty":
        harness.workdir.mkdir(parents=True)

    error = failure(harness, purpose="plan", sync_on_plan=False)

    assert error.code == "sync.no_local_clone"
    assert str(harness.workdir) in str(error)
    assert harness.runner.calls == []
    assert "sync_on_plan: true" in error.hint


def test_apply_always_syncs_even_when_sync_on_plan_is_off(clone: SyncHarness) -> None:
    state = clone.sync(sync_on_plan=False).ensure_ready("apply")

    assert state.offline is False
    assert clone.network_calls() == ["ls-remote", "fetch"]
    assert "merge" in clone.subcommands()


def test_an_online_plan_runs_one_fetch_before_reading(clone: SyncHarness) -> None:
    state = clone.sync().ensure_ready("plan")

    assert state.offline is False
    assert clone.subcommands().count("fetch") == 1


# -- fetch and fast-forward ----------------------------------------------------------------------


def test_fetch_updates_only_the_resolved_branchs_tracking_ref(clone: SyncHarness) -> None:
    clone.sync().ensure_ready("apply")

    fetch = next(c.argv for c in clone.runner.calls if subcommand_and_args(c.argv)[0] == "fetch")
    assert subcommand_and_args(fetch)[1] == ["--no-tags", "origin", "+refs/heads/master:refs/remotes/origin/master"]


def test_a_remote_that_advanced_is_fast_forwarded(clone: SyncHarness) -> None:
    clone.fake.remote["master"] = ["c1", "c2"]

    state = clone.sync().ensure_ready("apply")

    merge = next(c.argv for c in clone.runner.calls if subcommand_and_args(c.argv)[0] == "merge")
    assert subcommand_and_args(merge)[1] == ["--ff-only", "origin/master"]
    assert not any(part.startswith("core.hooksPath=") for part in merge)  # a local command
    assert clone.fake.local == ["c1", "c2"]
    assert state.head_sha == sha_for("c2") and state.unpushed == 0


def test_an_up_to_date_clone_is_a_no_op(clone: SyncHarness) -> None:
    state = clone.sync().ensure_ready("apply")

    assert clone.fake.local == ["c1"] and state.head_sha == sha_for("c1")
    assert REWRITING.isdisjoint(clone.subcommands())


def test_a_diverged_history_is_reported_and_never_resolved(clone: SyncHarness) -> None:
    clone.fake.local = ["c1", "l1"]
    clone.fake.remote["master"] = ["c1", "r1"]

    error = failure(clone)

    assert error.code == "sync.diverged"
    assert error.context["local"] == sha_for("l1")[:7]
    assert str(clone.workdir) in str(error)
    assert "web" in error.hint and "accumulate" in error.hint
    assert REWRITING.isdisjoint(clone.subcommands())
    assert clone.fake.local == ["c1", "l1"]  # no data lost


@pytest.mark.parametrize(
    "stderr",
    [
        "error: Your local changes to the following files would be overwritten by merge:\n\tHome.md\nAborting\n",
        "error: The following untracked working tree files would be overwritten by merge:\n\tNew.md\nAborting\n",
    ],
    ids=["modified", "untracked"],
)
def test_a_git_refusal_over_local_changes_is_workdir_dirty(clone: SyncHarness, stderr: str) -> None:
    clone.fake.fail["merge"] = (1, stderr)

    error = failure(clone)

    assert error.code == "workdir.dirty"
    assert str(clone.workdir) in str(error)


def test_an_unclassified_merge_failure_is_git_failed_with_the_stderr_tail(clone: SyncHarness) -> None:
    clone.fake.fail["merge"] = (128, "fatal: refusing to merge unrelated histories\n")

    error = failure(clone)

    assert error.code == "sync.git_failed"
    assert "unrelated histories" in str(error)


def test_local_commits_ahead_of_an_unchanged_remote_are_kept(clone: SyncHarness) -> None:
    clone.fake.local = ["c1", "l1", "l2"]

    state = clone.sync().ensure_ready("apply")

    assert clone.fake.local == ["c1", "l1", "l2"]
    assert state.unpushed == 2
    assert state.head_sha == sha_for("l2")
    assert REWRITING.isdisjoint(clone.subcommands())


def test_the_unpushed_count_comes_from_rev_list_against_the_remote_branch(clone: SyncHarness) -> None:
    clone.sync().ensure_ready("apply")

    rev_list = next(c.argv for c in clone.runner.calls if subcommand_and_args(c.argv)[0] == "rev-list")
    assert subcommand_and_args(rev_list)[1] == ["--count", "origin/master..HEAD"]


def test_the_sync_state_carries_branch_head_unpushed_and_pending(clone: SyncHarness) -> None:
    write_pending(clone, "Home.md")
    write_pending(clone, "Guide.md")
    clone.fake.dirty = ["?? Home.md", "?? Guide.md"]
    clone.fake.local = ["c1", "l1"]

    state = clone.sync().ensure_ready("apply")

    assert (state.workdir, state.branch, state.head_sha, state.offline, state.unpushed, state.pending) == (
        clone.workdir,
        "master",
        sha_for("l1"),
        False,
        1,
        2,
    )


# -- once per instance, idempotent across instances -------------------------------------------------


def test_ensure_ready_runs_at_most_once_per_instance(clone: SyncHarness) -> None:
    sync = clone.sync()
    first = sync.ensure_ready("apply")
    calls = len(clone.runner.calls)

    second = sync.ensure_ready("apply")
    third = sync.ensure_ready("plan")

    assert first is second is third
    assert len(clone.runner.calls) == calls
    assert sync.state is first


def test_an_offline_result_is_upgraded_when_the_same_instance_applies(clone: SyncHarness) -> None:
    sync = clone.sync(sync_on_plan=False)
    assert sync.ensure_ready("plan").offline is True
    assert clone.network_calls() == []

    applied = sync.ensure_ready("apply")

    assert applied.offline is False
    assert clone.network_calls() == ["ls-remote", "fetch"]


def test_a_second_instance_performs_no_state_changing_git_operation(tmp_path: Path) -> None:
    harness = build(tmp_path)
    first = harness.sync().ensure_ready("plan")
    mark = len(harness.runner.calls)
    before = (list(harness.fake.local), harness.fake.local_branch, harness.fake.origin_url)

    second = harness.sync().ensure_ready("apply")

    later = [subcommand_and_args(c.argv)[0] for c in harness.runner.calls[mark:]]
    assert REWRITING.isdisjoint(later) and "clone" not in later  # reads, fetch and a no-op merge only
    assert (list(harness.fake.local), harness.fake.local_branch, harness.fake.origin_url) == before
    assert second.head_sha == first.head_sha and second.branch == first.branch


# -- the lock ----------------------------------------------------------------------------------------


def test_the_lock_is_taken_after_the_clone_and_released_at_the_end_of_the_sync(tmp_path: Path) -> None:
    harness = build(tmp_path)

    harness.sync().ensure_ready("apply")

    acquire, release = harness.lock_events
    assert (acquire.event, release.event) == ("acquire", "release")
    assert acquire.clones_before == 1
    assert release.calls_before == len(harness.runner.calls)  # held across fetch and fast-forward
    assert release.calls_before > acquire.calls_before


def test_a_lock_held_by_another_run_fails_fast_and_syncs_nothing(clone: SyncHarness) -> None:
    other = WorkdirLock(lock_path(clone.fake.git_dir), workdir=clone.workdir)
    other.acquire("apply")
    try:
        error = failure(clone)
    finally:
        other.release()

    assert error.code == "workdir.locked"
    assert {"fetch", "merge"}.isdisjoint(clone.subcommands())


def test_the_lock_is_released_when_the_fast_forward_fails(clone: SyncHarness) -> None:
    clone.fake.local = ["c1", "l1"]
    clone.fake.remote["master"] = ["c1", "r1"]
    failure(clone)

    assert [event.event for event in clone.lock_events] == ["acquire", "release"]


def test_the_state_files_live_in_the_git_directory_git_reports(tmp_path: Path) -> None:
    git_dir = tmp_path / "gitdirs" / "linked"  # a worktree or gitfile: not <workdir>/.git
    harness = build(tmp_path, cloned=True, git_dir_path=git_dir)
    write_pending(harness, "Home.md")
    harness.fake.dirty = ["?? Home.md"]

    state = harness.sync().ensure_ready("apply")

    assert state.pending == 1
    assert json.loads((git_dir / "wikiops" / "pending.json").read_text())["paths"].keys() == {"Home.md"}
    assert (git_dir / "wikiops" / "lock").exists()
    assert not (harness.workdir / ".git" / "wikiops").exists()
    absolute_git_dir = [
        c for c in harness.runner.calls if subcommand_and_args(c.argv) == ("rev-parse", ["--absolute-git-dir"])
    ]
    assert len(absolute_git_dir) == 1


def test_the_lock_and_manifest_are_unavailable_before_the_first_sync(clone: SyncHarness) -> None:
    sync = clone.sync()

    assert sync.state is None
    with pytest.raises(RuntimeError):
        _ = sync.lock
    with pytest.raises(RuntimeError):
        _ = sync.manifest


def test_the_lock_and_manifest_are_exposed_after_a_sync(clone: SyncHarness) -> None:
    sync = clone.sync()
    sync.ensure_ready("apply")

    assert sync.lock.path == lock_path(clone.fake.git_dir) and not sync.lock.held
    assert sync.manifest.path == manifest_path(clone.fake.git_dir)


# -- timeouts and unexpected git output -------------------------------------------------------------


@pytest.mark.parametrize("operation", ["rev-parse", "symbolic-ref", "config", "merge", "status", "fetch"])
def test_a_timed_out_git_command_is_sync_timeout_naming_it_and_the_lock_is_released(
    clone: SyncHarness, operation: str
) -> None:
    clone.fake.timeout_on.add(operation)

    error = failure(clone)

    assert error.code == "sync.timeout"
    assert operation in str(error)
    probe = WorkdirLock(lock_path(clone.fake.git_dir), workdir=clone.workdir)
    probe.acquire("probe")  # raises workdir.locked if the failed sync leaked its lock
    probe.release()


def test_an_unexpected_unpushed_count_is_git_failed_not_a_crash(clone: SyncHarness) -> None:
    clone.fake.rev_list_output = "lots\n"

    error = failure(clone)

    assert error.code == "sync.git_failed"
    assert "rev-list" in str(error)
