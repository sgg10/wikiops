"""Real git: what an apply stages, commits and leaves behind (GW-S8..S10, S13, S15).

The real provider writes through the real ``local_files`` backend, then stages,
commits and (here) never pushes; the clone is inspected with real git.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from wikiops_sdk.domain import OperationStatus

from tests.support.real_git import SEED_CONTENT, SEED_PAGE, Wiki, install_hook, run_git
from tests.support.write_ops import ScriptedResolver, asset, change_set, create, ref, update
from wikiops.providers.github_wiki.errors import GithubWikiError
from wikiops.providers.github_wiki.manifest import PendingManifest

pytestmark = pytest.mark.git_integration

COMMIT_MESSAGE = "docs(wiki): update via wikiops plugin azure-docs"


def statuses(result) -> list[OperationStatus]:  # noqa: ANN001
    return [item.status for item in result.results]


def messages(result) -> str:  # noqa: ANN001
    return " | ".join(item.message or "" for item in result.results)


# -- one commit, exact paths -------------------------------------------------------------------


def test_one_commit_holds_every_page_and_the_asset(wiki: Wiki) -> None:
    provider = wiki.provider()
    stored = provider.put_asset(asset("logo.png"), b"PNG-bytes")
    asset_path = stored.ref.locator["path"]

    result = provider.apply_changes(
        change_set(create("Guide.md", "# Guide\n"), create("Setup.md", "# Setup\n"), update(SEED_PAGE, "# New home\n"))
    )

    assert statuses(result) == [OperationStatus.APPLIED] * 3
    assert wiki.commit_count() == 2  # the seed plus ONE commit
    assert wiki.last_commit_files() == sorted(["Guide.md", "Setup.md", SEED_PAGE, asset_path])
    assert wiki.subjects()[0] == COMMIT_MESSAGE
    assert wiki.git("show", f"HEAD:{asset_path}") == "PNG-bytes"
    assert wiki.status() == []


def test_unrelated_ignored_untracked_and_staged_files_never_ride_along(wiki: Wiki) -> None:
    resolver = ScriptedResolver()
    wiki.resolver = resolver
    provider = wiki.provider()
    provider.exists(ref(SEED_PAGE))  # clone first so the exclude file can be written
    (wiki.git_dir / "info" / "exclude").write_text("scratch.tmp\n")
    (wiki.workdir / "scratch.tmp").write_text("ignored\n")

    def foreign_changes_appear_mid_apply() -> None:
        # After the dirty re-check, as a concurrent user would: one staged, one untracked.
        (wiki.workdir / "staged.txt").write_text("staged by someone\n")
        wiki.git("add", "--", "staged.txt")
        (wiki.workdir / "late.tmp").write_text("untracked\n")

    assert resolver.backend is not None
    resolver.backend.before = foreign_changes_appear_mid_apply
    result = provider.apply_changes(change_set(create("Mine.md", "# mine\n")))

    assert statuses(result) == [OperationStatus.APPLIED]
    assert wiki.last_commit_files() == ["Mine.md"]
    assert "staged.txt" not in wiki.committed_files()
    assert sorted(wiki.status()) == ["?? late.tmp", "A  staged.txt"]  # still theirs, untouched


def test_a_glob_pattern_in_a_page_name_stages_only_that_literal_page(wiki: Wiki) -> None:
    resolver = ScriptedResolver()
    wiki.resolver = resolver
    provider = wiki.provider()
    provider.exists(ref(SEED_PAGE))

    def lookalike_appears_mid_apply() -> None:
        (wiki.workdir / "xa.md").write_text("# look-alike\n")

    assert resolver.backend is not None
    resolver.backend.before = lookalike_appears_mid_apply
    result = provider.apply_changes(change_set(create("x[ab].md", "# bracket\n")))

    assert statuses(result) == [OperationStatus.APPLIED], messages(result)
    assert wiki.last_commit_files() == ["x[ab].md"]  # a glob pathspec would also have taken xa.md
    assert wiki.status() == ["?? xa.md"]


def test_an_identical_apply_makes_no_commit(wiki: Wiki) -> None:
    first = wiki.provider().apply_changes(change_set(update(SEED_PAGE, "# Changed\n")))
    head = wiki.head()

    second = wiki.provider().apply_changes(change_set(update(SEED_PAGE, "# Changed\n")))

    assert statuses(first) == [OperationStatus.APPLIED]
    assert statuses(second) == [OperationStatus.SKIPPED]
    assert wiki.head() == head and wiki.commit_count() == 2 and wiki.status() == []


def test_the_seed_content_unchanged_is_not_committed_either(wiki: Wiki) -> None:
    result = wiki.provider().apply_changes(change_set(update(SEED_PAGE, SEED_CONTENT)))

    assert statuses(result) == [OperationStatus.SKIPPED]
    assert wiki.commit_count() == 1


# -- the pending manifest ---------------------------------------------------------------------------


def test_uncommitted_pages_stay_pending_and_ride_the_next_commit(wiki: Wiki) -> None:
    held_back = wiki.provider(allow_auto_commit=False).apply_changes(change_set(create("First.md", "# first\n")))
    manifest = PendingManifest(wiki.manifest_file, workdir=wiki.workdir)

    assert statuses(held_back) == [OperationStatus.APPLIED]
    assert "not committed" in messages(held_back) and "allow_auto_commit=false" in messages(held_back)
    assert wiki.commit_count() == 1
    assert set(manifest.entries()) == {"First.md"}
    assert wiki.status() == ["?? First.md"]  # the manifest lives inside .git, never in the status

    committed = wiki.provider(allow_auto_commit=True).apply_changes(change_set(create("Second.md", "# second\n")))

    assert statuses(committed) == [OperationStatus.APPLIED]
    assert wiki.last_commit_files() == ["First.md", "Second.md"]
    assert manifest.entries() == {} and wiki.status() == []
    assert wiki.manifest_file.exists()


def test_a_pending_page_edited_by_hand_blocks_the_next_apply(wiki: Wiki) -> None:
    wiki.provider(allow_auto_commit=False).apply_changes(change_set(create("First.md", "# first\n")))
    (wiki.workdir / "First.md").write_text("# edited by the user\n")

    result = wiki.provider().apply_changes(change_set(create("Second.md", "# second\n")))

    assert statuses(result) == [OperationStatus.FAILED]
    assert "workdir.dirty" in messages(result) and "First.md" in messages(result)
    assert not (wiki.workdir / "Second.md").exists() and wiki.commit_count() == 1


def test_a_corrupt_manifest_stops_everything_and_writes_nothing(wiki: Wiki) -> None:
    wiki.provider().exists(ref(SEED_PAGE))
    wiki.manifest_file.parent.mkdir(parents=True, exist_ok=True)
    wiki.manifest_file.write_text("{this is not json")

    result = wiki.provider().apply_changes(change_set(create("New.md", "# new\n")))
    with pytest.raises(GithubWikiError) as plan:
        wiki.provider().exists(ref(SEED_PAGE))

    assert statuses(result) == [OperationStatus.FAILED]
    assert "workdir.manifest_corrupt" in messages(result) and str(wiki.manifest_file) in messages(result)
    assert plan.value.code == "workdir.manifest_corrupt"
    assert not (wiki.workdir / "New.md").exists() and wiki.status() == [] and wiki.commit_count() == 1


# -- a failing commit hook -------------------------------------------------------------------------


def test_a_failing_pre_commit_hook_keeps_the_pages_and_recovery_commits_both(wiki: Wiki) -> None:
    wiki.provider().exists(ref(SEED_PAGE))
    hook = install_hook(wiki.git_dir, "pre-commit", "echo 'lint failed: no trailing blank lines' >&2\nexit 1")

    failed = wiki.provider().apply_changes(change_set(create("First.md", "# first\n")))

    assert statuses(failed) == [OperationStatus.FAILED]
    text = messages(failed)
    assert "commit.failed" in text and "written but not committed" in text
    assert str(wiki.workdir) in text and "lint failed" in text
    assert wiki.commit_count() == 1 and (wiki.workdir / "First.md").exists()
    assert wiki.status() == ["A  First.md"]  # left staged for inspection

    hook.unlink()  # the hook problem is fixed
    recovered = wiki.provider().apply_changes(change_set(create("Second.md", "# second\n")))

    assert statuses(recovered) == [OperationStatus.APPLIED]
    assert wiki.last_commit_files() == ["First.md", "Second.md"]
    assert wiki.status() == []


# -- identity ----------------------------------------------------------------------------------------


def authors(wiki: Wiki) -> str:
    return wiki.git("log", "-1", "--format=%an <%ae> | %cn <%ce>").strip()


@pytest.mark.parametrize(
    ("commit", "expected"),
    [
        (None, "Git User <git.user@example.test> | Git User <git.user@example.test>"),
        (
            {"identity": {"mode": "bot"}},
            "wikiops <wikiops@users.noreply.github.com> | wikiops <wikiops@users.noreply.github.com>",
        ),
        (
            {"identity": {"mode": "custom", "name": "Doc Bot", "email": "doc.bot@example.test"}},
            "Doc Bot <doc.bot@example.test> | Doc Bot <doc.bot@example.test>",
        ),
    ],
    ids=["git", "bot", "custom"],
)
def test_the_commit_identity_follows_the_mode_and_the_clone_config_is_untouched(
    wiki: Wiki, commit: dict | None, expected: str
) -> None:
    settings = {} if commit is None else {"commit": commit}
    wiki.provider(**settings).exists(ref(SEED_PAGE))
    config = (wiki.git_dir / "config").read_bytes()

    result = wiki.provider(**settings).apply_changes(change_set(create("New.md", "# new\n")))

    assert statuses(result) == [OperationStatus.APPLIED]
    assert authors(wiki) == expected
    assert (wiki.git_dir / "config").read_bytes() == config
    assert wiki.git("config", "--local", "--get", "user.name", check=False) == ""


def test_a_missing_git_identity_is_reported_and_the_page_stays_pending(
    wiki: Wiki, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wiki.provider().exists(ref(SEED_PAGE))
    empty = tmp_path / "empty.gitconfig"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.delenv("EMAIL", raising=False)
    run_git("config", "--local", "user.useConfigOnly", "true", cwd=wiki.workdir)  # no guessing from the host

    result = wiki.provider().apply_changes(change_set(create("New.md", "# new\n")))

    assert statuses(result) == [OperationStatus.FAILED]
    assert "commit.identity_missing" in messages(result) and "written but not committed" in messages(result)
    assert (wiki.workdir / "New.md").exists() and wiki.commit_count() == 1


# -- the manifest file itself ------------------------------------------------------------------------


def test_the_manifest_records_what_was_written_with_its_hash(wiki: Wiki) -> None:
    wiki.provider(allow_auto_commit=False).apply_changes(change_set(create("First.md", "# first\n")))

    document = json.loads(wiki.manifest_file.read_text())

    assert document["version"] == 1
    assert list(document["paths"]) == ["First.md"]
    assert document["paths"]["First.md"].startswith("sha256:")


# -- a rejected backend write leaves nothing behind (S12.F1) ----------------------------------------


def test_a_rejected_backend_write_is_rolled_back_and_the_next_apply_goes_through(wiki: Wiki) -> None:
    resolver = ScriptedResolver()
    wiki.resolver = resolver
    provider = wiki.provider()
    provider.exists(ref(SEED_PAGE))  # clone and create the scripted backend
    assert resolver.backend is not None

    def report_a_nested_ref(changeset, real):  # noqa: ANN001, ANN202
        reported = [item.model_copy(update={"resolved_ref": ref("../evil.md")}) for item in real.results]
        return type(real)(provider_name=real.provider_name, results=reported)

    resolver.backend.on_apply = report_a_nested_ref

    rejected = provider.apply_changes(change_set(create("Orphan.md", "# orphan\n"), update(SEED_PAGE, "# edited\n")))

    assert statuses(rejected) == [OperationStatus.FAILED] * 2
    assert wiki.status() == []  # the new file is gone, the seed page is back to HEAD
    assert (wiki.workdir / SEED_PAGE).read_text() == SEED_CONTENT
    assert wiki.commit_count() == 1

    resolver.backend.on_apply = None
    again = wiki.provider().apply_changes(change_set(create("Next.md", "# next\n")))

    assert statuses(again) == [OperationStatus.APPLIED]
    assert wiki.last_commit_files() == ["Next.md"]
