"""Unit tests for the pure sidebar hint model of ``github_wiki`` (``sidebar.py``).

Covers hint validation (SB9), partial-patch application and sticky merge (SB8) and
hint collection from operation metadata (SB7). Everything here is pure: no
filesystem, process or backend access.
"""

from __future__ import annotations

from typing import Any

import pytest

from wikiops.providers.github_wiki.errors import CODES
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
    render_hint_warning,
    validate_hint,
)

TEXT_SHAPE = "a non-empty single-line string of at most 80 characters"
ORDER_SHAPE = "an integer between -1000000 and 1000000"
ZERO_WIDTH = chr(0x200B)
BOM = chr(0xFEFF)
BIDI_OVERRIDE = chr(0x202E)
SOFT_HYPHEN = chr(0x00AD)
NBSP = chr(0x00A0)


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


# -- validate_hint: unknown keys are echoed bounded and escaped ---------------

ECHO_LIMIT = 80


def sidebar_key_field(key: Any) -> str:
    return problem_of(hint({key: 1})).field


def namespace_key_field(key: Any) -> str:
    return problem_of({"github_wiki": {key: 1}}).field


@pytest.mark.parametrize("length", [1, 40, ECHO_LIMIT])
def test_unknown_key_up_to_the_limit_is_echoed_whole(length):
    key = "k" * length

    assert sidebar_key_field(key) == key
    assert namespace_key_field(key) == f"github_wiki.{key}"


@pytest.mark.parametrize("length", [ECHO_LIMIT + 1, 500, 100_000])
def test_unknown_key_over_the_limit_is_truncated_with_an_ellipsis(length):
    key = "k" * length

    assert sidebar_key_field(key) == "k" * ECHO_LIMIT + "..."
    assert namespace_key_field(key) == "github_wiki." + "k" * ECHO_LIMIT + "..."


def test_unknown_key_truncation_does_not_change_which_key_is_reported_first():
    shared = "k" * 200

    outcome = problem_of(hint({shared + "b": 1, shared + "a": 1}))

    assert outcome.field == "k" * ECHO_LIMIT + "..."
    assert outcome.expected == "only group, order and label are allowed"


@pytest.mark.parametrize(
    ("key", "echoed"),
    [
        ("a\nb", "a\\nb"),
        ("a\rb", "a\\rb"),
        ("a\r\nb", "a\\r\\nb"),
        ("a\x0bb", "a\\x0bb"),
        ("a\x0cb", "a\\x0cb"),
        ("a\x1cb", "a\\x1cb"),
        ("a\x1db", "a\\x1db"),
        ("a\x1eb", "a\\x1eb"),
        ("a\x85b", "a\\x85b"),
        ("a\u2028b", "a\\u2028b"),
        ("a\u2029b", "a\\u2029b"),
        ("a\x00b", "a\\x00b"),
        ("a\x1bb", "a\\x1bb"),
        ("a\x7fb", "a\\x7fb"),
        ("a\u202eb", "a\\u202eb"),
        ("a\u200bb", "a\\u200bb"),
    ],
)
def test_unknown_key_control_and_line_break_characters_are_escaped(key, echoed):
    assert sidebar_key_field(key) == echoed
    assert namespace_key_field(key) == f"github_wiki.{echoed}"


def test_unknown_key_echo_is_one_line_of_visible_characters():
    hostile = "x\n\r\u2028\u2029\x1b[31m\u202e" * 20

    field = sidebar_key_field(hostile)

    assert len(field.splitlines()) == 1
    assert field.isprintable()
    assert field.startswith("x\\n\\r\\u2028\\u2029\\x1b[31m\\u202e")


def test_unknown_key_echo_is_bounded_even_when_every_character_expands():
    field = sidebar_key_field("\u202e" * 10_000)

    assert len(field) <= ECHO_LIMIT + len("...")
    assert field.endswith("...")
    assert field.count("\\u202e") == ECHO_LIMIT // len("\\u202e")


