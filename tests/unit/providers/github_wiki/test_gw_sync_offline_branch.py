"""An offline plan must not read a branch that has no remote-tracking ref (GW-S6, GW-S4).

With ``sync_on_plan: false`` the branch comes from the override, the clone's
``origin/HEAD`` or, failing both, the checked-out branch. None of those proves the
clone ever fetched that branch: without ``refs/remotes/origin/<branch>`` the
plan would be computed against nothing (unpushed counts, staleness). The run is
refused with ``sync.branch_not_found`` instead, before any network command.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.sync_harness import SyncHarness, build
from wikiops.providers.github_wiki.errors import GithubWikiError


def refused(harness: SyncHarness, *, purpose: str = "plan", **options: object) -> GithubWikiError:
    with pytest.raises(GithubWikiError) as caught:
        harness.sync(**options).ensure_ready(purpose)  # type: ignore[arg-type]
    return caught.value


def test_an_override_without_a_remote_tracking_ref_is_branch_not_found(tmp_path: Path) -> None:
    harness = build(
        tmp_path, cloned=True, local_branch="wiki", origin_head="master", tracking_refs={"master"}
    )

    error = refused(harness, sync_on_plan=False, branch="wiki")

    assert error.code == "sync.branch_not_found"
    assert "'wiki'" in error.summary
    assert error.context["available"] == "master"
    assert error.context["workdir"] == str(harness.workdir)
    assert "sync_on_plan: true" in error.hint
    assert harness.network_calls() == []
    assert {"ls-remote", "fetch", "clone", "merge"}.isdisjoint(harness.subcommands())


def test_the_checked_out_fallback_without_origin_head_needs_a_tracking_ref_too(tmp_path: Path) -> None:
    harness = build(
        tmp_path, cloned=True, local_branch="wiki", origin_head=None, tracking_refs={"master"}
    )

    error = refused(harness, sync_on_plan=False)

    assert error.code == "sync.branch_not_found"
    assert error.context["available"] == "master"
    assert harness.network_calls() == []


def test_a_dangling_origin_head_is_branch_not_found(tmp_path: Path) -> None:
    harness = build(
        tmp_path, cloned=True, local_branch="gone", origin_head="gone", tracking_refs={"master"}
    )

    assert refused(harness, sync_on_plan=False).code == "sync.branch_not_found"


def test_a_clone_with_no_remote_tracking_refs_at_all_lists_none(tmp_path: Path) -> None:
    harness = build(tmp_path, cloned=True, origin_head=None, tracking_refs=set())

    error = refused(harness, sync_on_plan=False)

    assert error.code == "sync.branch_not_found"
    assert error.context["available"] == "(none)"


def test_several_tracking_refs_are_listed_sorted(tmp_path: Path) -> None:
    harness = build(
        tmp_path,
        cloned=True,
        local_branch="wiki",
        origin_head=None,
        tracking_refs={"zeta", "alpha", "master"},
    )

    assert refused(harness, sync_on_plan=False).context["available"] == "alpha, master, zeta"


@pytest.mark.parametrize(
    ("options", "model"),
    [
        ({"branch": "wiki"}, {"local_branch": "wiki", "tracking_refs": {"master", "wiki"}}),
        ({}, {"local_branch": "wiki", "origin_head": None, "tracking_refs": {"wiki"}}),
        ({}, {"local_branch": "master", "origin_head": "master", "tracking_refs": {"master"}}),
    ],
    ids=["override-tracked", "checked-out-tracked", "origin-head-tracked"],
)
def test_a_branch_with_a_tracking_ref_still_plans_offline(
    tmp_path: Path, options: dict, model: dict
) -> None:
    harness = build(tmp_path, cloned=True, **model)

    state = harness.sync(sync_on_plan=False, **options).ensure_ready("plan")

    assert state.offline is True
    assert state.branch == (options.get("branch") or model["local_branch"])
    assert harness.network_calls() == []


def test_the_branch_mismatch_is_still_reported_before_the_tracking_ref(tmp_path: Path) -> None:
    harness = build(tmp_path, cloned=True, tracking_refs=set())

    assert refused(harness, sync_on_plan=False, branch="main").code == "sync.branch_mismatch"


def test_apply_does_not_need_a_tracking_ref_because_the_fetch_creates_it(tmp_path: Path) -> None:
    harness = build(tmp_path, cloned=True, tracking_refs=set())

    state = harness.sync(sync_on_plan=False).ensure_ready("apply")

    assert state.offline is False
    assert "fetch" in harness.subcommands()


def test_peek_reports_a_missing_tracking_ref_instead_of_a_raw_git_failure(tmp_path: Path) -> None:
    harness = build(
        tmp_path, cloned=True, local_branch="wiki", origin_head=None, tracking_refs={"master"}
    )

    with pytest.raises(GithubWikiError) as caught:
        harness.sync().peek()

    assert caught.value.code == "sync.branch_not_found"
    assert harness.network_calls() == []
