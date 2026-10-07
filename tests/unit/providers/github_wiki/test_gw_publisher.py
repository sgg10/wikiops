"""Publisher: exact staging, one commit, identity and message (GW-S8, S9, S10, S13).

``Publisher.publish`` stages exactly the paths wikiops wrote plus the pending
paths that still hash like the manifest says (never ``-A``, ``.`` or ``commit -a``),
commits only those paths with the identity of the configured mode and the
rendered message, keeps the manifest honest in both outcomes, and reports a failed
commit as ``commit.failed`` ("written but not committed") with the paths left
pending and the staged state untouched. Everything runs over ``FakeWikiGit``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.publisher_harness import PublisherHarness, build_publisher
from wikiops.providers.github_wiki.errors import GithubWikiError
from wikiops.providers.github_wiki.publisher import Publisher, render_commit_message
from wikiops.providers.github_wiki.settings import CommitSettings

ADD = ("git", "--literal-pathspecs", "add", "--")
BOT = ("wikiops", "wikiops@users.noreply.github.com")
FORBIDDEN_STAGING = {"-A", "--all", ".", "-a", "-u", "--update", "--force", "-f"}
REWRITING = {"reset", "restore", "checkout", "switch", "stash", "rebase", "clean", "rm"}


@pytest.fixture
def wiki(tmp_path: Path) -> PublisherHarness:
    return build_publisher(tmp_path)


# -- exact staging ---------------------------------------------------------------------------


def test_only_the_written_paths_are_staged_and_committed(wiki: PublisherHarness) -> None:
    wiki.write("Home.md")
    wiki.write("assets/a1.png", "png")
    wiki.fake.dirty.append("?? scratch.tmp")  # unrelated untracked file

    outcome = wiki.publish(["Home.md", "assets/a1.png"])

    assert wiki.argvs("add") == [(*ADD, "Home.md", "assets/a1.png")]
    (commit,) = wiki.fake.commits
    assert commit.paths == ("Home.md", "assets/a1.png")
    assert outcome.committed is True
    assert wiki.fake.dirty == ["?? scratch.tmp"]  # never staged, never committed


def test_staging_and_commit_never_use_a_broad_or_pathless_form(wiki: PublisherHarness) -> None:
    wiki.write("Home.md")

    wiki.publish(["Home.md"])

    for argv in wiki.argvs("add") + wiki.argvs("commit") + wiki.argvs("diff"):
        assert FORBIDDEN_STAGING.isdisjoint(argv)
        assert argv[1] == "--literal-pathspecs"
        assert argv.index("--") < len(argv) - 1  # at least one path after the separator
    assert REWRITING.isdisjoint(wiki.subcommands())


@pytest.mark.parametrize("name", ["a*b.md", ":(top)x.md", "[x].md", "-lead.md"])
def test_pathspecs_are_literal(tmp_path: Path, name: str) -> None:
    wiki = build_publisher(tmp_path)
    wiki.write(name)

    wiki.publish([name])

    assert wiki.argvs("add") == [(*ADD, name)]
    assert wiki.fake.commits[0].paths == (name,)
    assert all(argv[1] == "--literal-pathspecs" for argv in wiki.argvs("commit"))


def test_pending_paths_that_still_match_their_hash_ride_along(wiki: PublisherHarness) -> None:
    wiki.write("assets/old.png", "old", record=True)  # an earlier asset-only run
    wiki.write("Home.md")

    outcome = wiki.publish(["Home.md"])

    assert wiki.fake.commits[0].paths == ("Home.md", "assets/old.png")
    assert {(item.path, item.origin) for item in outcome.paths} == {
        ("Home.md", "written"),
        ("assets/old.png", "pending"),
    }
    assert all(item.committed for item in outcome.paths)


def test_foreign_paths_never_ride_along_even_when_staged(wiki: PublisherHarness) -> None:
    wiki.write("Home.md")
    wiki.foreign("Other.md", "M ")  # a foreign staged change
    wiki.foreign("notes.txt")  # a foreign untracked file
    wiki.fake.staged.add("Other.md")

    wiki.publish(["Home.md"])

    (commit,) = wiki.fake.commits
    assert commit.paths == ("Home.md",)
    assert "Other.md" not in wiki.argvs("add")[0] and "notes.txt" not in wiki.argvs("add")[0]
    assert "Other.md" in wiki.fake.staged  # untouched: not unstaged, not committed
    assert {"M  Other.md", "?? notes.txt"} <= set(wiki.fake.dirty)


def test_a_pending_path_edited_after_the_write_is_foreign_and_left_alone(wiki: PublisherHarness) -> None:
    wiki.write("Home.md", "mine", record=True)
    (wiki.workdir / "Home.md").write_text("edited by the user")
    wiki.write("Other.md")

    wiki.publish(["Other.md"])

    assert wiki.fake.commits[0].paths == ("Other.md",)
    assert "Home.md" not in wiki.argvs("add")[0]


def test_written_paths_are_deduplicated_and_sorted(wiki: PublisherHarness) -> None:
    for name in ("b.md", "a.md"):
        wiki.write(name)

    wiki.publish(["b.md", "a.md", "b.md"])

    assert wiki.argvs("add") == [(*ADD, "a.md", "b.md")]


def test_a_pending_only_publish_commits_the_leftovers(wiki: PublisherHarness) -> None:
    wiki.write("assets/old.png", "old", record=True)

    outcome = wiki.publish([])

    assert wiki.fake.commits[0].paths == ("assets/old.png",)
    assert outcome.committed is True


def test_nothing_written_and_nothing_pending_runs_no_git_command_that_writes(wiki: PublisherHarness) -> None:
    outcome = wiki.publish([])

    assert outcome.committed is False and outcome.sha is None and outcome.error is None
    assert {"add", "commit", "diff"}.isdisjoint(wiki.subcommands())


# -- idempotency (GW-S13) -------------------------------------------------------------------


def test_identical_content_produces_no_empty_commit(wiki: PublisherHarness) -> None:
    (wiki.workdir / "Home.md").write_text("# same as HEAD\n")  # clean in git: not dirty
    wiki.manifest.record(["Home.md"])

    outcome = wiki.publish(["Home.md"])

    assert wiki.fake.commits == []
    assert not {"add", "diff", "commit"} & set(wiki.subcommands())  # git status already said: nothing differs
    assert outcome.committed is False and outcome.sha is None and outcome.error is None
    assert outcome.note == ""
    assert wiki.manifest.entries() == {}  # nothing left pending


def test_a_changed_page_is_committed_where_an_unchanged_one_is_not(wiki: PublisherHarness) -> None:
    (wiki.workdir / "Same.md").write_text("same")
    wiki.write("Changed.md")

    outcome = wiki.publish(["Changed.md", "Same.md"])

    assert outcome.committed is True
    assert wiki.fake.commits[0].paths == ("Changed.md", "Same.md")  # the whole stage set, once one is dirty


# -- manifest ----------------------------------------------------------------------------------


def test_committed_paths_leave_the_manifest_and_clean_entries_are_pruned(wiki: PublisherHarness) -> None:
    wiki.write("Home.md")
    (wiki.workdir / "Stale.md").write_text("committed by the user meanwhile")
    wiki.manifest.record(["Stale.md"])  # recorded, but no longer dirty in git

    wiki.publish(["Home.md"])

    assert wiki.manifest.entries() == {}
    assert "Stale.md" not in wiki.argvs("add")[0]  # a clean path is not staged


def test_a_failed_commit_keeps_the_dirty_entries_and_still_prunes_the_clean_ones(
    wiki: PublisherHarness,
) -> None:
    wiki.write("Home.md", record=True)
    (wiki.workdir / "Stale.md").write_text("clean")
    wiki.manifest.record(["Stale.md"])
    wiki.fake.fail["commit"] = (1, "hook declined\n")

    wiki.publish(["Home.md"])

    assert set(wiki.manifest.entries()) == {"Home.md"}


def test_written_paths_are_recorded_so_a_failed_commit_leaves_them_pending(wiki: PublisherHarness) -> None:
    wiki.write("Home.md")  # NOT recorded by the caller
    wiki.fake.fail["commit"] = (1, "hook declined\n")

    outcome = wiki.publish(["Home.md"])

    assert outcome.error is not None
    assert set(wiki.manifest.entries()) == {"Home.md"}


def test_the_manifest_hash_is_the_written_bytes(wiki: PublisherHarness) -> None:
    wiki.write("Home.md", "version one")
    wiki.fake.fail["commit"] = (1, "hook declined\n")
    wiki.publish(["Home.md"])
    first = wiki.manifest.entries()["Home.md"]

    (wiki.workdir / "Home.md").write_text("version two")
    wiki.publish(["Home.md"])

    assert wiki.manifest.entries()["Home.md"] != first


# -- commit identity (GW-S10) ---------------------------------------------------------------


IDENTITY_KEYS = ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL")


def test_git_identity_adds_no_overrides_and_uses_the_configured_user(wiki: PublisherHarness) -> None:
    wiki.write("Home.md")

    wiki.publish(["Home.md"])

    (commit,) = wiki.fake.commits
    assert all(commit.env.get(key) is None for key in IDENTITY_KEYS)
    assert commit.author == commit.committer == wiki.fake.git_identity


def test_git_identity_missing_is_commit_identity_missing_and_keeps_the_paths_pending(
    tmp_path: Path,
) -> None:
    wiki = build_publisher(tmp_path, git_identity=None)
    wiki.write("Home.md")

    outcome = wiki.publish(["Home.md"])

    assert outcome.error is not None and outcome.error.code == "commit.identity_missing"
    assert "written but not committed" in str(outcome.error)
    assert outcome.committed is False
    assert set(wiki.manifest.entries()) == {"Home.md"}
    assert "configure git user.name" in outcome.error.hint


@pytest.mark.parametrize(
    ("commit", "expected"),
    [
        ({"identity": {"mode": "bot"}}, BOT),
        ({"identity": {"mode": "bot", "name": "docs-bot", "email": "docs@acme.test"}}, ("docs-bot", "docs@acme.test")),
        ({"identity": {"mode": "custom", "name": "Ana Gómez", "email": "ana@acme.test"}}, ("Ana Gómez", "ana@acme.test")),
    ],
    ids=["bot-default", "bot-configured", "custom"],
)
def test_bot_and_custom_identities_are_passed_through_the_environment_only(
    tmp_path: Path, commit: dict, expected: tuple[str, str]
) -> None:
    wiki = build_publisher(tmp_path, commit=commit, git_identity=None)  # no git user at all
    wiki.write("Home.md")

    wiki.publish(["Home.md"])

    (made,) = wiki.fake.commits
    assert made.author == made.committer == expected
    assert made.env["GIT_AUTHOR_NAME"] == made.env["GIT_COMMITTER_NAME"] == expected[0]
    assert made.env["GIT_AUTHOR_EMAIL"] == made.env["GIT_COMMITTER_EMAIL"] == expected[1]


def test_the_git_configuration_is_never_touched(tmp_path: Path) -> None:
    wiki = build_publisher(tmp_path, commit={"identity": {"mode": "custom", "name": "N", "email": "n@x.test"}})
    wiki.write("Home.md")

    wiki.publish(["Home.md"])

    assert "config" not in wiki.subcommands()
    for call in wiki.runner.calls:
        assert "-c" not in call.argv
        assert not any(part.startswith(("user.", "--global", "--local")) for part in call.argv)


# -- message template (GW-S10) --------------------------------------------------------------------


def test_the_default_message_names_the_plugin(wiki: PublisherHarness) -> None:
    wiki.write("Home.md")

    wiki.publish(["Home.md"], plugin_id="azure-docs")

    assert wiki.fake.commits[0].message == "docs(wiki): update via wikiops plugin azure-docs"


def test_every_placeholder_is_rendered(tmp_path: Path) -> None:
    wiki = build_publisher(
        tmp_path,
        commit={"message": "{plugin_id}/{provider_name}: {page_count} page(s)"},
        provider_name="docs-a",
    )
    wiki.write("Home.md")
    wiki.write("Other.md")

    wiki.publish(["Home.md", "Other.md"], plugin_id="p", page_count=2)

    assert wiki.fake.commits[0].message == "p/docs-a: 2 page(s)"


@pytest.mark.parametrize(
    "plugin_id",
    ["$(touch /tmp/pwn)", "`id`; rm -rf /", "a\"b'c", "%s %d {x} {provider_name}", "-m evil", "line1\nline2", "ünï"],
)
def test_the_message_is_rendered_without_shell_or_template_interpretation(
    tmp_path: Path, plugin_id: str
) -> None:
    wiki = build_publisher(tmp_path)
    wiki.write("Home.md")

    wiki.publish(["Home.md"], plugin_id=plugin_id)

    assert wiki.fake.commits[0].message == f"docs(wiki): update via wikiops plugin {plugin_id}"
    (argv,) = wiki.argvs("commit")
    assert argv[argv.index("-m") + 1] == wiki.fake.commits[0].message  # one argv element
    assert wiki.runner.calls[0].argv[0] == "git"  # list form, never a shell string


def test_escaped_braces_in_the_template_are_literal_and_nul_is_dropped(tmp_path: Path) -> None:
    wiki = build_publisher(tmp_path, commit={"message": "{{done}} {plugin_id}"})
    wiki.write("Home.md")

    wiki.publish(["Home.md"], plugin_id="a\x00b")

    assert wiki.fake.commits[0].message == "{done} ab"


# -- credentials never reach local commands -----------------------------------------------------


def test_add_diff_and_commit_carry_no_credentials_and_no_transport(wiki: PublisherHarness) -> None:
    wiki.write("Home.md")

    wiki.publish(["Home.md"])

    assert wiki.strategy.issued == 0
    for call in wiki.runner.calls:
        assert not any(key.startswith("GIT_CONFIG") and value for key, value in call.env_overrides.items())
        assert not any(part.startswith("core.hooksPath=") for part in call.argv)  # local hooks stay the user's
        assert call.cwd == wiki.workdir


# -- commit failure (GW-S9) -------------------------------------------------------------------------


def test_a_failed_commit_is_written_but_not_committed_with_the_workdir(wiki: PublisherHarness) -> None:
    wiki.write("Home.md")
    wiki.fake.fail["commit"] = (1, "pre-commit hook failed: lint\n")

    outcome = wiki.publish(["Home.md"])

    error = outcome.error
    assert error is not None and error.code == "commit.failed"
    assert "written but not committed" in str(error)
    assert str(wiki.workdir) in str(error)
    assert "pre-commit hook failed: lint" in str(error)  # the redacted stderr tail
    assert outcome.committed is False and outcome.sha is None and outcome.pushed is False
    assert [item.committed for item in outcome.paths] == [False]
    assert set(wiki.manifest.entries()) == {"Home.md"}


def test_a_failed_commit_leaves_the_staged_state_for_inspection(wiki: PublisherHarness) -> None:
    wiki.write("Home.md")
    wiki.fake.fail["commit"] = (1, "hook declined\n")

    wiki.publish(["Home.md"])

    assert wiki.fake.staged == {"Home.md"}
    assert REWRITING.isdisjoint(wiki.subcommands())


def test_a_failed_commit_never_pushes(tmp_path: Path) -> None:
    wiki = build_publisher(tmp_path, push=True)
    wiki.write("Home.md")
    wiki.fake.fail["commit"] = (1, "hook declined\n")

    wiki.publish(["Home.md"], unpushed=3)

    assert "push" not in wiki.subcommands()


@pytest.mark.parametrize(
    ("failing", "options"),
    [
        ("status", {"fail": (128, "fatal: unable to read the index\n")}),
        ("add", {"fail": (128, "fatal: Unable to create index.lock\n")}),
        ("diff", {"fail": (2, "fatal: bad revision\n")}),
        ("commit", {"timeout": True}),
        ("add", {"timeout": True}),
    ],
    ids=["status-failed", "add-failed", "diff-error", "commit-timeout", "add-timeout"],
)
def test_any_failure_between_staging_and_commit_is_commit_failed(
    wiki: PublisherHarness, failing: str, options: dict
) -> None:
    wiki.write("Home.md")
    if "fail" in options:
        wiki.fake.fail[failing] = options["fail"]
    else:
        wiki.fake.timeout_on.add(failing)

    outcome = wiki.publish(["Home.md"])

    assert outcome.error is not None and outcome.error.code == "commit.failed"
    assert "written but not committed" in str(outcome.error)
    assert str(wiki.workdir) in str(outcome.error)
    assert set(wiki.manifest.entries()) == {"Home.md"}
    assert wiki.fake.commits == []


def test_recovery_after_a_failed_commit_commits_both_pages(wiki: PublisherHarness) -> None:
    wiki.write("Home.md")
    wiki.fake.fail["commit"] = (1, "hook declined\n")
    assert wiki.publish(["Home.md"]).error is not None
    del wiki.fake.fail["commit"]  # the hook problem is fixed
    wiki.write("Other.md")

    outcome = wiki.publish(["Other.md"])

    assert outcome.error is None and outcome.committed is True
    assert wiki.fake.commits[0].paths == ("Home.md", "Other.md")
    assert wiki.manifest.entries() == {}


# -- failures stay in the outcome ------------------------------------------------------------------


def test_a_head_that_cannot_be_read_after_the_commit_is_reported_in_the_outcome(
    wiki: PublisherHarness,
) -> None:
    wiki.write("Home.md")
    wiki.fake.fail["rev-parse"] = (128, "fatal: unable to read HEAD\n")

    outcome = wiki.publish(["Home.md"])  # must not raise: the commit exists

    assert len(wiki.fake.commits) == 1
    assert outcome.committed is True and outcome.sha is None and outcome.pushed is False
    assert [item.committed for item in outcome.paths] == [True]
    error = outcome.error
    assert error is not None and error.code == "sync.git_failed"
    assert "committed locally" in str(error) and "sha could not be read" in str(error)
    assert str(wiki.workdir) in str(error)
    assert "unable to read HEAD" in str(error)
    assert outcome.note == ""
    assert wiki.manifest.entries() == {}  # the commit took the page out of the manifest


def test_a_head_read_that_times_out_after_the_commit_keeps_its_timeout_code(
    wiki: PublisherHarness,
) -> None:
    wiki.write("Home.md")
    wiki.fake.timeout_on.add("rev-parse")

    outcome = wiki.publish(["Home.md"])

    assert outcome.committed is True and outcome.sha is None
    assert outcome.error is not None and outcome.error.code == "sync.timeout"
    assert "committed locally" in str(outcome.error) and str(wiki.workdir) in str(outcome.error)


def test_a_message_that_cannot_be_rendered_fails_before_anything_is_staged(
    wiki: PublisherHarness,
) -> None:
    broken = CommitSettings().model_copy(update={"message": "x {nope}"})
    publisher = Publisher(
        wiki.git, wiki.manifest, wiki.lock, commit=broken, branch="master", provider_name="n", push=False
    )
    wiki.write("Home.md")

    with wiki.lock.hold("apply"):
        outcome = publisher.publish(["Home.md"], plugin_id="p", page_count=1)

    error = outcome.error
    assert error is not None and error.code == "config.invalid_message"  # never recoded
    assert "{nope}" in str(error) and "written but not committed" in str(error)
    assert str(wiki.workdir) in str(error)
    assert "add" not in wiki.subcommands() and "commit" not in wiki.subcommands()
    assert wiki.fake.staged == set()  # nothing was left staged
    assert outcome.committed is False and outcome.sha is None
    assert set(wiki.manifest.entries()) == {"Home.md"}  # still pending for the next apply


def broken_publisher(wiki: PublisherHarness) -> Publisher:
    broken = CommitSettings().model_copy(update={"message": "x {nope}"})
    return Publisher(
        wiki.git, wiki.manifest, wiki.lock, commit=broken, branch="master", provider_name="n", push=False
    )


def test_a_no_op_apply_never_fails_on_the_message_template(wiki: PublisherHarness) -> None:
    (wiki.workdir / "Home.md").write_text("# same as HEAD\n")  # clean in git: not dirty
    wiki.manifest.record(["Home.md"])

    with wiki.lock.hold("apply"):
        outcome = broken_publisher(wiki).publish(["Home.md"], plugin_id="p", page_count=1)

    assert outcome.error is None and outcome.committed is False and wiki.fake.commits == []
    assert wiki.manifest.entries() == {}  # identical to HEAD: nothing left pending


def test_a_mixed_apply_with_a_changed_page_still_fails_on_the_template_before_staging(
    wiki: PublisherHarness,
) -> None:
    (wiki.workdir / "Same.md").write_text("same")
    wiki.write("Changed.md")

    with wiki.lock.hold("apply"):
        outcome = broken_publisher(wiki).publish(["Changed.md", "Same.md"], plugin_id="p", page_count=2)

    assert outcome.error is not None and outcome.error.code == "config.invalid_message"
    assert "add" not in wiki.subcommands() and wiki.fake.staged == set()


def test_a_clean_stage_set_runs_no_staging_command_whatever_the_template_is(
    wiki: PublisherHarness,
) -> None:
    # git status is the authority on what differs from HEAD: with nothing dirty there is
    # nothing to stage, to commit or to render, even if a later `diff --cached` would disagree.
    (wiki.workdir / "Home.md").write_text("# same as HEAD\n")
    wiki.manifest.record(["Home.md"])
    wiki.fake.fail["diff"] = (1, "")

    with wiki.lock.hold("apply"):
        outcome = broken_publisher(wiki).publish(["Home.md"], plugin_id="p", page_count=1)

    assert outcome.error is None and outcome.committed is False and wiki.fake.commits == []
    assert not {"add", "diff", "commit"} & set(wiki.subcommands())
    assert wiki.fake.staged == set()
    assert wiki.manifest.entries() == {}


def test_a_message_failure_never_leaves_a_staged_path_whatever_git_reports_afterwards(
    wiki: PublisherHarness,
) -> None:
    wiki.write("Home.md")
    wiki.fake.fail["diff"] = (1, "")  # whatever a later diff says, the template fails first

    with wiki.lock.hold("apply"):
        outcome = broken_publisher(wiki).publish(["Home.md"], plugin_id="p", page_count=1)

    assert outcome.error is not None and outcome.error.code == "config.invalid_message"
    assert wiki.fake.staged == set()
    assert not {"add", "diff", "commit"} & set(wiki.subcommands())


def test_a_valid_message_is_rendered_once_before_the_first_staging_command(
    wiki: PublisherHarness,
) -> None:
    wiki.write("Home.md")

    outcome = wiki.publish(["Home.md"])

    assert outcome.error is None and len(wiki.fake.commits) == 1
    assert wiki.subcommands().index("add") < wiki.subcommands().index("commit")
    assert wiki.fake.commits[0].message == "docs(wiki): update via wikiops plugin azure-docs"


# -- the workdir lock (GW-S15) ----------------------------------------------------------------------


def test_publishing_without_the_workdir_lock_is_refused_before_any_command(wiki: PublisherHarness) -> None:
    wiki.write("Home.md")

    with pytest.raises(RuntimeError, match="workdir lock"):
        wiki.publisher.publish(["Home.md"], plugin_id="p", page_count=1, unpushed=0)

    assert wiki.runner.calls == []


def test_a_corrupt_manifest_stops_publishing_before_staging(wiki: PublisherHarness) -> None:
    wiki.write("Home.md")
    wiki.manifest.path.parent.mkdir(parents=True, exist_ok=True)
    wiki.manifest.path.write_text("{not json")

    with pytest.raises(GithubWikiError) as caught:
        wiki.publish(["Home.md"])

    assert caught.value.code == "workdir.manifest_corrupt"
    assert "add" not in wiki.subcommands()


def test_a_template_with_an_unknown_placeholder_is_refused_even_if_settings_missed_it() -> None:
    with pytest.raises(GithubWikiError) as caught:
        render_commit_message("x {nope}", plugin_id="p", provider_name="n", page_count=1)

    assert caught.value.code == "config.invalid_message"
    assert "{nope}" in caught.value.summary
