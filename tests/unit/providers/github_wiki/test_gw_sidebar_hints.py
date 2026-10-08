"""Unit tests for the pure sidebar hint model of ``github_wiki`` (``sidebar.py``).

Covers hint validation (SB9), partial-patch application and sticky merge (SB8) and
hint collection from operation metadata (SB7). Everything here is pure: no
filesystem, process or backend access.
"""

from __future__ import annotations

from typing import Any

import pytest

from wikiops.providers.github_wiki.sidebar import (
    UNSET,
    HintPatch,
    HintProblem,
    HintSource,
    Placement,
    RejectedHint,
    apply_patch,
    collect,
    merge,
    page_name,
    validate_hint,
)

TEXT_SHAPE = "a non-empty single-line string of at most 80 characters"
ORDER_SHAPE = "an integer"


def hint(sidebar: Any) -> dict[str, Any]:
    return {"github_wiki": {"sidebar": sidebar}}


def problem_of(metadata: dict[str, Any]) -> HintProblem:
    outcome = validate_hint(metadata)
    assert isinstance(outcome, HintProblem)
    return outcome


def patch_of(metadata: dict[str, Any]) -> HintPatch:
    outcome = validate_hint(metadata)
    assert isinstance(outcome, HintPatch)
    return outcome


# -- validate_hint: absence and clearing --------------------------------------


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"other": {"x": 1}},
        {"github_wiki": {}},
    ],
)
def test_validate_hint_is_none_when_no_sidebar_hint_is_present(metadata):
    assert validate_hint(metadata) is None


def test_validate_hint_sidebar_null_clears_all_three_keys():
    assert patch_of(hint(None)) == HintPatch(None, None, None)


def test_validate_hint_empty_mapping_is_a_valid_no_op_patch():
    patch = patch_of(hint({}))

    assert patch == HintPatch(UNSET, UNSET, UNSET)
    assert patch == HintPatch()


def test_validate_hint_valid_hint_keeps_values_and_leaves_absent_keys_unset():
    patch = patch_of(hint({"group": "Guides", "order": 10}))

    assert patch == HintPatch(group="Guides", order=10, label=UNSET)
    full = patch_of(hint({"group": "G", "order": -3, "label": "Install guide"}))
    assert full == HintPatch(group="G", order=-3, label="Install guide")


def test_validate_hint_null_per_key_is_a_clear_not_an_unset():
    patch = patch_of(hint({"group": None, "order": None, "label": None}))

    assert patch == HintPatch(None, None, None)
    assert patch.group is None
    assert patch.group is not UNSET


# -- validate_hint: structural problems ---------------------------------------


@pytest.mark.parametrize("github_wiki", ["text", 3, ["sidebar"], None, True])
def test_validate_hint_non_mapping_github_wiki_names_the_field(github_wiki):
    outcome = problem_of({"github_wiki": github_wiki})

    assert outcome.field == "github_wiki"
    assert "sidebar" in outcome.expected


def test_validate_hint_typo_key_names_the_offender_and_the_valid_key():
    outcome = problem_of({"github_wiki": {"sidbar": {"group": "G"}}})

    assert outcome.field == "github_wiki.sidbar"
    assert "'sidebar'" in outcome.expected


def test_validate_hint_typo_next_to_a_valid_sidebar_rejects_the_whole_hint():
    outcome = problem_of({"github_wiki": {"sidebar": {"group": "G"}, "extra": 1}})

    assert outcome.field == "github_wiki.extra"


def test_validate_hint_unknown_github_wiki_key_is_the_first_sorted_one():
    outcome = problem_of({"github_wiki": {"zeta": 1, "alpha": 2}})

    assert outcome.field == "github_wiki.alpha"


@pytest.mark.parametrize("sidebar", ["Guides", 5, ["group"], True])
def test_validate_hint_non_mapping_sidebar_expects_a_mapping_or_null(sidebar):
    outcome = problem_of(hint(sidebar))

    assert outcome.field == "sidebar"
    assert "mapping or null" in outcome.expected


