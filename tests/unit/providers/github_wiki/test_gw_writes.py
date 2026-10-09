"""Pure decisions behind ``apply_changes``: preparing operations, settling results, annotating.

No git, no filesystem: operations and ``ApplyResult`` objects go in, results and path
lists come out. The provider-level behavior is covered by ``test_gw_provider_write_apply``.
"""

from __future__ import annotations

import pytest
from wikiops_sdk.domain import (
    AppliedOperationResult,
    ApplyResult,
    OperationStatus,
)

from tests.support.write_ops import asset, asset_ref, child, create, ref, sidebar_hint, update
from wikiops.providers._fs import hashed_asset_name
from wikiops.providers.github_wiki import writes
from wikiops.providers.github_wiki.errors import GithubWikiError
from wikiops.providers.github_wiki.sidebar import HintSource

APPLIED, SKIPPED, FAILED = OperationStatus.APPLIED, OperationStatus.SKIPPED, OperationStatus.FAILED


def result(operation, status=APPLIED, *, path=None, message="Document created", **fields):  # noqa: ANN001, ANN201
    return AppliedOperationResult(
        operation_id=operation.operation_id,
        status=status,
        message=message,
        resolved_ref=ref(path) if path else None,
        **fields,
    )


# -- prepare ------------------------------------------------------------------------------------


def test_prepare_delegates_valid_operations_in_order_and_isolates_the_invalid_ones() -> None:
    first, bad, second = create("A.md"), create("x/y.md"), update("B.md", "z")

    prepared = writes.prepare([first, bad, second], provider_name="docs")

    assert [op.operation_id for op in prepared.delegated] == [first.operation_id, second.operation_id]
    assert list(prepared.failures) == [bad.operation_id]
    failure = prepared.failures[bad.operation_id]
    assert failure.status is FAILED and failure.resolved_ref == bad.ref
    assert (failure.message or "").startswith("[github_wiki:path.nested_not_supported]")


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        (create(None, title="Release  Notes"), "Release-Notes.md"),
        (create(None, title="Home"), "Home.md"),
        (child("Getting Started"), "Getting-Started.md"),
    ],
)
def test_prepare_derives_a_flat_root_ref_for_ref_less_creates(operation, expected) -> None:  # noqa: ANN001
    prepared = writes.prepare([operation], provider_name="docs")

    derived = prepared.delegated[0].ref
    assert derived.locator["path"] == expected
    assert derived.provider == "docs"
    assert operation.ref is None  # the planned operation is not mutated


def test_prepare_keeps_an_explicit_flat_ref_untouched() -> None:
    operation = child("Whatever", path="Chosen.md")

    prepared = writes.prepare([operation], provider_name="docs")

    assert prepared.delegated == (operation,)


def test_prepare_passes_operations_it_does_not_know_to_the_backend() -> None:
    operation = asset()

    prepared = writes.prepare([operation], provider_name="docs")

    assert prepared.delegated == (operation,)
    assert prepared.failures == {}


# -- settle -------------------------------------------------------------------------------------


def test_settle_collects_the_pages_and_assets_of_applied_results() -> None:
    first, second = create("A.md"), update("B.md", "x")
    reply = ApplyResult(
        provider_name="docs",
        results=[
            result(first, path="A.md", resolved_asset_ref=asset_ref("assets/a.png")),
            result(second, path="B.md"),
        ],
    )

    settled = writes.settle([first, second], reply)

    assert settled.pages == ("A.md", "B.md")
    assert settled.assets == ("assets/a.png",)
    assert settled.written == ("A.md", "B.md", "assets/a.png")


@pytest.mark.parametrize("status", [SKIPPED, FAILED])
def test_settle_counts_nothing_as_written_for_results_that_are_not_applied(status) -> None:  # noqa: ANN001
    operation = create("A.md")
    reply = ApplyResult(provider_name="docs", results=[result(operation, status, path="A.md")])

    settled = writes.settle([operation], reply)

    assert settled.written == ()
    assert settled.results[operation.operation_id].status is status


