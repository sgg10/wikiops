"""Unit tests for the pure sidebar renderer of ``github_wiki`` (``sidebar.py``).

Covers marker classification (SB3/SB4), default and hinted ordering (SB6/SB7), labels,
the golden byte layout of the managed file (SB5) and the escaping that keeps labels and
group names from injecting markdown/HTML or forging the machine record. Everything here
is pure: no filesystem, process or backend access.
"""

from __future__ import annotations

import json
import re
import string

import pytest

from wikiops.providers.github_wiki import sidebar
from wikiops.providers.github_wiki.errors import GithubWikiError
from wikiops.providers.github_wiki.sidebar import (
    MARKER,
    Placement,
    classify,
    display_label,
    order,
    render,
)
from wikiops.providers.github_wiki.text import (
    CONTROL_CHARACTERS,
    INVISIBLE_CHARACTERS,
    escape_unsafe_characters,
)

RECORD_PREFIX = " <!-- wikiops:entry "
RECORD_SUFFIX = " -->"
# JSON escapes the record uses (spelled without a literal escape sequence in the source).
HYPHEN = chr(92) + "u002d"
LESS = chr(92) + "u003c"
GREATER = chr(92) + "u003e"
E_ACUTE = chr(92) + "u00e9"
E_DIAERESIS = chr(92) + "u00eb"
ZERO_WIDTH = chr(0x200B)
BOM = chr(0xFEFF)
NBSP = chr(0x00A0)


def p(**keys: object) -> Placement:
    return Placement(**keys)  # type: ignore[arg-type]


def entry(display: str, target: str, record: str) -> str:
    return f"- [{display}]({target}){RECORD_PREFIX}{record}{RECORD_SUFFIX}"


def listing(*lines: str) -> str:
    return "\n".join(lines) + "\n"


def entry_lines(text: str) -> list[str]:
    return [line for line in text.split("\n") if line.startswith("- ")]


def record_of(line: str) -> str:
    _, separator, rest = line.rpartition(RECORD_PREFIX)
    assert separator, line
    assert rest.endswith(RECORD_SUFFIX), line
    return rest[: -len(RECORD_SUFFIX)]


def display_of(line: str, target: str) -> str:
    marker = f"]({target}){RECORD_PREFIX}"
    assert line.startswith("- [")
    return line[len("- [") :].partition(marker)[0]


def unescape_markdown(display: str) -> str:
    """Undo CommonMark backslash escapes; fail on any unescaped ASCII punctuation."""
    out: list[str] = []
    chars = iter(display)
    for char in chars:
        if char == "\\":
            following = next(chars, None)
            assert following is not None and following in string.punctuation, display
            out.append(following)
        else:
            assert char not in string.punctuation, f"unescaped {char!r} in {display!r}"
            out.append(char)
    return "".join(out)


def ordered(pages: list[str], placements: dict[str, Placement] | None = None) -> list[str]:
    """The flat page order of the ungrouped list followed by each group, in order."""
    return [stem for _, stems in order(pages, placements or {}) for stem in stems]


# -- classify (SB3, SB4, G7) ----------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        MARKER,
        MARKER + "\n",
        MARKER + "\n\n- [Home](Home)\n",
        MARKER + "\r\n- [Home](Home)\r\n",
        MARKER + "\r",
    ],
    ids=["marker-only", "lf", "lf-with-body", "crlf", "cr-without-newline"],
)
def test_classify_marker_on_the_first_line_is_managed(text):
    assert classify(text) == "managed"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "\n" + MARKER,
        " " + MARKER + "\n",
        MARKER + " \n",
        MARKER + " trailing text\n",
        MARKER + "x\n",
        "x" + MARKER + "\n",
        "﻿" + MARKER + "\n",
        MARKER + "\r\r\n",
        "# Title\n" + MARKER + "\n",
        MARKER.upper() + "\n",
        "<!-- wikiops:managed sidebar -- >\n",
        "# My own sidebar\n- [Home](Home)\n",
    ],
    ids=[
        "empty",
        "marker-not-on-first-line",
        "leading-space",
        "trailing-space",
        "trailing-text",
        "glued-suffix",
        "glued-prefix",
        "bom",
        "two-carriage-returns",
        "marker-on-second-line",
        "uppercased",
        "near-miss-comment",
        "user-written",
    ],
)
def test_classify_everything_but_the_exact_first_line_is_unmanaged(text):
    assert classify(text) == "unmanaged"


