"""A write the backend completed but the provider then rejects is rolled back (S12.F1).

When the backend already wrote a page or an asset and the provider refuses what it
reported (a ref the page policy rejects, a forbidden link, an asset reference a wiki cannot
link, a manifest that cannot record it), the file would stay in the clone and the NEXT run
would be blocked by our own orphan as ``workdir.dirty``. The provider restores a tracked file
from HEAD and removes a file that this operation created, under the same lock, and never
touches what was already pending before the operation started.
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


def test_an_unreported_stray_file_is_removed_with_the_rejected_write(tmp_path: Path) -> None:
    harness, backend = scripted(tmp_path)

    def stray_and_reject(changeset: Any, real: ApplyResult) -> ApplyResult:
        (harness.workdir / "stray.md").write_text("unreported")
        return retarget(real, resolved_ref=ref("../evil.md"))

    backend.on_apply = stray_and_reject

    harness.provider.apply_changes(change_set(create("A.md")))

    assert not (harness.workdir / "stray.md").exists()
    assert_next_apply_is_not_blocked(harness, backend)


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


# -- the rollback function on its own ------------------------------------------------------------------


def test_roll_back_ignores_dirt_that_existed_before_and_what_it_must_keep(tmp_path: Path) -> None:
    harness, backend = scripted(tmp_path)
    git = harness.provider._git_facade()
    (harness.workdir / "new.md").write_text("n")
    (harness.workdir / "kept.md").write_text("k")
    before = set(manifest_of(harness))

    left = rollback.roll_back(git, before=before, keep=["kept.md"])

    assert left == ()
    assert not (harness.workdir / "new.md").exists()
    assert (harness.workdir / "kept.md").exists()
    assert all((harness.workdir / path).exists() for path in before)


def test_roll_back_refuses_to_delete_through_a_symlinked_directory(tmp_path: Path) -> None:
    harness, backend = scripted(tmp_path)
    git = harness.provider._git_facade()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim.md").write_text("precious")
    (harness.workdir / "link").symlink_to(outside, target_is_directory=True)
    harness.fake.dirty.append("?? link/victim.md")

    left = rollback.roll_back(git, before=set(), keep=[])

    assert left == ("link/victim.md",)
    assert (outside / "victim.md").read_text() == "precious"


def test_roll_back_reports_a_collapsed_directory_it_cannot_expand(tmp_path: Path) -> None:
    harness, backend = scripted(tmp_path)
    git = harness.provider._git_facade()
    harness.fake.dirty.append("?? nested-repo/")

    left = rollback.roll_back(git, before=set(), keep=[])

    assert left == ("nested-repo/",)


def test_roll_back_reports_a_file_it_cannot_unlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    harness, backend = scripted(tmp_path)
    git = harness.provider._git_facade()
    (harness.workdir / "new.md").write_text("n")

    def refuse(self: Path, *args: Any, **kwargs: Any) -> None:
        raise PermissionError("read-only file system")

    monkeypatch.setattr(Path, "unlink", refuse)

    left = rollback.roll_back(git, before=set(manifest_of(harness)), keep=[])

    assert left == ("new.md",)
    assert (harness.workdir / "new.md").exists()


def test_roll_back_never_deletes_a_path_that_climbs_out_of_the_workdir(tmp_path: Path) -> None:
    harness, backend = scripted(tmp_path)
    git = harness.provider._git_facade()
    (tmp_path / "victim.md").write_text("precious")
    harness.fake.dirty.append(f"?? {'../' * 12}{tmp_path.as_posix().lstrip('/')}/victim.md")

    left = rollback.roll_back(git, before=set(), keep=[])

    assert len(left) == 1
    assert (tmp_path / "victim.md").read_text() == "precious"
