"""Unit tests for the bounded sidebar parser of ``github_wiki`` (``sidebar.py``).

Covers the recovery of page placement from a previous managed ``_Sidebar.md`` (SB8):
unmanaged and oversized files are never parsed, malformed or hand-edited entries fall
back to defaults without failing, the parse is linear in the input, and
``render -> parse -> render`` is byte-identical. Everything here is pure: no
filesystem, process or backend access.
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable
from typing import Any

import pytest

from wikiops.providers.github_wiki.sidebar import (
    MARKER,
    MAX_PARSE_BYTES,
    HintPatch,
    Placement,
    classify,
    merge,
    parse,
    render,
    validate_hint,
)

PREFIX = " <!-- wikiops:entry "
SUFFIX = " -->"
SEED_RANGE = range(200)
# JSON escape of a zero-width space (spelled without a literal escape in the source).
ZW_ESCAPE = chr(92) + "u200b"


def p(group: str | None = None, order: int | None = None, label: str | None = None) -> Placement:
    return Placement(group=group, order=order, label=label)


def line(record: str, label: str = "x", target: str = "x") -> str:
    return f"- [{label}]({target}){PREFIX}{record}{SUFFIX}"


def managed(*lines: str) -> str:
    return "\n".join([MARKER, "", *lines]) + "\n"


def record_for(**keys: object) -> str:
    return json.dumps(keys, separators=(",", ":"), sort_keys=True)


INSTALL = line(record_for(page="Install", group="Guides", order=10))
INSTALL_PLACEMENT = {"Install": p(group="Guides", order=10)}


def assert_all_valid(parsed: dict[str, Placement]) -> None:
    """Every parsed entry holds exactly what a valid hint could hold."""
    for stem, placement in parsed.items():
        assert isinstance(stem, str) and stem
        keys = {
            key: value
            for key, value in (
                ("group", placement.group),
                ("label", placement.label),
                ("order", placement.order),
            )
            if value is not None
        }
        assert isinstance(validate_hint({"github_wiki": {"sidebar": keys}}), HintPatch)


# -- unmanaged text is never parsed --------------------------------------------


def test_parse_managed_text_recovers_the_placement_of_an_entry():
    assert parse(managed(INSTALL)) == INSTALL_PLACEMENT


@pytest.mark.parametrize(
    "text",
    [
        "",
        "\n",
        "# My own sidebar\n" + INSTALL + "\n",
        INSTALL + "\n",
        " " + MARKER + "\n" + INSTALL + "\n",
        MARKER + " trailing\n" + INSTALL + "\n",
        "\ufeff" + MARKER + "\n" + INSTALL + "\n",
        MARKER.upper() + "\n" + INSTALL + "\n",
        "\n" + MARKER + "\n" + INSTALL + "\n",
        "intro\n" + MARKER + "\n" + INSTALL + "\n",
        MARKER[:-1] + "\n" + INSTALL + "\n",
        MARKER + "\r\r\n" + INSTALL + "\n",
        MARKER + "\r" + INSTALL + "\n",
    ],
)
def test_parse_unmanaged_text_is_never_parsed(text):
    assert classify(text) == "unmanaged"
    assert parse(text) == {}


def test_parse_marker_alone_is_managed_and_has_no_entries():
    assert classify(MARKER) == "managed"
    assert parse(MARKER) == {}
    assert parse(MARKER + "\n") == {}


def test_parse_marker_alone_is_not_confused_with_an_entry():
    assert parse(MARKER + "\n\n") == {}


# -- the size bound is measured in utf-8 bytes ----------------------------------


def padded_to(base: str, size: int, filler: str = "x") -> str:
    """Return ``base`` plus trailing filler lines so the utf-8 length is exactly ``size``."""
    missing = size - len(base.encode())
    width = len(filler.encode())
    assert missing >= width
    return base + "x" * (missing % width) + filler * (missing // width)


def test_parse_accepts_input_of_exactly_the_limit():
    text = padded_to(managed(INSTALL), MAX_PARSE_BYTES)

    assert len(text.encode()) == MAX_PARSE_BYTES == 1024 * 1024
    assert parse(text) == INSTALL_PLACEMENT


def test_parse_rejects_input_one_byte_over_the_limit():
    text = padded_to(managed(INSTALL), MAX_PARSE_BYTES + 1)

    assert len(text.encode()) == MAX_PARSE_BYTES + 1
    assert parse(text) == {}


def test_parse_measures_bytes_not_code_points():
    base = managed(INSTALL)
    at_limit = padded_to(base, MAX_PARSE_BYTES, filler="é")
    over_limit = at_limit + "x"

    assert len(at_limit) < MAX_PARSE_BYTES
    assert len(over_limit) < MAX_PARSE_BYTES < len(over_limit.encode())
    assert parse(at_limit) == INSTALL_PLACEMENT
    assert parse(over_limit) == {}


def test_parse_oversized_text_with_lone_surrogates_does_not_raise():
    # Three bytes each when encoded with surrogatepass: over the limit, yet no error.
    text = managed(INSTALL) + "\ud800" * (MAX_PARSE_BYTES // 2)
    fitting = managed(INSTALL) + "\ud800" * 100

    assert parse(text) == {}
    assert parse(fitting) == INSTALL_PLACEMENT


# -- linear time ---------------------------------------------------------------
#
# Cost is judged by how it SCALES, never by an absolute wall-clock bound: the same input
# is parsed at 1/SCALE of its size and at full size, each timed as the best of REPEATS
# runs after one warm-up, and the ratio of the two must stay far below the quadratic
# ratio. For SCALE = 8 a linear parse costs about 8x more on the big input and a
# quadratic one about 64x; the bound sits between them with a wide margin on each side,
# so a slow or coverage-instrumented machine (which slows both runs alike) cannot flake
# it, while a quadratic regression cannot hide.

BIG = MAX_PARSE_BYTES - 1024
SCALE = 8
REPEATS = 5
MAX_RATIO = 24.0  # linear is about 8, quadratic about 64
TIMER_FLOOR = 1e-4  # seconds; keeps a near-zero small run from inflating the ratio


def best_seconds(function: Callable[[str], object], text: str) -> float:
    """Return the fastest of REPEATS timed runs of ``function(text)`` after a warm-up."""
    function(text)
    timings = []
    for _ in range(REPEATS):
        started = time.perf_counter()
        function(text)
        timings.append(time.perf_counter() - started)
    return min(timings)


def cost_ratio(function: Callable[[str], object], build: Callable[[int], str]) -> float:
    """Return time(full size) / time(1/SCALE size) for ``function`` over ``build(size)``."""
    small = best_seconds(function, build(BIG // SCALE))
    big = best_seconds(function, build(BIG))
    return big / max(small, TIMER_FLOOR)


def quadratic(text: str) -> int:
    """A deliberately quadratic stand-in: one full scan per 8 KiB of input."""
    return sum(text.count("a") for _ in range(0, len(text), 8192))


def hostile(shape: Callable[[int], str]) -> Callable[[int], str]:
    """Wrap a body builder so the text is a managed sidebar of roughly ``size`` bytes."""
    return lambda size: MARKER + "\n" + shape(size)


def test_cost_ratio_tells_linear_from_quadratic_cost():
    build = lambda size: "a" * size  # noqa: E731
    linear = cost_ratio(lambda text: text.count("a"), build)
    assert cost_ratio(quadratic, build) > MAX_RATIO > linear


SHAPES = [
    pytest.param(lambda size: "- " + "a" * size, id="one-long-line-no-suffix"),
    pytest.param(lambda size: "- " + PREFIX * (size // len(PREFIX)) + SUFFIX, id="repeated-prefix"),
    pytest.param(lambda size: "- " + SUFFIX * (size // len(SUFFIX)) + SUFFIX, id="repeated-suffix"),
    pytest.param(
        lambda size: "- x" + PREFIX + '{"page":"' + "a" * size + '"}' + SUFFIX, id="huge-valid-page"
    ),
    pytest.param(lambda size: "- x" + PREFIX + "[" * size + "]" * 3 + SUFFIX, id="deep-nesting"),
    pytest.param(lambda size: "- x\n" * (size // 4), id="many-lines"),
    pytest.param(
        lambda size: ("- " + PREFIX + "{" + SUFFIX + "\n") * (size // 40), id="many-bad-records"
    ),
]


@pytest.mark.parametrize("shape", SHAPES)
def test_parse_one_mebibyte_of_hostile_text_defaults_and_scales_linearly(shape):
    text = hostile(shape)(BIG)

    parsed = parse(text)

    assert parsed == {} or all(stem == "a" * BIG for stem in parsed)
    assert cost_ratio(parse, hostile(shape)) < MAX_RATIO


def test_parse_huge_page_name_is_a_valid_entry_and_scales_linearly():
    huge = "a" * BIG

    assert parse(managed(line(record_for(page=huge)))) == {huge: p()}
    assert cost_ratio(parse, lambda size: managed(line(record_for(page="a" * size)))) < MAX_RATIO


def test_parse_one_mebibyte_single_line_unmanaged_is_not_parsed():
    # Rejected after reading the first line only; its cost is below timer resolution,
    # so only the outcome is asserted.
    assert parse("x" * MAX_PARSE_BYTES) == {}


# -- robustness: hand-edited and malformed entries ------------------------------


@pytest.mark.parametrize(
    "bad_line",
    [
        " " + INSTALL,
        "\t" + INSTALL,
        INSTALL.replace("- ", "* ", 1),
        INSTALL.replace("- ", "-", 1),
        INSTALL.replace("- ", "", 1),
        INSTALL.replace("- ", "1. ", 1),
        INSTALL + " ",
        INSTALL + "\t",
        INSTALL.removesuffix(SUFFIX),
        INSTALL.removesuffix(SUFFIX) + "-->",
        INSTALL.removesuffix(SUFFIX) + " --",
        INSTALL.removesuffix(SUFFIX) + " --!>",
        INSTALL.replace(PREFIX, " <!--wikiops:entry "),
        INSTALL.replace(PREFIX, " <!-- WIKIOPS:ENTRY "),
        INSTALL.replace(PREFIX, "<!-- wikiops:entry "),
        INSTALL.replace(PREFIX, " <!-- wikiops:entry"),
        "- [Install](Install)",
        "[Install](Install)",
        "**Guides**",
        "just some prose",
        "<!-- wikiops:entry " + record_for(page="Install") + " -->",
        "- " + PREFIX.strip() + " " + record_for(page="Install") + " -->",
    ],
)
def test_parse_reformatted_lines_fall_back_to_defaults(bad_line):
    assert parse(managed(bad_line)) == {}


def test_parse_reformatted_line_does_not_disturb_its_neighbours():
    other = line(record_for(page="Other", order=2))

    parsed = parse(managed("broken " + INSTALL, other, "", "**Group**", "- junk"))

    assert parsed == {"Other": p(order=2)}


def test_parse_entry_that_is_only_the_prefix_and_suffix_has_no_payload():
    assert parse(managed("- x" + PREFIX + "-->")) == {}
    assert parse(managed("- x" + PREFIX.rstrip() + SUFFIX)) == {}
    assert parse(managed("- x" + PREFIX + SUFFIX)) == {}


@pytest.mark.parametrize(
    "payload",
    [
        "",
        " ",
        "{",
        "}",
        '{"page":"A"',
        '{"page":"A",}',
        "{page:'A'}",
        "{'page':'A'}",
        '{"page":"A"}}',
        '{"page":"A"} {"page":"B"}',
        "not json",
        "NaN",
        "Infinity",
    ],
)
def test_parse_malformed_json_falls_back_to_defaults(payload):
    assert parse(managed(line(payload))) == {}


@pytest.mark.parametrize(
    "payload",
    ['["A"]', '"A"', "1", "1.5", "null", "true", "false", "[]", "[[]]", '[{"page":"A"}]'],
)
def test_parse_json_that_is_not_an_object_falls_back_to_defaults(payload):
    assert parse(managed(line(payload))) == {}


@pytest.mark.parametrize("depth", [200, 5_000, 100_000])
def test_parse_deeply_nested_json_is_caught_and_defaults(depth):
    nested = "[" * depth + "]" * depth
    text = managed(
        line(nested),
        line('{"page":"A","order":' + nested + "}"),
        line('{"page":"B","order":3}'),
    )

    assert parse(text) == {"B": p(order=3)}


@pytest.mark.parametrize(
    "payload",
    [
        '{"page":"A","weight":3}',
        '{"page":"A","sidebar":{}}',
        '{"page":"A","Group":"G"}',
        '{"page":"A","group":"G","extra":null}',
    ],
)
def test_parse_unknown_keys_reject_the_whole_entry(payload):
    assert parse(managed(line(payload))) == {}


@pytest.mark.parametrize(
    "payload",
    [
        '{"page":"A","order":true}',
        '{"page":"A","order":false}',
        '{"page":"A","order":"10"}',
        '{"page":"A","order":1.5}',
        '{"page":"A","order":2.0}',
        '{"page":"A","order":1e2}',
        '{"page":"A","order":[1]}',
        '{"page":"A","order":{}}',
        '{"page":"A","order":NaN}',
        '{"page":"A","group":"' + "g" * 81 + '"}',
        '{"page":"A","label":"' + "l" * 81 + '"}',
        '{"page":"A","group":""}',
        '{"page":"A","group":"   "}',
        '{"page":"A","label":"\\t"}',
        '{"page":"A","group":5}',
        '{"page":"A","group":true}',
        '{"page":"A","label":["x"]}',
        '{"page":"A","group":"a\\nb"}',
        '{"page":"A","label":"a\\rb"}',
        '{"page":"A","label":"a\\u2028b"}',
        '{"page":"A","group":"a\\u0085b"}',
        '{"page":"A","group":"a\\u001cb"}',
        '{"page":"A","label":"\\ud800"}',
        '{"page":"A","group":"G","order":"bad"}',
        '{"page":"A","order":1000001}',
        '{"page":"A","order":-1000001}',
        '{"page":"A","order":' + "9" * 30 + "}",
        '{"page":"A","order":1' + "0" * 5000 + "}",
        '{"page":"A","label":"' + ZW_ESCAPE + '"}',
        '{"page":"A","group":"' + ZW_ESCAPE * 3 + '"}',
        '{"page":"A","group":" ' + ZW_ESCAPE + ' "}',
    ],
)
def test_parse_invalid_values_are_rejected_with_the_hint_validators(payload):
    assert parse(managed(line(payload))) == {}


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ('{"page":"A","group":"' + "g" * 80 + '"}', {"A": p(group="g" * 80)}),
        ('{"page":"A","label":"' + "l" * 80 + '"}', {"A": p(label="l" * 80)}),
        ('{"page":"A","order":0}', {"A": p(order=0)}),
        ('{"page":"A","order":-7}', {"A": p(order=-7)}),
        ('{"page":"A","order":1000000}', {"A": p(order=1_000_000)}),
        ('{"page":"A","order":-1000000}', {"A": p(order=-1_000_000)}),
        ('{"page":"A","group":null,"order":null,"label":null}', {"A": p()}),
        ('{"page":"A"}', {"A": p()}),
        ('{"page":"A","group":"  Padded  "}', {"A": p(group="Padded")}),
        ('{"page":"A","label":"  Padded  "}', {"A": p(label="  Padded  ")}),
        ('{"label":"L","order":3,"group":"G","page":"A"}', {"A": p("G", 3, "L")}),
    ],
)
def test_parse_valid_records_keep_their_exact_values(payload, expected):
    assert parse(managed(line(payload))) == expected


@pytest.mark.parametrize(
    "payload",
    [
        "{}",
        '{"group":"G"}',
        '{"page":""}',
        '{"page":5}',
        '{"page":null}',
        '{"page":true}',
        '{"page":["A"]}',
        '{"page":{"a":1}}',
        '{"page":"\\ud800"}',
        '{"page":"\\udfff"}',
    ],
)
def test_parse_entry_without_a_valid_page_is_skipped(payload):
    assert parse(managed(line(payload))) == {}


def test_parse_page_with_an_astral_character_is_kept():
    assert parse(managed(line(record_for(page="a\U0001f600b")))) == {"a\U0001f600b": p()}
    pair = '{"page":"\\ud83d\\ude00"}'
    assert parse(managed(line(pair))) == {"\U0001f600": p()}


def test_parse_first_valid_entry_of_a_page_wins():
    first = line(record_for(page="A", group="First"))
    second = line(record_for(page="A", group="Second", order=9))
    third = line(record_for(page="B", order=1))

    assert parse(managed(first, second, third)) == {"A": p(group="First"), "B": p(order=1)}


def test_parse_an_invalid_duplicate_does_not_claim_the_page():
    broken = line(record_for(page="A", order=True))
    good = line(record_for(page="A", group="Good"))

    assert parse(managed(broken, good)) == {"A": p(group="Good")}


def test_parse_ignores_the_markdown_text_and_target_of_an_entry():
    edited = line(record_for(page="Install", order=4), label="Totally Different", target="Elsewhere")

    assert parse(managed(edited)) == {"Install": p(order=4)}


def test_parse_ignores_text_that_is_not_an_entry_line():
    text = managed(
        "# heading",
        "",
        INSTALL,
        "**Group**",
        "",
        "- plain list item without a record",
        "<!-- a comment -->",
        "tail",
    )

    assert parse(text) == INSTALL_PLACEMENT


def test_parse_the_last_record_prefix_on_a_line_is_the_one_decoded():
    inside_string = '{"page":"A","group":"x' + PREFIX + '{"}'
    two_comments = (
        f"- x{PREFIX}{record_for(page='A', group='Old')}{SUFFIX}{PREFIX}"
        f"{record_for(page='B', group='New')}{SUFFIX}"
    )

    assert parse(managed("- x" + PREFIX + inside_string + SUFFIX)) == {}
    assert parse(managed(two_comments)) == {"B": p(group="New")}


def test_parse_a_comment_closer_inside_a_hand_written_string_is_plain_text():
    hand_written = line('{"page":"A","group":"x --> y"}')

    assert parse(managed(hand_written)) == {"A": p(group="x --> y")}


# -- line endings --------------------------------------------------------------


def test_parse_tolerates_crlf_line_endings():
    text = managed(INSTALL, line(record_for(page="B", order=2))).replace("\n", "\r\n")

    assert classify(text) == "managed"
    assert parse(text) == {**INSTALL_PLACEMENT, "B": p(order=2)}


def test_parse_removes_exactly_one_carriage_return_per_line():
    two = INSTALL + "\r\r\n"
    one = INSTALL + "\r\n"

    assert parse(MARKER + "\n" + two) == {}
    assert parse(MARKER + "\n" + one) == INSTALL_PLACEMENT
    assert parse(MARKER + "\r\n" + two) == {}
    assert parse(MARKER + "\r\n" + INSTALL) == INSTALL_PLACEMENT


def test_parse_does_not_split_lines_on_exotic_line_boundaries():
    entries = [line(record_for(page=page, order=1)) for page in ("A", "B", "C", "D")]
    separators = ["\x85", "\u2028", "\x0b", "\x0c"]
    glued = entries[0]
    for separator, entry in zip(separators, entries[1:] + [""]):
        glued += separator + entry

    assert parse(managed(glued)) == {}
    assert parse(managed(glued.removesuffix(separators[-1]))) == {"D": p(order=1)}


def test_parse_a_lone_carriage_return_is_not_a_line_break():
    assert classify(MARKER + "\r" + INSTALL + "\n") == "unmanaged"
    assert parse(managed("junk\r" + INSTALL)) == {}
    assert parse(managed("junk\n" + INSTALL)) == INSTALL_PLACEMENT


# -- exact-value recovery (threat: injection) ----------------------------------

ADVERSARIAL = [
    "x --> <script>alert(1)</script>",
    "a--b",
    "--",
    "-->",
    "--!>",
    "<!--",
    "<!-- wikiops:entry {} -->",
    PREFIX + '{"page":"Forged"}' + SUFFIX,
    "]",
    "[[x]]",
    "[x](http://evil)",
    "a|b",
    "*bold*",
    "_it_",
    "`code`",
    "back\\slash",
    "two\\\\slashes",
    "\\u002d literally",
    "&amp; &lt;",
    "# heading",
    "!bang",
    "é ñ 日本 ☃ \U0001f600",
    "tab\there",
    "\x1b[31mred",
    "\u202ebidi",
    "\u200bzero width",
    '"quotes" and \'single\'',
    "{\"page\":\"x\"}",
    "  padded  ",
]


@pytest.mark.parametrize("text", ADVERSARIAL)
def test_parse_recovers_adversarial_group_and_label_exactly(text):
    placements = {"Page": p(group=text, order=3, label=text)}

    parsed = parse(render(["Page"], placements))

    # The group is stored stripped; the label keeps its exact value.
    assert parsed == {"Page": p(group=text.strip(), order=3, label=text)}


@pytest.mark.parametrize("text", ADVERSARIAL)
def test_parse_adversarial_text_forges_no_other_entry(text):
    placements = {"Page": p(group=text, label=text), "Other": p(order=1)}

    parsed = parse(render(["Page", "Other"], placements))

    assert set(parsed) == {"Page", "Other"}
    assert parsed == {"Page": p(group=text.strip(), label=text), "Other": p(order=1)}


@pytest.mark.parametrize(
    "stem",
    [
        "Getting-Started",
        "a--b",
        "x --> y",
        "<script>",
        "[[Page]]",
        "a|b",
        "é",
        "日本語",
        "with space",
        "100%",
        "a\U0001f600b",
        "--",
        PREFIX.strip(),
    ],
)
def test_parse_recovers_the_page_name_of_adversarial_pages(stem):
    placements = {stem: p(group="G", order=1)}

    assert parse(render([stem], placements)) == placements


# -- round trip and idempotence ------------------------------------------------


def test_round_trip_of_a_hand_picked_sidebar_is_byte_identical():
    pages = ["Home", "Overview", "Install", "Upgrade", "Orphan"]
    placements = {
        "Install": p("Guides", 10, "Install guide"),
        "Upgrade": p("Guides", 20),
        "Overview": p(order=1),
    }
    text = render(pages, placements)

    assert render(pages, merge(parse(text), ())) == text
    assert parse(text) == {**placements, "Home": p(), "Orphan": p()}


def test_round_trip_of_no_pages_is_just_the_marker():
    text = render([], {})

    assert text == MARKER + "\n"
    assert parse(text) == {}
    assert render([], merge(parse(text), ())) == text


def test_round_trip_keeps_the_placement_of_pages_that_are_not_listed():
    text = render(["A"], {"A": p(order=1), "Gone": p(group="G")})

    assert parse(text) == {"A": p(order=1)}


def test_round_trip_through_crlf_regenerates_lf_bytes():
    text = render(["A", "B"], {"A": p("G", 1, "Label")})

    assert render(["A", "B"], merge(parse(text.replace("\n", "\r\n")), ())) == text


def test_regeneration_drops_the_defaults_of_a_hand_edited_entry():
    pages = ["Install", "Other"]
    text = render(pages, {"Install": p("Guides", 10), "Other": p(order=2)})
    edited = "\n".join(
        entry.removesuffix(SUFFIX) if entry.startswith("- [Install]") else entry
        for entry in text.split("\n")
    )

    parsed = parse(edited)

    assert edited != text
    assert parsed == {"Other": p(order=2)}
    assert render(pages, merge(parsed, ())) == render(pages, {"Other": p(order=2)})
    assert render(pages, merge(parsed, ())) != text


def test_hint_patches_apply_on_top_of_the_parsed_placement():
    text = render(["Install"], {"Install": p("Guides", 10, "Install guide")})
    patches = (("Install", HintPatch(order=5)), ("New", HintPatch(group="Fresh")))

    merged = merge(parse(text), patches)

    assert merged == {"Install": p("Guides", 5, "Install guide"), "New": p(group="Fresh")}


# -- seeded property-like invariants -------------------------------------------

STEM_PARTS = [
    "a", "B", "z", "Q", "0", "9", "é", "ñ", "日", "本", "\U0001f600", "-", "-", "--",
    " ", "_", ".", "[", "]", "<", ">", "|", "*", "&", "!", "#", "`", "\\", "'", '"',
    "%", "~", "(", ")",
]  # fmt: skip
TEXT_PARTS = [
    "x", "Guides", " ", "-->", "<!--", "--!>", "--", "<script>", "]", "[[x]]", "|", "*",
    "_", "`", "\\", "\\\\", "&amp;", "é", "日本", "\u202e", "\x1b", "\t", '"', "'", "!",
    "#", "%", "{", "}", ",", ":", PREFIX, SUFFIX, "☃", "\U0001f600",
]  # fmt: skip


def random_stem(rng: random.Random) -> str:
    return "".join(rng.choice(STEM_PARTS) for _ in range(rng.randint(1, 12)))


def random_text(rng: random.Random) -> str:
    while True:
        text = "".join(rng.choice(TEXT_PARTS) for _ in range(rng.randint(1, 6)))
        if isinstance(validate_hint({"github_wiki": {"sidebar": {"group": text}}}), HintPatch):
            return text


def random_scenario(seed: int) -> tuple[list[str], dict[str, Placement]]:
    rng = random.Random(seed)
    pages = list(dict.fromkeys(random_stem(rng) for _ in range(rng.randint(0, 12))))
    if rng.random() < 0.3 and "Home" not in pages:
        pages.append("Home")
    groups = [random_text(rng) for _ in range(3)]
    placements: dict[str, Placement] = {}
    for stem in [*pages, *(random_stem(rng) for _ in range(rng.randint(0, 2)))]:
        if rng.random() < 0.7:
            placements[stem] = Placement(
                group=rng.choice([None, *groups]),
                order=rng.choice([None, rng.randint(-5, 5), rng.randint(-1_000_000, 1_000_000)]),
                label=rng.choice([None, random_text(rng)]),
            )
    rng.shuffle(pages)
    return pages, placements


def expected_for(pages: list[str], placements: dict[str, Placement]) -> dict[str, Placement]:
    """The placements ``render`` keeps: a group is stored stripped, everything else as given."""
    expected = {}
    for stem in pages:
        placement = placements.get(stem, Placement())
        group = None if placement.group is None else placement.group.strip()
        expected[stem] = Placement(group=group, order=placement.order, label=placement.label)
    return expected


@pytest.mark.parametrize("seed", SEED_RANGE)
def test_round_trip_is_byte_identical_for_random_sidebars(seed):
    pages, placements = random_scenario(seed)

    text = render(pages, placements)
    parsed = parse(text)

    assert classify(text) == "managed"
    assert parsed == expected_for(pages, placements)
    assert render(pages, merge(parsed, ())) == text


@pytest.mark.parametrize("seed", SEED_RANGE)
def test_render_is_idempotent_and_independent_of_page_order(seed):
    pages, placements = random_scenario(seed)
    shuffled = random.Random(seed + 1_000).sample(pages, len(pages))

    text = render(pages, placements)

    assert render(pages, placements) == text
    assert render(shuffled, placements) == text
    assert render(pages, merge(parse(text), ())) == text
    again = render(pages, merge(parse(render(pages, merge(parse(text), ()))), ()))
    assert again == text
    assert text.endswith("\n") and not text.endswith("\n\n")
    assert "\r" not in text


@pytest.mark.parametrize("seed", SEED_RANGE)
def test_hand_edited_sidebar_falls_back_to_defaults_without_raising(seed):
    pages, placements = random_scenario(seed)
    rng = random.Random(seed)
    text = render(pages, placements)

    for _ in range(5):
        position = rng.randrange(len(text) + 1)
        junk = rng.choice(["", "\n", "\r", " ", "{", "}", '"', "\\", "-->", PREFIX, "é", "\ud800"])
        if rng.random() < 0.5:
            text = text[:position] + junk + text[position + rng.randint(0, 6) :]
        else:
            text = text[:position] + junk + text[position:]

    parsed = parse(text)

    assert_all_valid(parsed)
    regenerated = render(pages, merge(parsed, ()))
    assert regenerated.startswith(MARKER + "\n")
    assert render(pages, merge(parse(regenerated), ())) == regenerated


GARBAGE_PARTS = [
    "- ", PREFIX, SUFFIX, "{", "}", '"page"', '"group"', '"order"', ":", '"', ",", "[", "]",
    "\\", "\r", "\n", "\ud800", "é", "null", "true", "1e999", "NaN", "-0", " ", "-", "x", "<",
    ">", "--", "\u2028", "\x85", "\x00",
]  # fmt: skip


@pytest.mark.parametrize("seed", SEED_RANGE)
def test_parse_never_raises_for_random_text(seed):
    rng = random.Random(seed)
    body = "".join(rng.choice(GARBAGE_PARTS) for _ in range(rng.randint(0, 300)))
    head = rng.choice([MARKER + "\n", MARKER + "\r\n", "", "x\n"])

    parsed = parse(head + body)

    assert isinstance(parsed, dict)
    assert_all_valid(parsed)
    assert render(list(parsed), parsed).startswith(MARKER + "\n")
    if not head.startswith(MARKER):
        assert parsed == {}


@pytest.mark.parametrize("seed", SEED_RANGE)
def test_parse_never_raises_for_random_well_formed_looking_entries(seed):
    rng = random.Random(seed)
    lines = []
    for _ in range(rng.randint(1, 20)):
        fields: dict[str, Any] = {"page": rng.choice(["A", "B", "", 5, None, "C" * 5])}
        for key in ("group", "label", "order", rng.choice(["weight", "page"])):
            if rng.random() < 0.5:
                fields[key] = rng.choice(
                    [None, True, 3, -1, 1.5, "ok", "", "x" * 81, ["l"], {"d": 1}, "a\nb"]
                )
        lines.append(line(json.dumps(fields)))

    parsed = parse(managed(*lines))

    assert_all_valid(parsed)
    assert render(list(parsed), parsed).startswith(MARKER + "\n")