def test_classify_marker_constant_is_the_documented_line():
    assert MARKER == "<!-- wikiops:managed sidebar -->"


# -- display_label ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("page", "placement", "expected"),
    [
        ("Getting-Started", Placement(), "Getting Started"),
        ("Home", Placement(), "Home"),
        ("a--b", Placement(), "a  b"),
        ("Install", Placement(label="Install guide"), "Install guide"),
        ("Getting-Started", Placement(label="Start here"), "Start here"),
        ("Getting-Started", Placement(group="G", order=3), "Getting Started"),
        ("Getting-Started", Placement(label=None), "Getting Started"),
    ],
)
def test_display_label_is_the_label_hint_or_the_page_with_spaces(page, placement, expected):
    assert display_label(page, placement) == expected


BLANK_VALUES = ["", " ", "   ", "\t", NBSP, ZERO_WIDTH, ZERO_WIDTH * 4, BOM, f" {ZERO_WIDTH} "]
BLANK_IDS = ["empty", "space", "spaces", "tab", "nbsp", "zero-width", "zero-width-run", "bom", "padded"]


@pytest.mark.parametrize("label", BLANK_VALUES, ids=BLANK_IDS)
def test_display_label_falls_back_to_the_page_when_the_label_is_blank(label):
    assert display_label("Getting-Started", Placement(label=label)) == "Getting Started"


@pytest.mark.parametrize(
    ("page", "expected"),
    [("-", "-"), ("--", "--"), (ZERO_WIDTH, ZERO_WIDTH), ("a-b", "a b")],
    ids=["hyphen", "hyphens", "zero-width-page", "ordinary"],
)
def test_display_label_never_derives_a_blank_text_from_the_page(page, expected):
    assert display_label(page, Placement()) == expected


def test_display_label_keeps_a_label_with_visible_text_exactly():
    assert display_label("P", Placement(label=f" a{ZERO_WIDTH} ")) == f" a{ZERO_WIDTH} "


# -- order (SB6, SB7) ---------------------------------------------------------------


def test_order_is_empty_without_pages():
    assert order([], {}) == ()


def test_order_default_is_home_first_then_alphabetical_case_insensitive():
    assert order(["beta", "Alpha", "Home"], {}) == ((None, ("Home", "Alpha", "beta")),)


def test_order_alphabetical_uses_casefold_before_the_exact_name():
    # Exact ordering would put "Beta" before "alpha"; casefold puts "alpha" first.
    assert ordered(["Beta", "alpha"]) == ["alpha", "Beta"]


def test_order_alphabetical_ties_break_by_exact_name():
    assert ordered(["alpha", "Alpha"]) == ["Alpha", "alpha"]


def test_order_alphabetical_key_is_the_display_label_not_the_page_name():
    # By name "a!" < "a-b"; by display label "a b" < "a!" (a space sorts before "!").
    assert ordered(["a!", "a-b"]) == ["a-b", "a!"]


def test_order_alphabetical_uses_the_label_hint():
    assert ordered(["Alpha", "Zed"], {"Zed": p(label="Aardvark")}) == ["Zed", "Alpha"]


def test_order_home_pin_requires_the_exact_name():
    assert ordered(["home", "Alpha"]) == ["Alpha", "home"]
    assert ordered(["Alpha", "Home"]) == ["Home", "Alpha"]


def test_order_home_precedes_a_page_with_order_one():
    assert ordered(["Intro", "Home"], {"Intro": p(order=1)}) == ["Home", "Intro"]


def test_order_label_only_hint_keeps_the_home_pin():
    assert ordered(["Alpha", "Home"], {"Home": p(label="Zebra")}) == ["Home", "Alpha"]


def test_order_home_with_an_order_loses_the_pin():
    placements = {"Home": p(order=5), "Intro": p(order=1)}
    assert ordered(["Home", "Intro"], placements) == ["Intro", "Home"]


