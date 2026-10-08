"""Real git: several profiles, a shared workdir and a swapped local backend (GW-P13, GW-LB3).

Two profiles of the same wiki never share a clone, manifest, commit history or
lock; an explicitly shared workdir is serialized by the lock and refuses a
different branch; and the local backend can be replaced (``fake_files`` for
``local_files``) without changing what lands in git.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from wikiops_sdk.domain import OperationStatus

from tests.support.real_git import SEED_PAGE, Wiki, build_wiki, run_git
from tests.support.write_ops import asset, change_set, create, ref, update
from wikiops.providers.github_wiki.errors import GithubWikiError
from wikiops.providers.github_wiki.lock import WorkdirLock
from wikiops.providers.github_wiki.manifest import PendingManifest
from wikiops.providers.github_wiki.workdir import lock_path, manifest_path, resolve_workdir

pytestmark = pytest.mark.git_integration


def statuses(result) -> list[OperationStatus]:  # noqa: ANN001
    return [item.status for item in result.results]


def profile_workdir(wiki: Wiki, name: str) -> Path:
    return resolve_workdir(
        configured=None,
        host="github.com",
        repository="acme/platform",
        provider_name=name,
        cache_dir=wiki.root / "cache",
    )


# -- two profiles, one wiki ---------------------------------------------------------------------------


def test_two_profiles_of_one_wiki_keep_separate_clones_manifests_commits_and_locks(wiki: Wiki) -> None:
    wiki.raw["workdir"] = None
    alpha, beta = profile_workdir(wiki, "alpha"), profile_workdir(wiki, "beta")
    assert alpha != beta

    wiki.provider(provider_name="alpha", allow_auto_commit=False).apply_changes(
        change_set(create("Alpha.md", "# alpha\n"))
    )
    with WorkdirLock(lock_path(alpha / ".git"), workdir=alpha).hold("alpha is busy"):
        # alpha's lock is held, beta is unaffected and commits on its own
        result = wiki.provider(provider_name="beta").apply_changes(change_set(create("Beta.md", "# beta\n")))

    assert statuses(result) == [OperationStatus.APPLIED]
    assert run_git("log", "--format=%s", cwd=beta).splitlines()[0].startswith("docs(wiki)")
    assert sorted(run_git("ls-files", cwd=beta).split()) == sorted([SEED_PAGE, "Beta.md"])
    assert sorted(run_git("ls-files", cwd=alpha).split()) == [SEED_PAGE]  # alpha never committed
    assert (alpha / "Alpha.md").exists() and not (beta / "Alpha.md").exists()
    assert set(PendingManifest(manifest_path(alpha / ".git"), workdir=alpha).entries()) == {"Alpha.md"}
    assert PendingManifest(manifest_path(beta / ".git"), workdir=beta).entries() == {}
    assert run_git("status", "--porcelain", cwd=beta) == ""
    assert run_git("status", "--porcelain", cwd=alpha) == "?? Alpha.md\n"


def test_a_foreign_change_in_one_profile_does_not_block_the_other(wiki: Wiki) -> None:
    wiki.raw["workdir"] = None
    wiki.provider(provider_name="alpha").exists(ref(SEED_PAGE))
    alpha = profile_workdir(wiki, "alpha")
    (alpha / "notes.txt").write_text("mine\n")

    blocked = wiki.provider(provider_name="alpha").apply_changes(change_set(create("A.md", "# a\n")))
    fine = wiki.provider(provider_name="beta").apply_changes(change_set(create("B.md", "# b\n")))

    assert statuses(blocked) == [OperationStatus.FAILED] and "workdir.dirty" in (blocked.results[0].message or "")
    assert statuses(fine) == [OperationStatus.APPLIED]


# -- an explicitly shared workdir ------------------------------------------------------------------------


def test_profiles_sharing_a_workdir_are_serialized_by_the_lock(wiki: Wiki) -> None:
    wiki.provider(provider_name="alpha").exists(ref(SEED_PAGE))

    with wiki.lock_handle().hold("alpha"):
        refused = wiki.provider(provider_name="beta").apply_changes(change_set(create("B.md", "# b\n")))
    accepted = wiki.provider(provider_name="beta").apply_changes(change_set(create("B.md", "# b\n")))

    assert statuses(refused) == [OperationStatus.FAILED] and "workdir.locked" in (refused.results[0].message or "")
    assert statuses(accepted) == [OperationStatus.APPLIED]
    assert wiki.committed_files() == sorted([SEED_PAGE, "B.md"])


def test_a_profile_on_another_branch_cannot_use_a_shared_workdir(wiki: Wiki) -> None:
    wiki.remote.edit("Docs.md", "# docs\n", branch="docs")
    wiki.provider(provider_name="alpha").exists(ref(SEED_PAGE))  # the clone is on master
    head = wiki.head()

    with pytest.raises(GithubWikiError) as caught:
        wiki.provider(provider_name="beta", branch="docs").exists(ref(SEED_PAGE))

    assert caught.value.code == "sync.branch_mismatch"
    assert wiki.branch() == "master" and wiki.head() == head


# -- swapping the local backend (GW-LB3) ------------------------------------------------------------------


def run_scenario(wiki: Wiki, **settings: object) -> dict[str, object]:
    """The same plugin run through whichever local backend the settings select."""
    provider = wiki.provider(allow_auto_push=True, **settings)
    stored = provider.put_asset(asset("logo.png"), b"logo-bytes")
    result = provider.apply_changes(
        change_set(
            create("Guide.md", "# Guide\n![logo](logo)\n"),
            update(SEED_PAGE, "# New home\n"),
        )
    )
    return {
        "statuses": statuses(result),
        "asset": stored.ref.locator["path"],
        "subjects": wiki.subjects(),
        "commit_files": wiki.last_commit_files(),
        "commits": wiki.commit_count(),
        "remote_files": wiki.remote.files(),
        "remote_head_is_local": wiki.remote.rev() == wiki.head(),
        "guide": wiki.remote.show("Guide.md"),
        "home": wiki.remote.show(SEED_PAGE),
        "status": wiki.status(),
    }


def test_swapping_the_local_backend_leaves_the_git_results_unchanged(
    make_wiki: Callable[..., Wiki], tmp_path: Path
) -> None:
    local = make_wiki()
    swapped = build_wiki(tmp_path / "swapped", template=local.remote)  # its own, identical remote

    with_local_files = run_scenario(local, local_backend={"type": "local_files"})
    with_fake_files = run_scenario(swapped, local_backend={"type": "fake_files"})

    assert with_local_files["statuses"] == [OperationStatus.APPLIED] * 2
    assert with_local_files["commits"] == 2 and with_local_files["remote_head_is_local"] is True
    assert with_fake_files == with_local_files
