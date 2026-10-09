"""Unit tests for the shared character helpers of the ``github_wiki`` provider."""

from __future__ import annotations

from pathlib import Path

import pytest

from wikiops.providers.github_wiki import text
from wikiops.providers.github_wiki.text import (
    escape_unsafe_characters,
    has_control_characters,
)

C0 = [pytest.param(chr(code), id=f"c0-{code:02x}") for code in range(0x20)]
C1 = [pytest.param(chr(code), id=f"c1-{code:02x}") for code in range(0x7F, 0xA0)]


@pytest.mark.parametrize("char", [*C0, *C1])
def test_every_c0_del_and_c1_character_is_a_control_character(char: str) -> None:
    assert has_control_characters(f"a{char}b") is True


@pytest.mark.parametrize(
    "value",
    [
        "",
        "plain text",
        "Página ñandú 日本語",
        "emoji \U0001f600",
        "nbsp\u00a0is printable",
        "line~tilde 0x7e",
    ],
)
def test_ordinary_text_has_no_control_characters(value: str) -> None:
    assert has_control_characters(value) is False


@pytest.mark.parametrize(
    ("raw", "escaped"),
    [
        ("a\x00b", "a\\x00b"),
        ("a\tb", "a\\tb"),
        ("a\x7fb", "a\\x7fb"),
        ("a\x9fb", "a\\x9fb"),
        ("a\u202eb", "a\\u202eb"),
        ("a\u200bb", "a\\u200bb"),
        ("a\u2066b\u2069", "a\\u2066b\\u2069"),
        ("a\ufeffb", "a\\ufeffb"),
        ("a\u00adb", "a\\xadb"),  # soft hyphen
    ],
)
def test_unsafe_characters_are_escaped_visibly(raw: str, escaped: str) -> None:
    assert escape_unsafe_characters(raw) == escaped


def test_text_without_unsafe_characters_is_returned_unchanged() -> None:
    assert escape_unsafe_characters("Página ñandú 日本語 \U0001f600") == "Página ñandú 日本語 \U0001f600"


# -- the invisible-character set is spelled with escapes, never literal ------------

INVISIBLE_CODE_POINTS = (
    [0x00AD, 0x061C, 0x180E, 0xFEFF]
    + list(range(0x200B, 0x2010))  # zero-width and directional marks
    + list(range(0x202A, 0x202F))  # embeddings and overrides
    + list(range(0x2060, 0x2065))  # word joiner and invisible operators
    + list(range(0x2066, 0x206A))  # isolates
)


def test_the_text_module_source_is_plain_ascii() -> None:
    # A literal invisible or bidi character in source is invisible in review and
    # can be silently dropped by an editor, so the set must be written as escapes.
    source = Path(text.__file__).read_bytes()

    assert source.isascii()


@pytest.mark.parametrize("code_point", INVISIBLE_CODE_POINTS, ids=lambda cp: f"u{cp:04x}")
def test_each_invisible_code_point_is_matched_and_escaped(code_point: int) -> None:
    char = chr(code_point)

    assert text.INVISIBLE_CHARACTERS.fullmatch(char) is not None
    assert escape_unsafe_characters(f"a{char}b") == "a" + ascii(char)[1:-1] + "b"


@pytest.mark.parametrize(
    "code_point", [0x00AC, 0x00AE, 0x061B, 0x061D, 0x180D, 0x180F, 0x200A, 0x2010, 0x2029, 0x202F, 0x205F, 0x2065, 0x206A, 0xFEFE, 0xFF00]
)
def test_neighbours_of_the_invisible_ranges_are_not_matched(code_point: int) -> None:
    assert text.INVISIBLE_CHARACTERS.search(chr(code_point)) is None


# Every range a class may touch, widened by a margin so a character gained or lost next to a
# boundary is seen, plus a deterministic stride over the rest of Unicode.
_RELEVANT_RANGES = (
    (0x0000, 0x00FF),  # C0, DEL, C1, soft hyphen and the Latin-1 neighbours
    (0x0600, 0x0620),  # Arabic letter mark
    (0x1800, 0x1810),  # Mongolian vowel separator
    (0x2000, 0x2070),  # zero-width, bidi, line separators, joiners, isolates
    (0xFEF0, 0xFF00),  # zero-width no-break space
)
_SCANNED_CODE_POINTS = sorted(
    {code for first, last in _RELEVANT_RANGES for code in range(first, last + 1)}
    | set(range(0, 0x110000, 0x101))
    | {0x10FFFF}
)


def _matching_code_points(pattern) -> set[int]:  # noqa: ANN001
    return {code for code in _SCANNED_CODE_POINTS if pattern.fullmatch(chr(code))}


def test_the_character_classes_match_exactly_their_documented_code_points() -> None:
    assert _matching_code_points(text.CONTROL_CHARACTERS) == {*range(0x20), *range(0x7F, 0xA0)}
    assert _matching_code_points(text.LINE_SEPARATORS) == {0x2028, 0x2029}
    assert _matching_code_points(text.BIDI_CONTROLS) == {
        0x061C,
        *range(0x200E, 0x2010),
        *range(0x202A, 0x202F),
        *range(0x2066, 0x206A),
    }


def test_the_page_name_class_is_exactly_the_union_of_controls_separators_and_bidi_controls() -> None:
    union = (
        _matching_code_points(text.CONTROL_CHARACTERS)
        | _matching_code_points(text.LINE_SEPARATORS)
        | _matching_code_points(text.BIDI_CONTROLS)
    )

    assert _matching_code_points(text.PAGE_NAME_UNSAFE_CHARACTERS) == union
    assert {0x0A, 0x7F, 0x85, 0x2028, 0x2029, 0x061C, 0x202E, 0x2069} <= union
