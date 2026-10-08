"""Two ``github_wiki`` profiles in one process share nothing on the write path (GW-P13, GW-S1).

Each provider owns its clone, manifest, lock, local commits and transport. Applies of two
profiles for the same wiki interleave: every commit and every push lands in the clone of
the profile that made it, a pending path or foreign change in one clone is invisible to the
other, and each recorded network command carries only its own profile's credentials.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from wikiops_sdk.domain import OperationStatus

from tests.support.fake_git_runner import RecordedCall
from tests.support.provider_harness import ProviderHarness, build_provider
from tests.support.write_ops import asset, change_set, create, update
from wikiops.providers.github_wiki.manifest import PendingManifest
from wikiops.providers.github_wiki.workdir import manifest_path

TOKEN_A = "ghp_write_A_SECRET_1"
TOKEN_B = "ghp_write_B_SECRET_2"
APPLIED, FAILED = OperationStatus.APPLIED, OperationStatus.FAILED


def forms(token: str) -> tuple[str, ...]:
    return (token, base64.b64encode(f"x-access-token:{token}".encode()).decode())


def env_text(call: RecordedCall) -> str:
    return "\n".join(f"{key}={value}" for key, value in call.env_overrides.items() if value is not None)


def carries(harness: ProviderHarness, token: str) -> bool:
    return any(secret in env_text(call) for call in harness.runner.calls for secret in forms(token))


def leaks(harness: ProviderHarness, token: str) -> bool:
    return any(
        secret in env_text(call) or any(secret in part for part in call.argv)
        for call in harness.runner.calls
        for secret in forms(token)
    )


def manifest_of(harness: ProviderHarness) -> dict[str, str]:
    return PendingManifest(manifest_path(harness.fake.git_dir), workdir=harness.workdir).entries()


def profile(
    tmp_path: Path, name: str, *, repository: str = "acme/platform", token: str | None = None, **settings: object
) -> ProviderHarness:
    raw: dict[str, object] = {"provider_name": name, "repository": repository, "workdir": None, **settings}
    if token is not None:
        raw["auth"] = {"mode": "gh", "account": f"acct-{name}"}
    return build_provider(
        tmp_path, cache_dir=tmp_path / "cache", settings=raw, gh_token=token, track_files=True
    )


@pytest.fixture
def pair(tmp_path: Path) -> tuple[ProviderHarness, ProviderHarness]:
    """Two profiles for the SAME wiki, default workdirs, two accounts."""
    docs_a = profile(tmp_path, "docs-a", token=TOKEN_A, allow_auto_push=True)
    docs_b = profile(tmp_path, "docs-b", token=TOKEN_B, allow_auto_push=True)
    return docs_a, docs_b


def test_interleaved_applies_commit_and_push_in_their_own_clone(
    pair: tuple[ProviderHarness, ProviderHarness],
) -> None:
    docs_a, docs_b = pair

    result_a = docs_a.provider.apply_changes(change_set(create("Alpha.md", "# a\n")))
    result_b = docs_b.provider.apply_changes(change_set(create("Beta.md", "# b\n")))
    again_a = docs_a.provider.apply_changes(change_set(update("Alpha.md", "# a2\n")))

    assert docs_a.workdir != docs_b.workdir
    assert [item.status for item in (*result_a.results, *result_b.results, *again_a.results)] == [APPLIED] * 3
    assert [commit.paths for commit in docs_a.fake.commits] == [("Alpha.md",), ("Alpha.md",)]
    assert [commit.paths for commit in docs_b.fake.commits] == [("Beta.md",)]
    assert len(docs_a.fake.pushes) == 2 and len(docs_b.fake.pushes) == 1
    assert (docs_a.workdir / "Alpha.md").read_text() == "# a2\n"
    assert not (docs_a.workdir / "Beta.md").exists() and not (docs_b.workdir / "Alpha.md").exists()


def test_interleaved_applies_carry_only_their_own_credentials(
    pair: tuple[ProviderHarness, ProviderHarness],
) -> None:
    docs_a, docs_b = pair

    docs_a.provider.apply_changes(change_set(create("Alpha.md")))
    docs_b.provider.apply_changes(change_set(create("Beta.md")))
    docs_a.provider.apply_changes(change_set(update("Alpha.md", "# again\n")))

    assert carries(docs_a, TOKEN_A) and carries(docs_b, TOKEN_B)  # the pushes really authenticated
    assert not leaks(docs_a, TOKEN_B)
    assert not leaks(docs_b, TOKEN_A)
    local = [call for call in docs_a.runner.calls if call.argv[0] == "git" and "commit" in call.argv]
    assert local and not any(key.startswith("GIT_CONFIG") for call in local for key in call.env_overrides)


def test_a_pending_path_in_one_clone_is_never_visible_to_the_other(tmp_path: Path) -> None:
    docs_a = profile(tmp_path, "docs-a", allow_auto_commit=False)
    docs_b = profile(tmp_path, "docs-b")

    docs_a.provider.apply_changes(change_set(create("Draft.md", "# draft\n")))
    docs_a.provider.put_asset(asset(), b"img")
    result_b = docs_b.provider.apply_changes(change_set(create("Beta.md")))

    assert set(manifest_of(docs_a)) >= {"Draft.md"} and len(manifest_of(docs_a)) == 2
    assert manifest_of(docs_b) == {}
    assert result_b.results[0].status is APPLIED
    assert docs_b.fake.commits[0].paths == ("Beta.md",)  # not Draft.md, not A's asset
    assert docs_a.fake.commits == []


def test_a_foreign_change_in_one_clone_blocks_only_that_profile(tmp_path: Path) -> None:
    docs_a = profile(tmp_path, "docs-a")
    docs_b = profile(tmp_path, "docs-b")
    docs_a.provider.apply_changes(change_set(create("Alpha.md")))  # syncs A
    (docs_a.workdir / "scratch.txt").write_text("mine")

    blocked = docs_a.provider.apply_changes(change_set(update("Alpha.md", "# changed\n")))
    free = docs_b.provider.apply_changes(change_set(create("Beta.md")))

    assert blocked.results[0].status is FAILED
    assert "[github_wiki:workdir.dirty]" in (blocked.results[0].message or "")
    assert free.results[0].status is APPLIED
    assert len(docs_b.fake.commits) == 1 and len(docs_a.fake.commits) == 1


def test_locks_are_per_clone_so_concurrent_profiles_never_contend(tmp_path: Path) -> None:
    from wikiops.providers.github_wiki.lock import WorkdirLock
    from wikiops.providers.github_wiki.workdir import lock_path

    docs_a = profile(tmp_path, "docs-a")
    docs_b = profile(tmp_path, "docs-b")
    docs_a.provider.apply_changes(change_set(create("Alpha.md")))
    docs_b.provider.apply_changes(change_set(create("Beta.md")))  # both clones exist now

    with WorkdirLock(lock_path(docs_a.fake.git_dir), workdir=docs_a.workdir).hold("A busy"):
        free = docs_b.provider.apply_changes(change_set(update("Beta.md", "# b2\n")))
        blocked = docs_a.provider.apply_changes(change_set(update("Alpha.md", "# a2\n")))

    assert free.results[0].status is APPLIED
    assert "[github_wiki:workdir.locked]" in (blocked.results[0].message or "")


def test_unpushed_commits_of_one_profile_are_not_pushed_by_the_other(tmp_path: Path) -> None:
    docs_a = profile(tmp_path, "docs-a")  # commits, never pushes
    docs_b = profile(tmp_path, "docs-b", allow_auto_push=True)
    docs_a.provider.apply_changes(change_set(create("Alpha.md")))

    docs_b.provider.apply_changes(change_set(create("Beta.md")))

    assert len(docs_a.fake.commits) == 1 and docs_a.fake.pushes == []
    assert docs_b.fake.remote["master"] == docs_b.fake.local
    assert len(docs_b.fake.pushes) == 1
    assert docs_a.fake.remote["master"] == ["c1"]  # A's commit never reached the remote


def test_provider_instances_for_the_same_profile_share_the_clone_and_its_pending_state(tmp_path: Path) -> None:
    first = profile(tmp_path, "docs-a", allow_auto_commit=False)
    first.provider.apply_changes(change_set(create("Home.md", "# v1\n")))

    second = first.rebuild(allow_auto_commit=False)
    result = second.apply_changes(change_set(update("Home.md", "# v2\n")))

    assert result.results[0].status is APPLIED  # the pending path of "the same profile" is its own