def test_settle_fails_an_operation_the_backend_did_not_answer() -> None:
    answered, silent = create("A.md"), create("B.md")
    reply = ApplyResult(provider_name="docs", results=[result(answered, path="A.md")])

    settled = writes.settle([answered, silent], reply)

    assert settled.results[silent.operation_id].status is FAILED
    assert "returned no result" in (settled.results[silent.operation_id].message or "")
    assert settled.pages == ("A.md",)


def test_settle_ignores_results_for_operations_it_never_delegated() -> None:
    delegated, stranger = create("A.md"), create("B.md")
    reply = ApplyResult(
        provider_name="docs",
        results=[result(delegated, path="A.md"), result(stranger, path="B.md")],
    )

    settled = writes.settle([delegated], reply)

    assert settled.pages == ("A.md",)
    assert list(settled.results) == [delegated.operation_id]


def test_settle_turns_an_unusable_asset_reference_into_a_coded_failure() -> None:
    operation = create("A.md")
    reply = ApplyResult(
        provider_name="docs",
        results=[result(operation, path="A.md", resolved_asset_ref=asset_ref("../outside.png"))],
    )

    settled = writes.settle([operation], reply)

    failure = settled.results[operation.operation_id]
    assert failure.status is FAILED
    assert (failure.message or "").startswith("[github_wiki:asset.ref_unsupported]")
    assert settled.written == ()  # all or nothing per operation


# -- in_order -----------------------------------------------------------------------------------


def test_in_order_follows_the_plan_and_takes_the_first_source_that_has_the_operation() -> None:
    one, two, three = create("A.md"), create("B.md"), create("C.md")
    early = {two.operation_id: writes.failed(two.operation_id, "early")}
    late = {
        one.operation_id: result(one, path="A.md"),
        two.operation_id: result(two, path="B.md"),
        three.operation_id: result(three, path="C.md"),
    }

    ordered = writes.in_order([one, two, three], early, late)

    assert [item.operation_id for item in ordered] == [one.operation_id, two.operation_id, three.operation_id]
    assert ordered[1].message == "early"
    assert ordered[0].status is APPLIED


# -- with_note ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Document created at '/w/A.md'", "Document created at '/w/A.md'; NOTE"),
        ("[local_files:applied.overwritten] Existing document overwritten. Hint: x.", "[local_files:applied.overwritten] Existing document overwritten. Hint: x. NOTE"),
        ("trailing space  ", "trailing space; NOTE"),
        ("", "NOTE"),
        (None, "NOTE"),
    ],
)
def test_with_note_keeps_the_backend_sentence_intact(message, expected) -> None:  # noqa: ANN001
    assert writes.with_note(message, "NOTE") == expected


# -- annotate -----------------------------------------------------------------------------------


def make_results():  # noqa: ANN201
    applied, skipped, failed = create("A.md"), create("B.md"), create("C.md")
    return [
        result(applied, path="A.md"),
        result(skipped, SKIPPED, path="B.md", message="unchanged"),
        result(failed, FAILED, path="C.md", message="boom"),
    ]


def test_annotate_puts_the_note_on_applied_results_only() -> None:
    annotated = writes.annotate(make_results(), note="committed abc1234, pushed to master")

    assert annotated[0].message == "Document created; committed abc1234, pushed to master"
    assert annotated[1].message == "unchanged"
    assert annotated[2].message == "boom"


def test_annotate_without_a_note_changes_nothing() -> None:
    results = make_results()

    assert writes.annotate(results) == results


def test_annotate_fails_every_applied_result_with_the_error_and_leaves_the_others() -> None:
    error = GithubWikiError("commit.failed", "git commit failed: written but not committed")

    annotated = writes.annotate(make_results(), error=error)

    assert [item.status for item in annotated] == [FAILED, SKIPPED, FAILED]
    assert annotated[0].message == str(error)
    assert annotated[1].message == "unchanged"
    assert annotated[2].message == "boom"  # a failure of its own is not rewritten


def test_annotate_reports_the_error_on_the_no_ops_when_nothing_was_written() -> None:
    skipped = [item for item in make_results() if item.status is SKIPPED]
    error = GithubWikiError("push.failed", "committed locally at abc1234 in '/w', push failed: x")

    annotated = writes.annotate(skipped, error=error)

    assert [item.status for item in annotated] == [FAILED]
    assert annotated[0].message == str(error)


