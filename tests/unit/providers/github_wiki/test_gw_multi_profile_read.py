"""Two ``github_wiki`` profiles in one process share nothing on the read path (GW-P13).

Each provider owns its runner view, credentials, clone, lock, manifest and
backend. Two accounts on one host interleave their syncs and each recorded
command carries only its own token; two profiles for the same wiki get two clone
directories; a foreign change in one clone never blocks the other.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from wikiops_sdk.domain import DocumentRef, RefKind

from tests.support.fake_git_runner import RecordedCall
from tests.support.provider_harness import ProviderHarness, build_provider
from wikiops.providers.github_wiki.errors import GithubWikiError

TOKEN_A = "ghp_account_A_SECRET_1"
TOKEN_B = "ghp_account_B_SECRET_2"


def page(path: str = "Home.md") -> DocumentRef:
    return DocumentRef(provider="", kind=RefKind.PATH, locator={"path": path})


def forms(token: str) -> tuple[str, ...]:
    """Every shape a token takes in a command environment or argv."""
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


@pytest.fixture
def accounts(tmp_path: Path) -> tuple[ProviderHarness, ProviderHarness]:
    cache = tmp_path / "cache"
    one = build_provider(
        tmp_path,
        cache_dir=cache,
        settings={
            "provider_name": "wiki-one",
            "repository": "acme/one",
            "workdir": None,
            "auth": {"mode": "gh", "account": "acct-a"},
        },
        gh_token=TOKEN_A,
    )
    two = build_provider(
        tmp_path,
        cache_dir=cache,
        settings={
            "provider_name": "wiki-two",
            "repository": "acme/two",
            "workdir": None,
            "auth": {"mode": "gh", "account": "acct-b"},
        },
        gh_token=TOKEN_B,
    )
    return one, two


def test_interleaved_reads_carry_only_their_own_account_token(
    accounts: tuple[ProviderHarness, ProviderHarness],
) -> None:
    one, two = accounts

    one.provider.resolve_ref(page())
    two.provider.resolve_ref(page())
    one.provider.exists(page())
    two.provider.exists(page())

    assert carries(one, TOKEN_A) and carries(two, TOKEN_B)  # the sync really authenticated
    assert not leaks(one, TOKEN_B)
    assert not leaks(two, TOKEN_A)


def test_each_profile_asks_gh_for_its_own_account_only(
    accounts: tuple[ProviderHarness, ProviderHarness],
) -> None:
    one, two = accounts

    one.provider.resolve_ref(page())
    two.provider.resolve_ref(page())

    def gh_users(harness: ProviderHarness) -> set[str]:
        return {call.argv[call.argv.index("--user") + 1] for call in harness.runner.calls if call.argv[0] == "gh"}

    assert gh_users(one) == {"acct-a"}
    assert gh_users(two) == {"acct-b"}


def test_two_profiles_get_distinct_clones_locks_and_backends(
    accounts: tuple[ProviderHarness, ProviderHarness],
) -> None:
    one, two = accounts

    one.provider.resolve_ref(page())
    two.provider.resolve_ref(page())

    assert one.workdir != two.workdir
    assert (one.workdir / ".git" / "wikiops" / "lock").is_file()
    assert (two.workdir / ".git" / "wikiops" / "lock").is_file()
    assert one.resolver.created == [(one.workdir, "wiki-one", True)]
    assert two.resolver.created == [(two.workdir, "wiki-two", True)]


def test_a_foreign_change_in_one_clone_never_blocks_the_other_profile(
    accounts: tuple[ProviderHarness, ProviderHarness],
) -> None:
    one, two = accounts
    one.fake.dirty = ["?? notes.txt"]

    with pytest.raises(GithubWikiError) as blocked:
        one.provider.resolve_ref(page())
    resolved = two.provider.resolve_ref(page())

    assert blocked.value.code == "workdir.dirty"
    assert resolved.provider == "wiki-two"
    assert one.resolver.created == []
    assert len(two.resolver.created) == 1


def test_same_wiki_two_profile_names_use_two_clone_directories(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    base = {"repository": "acme/platform", "workdir": None}
    docs_a = build_provider(
        tmp_path, cache_dir=cache, settings={**base, "provider_name": "docs-a", "auth": {"mode": "gh", "account": "alice"}}, gh_token=TOKEN_A
    )
    docs_b = build_provider(
        tmp_path, cache_dir=cache, settings={**base, "provider_name": "docs-b", "auth": {"mode": "ssh"}}
    )

    docs_a.provider.resolve_ref(page())
    docs_b.provider.resolve_ref(page())

    assert docs_a.workdir != docs_b.workdir
    assert docs_a.workdir.parent == docs_b.workdir.parent  # same host/owner/repo, keyed by profile
    assert docs_a.provider.describe_target() != docs_b.provider.describe_target()
    assert docs_a.fake.origin_url == "https://github.com/acme/platform.wiki.git"
    assert docs_b.fake.origin_url == "git@github.com:acme/platform.wiki.git"  # no transport crossing


def test_provider_instances_hold_no_module_level_state(tmp_path: Path) -> None:
    first = build_provider(tmp_path / "x", cloned=True)
    second = build_provider(tmp_path / "y", cloned=True)

    first.provider.resolve_ref(page())

    assert second.runner.calls == []
    assert second.resolver.created == []
    assert len(first.resolver.created) == 1