def test_order_home_with_a_group_loses_the_pin_inside_the_group():
    placements = {"Home": p(group="G"), "Alpha": p(group="G")}
    assert order(["Home", "Alpha"], placements) == (("G", ("Alpha", "Home")),)


def test_order_home_with_order_zero_still_loses_the_pin():
    placements = {"Home": p(order=0), "Intro": p(order=-1)}
    assert ordered(["Home", "Intro"], placements) == ["Intro", "Home"]


def test_order_lists_ungrouped_pages_before_the_groups():
    placements = {
        "Install": p(group="Guides", order=10),
        "Upgrade": p(group="Guides", order=20),
    }
    assert order(["Upgrade", "Overview", "Install"], placements) == (
        (None, ("Overview",)),
        ("Guides", ("Install", "Upgrade")),
    )


def test_order_omits_the_ungrouped_list_when_every_page_is_grouped():
    assert order(["Install"], {"Install": p(group="Guides")}) == (("Guides", ("Install",)),)


def test_order_groups_by_their_lowest_order():
    placements = {"a1": p(group="A", order=5), "b1": p(group="B", order=1)}
    assert order(["a1", "b1"], placements) == (("B", ("b1",)), ("A", ("a1",)))


def test_order_group_lowest_order_ignores_later_pages_of_the_group():
    placements = {
        "a1": p(group="A", order=9),
        "a2": p(group="A", order=2),
        "b1": p(group="B", order=3),
    }
    assert [group for group, _ in order(["a1", "a2", "b1"], placements)] == ["A", "B"]


def test_order_group_with_only_unordered_pages_counts_last():
    placements = {"x": p(group="A"), "y": p(group="Z", order=100)}
    assert [group for group, _ in order(["x", "y"], placements)] == ["Z", "A"]


def test_order_group_with_an_unordered_page_uses_the_ordered_pages_minimum():
    placements = {
        "a1": p(group="A", order=5),
        "a2": p(group="A"),
        "b1": p(group="B", order=3),
    }
    assert [group for group, _ in order(["a1", "a2", "b1"], placements)] == ["B", "A"]


def test_order_group_with_order_zero_sorts_before_unordered_and_after_negative():
    placements = {
        "z": p(group="Z", order=-1),
        "m": p(group="M", order=0),
        "a": p(group="A"),
    }
    assert [group for group, _ in order(["a", "m", "z"], placements)] == ["Z", "M", "A"]


def test_order_group_ties_break_by_casefold_then_exact_name():
    placements = {"x": p(group="B", order=1), "y": p(group="a", order=1)}
    assert [group for group, _ in order(["x", "y"], placements)] == ["a", "B"]
    placements = {"x": p(group="a", order=1), "y": p(group="A", order=1)}
    assert [group for group, _ in order(["x", "y"], placements)] == ["A", "a"]


def test_order_pages_without_order_come_after_ordered_ones():
    assert ordered(["Alpha", "Zed"], {"Zed": p(order=3)}) == ["Zed", "Alpha"]


def test_order_ordered_pages_sort_by_order_then_label_then_name():
    placements = {
        "late": p(order=2, label="a"),
        "x": p(order=1, label="b"),
        "y": p(order=1, label="a"),
        "pb": p(order=1, label="L"),
        "pa": p(order=1, label="L"),
    }
    assert ordered(["late", "x", "y", "pb", "pa"], placements) == [
        "y",
        "x",
        "pa",
        "pb",
        "late",
    ]


def test_order_negative_orders_sort_before_positive_ones():
    placements = {"a": p(order=1), "b": p(order=-3)}
    assert ordered(["a", "b"], placements) == ["b", "a"]


def test_order_ignores_placements_of_pages_that_are_not_listed():
    placements = {"Ghost": p(group="G", order=1), "Alpha": p(order=1)}
    assert order(["Alpha"], placements) == ((None, ("Alpha",)),)


def test_order_lists_a_duplicated_page_once():
    assert order(["Home", "Alpha", "Home"], {}) == ((None, ("Home", "Alpha")),)


def test_order_groups_with_different_letter_case_are_different_groups():
    placements = {"x": p(group="g"), "y": p(group="G")}
    assert order(["x", "y"], placements) == (("G", ("y",)), ("g", ("x",)))


