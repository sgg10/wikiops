"""``WikiSync.peek``: a read-only look at an existing clone for ``describe_target`` (GW-P11).

Peeking never fetches, locks, writes or issues a credential: it reports the
branch, the unpushed commits and the pending paths of a clone that is already
there, or ``None`` when there is none (so nothing is run before the clone exists).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.sync_harness import SyncHarness, build
from wikiops.providers.github_wiki.errors import GithubWikiError
from wikiops.providers.github_wiki.manifest import PendingManifest
from wikiops.providers.github_wiki.workdir import manifest_path

READ_ONLY = {"status", "rev-parse", "config", "symbolic-ref", "rev-list"}


def record_pending(harness: SyncHarness, *paths: str) -> None:
    for path in paths:
        (harness.workdir / path).write_text(f"# {path}")
    PendingManifest(manifest_path(harness.fake.git_dir), workdir=harness.workdir).record(list(paths))


def test_peek_without_a_clone_returns_none_and_runs_nothing(tmp_path: Path) -> None:
    harness = build(tmp_path)

    assert harness.sync().peek() is None
    assert harness.runner.calls == []


def test_peek_of_an_empty_existing_directory_returns_none_and_runs_nothing(tmp_path: Path) -> None:
    harness = build(tmp_path)
    harness.workdir.mkdir(parents=True)

    assert harness.sync().peek() is None
    assert harness.runner.calls == []


def test_peek_reports_branch_unpushed_and_pending_of_an_existing_clone(tmp_path: Path) -> None:
    harness = build(tmp_path, cloned=True, local=["c1", "c2", "c3"], tracking=["c1"])
    record_pending(harness, "Home.md", "Other.md")

    state = harness.sync().peek()

    assert state is not None
    assert (state.branch, state.unpushed, state.pending) == ("master", 2, 2)
    assert state.workdir == harness.workdir
    assert state.offline is True


def test_peek_is_read_only_local_and_credential_free(tmp_path: Path) -> None:
    harness = build(tmp_path, cloned=True)

    harness.sync().peek()

    assert set(harness.subcommands()) <= READ_ONLY
    assert harness.network_calls() == []
    assert harness.strategy.issued == 0
    assert harness.lock_events == []


def test_peek_prefers_the_branch_override_then_origin_head_then_the_checked_out_branch(tmp_path: Path) -> None:
    override = build(tmp_path / "a", cloned=True, remote={"master": ["c1"], "wiki": ["c1"]}, local_branch="wiki")
    assert override.sync(branch="wiki").peek().branch == "wiki"  # type: ignore[union-attr]

    from_origin_head = build(tmp_path / "b", cloned=True, origin_head="master")
    assert from_origin_head.sync().peek().branch == "master"  # type: ignore[union-attr]

    from_checkout = build(tmp_path / "c", cloned=True, origin_head=None, local_branch="trunk")
    assert from_checkout.sync().peek().branch == "trunk"  # type: ignore[union-attr]


def test_peek_of_a_detached_clone_without_a_branch_source_returns_none(tmp_path: Path) -> None:
    harness = build(tmp_path, cloned=True, origin_head=None, local_branch=None)

    assert harness.sync().peek() is None


def test_peek_does_not_count_as_the_sync_and_leaves_ensure_ready_to_run_fully(tmp_path: Path) -> None:
    harness = build(tmp_path, cloned=True)
    sync = harness.sync()

    assert sync.peek() is not None
    assert sync.state is None
    sync.ensure_ready("apply")

    assert "fetch" in harness.subcommands()
    assert sync.state is not None and sync.state.offline is False


def test_peek_returns_the_cached_state_after_a_sync_without_new_commands(tmp_path: Path) -> None:
    harness = build(tmp_path, cloned=True)
    sync = harness.sync()
    synced = sync.ensure_ready("apply")
    seen = len(harness.runner.calls)

    assert sync.peek() is synced
    assert len(harness.runner.calls) == seen


def test_peek_refuses_a_directory_that_is_not_the_clone_of_this_wiki(tmp_path: Path) -> None:
    harness = build(tmp_path, cloned=True, origin_url="https://github.com/acme/other.wiki.git")

    with pytest.raises(GithubWikiError) as error:
        harness.sync().peek()

    assert error.value.code == "sync.remote_mismatch"
    assert set(harness.subcommands()) <= READ_ONLY