def test_unknown_key_truncation_never_splits_an_escape_sequence():
    field = sidebar_key_field("\u202e" * 100)

    assert field == "\\u202e" * (ECHO_LIMIT // 6) + "..."


@pytest.mark.parametrize("key", [3, 1.5, ("a", 1), None])
def test_unknown_non_string_key_is_echoed_through_str(key):
    assert sidebar_key_field(key) == str(key)


def test_unknown_key_with_a_hostile_repr_is_still_one_bounded_line():
    class Hostile:
        def __str__(self) -> str:
            return "line one\nline two " + "z" * 1_000

    field = sidebar_key_field(Hostile())

    assert "\n" not in field
    assert len(field) <= ECHO_LIMIT + len("...")
    assert field.startswith("line one\\nline two ")


def test_known_field_names_are_never_altered_by_the_echo_rules():
    assert problem_of(hint({"group": "x\ny"})).field == "group"
    assert problem_of(hint({"order": "ten"})).field == "order"
    assert problem_of(hint("text")).field == "sidebar"
    assert problem_of({"github_wiki": "text"}).field == "github_wiki"


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
        "a\u2028b",
        "a\u2029b",
        "a\x0bb",
        "a\x0cb",
        "a\x85b",
        "a\x1cb",
        "a\x1db",
        "a\x1eb",
        "trailing\n",
    ],
)
def test_validate_hint_text_rejects_every_line_boundary(field, value):
    assert problem_of(hint({field: value})) == HintProblem(
        field=field, expected=TEXT_SHAPE
    )


@pytest.mark.parametrize("field", ["group", "label"])
def test_validate_hint_text_boundary_set_is_exactly_what_splitlines_splits_on(field):
    # Guards the hand-written boundary class against drifting from ``str.splitlines``:
    # a character is rejected inside a text exactly when it breaks a line.
    wrong = []
    for code in range(0x10000):
        if 0xD800 <= code <= 0xDFFF:  # lone surrogates are rejected for another reason
            continue
        text = f"a{chr(code)}b"
        rejected = isinstance(validate_hint(hint({field: text})), HintProblem)
        if rejected != (len(text.splitlines()) > 1):
            wrong.append(hex(code))

    assert wrong == []
    assert len("a\x1cb".splitlines()) == 2


@pytest.mark.parametrize("field", ["group", "label"])
@pytest.mark.parametrize("value", ["\ud800", "a\udfffb", "\ud83d", "x\udc00"])
def test_validate_hint_text_rejects_lone_surrogates_that_no_file_can_hold(field, value):
    assert problem_of(hint({field: value})) == HintProblem(
        field=field, expected=TEXT_SHAPE
    )


@pytest.mark.parametrize("field", ["group", "label"])
def test_validate_hint_text_accepts_astral_characters_and_counts_them_once(field):
    emoji = "\U0001f600"

    assert patch_of(hint({field: emoji * 80})) == HintPatch(**{field: emoji * 80})
    assert isinstance(validate_hint(hint({field: emoji * 81})), HintProblem)


@pytest.mark.parametrize("field", ["group", "label"])
@pytest.mark.parametrize("value", ["", " ", "   ", "\t", "\u00a0"])
def test_validate_hint_text_rejects_empty_and_whitespace_only(field, value):
    assert problem_of(hint({field: value})).field == field


@pytest.mark.parametrize("field", ["group", "label"])
@pytest.mark.parametrize("value", [5, 1.5, True, ["a"], {"a": 1}, b"bytes"])
def test_validate_hint_text_rejects_non_strings(field, value):
    assert problem_of(hint({field: value})) == HintProblem(
        field=field, expected=TEXT_SHAPE
    )


@pytest.mark.parametrize("field", ["group", "label"])
@pytest.mark.parametrize(
    "value",
    [
        ZERO_WIDTH,
        ZERO_WIDTH * 5,
        BOM,
        BIDI_OVERRIDE,
        SOFT_HYPHEN,
        f" {ZERO_WIDTH} ",
        f"{ZERO_WIDTH}{NBSP}{BOM}",
    ],
    ids=["zero-width", "zero-width-run", "bom", "bidi", "soft-hyphen", "padded", "mixed"],
)
def test_validate_hint_text_rejects_invisible_only_values(field, value):
    assert problem_of(hint({field: value})) == HintProblem(
        field=field, expected=TEXT_SHAPE
    )


