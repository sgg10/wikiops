"""Unit tests for the shared character helpers of the ``github_wiki`` provider."""

from __future__ import annotations

import pytest

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
