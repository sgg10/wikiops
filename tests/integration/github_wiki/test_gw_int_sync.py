"""Real git: clone, branch detection, fast-forward, divergence and repository selection (GW-S1..S7, S13).

Each test drives the real provider (``SubprocessGitRunner`` + the real ``local_files``
backend) against a ``file://`` bare repository and inspects the clone with real git.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from wikiops_sdk.domain import OperationStatus

from tests.support.real_git import (
    SEED_CONTENT,
    SEED_PAGE,
    FileTransportStrategy,
    Wiki,
    run_git,
)
from tests.support.write_ops import change_set, create, ref
from wikiops.providers.github_wiki.errors import GithubWikiError
from wikiops.providers.github_wiki.workdir import resolve_workdir

pytestmark = pytest.mark.git_integration


def failure_codes(result) -> list[str]:  # noqa: ANN001
    return [item.message or "" for item in result.results if item.status is OperationStatus.FAILED]


# -- clone and branch detection ----------------------------------------------------------------


@pytest.mark.parametrize("head", ["master", "main"])
def test_the_default_branch_is_detected_whatever_its_name(make_wiki: Callable[..., Wiki], head: str) -> None:
    wiki = make_wiki(head=head)

    document = wiki.provider().get_document(ref(SEED_PAGE))

    assert document.content == SEED_CONTENT
    assert wiki.branch() == head
    assert wiki.git("config", "--get", "remote.origin.url").strip() == wiki.remote.url
    assert wiki.head() == wiki.remote.rev()
    assert wiki.status() == []


def test_a_branch_override_clones_that_branch(wiki: Wiki) -> None:
    wiki.remote.edit(SEED_PAGE, "# From docs\n", branch="docs")

    document = wiki.provider(branch="docs").get_document(ref(SEED_PAGE))

    assert document.content == "# From docs\n"
    assert wiki.branch() == "docs"
    assert wiki.head() == wiki.remote.rev("docs")


def test_an_unknown_branch_override_lists_the_remote_branches_and_clones_nothing(wiki: Wiki) -> None:
    wiki.remote.edit("Other.md", "# o\n", branch="docs")

    with pytest.raises(GithubWikiError) as caught:
        wiki.provider(branch="nope").exists(ref(SEED_PAGE))

    assert caught.value.code == "sync.branch_not_found"
    assert "docs" in str(caught.value) and "master" in str(caught.value)
    assert not wiki.workdir.exists()


def test_a_wiki_that_does_not_exist_is_not_initialized(wiki: Wiki) -> None:
    missing = (wiki.root / "remotes" / "missing.wiki.git").as_uri()
    wiki.strategy = FileTransportStrategy(missing)

    with pytest.raises(GithubWikiError) as caught:
        wiki.provider().exists(ref(SEED_PAGE))

    assert caught.value.code == "wiki.not_initialized"
    assert "auth='file'" in str(caught.value)
    assert not wiki.workdir.exists()


# -- fast-forward and divergence ----------------------------------------------------------------


def test_a_new_run_fast_forwards_to_what_the_remote_gained(wiki: Wiki) -> None:
    wiki.provider().exists(ref(SEED_PAGE))
    first = wiki.head()
    advanced = wiki.remote.edit(SEED_PAGE, "# Edited on the web\n")

    document = wiki.provider().get_document(ref(SEED_PAGE))

    assert document.content == "# Edited on the web\n"
    assert wiki.head() == advanced != first
    assert wiki.commit_count() == 2
    assert wiki.status() == []


def test_a_run_without_remote_changes_is_a_no_op(wiki: Wiki) -> None:
    wiki.provider().exists(ref(SEED_PAGE))
    before = wiki.head()

    wiki.provider().exists(ref(SEED_PAGE))
    wiki.provider().apply_changes(change_set())

    assert wiki.head() == before and wiki.status() == []


def test_a_diverged_clone_is_reported_and_loses_nothing(wiki: Wiki) -> None:
    wiki.provider().apply_changes(change_set(create("Mine.md", "# mine\n")))
    mine = wiki.head()
    theirs = wiki.remote.edit("Theirs.md", "# theirs\n")

    with pytest.raises(GithubWikiError) as caught:
        wiki.provider().exists(ref(SEED_PAGE))

    error = caught.value
    assert error.code == "sync.diverged"
    assert mine[:7] in str(error) and str(wiki.workdir) in str(error)
    assert wiki.head() == mine and "Mine.md" in wiki.committed_files()  # the local commit is intact
    assert wiki.status() == []
    assert wiki.remote.rev() == theirs  # and the remote was not touched


def test_a_clone_that_is_only_ahead_is_left_alone(wiki: Wiki) -> None:
    wiki.provider().apply_changes(change_set(create("Mine.md", "# mine\n")))
    mine = wiki.head()

    note = wiki.provider().describe_target()
    wiki.provider().exists(ref("Mine.md"))

    assert wiki.head() == mine and wiki.remote.subjects() == ["Initial Home page"]
    assert "unpushed_commits=1" in note


def test_a_checked_out_feature_branch_is_a_mismatch_and_head_does_not_move(wiki: Wiki) -> None:
    wiki.provider().exists(ref(SEED_PAGE))
    wiki.git("checkout", "-q", "-b", "feature")
    head = wiki.head()

    with pytest.raises(GithubWikiError) as caught:
        wiki.provider().exists(ref(SEED_PAGE))

    assert caught.value.code == "sync.branch_mismatch"
    assert wiki.branch() == "feature" and wiki.head() == head


# -- foreign changes ------------------------------------------------------------------------------


def test_a_foreign_untracked_file_blocks_plan_and_apply_and_nothing_is_written(wiki: Wiki) -> None:
    wiki.provider().exists(ref(SEED_PAGE))
    (wiki.workdir / "notes.txt").write_text("mine\n")

    with pytest.raises(GithubWikiError) as plan:
        wiki.provider().exists(ref(SEED_PAGE))
    result = wiki.provider().apply_changes(change_set(create("New.md", "# new\n")))

    assert plan.value.code == "workdir.dirty" and "notes.txt" in str(plan.value)
    messages = failure_codes(result)
    assert len(messages) == 1 and "workdir.dirty" in messages[0] and "notes.txt" in messages[0]
    assert not (wiki.workdir / "New.md").exists()
    assert wiki.commit_count() == 1
    assert wiki.status() == ["?? notes.txt"]


def test_a_foreign_edit_of_a_tracked_page_blocks_an_apply(wiki: Wiki) -> None:
    wiki.provider().exists(ref(SEED_PAGE))
    (wiki.workdir / SEED_PAGE).write_text("# edited by hand\n")

    result = wiki.provider().apply_changes(change_set(create("New.md", "# new\n")))

    assert "workdir.dirty" in failure_codes(result)[0] and SEED_PAGE in failure_codes(result)[0]
    assert (wiki.workdir / SEED_PAGE).read_text() == "# edited by hand\n"
    assert wiki.commit_count() == 1


# -- repository selection (threat matrix) ----------------------------------------------------------


def test_an_empty_workdir_inside_another_repository_is_cloned_into_and_the_parent_is_untouched(
    make_wiki: Callable[..., Wiki], tmp_path: Path
) -> None:
    outer = tmp_path / "outer"
    run_git("init", "-q", "--initial-branch=main", str(outer))
    (outer / "README.md").write_text("# outer\n")
    run_git("add", "README.md", cwd=outer)
    run_git("commit", "-q", "-m", "outer commit", cwd=outer)
    nested = outer / "wiki"
    nested.mkdir()
    wiki = make_wiki(workdir=nested)
    outer_head = run_git("rev-parse", "HEAD", cwd=outer)

    result = wiki.provider().apply_changes(change_set(create("New.md", "# new\n")))

    assert result.results[0].status is OperationStatus.APPLIED
    assert run_git("rev-parse", "--show-toplevel", cwd=nested).strip() == str(nested.resolve())
    assert wiki.commit_count() == 2  # the commit landed in the inner clone
    assert run_git("rev-parse", "HEAD", cwd=outer) == outer_head
    assert run_git("ls-files", cwd=outer).split() == ["README.md"]
    assert run_git("status", "--porcelain", cwd=outer).splitlines() == ["?? wiki/"]


def test_repository_selection_variables_in_the_environment_are_ignored(
    wiki: Wiki, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decoy = tmp_path / "decoy"
    run_git("init", "-q", "--initial-branch=main", str(decoy))
    (decoy / "keep.md").write_text("# decoy\n")
    run_git("add", "keep.md", cwd=decoy)
    run_git("commit", "-q", "-m", "decoy commit", cwd=decoy)
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(decoy))
    monkeypatch.setenv("GIT_INDEX_FILE", str(decoy / ".git" / "index"))

    result = wiki.provider().apply_changes(change_set(create("New.md", "# new\n")))

    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(name)
    assert result.results[0].status is OperationStatus.APPLIED
    assert wiki.last_commit_files() == ["New.md"] and wiki.commit_count() == 2
    assert run_git("log", "--format=%s", cwd=decoy).splitlines() == ["decoy commit"]
    assert run_git("status", "--porcelain", cwd=decoy) == ""


def test_a_relative_workdir_resolves_against_the_current_directory(
    make_wiki: Callable[..., Wiki], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    wiki = make_wiki(workdir=Path("rel") / "wiki")  # relative path as the user wrote it
    wiki.raw["workdir"] = "rel/wiki"

    wiki.provider().exists(ref(SEED_PAGE))

    assert (tmp_path / "rel" / "wiki" / ".git").is_dir()
    assert run_git("rev-parse", "--show-toplevel", cwd=tmp_path / "rel" / "wiki").strip() == str(
        (tmp_path / "rel" / "wiki").resolve()
    )


def test_a_workdir_reached_through_a_symlink_is_the_real_directory(make_wiki: Callable[..., Wiki], tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    wiki = make_wiki(workdir=real / "wiki")
    wiki.raw["workdir"] = str(link / "wiki")

    wiki.provider().exists(ref(SEED_PAGE))

    assert (real / "wiki" / ".git").is_dir()
    assert run_git("rev-parse", "--show-toplevel", cwd=link / "wiki").strip() == str((real / "wiki").resolve())
    assert wiki.provider().describe_target().count(str((real / "wiki").resolve())) == 1


def test_profiles_whose_names_differ_only_by_case_use_different_clones(make_wiki: Wiki) -> None:
    wiki = make_wiki()
    wiki.raw["workdir"] = None  # default per-profile workdir below the private cache

    paths = {}
    for name in ("Docs", "docs"):
        provider = wiki.provider(provider_name=name)
        provider.apply_changes(change_set(create(f"{name}.md", f"# {name}\n")))
        paths[name] = resolve_workdir(
            configured=None,
            host="github.com",
            repository="acme/platform",
            provider_name=name,
            cache_dir=wiki.root / "cache",
        )

    upper, lower = paths["Docs"], paths["docs"]
    assert upper != lower and str(upper).casefold() != str(lower).casefold()
    assert sorted(run_git("ls-files", cwd=upper).split()) == ["Docs.md", "Home.md"]
    assert sorted(run_git("ls-files", cwd=lower).split()) == ["Home.md", "docs.md"]