@pytest.mark.parametrize("field", ["group", "label"])
@pytest.mark.parametrize("value", [f"a{ZERO_WIDTH}", f"{ZERO_WIDTH}a", f"{BIDI_OVERRIDE}x{BOM}"])
def test_validate_hint_text_accepts_invisible_characters_next_to_visible_text(field, value):
    assert isinstance(validate_hint(hint({field: value})), HintPatch)


def test_validate_hint_label_keeps_the_exact_value_including_padding():
    assert patch_of(hint({"label": "  Install  "})).label == "  Install  "


@pytest.mark.parametrize(
    ("value", "expected"),
    [("  Guides  ", "Guides"), ("Guides", "Guides"), ("\tGuides ", "Guides"), (f"{NBSP}G{NBSP}", "G")],
)
def test_validate_hint_group_is_normalized_by_stripping(value, expected):
    assert patch_of(hint({"group": value})).group == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (f"{ZERO_WIDTH}Guides{BOM}", "Guides"),
        (f"{BIDI_OVERRIDE} Guides {ZERO_WIDTH}", "Guides"),
        (f" {ZERO_WIDTH}{NBSP}G{BOM} ", "G"),
        (f"{SOFT_HYPHEN}{BOM}{ZERO_WIDTH}Guides", "Guides"),
        (f"Gu{ZERO_WIDTH}ides", f"Gu{ZERO_WIDTH}ides"),
        (f"{ZERO_WIDTH}Gu{BOM}ides{ZERO_WIDTH}", f"Gu{BOM}ides"),
    ],
    ids=["zero-width-and-bom", "bidi-and-spaces", "mixed", "soft-hyphen-run", "inner-kept", "inner-bom-kept"],
)
def test_validate_hint_group_is_trimmed_of_edge_whitespace_and_invisible_characters(value, expected):
    assert patch_of(hint({"group": value})).group == expected


@pytest.mark.parametrize("value", [f"{ZERO_WIDTH}Install", f"Install{BOM}", f" {BIDI_OVERRIDE}Install "])
def test_validate_hint_label_keeps_invisible_edges_exactly(value):
    assert patch_of(hint({"label": value})).label == value


def test_group_spellings_differing_only_in_invisible_padding_merge_into_one_group():
    patches, rejected = collect(
        [
            HintSource("op1", "A.md", hint({"group": f"{ZERO_WIDTH}Guides{BOM}"})),
            HintSource("op2", "B.md", hint({"group": "Guides"})),
        ]
    )

    assert rejected == ()
    assert merge({}, patches) == {"A": Placement(group="Guides"), "B": Placement(group="Guides")}


def test_padded_and_unpadded_group_spellings_merge_into_one_group():
    patches, rejected = collect(
        [
            HintSource("op1", "A.md", hint({"group": " Guides "})),
            HintSource("op2", "B.md", hint({"group": "Guides"})),
        ]
    )

    assert rejected == ()
    assert merge({}, patches) == {"A": Placement(group="Guides"), "B": Placement(group="Guides")}


# -- validate_hint: order -----------------------------------------------------


@pytest.mark.parametrize("value", [0, 1, -7, 1_000_000, -1_000_000])
def test_validate_hint_order_accepts_integers(value):
    assert patch_of(hint({"order": value})).order == value


@pytest.mark.parametrize("value", [True, False, "ten", "10", 1.5, 2.0, [1], {}])
def test_validate_hint_order_rejects_booleans_and_non_integers(value):
    assert problem_of(hint({"order": value})) == HintProblem(
        field="order", expected=ORDER_SHAPE
    )


@pytest.mark.parametrize(
    "value",
    [1_000_001, -1_000_001, 10**12, 10**5000, -(10**5000)],
    ids=["over", "under", "trillion", "huge", "huge-negative"],
)
def test_validate_hint_order_rejects_integers_outside_the_sane_range(value):
    assert problem_of(hint({"order": value})) == HintProblem(
        field="order", expected=ORDER_SHAPE
    )


