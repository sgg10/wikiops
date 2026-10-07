"""Publisher push: explicit refspec, porcelain classification, no retry (GW-S11, S12, A10).

Push runs only when enabled and something is there to push (a new commit or
earlier unpushed ones), through the git facade (hooks disabled, per-call
credential). A rejection or any other failure keeps the local commit, names its
short sha and the workdir, and is never retried, rebased or forced.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.fake_wiki_git import sha_for
from tests.support.publisher_harness import PublisherHarness, build_publisher
from tests.support.sync_harness import TOKEN
from wikiops.providers.github_wiki.publisher import PublishOutcome

FORCE_FORMS = ("--force", "-f", "--force-with-lease", "--mirror", "--delete", "--set-upstream", "-u")
NO_REWRITE = {"rebase", "pull", "reset", "merge", "fetch", "checkout", "switch", "stash"}


def short(label: str) -> str:
    return sha_for(label)[:7]


@pytest.fixture
def pusher(tmp_path: Path) -> PublisherHarness:
    return build_publisher(tmp_path, push=True)


def pushed_page(harness: PublisherHarness, name: str = "Home.md", **options: object) -> PublishOutcome:
    harness.write(name)
    return harness.publish([name], **options)


# -- push disabled -----------------------------------------------------------------------------


def test_with_push_disabled_the_commit_stays_local_and_the_note_says_so(tmp_path: Path) -> None:
    wiki = build_publisher(tmp_path, push=False)

    outcome = pushed_page(wiki)

    assert "push" not in wiki.subcommands()
    assert outcome.committed is True and outcome.pushed is False and outcome.error is None
    assert outcome.sha == sha_for("w1")
    assert outcome.note == f"committed locally at {short('w1')} in '{wiki.workdir}'; not pushed"
    assert wiki.fake.remote["master"] == ["c1"]  # the remote is unchanged
    assert wiki.strategy.issued == 0  # no credential was ever requested


def test_push_disabled_ignores_earlier_unpushed_commits(tmp_path: Path) -> None:
    wiki = build_publisher(tmp_path, push=False)

    outcome = wiki.publish([], unpushed=4)

    assert "push" not in wiki.subcommands() and outcome.pushed is False


# -- push enabled ---------------------------------------------------------------------------------


def test_the_push_argv_is_explicit_porcelain_and_hooks_disabled(pusher: PublisherHarness) -> None:
    outcome = pushed_page(pusher)

    (argv,) = pusher.fake.pushes
    assert argv[:2] == ("git", "-c") and argv[2].startswith("core.hooksPath=")
    assert argv[3:] == ("push", "--porcelain", "origin", "HEAD:refs/heads/master")
    assert not any(form in argv for form in FORCE_FORMS)
    assert not any(part.startswith(("+", "refs/heads/")) for part in argv[3:])
    assert pusher.fake.remote["master"] == ["c1", "w1"]
    assert outcome.pushed is True and outcome.committed is True and outcome.error is None
    assert outcome.note == f"committed {short('w1')}, pushed to master"


def test_the_hooks_path_is_an_empty_scratch_directory_and_only_the_push_gets_it(pusher: PublisherHarness) -> None:
    pushed_page(pusher)

    hooks_dirs = [
        part.removeprefix("core.hooksPath=")
        for call in pusher.runner.calls
        for part in call.argv
        if part.startswith("core.hooksPath=")
    ]
    assert len(hooks_dirs) == 1  # add, diff, status, commit and rev-parse never carry it
    assert not Path(hooks_dirs[0]).exists()  # per-command scratch directory, removed afterwards
    assert str(pusher.workdir) not in hooks_dirs[0]


def test_only_the_push_receives_credentials_and_a_fresh_transport(pusher: PublisherHarness) -> None:
    pushed_page(pusher)

    for call in pusher.runner.calls:
        carries_token = any(value == TOKEN for value in call.env_overrides.values())
        is_push = "push" in call.argv
        assert carries_token == is_push
    assert pusher.strategy.issued == 1


@pytest.mark.parametrize("branch", ["master", "wiki", "docs/v2"])
def test_the_push_targets_the_resolved_branch_explicitly(tmp_path: Path, branch: str) -> None:
    wiki = build_publisher(tmp_path, push=True, branch=branch, remote={branch: ["c1"]}, local_branch=branch)

    pushed_page(wiki)

    assert wiki.fake.pushes[0][-1] == f"HEAD:refs/heads/{branch}"
    assert wiki.fake.pushes[0][-2] == "origin"


def test_earlier_unpushed_commits_are_pushed_with_the_new_one(pusher: PublisherHarness) -> None:
    pusher.fake.local.append("old-unpushed")

    outcome = pushed_page(pusher, unpushed=1)

    assert pusher.fake.remote["master"] == ["c1", "old-unpushed", "w1"]
    assert outcome.pushed is True


def test_unpushed_commits_are_pushed_even_when_nothing_new_was_committed(pusher: PublisherHarness) -> None:
    pusher.fake.local.append("old-unpushed")

    outcome = pusher.publish([], unpushed=1)

    assert pusher.fake.commits == []
    assert len(pusher.fake.pushes) == 1
    assert pusher.fake.remote["master"] == ["c1", "old-unpushed"]
    assert outcome.committed is False and outcome.pushed is True
    assert outcome.sha == sha_for("old-unpushed")  # HEAD
    assert outcome.note == f"committed {short('old-unpushed')}, pushed to master"


def test_nothing_new_and_nothing_unpushed_pushes_nothing(pusher: PublisherHarness) -> None:
    outcome = pusher.publish([], unpushed=0)

    assert pusher.fake.pushes == [] and outcome.pushed is False and outcome.note == ""
    assert pusher.strategy.issued == 0


def test_an_identical_reapply_does_not_push_again(pusher: PublisherHarness) -> None:
    pushed_page(pusher)
    pusher.fake.pushes.clear()
    (pusher.workdir / "Home.md").write_text("# page\n")  # same bytes, clean in git

    outcome = pusher.publish(["Home.md"], unpushed=0)

    assert pusher.fake.pushes == [] and outcome.committed is False


# -- rejection (GW-S12) --------------------------------------------------------------------------------


def test_a_rejected_push_keeps_the_commit_and_names_sha_and_workdir(pusher: PublisherHarness) -> None:
    pusher.fake.remote["master"] = ["c1", "someone-else"]  # the remote advanced after the sync

    outcome = pushed_page(pusher)

    error = outcome.error
    assert error is not None and error.code == "push.rejected"
    assert error.summary == f"committed locally at {short('w1')} in '{pusher.workdir}', push rejected"
    assert error.context["sha"] == short("w1") and error.context["workdir"] == str(pusher.workdir)
    assert "pull or rebase in the workdir" in error.hint
    assert outcome.committed is True and outcome.pushed is False and outcome.sha == sha_for("w1")
    assert outcome.note == ""  # a failure has no success suffix
    assert pusher.fake.remote["master"] == ["c1", "someone-else"]  # remote unchanged
    assert pusher.fake.local[-1] == "w1"  # local commit retained
    assert [item.committed for item in outcome.paths] == [True]
    assert pusher.manifest.entries() == {}  # committed, so no longer pending


def test_a_rejection_is_never_retried_rebased_or_forced(pusher: PublisherHarness) -> None:
    pusher.fake.remote["master"] = ["c1", "someone-else"]

    pushed_page(pusher)

    assert len(pusher.fake.pushes) == 1
    assert NO_REWRITE.isdisjoint(pusher.subcommands())
    assert not any(form in pusher.fake.pushes[0] for form in FORCE_FORMS)


# -- other push failures --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stderr", "code"),
    [
        (f"fatal: Authentication failed for 'https://x-access-token:{TOKEN}@github.com/a/b.wiki.git/'\n", "auth.rejected"),
        ("git@github.com: Permission denied (publickey).\nfatal: Could not read from remote repository.\n", "auth.rejected"),
        ("fatal: unable to access 'https://github.com/a/b.wiki.git/': Could not resolve host: github.com\n", "network.unreachable"),
        ("ssh: connect to host github.com port 22: Connection refused\n", "network.unreachable"),
        ("remote: Repository not found.\nfatal: repository 'https://github.com/a/b.wiki.git/' not found\n", "wiki.not_initialized"),
        ("error: something odd happened\n", "push.failed"),
    ],
    ids=["https-auth", "ssh-auth", "dns", "ssh-refused", "not-found", "unclassified"],
)
def test_other_push_failures_keep_their_code_and_name_sha_and_workdir(
    pusher: PublisherHarness, stderr: str, code: str
) -> None:
    pusher.fake.fail["push"] = (128, stderr)

    outcome = pushed_page(pusher)

    error = outcome.error
    assert error is not None and error.code == code
    assert f"committed locally at {short('w1')} in '{pusher.workdir}'" in str(error)
    assert error.context["sha"] == short("w1")
    assert TOKEN not in str(error) and "x-access-token" not in str(error)
    assert outcome.committed is True and outcome.pushed is False
    assert pusher.fake.local[-1] == "w1"
    assert len([call for call in pusher.runner.calls if "push" in call.argv]) == 1  # no retry


def test_a_push_timeout_is_push_failed_naming_the_timeout(pusher: PublisherHarness) -> None:
    pusher.fake.timeout_on.add("push")

    outcome = pushed_page(pusher)

    error = outcome.error
    assert error is not None and error.code == "push.failed"
    assert "timed out" in error.summary
    assert f"committed locally at {short('w1')}" in error.summary
    assert outcome.committed is True and outcome.pushed is False


def test_a_remote_refusal_line_is_push_failed(pusher: PublisherHarness) -> None:
    def refuse(call):  # a hook on the server side declined the update
        from wikiops.providers.github_wiki.ports import CommandResult

        return CommandResult(
            call.argv,
            1,
            "To x\n!\tHEAD:refs/heads/master\t[remote rejected] (pre-receive hook declined)\nDone\n",
            "error: failed to push some refs\n",
        )

    pusher.runner.script_with(["git", "-c"], refuse)  # push is the only `-c` command in this run

    outcome = pushed_page(pusher)

    assert outcome.error is not None and outcome.error.code == "push.failed"
    assert "remote rejected" in str(outcome.error)


def test_a_push_failure_after_unpushed_only_still_names_head(pusher: PublisherHarness) -> None:
    pusher.fake.local.append("old-unpushed")
    pusher.fake.remote["master"] = ["c1", "someone-else"]

    outcome = pusher.publish([], unpushed=1)

    assert outcome.error is not None and outcome.error.code == "push.rejected"
    assert outcome.error.context["sha"] == short("old-unpushed")
    assert outcome.committed is False  # nothing new; the earlier commits are retained too
