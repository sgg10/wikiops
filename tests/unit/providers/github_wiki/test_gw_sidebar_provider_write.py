"""``GithubWikiProvider.apply_changes`` with the managed sidebar wired in (SB1, SB2, SB4, SB10-SB13).

The setting is opt-in: off, the provider behaves exactly as 1.4.0 and never reaches sidebar code.
On, ``_Sidebar.md`` is a reserved name and every apply that wrote or skipped a page regenerates it
inside the apply lock, after the pre-write snapshot and before publishing, so it rides the same
commit as the pages (and never counts as a page). Whatever goes wrong with the sidebar becomes a
warning on ONE result, never a failure of a page operation. Everything runs over fakes only: the
real ``local_files`` backend (wrapped so a test can fail or replace what it reports), the
``FakeWikiGit`` model with its argv log, and a real ``PendingManifest``; no process, no network.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from wikiops_sdk.domain import AppliedOperationResult, ApplyResult, ChangeSet, OperationStatus

from tests.support.fake_wiki_git import subcommand_and_args
from tests.support.provider_harness import ProviderHarness, build_provider
from tests.support.write_ops import (
    ScriptedBackend,
    ScriptedResolver,
    asset,
    change_set,
    child,
    create,
    ref,
    sidebar_hint,
    update,
)
from wikiops.providers.github_wiki import sidebar, sidebar_io
from wikiops.providers.github_wiki.manifest import PendingManifest
from wikiops.providers.github_wiki.workdir import manifest_path

APPLIED, SKIPPED, FAILED = OperationStatus.APPLIED, OperationStatus.SKIPPED, OperationStatus.FAILED
SIDEBAR = "_Sidebar.md"
SIDEBAR_OPERATION = "wikiops-sidebar"
UNMARKED = "# my own sidebar\n- [Home](Home)\n"
SECRET_URL = "https://x-access-token:ghp_SuperSecretToken123@github.com/acme/wiki.git"
ENTRY_TARGET = re.compile(r"^- \[[^\]]*\]\(([^)]*)\)", re.MULTILINE)


class Hooked(ScriptedResolver):
    """A ``ScriptedResolver`` that arms every backend it creates before the provider uses it."""

    def __init__(self, arm: Callable[[ScriptedBackend], None] | None) -> None:
        super().__init__()
        self._arm = arm

    def create(self, backend: Any, *, root: Path, provider_name: str) -> Any:
        created = super().create(backend, root=root, provider_name=provider_name)
        if self._arm is not None:
            self._arm(self.backend)  # type: ignore[arg-type]
        return created


def build(
    tmp_path: Path,
    *,
    on: bool = True,
    arm: Callable[[ScriptedBackend], None] | None = None,
    settings: dict[str, Any] | None = None,
    **model: Any,
) -> ProviderHarness:
    options: dict[str, Any] = {"generate_sidebar": on, **(settings or {})}
    return build_provider(
        tmp_path, cloned=True, track_files=True, backends=Hooked(arm), settings=options, **model
    )


def seed(harness: ProviderHarness, path: str, content: str | bytes, *, committed: bool = True) -> bytes:
    data = content.encode() if isinstance(content, str) else content
    (harness.workdir / path).write_bytes(data)
    if committed:
        harness.fake.committed_files[path] = data
    return data


def sidebar_bytes(harness: ProviderHarness) -> bytes | None:
    target = harness.workdir / SIDEBAR
    return target.read_bytes() if target.is_file() else None


def listed(harness: ProviderHarness) -> list[str]:
    data = sidebar_bytes(harness)
    assert data is not None
    return ENTRY_TARGET.findall(data.decode())


def manifest_of(harness: ProviderHarness) -> dict[str, str]:
    return PendingManifest(manifest_path(harness.fake.git_dir), workdir=harness.workdir).entries()


def staging_argv(harness: ProviderHarness) -> list[tuple[str, ...]]:
    return [
        call.argv
        for call in harness.runner.calls
        if call.argv[0] == "git" and subcommand_and_args(call.argv)[0] in {"add", "commit"}
    ]


def messages(result: ApplyResult) -> list[str]:
    return [item.message or "" for item in result.results]


def carriers(result: ApplyResult, fragment: str) -> list[int]:
    """The positions of the results whose message holds ``fragment``."""
    return [index for index, text in enumerate(messages(result)) if fragment in text]


def is_sidebar_call(changeset: ChangeSet) -> bool:
    return [operation.operation_id for operation in changeset.operations] == [SIDEBAR_OPERATION]


def on_sidebar_write(change: Callable[[ChangeSet, ApplyResult], ApplyResult]) -> Callable[[ScriptedBackend], None]:
    """Arm a backend so ``change`` post-processes only the sidebar's own write."""

    def arm(backend: ScriptedBackend) -> None:
        backend.on_apply = lambda changeset, real: change(changeset, real) if is_sidebar_call(changeset) else real

    return arm