def test_validate_hint_order_huge_integer_is_rejected_without_converting_it_to_text():
    # A >4300-digit int makes str()/json.dumps raise; the bound must reject it first.
    outcome = validate_hint(hint({"order": 10**5000, "group": "G"}))

    assert outcome == HintProblem(field="order", expected=ORDER_SHAPE)


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


# -- render_hint_warning ------------------------------------------------------

ORDER_REJECTION = RejectedHint(
    operation_id="op-1", page="Install", problem=HintProblem("order", ORDER_SHAPE)
)


def rejection_of(page: str, metadata: dict[str, Any], operation_id: str = "op") -> RejectedHint:
    sources = [HintSource(operation_id, f"{page}.md", metadata)]
    rejected = collect(sources)[1]
    assert len(rejected) == 1
    return rejected[0]


def test_render_hint_warning_exact_message_for_an_order_problem():
    assert render_hint_warning(ORDER_REJECTION) == (
        "[github_wiki:sidebar.invalid_hint] Sidebar hint ignored: 'order' is invalid, "
        f"expected {ORDER_SHAPE}. op='op-1' page='Install' field='order'. "
        f"Hint: {CODES['sidebar.invalid_hint'].default_hint}."
    )


@pytest.mark.parametrize(
    ("rejected", "field", "expected"),
    [
        (ORDER_REJECTION, "order", ORDER_SHAPE),
        (
            RejectedHint("op-2", "Setup", HintProblem("group", TEXT_SHAPE)),
            "group",
            TEXT_SHAPE,
        ),
        (
            RejectedHint("op-3", "Setup", HintProblem("weight", "only group, order and label are allowed")),
            "weight",
            "only group, order and label are allowed",
        ),
    ],
    ids=["order", "group", "unknown-key"],
)
def test_render_hint_warning_names_the_operation_page_field_and_expected_shape(
    rejected, field, expected
):
    message = render_hint_warning(rejected)

    assert message.startswith("[github_wiki:sidebar.invalid_hint] Sidebar hint ignored: ")
    assert f"'{field}'" in message
    assert expected in message
    assert f"op='{rejected.operation_id}'" in message
    assert f"page='{rejected.page}'" in message
    assert f"field='{field}'" in message


def test_render_hint_warning_for_a_wrong_order_type_names_the_page_order_and_integer():
    message = render_hint_warning(rejection_of("Install", hint({"group": "Guides", "order": "ten"})))

    assert "page='Install'" in message
    assert "field='order'" in message
    assert "integer" in message


def test_render_hint_warning_for_a_typo_lists_sidebar_as_the_valid_key():
    message = render_hint_warning(rejection_of("Install", {"github_wiki": {"sidbar": {}}}))

    assert "field='github_wiki.sidbar'" in message
    assert "'sidebar'" in message


def test_render_hint_warning_for_an_excluded_page_names_the_page():
    message = render_hint_warning(rejection_of("_Footer", hint({"group": "X"})))

    assert "page='_Footer'" in message
    assert "does not start with '_'" in message


def test_render_hint_warning_context_carries_op_page_and_field_in_that_order():
    message = render_hint_warning(ORDER_REJECTION)

    assert message.index("op='op-1'") < message.index("page='Install'") < message.index("field='order'")


def test_render_hint_warning_stays_one_escaped_line_for_hostile_text():
    hostile = RejectedHint(
        operation_id="a\nb\x1b[31m",
        page="x\ry\u202e",
        problem=HintProblem("w\nz", "an\nint"),
    )

    message = render_hint_warning(hostile)

    assert "\n" not in message and "\r" not in message and "\x1b" not in message
    assert "\u202e" not in message
    assert "a | b" in message
    assert r"\x1b[31m" in message
    assert r"\u202e" in message
    assert message.count("Hint:") == 1


def test_render_hint_warning_renders_a_bounded_unknown_key_echo_unchanged():
    long_key = "k" * 200
    message = render_hint_warning(rejection_of("P", hint({long_key: 1})))

    assert "k" * 80 + "..." in message
    assert "k" * 81 not in message