def test_validate_hint_unknown_key_rejects_the_whole_hint_without_a_patch():
    outcome = validate_hint(hint({"group": "G", "weight": 3}))

    assert outcome == HintProblem(
        field="weight", expected="only group, order and label are allowed"
    )


def test_validate_hint_reports_the_first_unknown_key_in_sorted_order():
    outcome = problem_of(hint({"zzz": 1, "weight": 3}))

    assert outcome.field == "weight"


# -- validate_hint: group and label -------------------------------------------


@pytest.mark.parametrize("field", ["group", "label"])
def test_validate_hint_text_accepts_up_to_eighty_characters(field):
    exactly_eighty = "a" * 80

    assert patch_of(hint({field: exactly_eighty})) == HintPatch(
        **{field: exactly_eighty}
    )


@pytest.mark.parametrize("field", ["group", "label"])
def test_validate_hint_text_rejects_eighty_one_characters(field):
    outcome = problem_of(hint({field: "a" * 81}))

    assert outcome == HintProblem(field=field, expected=TEXT_SHAPE)


@pytest.mark.parametrize("field", ["group", "label"])
def test_validate_hint_text_counts_code_points_not_bytes(field):
    assert isinstance(validate_hint(hint({field: "é" * 80})), HintPatch)
    assert isinstance(validate_hint(hint({field: "é" * 81})), HintProblem)


@pytest.mark.parametrize("field", ["group", "label"])
@pytest.mark.parametrize(
    "value",
    [
        "a\nb",
        "a\rb",
        "a\r\nb",
        "a b",
        "a b",
        "a\x0bb",
        "a\x0cb",
        "a\x85b",
        "trailing\n",
    ],
)
def test_validate_hint_text_rejects_every_line_boundary(field, value):
    assert problem_of(hint({field: value})) == HintProblem(
        field=field, expected=TEXT_SHAPE
    )


@pytest.mark.parametrize("field", ["group", "label"])
@pytest.mark.parametrize("value", ["", " ", "   ", "\t", " "])
def test_validate_hint_text_rejects_empty_and_whitespace_only(field, value):
    assert problem_of(hint({field: value})).field == field


@pytest.mark.parametrize("field", ["group", "label"])
@pytest.mark.parametrize("value", [5, 1.5, True, ["a"], {"a": 1}, b"bytes"])
def test_validate_hint_text_rejects_non_strings(field, value):
    assert problem_of(hint({field: value})) == HintProblem(
        field=field, expected=TEXT_SHAPE
    )


def test_validate_hint_text_keeps_the_exact_value_including_padding():
    assert patch_of(hint({"group": "  Guides  "})).group == "  Guides  "


# -- validate_hint: order -----------------------------------------------------


@pytest.mark.parametrize("value", [0, 1, -7, 10**12])
def test_validate_hint_order_accepts_integers(value):
    assert patch_of(hint({"order": value})).order == value


@pytest.mark.parametrize("value", [True, False, "ten", "10", 1.5, 2.0, [1], {}])
def test_validate_hint_order_rejects_booleans_and_non_integers(value):
    assert problem_of(hint({"order": value})) == HintProblem(
        field="order", expected=ORDER_SHAPE
    )


def test_validate_hint_order_none_is_a_clear():
    assert patch_of(hint({"order": None})).order is None


# -- validate_hint: first problem is deterministic ----------------------------


def test_validate_hint_reports_the_first_invalid_known_key_in_sorted_order():
    both = {"order": "ten", "label": "a" * 81, "group": "x\ny"}

    assert problem_of(hint(both)).field == "group"
    assert problem_of(hint({"order": "ten", "label": "a" * 81})).field == "label"


def test_validate_hint_unknown_key_outranks_an_invalid_known_value():
    outcome = problem_of(hint({"group": "x\ny", "weight": 1}))

    assert outcome.field == "weight"


def test_validate_hint_with_non_string_keys_is_deterministic():
    outcome = problem_of(hint({3: "x", 1: "y"}))

    assert outcome.field == "1"


# -- apply_patch --------------------------------------------------------------

CURRENT = Placement(group="Guides", order=10, label="Install guide")