def raise_on_sidebar_write(error: Exception) -> Callable[[ScriptedBackend], None]:
    def arm(backend: ScriptedBackend) -> None:
        def before() -> None:
            if is_sidebar_call(backend.applied[-1]):
                raise error

        backend.before = before

    return arm


def sidebar_calls(harness: ProviderHarness) -> list[ChangeSet]:
    backend = harness.resolver.inner.backend  # type: ignore[attr-defined]
    return [changeset for changeset in backend.applied if is_sidebar_call(changeset)]


def managed_text(*stems: str, placements: dict[str, sidebar.Placement] | None = None) -> str:
    return sidebar.render(stems, placements or {})


# -- SB1: off is 1.4.0, byte for byte --------------------------------------------------------------------


def test_the_setting_off_never_runs_any_sidebar_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("sidebar code ran although generate_sidebar is off")

    for name in ("SidebarWriter", "plan_action", "root_pages"):
        monkeypatch.setattr(sidebar_io, name, forbidden)
    for name in ("collect", "validate_hint", "render_hint_warning", "render", "parse", "merge"):
        monkeypatch.setattr(sidebar, name, forbidden)
    harness = build(tmp_path, on=False)
    bad_hint = create("Bad.md", metadata=sidebar_hint(order="ten"))

    result = harness.provider.apply_changes(change_set(create("Home.md"), bad_hint, create("_Sidebar.md", "# mine\n")))
    harness.provider.describe_target()
    harness.provider.resolve_ref(ref("_Sidebar.md"))

    assert [item.status for item in result.results] == [APPLIED, APPLIED, APPLIED]


def test_the_setting_off_reads_and_writes_nothing_of_the_sidebar(tmp_path: Path) -> None:
    reads: list[str] = []

    def arm(backend: ScriptedBackend) -> None:
        real_exists, real_get = backend.exists, backend.get_document
        backend.exists = lambda document: (reads.append(document.locator["path"]), real_exists(document))[1]  # type: ignore[method-assign]
        backend.get_document = lambda document: (reads.append(document.locator["path"]), real_get(document))[1]  # type: ignore[method-assign]

    harness = build(tmp_path, on=False, arm=arm)

    result = harness.provider.apply_changes(change_set(create("Home.md"), create("Setup.md")))

    assert [item.status for item in result.results] == [APPLIED, APPLIED]
    assert SIDEBAR not in reads and sidebar_bytes(harness) is None
    assert not any(SIDEBAR in argv for argv in staging_argv(harness))
    assert len(harness.resolver.inner.backend.applied) == 1  # type: ignore[attr-defined]


def test_a_sidebar_page_is_an_ordinary_page_when_the_setting_is_off(tmp_path: Path) -> None:
    harness = build(tmp_path, on=False)

    result = harness.provider.apply_changes(change_set(create("_Sidebar.md", "# my sidebar\n")))

    assert result.results[0].status is APPLIED
    assert "path.reserved" not in messages(result)[0]
    assert harness.fake.commits[0].paths == (SIDEBAR,)


def test_a_managed_sidebar_stays_byte_identical_and_unstaged_when_the_setting_is_off(tmp_path: Path) -> None:
    harness = build(tmp_path, on=False)
    before = seed(harness, SIDEBAR, managed_text("Old"))

    harness.provider.apply_changes(change_set(create("Home.md")))

    assert sidebar_bytes(harness) == before
    assert harness.fake.commits[0].paths == ("Home.md",)
    assert not any(SIDEBAR in argv for argv in staging_argv(harness))


