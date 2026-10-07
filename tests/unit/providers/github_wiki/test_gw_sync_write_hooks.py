"""The two hooks the write side needs from ``WikiSync``: ``recheck`` and ``unpushed_commits``.

``recheck`` repeats the foreign-change check under the held lock before every write
sequence (the clone may have been edited since the one sync of the instance).
``unpushed_commits`` reads the number of local commits the remote lacks NOW, because the
sync state is a snapshot that goes stale after the first commit or push.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.sync_harness import SyncHarness, build
from wikiops.providers.github_wiki.errors import GithubWikiError
from wikiops.providers.github_wiki.manifest import PendingManifest
from wikiops.providers.github_wiki.workdir import manifest_path


@pytest.fixture
def clone(tmp_path: Path) -> SyncHarness:
    return build(tmp_path, cloned=True)


def manifest_of(harness: SyncHarness) -> PendingManifest:
    return PendingManifest(manifest_path(harness.fake.git_dir), workdir=harness.workdir)


# -- recheck ------------------------------------------------------------------------------------


def test_recheck_needs_the_lock_to_be_held(clone: SyncHarness) -> None:
    wiki = clone.sync()
    wiki.ensure_ready("apply")

    with pytest.raises(RuntimeError, match="lock"):
        wiki.recheck()


def test_recheck_refuses_a_foreign_path_that_appeared_after_the_sync(clone: SyncHarness) -> None:
    wiki = clone.sync()
    wiki.ensure_ready("apply")
    clone.fake.dirty = ["?? notes.txt"]

    with wiki.lock.hold("apply"), pytest.raises(GithubWikiError) as caught:
        wiki.recheck()

    assert caught.value.code == "workdir.dirty"
    assert caught.value.context["paths"] == "notes.txt"


def test_recheck_passes_on_a_clean_clone_and_on_pending_paths(clone: SyncHarness) -> None:
    wiki = clone.sync()
    wiki.ensure_ready("apply")
    (clone.workdir / "Home.md").write_text("# mine")
    manifest_of(clone).record(["Home.md"])
    clone.fake.dirty = ["?? Home.md"]

    with wiki.lock.hold("apply"):
        wiki.recheck()

    assert manifest_of(clone).entries().keys() == {"Home.md"}


def test_recheck_forgets_manifest_entries_that_are_no_longer_dirty(clone: SyncHarness) -> None:
    wiki = clone.sync()
    wiki.ensure_ready("apply")
    (clone.workdir / "Home.md").write_text("# mine")
    manifest_of(clone).record(["Home.md"])
    clone.fake.dirty = []  # the user committed it meanwhile

    with wiki.lock.hold("apply"):
        wiki.recheck()

    assert manifest_of(clone).entries() == {}


# -- unpushed_commits ---------------------------------------------------------------------------


def test_unpushed_commits_needs_a_finished_sync(clone: SyncHarness) -> None:
    with pytest.raises(RuntimeError, match="synced"):
        clone.sync().unpushed_commits()


def test_unpushed_commits_counts_what_the_remote_lacks(tmp_path: Path) -> None:
    harness = build(tmp_path, cloned=True, local=["c1", "c2", "c3"], tracking=["c1"])
    wiki = harness.sync()
    state = wiki.ensure_ready("apply")

    assert state.unpushed == 2
    assert wiki.unpushed_commits() == 2


def test_unpushed_commits_is_read_again_after_the_history_moved(tmp_path: Path) -> None:
    harness = build(tmp_path, cloned=True, local=["c1", "c2"], tracking=["c1"])
    wiki = harness.sync()
    wiki.ensure_ready("apply")
    harness.fake.local.append("c3")  # a commit made after the sync
    harness.fake.tracking = list(harness.fake.local)  # and a push of everything

    assert wiki.state is not None and wiki.state.unpushed == 1  # the snapshot is stale
    assert wiki.unpushed_commits() == 0


def test_an_unexpected_count_is_a_coded_failure(tmp_path: Path) -> None:
    harness = build(tmp_path, cloned=True)
    wiki = harness.sync()
    wiki.ensure_ready("apply")
    harness.fake.rev_list_output = "garbage\n"

    with pytest.raises(GithubWikiError) as caught:
        wiki.unpushed_commits()

    assert caught.value.code == "sync.git_failed"
