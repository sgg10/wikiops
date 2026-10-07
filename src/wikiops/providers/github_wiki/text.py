"""Character-level helpers shared by the pure modules of ``github_wiki``.

One definition of "control character" (C0, DEL and C1) and of the invisible
characters that can spoof text (bidi marks and overrides, zero-width and joiner
characters). Settings validation, the page policy and message rendering all use
these, so no rule can drift between them.
"""

from __future__ import annotations

import re

CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# Render as nothing or reorder surrounding text: soft hyphen, Arabic letter mark,
# Mongolian vowel separator, zero-width and directional marks (U+200B-200F),
# embeddings and overrides (U+202A-202E), word joiner and invisible operators
# (U+2060-2064), isolates (U+2066-2069) and the zero-width no-break space.
INVISIBLE_CHARACTERS = re.compile(
    "[­؜᠎​-‏‪-‮⁠-⁤⁦-⁩﻿]"
)


def has_control_characters(value: str) -> bool:
    """Whether ``value`` holds a C0 control, DEL or a C1 control character."""
    return CONTROL_CHARACTERS.search(value) is not None


def _escape(match: re.Match[str]) -> str:
    return match.group().encode("unicode_escape").decode("ascii")


def escape_unsafe_characters(text: str) -> str:
    """Replace control and invisible characters by their visible escape (``\\x1b``, ``\\u202e``)."""
    return INVISIBLE_CHARACTERS.sub(_escape, CONTROL_CHARACTERS.sub(_escape, text))