def test_an_invalid_hint_is_not_reported_when_the_setting_is_off(tmp_path: Path) -> None:
    harness = build(tmp_path, on=False)

    result = harness.provider.apply_changes(change_set(create("Home.md", metadata=sidebar_hint(order=True))))

    assert result.results[0].status is APPLIED
    assert "sidebar." not in messages(result)[0]


# -- SB4: an unmarked sidebar is never touched ------------------------------------------------------------


def test_an_unmarked_sidebar_is_kept_pages_are_committed_and_the_warning_rides_the_first_op(tmp_path: Path) -> None:
    harness = build(tmp_path)
    before = seed(harness, SIDEBAR, UNMARKED)

    result = harness.provider.apply_changes(change_set(create("A.md"), create("B.md"), create("C.md")))

    assert [item.status for item in result.results] == [APPLIED, APPLIED, APPLIED]
    assert sidebar_bytes(harness) == before
    assert harness.fake.commits[0].paths == ("A.md", "B.md", "C.md")
    assert not any(SIDEBAR in argv for argv in staging_argv(harness))
    assert carriers(result, "sidebar.unmanaged_exists") == [0]
    assert "committed locally" in messages(result)[0] and "committed locally" in messages(result)[2]
    assert sidebar_calls(harness) == []


# -- SB2: the sidebar name is reserved when enabled ---------------------------------------------------------


@pytest.mark.parametrize(
    "operation",
    [
        create("_Sidebar.md"),
        create("_sidebar.md"),
        update("_Sidebar.md", "x"),
        create(None, title="_Sidebar"),
        child("_Sidebar"),
        child("Whatever", path="_Sidebar.md"),
    ],
    ids=["create", "create-lowercase", "update", "ref-less-title", "child-derived", "child-explicit"],
)
def test_the_sidebar_name_is_reserved_at_apply_and_other_operations_are_unaffected(
    tmp_path: Path, operation: Any
) -> None:
    harness = build(tmp_path)
    fine = create("Fine.md")

    result = harness.provider.apply_changes(change_set(operation, fine))

    assert [item.status for item in result.results] == [FAILED, APPLIED]
    assert messages(result)[0].startswith("[github_wiki:path.reserved]")
    assert "generate_sidebar" in messages(result)[0]
    # the file is the generated one, never the plugin's content
    assert (harness.workdir / SIDEBAR).read_text().startswith(sidebar.MARKER + "\n")
    assert listed(harness) == ["Fine"]


@pytest.mark.parametrize("entry_point", ["resolve_ref", "exists", "get_document"])
def test_the_sidebar_name_is_reserved_when_planning_too(tmp_path: Path, entry_point: str) -> None:
    from wikiops.providers.github_wiki.errors import GithubWikiError

    harness = build(tmp_path)

    with pytest.raises(GithubWikiError) as caught:
        getattr(harness.provider, entry_point)(ref("_Sidebar.md"))

    assert caught.value.code == "path.reserved" and "generate_sidebar" in caught.value.hint
    assert harness.runner.calls == []  # refused before any command ran


def test_the_footer_is_an_ordinary_page_when_the_sidebar_is_enabled(tmp_path: Path) -> None:
    harness = build(tmp_path)

    result = harness.provider.apply_changes(change_set(create("_Footer.md", "# footer\n"), create("Home.md")))

    assert [item.status for item in result.results] == [APPLIED, APPLIED]
    assert set(harness.fake.commits[0].paths) == {"_Footer.md", "Home.md", SIDEBAR}
    assert listed(harness) == ["Home"]  # underscore pages are never listed


def test_a_sidebar_reference_resolves_normally_when_the_setting_is_off(tmp_path: Path) -> None:
    harness = build(tmp_path, on=False)

    assert harness.provider.resolve_ref(ref("_Sidebar.md")).locator["path"] == SIDEBAR
    assert harness.provider.exists(ref("_Sidebar.md")) is False


# -- SB10: when the sidebar is regenerated and when it is left alone -------------------------------------


def test_an_empty_change_set_does_nothing_for_the_sidebar(tmp_path: Path) -> None:
    harness = build(tmp_path)

    result = harness.provider.apply_changes(change_set())

    assert result.results == [] and harness.runner.calls == []