def test_apply_patch_unset_keeps_every_key():
    assert apply_patch(CURRENT, HintPatch()) == CURRENT


def test_apply_patch_value_overrides_only_that_key():
    assert apply_patch(CURRENT, HintPatch(order=5)) == Placement(
        group="Guides", order=5, label="Install guide"
    )
    assert apply_patch(CURRENT, HintPatch(group="Ops", label="L")) == Placement(
        group="Ops", order=10, label="L"
    )


def test_apply_patch_none_clears_only_that_key():
    assert apply_patch(CURRENT, HintPatch(group=None)) == Placement(
        group=None, order=10, label="Install guide"
    )
    assert apply_patch(CURRENT, HintPatch(order=None)) == Placement(
        group="Guides", order=None, label="Install guide"
    )
    assert apply_patch(CURRENT, HintPatch(label=None)) == Placement(
        group="Guides", order=10, label=None
    )


def test_apply_patch_null_sidebar_clears_all_three():
    assert apply_patch(CURRENT, HintPatch(None, None, None)) == Placement()


def test_apply_patch_zero_order_is_a_value_not_a_clear():
    assert apply_patch(Placement(), HintPatch(order=0)).order == 0


def test_apply_patch_does_not_mutate_the_current_placement():
    apply_patch(CURRENT, HintPatch(None, None, None))

    assert CURRENT == Placement(group="Guides", order=10, label="Install guide")


# -- page_name ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("file_name", "expected"),
    [
        ("Install.md", "Install"),
        ("Install.MD", "Install"),
        ("Install.Md", "Install"),
        ("a.md.md", "a.md"),
        ("Getting-Started.md", "Getting-Started"),
        ("NoSuffix", "NoSuffix"),
    ],
)
def test_page_name_strips_only_the_final_markdown_suffix(file_name, expected):
    assert page_name(file_name) == expected


# -- collect ------------------------------------------------------------------


def source(op: str, page: str, metadata: dict[str, Any]) -> HintSource:
    return HintSource(operation_id=op, page=page, metadata=metadata)


def test_collect_yields_patches_in_operation_order():
    patches, rejected = collect(
        [
            source("op-2", "B.md", hint({"order": 2})),
            source("op-1", "A.md", hint({"group": "G"})),
        ]
    )

    assert patches == (
        ("B", HintPatch(order=2)),
        ("A", HintPatch(group="G")),
    )
    assert rejected == ()


def test_collect_skips_sources_without_a_hint():
    patches, rejected = collect(
        [
            source("op-1", "A.md", {}),
            source("op-2", "B.md", {"github_wiki": {}}),
            source("op-3", "C.md", hint({"label": "See"})),
        ]
    )

    assert patches == (("C", HintPatch(label="See")),)
    assert rejected == ()


def test_collect_with_nothing_to_collect_is_empty():
    assert collect([]) == ((), ())


def test_collect_rejects_a_hint_on_an_underscore_page():
    patches, rejected = collect(
        [
            source("op-1", "_Footer.md", hint({"group": "G"})),
            source("op-2", "Real.md", hint({"order": 1})),
        ]
    )

    assert patches == (("Real", HintPatch(order=1)),)
    assert rejected == (
        RejectedHint(
            operation_id="op-1",
            page="_Footer",
            problem=HintProblem(
                field="sidebar",
                expected="a page whose name does not start with '_'",
            ),
        ),
    )


def test_collect_rejects_a_clearing_hint_on_an_underscore_page_too():
    patches, rejected = collect([source("op-1", "_Sidebar.md", hint(None))])

    assert patches == ()
    assert [r.page for r in rejected] == ["_Sidebar"]


def test_collect_ignores_underscore_pages_without_any_hint():
    assert collect([source("op-1", "_Footer.md", {})]) == ((), ())


def test_collect_invalid_hint_yields_a_rejection_and_no_partial_patch():
    patches, rejected = collect(
        [source("op-1", "Install.md", hint({"group": "Guides", "order": "ten"}))]
    )

    assert patches == ()
    assert rejected == (
        RejectedHint(
            operation_id="op-1",
            page="Install",
            problem=HintProblem(field="order", expected=ORDER_SHAPE),
        ),
    )