# -- failure_text / fail_all --------------------------------------------------------------------


def test_failure_text_keeps_a_coded_error_as_it_is() -> None:
    error = GithubWikiError("workdir.locked", "The workdir is locked")

    assert writes.failure_text(error) == str(error)


def test_failure_text_names_and_redacts_any_other_error() -> None:
    text = writes.failure_text(RuntimeError("cannot reach https://user:hunter2@example.test/x.git"))

    assert "RuntimeError" in text and "cannot reach" in text
    assert "hunter2" not in text


def test_fail_all_returns_one_failed_result_per_operation_in_order_keeping_page_refs() -> None:
    page, fresh, stored = update("A.md", "x"), create(None, title="T"), asset()

    results = writes.fail_all([page, fresh, stored], RuntimeError("down"))

    assert [item.operation_id for item in results] == [page.operation_id, fresh.operation_id, stored.operation_id]
    assert all(item.status is FAILED and "down" in (item.message or "") for item in results)
    assert results[0].resolved_ref == page.ref
    assert results[1].resolved_ref is None
    assert results[2].resolved_ref is None  # an asset ref is not a page ref


def test_in_order_leaves_out_an_operation_no_source_answered() -> None:
    answered, unanswered = create("A.md"), create("B.md")

    ordered = writes.in_order([answered, unanswered], {answered.operation_id: result(answered, path="A.md")})

    assert [item.operation_id for item in ordered] == [answered.operation_id]


# -- what an operation owns (the rollback never goes beyond it) ----------------------------------


def test_page_targets_are_the_pages_the_operations_carry_not_what_a_backend_reports() -> None:
    created, updated, nested, uploaded = create("A.md"), update("B.md", "z"), create("x/y.md"), asset()

    targets = writes.page_targets([created, updated, nested, uploaded])

    assert targets == frozenset({"A.md", "B.md"})  # nested is invalid, an asset has no page


def test_page_targets_of_nothing_is_empty() -> None:
    assert writes.page_targets([]) == frozenset()


def test_an_asset_owns_its_content_hashed_name_in_any_directory_and_the_reported_path() -> None:
    owns = writes.asset_owner(asset("logo.png"), b"bytes", reported="assets/reported.png")

    stored = hashed_asset_name("logo.png", b"bytes")
    assert owns(f"assets/{stored}") and owns(f"other/{stored}")
    assert owns("assets/reported.png")
    assert not owns("assets/logo--0000000000000000.png")  # same stem, other bytes
    assert not owns("assets/notes.txt")


# -- rollback ownership of a whole changeset (pages AND assets) ---------------------------------


def test_asset_owner_without_content_owns_any_content_hash_of_the_asset_name() -> None:
    owns = writes.asset_owner(asset("logo.png"), None)

    assert owns(f"assets/{hashed_asset_name('logo.png', b'one')}")
    assert owns(f"assets/{hashed_asset_name('logo.png', b'two')}")
    assert owns(f"other/{hashed_asset_name('logo.png', b'two')}")
    assert not owns("assets/logo--nothex0000000000.png")  # not a content hash
    assert not owns("assets/logo--0123.png")  # hash too short
    assert not owns(f"assets/{hashed_asset_name('logo.jpg', b'one')}")  # other suffix
    assert not owns(f"assets/{hashed_asset_name('chart.png', b'one')}")  # other stem
    assert not owns("assets/logo.png")


def test_the_rollback_owner_of_a_changeset_covers_its_pages_and_its_assets() -> None:
    created, uploaded = create("A.md"), asset("logo.png")

    owns = writes.rollback_owner([created, uploaded])

    assert owns("A.md")
    assert owns(f"assets/{hashed_asset_name('logo.png', b'any bytes')}")
    assert not owns("B.md")
    assert not owns("notes.txt")