def test_a_run_where_every_operation_failed_policy_does_not_touch_the_sidebar(tmp_path: Path) -> None:
    harness = build(tmp_path)

    result = harness.provider.apply_changes(change_set(create("x/y.md"), create("_Sidebar.md")))

    assert [item.status for item in result.results] == [FAILED, FAILED]
    assert sidebar_bytes(harness) is None and harness.fake.commits == []
    assert not any("sidebar.write_failed" in text or "sidebar.unmanaged" in text for text in messages(result))


def test_a_run_where_every_operation_failed_leaves_the_sidebar_unwritten_and_silent(tmp_path: Path) -> None:
    def fail_everything(backend: ScriptedBackend) -> None:
        backend.on_apply = lambda changeset, real: ApplyResult(
            provider_name="docs",
            results=[
                AppliedOperationResult(operation_id=item.operation_id, status=FAILED, message="boom")
                for item in real.results
            ],
        )

    harness = build(tmp_path, arm=fail_everything)
    seed(harness, SIDEBAR, UNMARKED)

    result = harness.provider.apply_changes(change_set(create("A.md"), create("B.md")))

    assert [item.status for item in result.results] == [FAILED, FAILED]
    assert all("sidebar" not in text for text in messages(result))
    assert sidebar_calls(harness) == [] and harness.fake.commits == []


def test_an_uploaded_asset_alone_does_not_regenerate_the_sidebar(tmp_path: Path) -> None:
    harness = build(tmp_path)

    harness.provider.put_asset(asset(), b"\x89PNG data")

    assert sidebar_bytes(harness) is None and harness.fake.commits == []


def test_a_skipped_only_run_with_a_changed_hint_still_regenerates_the_sidebar(tmp_path: Path) -> None:
    harness = build(tmp_path)
    seed(harness, "Install.md", "# page\n")
    seed(harness, SIDEBAR, managed_text("Install"))

    result = harness.provider.apply_changes(
        change_set(update("Install.md", "# page\n", metadata=sidebar_hint(group="Ops")))
    )

    assert [item.status for item in result.results] == [SKIPPED]
    assert sidebar.parse((harness.workdir / SIDEBAR).read_text())["Install"] == sidebar.Placement(group="Ops")


def test_the_sidebar_lists_a_page_pulled_in_from_the_web_after_the_sync(tmp_path: Path) -> None:
    harness = build(tmp_path)
    seed(harness, "Web.md", "# edited on the web\n")

    harness.provider.apply_changes(change_set(create("Mine.md")))

    assert listed(harness) == ["Mine", "Web"]


# -- SB11: same commit, exact staging, manifest ------------------------------------------------------------


def test_the_sidebar_is_staged_by_exact_path_in_the_same_single_commit_as_the_pages(tmp_path: Path) -> None:
    harness = build(tmp_path, settings={"commit": {"message": "wiki: {page_count} pages"}})

    result = harness.provider.apply_changes(change_set(create("A.md"), create("B.md")))

    assert [item.status for item in result.results] == [APPLIED, APPLIED]
    assert len(harness.fake.commits) == 1
    assert set(harness.fake.commits[0].paths) == {"A.md", "B.md", SIDEBAR}
    assert harness.fake.commits[0].message == "wiki: 2 pages"  # the sidebar is not a page
    assert any(SIDEBAR in argv for argv in staging_argv(harness))
    assert listed(harness) == ["A", "B"]
    assert manifest_of(harness) == {}
    assert len(sidebar_calls(harness)) == 1


def test_the_sidebar_starts_with_the_marker_and_the_sidebar_op_never_reaches_the_results(tmp_path: Path) -> None:
    harness = build(tmp_path)

    result = harness.provider.apply_changes(change_set(create("Home.md")))

    assert (harness.workdir / SIDEBAR).read_text().startswith(sidebar.MARKER + "\n")
    assert SIDEBAR_OPERATION not in [item.operation_id for item in result.results]
    assert len(result.results) == 1


def test_an_identical_rerun_creates_no_commit(tmp_path: Path) -> None:
    harness = build(tmp_path)
    harness.provider.apply_changes(change_set(create("A.md"), create("B.md")))
    assert len(harness.fake.commits) == 1

    result = harness.provider.apply_changes(change_set(create("A.md"), create("B.md")))

    assert [item.status for item in result.results] == [SKIPPED, SKIPPED]
    assert len(harness.fake.commits) == 1
    assert len(sidebar_calls(harness)) == 1  # the second render was identical: no second write