@pytest.mark.parametrize("label", BLANK_VALUES, ids=BLANK_IDS)
def test_order_sorts_a_blank_label_by_the_page_derived_label(label):
    assert order(["a", "b"], {"b": p(label=label)}) == ((None, ("a", "b")),)
    assert order(["b", "a"], {"a": p(label=label)}) == ((None, ("a", "b")),)


@pytest.mark.parametrize("group", BLANK_VALUES, ids=BLANK_IDS)
def test_order_treats_a_blank_group_as_ungrouped(group):
    assert order(["x", "y"], {"x": p(group=group)}) == ((None, ("x", "y")),)


@pytest.mark.parametrize("group", BLANK_VALUES, ids=BLANK_IDS)
def test_order_blank_group_keeps_the_home_pin_and_a_blank_order_rank(group):
    assert order(["Alpha", "Home"], {"Home": p(group=group)}) == ((None, ("Home", "Alpha")),)


def test_order_padded_and_unpadded_groups_are_one_group_named_without_padding():
    placements = {"x": p(group=" Guides "), "y": p(group="Guides"), "z": p(group="Guides ")}

    assert order(["z", "y", "x"], placements) == (("Guides", ("x", "y", "z")),)


def test_order_group_padding_does_not_change_the_group_rank():
    placements = {"x": p(group=" B", order=1), "y": p(group="A", order=5)}

    assert order(["x", "y"], placements) == (("B", ("x",)), ("A", ("y",)))


def test_order_groups_that_only_differ_in_padding_do_not_split_the_minimum_order():
    placements = {"x": p(group=" G", order=9), "y": p(group="G", order=2), "z": p(group="H", order=5)}

    assert order(["x", "y", "z"], placements) == (("G", ("y", "x")), ("H", ("z",)))


# -- render golden bytes (SB3, SB5, D7) ---------------------------------------------


def test_render_without_pages_is_the_marker_line_alone():
    assert render([], {}) == MARKER + "\n"


def test_render_ungrouped_pages_golden():
    assert render(["Overview", "Home"], {}) == listing(
        MARKER,
        "",
        entry("Home", "Home", '{"page":"Home"}'),
        entry("Overview", "Overview", '{"page":"Overview"}'),
    )


def test_render_groups_and_hints_golden():
    placements = {
        "Install": p(group="Guides", order=10, label="Install guide"),
        "Upgrade": p(group="Guides", order=20),
    }
    assert render(["Upgrade", "Overview", "Home", "Install"], placements) == listing(
        MARKER,
        "",
        entry("Home", "Home", '{"page":"Home"}'),
        entry("Overview", "Overview", '{"page":"Overview"}'),
        "",
        "**Guides**",
        "",
        entry(
            "Install guide",
            "Install",
            '{"group":"Guides","label":"Install guide","order":10,"page":"Install"}',
        ),
        entry("Upgrade", "Upgrade", '{"group":"Guides","order":20,"page":"Upgrade"}'),
    )


def test_render_two_groups_are_separated_by_a_blank_line():
    placements = {"a": p(group="B", order=1), "b": p(group="A", order=2)}
    assert render(["a", "b"], placements) == listing(
        MARKER,
        "",
        "**B**",
        "",
        entry("a", "a", '{"group":"B","order":1,"page":"a"}'),
        "",
        "**A**",
        "",
        entry("b", "b", '{"group":"A","order":2,"page":"b"}'),
    )


def test_render_default_label_replaces_hyphens_and_keeps_the_page_as_target():
    # The record escapes every "-" of a string value as - (see the injection tests).
    assert render(["Getting-Started"], {}) == listing(
        MARKER,
        "",
        entry("Getting Started", "Getting-Started", f'{{"page":"Getting{HYPHEN}Started"}}'),
    )


def test_render_ends_with_exactly_one_trailing_newline():
    text = render(["A", "B"], {"B": p(group="G")})
    assert text.endswith("-->\n")
    assert not text.endswith("\n\n")