def test_the_rollback_owner_adds_the_asset_path_the_backend_reported_for_that_operation() -> None:
    uploaded, other = asset("logo.png"), asset("chart.png", "chart")
    reply = ApplyResult(
        provider_name="docs",
        results=[
            AppliedOperationResult(
                operation_id=uploaded.operation_id,
                status=APPLIED,
                resolved_asset_ref=asset_ref("assets/odd name.png"),
            ),
            AppliedOperationResult(
                operation_id="unrelated",
                status=APPLIED,
                resolved_asset_ref=asset_ref("assets/not-mine.png"),
            ),
        ],
    )

    owns = writes.rollback_owner([uploaded, other], reply)

    assert owns("assets/odd name.png")
    assert not owns("assets/not-mine.png")  # reported by a result of an operation it does not own


def test_the_rollback_owner_of_a_page_only_changeset_owns_pages_alone() -> None:
    owns = writes.rollback_owner([create("A.md"), update("B.md", "z")])

    assert owns("A.md") and owns("B.md")
    assert not owns("assets/anything--0123456789abcdef.png")


def test_an_asset_without_a_usable_name_owns_only_what_was_reported() -> None:
    nameless = asset("logo.png").model_copy(update={"name": None})
    owns = writes.asset_owner(nameless, b"bytes", reported="assets/x.png")

    assert owns("assets/x.png")
    assert not owns("assets/logo--anything.png")


# -- prepare: the reserved sidebar name (SB2, GWP-D2) ---------------------------------------------------


@pytest.mark.parametrize("name", ["_Sidebar.md", "_sidebar.md", "_SIDEBAR.MD"])
def test_prepare_with_the_sidebar_reserved_rejects_an_explicit_create_and_update(name: str) -> None:
    created, updated, other = create(name), update(name, "x"), create("Other.md")

    prepared = writes.prepare([created, other, updated], provider_name="docs", reserve_sidebar=True)

    assert [op.operation_id for op in prepared.delegated] == [other.operation_id]
    assert set(prepared.failures) == {created.operation_id, updated.operation_id}
    for failure in prepared.failures.values():
        assert failure.status is FAILED
        assert (failure.message or "").startswith("[github_wiki:path.reserved]")
        assert "generate_sidebar" in (failure.message or "")


def test_prepare_with_the_sidebar_reserved_rejects_a_ref_less_title_that_derives_it() -> None:
    titled, other = create(None, title="_Sidebar"), create("Other.md")

    prepared = writes.prepare([titled, other], provider_name="docs", reserve_sidebar=True)

    assert [op.operation_id for op in prepared.delegated] == [other.operation_id]
    failure = prepared.failures[titled.operation_id]
    assert (failure.message or "").startswith("[github_wiki:path.reserved]")
    assert failure.resolved_ref is None


@pytest.mark.parametrize("operation", [child("_Sidebar"), child("Whatever", path="_Sidebar.md")], ids=["derived", "explicit"])
def test_prepare_with_the_sidebar_reserved_rejects_a_child_create_resolving_to_it(operation) -> None:  # noqa: ANN001
    sibling = child("Fine")

    prepared = writes.prepare([operation, sibling], provider_name="docs", reserve_sidebar=True)

    assert [op.operation_id for op in prepared.delegated] == [sibling.operation_id]
    assert (prepared.failures[operation.operation_id].message or "").startswith("[github_wiki:path.reserved]")


def test_prepare_with_the_sidebar_reserved_leaves_the_footer_and_other_pages_alone() -> None:
    operations = [create("_Footer.md"), create("Home.md"), child("Sidebar notes")]

    prepared = writes.prepare(operations, provider_name="docs", reserve_sidebar=True)

    assert len(prepared.delegated) == 3 and prepared.failures == {}


@pytest.mark.parametrize("options", [{}, {"reserve_sidebar": False}], ids=["omitted", "false"])
def test_prepare_treats_the_sidebar_as_an_ordinary_page_unless_reserved(options) -> None:  # noqa: ANN001
    operations = [create("_Sidebar.md"), update("_Sidebar.md", "x"), create(None, title="_Sidebar"), child("_Sidebar")]

    prepared = writes.prepare(operations, provider_name="docs", **options)

    assert len(prepared.delegated) == 4 and prepared.failures == {}


def test_prepare_still_rejects_a_git_component_whatever_the_flag() -> None:
    for flag in (True, False):
        prepared = writes.prepare([create(".git/x.md")], provider_name="docs", reserve_sidebar=flag)

        assert prepared.delegated == ()