def test_with_auto_commit_off_the_sidebar_is_written_and_pending_and_a_later_commit_takes_it(tmp_path: Path) -> None:
    harness = build(tmp_path, settings={"allow_auto_commit": False})

    first = harness.provider.apply_changes(change_set(create("A.md"), create("B.md")))

    assert harness.fake.commits == []
    assert set(manifest_of(harness)) == {"A.md", "B.md", SIDEBAR}
    assert listed(harness) == ["A", "B"]
    assert all("not committed (allow_auto_commit=false)" in text for text in messages(first))
    assert all("sidebar" not in text for text in messages(first))

    committing = harness.rebuild(allow_auto_commit=True)
    committing.apply_changes(change_set(create("C.md")))

    assert len(harness.fake.commits) == 1
    assert set(harness.fake.commits[0].paths) == {"A.md", "B.md", "C.md", SIDEBAR}
    assert listed(harness) == ["A", "B", "C"]
    assert manifest_of(harness) == {}


def test_a_hint_only_change_on_a_skipped_page_commits_only_the_sidebar(tmp_path: Path) -> None:
    harness = build(tmp_path, settings={"commit": {"message": "wiki: {page_count} pages"}})
    seed(harness, "Install.md", "# page\n")
    seed(harness, SIDEBAR, managed_text("Install"))

    harness.provider.apply_changes(change_set(update("Install.md", "# page\n", metadata=sidebar_hint(group="Ops"))))

    assert len(harness.fake.commits) == 1
    assert harness.fake.commits[0].paths == (SIDEBAR,)
    assert harness.fake.commits[0].message == "wiki: 0 pages"


# -- SB12: a failing sidebar never fails a page ----------------------------------------------------------


def arm_returns(result: Callable[[], AppliedOperationResult]) -> Callable[[ScriptedBackend], None]:
    return on_sidebar_write(lambda changeset, real: ApplyResult(provider_name="docs", results=[result()]))


def failed_result() -> AppliedOperationResult:
    return AppliedOperationResult(operation_id=SIDEBAR_OPERATION, status=FAILED, message=f"nope {SECRET_URL}")


def wrong_ref_result() -> AppliedOperationResult:
    return AppliedOperationResult(
        operation_id=SIDEBAR_OPERATION, status=APPLIED, message="ok", resolved_ref=ref("Other.md")
    )


BACKEND_FAILURES = {
    "backend-raises": raise_on_sidebar_write(RuntimeError(f"disk on fire ({SECRET_URL})")),
    "failed-result": arm_returns(failed_result),
    "wrong-resolved-ref": arm_returns(wrong_ref_result),
}


@pytest.mark.parametrize("failure", sorted(BACKEND_FAILURES))
@pytest.mark.parametrize("previous", [None, "managed"], ids=["no-previous-sidebar", "previous-managed"])
def test_a_failing_sidebar_leaves_pages_applied_and_committed_and_the_file_as_it_was(
    tmp_path: Path, failure: str, previous: str | None
) -> None:
    harness = build(tmp_path, arm=BACKEND_FAILURES[failure])
    before = None if previous is None else seed(harness, SIDEBAR, managed_text("Old"))

    result = harness.provider.apply_changes(change_set(create("A.md"), create("B.md")))

    assert [item.status for item in result.results] == [APPLIED, APPLIED]
    assert harness.fake.commits[0].paths == ("A.md", "B.md")
    assert not any(SIDEBAR in argv for argv in staging_argv(harness))
    assert sidebar_bytes(harness) == before
    assert SIDEBAR not in manifest_of(harness)
    assert carriers(result, "sidebar.write_failed") == [0]
    assert "ghp_SuperSecretToken123" not in " ".join(messages(result))
    assert (harness.workdir / "A.md").read_text() == "# page\n" and (harness.workdir / "B.md").is_file()


def test_a_manifest_failure_on_the_sidebar_keeps_the_pages_and_restores_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = build(tmp_path)
    before = seed(harness, SIDEBAR, managed_text("Old"))
    real_record = PendingManifest.record

    def record(self: PendingManifest, paths: Any) -> None:
        if list(paths) == [SIDEBAR]:
            raise OSError("read-only file system")
        real_record(self, paths)

    monkeypatch.setattr(PendingManifest, "record", record)

    result = harness.provider.apply_changes(change_set(create("A.md")))

    assert [item.status for item in result.results] == [APPLIED]
    assert sidebar_bytes(harness) == before
    assert harness.fake.commits[0].paths == ("A.md",)
    assert carriers(result, "sidebar.write_failed") == [0]
    assert SIDEBAR not in manifest_of(harness)


