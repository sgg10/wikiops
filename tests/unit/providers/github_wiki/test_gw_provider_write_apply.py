"""``GithubWikiProvider.apply_changes``: validate, write through the backend, commit, push.

The provider never raises from ``apply_changes``: every failure becomes a FAILED result.
Under the workdir lock it re-checks the clone for foreign changes, validates each
operation against the flat page policy (failures are not delegated), hands the rest to
the backend in ONE call, learns what was written only from the SDK result fields,
publishes (stage, one commit, optional push) and maps publish errors onto every operation
that was written. The manifest tracks written paths in both commit modes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from wikiops_sdk.domain import (
    ApplyResult,
    CreateDocumentOperation,
    DocumentRef,
    OperationStatus,
    RefKind,
)

from tests.support.fake_wiki_git import sha_for
from tests.support.provider_harness import ProviderHarness, build_provider
from tests.support.write_ops import (
    PLUGIN,
    ScriptedBackend,
    ScriptedResolver,
    asset,
    asset_ref,
    change_set,
    child,
    create,
    ref,
    result_for,
    update,
)
from wikiops.providers._fs import content_version
from wikiops.providers.github_wiki.errors import GithubWikiError
from wikiops.providers.github_wiki.lock import WorkdirLock
from wikiops.providers.github_wiki.manifest import PendingManifest
from wikiops.providers.github_wiki.workdir import lock_path, manifest_path

APPLIED, SKIPPED, FAILED = OperationStatus.APPLIED, OperationStatus.SKIPPED, OperationStatus.FAILED


def build(tmp_path: Path, **options: Any) -> ProviderHarness:
    return build_provider(tmp_path, cloned=True, track_files=True, **options)


def scripted(tmp_path: Path, **options: Any) -> tuple[ProviderHarness, ScriptedResolver]:
    resolver = ScriptedResolver()
    return build(tmp_path, backends=resolver, **options), resolver


def backend_of(resolver: ScriptedResolver) -> ScriptedBackend:
    assert resolver.backend is not None
    return resolver.backend


def manifest_of(harness: ProviderHarness) -> dict[str, str]:
    return PendingManifest(manifest_path(harness.fake.git_dir), workdir=harness.workdir).entries()


def seed_committed(harness: ProviderHarness, path: str, content: str) -> None:
    """A page that is already committed in the clone (clean)."""
    (harness.workdir / path).write_text(content)
    harness.fake.committed_files[path] = content.encode()


def short(harness: ProviderHarness) -> str:
    return sha_for(harness.fake.local[-1])[:7]


def on_disk(harness: ProviderHarness) -> set[str]:
    return {
        found.relative_to(harness.workdir).as_posix()
        for found in harness.workdir.rglob("*")
        if found.is_file() and ".git" not in found.relative_to(harness.workdir).parts
    }


def subcommands(harness: ProviderHarness) -> list[str]:
    return harness.git_subcommands()


# -- the happy path: one commit for everything -------------------------------------------------------


def test_pages_are_written_and_committed_once_with_the_standard_note(tmp_path: Path) -> None:
    harness = build(tmp_path)
    home, setup = create("Home.md", "# Home\n"), create("Setup.md", "# Setup\n")

    result = harness.provider.apply_changes(change_set(home, setup))

    assert result.provider_name == "docs"
    assert [item.status for item in result.results] == [APPLIED, APPLIED]
    assert (harness.workdir / "Home.md").read_text() == "# Home\n"
    assert len(harness.fake.commits) == 1
    assert harness.fake.commits[0].paths == ("Home.md", "Setup.md")
    note = f"committed locally at {short(harness)} in '{harness.workdir}'; not pushed"
    assert all(item.message and item.message.endswith(note) for item in result.results)


def test_results_keep_the_operation_ids_and_the_input_order(tmp_path: Path) -> None:
    harness = build(tmp_path)
    operations = [create("B.md"), create("A.md"), create("C.md")]

    result = harness.provider.apply_changes(change_set(*operations))

    assert [item.operation_id for item in result.results] == [op.operation_id for op in operations]


def test_the_commit_message_uses_the_plugin_id_and_the_number_of_pages(tmp_path: Path) -> None:
    harness = build(tmp_path, settings={"commit": {"message": "wiki({provider_name}): {page_count} via {plugin_id}"}})

    harness.provider.apply_changes(change_set(create("A.md"), create("B.md"), plugin_id="azure-docs"))

    assert harness.fake.commits[0].message == "wiki(docs): 2 via azure-docs"


def test_the_default_message_names_the_plugin(tmp_path: Path) -> None:
    harness = build(tmp_path)

    harness.provider.apply_changes(change_set(create("A.md")))

    assert harness.fake.commits[0].message == f"docs(wiki): update via wikiops plugin {PLUGIN}"


def test_an_asset_uploaded_earlier_rides_the_same_commit_as_the_pages(tmp_path: Path) -> None:
    harness = build(tmp_path)
    stored = harness.provider.put_asset(asset(), b"\x89PNGdata")

    harness.provider.apply_changes(change_set(create("Home.md")))

    assert len(harness.fake.commits) == 1
    assert set(harness.fake.commits[0].paths) == {"Home.md", stored.ref.locator["path"]}
    assert manifest_of(harness) == {}  # committed: nothing stays pending


def test_an_update_of_a_committed_page_commits_the_new_content(tmp_path: Path) -> None:
    harness = build(tmp_path)
    seed_committed(harness, "Home.md", "# old\n")

    result = harness.provider.apply_changes(change_set(update("Home.md", "# new\n")))

    assert result.results[0].status is APPLIED
    assert harness.fake.committed_files["Home.md"] == b"# new\n"
    assert harness.fake.commits[0].paths == ("Home.md",)


def test_a_custom_backend_swaps_in_without_changing_the_git_result(tmp_path: Path) -> None:
    harness = build(tmp_path, settings={"local_backend": {"type": "fake_files"}})

    result = harness.provider.apply_changes(change_set(create("Home.md"), create("Setup.md")))

    assert [item.status for item in result.results] == [APPLIED, APPLIED]
    assert harness.fake.commits[0].paths == ("Home.md", "Setup.md")


# -- push ------------------------------------------------------------------------------------------


def test_with_push_enabled_the_note_says_pushed_and_one_push_ran(tmp_path: Path) -> None:
    harness = build(tmp_path, settings={"allow_auto_push": True})

    result = harness.provider.apply_changes(change_set(create("Home.md")))

    assert result.results[0].message and result.results[0].message.endswith(f"committed {short(harness)}, pushed to master")
    assert len(harness.fake.pushes) == 1
    assert harness.fake.remote["master"] == harness.fake.local


def test_earlier_unpushed_commits_are_pushed_with_the_new_one(tmp_path: Path) -> None:
    harness = build(tmp_path, settings={"allow_auto_push": True}, local=["c1", "c2"], tracking=["c1"])

    harness.provider.apply_changes(change_set(create("Home.md")))

    assert len(harness.fake.pushes) == 1
    assert harness.fake.remote["master"] == ["c1", "c2", "w1"]


def test_push_stays_off_without_the_setting_and_no_push_argv_exists(tmp_path: Path) -> None:
    harness = build(tmp_path)

    harness.provider.apply_changes(change_set(create("Home.md")))

    assert "push" not in subcommands(harness)
    assert harness.fake.pushes == []


@pytest.fixture
def raced(tmp_path: Path) -> tuple[ProviderHarness, ApplyResult, list[Any]]:
    """An apply whose push is rejected because the remote advanced after the sync."""
    harness = build(tmp_path, settings={"allow_auto_push": True})
    harness.provider.put_asset(asset(), b"warm")  # runs the sync now
    harness.fake.remote["master"] = ["c1", "theirs"]  # a web edit lands afterwards
    operations = [create("Home.md"), create("Setup.md")]
    return harness, harness.provider.apply_changes(change_set(*operations)), operations


def test_a_rejected_push_fails_every_written_operation_with_sha_and_workdir(
    raced: tuple[ProviderHarness, ApplyResult, list[Any]],
) -> None:
    harness, result, _ = raced

    assert [item.status for item in result.results] == [FAILED, FAILED]
    for item in result.results:
        assert item.message and item.message.startswith("[github_wiki:push.rejected]")
        assert short(harness) in item.message and str(harness.workdir) in item.message
        assert "Hint:" in item.message


def test_a_rejected_push_keeps_the_local_commit_and_never_retries_or_forces(
    raced: tuple[ProviderHarness, ApplyResult, list[Any]],
) -> None:
    harness, _, _ = raced

    assert len(harness.fake.commits) == 1
    assert len(harness.fake.pushes) == 1  # no retry
    assert not any(flag in harness.fake.pushes[0] for flag in ("--force", "-f", "--force-with-lease"))
    assert not {"rebase", "reset", "pull"} & set(subcommands(harness))
    assert manifest_of(harness) == {}  # committed paths are not pending any more


def test_a_failed_push_of_another_class_carries_its_own_code_and_the_sha(tmp_path: Path) -> None:
    harness = build(tmp_path, settings={"allow_auto_push": True})
    harness.provider.put_asset(asset(), b"warm")
    harness.fake.fail["push"] = (128, "fatal: unable to access 'https://x': Could not resolve host: github.com\n")

    result = harness.provider.apply_changes(change_set(create("Home.md")))

    message = result.results[0].message or ""
    assert result.results[0].status is FAILED
    assert message.startswith("[github_wiki:network.unreachable]")
    assert short(harness) in message and str(harness.workdir) in message
    assert len(harness.fake.commits) == 1


def test_a_push_failure_leaves_skipped_operations_skipped_when_something_was_written(tmp_path: Path) -> None:
    harness = build(tmp_path, settings={"allow_auto_push": True})
    seed_committed(harness, "Same.md", "# same\n")
    harness.provider.put_asset(asset(), b"warm")
    harness.fake.remote["master"] = ["c1", "theirs"]
    same, fresh = create("Same.md", "# same\n"), create("Fresh.md")

    result = harness.provider.apply_changes(change_set(same, fresh))

    assert result_for(result, same).status is SKIPPED
    assert result_for(result, fresh).status is FAILED


# -- never raises: failures before anything is written -------------------------------------------------


def test_a_sync_failure_fails_every_operation_with_the_coded_message_and_writes_nothing(tmp_path: Path) -> None:
    harness = build(tmp_path, local=["c1", "mine"], remote={"master": ["c1", "theirs"]})
    operations = [create("Home.md"), update("Setup.md", "x"), asset("a.png")]

    result = harness.provider.apply_changes(change_set(*operations))

    assert [item.operation_id for item in result.results] == [op.operation_id for op in operations]
    assert all(item.status is FAILED for item in result.results)
    assert all((item.message or "").startswith("[github_wiki:sync.diverged]") for item in result.results)
    assert on_disk(harness) == set()
    assert harness.fake.commits == []


def test_a_lock_held_by_another_run_fails_every_operation_with_workdir_locked(tmp_path: Path) -> None:
    harness = build(tmp_path)
    other = WorkdirLock(lock_path(harness.fake.git_dir), workdir=harness.workdir)

    with other.hold("other-run"):
        result = harness.provider.apply_changes(change_set(create("Home.md"), create("Setup.md")))

    assert all(item.status is FAILED for item in result.results)
    assert all("[github_wiki:workdir.locked]" in (item.message or "") for item in result.results)
    assert on_disk(harness) == set()


def test_an_empty_change_set_returns_no_results_and_runs_nothing_that_writes(tmp_path: Path) -> None:
    harness = build(tmp_path, settings={"allow_auto_push": True}, local=["c1", "c2"], tracking=["c1"])

    result = harness.provider.apply_changes(change_set())

    assert result.results == []
    assert not {"add", "commit", "push"} & set(subcommands(harness))
    assert harness.network_calls() == [] and harness.resolver.created == []  # nothing to do: no sync, no backend


def test_a_backend_that_cannot_be_created_fails_every_operation(tmp_path: Path) -> None:
    harness = build(tmp_path, settings={"local_backend": {"type": "local_files", "bogus_option": 1}})

    result = harness.provider.apply_changes(change_set(create("Home.md")))

    assert result.results[0].status is FAILED
    assert "[github_wiki:config.backend_invalid]" in (result.results[0].message or "")


def test_an_unexpected_backend_exception_becomes_failed_results_and_releases_the_lock(tmp_path: Path) -> None:
    harness, resolver = scripted(tmp_path)
    harness.provider.put_asset(asset(), b"warm")
    backend_of(resolver).apply_error = RuntimeError("backend exploded")

    result = harness.provider.apply_changes(change_set(create("Home.md")))

    assert result.results[0].status is FAILED
    assert "backend exploded" in (result.results[0].message or "")
    assert harness.fake.commits == []
    with WorkdirLock(lock_path(harness.fake.git_dir), workdir=harness.workdir).hold("probe"):
        pass


def test_exception_text_is_redacted_before_it_reaches_a_result(tmp_path: Path) -> None:
    harness, resolver = scripted(tmp_path)
    harness.provider.put_asset(asset(), b"warm")
    backend_of(resolver).apply_error = RuntimeError("cannot reach https://user:hunter2@example.test/wiki.git")

    result = harness.provider.apply_changes(change_set(create("Home.md")))

    assert "hunter2" not in (result.results[0].message or "")


# -- the dirty re-check under the lock ------------------------------------------------------------------


def test_a_foreign_file_fails_the_apply_listing_it_and_nothing_is_written(tmp_path: Path) -> None:
    harness = build(tmp_path)
    (harness.workdir / "Other.md").write_text("not ours")

    result = harness.provider.apply_changes(change_set(create("Home.md")))

    assert result.results[0].status is FAILED
    assert "[github_wiki:workdir.dirty]" in (result.results[0].message or "")
    assert "Other.md" in (result.results[0].message or "")
    assert on_disk(harness) == {"Other.md"}
    assert harness.fake.commits == []


def test_a_foreign_change_after_the_sync_lists_only_the_foreign_path_not_the_pending_one(tmp_path: Path) -> None:
    harness = build(tmp_path, settings={"allow_auto_commit": False})
    harness.provider.apply_changes(change_set(create("Home.md")))  # pending, hash recorded
    (harness.workdir / "Other.md").write_text("foreign")

    result = harness.provider.apply_changes(change_set(update("Home.md", "# again\n")))

    message = result.results[0].message or ""
    assert result.results[0].status is FAILED
    assert "[github_wiki:workdir.dirty]" in message and "paths='Other.md'" in message
    assert "Home.md" not in message
    assert (harness.workdir / "Home.md").read_text() == "# page\n"  # the second write never happened


def test_a_pending_page_the_user_edited_is_listed_and_nothing_is_written(tmp_path: Path) -> None:
    harness = build(tmp_path, settings={"allow_auto_commit": False})
    harness.provider.apply_changes(change_set(create("Home.md")))
    (harness.workdir / "Home.md").write_text("# edited by hand\n")

    result = harness.provider.apply_changes(change_set(update("Home.md", "# again\n")))

    assert result.results[0].status is FAILED
    assert "paths='Home.md'" in (result.results[0].message or "")
    assert (harness.workdir / "Home.md").read_text() == "# edited by hand\n"


def test_a_corrupt_manifest_fails_the_apply_and_writes_nothing(tmp_path: Path) -> None:
    harness = build(tmp_path)
    path = manifest_path(harness.fake.git_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json")

    result = harness.provider.apply_changes(change_set(create("Home.md")))

    assert result.results[0].status is FAILED
    assert "[github_wiki:workdir.manifest_corrupt]" in (result.results[0].message or "")
    assert on_disk(harness) == set()


def test_a_manifest_that_breaks_mid_run_fails_the_operations_without_raising(tmp_path: Path) -> None:
    harness, resolver = scripted(tmp_path)
    harness.provider.put_asset(asset(), b"warm")
    backend_of(resolver).before = lambda: manifest_path(harness.fake.git_dir).write_text("garbage")

    result = harness.provider.apply_changes(change_set(create("Home.md")))

    assert result.results[0].status is FAILED
    assert "workdir.manifest_corrupt" in (result.results[0].message or "")


def test_the_lock_is_held_during_the_backend_write_and_released_after(tmp_path: Path) -> None:
    harness, resolver = scripted(tmp_path)
    harness.provider.put_asset(asset(), b"warm")
    seen: list[dict[str, Any]] = []
    lock_file = harness.fake.git_dir / "wikiops" / "lock"
    backend_of(resolver).before = lambda: seen.append(json.loads(lock_file.read_text()))

    harness.provider.apply_changes(change_set(create("Home.md")))

    assert len(seen) == 1 and seen[0]["purpose"] == "apply"
    assert lock_file.read_text().strip() == ""


# -- per-operation page policy ----------------------------------------------------------------------------


ALIAS_REF = DocumentRef(provider="", kind=RefKind.ALIAS, alias="home")


@pytest.mark.parametrize(
    ("operation", "code"),
    [
        (create("guides/setup.md"), "path.nested_not_supported"),
        (update("guides/setup.md", "x"), "path.nested_not_supported"),
        (create(".git/config"), "path.reserved"),
        (create("notes.txt"), "path.not_markdown"),
        (update("README", "x"), "path.not_markdown"),
        (CreateDocumentOperation(ref=ALIAS_REF, title="T", content="x"), "ref.unsupported_kind"),
        (create("  ", title="T"), "ref.missing_path"),
        (create(None, title="a/b"), "title.invalid"),
        (child("..hidden"), "title.invalid"),
    ],
    ids=["nested", "nested-update", "reserved", "not-markdown", "no-extension", "alias", "blank", "bad-title", "bad-child-title"],
)
def test_an_invalid_operation_fails_with_its_code_and_is_never_delegated(
    tmp_path: Path, operation: Any, code: str
) -> None:
    harness, resolver = scripted(tmp_path)
    flat = create("Flat.md")

    result = harness.provider.apply_changes(change_set(operation, flat))

    assert result_for(result, operation).status is FAILED
    assert (result_for(result, operation).message or "").startswith(f"[github_wiki:{code}]")
    delegated = [op.operation_id for call in backend_of(resolver).applied for op in call.operations]
    assert delegated == [flat.operation_id]
    # the flat sibling is applied and committed alone
    assert result_for(result, flat).status is APPLIED
    assert harness.fake.commits[0].paths == ("Flat.md",)


def test_the_nested_hint_names_the_flat_page(tmp_path: Path) -> None:
    harness = build(tmp_path)
    operation = create("guides/setup.md")

    result = harness.provider.apply_changes(change_set(operation))

    assert "guides-setup.md" in (result.results[0].message or "")
    assert on_disk(harness) == set()
    assert not {"add", "commit"} & set(subcommands(harness))


def test_an_invalid_operation_keeps_its_ref_in_the_failed_result(tmp_path: Path) -> None:
    harness = build(tmp_path)
    operation = create("notes.txt")

    result = harness.provider.apply_changes(change_set(operation))

    assert result.results[0].resolved_ref == operation.ref


def test_all_valid_operations_reach_the_backend_in_one_call_in_order(tmp_path: Path) -> None:
    harness, resolver = scripted(tmp_path)
    harness.provider.put_asset(asset(), b"warm")
    first, bad, second = create("A.md"), create("x/y.md"), update("A.md", "# changed\n")

    harness.provider.apply_changes(change_set(first, bad, second, plugin_id="p1"))

    calls = backend_of(resolver).applied
    assert len(calls) == 1
    assert [op.operation_id for op in calls[0].operations] == [first.operation_id, second.operation_id]
    assert calls[0].plugin_id == "p1"


def test_a_ref_less_create_gets_a_derived_root_ref(tmp_path: Path) -> None:
    harness = build(tmp_path)
    operation = create(None, "# notes\n", title="Release  Notes 2.0")

    result = harness.provider.apply_changes(change_set(operation))

    assert result.results[0].status is APPLIED
    assert result.results[0].resolved_ref is not None
    assert result.results[0].resolved_ref.locator["path"] == "Release-Notes-2.0.md"
    assert (harness.workdir / "Release-Notes-2.0.md").read_text() == "# notes\n"
    assert harness.fake.commits[0].paths == ("Release-Notes-2.0.md",)


def test_a_ref_less_child_create_is_a_root_page_never_nested_under_the_parent(tmp_path: Path) -> None:
    harness = build(tmp_path)
    seed_committed(harness, "Home.md", "# home\n")
    operation = child("Getting Started")

    result = harness.provider.apply_changes(change_set(operation))

    assert result.results[0].status is APPLIED
    assert on_disk(harness) == {"Home.md", "Getting-Started.md"}
    assert harness.fake.commits[0].paths == ("Getting-Started.md",)


def test_a_child_create_with_an_explicit_flat_ref_keeps_it(tmp_path: Path) -> None:
    harness = build(tmp_path)

    harness.provider.apply_changes(change_set(child("Whatever", path="Chosen.md")))

    assert on_disk(harness) == {"Chosen.md"}


# -- backend semantics pass through ---------------------------------------------------------------------------


def test_a_backend_error_text_passes_through_unchanged_and_nothing_is_committed(tmp_path: Path) -> None:
    harness, resolver = scripted(tmp_path)
    seed_committed(harness, "Home.md", "# existing\n")
    operation = create("Home.md", "# different\n")

    result = harness.provider.apply_changes(change_set(operation))

    message = result.results[0].message or ""
    assert result.results[0].status is FAILED
    assert message.startswith("[local_files:conflict.exists]")
    assert "[github_wiki:" not in message
    assert harness.fake.commits == []
    assert (harness.workdir / "Home.md").read_text() == "# existing\n"


def test_an_identical_reapply_is_skipped_and_makes_no_commit(tmp_path: Path) -> None:
    harness = build(tmp_path)
    harness.provider.apply_changes(change_set(create("Home.md", "# home\n")))

    again = create("Home.md", "# home\n")
    result = harness.provider.apply_changes(change_set(again))

    assert result.results[0].status is SKIPPED
    assert len(harness.fake.commits) == 1
    assert subcommands(harness).count("commit") == 1


def test_an_identical_reapply_pushes_earlier_unpushed_commits_when_push_is_on(tmp_path: Path) -> None:
    harness = build(tmp_path, settings={"allow_auto_push": True}, local=["c1", "c2"], tracking=["c1"])
    seed_committed(harness, "Home.md", "# home\n")

    result = harness.provider.apply_changes(change_set(create("Home.md", "# home\n")))

    assert result.results[0].status is SKIPPED
    assert harness.fake.commits == []
    assert len(harness.fake.pushes) == 1


def test_a_push_failure_with_nothing_written_is_reported_on_the_skipped_operations(tmp_path: Path) -> None:
    harness = build(tmp_path, settings={"allow_auto_push": True}, local=["c1", "c2"], tracking=["c1"])
    seed_committed(harness, "Home.md", "# home\n")
    harness.fake.fail["push"] = (128, "fatal: unable to access 'x': Could not resolve host: github.com\n")

    result = harness.provider.apply_changes(change_set(create("Home.md", "# home\n")))

    assert result.results[0].status is FAILED
    assert "[github_wiki:network.unreachable]" in (result.results[0].message or "")


# -- a write that git ignores is never silent --------------------------------------------------------------------


def test_a_page_hidden_by_gitignore_fails_its_operation_and_names_the_path(tmp_path: Path) -> None:
    harness = build(tmp_path)
    harness.fake.ignored.add("Secret.md")

    result = harness.provider.apply_changes(change_set(create("Home.md"), create("Secret.md")))

    home, secret = result.results
    assert home.status is APPLIED and secret.status is FAILED
    message = secret.message or ""
    assert "[github_wiki:commit.failed]" in message
    assert "'Secret.md'" in message
    assert "ignored by git" in message and ".gitignore" in message and "info/exclude" in message
    assert "Hint: remove the ignore rule or rename the page." in message
    assert harness.fake.commits[0].paths == ("Home.md",)  # the visible page is still committed
    assert "committed" in (home.message or "")
    assert "committed locally" not in message  # the commit note belongs to the page that went in


def test_an_apply_where_every_page_is_ignored_commits_nothing_and_fails_each_one(tmp_path: Path) -> None:
    harness = build(tmp_path)
    harness.fake.ignored.update({"A.md", "B.md"})

    result = harness.provider.apply_changes(change_set(create("A.md"), create("B.md")))

    assert [item.status for item in result.results] == [FAILED, FAILED]
    assert "'A.md'" in (result.results[0].message or "")
    assert "'B.md'" in (result.results[1].message or "")
    assert harness.fake.commits == []
    assert "commit" not in subcommands(harness)


def test_an_asset_hidden_by_gitignore_is_refused_naming_its_path(tmp_path: Path) -> None:
    harness = build(tmp_path)
    path = harness.provider.put_asset(asset(), b"\x89PNGdata").ref.locator["path"]
    harness.fake.ignored.add(path)

    with pytest.raises(GithubWikiError) as raised:
        harness.provider.put_asset(asset(), b"\x89PNGdata")

    assert raised.value.code == "commit.failed"
    assert f"'{path}'" in str(raised.value) and "ignored by git" in str(raised.value)
    assert "remove the ignore rule or rename the page" in str(raised.value)
    assert manifest_of(harness) == {}


# -- what counts as written: only the SDK result fields -----------------------------------------------------------


def test_a_file_the_backend_wrote_but_did_not_report_is_never_staged(tmp_path: Path) -> None:
    harness, resolver = scripted(tmp_path)
    stored = harness.provider.put_asset(asset(), b"warm")

    def write_stray(changeset: Any, real: ApplyResult) -> ApplyResult:
        (harness.workdir / "stray.md").write_text("unreported")
        return real

    backend_of(resolver).on_apply = write_stray

    result = harness.provider.apply_changes(change_set(create("A.md")))

    assert result.results[0].status is APPLIED
    assert harness.fake.commits[0].paths == ("A.md", stored.ref.locator["path"])
    assert "stray.md" not in manifest_of(harness)
    assert "stray.md" not in harness.fake.committed_files


def _retarget(real: ApplyResult, **update_fields: Any) -> ApplyResult:
    results = [item.model_copy(update=update_fields) for item in real.results]
    return ApplyResult(provider_name=real.provider_name, results=results)


@pytest.mark.parametrize(
    ("reported", "code"),
    [
        (None, "ref.missing_path"),
        (ref("../evil.md"), "path.nested_not_supported"),
        (ref(".git/hooks.md"), "path.reserved"),
        (ref("notes.txt"), "path.not_markdown"),
    ],
    ids=["no-ref", "traversal", "dot-git", "not-markdown"],
)
def test_an_applied_result_without_a_trackable_path_fails_and_is_not_committed(
    tmp_path: Path, reported: DocumentRef | None, code: str
) -> None:
    harness, resolver = scripted(tmp_path)
    harness.provider.put_asset(asset(), b"warm")
    backend_of(resolver).on_apply = lambda cs, real: _retarget(real, resolved_ref=reported)
    operation = create("A.md")

    result = harness.provider.apply_changes(change_set(operation))

    assert result.results[0].status is FAILED
    assert (result.results[0].message or "").startswith(f"[github_wiki:{code}]")
    assert harness.fake.commits == []  # nothing succeeded: the pending warm asset is not committed either
    assert "A.md" not in manifest_of(harness)


def test_the_page_path_comes_from_the_reported_ref_not_from_the_planned_one(tmp_path: Path) -> None:
    harness, resolver = scripted(tmp_path)
    harness.provider.put_asset(asset(), b"warm")

    def report_elsewhere(changeset: Any, real: ApplyResult) -> ApplyResult:
        (harness.workdir / "Reported.md").write_text("# reported\n")
        return _retarget(real, resolved_ref=ref("Reported.md"))

    backend_of(resolver).on_apply = report_elsewhere

    harness.provider.apply_changes(change_set(create("Planned.md")))

    assert "Reported.md" in harness.fake.commits[0].paths
    assert "Planned.md" not in harness.fake.commits[0].paths


def test_a_backend_that_omits_a_result_fails_that_operation_only(tmp_path: Path) -> None:
    harness, resolver = scripted(tmp_path)
    harness.provider.put_asset(asset(), b"warm")
    first, second = create("A.md"), create("B.md")

    def drop_second(changeset: Any, real: ApplyResult) -> ApplyResult:
        return ApplyResult(provider_name=real.provider_name, results=real.results[:1])

    backend_of(resolver).on_apply = drop_second

    result = harness.provider.apply_changes(change_set(first, second))

    assert result_for(result, first).status is APPLIED
    assert result_for(result, second).status is FAILED
    assert "returned no result" in (result_for(result, second).message or "")


# -- the link guard on what the backend reports -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reference", "code"),
    [
        ("/assets/x.png", "link.root_anchored"),
        ("  /assets/x.png", "link.root_anchored"),
        ("https://raw.githubusercontent.com/wiki/acme/p/assets/x.png", "link.raw_url"),
        ("http://raw.githubusercontent.com/wiki/acme/p/x.png", "link.raw_url"),
    ],
)
def test_a_backend_reference_in_a_forbidden_form_fails_the_operation(
    tmp_path: Path, reference: str, code: str
) -> None:
    harness, resolver = scripted(tmp_path)
    harness.provider.put_asset(asset(), b"warm")
    backend_of(resolver).on_apply = lambda cs, real: _retarget(real, resolved_asset_reference=reference)

    result = harness.provider.apply_changes(change_set(create("A.md")))

    assert result.results[0].status is FAILED
    assert (result.results[0].message or "").startswith(f"[github_wiki:{code}]")


def test_a_document_relative_backend_reference_is_left_alone(tmp_path: Path) -> None:
    harness, resolver = scripted(tmp_path)
    harness.provider.put_asset(asset(), b"warm")
    backend_of(resolver).on_apply = lambda cs, real: _retarget(
        real, resolved_asset_reference="assets/x.png"
    )

    result = harness.provider.apply_changes(change_set(create("A.md")))

    assert result.results[0].status is APPLIED
    assert result.results[0].resolved_asset_reference == "assets/x.png"


def test_page_content_with_a_root_anchored_link_is_written_byte_identical(tmp_path: Path) -> None:
    harness = build(tmp_path)
    content = "[repo](/owner/repo) ![](/assets/x.png) https://raw.githubusercontent.com/a/b\n"

    result = harness.provider.apply_changes(change_set(create("Links.md", content)))

    assert result.results[0].status is APPLIED
    assert (harness.workdir / "Links.md").read_text() == content


def test_an_asset_reported_by_the_backend_is_tracked_as_written(tmp_path: Path) -> None:
    harness, resolver = scripted(tmp_path)
    harness.provider.put_asset(asset(), b"warm")

    def report_asset(changeset: Any, real: ApplyResult) -> ApplyResult:
        (harness.workdir / "assets").mkdir(exist_ok=True)
        (harness.workdir / "assets" / "z.png").write_bytes(b"z")
        return _retarget(real, resolved_asset_ref=asset_ref("assets/z.png"))

    backend_of(resolver).on_apply = report_asset

    harness.provider.apply_changes(change_set(create("A.md")))

    assert "assets/z.png" in harness.fake.commits[0].paths


# -- commit modes and the manifest -----------------------------------------------------------------------------------------


def test_with_auto_commit_off_pages_are_written_recorded_and_not_committed(tmp_path: Path) -> None:
    harness = build(tmp_path, settings={"allow_auto_commit": False})
    home, setup = create("Home.md", "# Home\n"), create("Setup.md", "# Setup\n")

    result = harness.provider.apply_changes(change_set(home, setup))

    note = f"written to '{harness.workdir}', not committed (allow_auto_commit=false)"
    assert [item.status for item in result.results] == [APPLIED, APPLIED]
    assert all(item.message and item.message.endswith(note) for item in result.results)
    assert manifest_of(harness) == {
        "Home.md": content_version(b"# Home\n"),
        "Setup.md": content_version(b"# Setup\n"),
    }
    assert not {"add", "commit", "push"} & set(subcommands(harness))
    assert harness.fake.commits == []


def test_with_auto_commit_off_a_pending_page_may_be_updated_again(tmp_path: Path) -> None:
    harness = build(tmp_path, settings={"allow_auto_commit": False})
    harness.provider.apply_changes(change_set(create("Home.md", "# v1\n")))

    result = harness.provider.apply_changes(change_set(update("Home.md", "# v2\n")))

    assert result.results[0].status is APPLIED
    assert manifest_of(harness) == {"Home.md": content_version(b"# v2\n")}


def test_switching_auto_commit_on_includes_the_earlier_pending_pages(tmp_path: Path) -> None:
    harness = build(tmp_path, settings={"allow_auto_commit": False})
    harness.provider.apply_changes(change_set(create("Home.md")))
    assert harness.fake.commits == []

    switched = harness.rebuild(allow_auto_commit=True)
    result = switched.apply_changes(change_set(create("Other.md")))

    assert result.results[0].status is APPLIED
    assert len(harness.fake.commits) == 1
    assert harness.fake.commits[0].paths == ("Home.md", "Other.md")
    assert manifest_of(harness) == {}


def test_auto_commit_off_asset_then_page_keeps_both_pending(tmp_path: Path) -> None:
    harness = build(tmp_path, settings={"allow_auto_commit": False})
    stored = harness.provider.put_asset(asset(), b"img")

    harness.provider.apply_changes(change_set(create("Home.md")))

    assert set(manifest_of(harness)) == {"Home.md", stored.ref.locator["path"]}


def test_a_failed_commit_fails_written_operations_and_keeps_their_paths_pending(tmp_path: Path) -> None:
    harness = build(tmp_path)
    harness.fake.fail["commit"] = (1, "pre-commit hook failed\n")
    home, setup = create("Home.md"), create("Setup.md")

    result = harness.provider.apply_changes(change_set(home, setup))

    assert [item.status for item in result.results] == [FAILED, FAILED]
    for item in result.results:
        assert (item.message or "").startswith("[github_wiki:commit.failed]")
        assert "written but not committed" in (item.message or "")
        assert str(harness.workdir) in (item.message or "")
    assert set(manifest_of(harness)) == {"Home.md", "Setup.md"}
    assert set(harness.fake.staged) == {"Home.md", "Setup.md"}  # left for inspection
    assert not {"push"} & set(subcommands(harness))


def test_a_later_apply_after_a_failed_commit_commits_both_the_old_and_the_new_page(tmp_path: Path) -> None:
    harness = build(tmp_path)
    harness.fake.fail["commit"] = (1, "pre-commit hook failed\n")
    harness.provider.apply_changes(change_set(create("Home.md")))
    del harness.fake.fail["commit"]  # the hook problem is fixed

    result = harness.provider.apply_changes(change_set(create("Other.md")))

    assert result.results[0].status is APPLIED
    assert len(harness.fake.commits) == 1
    assert harness.fake.commits[0].paths == ("Home.md", "Other.md")
    assert manifest_of(harness) == {}


def test_a_missing_git_identity_fails_the_written_operations_with_its_own_code(tmp_path: Path) -> None:
    harness = build(tmp_path, git_identity=None)

    result = harness.provider.apply_changes(change_set(create("Home.md")))

    assert result.results[0].status is FAILED
    assert (result.results[0].message or "").startswith("[github_wiki:commit.identity_missing]")
    assert "Home.md" in manifest_of(harness)


def test_the_bot_identity_is_used_without_touching_the_clone_config(tmp_path: Path) -> None:
    harness = build(tmp_path, git_identity=None, settings={"commit": {"identity": {"mode": "bot"}}})

    result = harness.provider.apply_changes(change_set(create("Home.md")))

    assert result.results[0].status is APPLIED
    assert harness.fake.commits[0].author == ("wikiops", "wikiops@users.noreply.github.com")
    config_calls = [call.argv for call in harness.runner.calls if call.argv[0] == "git" and "config" in call.argv]
    assert not any({"--add", "--unset", "--replace-all", "--global", "--local"} & set(argv) for argv in config_calls)
    assert not any(argv[-1] in {"wikiops", "wikiops@users.noreply.github.com"} for argv in config_calls)


def test_partial_batches_commit_the_successful_operations(tmp_path: Path) -> None:
    harness = build(tmp_path)
    seed_committed(harness, "Exists.md", "# existing\n")
    good, conflict, nested = create("Good.md"), create("Exists.md", "# changed\n"), create("a/b.md")

    result = harness.provider.apply_changes(change_set(good, conflict, nested))

    assert result_for(result, good).status is APPLIED
    assert result_for(result, conflict).status is FAILED
    assert result_for(result, nested).status is FAILED
    assert "committed locally at" in (result_for(result, good).message or "")
    assert harness.fake.commits[0].paths == ("Good.md",)
    assert "committed" not in (result_for(result, conflict).message or "")


def test_local_commands_carry_no_credentials(tmp_path: Path) -> None:
    harness = build(
        tmp_path,
        settings={"auth": {"mode": "env", "variable": "WIKI_T"}, "allow_auto_push": True},
        environ={"WIKI_T": "ghp_apply_SECRET_3"},
    )

    harness.provider.apply_changes(change_set(create("Home.md")))

    local = [c for c in harness.runner.calls if c.argv[0] == "git" and not any(p.startswith("core.hooksPath=") for p in c.argv)]
    assert any("commit" in call.argv for call in local)
    for call in local:
        assert not any(key.startswith("GIT_CONFIG") for key in call.env_overrides)
        assert "ghp_apply_SECRET_3" not in " ".join(call.argv)


# -- a fully failed apply, recovery by re-running, and what stays accurate between applies ------------------


def test_a_fully_failed_apply_leaves_the_pending_leftovers_pending(tmp_path: Path) -> None:
    harness = build(tmp_path)
    stored = harness.provider.put_asset(asset(), b"img")

    result = harness.provider.apply_changes(change_set(create("a/b.md")))

    assert result.results[0].status is FAILED
    assert harness.fake.commits == []
    assert set(manifest_of(harness)) == {stored.ref.locator["path"]}


def test_rerunning_the_same_plan_after_a_failed_commit_commits_the_leftover_page(tmp_path: Path) -> None:
    harness = build(tmp_path)
    harness.fake.fail["commit"] = (1, "pre-commit hook failed\n")
    harness.provider.apply_changes(change_set(create("Home.md", "# home\n")))
    del harness.fake.fail["commit"]  # the hook problem is fixed

    result = harness.provider.apply_changes(change_set(create("Home.md", "# home\n")))

    assert result.results[0].status is SKIPPED  # the page is already on disk, byte-identical
    assert harness.fake.commits[0].paths == ("Home.md",)
    assert manifest_of(harness) == {}


def test_a_second_apply_after_a_successful_push_does_not_push_again(tmp_path: Path) -> None:
    harness = build(tmp_path, settings={"allow_auto_push": True}, local=["c1", "c2"], tracking=["c1"])
    harness.provider.apply_changes(change_set(create("Home.md", "# home\n")))
    assert len(harness.fake.pushes) == 1

    harness.provider.apply_changes(change_set(create("Home.md", "# home\n")))  # identical

    assert len(harness.fake.pushes) == 1  # the live count is zero: the sync snapshot is not reused


def test_a_second_apply_after_a_rejected_push_tries_again_with_the_retained_commit(tmp_path: Path) -> None:
    harness = build(tmp_path, settings={"allow_auto_push": True})
    harness.provider.put_asset(asset(), b"warm")
    harness.fake.remote["master"] = ["c1", "theirs"]
    harness.provider.apply_changes(change_set(create("Home.md")))
    assert len(harness.fake.pushes) == 1

    again = harness.provider.apply_changes(change_set(create("Home.md")))

    assert len(harness.fake.pushes) == 2  # the earlier commit is still unpushed
    assert again.results[0].status is FAILED
    assert "[github_wiki:push.rejected]" in (again.results[0].message or "")
    assert len(harness.fake.commits) == 1  # and nothing new was committed


def test_the_page_count_of_the_message_counts_pages_not_assets(tmp_path: Path) -> None:
    harness, resolver = scripted(tmp_path, settings={"commit": {"message": "{page_count} page(s)"}})
    harness.provider.put_asset(asset(), b"warm")

    def report_asset(changeset: Any, real: ApplyResult) -> ApplyResult:
        (harness.workdir / "assets" / "z.png").write_bytes(b"z")
        return _retarget(real, resolved_asset_ref=asset_ref("assets/z.png"))

    backend_of(resolver).on_apply = report_asset

    harness.provider.apply_changes(change_set(create("A.md"), create("B.md")))

    assert harness.fake.commits[0].message == "2 page(s)"


def test_the_note_follows_a_backend_message_that_already_ends_in_a_sentence(tmp_path: Path) -> None:
    harness = build(tmp_path, settings={"local_backend": {"type": "local_files", "overwrite_existing": True}})
    seed_committed(harness, "Home.md", "# old\n")

    result = harness.provider.apply_changes(change_set(create("Home.md", "# new\n")))

    message = result.results[0].message or ""
    assert message.startswith("[local_files:applied.overwritten]")
    note = f"committed locally at {short(harness)} in '{harness.workdir}'; not pushed"
    assert message.endswith(f"root='{harness.workdir}'. {note}")  # the backend sentence is left whole


def test_an_operation_the_provider_does_not_know_reaches_the_backend_and_keeps_its_answer(tmp_path: Path) -> None:
    harness = build(tmp_path)
    stored = asset("x.png", "x")
    page = create("Home.md")

    result = harness.provider.apply_changes(change_set(stored, page))

    assert result_for(result, stored).status is SKIPPED
    assert "[local_files:op.unsupported]" in (result_for(result, stored).message or "")
    assert result_for(result, page).status is APPLIED


def test_a_failure_before_the_backend_keeps_the_planned_page_refs_on_the_results(tmp_path: Path) -> None:
    harness = build(tmp_path, local=["c1", "mine"], remote={"master": ["c1", "theirs"]})
    operation = update("Home.md", "x")

    result = harness.provider.apply_changes(change_set(operation))

    assert result.results[0].resolved_ref == operation.ref


def test_what_the_backend_reported_stays_pending_when_publishing_blows_up_unexpectedly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wikiops.providers.github_wiki import publisher

    harness = build(tmp_path)
    harness.provider.put_asset(asset(), b"warm")

    def explode(self: Any, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("publisher exploded")

    monkeypatch.setattr(publisher.Publisher, "publish", explode)

    result = harness.provider.apply_changes(change_set(create("Home.md")))

    assert result.results[0].status is FAILED
    assert "publisher exploded" in (result.results[0].message or "")
    assert "Home.md" in manifest_of(harness)  # not a foreign file at the next run
    monkeypatch.undo()
    retry = harness.provider.apply_changes(change_set(create("Home.md")))
    assert retry.results[0].status is SKIPPED
    assert "Home.md" in harness.fake.commits[0].paths


def test_an_interrupt_is_not_swallowed_and_releases_the_lock(tmp_path: Path) -> None:
    harness, resolver = scripted(tmp_path)
    harness.provider.put_asset(asset(), b"warm")
    backend_of(resolver).apply_error = KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        harness.provider.apply_changes(change_set(create("Home.md")))

    with WorkdirLock(lock_path(harness.fake.git_dir), workdir=harness.workdir).hold("probe"):
        pass


def test_only_the_reported_pages_are_written_to_the_workdir(tmp_path: Path) -> None:
    harness = build(tmp_path)

    harness.provider.apply_changes(change_set(create("Home.md")))

    assert on_disk(harness) == {"Home.md"}
