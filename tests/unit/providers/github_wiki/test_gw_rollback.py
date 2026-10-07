"""A write the backend completed but the provider then rejects is rolled back (S12.F1).

When the backend already wrote a page or an asset and the provider refuses what it
reported (a ref the page policy rejects, a forbidden link, an asset reference a wiki cannot
link, a manifest that cannot record it), the file would stay in the clone and the NEXT run
would be blocked by our own orphan as ``workdir.dirty``. The provider restores a tracked file
from HEAD and removes a file that this operation created, under the same lock.

The rollback is confined (S13): it only touches a path that appeared since the status snapshot
taken right before delegating AND is a target of the rejected operation (its page reference, or
the content-hashed name of its asset). The advisory lock cannot stop the user from creating a
file meanwhile, so any other new path is left alone and reported. A backend that raises after
writing is rolled back the same way, and what could not be undone is part of the failure text.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from wikiops_sdk.domain import ApplyResult, Asset, AssetRef, AssetRefKind, OperationStatus

from tests.support.provider_harness import ProviderHarness, build_provider
from tests.support.write_ops import (
    ScriptedBackend,
    ScriptedResolver,
    asset,
    change_set,
    create,
    ref,
    result_for,
    update,
)
from wikiops.providers.github_wiki import rollback
from wikiops.providers.github_wiki.errors import GithubWikiError
from wikiops.providers.github_wiki.manifest import PendingManifest
from wikiops.providers.github_wiki.workdir import manifest_path

APPLIED, FAILED = OperationStatus.APPLIED, OperationStatus.FAILED
PNG = b"\x89PNG\r\n\x1a\nfake image bytes"


def scripted(tmp_path: Path, **options: Any) -> tuple[ProviderHarness, ScriptedBackend]:
    resolver = ScriptedResolver()
    harness = build_provider(tmp_path, cloned=True, track_files=True, backends=resolver, **options)
    harness.provider.put_asset(asset("warm.png", "warm"), PNG)  # creates the backend; stays pending
    assert resolver.backend is not None
    return harness, resolver.backend


def manifest_of(harness: ProviderHarness) -> dict[str, str]:
    return PendingManifest(manifest_path(harness.fake.git_dir), workdir=harness.workdir).entries()


def retarget(real: ApplyResult, **fields: Any) -> ApplyResult:
    return ApplyResult(
        provider_name=real.provider_name,
        results=[item.model_copy(update=fields) for item in real.results],
    )


def reject_ref(backend: ScriptedBackend) -> None:
    """The backend writes the page, then reports a path the page policy refuses."""
    backend.on_apply = lambda cs, real: retarget(real, resolved_ref=ref("../evil.md"))


def seed_committed(harness: ProviderHarness, path: str, content: str) -> None:
    (harness.workdir / path).write_text(content)
    harness.fake.committed_files[path] = content.encode()


def assert_next_apply_is_not_blocked(harness: ProviderHarness, backend: ScriptedBackend) -> None:
    backend.on_apply = None
    follow_up = create("Next.md", "# next\n")
    result = harness.provider.apply_changes(change_set(follow_up))
    assert result_for(result, follow_up).status is APPLIED, result_for(result, follow_up).message


# -- apply_changes: a rejected reported ref -----------------------------------------------------------


def test_a_page_written_under_a_rejected_ref_is_removed_and_the_next_apply_is_not_blocked(
    tmp_path: Path,
) -> None:
    harness, backend = scripted(tmp_path)
    reject_ref(backend)
    operation = create("A.md", "# a\n")

    result = harness.provider.apply_changes(change_set(operation))

    assert result_for(result, operation).status is FAILED
    assert (result_for(result, operation).message or "").startswith("[github_wiki:path.nested_not_supported]")
    assert not (harness.workdir / "A.md").exists()
    assert_next_apply_is_not_blocked(harness, backend)


def test_a_tracked_page_updated_under_a_rejected_ref_is_restored_from_head(tmp_path: Path) -> None:
    harness, backend = scripted(tmp_path)
    seed_committed(harness, "A.md", "# committed\n")
    reject_ref(backend)
    operation = update("A.md", "# changed\n")

    result = harness.provider.apply_changes(change_set(operation))

    assert result_for(result, operation).status is FAILED
    assert (harness.workdir / "A.md").read_text() == "# committed\n"
    assert ("--literal-pathspecs", "checkout", "HEAD", "--", "A.md") in [
        call.argv[1:6] for call in harness.runner.calls if "checkout" in call.argv
    ]
    assert_next_apply_is_not_blocked(harness, backend)


def test_a_forbidden_asset_link_rolls_the_page_back_too(tmp_path: Path) -> None:
    harness, backend = scripted(tmp_path)
    backend.on_apply = lambda cs, real: retarget(real, resolved_asset_reference="/assets/x.png")

    result = harness.provider.apply_changes(change_set(create("A.md")))

    assert result.results[0].status is FAILED
    assert not (harness.workdir / "A.md").exists()
    assert_next_apply_is_not_blocked(harness, backend)


def test_only_the_rejected_operation_is_rolled_back_the_accepted_one_is_committed(tmp_path: Path) -> None:
    harness, backend = scripted(tmp_path)
    good, bad = create("Good.md", "# good\n"), create("Bad.md", "# bad\n")

    def reject_second(changeset: Any, real: ApplyResult) -> ApplyResult:
        results = list(real.results)
        results[1] = results[1].model_copy(update={"resolved_ref": ref("../evil.md")})
        return ApplyResult(provider_name=real.provider_name, results=results)

    backend.on_apply = reject_second

    result = harness.provider.apply_changes(change_set(good, bad))

    assert result_for(result, good).status is APPLIED
    assert result_for(result, bad).status is FAILED
    assert (harness.workdir / "Good.md").exists()
    assert not (harness.workdir / "Bad.md").exists()
    assert "Good.md" in harness.fake.commits[0].paths
    assert "Bad.md" not in harness.fake.commits[0].paths
    assert_next_apply_is_not_blocked(harness, backend)


def test_a_file_that_was_already_pending_is_never_touched_by_the_rollback(tmp_path: Path) -> None:
    harness, backend = scripted(tmp_path)
    pending = dict(manifest_of(harness))
    assert pending  # the warm-up asset is pending
    reject_ref(backend)

    harness.provider.apply_changes(change_set(create("A.md")))

    assert manifest_of(harness) == pending
    for path in pending:
        assert (harness.workdir / path).read_bytes() == PNG


def test_an_unreported_stray_file_is_left_alone_and_named_not_deleted(tmp_path: Path) -> None:
    harness, backend = scripted(tmp_path)

    def stray_and_reject(changeset: Any, real: ApplyResult) -> ApplyResult:
        (harness.workdir / "stray.md").write_text("unreported")
        return retarget(real, resolved_ref=ref("../evil.md"))

    backend.on_apply = stray_and_reject
    operation = create("A.md")

    result = harness.provider.apply_changes(change_set(operation))

    message = result_for(result, operation).message or ""
    assert not (harness.workdir / "A.md").exists()
    assert (harness.workdir / "stray.md").read_text() == "unreported"
    assert "'stray.md'" in message
    assert "not part of" in message
    blocked = harness.provider.apply_changes(change_set(create("B.md")))
    assert "[github_wiki:workdir.dirty]" in (blocked.results[0].message or "")


def test_a_successful_apply_runs_no_rollback_command(tmp_path: Path) -> None:
    harness, backend = scripted(tmp_path)

    harness.provider.apply_changes(change_set(create("A.md")))

    assert not [call for call in harness.runner.calls if "checkout" in call.argv]
    assert (harness.workdir / "A.md").exists()


def test_a_page_the_manifest_cannot_record_is_rolled_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, backend = scripted(tmp_path)

    def broken(self: PendingManifest, paths: Any) -> None:
        raise GithubWikiError("workdir.unusable", "manifest write failed")

    monkeypatch.setattr(PendingManifest, "record", broken)

    result = harness.provider.apply_changes(change_set(create("A.md")))

    assert result.results[0].status is FAILED
    assert (result.results[0].message or "").startswith("[github_wiki:workdir.unusable]")
    assert not (harness.workdir / "A.md").exists()
    monkeypatch.undo()
    assert_next_apply_is_not_blocked(harness, backend)


# -- what could not be undone is said, never hidden ---------------------------------------------------------------------------------


def test_a_rollback_that_cannot_restore_a_file_tells_the_user_which_one(tmp_path: Path) -> None:
    harness, backend = scripted(tmp_path)
    seed_committed(harness, "A.md", "# committed\n")
    reject_ref(backend)
    harness.fake.fail["checkout"] = (1, "error: unable to write\n")
    operation = update("A.md", "# changed\n")

    result = harness.provider.apply_changes(change_set(operation))

    message = result_for(result, operation).message or ""
    assert message.startswith("[github_wiki:path.nested_not_supported]")
    assert "'A.md'" in message
    assert "restore or delete" in message


def test_an_unreadable_status_after_a_rejection_is_reported_not_swallowed(tmp_path: Path) -> None:
    harness, backend = scripted(tmp_path)

    def reject_then_break_status(changeset: Any, real: ApplyResult) -> ApplyResult:
        harness.fake.fail["status"] = (128, "fatal: index file corrupt\n")
        return retarget(real, resolved_ref=ref("../evil.md"))

    backend.on_apply = reject_then_break_status

    result = harness.provider.apply_changes(change_set(create("A.md")))

    assert result.results[0].status is FAILED
    assert "could not be read" in (result.results[0].message or "")


# -- confinement: only what this operation wrote is touched --------------------------------------------------


def test_a_user_file_created_during_a_rejected_write_survives_and_is_reported(tmp_path: Path) -> None:
    harness, backend = scripted(tmp_path)

    def user_file_then_reject(changeset: Any, real: ApplyResult) -> ApplyResult:
        (harness.workdir / "mine.md").write_text("the user's own file")
        return retarget(real, resolved_ref=ref("../evil.md"))

    backend.on_apply = user_file_then_reject
    operation = create("A.md")

    result = harness.provider.apply_changes(change_set(operation))

    message = result_for(result, operation).message or ""
    assert not (harness.workdir / "A.md").exists()
    assert (harness.workdir / "mine.md").read_text() == "the user's own file"
    assert "'mine.md'" in message
    assert "left untouched" in message


def test_a_modified_tracked_file_the_operation_did_not_target_is_not_restored(tmp_path: Path) -> None:
    harness, backend = scripted(tmp_path)
    seed_committed(harness, "Other.md", "# committed\n")

    def user_edit_then_reject(changeset: Any, real: ApplyResult) -> ApplyResult:
        (harness.workdir / "Other.md").write_text("# the user's edit\n")
        return retarget(real, resolved_ref=ref("../evil.md"))

    backend.on_apply = user_edit_then_reject

    harness.provider.apply_changes(change_set(create("A.md")))

    assert (harness.workdir / "Other.md").read_text() == "# the user's edit\n"
    assert not [call for call in harness.runner.calls if "checkout" in call.argv]


def test_a_second_accepted_page_is_neither_rolled_back_nor_reported_as_foreign(tmp_path: Path) -> None:
    harness, backend = scripted(tmp_path)
    good, bad = create("Good.md", "# good\n"), create("Bad.md", "# bad\n")

    def reject_second(changeset: Any, real: ApplyResult) -> ApplyResult:
        results = list(real.results)
        results[1] = results[1].model_copy(update={"resolved_ref": ref("../evil.md")})
        return ApplyResult(provider_name=real.provider_name, results=results)

    backend.on_apply = reject_second

    result = harness.provider.apply_changes(change_set(good, bad))

    assert "Good.md" not in (result_for(result, bad).message or "")
    assert "left untouched" not in (result_for(result, bad).message or "")


# -- a backend that raises after writing is rolled back the same way ---------------------------------------


def raise_after_writing(harness: ProviderHarness, backend: ScriptedBackend, *, concurrent: str | None = None) -> None:
    def explode(changeset: Any, real: ApplyResult) -> ApplyResult:
        if concurrent:
            (harness.workdir / concurrent).write_text("the user's own file")
        raise RuntimeError("disk exploded")

    backend.on_apply = explode


def test_a_backend_that_raises_after_writing_is_rolled_back_and_the_next_apply_is_not_blocked(
    tmp_path: Path,
) -> None:
    harness, backend = scripted(tmp_path)
    raise_after_writing(harness, backend)
    operation = create("A.md", "# a\n")

    result = harness.provider.apply_changes(change_set(operation))

    assert result_for(result, operation).status is FAILED
    assert "disk exploded" in (result_for(result, operation).message or "")
    assert not (harness.workdir / "A.md").exists()
    assert_next_apply_is_not_blocked(harness, backend)


def test_a_raising_backend_restores_a_tracked_page_and_spares_a_concurrent_user_file(tmp_path: Path) -> None:
    harness, backend = scripted(tmp_path)
    seed_committed(harness, "A.md", "# committed\n")

    def write_then_raise(changeset: Any, real: ApplyResult) -> ApplyResult:
        (harness.workdir / "mine.md").write_text("the user's own file")
        raise RuntimeError("disk exploded")

    backend.on_apply = write_then_raise
    operation = update("A.md", "# changed\n")

    result = harness.provider.apply_changes(change_set(operation))

    message = result_for(result, operation).message or ""
    assert (harness.workdir / "A.md").read_text() == "# committed\n"
    assert (harness.workdir / "mine.md").read_text() == "the user's own file"
    assert "'mine.md'" in message and "disk exploded" in message


def test_a_raising_backend_rolls_back_every_delegated_target(tmp_path: Path) -> None:
    harness, backend = scripted(tmp_path)
    raise_after_writing(harness, backend)
    first, second = create("One.md"), create("Two.md")

    harness.provider.apply_changes(change_set(first, second))

    assert not (harness.workdir / "One.md").exists()
    assert not (harness.workdir / "Two.md").exists()


def test_a_snapshot_failure_fails_the_apply_before_the_backend_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, backend = scripted(tmp_path)

    def unreadable(git: Any) -> None:
        raise GithubWikiError("sync.git_failed", "git status failed")

    monkeypatch.setattr(rollback, "take_snapshot", unreadable)
    operation = create("A.md")

    result = harness.provider.apply_changes(change_set(operation))

    assert result_for(result, operation).status is FAILED
    assert backend.applied == []


# -- what could not be undone is also said on the exception paths ------------------------------------------


def test_leftovers_of_a_failed_manifest_record_are_in_the_failure_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, backend = scripted(tmp_path)
    seed_committed(harness, "A.md", "# committed\n")
    harness.fake.fail["checkout"] = (1, "error: unable to write\n")

    def broken(self: PendingManifest, paths: Any) -> None:
        raise GithubWikiError("workdir.unusable", "manifest write failed")

    monkeypatch.setattr(PendingManifest, "record", broken)
    operation = update("A.md", "# changed\n")

    result = harness.provider.apply_changes(change_set(operation))

    message = result_for(result, operation).message or ""
    assert message.startswith("[github_wiki:workdir.unusable]")
    assert "'A.md'" in message and "restore or delete" in message


def test_leftovers_of_a_raising_backend_are_in_the_failure_message_without_losing_the_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, backend = scripted(tmp_path)
    raise_after_writing(harness, backend)

    def refuse(self: Path, *args: Any, **kwargs: Any) -> None:
        raise PermissionError("read-only file system")

    monkeypatch.setattr(Path, "unlink", refuse)
    operation = create("A.md")

    result = harness.provider.apply_changes(change_set(operation))

    message = result_for(result, operation).message or ""
    assert "Unexpected error while applying the operations (RuntimeError: disk exploded)" in message
    assert "'A.md'" in message and "restore or delete" in message


def test_leftovers_of_a_rejected_asset_reference_are_in_the_raised_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, backend = scripted(tmp_path)
    backend.on_asset = lambda op, content, real: Asset(
        ref=AssetRef(provider="docs", kind=AssetRefKind.PATH, locator={"path": "../x.png"}),
        name=real.name,
        media_type=real.media_type,
    )

    def refuse(self: Path, *args: Any, **kwargs: Any) -> None:
        raise PermissionError("read-only file system")

    monkeypatch.setattr(Path, "unlink", refuse)

    with pytest.raises(GithubWikiError) as error:
        harness.provider.put_asset(asset("b.png", "b"), PNG + b"3")

    assert error.value.code == "asset.ref_unsupported"
    assert "assets/b--" in str(error.value) and "restore or delete" in str(error.value)


# -- put_asset: a rejected asset reference ---------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        AssetRef(provider="docs", kind=AssetRefKind.PATH, locator={"path": "/abs/x.png"}),
        AssetRef(provider="docs", kind=AssetRefKind.PATH, locator={"path": "../x.png"}),
        AssetRef(provider="docs", kind=AssetRefKind.URL, locator={"url": "https://x.test/a.png"}),
    ],
    ids=["root-anchored", "traversal", "url"],
)
def test_an_asset_stored_under_a_rejected_reference_is_removed_and_not_pending(
    tmp_path: Path, bad: AssetRef
) -> None:
    harness, backend = scripted(tmp_path)
    pending = dict(manifest_of(harness))
    backend.on_asset = lambda op, content, real: Asset(ref=bad, name=real.name, media_type=real.media_type)
    stored_path = "assets/b.png"

    with pytest.raises(GithubWikiError) as error:
        harness.provider.put_asset(asset("b.png", "b"), PNG + b"3")

    assert error.value.code == "asset.ref_unsupported"
    assert not [found for found in (harness.workdir / "assets").glob("*") if found.name.startswith("b")], stored_path
    assert manifest_of(harness) == pending
    backend.on_asset = None
    harness.provider.put_asset(asset("c.png", "c"), PNG + b"4")  # not blocked by the orphan


def test_an_asset_the_manifest_cannot_record_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, backend = scripted(tmp_path)
    files_before = {found.name for found in (harness.workdir / "assets").glob("*")}

    def broken(self: PendingManifest, paths: Any) -> None:
        raise GithubWikiError("workdir.unusable", "manifest write failed")

    monkeypatch.setattr(PendingManifest, "record", broken)

    with pytest.raises(GithubWikiError) as error:
        harness.provider.put_asset(asset("b.png", "b"), PNG + b"3")

    assert error.value.code == "workdir.unusable"
    assert {found.name for found in (harness.workdir / "assets").glob("*")} == files_before


def test_an_asset_backend_that_raises_after_writing_is_rolled_back_and_a_user_file_survives(
    tmp_path: Path,
) -> None:
    harness, backend = scripted(tmp_path)
    pending = dict(manifest_of(harness))
    stored = harness.workdir / "assets"

    def write_then_raise(op: Any, content: bytes, real: Asset) -> Asset:
        (harness.workdir / "mine.md").write_text("the user's own file")
        raise RuntimeError("disk exploded")

    backend.on_asset = write_then_raise
    before = {found.name for found in stored.glob("*")}

    with pytest.raises(rollback.RollbackIncomplete) as error:
        harness.provider.put_asset(asset("b.png", "b"), PNG + b"3")

    assert "disk exploded" in str(error.value) and "'mine.md'" in str(error.value)
    assert {found.name for found in stored.glob("*")} == before
    assert (harness.workdir / "mine.md").read_text() == "the user's own file"
    assert manifest_of(harness) == pending


def test_a_raising_asset_backend_reports_what_it_could_not_undo_in_the_raised_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, backend = scripted(tmp_path)

    def write_then_raise(op: Any, content: bytes, real: Asset) -> Asset:
        raise RuntimeError("disk exploded")

    backend.on_asset = write_then_raise

    def refuse(self: Path, *args: Any, **kwargs: Any) -> None:
        raise PermissionError("read-only file system")

    monkeypatch.setattr(Path, "unlink", refuse)

    with pytest.raises(rollback.RollbackIncomplete) as error:
        harness.provider.put_asset(asset("b.png", "b"), PNG + b"3")

    assert "RuntimeError: disk exploded" in str(error.value)
    assert "assets/b--" in str(error.value) and "restore or delete" in str(error.value)
    assert isinstance(error.value.__cause__, RuntimeError)


def test_an_asset_stored_under_another_name_by_a_user_is_not_deleted(tmp_path: Path) -> None:
    harness, backend = scripted(tmp_path)

    def user_file_then_bad_ref(op: Any, content: bytes, real: Asset) -> Asset:
        (harness.workdir / "assets" / "user-notes.txt").write_text("mine")
        return Asset(
            ref=AssetRef(provider="docs", kind=AssetRefKind.PATH, locator={"path": "../x.png"}),
            name=real.name,
            media_type=real.media_type,
        )

    backend.on_asset = user_file_then_bad_ref

    with pytest.raises(GithubWikiError) as error:
        harness.provider.put_asset(asset("b.png", "b"), PNG + b"3")

    assert (harness.workdir / "assets" / "user-notes.txt").read_text() == "mine"
    assert "'assets/user-notes.txt'" in str(error.value) and "left untouched" in str(error.value)


# -- the rollback function on its own ------------------------------------------------------------------------


def everything(path: str) -> bool:
    return True


def test_roll_back_touches_only_owned_paths_that_appeared_since_the_snapshot(tmp_path: Path) -> None:
    harness, backend = scripted(tmp_path)
    git = harness.provider._git_facade()
    before = rollback.take_snapshot(git)
    (harness.workdir / "new.md").write_text("n")
    (harness.workdir / "kept.md").write_text("k")
    (harness.workdir / "other.md").write_text("o")

    result = rollback.roll_back(git, before=before, owns={"new.md", "kept.md"}.__contains__, keep=["kept.md"])

    assert result == rollback.RollbackResult(foreign=("other.md",))
    assert not (harness.workdir / "new.md").exists()
    assert (harness.workdir / "kept.md").exists()
    assert (harness.workdir / "other.md").exists()
    assert all((harness.workdir / path).exists() for path in manifest_of(harness))


def test_roll_back_never_touches_a_path_that_was_dirty_at_the_snapshot(tmp_path: Path) -> None:
    harness, backend = scripted(tmp_path)
    git = harness.provider._git_facade()
    (harness.workdir / "early.md").write_text("e")
    before = rollback.take_snapshot(git)

    result = rollback.roll_back(git, before=before, owns=everything)

    assert result == rollback.RollbackResult()
    assert (harness.workdir / "early.md").read_text() == "e"


def test_roll_back_refuses_to_delete_through_a_symlinked_directory(tmp_path: Path) -> None:
    harness, backend = scripted(tmp_path)
    git = harness.provider._git_facade()
    before = rollback.take_snapshot(git)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim.md").write_text("precious")
    (harness.workdir / "link").symlink_to(outside, target_is_directory=True)
    harness.fake.dirty.append("?? link/victim.md")

    result = rollback.roll_back(git, before=before, owns=everything)

    assert result.leftovers == ("link/victim.md",)
    assert (outside / "victim.md").read_text() == "precious"


def test_roll_back_never_expands_a_collapsed_directory_even_when_it_is_owned(tmp_path: Path) -> None:
    harness, backend = scripted(tmp_path)
    git = harness.provider._git_facade()
    before = rollback.take_snapshot(git)
    harness.fake.dirty.append("?? nested-repo/")

    result = rollback.roll_back(git, before=before, owns=everything)

    assert result == rollback.RollbackResult(foreign=("nested-repo/",))


def test_roll_back_reports_a_file_it_cannot_unlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    harness, backend = scripted(tmp_path)
    git = harness.provider._git_facade()
    before = rollback.take_snapshot(git)
    (harness.workdir / "new.md").write_text("n")

    def refuse(self: Path, *args: Any, **kwargs: Any) -> None:
        raise PermissionError("read-only file system")

    monkeypatch.setattr(Path, "unlink", refuse)

    result = rollback.roll_back(git, before=before, owns=everything)

    assert result == rollback.RollbackResult(leftovers=("new.md",))
    assert (harness.workdir / "new.md").exists()


def test_roll_back_never_deletes_a_path_that_climbs_out_of_the_workdir(tmp_path: Path) -> None:
    harness, backend = scripted(tmp_path)
    git = harness.provider._git_facade()
    before = rollback.take_snapshot(git)
    (tmp_path / "victim.md").write_text("precious")
    harness.fake.dirty.append(f"?? {'../' * 12}{tmp_path.as_posix().lstrip('/')}/victim.md")

    result = rollback.roll_back(git, before=before, owns=everything)

    assert len(result.leftovers) == 1
    assert (tmp_path / "victim.md").read_text() == "precious"


def test_roll_back_reports_an_unreadable_status_as_a_typed_error_not_as_a_path(tmp_path: Path) -> None:
    harness, backend = scripted(tmp_path)
    git = harness.provider._git_facade()
    before = rollback.take_snapshot(git)
    harness.fake.fail["status"] = (128, "fatal: index file corrupt\n")

    result = rollback.roll_back(git, before=before, owns=everything)

    assert result.leftovers == ()
    assert result.status_error is not None
    assert "could not be read" in result.note
    assert not result.clean


def test_a_clean_result_has_no_note_and_a_dirty_one_names_every_path(tmp_path: Path) -> None:
    assert rollback.RollbackResult().note == ""
    assert rollback.RollbackResult().clean
    dirty = rollback.RollbackResult(leftovers=("a.md",), foreign=("b.md",))
    assert "'a.md'" in dirty.note and "restore or delete" in dirty.note
    assert "'b.md'" in dirty.note and "left untouched" in dirty.note


def test_annotate_keeps_the_error_when_nothing_is_left_and_adds_the_note_otherwise() -> None:
    plain = RuntimeError("boom")
    assert rollback.annotate(plain, rollback.RollbackResult()) is plain
    leftovers = rollback.RollbackResult(leftovers=("a.md",))
    coded = rollback.annotate(GithubWikiError("workdir.unusable", "no space"), leftovers)
    assert isinstance(coded, GithubWikiError) and coded.code == "workdir.unusable"
    assert "no space" in str(coded) and "'a.md'" in str(coded)
    wrapped = rollback.annotate(plain, leftovers)
    assert isinstance(wrapped, rollback.RollbackIncomplete)
    assert wrapped.cause is plain and "RuntimeError: boom" in str(wrapped) and "'a.md'" in str(wrapped)