# -- Settled.generated ---------------------------------------------------------------------------------


def settled_with(*, generated: tuple[str, ...] = ()) -> writes.Settled:
    page, other = create("A.md"), create("B.md")
    reply = ApplyResult(
        provider_name="docs",
        results=[result(page, path="A.md"), result(other, path="B.md", resolved_asset_ref=asset_ref("assets/x.png"))],
    )
    return writes.with_generated(writes.settle([page, other], reply), generated)


def test_settled_written_joins_pages_assets_and_the_generated_sidebar_in_that_order() -> None:
    settled = settled_with(generated=("_Sidebar.md",))

    assert settled.written == ("A.md", "B.md", "assets/x.png", "_Sidebar.md")
    assert settled.generated == ("_Sidebar.md",)


def test_the_sidebar_never_counts_as_a_page() -> None:
    settled = settled_with(generated=("_Sidebar.md",))

    assert settled.pages == ("A.md", "B.md")
    assert len(settled.pages) == 2


def test_a_settled_has_no_generated_path_by_default() -> None:
    settled = settled_with()

    assert settled.generated == () and settled.written == ("A.md", "B.md", "assets/x.png")


def test_with_generated_keeps_everything_else_and_does_not_mutate_the_original() -> None:
    original = settled_with()

    changed = writes.with_generated(original, ("_Sidebar.md",))

    assert original.generated == ()
    assert (changed.results, changed.pages, changed.assets, changed.rejected) == (
        original.results,
        original.pages,
        original.assets,
        original.rejected,
    )


def test_note_rollback_preserves_the_generated_paths() -> None:
    from wikiops.providers.github_wiki.rollback import RollbackResult

    settled = settled_with(generated=("_Sidebar.md",))
    settled = writes.Settled(
        {**settled.results}, settled.pages, settled.assets, rejected=(next(iter(settled.results)),), generated=("_Sidebar.md",)
    )

    noted = writes.note_rollback(settled, RollbackResult(leftovers=("A.md",)))

    assert noted.generated == ("_Sidebar.md",)
    assert noted.written[-1] == "_Sidebar.md"


# -- hint_sources --------------------------------------------------------------------------------------

HINT = sidebar_hint(group="Guides", order=1)


def settled_results(*pairs):  # noqa: ANN001, ANN201
    return {op.operation_id: result(op, status, path=path) for op, status, path in pairs}


def test_hint_sources_takes_only_applied_and_skipped_operations_in_plan_order() -> None:
    applied, skipped, failed_op, bare = (create("A.md", metadata=HINT), update("B.md", "x", metadata=HINT),
                                         create("C.md", metadata=HINT), create("D.md"))
    results = settled_results(
        (applied, APPLIED, "A.md"), (skipped, SKIPPED, "B.md"), (failed_op, FAILED, "C.md"), (bare, APPLIED, "D.md")
    )

    sources = writes.hint_sources([failed_op, applied, bare, skipped], results)

    assert sources == (
        HintSource(applied.operation_id, "A.md", HINT),
        HintSource(bare.operation_id, "D.md", {}),
        HintSource(skipped.operation_id, "B.md", HINT),
    )


def test_hint_sources_reads_child_metadata_for_a_child_create_and_metadata_otherwise() -> None:
    kid = child("Kid", path="Kid.md", metadata=HINT)
    created = create("A.md", metadata=sidebar_hint(label="L"))
    results = settled_results((kid, APPLIED, "Kid.md"), (created, APPLIED, "A.md"))

    sources = writes.hint_sources([kid, created], results)

    assert [(item.page, item.metadata) for item in sources] == [("Kid.md", HINT), ("A.md", sidebar_hint(label="L"))]


def test_hint_sources_derives_the_page_of_a_ref_less_create_and_child() -> None:
    titled, kid = create(None, title="Release Notes", metadata=HINT), child("Getting Started", metadata=HINT)
    prepared = writes.prepare([titled, kid], provider_name="docs")
    results = settled_results(*[(op, APPLIED, op.ref.locator["path"]) for op in prepared.delegated])

    sources = writes.hint_sources(prepared.delegated, results)

    assert [item.page for item in sources] == ["Release-Notes.md", "Getting-Started.md"]


