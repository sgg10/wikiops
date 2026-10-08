"""Real git: pushing to a bare remote, rejections and what a push never does (GW-S11..S13).

The provider pushes ``HEAD:refs/heads/<branch>`` with ``--porcelain`` over
``file://``. A rejection is produced for real by another clone pushing first.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from wikiops_sdk.domain import OperationStatus

from tests.support.real_git import SEED_PAGE, Wiki
from tests.support.write_ops import ScriptedResolver, change_set, create, ref
from wikiops.providers.github_wiki.errors import GithubWikiError

pytestmark = pytest.mark.git_integration

FORCE_FORMS = ("--force", "-f", "--force-with-lease", "--mirror", "--delete", "--set-upstream", "-u", "--all")


def statuses(result) -> list[OperationStatus]:  # noqa: ANN001
    return [item.status for item in result.results]


def messages(result) -> str:  # noqa: ANN001
    return " | ".join(item.message or "" for item in result.results)


# -- a successful push ---------------------------------------------------------------------------


@pytest.mark.parametrize("head", ["master", "main"])
def test_the_commit_reaches_the_bare_remote(make_wiki: Callable[..., Wiki], head: str) -> None:
    wiki = make_wiki(head=head, allow_auto_push=True)

    result = wiki.provider().apply_changes(change_set(create("Guide.md", "# Guide\n")))

    assert statuses(result) == [OperationStatus.APPLIED]
    assert f"pushed to {head}" in messages(result)
    assert wiki.remote.rev() == wiki.head()
    assert wiki.remote.subjects()[0] == "docs(wiki): update via wikiops plugin azure-docs"
    assert wiki.remote.show("Guide.md") == "# Guide\n"
    assert wiki.git("rev-list", "--count", f"origin/{head}..HEAD").strip() == "0"


def test_the_push_never_carries_a_force_flag_and_names_the_branch_explicitly(wiki: Wiki) -> None:
    wiki.provider(allow_auto_push=True).apply_changes(change_set(create("Guide.md", "# Guide\n")))

    pushes = wiki.runner.with_subcommand("push")
    assert len(pushes) == 1
    argv = pushes[0]
    assert argv[argv.index("push") :] == ("push", "--porcelain", "origin", "HEAD:refs/heads/master")
    assert not set(argv) & set(FORCE_FORMS)
    assert any(part.startswith("core.hooksPath=") for part in argv)  # hooks off on the network command


def test_earlier_unpushed_commits_reach_the_remote_with_the_next_push(wiki: Wiki) -> None:
    wiki.provider().apply_changes(change_set(create("First.md", "# first\n")))  # push disabled
    assert wiki.remote.files() == [SEED_PAGE]

    result = wiki.provider(allow_auto_push=True).apply_changes(change_set(create("Second.md", "# second\n")))

    assert statuses(result) == [OperationStatus.APPLIED]
    assert wiki.remote.files() == sorted([SEED_PAGE, "First.md", "Second.md"])
    assert wiki.remote.rev() == wiki.head() and wiki.remote.subjects().count(wiki.subjects()[0]) == 2


def test_an_identical_apply_with_unpushed_commits_still_pushes_them(wiki: Wiki) -> None:
    wiki.provider().apply_changes(change_set(create("First.md", "# first\n")))  # committed, not pushed

    result = wiki.provider(allow_auto_push=True).apply_changes(change_set(create("First.md", "# first\n")))

    assert wiki.remote.rev() == wiki.head() and "First.md" in wiki.remote.files()
    assert OperationStatus.FAILED not in statuses(result)


def test_a_branch_override_is_the_push_target(wiki: Wiki) -> None:
    wiki.remote.edit(SEED_PAGE, "# docs branch\n", branch="docs")
    master_before = wiki.remote.rev("master")

    result = wiki.provider(branch="docs", allow_auto_push=True).apply_changes(
        change_set(create("Guide.md", "# Guide\n"))
    )

    assert statuses(result) == [OperationStatus.APPLIED] and "pushed to docs" in messages(result)
    assert wiki.remote.show("Guide.md", "docs") == "# Guide\n"
    assert wiki.remote.rev("master") == master_before and "Guide.md" not in wiki.remote.files("master")


def test_push_default_matching_pushes_nothing_but_the_wiki_branch(wiki: Wiki) -> None:
    wiki.remote.edit("Side.md", "# side\n", branch="side")
    side_before = wiki.remote.rev("side")
    wiki.provider().exists(ref(SEED_PAGE))
    wiki.git("config", "push.default", "matching")
    wiki.git("branch", "side", "origin/side")
    wiki.git("checkout", "-q", "side")
    wiki.git("commit", "-q", "--allow-empty", "-m", "local-only work on side")
    wiki.git("checkout", "-q", "master")

    result = wiki.provider(allow_auto_push=True).apply_changes(change_set(create("Guide.md", "# Guide\n")))

    assert statuses(result) == [OperationStatus.APPLIED]
    assert wiki.remote.rev("master") == wiki.head()
    assert wiki.remote.rev("side") == side_before  # `matching` would have pushed it


# -- a rejected push -------------------------------------------------------------------------------


def test_a_concurrent_remote_edit_rejects_the_push_and_keeps_the_local_commit(wiki: Wiki) -> None:
    resolver = ScriptedResolver()
    wiki.resolver = resolver
    provider = wiki.provider(allow_auto_push=True)
    provider.exists(ref(SEED_PAGE))
    assert resolver.backend is not None
    concurrent: list[str] = []
    resolver.backend.before = lambda: concurrent.append(wiki.remote.edit("Web.md", "# web\n"))

    result = provider.apply_changes(change_set(create("Guide.md", "# Guide\n")))

    assert statuses(result) == [OperationStatus.FAILED]
    text = messages(result)
    local = wiki.head()
    assert "push.rejected" in text and local[:7] in text and str(wiki.workdir) in text
    assert "push rejected" in text
    assert wiki.remote.rev() == concurrent[0] and "Guide.md" not in wiki.remote.files()  # nothing forced
    assert wiki.subjects()[0] == "docs(wiki): update via wikiops plugin azure-docs"  # commit retained
    assert wiki.status() == []
    assert len(wiki.runner.with_subcommand("push")) == 1  # no retry
    for forbidden in ("rebase", "pull", "reset"):
        assert wiki.runner.with_subcommand(forbidden) == []

    with pytest.raises(GithubWikiError) as next_run:
        wiki.provider().exists(ref(SEED_PAGE))

    assert next_run.value.code == "sync.diverged" and local[:7] in str(next_run.value)
    assert wiki.head() == local  # and the next run changed nothing
