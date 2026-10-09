"""Unit tests for ``SidebarWriter`` of ``github_wiki`` (``sidebar_io.py``): SB4, SB8, SB10-SB13.

The writer reads ``_Sidebar.md`` through the backend, decides (unmarked: leave alone; ignored by
git: report), renders, writes through the backend and records the file in the pending manifest
-- and it NEVER raises: every failure becomes a ``sidebar.write_failed`` warning after the file
is put back byte for byte. It runs over a real ``tmp_path`` clone, the independent
``FakeFileBackend`` (wrapped so a test can replace what it reports), ``FakeWikiGit`` and a real
``PendingManifest``; no process, no network, and no ``apply_changes`` of the provider.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from wikiops_sdk.domain import (
    AppliedOperationResult,
    ApplyResult,
    ChangeSet,
    CreateDocumentOperation,
    OperationStatus,
    UpdateDocumentOperation,
)

from tests.support.sidebar_harness import SidebarHarness, build_sidebar
from tests.support.write_ops import ref
from wikiops.providers.github_wiki import rollback, sidebar, sidebar_io
from wikiops.providers.github_wiki.errors import GithubWikiError
from wikiops.providers.github_wiki.layout import SIDEBAR_PAGE
from wikiops.providers.github_wiki.sidebar import MARKER, HintSource, Placement

OPERATION_ID = "wikiops-sidebar"
APPLIED, FAILED, SKIPPED = OperationStatus.APPLIED, OperationStatus.FAILED, OperationStatus.SKIPPED
UNMANAGED = "# my own sidebar\n- [Home](Home)\n"
SECRET_URL = "https://x-access-token:ghp_SuperSecretToken123@github.com/acme/wiki.git"


def managed(*stems: str, placements: dict[str, Placement] | None = None, crlf: bool = False) -> str:
    """A managed sidebar text as a previous apply would have left it."""
    text = sidebar.render(stems, placements or {})
    return text.replace("\n", "\r\n") if crlf else text


def hint(page: str, value: Any, *, operation: str = "op-1") -> HintSource:
    return HintSource(operation_id=operation, page=f"{page}.md", metadata={"github_wiki": {"sidebar": value}})


def reply(*results: AppliedOperationResult) -> ApplyResult:
    return ApplyResult(provider_name="docs", results=list(results))


def outcome(status: OperationStatus, path: str | None = SIDEBAR_PAGE, message: str = "") -> AppliedOperationResult:
    return AppliedOperationResult(
        operation_id=OPERATION_ID,
        status=status,
        message=message,
        resolved_ref=None if path is None else ref(path),
    )


def scene(tmp_path: Path, *pages: str, **model: Any) -> SidebarHarness:
    harness = build_sidebar(tmp_path, **model)
    harness.pages(*pages)
    return harness


def sha(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def assert_failed(report: sidebar_io.SidebarReport, *fragments: str) -> str:
    """The report says write_failed on one line, names every fragment and generated nothing."""
    assert report.generated == ()
    status = report.status_warning
    assert status is not None and status.startswith("[github_wiki:sidebar.write_failed] ")
    assert "\n" not in status and "Hint: " in status
    for fragment in fragments:
        assert fragment in status
    return status


# -- early branches: nothing is written ------------------------------------------------------------------


def test_an_unmarked_sidebar_is_never_overwritten_parsed_or_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = scene(tmp_path, "Home", "Guide")
    original = harness.seed(UNMANAGED)

    def forbidden(text: str) -> Any:
        raise AssertionError("an unmarked sidebar must never be parsed")

    monkeypatch.setattr(sidebar_io.sidebar, "parse", forbidden)

    report = harness.writer().regenerate([])

    assert report.generated == () and report.hint_warnings == ()
    assert report.status_warning is not None
    assert report.status_warning.startswith("[github_wiki:sidebar.unmanaged_exists] ")
    assert "first line" in report.status_warning and "generate_sidebar: false" in report.status_warning
    assert harness.backend.applied == []
    assert harness.sidebar() == original
    assert harness.manifest.entries() == {}


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(" " + MARKER + "\n", id="leading-space"),
        pytest.param(MARKER + " extra\n", id="trailing-text"),
        pytest.param("", id="empty"),
    ],
)
def test_a_near_miss_marker_is_unmarked_too(tmp_path: Path, text: str) -> None:
    harness = scene(tmp_path, "Home")
    original = harness.seed(text)

    report = harness.writer().regenerate([])

    assert report.status_warning is not None and "sidebar.unmanaged_exists" in report.status_warning
    assert harness.backend.applied == [] and harness.sidebar() == original


def test_an_unmarked_sidebar_wins_over_the_ignore_check_and_nothing_asks_git(tmp_path: Path) -> None:
    harness = scene(tmp_path, "Home", ignored={SIDEBAR_PAGE})
    harness.seed(UNMANAGED)

    report = harness.writer().regenerate([])

    assert report.status_warning is not None and "sidebar.unmanaged_exists" in report.status_warning
    assert harness.check_ignore_calls() == []


def test_a_sidebar_git_ignores_is_reported_and_nothing_is_written(tmp_path: Path) -> None:
    harness = scene(tmp_path, "Home", ignored={SIDEBAR_PAGE})

    report = harness.writer().regenerate([])

    assert_failed(report, "ignored by git", SIDEBAR_PAGE)
    assert harness.backend.applied == []
    assert harness.sidebar() is None
    assert harness.manifest.entries() == {}


def test_an_ignored_managed_sidebar_that_exists_is_not_rewritten(tmp_path: Path) -> None:
    harness = scene(tmp_path, "Home", ignored={SIDEBAR_PAGE})
    original = harness.seed(managed("Old"))

    report = harness.writer().regenerate([])

    assert_failed(report, "ignored by git")
    assert harness.backend.applied == [] and harness.sidebar() == original


def test_a_tracked_sidebar_matching_an_ignore_rule_is_an_ordinary_file_and_is_regenerated(tmp_path: Path) -> None:
    harness = scene(tmp_path, "Home", ignored={SIDEBAR_PAGE})
    harness.seed(managed("Old"), committed=True)

    report = harness.writer().regenerate([])

    assert report.status_warning is None and report.generated == (SIDEBAR_PAGE,)
    assert harness.sidebar() == managed("Home").encode()


@pytest.mark.parametrize("failing", ["exists", "get_document"])
def test_a_read_failure_of_the_existing_sidebar_is_a_write_failure_and_writes_nothing(
    tmp_path: Path, failing: str
) -> None:
    harness = scene(tmp_path, "Home")
    original = harness.seed(managed("Old"))

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise OSError(f"cannot read {SECRET_URL}")

    setattr(harness.backend, failing, refuse)

    report = harness.writer().regenerate([])

    status = assert_failed(report, "OSError")
    assert "ghp_SuperSecretToken123" not in status
    assert harness.backend.applied == [] and harness.sidebar() == original


def test_an_undecodable_existing_sidebar_is_a_write_failure_and_stays_as_it_is(tmp_path: Path) -> None:
    harness = scene(tmp_path, "Home")
    original = harness.seed(MARKER.encode() + b"\n\xff\xfe broken\n")

    report = harness.writer().regenerate([])

    assert_failed(report)
    assert harness.backend.applied == [] and harness.sidebar() == original


def test_a_directory_in_place_of_the_sidebar_is_a_write_failure_and_nothing_is_removed(tmp_path: Path) -> None:
    harness = scene(tmp_path, "Home")
    (harness.workdir / SIDEBAR_PAGE).mkdir()
    (harness.workdir / SIDEBAR_PAGE / "inner.md").write_bytes(b"inside\n")

    report = harness.writer().regenerate([])

    assert_failed(report, "not a regular file")
    assert harness.backend.applied == []
    assert (harness.workdir / SIDEBAR_PAGE / "inner.md").read_bytes() == b"inside\n"


def test_a_symlink_in_place_of_the_sidebar_is_never_followed(tmp_path: Path) -> None:
    harness = scene(tmp_path, "Home")
    outside = tmp_path / "outside.md"
    outside.write_bytes((managed("Old")).encode())
    (harness.workdir / SIDEBAR_PAGE).symlink_to(outside)

    report = harness.writer().regenerate([])

    assert_failed(report, "not a regular file")
    assert harness.backend.applied == []
    assert (harness.workdir / SIDEBAR_PAGE).is_symlink()
    assert outside.read_bytes() == managed("Old").encode()


def test_invalid_hints_are_reported_even_when_the_sidebar_is_unmarked_ignored_or_unreadable(
    tmp_path: Path,
) -> None:
    sources = [hint("Install", {"order": "ten"}, operation="op-7")]
    unmarked = scene(tmp_path / "unmarked", "Home")
    unmarked.seed(UNMANAGED)
    ignored = scene(tmp_path / "ignored", "Home", ignored={SIDEBAR_PAGE})
    unreadable = scene(tmp_path / "unreadable", "Home")
    unreadable.seed(managed("Old"))
    unreadable.backend.exists = lambda *a, **k: (_ for _ in ()).throw(OSError("boom"))

    reports = [h.writer().regenerate(sources) for h in (unmarked, ignored, unreadable)]

    for report in reports:
        assert len(report.hint_warnings) == 1
        assert "[github_wiki:sidebar.invalid_hint]" in report.hint_warnings[0]
        assert "op-7" in report.hint_warnings[0] and "order" in report.hint_warnings[0]
    assert [r.status_warning.split("]")[0] if r.status_warning else None for r in reports] == [
        "[github_wiki:sidebar.unmanaged_exists",
        "[github_wiki:sidebar.write_failed",
        "[github_wiki:sidebar.write_failed",
    ]


def test_a_git_failure_while_listing_the_pages_is_a_write_failure(tmp_path: Path) -> None:
    harness = scene(tmp_path, "Home")
    harness.fake.fail["check-ignore"] = (128, "fatal: unable to access the index")

    report = harness.writer().regenerate([])

    assert_failed(report)
    assert harness.backend.applied == [] and harness.sidebar() is None


def test_a_link_guard_violation_is_a_write_failure_and_nothing_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = scene(tmp_path, "Home")

    def violated(link: str) -> str:
        raise GithubWikiError("link.root_anchored", "The link is root-anchored", context={"link": link})

    monkeypatch.setattr(sidebar, "guard_link", violated)

    report = harness.writer().regenerate([])

    assert_failed(report, "link.root_anchored")
    assert harness.backend.applied == [] and harness.sidebar() is None


# -- the write path --------------------------------------------------------------------------------------


def test_an_absent_sidebar_is_created_through_one_separate_backend_call(tmp_path: Path) -> None:
    harness = scene(tmp_path, "Home", "Guide", "Zed")

    report = harness.writer().regenerate([])

    assert report == sidebar_io.SidebarReport(generated=(SIDEBAR_PAGE,), hint_warnings=(), status_warning=None)
    expected = sidebar.render(["Home", "Guide", "Zed"], {})
    assert harness.sidebar() == expected.encode()
    [call] = harness.backend.applied
    [operation] = call.operations
    assert isinstance(operation, CreateDocumentOperation)
    assert operation.operation_id == OPERATION_ID and operation.title == "_Sidebar"
    assert operation.ref is not None and operation.ref.locator == {"path": SIDEBAR_PAGE}
    assert operation.content == expected
    assert call.plugin_id == harness.changeset.plugin_id
    assert len(harness.changeset.operations) == 1  # the surrounding change set is never altered
    assert harness.changeset.operations[0].operation_id != OPERATION_ID


def test_an_existing_managed_sidebar_is_updated_with_its_version_as_the_precondition(tmp_path: Path) -> None:
    harness = scene(tmp_path, "Home", "Guide")
    before = harness.seed(managed("Old"))

    report = harness.writer().regenerate([])

    assert report.generated == (SIDEBAR_PAGE,) and report.status_warning is None
    [call] = harness.backend.applied
    [operation] = call.operations
    assert isinstance(operation, UpdateDocumentOperation)
    assert operation.operation_id == OPERATION_ID
    assert operation.expected_version is not None and operation.expected_version.token == sha(before)
    assert operation.new_content == sidebar.render(["Home", "Guide"], {})
    assert harness.sidebar() == operation.new_content.encode()


def test_regenerated_bytes_equal_to_the_existing_file_write_and_record_nothing(tmp_path: Path) -> None:
    harness = scene(tmp_path, "Home", "Guide")
    original = harness.seed(managed("Home", "Guide"), committed=True)

    report = harness.writer().regenerate([])

    assert report == sidebar_io.SidebarReport(generated=(), hint_warnings=(), status_warning=None)
    assert harness.backend.applied == []
    assert harness.sidebar() == original
    assert harness.manifest.entries() == {}


def test_the_pages_of_the_whole_clone_are_listed_not_only_the_change_sets(tmp_path: Path) -> None:
    harness = scene(tmp_path, "Home", "Guide", "Zed", "Alpha")
    (harness.workdir / "_Footer.md").write_bytes(b"footer\n")
    (harness.workdir / "notes.txt").write_bytes(b"notes\n")

    harness.writer().regenerate([])

    assert harness.sidebar() == sidebar.render(["Home", "Alpha", "Guide", "Zed"], {}).encode()


def test_a_page_deleted_in_the_clone_is_dropped_from_the_regenerated_sidebar(tmp_path: Path) -> None:
    harness = scene(tmp_path, "Home")
    harness.seed(managed("Home", "Old"))

    harness.writer().regenerate([])

    assert harness.sidebar() == managed("Home").encode()


def test_the_manifest_records_the_sidebar_last_after_the_backend_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = scene(tmp_path, "Home")
    events: list[str] = []
    harness.backend.before = lambda: events.append("write")
    real_record = harness.manifest.record

    def spy(paths: Any) -> None:
        paths = list(paths)
        events.append(f"record:{paths}")
        real_record(paths)

    monkeypatch.setattr(harness.manifest, "record", spy)

    harness.writer().regenerate([])

    assert events == ["write", f"record:{[SIDEBAR_PAGE]}"]
    assert harness.manifest.classify([SIDEBAR_PAGE]).pending == (SIDEBAR_PAGE,)


def test_a_sidebar_already_pending_from_an_earlier_run_is_updated_and_stays_pending(tmp_path: Path) -> None:
    harness = scene(tmp_path, "Home")
    harness.seed(managed("Old"), pending=True)

    report = harness.writer().regenerate([])

    assert report.generated == (SIDEBAR_PAGE,)
    assert harness.manifest.classify([SIDEBAR_PAGE]).pending == (SIDEBAR_PAGE,)
    assert harness.manifest.classify([SIDEBAR_PAGE]).foreign == ()


def test_a_skipped_sidebar_write_generates_nothing_and_records_nothing(tmp_path: Path) -> None:
    harness = scene(tmp_path, "Home")
    harness.backend.on_apply = lambda changeset, real: reply(outcome(SKIPPED))

    report = harness.writer().regenerate([])

    assert report == sidebar_io.SidebarReport(generated=(), hint_warnings=(), status_warning=None)
    assert harness.manifest.entries() == {}


# -- sticky placement (SB8) ------------------------------------------------------------------------------

INSTALL = Placement(group="Guides", order=10, label="Install guide")


@pytest.mark.parametrize(
    ("sources", "expected"),
    [
        pytest.param([], {"Install": INSTALL, "Other": Placement()}, id="untouched-page-keeps-placement"),
        pytest.param(
            [hint("Other", {"group": "Ops"})],
            {"Install": INSTALL, "Other": Placement(group="Ops")},
            id="other-page-hint-leaves-install",
        ),
        pytest.param(
            [hint("Install", {})],
            {"Install": INSTALL, "Other": Placement()},
            id="hintless-update-keeps-placement",
        ),
        pytest.param(
            [hint("Install", {"order": 5})],
            {"Install": Placement(group="Guides", order=5, label="Install guide"), "Other": Placement()},
            id="partial-hint-overrides-one-key",
        ),
        pytest.param(
            [hint("Install", {"group": None})],
            {"Install": Placement(order=10, label="Install guide"), "Other": Placement()},
            id="null-group-clears-only-the-group",
        ),
        pytest.param(
            [hint("Install", {"label": None})],
            {"Install": Placement(group="Guides", order=10), "Other": Placement()},
            id="null-label-reverts-the-label",
        ),
        pytest.param(
            [hint("Install", None)],
            {"Install": Placement(), "Other": Placement()},
            id="null-sidebar-clears-all-three-keys",
        ),
        pytest.param(
            [hint("Install", {"order": "ten"})],
            {"Install": INSTALL, "Other": Placement()},
            id="invalid-hint-keeps-placement",
        ),
        pytest.param(
            [hint("Install", {"order": 1}), hint("Install", {"order": 2}, operation="op-2")],
            {"Install": Placement(group="Guides", order=2, label="Install guide"), "Other": Placement()},
            id="later-op-wins-on-one-page",
        ),
    ],
)
def test_placements_are_recovered_from_the_previous_sidebar_and_merged_with_the_hints(
    tmp_path: Path, sources: list[HintSource], expected: dict[str, Placement]
) -> None:
    harness = scene(tmp_path, "Install", "Other")
    harness.seed(managed("Install", "Other", placements={"Install": INSTALL}))

    harness.writer().regenerate(sources)

    assert sidebar.parse(harness.sidebar().decode()) == expected  # type: ignore[union-attr]


def test_a_new_page_without_a_hint_gets_default_placement(tmp_path: Path) -> None:
    harness = scene(tmp_path, "Install", "Fresh")
    harness.seed(managed("Install", placements={"Install": INSTALL}))

    harness.writer().regenerate([])

    assert sidebar.parse(harness.sidebar().decode()) == {"Install": INSTALL, "Fresh": Placement()}  # type: ignore[union-attr]


def test_a_hand_edited_or_oversized_managed_sidebar_falls_back_to_defaults_and_is_regenerated(
    tmp_path: Path,
) -> None:
    edited = scene(tmp_path / "edited", "Home", "Guide")
    edited.seed(MARKER + "\n\n* Home, then some prose that is not an entry\n")
    oversized = scene(tmp_path / "oversized", "Home", "Guide")
    oversized.seed(MARKER + "\n" + "x" * (sidebar.MAX_PARSE_BYTES + 1))

    reports = [h.writer().regenerate([hint("Guide", {"order": 3})]) for h in (edited, oversized)]

    for harness, report in zip((edited, oversized), reports, strict=True):
        assert report.generated == (SIDEBAR_PAGE,) and report.status_warning is None
        assert sidebar.parse(harness.sidebar().decode()) == {  # type: ignore[union-attr]
            "Home": Placement(),
            "Guide": Placement(order=3),
        }


def test_invalid_hints_are_returned_in_operation_order_while_the_sidebar_is_still_written(tmp_path: Path) -> None:
    harness = scene(tmp_path, "Home", "Guide")

    report = harness.writer().regenerate(
        [
            hint("Home", {"weight": 3}, operation="op-1"),
            hint("Guide", {"group": "x" * 81}, operation="op-2"),
            hint("Guide", {"order": 4}, operation="op-3"),
        ]
    )

    assert report.generated == (SIDEBAR_PAGE,) and report.status_warning is None
    assert len(report.hint_warnings) == 2
    assert "op-1" in report.hint_warnings[0] and "weight" in report.hint_warnings[0]
    assert "op-2" in report.hint_warnings[1] and "group" in report.hint_warnings[1]
    assert sidebar.parse(harness.sidebar().decode())["Guide"] == Placement(order=4)  # type: ignore[union-attr]


# -- failure and byte-level rollback (SB12) --------------------------------------------------------------

PREVIOUS_LF = managed("Old")
PREVIOUS_CRLF = managed("Old", crlf=True)


def after_real_write(change: Callable[[ChangeSet, ApplyResult], ApplyResult]) -> Callable[[SidebarHarness, Any], None]:
    def arm(harness: SidebarHarness, monkeypatch: pytest.MonkeyPatch) -> None:
        harness.backend.on_apply = change

    return arm


def arm_raises_before_writing(harness: SidebarHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    harness.backend.apply_error = RuntimeError(f"disk on fire ({SECRET_URL})")


def arm_record_fails(harness: SidebarHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(paths: Any) -> None:
        raise GithubWikiError("workdir.unusable", "The manifest cannot be saved", context={"path": SIDEBAR_PAGE})

    monkeypatch.setattr(harness.manifest, "record", refuse)


def raise_after_writing(changeset: ChangeSet, real: ApplyResult) -> ApplyResult:
    raise RuntimeError(f"connection lost ({SECRET_URL})")


FAILURES = {
    "backend-raises-before-writing": arm_raises_before_writing,
    "backend-raises-after-writing": after_real_write(raise_after_writing),
    "failed-result": after_real_write(lambda cs, real: reply(outcome(FAILED, message="boom"))),
    "result-missing": after_real_write(lambda cs, real: reply()),
    "result-for-another-operation": after_real_write(
        lambda cs, real: reply(real.results[0].model_copy(update={"operation_id": "someone-else"}))
    ),
    "wrong-resolved-ref": after_real_write(lambda cs, real: reply(outcome(APPLIED, path="Other.md"))),
    "no-resolved-ref": after_real_write(lambda cs, real: reply(outcome(APPLIED, path=None))),
    "unusable-resolved-ref": after_real_write(lambda cs, real: reply(outcome(APPLIED, path="../_Sidebar.md"))),
    "manifest-record-fails": arm_record_fails,
}


@pytest.mark.parametrize(
    "previous",
    [
        pytest.param(None, id="no-previous-sidebar"),
        pytest.param(PREVIOUS_LF, id="previous-lf"),
        pytest.param(PREVIOUS_CRLF, id="previous-crlf"),
    ],
)
@pytest.mark.parametrize("failure", sorted(FAILURES))
def test_any_failure_restores_the_previous_bytes_and_says_write_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str, previous: str | None
) -> None:
    harness = scene(tmp_path, "Home", "Guide")
    (harness.workdir / "assets").mkdir()
    (harness.workdir / "assets" / "logo.png").write_bytes(b"\x89PNG bytes")
    before = None if previous is None else harness.seed(previous, committed=True)
    FAILURES[failure](harness, monkeypatch)

    report = harness.writer().regenerate([hint("Guide", {"order": "ten"})])

    status = assert_failed(report)
    assert "ghp_SuperSecretToken123" not in status
    assert harness.sidebar() == before
    assert harness.manifest.entries() == {}
    assert len(report.hint_warnings) == 1
    assert (harness.workdir / "Home.md").read_bytes() == b"# Home\n"
    assert (harness.workdir / "Guide.md").read_bytes() == b"# Guide\n"
    assert (harness.workdir / "assets" / "logo.png").read_bytes() == b"\x89PNG bytes"


def normalizing_backend(harness: SidebarHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    """A backend whose documents come back with LF line ends, whatever the file holds."""
    real_get = harness.backend.get_document

    def get_document(ref: Any) -> Any:
        document = real_get(ref)
        return document.model_copy(update={"content": document.content.replace("\r\n", "\n")})

    monkeypatch.setattr(harness.backend, "get_document", get_document)


@pytest.mark.parametrize("failure", ["backend-raises-after-writing", "failed-result", "manifest-record-fails"])
def test_the_restore_snapshot_is_the_raw_bytes_on_disk_not_the_backends_decoded_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    harness = scene(tmp_path, "Home")
    before = harness.seed(PREVIOUS_CRLF, committed=True)
    normalizing_backend(harness, monkeypatch)
    FAILURES[failure](harness, monkeypatch)

    report = harness.writer().regenerate([])

    assert_failed(report)
    assert harness.sidebar() == before
    assert b"\r\n" in before


def test_the_post_publish_restore_also_returns_the_raw_bytes_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = scene(tmp_path, "Home")
    before = harness.seed(PREVIOUS_CRLF, committed=True)
    normalizing_backend(harness, monkeypatch)
    writer = harness.writer()
    assert writer.regenerate([]).generated == (SIDEBAR_PAGE,)

    writer.restore_after_publish()

    assert harness.sidebar() == before


def test_a_sidebar_the_backend_reports_but_the_disk_cannot_give_is_a_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = scene(tmp_path, "Home")
    harness.seed(PREVIOUS_LF, committed=True)
    monkeypatch.setattr("wikiops.providers._fs.resolve_within_root", lambda root, path: root / "missing.md")

    report = harness.writer().regenerate([])

    assert_failed(report)
    assert harness.backend.applied == []


def spy_on_restore(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    real_restore = rollback.restore_file

    def spy(workdir: Path, path: str, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return real_restore(workdir, path, **kwargs)

    monkeypatch.setattr(rollback, "restore_file", spy)
    return calls


@pytest.mark.parametrize("failure", sorted(FAILURES))
def test_a_failed_write_cannot_be_restored_a_second_time_after_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    harness = scene(tmp_path, "Home")
    before = harness.seed(PREVIOUS_LF, pending=True)
    FAILURES[failure](harness, monkeypatch)
    writer = harness.writer()
    assert_failed(writer.regenerate([]))
    calls = spy_on_restore(monkeypatch)
    touched: list[str] = []
    monkeypatch.setattr(harness.manifest, "record", lambda paths: touched.append("record"))
    monkeypatch.setattr(harness.manifest, "discard", lambda paths: touched.append("discard"))

    text = writer.restore_after_publish()

    assert "sidebar.write_failed" in text and "ignored by git" in text
    assert calls == [] and touched == []
    assert harness.sidebar() == before


def test_a_skipped_write_cannot_be_restored_after_publish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    harness = scene(tmp_path, "Home")
    harness.seed(PREVIOUS_LF, committed=True)
    harness.backend.on_apply = lambda changeset, real: reply(outcome(SKIPPED))
    writer = harness.writer()
    assert writer.regenerate([]).generated == ()
    written = harness.sidebar()
    calls = spy_on_restore(monkeypatch)
    touched: list[str] = []
    monkeypatch.setattr(harness.manifest, "discard", lambda paths: touched.append("discard"))

    writer.restore_after_publish()

    assert calls == [] and touched == []
    assert harness.sidebar() == written


def test_a_writer_that_regenerates_again_forgets_the_previous_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = scene(tmp_path, "Home")
    writer = harness.writer()
    assert writer.regenerate([]).generated == (SIDEBAR_PAGE,)
    assert writer.regenerate([]).generated == ()  # identical bytes: nothing written this time
    calls = spy_on_restore(monkeypatch)

    writer.restore_after_publish()

    assert calls == []


def test_a_failed_regeneration_leaves_a_pending_previous_sidebar_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = scene(tmp_path, "Home")
    before = harness.seed(PREVIOUS_LF, pending=True)
    arm_raises_before_writing(harness, monkeypatch)

    report = harness.writer().regenerate([])

    assert_failed(report)
    assert harness.sidebar() == before
    assert harness.manifest.classify([SIDEBAR_PAGE]).pending == (SIDEBAR_PAGE,)


def test_the_warning_carries_the_redacted_reason_of_the_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    harness = scene(tmp_path, "Home")
    arm_raises_before_writing(harness, monkeypatch)

    status = assert_failed(harness.writer().regenerate([]), "RuntimeError", "disk on fire")

    assert "***@github.com" in status


def test_a_failed_result_names_what_the_backend_said_redacted(tmp_path: Path) -> None:
    harness = scene(tmp_path, "Home")
    harness.backend.on_apply = lambda cs, real: reply(outcome(FAILED, message=f"could not reach {SECRET_URL}"))

    status = assert_failed(harness.writer().regenerate([]), "could not reach")

    assert "ghp_SuperSecretToken123" not in status


def test_a_foreign_edit_during_the_failed_write_is_left_untouched_and_named(tmp_path: Path) -> None:
    harness = scene(tmp_path, "Home")
    before = harness.seed(PREVIOUS_LF, committed=True)

    def user_edits_meanwhile(changeset: ChangeSet, real: ApplyResult) -> ApplyResult:
        (harness.workdir / SIDEBAR_PAGE).write_bytes(b"the user edited this\n")
        return reply(outcome(FAILED, message="boom"))

    harness.backend.on_apply = user_edits_meanwhile

    report = harness.writer().regenerate([])

    assert_failed(report, "left untouched", f"'{SIDEBAR_PAGE}'")
    assert harness.sidebar() == b"the user edited this\n" != before


def test_a_restore_that_fails_names_what_the_clone_still_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = scene(tmp_path, "Home")
    harness.seed(PREVIOUS_LF, committed=True)
    harness.backend.on_apply = lambda cs, real: reply(outcome(FAILED, message="boom"))

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr("wikiops.providers._fs.atomic_write_bytes", refuse)

    report = harness.writer().regenerate([])

    assert_failed(report, "still holds", f"'{SIDEBAR_PAGE}'")
    assert harness.sidebar() == sidebar.render(["Home"], {}).encode()


def test_the_restore_is_confined_to_the_sidebar_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    harness = scene(tmp_path, "Home")
    harness.seed(PREVIOUS_LF, committed=True)
    harness.backend.on_apply = lambda cs, real: reply(outcome(FAILED, message="boom"))
    seen: list[tuple[Path, str]] = []
    real_restore = rollback.restore_file

    def spy(workdir: Path, path: str, **kwargs: Any) -> Any:
        seen.append((workdir, path))
        return real_restore(workdir, path, **kwargs)

    monkeypatch.setattr(rollback, "restore_file", spy)

    harness.writer().regenerate([])

    assert seen == [(harness.workdir, SIDEBAR_PAGE)]


def test_the_writer_never_raises_even_for_an_error_it_did_not_expect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = scene(tmp_path, "Home")
    monkeypatch.setattr(sidebar_io, "root_pages", lambda git: (_ for _ in ()).throw(ValueError("surprise")))

    report = harness.writer().regenerate([hint("Home", {"order": True})])

    assert_failed(report, "ValueError")
    assert len(report.hint_warnings) == 1


# -- the post-publish fallback ---------------------------------------------------------------------------


def test_restore_after_publish_puts_the_previous_bytes_back_and_forgets_the_sidebar(tmp_path: Path) -> None:
    harness = scene(tmp_path, "Home")
    before = harness.seed(PREVIOUS_CRLF, committed=True)
    writer = harness.writer()
    assert writer.regenerate([]).generated == (SIDEBAR_PAGE,)
    assert harness.sidebar() != before

    text = writer.restore_after_publish()

    assert text.startswith("[github_wiki:sidebar.write_failed] ") and "ignored by git" in text
    assert "\n" not in text
    assert harness.sidebar() == before
    assert harness.manifest.entries() == {}


def test_restore_after_publish_deletes_a_sidebar_that_did_not_exist_before(tmp_path: Path) -> None:
    harness = scene(tmp_path, "Home")
    writer = harness.writer()
    assert writer.regenerate([]).generated == (SIDEBAR_PAGE,)

    text = writer.restore_after_publish()

    assert "sidebar.write_failed" in text
    assert harness.sidebar() is None
    assert harness.manifest.entries() == {}


def test_restore_after_publish_keeps_an_earlier_pending_sidebar_pending(tmp_path: Path) -> None:
    harness = scene(tmp_path, "Home")
    before = harness.seed(PREVIOUS_LF, pending=True)
    writer = harness.writer()
    assert writer.regenerate([]).generated == (SIDEBAR_PAGE,)

    writer.restore_after_publish()

    assert harness.sidebar() == before
    assert harness.manifest.classify([SIDEBAR_PAGE]).pending == (SIDEBAR_PAGE,)


def test_restore_after_publish_leaves_a_foreign_edit_alone_and_says_so(tmp_path: Path) -> None:
    harness = scene(tmp_path, "Home")
    writer = harness.writer()
    assert writer.regenerate([]).generated == (SIDEBAR_PAGE,)
    (harness.workdir / SIDEBAR_PAGE).write_bytes(b"edited after the write\n")

    text = writer.restore_after_publish()

    assert "left untouched" in text
    assert harness.sidebar() == b"edited after the write\n"


def test_restore_after_publish_without_a_write_touches_nothing_and_still_reports(tmp_path: Path) -> None:
    harness = scene(tmp_path, "Home")
    original = harness.seed(managed("Home"), committed=True)

    text = harness.writer().restore_after_publish()

    assert "sidebar.write_failed" in text and "ignored by git" in text
    assert harness.sidebar() == original


def test_restore_after_publish_reports_a_manifest_it_could_not_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = scene(tmp_path, "Home")
    writer = harness.writer()
    assert writer.regenerate([]).generated == (SIDEBAR_PAGE,)

    def refuse(paths: Any) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(harness.manifest, "discard", refuse)

    text = writer.restore_after_publish()

    assert "sidebar.write_failed" in text and "manifest" in text and "OSError" in text
    assert harness.sidebar() is None