def test_hint_sources_skips_assets_and_operations_without_a_result_silently() -> None:
    image, answered, missing = asset(), create("A.md", metadata=HINT), create("B.md", metadata=HINT)
    results = settled_results((answered, APPLIED, "A.md"))

    sources = writes.hint_sources([image, answered, missing], results)

    assert [item.page for item in sources] == ["A.md"]


def test_hint_sources_ignores_a_rejected_operation_the_provider_turned_into_a_failure() -> None:
    bad = create("A.md", metadata=HINT)
    reply = ApplyResult(provider_name="docs", results=[result(bad, path="../escape.md")])
    settled = writes.settle([bad], reply)

    assert settled.results[bad.operation_id].status is FAILED
    assert writes.hint_sources([bad], settled.results) == ()


# -- first_carrier and attach_warnings (SB13) ----------------------------------------------------------

WARNING_A = "[github_wiki:sidebar.invalid_hint] Sidebar hint ignored. Hint: fix it."
WARNING_B = "[github_wiki:sidebar.write_failed] The sidebar was not written. Hint: re-run."


def carrier_results():  # noqa: ANN201
    first, second, third = create("A.md"), create("B.md"), create("C.md")
    return [
        result(first, FAILED, path="A.md", message="boom"),
        result(second, SKIPPED, path="B.md", message="unchanged"),
        result(third, APPLIED, path="C.md", message="created"),
    ]


def test_first_carrier_is_the_first_applied_or_skipped_result_in_plan_order() -> None:
    results = carrier_results()

    assert writes.first_carrier(results) == results[1].operation_id
    assert writes.first_carrier(list(reversed(results))) == results[2].operation_id


def test_first_carrier_is_none_when_every_result_failed_or_there_is_none() -> None:
    failed_only = [item for item in carrier_results() if item.status is FAILED]

    assert writes.first_carrier(failed_only) is None
    assert writes.first_carrier([]) is None


def test_attach_warnings_appends_once_to_the_carrier_and_leaves_the_others() -> None:
    results = carrier_results()

    attached = writes.attach_warnings(results, [WARNING_A, WARNING_B], fallback=None)

    assert attached[0] == results[0] and attached[2] == results[2]
    assert attached[1].message == writes.with_note("unchanged", f"{WARNING_A} {WARNING_B}")
    assert (attached[1].message or "").count("sidebar.invalid_hint") == 1
    assert attached[1].status is SKIPPED and len(attached) == 3


def test_attach_warnings_keeps_the_commit_note_and_puts_the_warnings_after_it() -> None:
    created = create("A.md")
    noted = result(created, path="A.md", message="Document created; committed locally at abc1234 in '/w'; not pushed")

    attached = writes.attach_warnings([noted], [WARNING_A, WARNING_B], fallback=None)

    message = attached[0].message or ""
    assert message.startswith("Document created; committed locally at abc1234 in '/w'; not pushed; ")
    assert message.index(WARNING_A) < message.index(WARNING_B)


def test_attach_warnings_without_warnings_returns_the_results_unchanged() -> None:
    results = carrier_results()

    assert writes.attach_warnings(results, [], fallback=results[0].operation_id) == results


def test_attach_warnings_falls_back_to_the_pre_publish_carrier_when_publishing_failed_everything() -> None:
    first, second = create("A.md"), create("B.md")
    results = [result(first, FAILED, path="A.md", message="push rejected"), result(second, FAILED, path="B.md", message="push rejected")]

    attached = writes.attach_warnings(results, [WARNING_B], fallback=second.operation_id)

    assert attached[0] == results[0]
    assert attached[1].status is FAILED
    assert attached[1].message == writes.with_note("push rejected", WARNING_B)


def test_attach_warnings_never_adds_a_synthetic_result() -> None:
    results = [result(create("A.md"), FAILED, path="A.md", message="boom")]

    assert writes.attach_warnings(results, [WARNING_A], fallback=None) == results
    assert writes.attach_warnings(results, [WARNING_A], fallback="unknown-id") == results
    assert writes.attach_warnings([], [WARNING_A], fallback="unknown-id") == []