def test_the_rollback_of_a_failed_sidebar_never_touches_the_pages_or_assets_of_the_apply(tmp_path: Path) -> None:
    harness = build(tmp_path, arm=BACKEND_FAILURES["failed-result"])
    stored = harness.provider.put_asset(asset(), b"\x89PNG data")

    result = harness.provider.apply_changes(change_set(create("A.md")))

    assert [item.status for item in result.results] == [APPLIED]
    assert set(harness.fake.commits[0].paths) == {"A.md", stored.ref.locator["path"]}
    assert (harness.workdir / stored.ref.locator["path"]).read_bytes() == b"\x89PNG data"


# -- SB12: a gitignored sidebar -------------------------------------------------------------------------


def test_a_sidebar_git_ignores_is_reported_without_failing_a_page_or_staging_it(tmp_path: Path) -> None:
    harness = build(tmp_path)
    harness.fake.ignored.add(SIDEBAR)

    result = harness.provider.apply_changes(change_set(create("A.md"), create("B.md")))

    assert [item.status for item in result.results] == [APPLIED, APPLIED]
    assert harness.fake.commits[0].paths == ("A.md", "B.md")
    assert carriers(result, "sidebar.write_failed") == [0]
    assert "ignored by git" in messages(result)[0]
    assert sidebar_bytes(harness) is None and sidebar_calls(harness) == []
    assert not any(SIDEBAR in argv for argv in staging_argv(harness))


def test_an_ignore_rule_that_appears_only_at_publish_restores_the_sidebar_and_reports_it(tmp_path: Path) -> None:
    def ignore_after_write(changeset: ChangeSet, real: ApplyResult) -> ApplyResult:
        harness.fake.ignored.add(SIDEBAR)  # the rule lands between the pre-check and the publish
        return real

    harness = build(tmp_path, arm=on_sidebar_write(ignore_after_write))

    result = harness.provider.apply_changes(change_set(create("A.md"), create("B.md")))

    assert [item.status for item in result.results] == [APPLIED, APPLIED]
    assert harness.fake.commits[0].paths == ("A.md", "B.md")
    assert carriers(result, "sidebar.write_failed") == [0]
    assert "ignored by git" in messages(result)[0]
    assert sidebar_bytes(harness) is None  # it did not exist before: it is removed again
    assert SIDEBAR not in manifest_of(harness)
    assert not any(SIDEBAR in argv for argv in staging_argv(harness))


def test_a_late_ignore_rule_restores_the_previous_managed_bytes(tmp_path: Path) -> None:
    def ignore_after_write(changeset: ChangeSet, real: ApplyResult) -> ApplyResult:
        harness.fake.ignored.add(SIDEBAR)
        return real

    harness = build(tmp_path, arm=on_sidebar_write(ignore_after_write))
    before = seed(harness, SIDEBAR, managed_text("Old"), committed=False)
    PendingManifest(manifest_path(harness.fake.git_dir), workdir=harness.workdir).record([SIDEBAR])

    result = harness.provider.apply_changes(change_set(create("A.md")))

    assert [item.status for item in result.results] == [APPLIED]
    assert sidebar_bytes(harness) == before


# -- SB13: warning delivery -------------------------------------------------------------------------------


def test_the_first_op_failing_moves_the_warning_to_the_second(tmp_path: Path) -> None:
    harness = build(tmp_path)
    seed(harness, SIDEBAR, UNMARKED)

    result = harness.provider.apply_changes(change_set(create("x/y.md"), create("B.md"), create("C.md")))

    assert [item.status for item in result.results] == [FAILED, APPLIED, APPLIED]
    assert carriers(result, "sidebar.unmanaged_exists") == [1]