def test_render_is_ascii_only_in_the_record():
    line = entry_lines(render(["P"], {"P": p(label="é", group="Zoë")}))[0]
    record = record_of(line)
    assert record.isascii()
    assert record == f'{{"group":"Zo{E_DIAERESIS}","label":"{E_ACUTE}","page":"P"}}'
    assert json.loads(record) == {"group": "Zoë", "label": "é", "page": "P"}


def test_render_record_keys_are_sorted_with_compact_separators():
    record = record_of(
        entry_lines(render(["P"], {"P": p(order=2, label="L", group="G")}))[0]
    )
    assert record == '{"group":"G","label":"L","order":2,"page":"P"}'


def test_render_record_holds_only_the_keys_that_are_set():
    assert record_of(entry_lines(render(["P"], {"P": p(order=0)}))[0]) == (
        '{"order":0,"page":"P"}'
    )
    assert record_of(entry_lines(render(["P"], {"P": p(group="G")}))[0]) == (
        '{"group":"G","page":"P"}'
    )


def test_render_display_label_comes_from_the_label_hint():
    line = entry_lines(render(["Install"], {"Install": p(label="Install guide")}))[0]
    assert line.startswith("- [Install guide](Install) ")


@pytest.mark.parametrize(
    ("page", "target"),
    [
        ("Home", "Home"),
        ("Getting-Started", "Getting-Started"),
        ("a b", "a%20b"),
        ("Guía de uso", "Gu%C3%ADa%20de%20uso"),
        ("A&B", "A%26B"),
        ("a/b", "a%2Fb"),
        ("/etc/passwd", "%2Fetc%2Fpasswd"),
        ("https://example.com/x", "https%3A%2F%2Fexample.com%2Fx"),
        ("a)b(c", "a%29b%28c"),
        ("100%", "100%25"),
        ("a#b?c", "a%23b%3Fc"),
    ],
)
def test_render_targets_are_percent_encoded_bare_names(page, target):
    line = entry_lines(render([page], {}))[0]
    assert f"]({target}){RECORD_PREFIX}" in line
    assert not target.startswith("/")
    assert "://" not in target


def test_render_guards_every_target_with_the_link_guard(monkeypatch):
    seen: list[str] = []

    def guard(link: str) -> str:
        seen.append(link)
        return link

    monkeypatch.setattr(sidebar, "guard_link", guard)
    render(["Guía", "Home"], {})
    assert seen == ["Home", "Gu%C3%ADa"]


def test_render_link_guard_failure_propagates(monkeypatch):
    def guard(link: str) -> str:
        raise GithubWikiError("link.root_anchored", "A generated link starts with '/'")

    monkeypatch.setattr(sidebar, "guard_link", guard)
    with pytest.raises(GithubWikiError) as caught:
        render(["Home"], {})
    assert caught.value.code == "link.root_anchored"


def test_render_is_deterministic_for_any_input_order():
    placements = {"b": p(group="G", order=1), "c": p(label="Zed")}
    first = render(["a", "b", "c"], placements)
    assert render(["c", "a", "b"], placements) == first
    assert render(["a", "b", "c"], placements) == first


@pytest.mark.parametrize("label", BLANK_VALUES, ids=BLANK_IDS)
def test_render_blank_label_shows_the_page_derived_text_and_records_no_label(label):
    text = render(["Getting-Started"], {"Getting-Started": p(label=label)})

    assert text == render(["Getting-Started"], {})
    assert display_of(entry_lines(text)[0], "Getting-Started") == "Getting Started"
    assert "label" not in record_of(entry_lines(text)[0])


@pytest.mark.parametrize("group", BLANK_VALUES, ids=BLANK_IDS)
def test_render_blank_group_is_ungrouped_without_a_heading_and_records_no_group(group):
    text = render(["P", "Q"], {"P": p(group=group, order=3)})

    assert text == render(["P", "Q"], {"P": p(order=3)})
    assert "**" not in text
    assert "group" not in text


@pytest.mark.parametrize("page", ["-", "--", ZERO_WIDTH])
def test_render_never_produces_an_empty_link_text(page):
    line = entry_lines(render([page], {}))[0]

    assert not line.startswith("- []")