def test_collect_typo_key_is_reported_against_the_op_and_page():
    _, rejected = collect([source("op-9", "Install.md", {"github_wiki": {"sidbar": {}}})])

    assert [(r.operation_id, r.page, r.problem.field) for r in rejected] == [
        ("op-9", "Install", "github_wiki.sidbar")
    ]


def test_collect_multiple_operations_on_one_page_keep_all_patches_in_order():
    patches, _ = collect(
        [
            source("op-1", "Install.md", hint({"group": "Old", "order": 1})),
            source("op-2", "Install.md", hint({"group": "New"})),
        ]
    )

    assert patches == (
        ("Install", HintPatch(group="Old", order=1)),
        ("Install", HintPatch(group="New")),
    )
    # later keys win once merged (G8)
    assert merge({}, patches)["Install"] == Placement(group="New", order=1)


def test_collect_an_invalid_hint_does_not_cancel_a_valid_one_on_the_same_page():
    patches, rejected = collect(
        [
            source("op-1", "Install.md", hint({"group": "Good"})),
            source("op-2", "Install.md", hint({"order": True})),
        ]
    )

    assert patches == (("Install", HintPatch(group="Good")),)
    assert [r.operation_id for r in rejected] == ["op-2"]


def test_collect_accepts_any_iterable():
    sources = (source("op-1", "A.md", hint({"order": 3})) for _ in range(1))

    patches, _ = collect(sources)

    assert patches == (("A", HintPatch(order=3)),)


# -- merge --------------------------------------------------------------------


def test_merge_keeps_previous_placement_for_pages_without_patches():
    previous = {"Install": CURRENT, "Other": Placement(order=1)}

    merged = merge(previous, [("Other", HintPatch(order=2))])

    assert merged["Install"] == CURRENT
    assert merged["Other"] == Placement(order=2)


def test_merge_with_no_patches_returns_an_equal_copy_not_the_same_mapping():
    previous = {"Install": CURRENT}

    merged = merge(previous, ())

    assert merged == previous
    assert merged is not previous


def test_merge_hintless_operation_keeps_placement():
    # a hintless op simply contributes no patch (collect skips it)
    patches, _ = collect([source("op-1", "Install.md", {})])

    assert merge({"Install": CURRENT}, patches)["Install"] == CURRENT


def test_merge_partial_hint_overrides_only_the_given_keys():
    merged = merge({"Install": CURRENT}, [("Install", HintPatch(order=5))])

    assert merged["Install"] == Placement(group="Guides", order=5, label="Install guide")


def test_merge_null_key_clears_that_key_and_keeps_the_others():
    merged = merge({"Install": CURRENT}, [("Install", HintPatch(group=None))])

    assert merged["Install"] == Placement(group=None, order=10, label="Install guide")


def test_merge_null_sidebar_clears_all_three_keys():
    merged = merge({"Install": CURRENT}, [("Install", HintPatch(None, None, None))])

    assert merged["Install"] == Placement()


def test_merge_new_page_without_previous_gets_defaults_plus_the_patch():
    merged = merge({}, [("Fresh", HintPatch(group="G"))])

    assert merged == {"Fresh": Placement(group="G", order=None, label=None)}


def test_merge_later_patches_for_one_page_apply_in_order():
    merged = merge(
        {},
        [
            ("Install", HintPatch(group="A", order=1)),
            ("Install", HintPatch(group=None)),
            ("Install", HintPatch(order=9)),
        ],
    )

    assert merged["Install"] == Placement(group=None, order=9)


def test_merge_does_not_mutate_the_previous_mapping():
    previous = {"Install": CURRENT}

    merge(previous, [("Install", HintPatch(None, None, None))])

    assert previous == {"Install": CURRENT}


# -- UNSET --------------------------------------------------------------------


def test_unset_is_a_distinct_sentinel_that_names_itself():
    assert repr(UNSET) == "UNSET"
    assert UNSET is not None
    assert HintPatch().group is UNSET