def test_two_invalid_hints_and_an_ignored_sidebar_ride_the_first_op_in_order(tmp_path: Path) -> None:
    harness = build(tmp_path)
    harness.fake.ignored.add(SIDEBAR)

    result = harness.provider.apply_changes(
        change_set(
            create("A.md", metadata=sidebar_hint(order="ten")),
            create("B.md", metadata=sidebar_hint(weight=3)),
            create("C.md"),
        )
    )

    text = messages(result)[0]
    assert text.count("sidebar.invalid_hint") == 2 and text.count("sidebar.write_failed") == 1
    assert text.index("'order'") < text.index("'weight'") < text.index("sidebar.write_failed")
    assert carriers(result, "sidebar.") == [0]


def test_the_commit_note_stays_and_the_warnings_follow_it(tmp_path: Path) -> None:
    harness = build(tmp_path)
    seed(harness, SIDEBAR, UNMARKED)

    result = harness.provider.apply_changes(change_set(create("A.md")))

    text = messages(result)[0]
    assert "committed locally at" in text and "; not pushed" in text
    assert text.index("not pushed") < text.index("sidebar.unmanaged_exists")


def test_a_commit_that_fails_every_page_still_delivers_the_warning_on_the_first_op(tmp_path: Path) -> None:
    harness = build(tmp_path)
    seed(harness, SIDEBAR, UNMARKED)
    harness.fake.fail["commit"] = (1, "fatal: cannot commit")

    result = harness.provider.apply_changes(change_set(create("A.md"), create("B.md")))

    assert [item.status for item in result.results] == [FAILED, FAILED]
    assert "commit.failed" in messages(result)[0]
    assert carriers(result, "sidebar.unmanaged_exists") == [0]


def test_a_secret_in_a_sidebar_failure_is_redacted_in_the_delivered_warning(tmp_path: Path) -> None:
    harness = build(tmp_path, arm=raise_on_sidebar_write(RuntimeError(f"connection lost ({SECRET_URL})")))

    result = harness.provider.apply_changes(change_set(create("A.md")))

    text = messages(result)[0]
    assert "sidebar.write_failed" in text and "ghp_SuperSecretToken123" not in text and "\n" not in text


def test_a_hint_on_the_footer_is_reported_and_the_footer_is_not_listed(tmp_path: Path) -> None:
    harness = build(tmp_path)

    result = harness.provider.apply_changes(
        change_set(create("_Footer.md", metadata=sidebar_hint(group="G")), create("Home.md"))
    )

    assert [item.status for item in result.results] == [APPLIED, APPLIED]
    assert carriers(result, "sidebar.invalid_hint") == [0]
    assert listed(harness) == ["Home"] and "**G**" not in (harness.workdir / SIDEBAR).read_text()


def test_hints_become_groups_and_a_child_create_hint_comes_from_child_metadata(tmp_path: Path) -> None:
    harness = build(tmp_path)

    harness.provider.apply_changes(
        change_set(
            create("Install.md", metadata=sidebar_hint(group="Guides", order=10, label="Install guide")),
            create("Upgrade.md", metadata=sidebar_hint(group="Guides", order=20)),
            create("Overview.md"),
            child("Ref", path="Ref.md", metadata=sidebar_hint(group="Ref", order=1)),
        )
    )

    parsed = sidebar.parse((harness.workdir / SIDEBAR).read_text())
    assert parsed["Install"] == sidebar.Placement(group="Guides", order=10, label="Install guide")
    assert parsed["Ref"] == sidebar.Placement(group="Ref", order=1)
    assert parsed["Overview"] == sidebar.Placement()
    assert listed(harness) == ["Overview", "Ref", "Install", "Upgrade"]  # groups rank by their lowest order


def test_placement_is_sticky_across_applies_of_one_provider_and_an_invalid_hint_keeps_it(tmp_path: Path) -> None:
    harness = build(tmp_path)
    harness.provider.apply_changes(
        change_set(create("Install.md", metadata=sidebar_hint(group="Guides", order=10)), create("Other.md"))
    )

    second = harness.provider.apply_changes(change_set(create("Third.md")))
    third = harness.provider.apply_changes(
        change_set(update("Install.md", "# page\n", metadata=sidebar_hint(order="ten")))
    )

    parsed = sidebar.parse((harness.workdir / SIDEBAR).read_text())
    assert parsed["Install"] == sidebar.Placement(group="Guides", order=10)
    assert [item.status for item in second.results] == [APPLIED]
    assert carriers(third, "sidebar.invalid_hint") == [0]
    assert listed(harness) == ["Other", "Third", "Install"]