def test_render_padded_and_unpadded_groups_share_one_heading_and_one_recorded_name():
    placements = {"x": p(group=" Guides "), "y": p(group="Guides")}

    assert render(["x", "y"], placements) == listing(
        MARKER,
        "",
        "**Guides**",
        "",
        entry("x", "x", '{"group":"Guides","page":"x"}'),
        entry("y", "y", '{"group":"Guides","page":"y"}'),
    )


def test_render_group_padding_is_not_kept_in_the_record():
    line = entry_lines(render(["P"], {"P": p(group="  Guides  ")}))[0]

    assert json.loads(record_of(line))["group"] == "Guides"


# -- display escaping (injection: label and group) ------------------------------------

DISPLAY_CASES = [
    ("]", r"\]"),
    ("[[x]]", r"\[\[x\]\]"),
    ("a|b", r"a\|b"),
    ("*b*", r"\*b\*"),
    ("_i_", r"\_i\_"),
    ("`c`", r"\`c\`"),
    ("a\\b", r"a\\b"),
    ("a&amp;b", r"a\&amp\;b"),
    ("Hi!", r"Hi\!"),
    ("# Title", r"\# Title"),
    ("<script>alert(1)</script>", r"\<script\>alert\(1\)\<\/script\>"),
    ("--", r"\-\-"),
    ("-->", r"\-\-\>"),
    ("--!>", r"\-\-\!\>"),
    ("<!--", r"\<\!\-\-"),
    ("[x](javascript:alert(1))", r"\[x\]\(javascript\:alert\(1\)\)"),
    ("![img](http://e/x.png)", r"\!\[img\]\(http\:\/\/e\/x\.png\)"),
    ("  padded  ", "padded"),
]


@pytest.mark.parametrize(("raw", "expected"), DISPLAY_CASES)
def test_label_display_is_escaped_to_the_exact_text(raw, expected):
    line = entry_lines(render(["P"], {"P": p(label=raw)}))[0]
    assert display_of(line, "P") == expected
    assert unescape_markdown(display_of(line, "P")) == raw.strip()


@pytest.mark.parametrize(("raw", "expected"), DISPLAY_CASES)
def test_group_display_is_escaped_to_the_exact_text(raw, expected):
    lines = render(["P"], {"P": p(group=raw)}).split("\n")
    assert lines[2] == f"**{expected}**"


@pytest.mark.parametrize(
    "char", [c for c in string.punctuation], ids=[f"U+{ord(c):04X}" for c in string.punctuation]
)
def test_every_ascii_punctuation_character_is_backslash_escaped(char):
    line = entry_lines(render(["P"], {"P": p(label=f"a{char}b")}))[0]
    assert display_of(line, "P") == f"a\\{char}b"


def test_label_display_never_holds_unescaped_markup_characters():
    nasty = "[[Page|x]] <b>&</b> ]] [[ ![i](u) `k` *e* _u_ #h"
    line = entry_lines(render(["P"], {"P": p(label=nasty)}))[0]
    display = display_of(line, "P")
    assert unescape_markdown(display) == nasty
    assert "[[" not in display.replace(r"\[", "")
    assert "]]" not in display.replace(r"\]", "")
    for char in "<>&":
        assert re.search(rf"(?<!\\){re.escape(char)}", display) is None


@pytest.mark.parametrize(
    "raw",
    ["\x1b[31mred", "a‮b", "a\x00b", "a\x7fb", "a\x85b", "x​y", "﻿bom"],
    ids=["ansi", "bidi-override", "nul", "del", "c1", "zero-width", "bom"],
)
def test_label_display_escapes_control_and_bidi_characters(raw):
    line = entry_lines(render(["P"], {"P": p(label=raw)}))[0]
    display = display_of(line, "P")
    assert CONTROL_CHARACTERS.search(display) is None
    assert INVISIBLE_CHARACTERS.search(display) is None
    assert unescape_markdown(display) == escape_unsafe_characters(raw.strip())


def test_label_display_shows_the_bidi_override_as_a_visible_escape():
    line = entry_lines(render(["P"], {"P": p(label="a‮b")}))[0]
    assert display_of(line, "P") == r"a\\u202eb"


