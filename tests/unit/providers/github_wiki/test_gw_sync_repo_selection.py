"""Repository-selection guards of the sync layer (GW-S1, GW-S3, threat matrix rows 1-5).

wikiops must only ever operate on the clone of the configured wiki: an empty
workdir nested inside another repository is cloned into without touching that
repository, ambient ``GIT_DIR`` and friends never reach git, a directory that is
not a complete clone is refused untouched, and an existing clone of another
repository (or reached over the other transport) is a mismatch that is never
"fixed up". Everything runs over the stateful ``FakeWikiGit`` model.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.support.fake_wiki_git import subcommand_and_args
from tests.support.sync_harness import REMOTE, SSH_REMOTE, TOKEN, SyncHarness, build
from wikiops.providers.github_wiki.errors import GithubWikiError

REPOSITORY_SELECTORS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")


def failure(harness: SyncHarness, *, purpose: str = "apply", **options: object) -> GithubWikiError:
    with pytest.raises(GithubWikiError) as caught:
        harness.sync(**options).ensure_ready(purpose)  # type: ignore[arg-type]
    return caught.value


# -- an empty workdir nested inside another repository ---------------------------------


@pytest.fixture
def nested(tmp_path: Path) -> SyncHarness:
    outer = tmp_path / "outer"
    (outer / ".git").mkdir(parents=True)
    workdir = outer / "wiki"
    workdir.mkdir()
    return build(tmp_path, workdir=workdir)


def test_an_empty_workdir_inside_another_repository_is_cloned_into(nested: SyncHarness) -> None:
    state = nested.sync().ensure_ready("apply")

    assert state.workdir == nested.workdir
    assert (nested.workdir / ".git").is_dir()  # the wiki's own repository, not the outer one
    assert nested.subcommands().count("clone") == 1


def test_the_enclosing_repository_is_never_touched(nested: SyncHarness) -> None:
    outer = nested.workdir.parent

    nested.sync().ensure_ready("apply")

    for call in nested.runner.calls:
        command, _ = subcommand_and_args(call.argv)
        if command in {"ls-remote", "clone"}:
            continue  # network commands run from the workdir's parent by design
        assert call.cwd == nested.workdir, call.argv  # no local command ever runs in the outer repo
    forbidden = {"add", "commit", "config", "checkout", "reset", "push", "remote"}
    assert forbidden.isdisjoint(nested.subcommands())
    assert list((outer / ".git").iterdir()) == []


def test_git_never_walks_up_into_the_enclosing_repository(nested: SyncHarness) -> None:
    nested.sync().ensure_ready("apply")

    ceilings = {call.env_overrides["GIT_CEILING_DIRECTORIES"] for call in nested.runner.calls}
    assert ceilings == {str(nested.workdir.parent)}


def test_ambient_repository_selectors_never_reach_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in REPOSITORY_SELECTORS:
        monkeypatch.setenv(name, "/somewhere/else")
    harness = build(tmp_path, cloned=True)

    harness.sync().ensure_ready("apply")

    assert harness.runner.calls
    for call in harness.runner.calls:
        for name in REPOSITORY_SELECTORS:
            assert name in call.env_overrides and call.env_overrides[name] is None, (name, call.argv)


# -- a directory that is not a complete clone of this workdir ----------------------------


def test_a_non_empty_non_git_directory_is_refused_and_nothing_is_deleted(tmp_path: Path) -> None:
    harness = build(tmp_path)
    harness.workdir.mkdir(parents=True)
    (harness.workdir / "mine.txt").write_text("keep")

    error = failure(harness)

    assert error.code == "sync.workdir_not_clone"
    assert (harness.workdir / "mine.txt").read_text() == "keep"
    assert "clone" not in harness.subcommands()
    assert [path.name for path in harness.workdir.iterdir()] == ["mine.txt"]


def test_a_partial_clone_without_a_commit_is_refused(tmp_path: Path) -> None:
    harness = build(tmp_path, cloned=True, head_valid=False)

    error = failure(harness)

    assert error.code == "sync.workdir_not_clone"
    assert "clone" not in harness.subcommands()
    assert "fetch" not in harness.subcommands()


def test_a_workdir_whose_repository_root_is_elsewhere_is_refused(tmp_path: Path) -> None:
    harness = build(tmp_path, cloned=True, top_level=tmp_path / "outer")

    error = failure(harness)

    assert error.code == "sync.workdir_not_clone"
    assert "fetch" not in harness.subcommands()


def test_a_repository_root_reached_through_a_symlink_is_the_same_directory(tmp_path: Path) -> None:
    real = tmp_path / "real-wiki"
    real.mkdir()
    link = tmp_path / "cache" / "wiki"
    link.parent.mkdir()
    link.symlink_to(real, target_is_directory=True)
    harness = build(tmp_path, cloned=True, workdir=real, top_level=link)

    state = harness.sync().ensure_ready("apply")

    assert state.workdir == real


def test_a_workdir_that_is_a_file_is_unusable(tmp_path: Path) -> None:
    harness = build(tmp_path)
    harness.workdir.parent.mkdir(parents=True)
    harness.workdir.write_text("not a directory")

    error = failure(harness)

    assert error.code == "workdir.unusable"
    assert harness.runner.calls == []


# -- the existing clone must be a clone of THIS wiki ---------------------------------------


OTHER_REMOTES = {
    "other-repository": "https://github.com/acme/other.wiki.git",
    "other-owner": "https://github.com/evil/platform.wiki.git",
    "other-host": "https://ghe.acme.io/acme/platform.wiki.git",
    "ssh-instead-of-https": SSH_REMOTE,
    "no-origin": "",
}


@pytest.mark.parametrize("origin", list(OTHER_REMOTES.values()), ids=list(OTHER_REMOTES))
def test_a_clone_of_another_remote_is_a_mismatch_and_origin_is_never_rewritten(
    tmp_path: Path, origin: str
) -> None:
    harness = build(tmp_path, cloned=True)
    harness.fake.origin_url = origin or None

    error = failure(harness)

    assert error.code == "sync.remote_mismatch"
    assert REMOTE in str(error)  # names what was expected, credential-free
    assert harness.fake.origin_url == (origin or None)  # untouched
    assert {"remote", "fetch", "merge"}.isdisjoint(harness.subcommands())
    assert all(args[0] == "--get" for args in config_calls(harness))


def config_calls(harness: SyncHarness) -> list[list[str]]:
    found = []
    for call in harness.runner.calls:
        command, args = subcommand_and_args(call.argv)
        if command == "config":
            found.append(args)
    return found


def test_the_mismatch_names_both_remotes_without_credentials(tmp_path: Path) -> None:
    harness = build(tmp_path, cloned=True)
    harness.fake.origin_url = f"https://user:{TOKEN}@github.com/acme/other.wiki.git"

    error = failure(harness)

    text = str(error)
    assert error.code == "sync.remote_mismatch"
    assert "github.com/acme/other.wiki.git" in text and REMOTE in text
    assert TOKEN not in text and "user:" not in text


def test_an_ssh_clone_with_an_https_profile_hints_at_the_auth_mode(tmp_path: Path) -> None:
    harness = build(tmp_path, cloned=True)
    harness.fake.origin_url = SSH_REMOTE

    error = failure(harness)

    assert error.code == "sync.remote_mismatch"
    assert "auth.mode" in error.hint and "workdir" in error.hint


@pytest.mark.parametrize(
    ("expected", "origin"),
    [
        (REMOTE, REMOTE),
        (REMOTE, "https://GitHub.com/Acme/Platform.wiki.git"),
        (REMOTE, "https://github.com/acme/platform.wiki.git/"),
        (REMOTE, "https://github.com/acme/platform.wiki"),
        (REMOTE, "https://user@github.com/acme/platform.wiki.git"),
        (SSH_REMOTE, SSH_REMOTE),
        (SSH_REMOTE, "git@GITHUB.com:ACME/Platform.wiki.git"),
        (SSH_REMOTE, "ssh://git@github.com/acme/platform.wiki.git"),
        ("file:///srv/wikis/platform.wiki.git", "file:///srv/wikis/platform.wiki.git"),
    ],
    ids=[
        "identical",
        "case-insensitive",
        "trailing-slash",
        "no-dot-git",
        "user-info-ignored",
        "ssh",
        "ssh-case-insensitive",
        "ssh-url-form",
        "file",
    ],
)
def test_an_origin_naming_the_same_wiki_over_the_same_transport_is_accepted(
    tmp_path: Path, expected: str, origin: str
) -> None:
    harness = build(tmp_path, cloned=True, remote_url=expected)
    harness.fake.origin_url = origin

    state = harness.sync().ensure_ready("apply")

    assert state.branch == "master"
    assert harness.fake.origin_url == origin


def test_file_remotes_are_case_sensitive(tmp_path: Path) -> None:
    harness = build(tmp_path, cloned=True, remote_url="file:///srv/Wikis/p.wiki.git")
    harness.fake.origin_url = "file:///srv/wikis/p.wiki.git"

    assert failure(harness).code == "sync.remote_mismatch"


def test_the_identity_checks_also_guard_an_offline_plan(tmp_path: Path) -> None:
    harness = build(tmp_path, cloned=True)
    harness.fake.origin_url = "https://github.com/acme/other.wiki.git"

    error = failure(harness, purpose="plan", sync_on_plan=False)

    assert error.code == "sync.remote_mismatch"
    assert harness.network_calls() == []
    assert harness.strategy.issued == 0


def test_a_clone_made_in_this_run_is_not_checked_against_itself(tmp_path: Path) -> None:
    harness = build(tmp_path)

    harness.sync().ensure_ready("apply")

    assert "config" not in harness.subcommands()  # origin was just set from the same remote
    assert os.path.isdir(harness.workdir / ".git")
