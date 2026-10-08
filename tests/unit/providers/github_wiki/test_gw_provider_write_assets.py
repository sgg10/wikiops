"""``GithubWikiProvider.put_asset``: one asset written under the workdir lock, tracked as pending.

The provider syncs for apply, takes the lock, delegates to the backend, validates the
reference the backend returns (a wiki can only link a canonical root-relative path),
records path and hash in the pending manifest and releases the lock. It never commits:
the asset stays pending and rides the next committing apply.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from wikiops_sdk.contracts import DocumentProvider
from wikiops_sdk.domain import Asset, AssetRef, AssetRefKind

from tests.support.provider_harness import ProviderHarness, build_provider
from tests.support.write_ops import ScriptedResolver, asset, asset_ref
from wikiops.providers._fs import content_version
from wikiops.providers.github_wiki.errors import GithubWikiError
from wikiops.providers.github_wiki.lock import WorkdirLock
from wikiops.providers.github_wiki.manifest import PendingManifest
from wikiops.providers.github_wiki.workdir import lock_path, manifest_path

PNG = b"\x89PNG\r\n\x1a\nfake image bytes"


def manifest_of(harness: ProviderHarness) -> dict[str, str]:
    return PendingManifest(manifest_path(harness.fake.git_dir), workdir=harness.workdir).entries()


def lock_record(harness: ProviderHarness) -> str:
    return (harness.fake.git_dir / "wikiops" / "lock").read_text().strip()


def scripted(tmp_path: Path, **options: object) -> tuple[ProviderHarness, ScriptedResolver]:
    resolver = ScriptedResolver()
    harness = build_provider(tmp_path, cloned=True, track_files=True, backends=resolver, **options)
    return harness, resolver


# -- the provider is a full DocumentProvider ---------------------------------------------------


def test_the_provider_now_satisfies_the_whole_document_provider_contract(tmp_path: Path) -> None:
    harness = build_provider(tmp_path)

    assert isinstance(harness.provider, DocumentProvider)


# -- the happy path -------------------------------------------------------------------------------


def test_put_asset_stores_the_bytes_under_the_workdir_and_returns_the_backend_asset(tmp_path: Path) -> None:
    harness = build_provider(tmp_path, cloned=True, track_files=True)

    stored = harness.provider.put_asset(asset(), PNG)

    path = stored.ref.locator["path"]
    assert path.startswith("assets/") and path.endswith(".png")
    assert (harness.workdir / path).read_bytes() == PNG
    assert stored.ref.kind is AssetRefKind.PATH


def test_the_asset_reference_of_the_stored_asset_is_document_relative(tmp_path: Path) -> None:
    harness = build_provider(tmp_path, cloned=True, track_files=True)

    stored = harness.provider.put_asset(asset(), PNG)

    reference = harness.provider.build_asset_reference(stored.ref)
    assert reference == stored.ref.locator["path"]
    assert reference.startswith("assets/") and not reference.startswith("/")


def test_put_asset_records_path_and_content_hash_in_the_pending_manifest(tmp_path: Path) -> None:
    harness = build_provider(tmp_path, cloned=True, track_files=True)

    stored = harness.provider.put_asset(asset(), PNG)

    assert manifest_of(harness) == {stored.ref.locator["path"]: content_version(PNG)}


def test_an_asset_does_not_make_a_commit_and_stays_dirty_and_pending(tmp_path: Path) -> None:
    harness = build_provider(tmp_path, cloned=True, track_files=True, settings={"allow_auto_push": True})

    stored = harness.provider.put_asset(asset(), PNG)

    assert harness.fake.commits == []
    assert harness.fake.pushes == []
    assert "commit" not in harness.git_subcommands() and "add" not in harness.git_subcommands()
    assert f"?? {stored.ref.locator['path']}" in harness.fake.dirty_entries()


def test_two_assets_are_both_tracked(tmp_path: Path) -> None:
    harness = build_provider(tmp_path, cloned=True, track_files=True)

    first = harness.provider.put_asset(asset("a.png", "a"), PNG)
    second = harness.provider.put_asset(asset("b.png", "b"), PNG + b"2")

    assert set(manifest_of(harness)) == {first.ref.locator["path"], second.ref.locator["path"]}


def test_a_custom_backend_decides_the_asset_location(tmp_path: Path) -> None:
    harness = build_provider(
        tmp_path, cloned=True, track_files=True, settings={"local_backend": {"type": "fake_files"}}
    )

    stored = harness.provider.put_asset(asset("diagram.svg"), b"<svg/>")

    assert stored.ref.locator["path"].startswith("assets/diagram--")
    assert (harness.workdir / stored.ref.locator["path"]).read_bytes() == b"<svg/>"
    assert set(manifest_of(harness)) == {stored.ref.locator["path"]}


# -- sync, lock ------------------------------------------------------------------------------------


def test_put_asset_syncs_for_apply_even_when_plans_are_offline(tmp_path: Path) -> None:
    harness = build_provider(
        tmp_path, cloned=True, track_files=True, settings={"sync_on_plan": False}
    )

    harness.provider.put_asset(asset(), PNG)

    assert "fetch" in harness.network_calls()


def test_the_lock_is_held_while_the_backend_writes_and_released_afterwards(tmp_path: Path) -> None:
    harness, resolver = scripted(tmp_path)
    seen: list[dict[str, object]] = []

    def probe() -> None:
        seen.append(json.loads(lock_record(harness)))

    harness.provider.put_asset(asset(), PNG)  # first call creates the backend
    assert resolver.backend is not None
    resolver.backend.before = probe

    harness.provider.put_asset(asset("again.png", "again"), PNG + b"1")

    assert len(seen) == 1 and seen[0]["purpose"] == "apply"
    assert lock_record(harness) == ""  # released: the holder record is cleared


def test_the_lock_is_released_when_the_backend_fails(tmp_path: Path) -> None:
    harness, resolver = scripted(tmp_path)
    harness.provider.put_asset(asset(), PNG)
    assert resolver.backend is not None
    resolver.backend.asset_error = OSError("disk full")

    with pytest.raises(OSError, match="disk full"):
        harness.provider.put_asset(asset("b.png", "b"), PNG)

    other = WorkdirLock(lock_path(harness.fake.git_dir), workdir=harness.workdir)
    with other.hold("check"):  # would raise workdir.locked if the provider kept it
        pass


def test_a_lock_held_by_another_run_fails_fast_and_writes_nothing(tmp_path: Path) -> None:
    harness = build_provider(tmp_path, cloned=True, track_files=True)
    other = WorkdirLock(lock_path(harness.fake.git_dir), workdir=harness.workdir)

    with other.hold("other-run"):
        with pytest.raises(GithubWikiError) as error:
            harness.provider.put_asset(asset(), PNG)

    assert error.value.code == "workdir.locked"
    assert not (harness.workdir / "assets").exists()
    assert harness.resolver.created == []


def test_a_sync_failure_propagates_and_nothing_is_written(tmp_path: Path) -> None:
    harness = build_provider(
        tmp_path, cloned=True, track_files=True, local=["c1", "mine"], remote={"master": ["c1", "theirs"]}
    )

    with pytest.raises(GithubWikiError) as error:
        harness.provider.put_asset(asset(), PNG)

    assert error.value.code == "sync.diverged"
    assert harness.resolver.created == []
    assert not (harness.workdir / "assets").exists()


# -- dirty re-check under the lock ------------------------------------------------------------------


def test_a_foreign_change_that_appeared_after_the_sync_refuses_the_next_asset(tmp_path: Path) -> None:
    harness, resolver = scripted(tmp_path)
    harness.provider.put_asset(asset(), PNG)  # synced; the first asset is now pending
    (harness.workdir / "notes.txt").write_text("not ours")

    with pytest.raises(GithubWikiError) as error:
        harness.provider.put_asset(asset("b.png", "b"), PNG + b"2")

    assert error.value.code == "workdir.dirty"
    assert "notes.txt" in str(error.value)
    assert resolver.backend is not None and len(resolver.backend.assets) == 1  # the second never reached it


def test_assets_written_earlier_in_the_run_are_pending_not_foreign(tmp_path: Path) -> None:
    harness = build_provider(tmp_path, cloned=True, track_files=True)

    harness.provider.put_asset(asset("a.png", "a"), PNG)
    second = harness.provider.put_asset(asset("b.png", "b"), PNG + b"2")  # re-check passes over the first

    assert second.ref.locator["path"] in manifest_of(harness)


# -- the returned reference is validated -------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        AssetRef(provider="docs", kind=AssetRefKind.PATH, locator={"path": "/assets/x.png"}),
        AssetRef(provider="docs", kind=AssetRefKind.PATH, locator={"path": "../x.png"}),
        AssetRef(provider="docs", kind=AssetRefKind.PATH, locator={"path": "assets/../../x.png"}),
        AssetRef(provider="docs", kind=AssetRefKind.PATH, locator={"path": ".git/hooks/pre-commit"}),
        AssetRef(provider="docs", kind=AssetRefKind.PATH, locator={}),
        AssetRef(provider="docs", kind=AssetRefKind.URL, locator={"url": "https://x.test/a.png"}),
        AssetRef(provider="docs", kind=AssetRefKind.ID, locator={"id": "42"}),
    ],
    ids=["root-anchored", "traversal", "inner-traversal", "dot-git", "no-path", "url", "id"],
)
def test_a_reference_the_wiki_cannot_link_fails_the_asset_and_is_not_recorded(
    tmp_path: Path, bad: AssetRef
) -> None:
    harness, resolver = scripted(tmp_path)
    harness.provider.put_asset(asset("warmup.png", "w"), PNG)  # creates the backend
    assert resolver.backend is not None
    before = dict(manifest_of(harness))
    resolver.backend.on_asset = lambda op, content, real: Asset(
        ref=bad, name=real.name, media_type=real.media_type
    )

    with pytest.raises(GithubWikiError) as error:
        harness.provider.put_asset(asset("b.png", "b"), PNG + b"3")

    assert error.value.code == "asset.ref_unsupported"
    assert manifest_of(harness) == before


def test_a_canonical_reference_with_odd_but_legal_characters_is_accepted(tmp_path: Path) -> None:
    harness, resolver = scripted(tmp_path)
    harness.provider.put_asset(asset("warmup.png", "w"), PNG)
    assert resolver.backend is not None

    def odd_location(operation: object, content: bytes, real: Asset) -> Asset:
        (harness.workdir / "media").mkdir(exist_ok=True)
        (harness.workdir / "media" / "my image (1).png").write_bytes(content)
        return Asset(ref=asset_ref("media/my image (1).png"), name="my image (1).png", media_type="image/png")

    resolver.backend.on_asset = odd_location

    stored = harness.provider.put_asset(asset("b.png", "b"), PNG)

    assert stored.ref.locator["path"] == "media/my image (1).png"
    assert "media/my image (1).png" in manifest_of(harness)
    assert harness.provider.build_asset_reference(stored.ref) == "media/my%20image%20%281%29.png"