@pytest.mark.parametrize("raw", ["a\nb", "a\rb", "a\r\nb"], ids=["lf", "cr", "crlf"])
def test_raw_newlines_in_label_or_group_cannot_break_the_line_structure(raw):
    text = render(["P"], {"P": p(label=raw, group=raw)})
    # marker, blank, heading, blank, entry: five lines, each ended by one newline.
    assert text.count("\n") == 5
    assert text.count("\r") == 0
    assert len(entry_lines(text)) == 1


def test_display_strip_does_not_change_the_recorded_value():
    line = entry_lines(render(["P"], {"P": p(label="  padded  ")}))[0]
    assert display_of(line, "P") == "padded"
    assert json.loads(record_of(line))["label"] == "  padded  "


# -- record escaping (injection: forge or break out) ---------------------------------


def test_record_of_a_closing_comment_group_cannot_break_out():
    group = "x --> <script>"
    text = render(["P"], {"P": p(group=group)})
    (line,) = entry_lines(text)
    payload = record_of(line)
    assert line.count(RECORD_PREFIX) == 1
    assert line.count("<!--") == 1
    assert line.endswith(RECORD_SUFFIX)
    assert line.count("-->") == 1
    for forbidden in ("--", "<", ">"):
        assert forbidden not in payload
    assert json.loads(payload)["group"] == group


def test_record_of_a_double_hyphen_label_is_a_valid_invisible_comment():
    (line,) = entry_lines(render(["P"], {"P": p(label="a--b")}))
    payload = record_of(line)
    assert "--" not in payload
    assert payload == f'{{"label":"a{HYPHEN}{HYPHEN}b","page":"P"}}'
    assert json.loads(payload)["label"] == "a--b"


@pytest.mark.parametrize(
    ("label", "escape"),
    [("a-b", f"a{HYPHEN}b"), ("a<b", f"a{LESS}b"), ("a>b", f"a{GREATER}b")],
    ids=["hyphen", "less-than", "greater-than"],
)
def test_record_escapes_each_comment_significant_character(label, escape):
    (line,) = entry_lines(render(["P"], {"P": p(label=label)}))
    assert record_of(line) == '{"label":"' + escape + '","page":"P"}'
    assert json.loads(record_of(line))["label"] == label


@pytest.mark.parametrize(
    "text",
    ["-->", "--!>", "<!--", "<!-- wikiops:entry {} -->", "--", ">", "<", "-"],
)
def test_record_never_holds_a_comment_delimiter_for_any_string_value(text):
    (line,) = entry_lines(render(["P"], {"P": p(label=text, group=text)}))
    payload = record_of(line)
    assert "--" not in payload
    assert "<" not in payload
    assert ">" not in payload
    assert line.count(RECORD_PREFIX) == 1
    assert json.loads(payload)["label"] == text
    assert json.loads(payload)["group"] == text


def test_record_of_a_forged_second_record_in_a_label_stays_inert():
    forged = 'x --> <!-- wikiops:entry {"page":"Evil"} -->'
    (line,) = entry_lines(render(["P"], {"P": p(label=forged)}))
    assert line.count(RECORD_PREFIX) == 1
    assert line.count("<!--") == 1
    assert json.loads(record_of(line))["page"] == "P"


def test_record_escapes_the_page_stem_too():
    (line,) = entry_lines(render(["a--b"], {}))
    assert record_of(line) == f'{{"page":"a{HYPHEN}{HYPHEN}b"}}'


@pytest.mark.parametrize("value", [-5, -1, 0, 7])
def test_record_order_renders_as_a_plain_json_integer(value):
    (line,) = entry_lines(render(["P"], {"P": p(order=value, group="-")}))
    payload = record_of(line)
    assert f'"order":{value},' in payload
    assert "--" not in payload
    assert json.loads(payload)["order"] == value


def test_record_negative_order_is_not_escaped_like_a_string_hyphen():
    (line,) = entry_lines(render(["P"], {"P": p(order=-5)}))
    assert record_of(line) == '{"order":-5,"page":"P"}'


def test_record_json_escaping_neutralizes_a_raw_newline():
    (line,) = entry_lines(render(["P"], {"P": p(group="a\nb")}))
    assert record_of(line) == r'{"group":"a\nb","page":"P"}'
