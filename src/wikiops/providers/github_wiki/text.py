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
    "["
    "\u00ad"  # soft hyphen
    "\u061c"  # Arabic letter mark
    "\u180e"  # Mongolian vowel separator
    "\u200b-\u200f"  # zero-width space/joiners and left/right marks
    "\u202a-\u202e"  # embeddings and overrides
    "\u2060-\u2064"  # word joiner and invisible operators
    "\u2066-\u2069"  # isolates
    "\ufeff"  # zero-width no-break space (BOM)
    "]"
)


def _character_class(*spans: tuple[int, int]) -> re.Pattern[str]:
    """Compile a class of the code point ``spans``; built from numbers, so the source stays ASCII."""
    body = "".join(
        chr(first) if first == last else f"{chr(first)}-{chr(last)}" for first, last in spans
    )
    return re.compile(f"[{body}]")


# Unicode line and paragraph separators (U+2028, U+2029): not C0/C1 controls, yet they end a
# line for ``str.splitlines`` and many viewers.
LINE_SEPARATORS = _character_class((0x2028, 0x2029))
# Text-direction controls that reorder what surrounds them: Arabic letter mark, left/right
# marks (U+200E-200F), embeddings and overrides (U+202A-202E) and isolates (U+2066-2069).
BIDI_CONTROLS = _character_class((0x061C, 0x061C), (0x200E, 0x200F), (0x202A, 0x202E), (0x2066, 0x2069))


def has_control_characters(value: str) -> bool:
    """Whether ``value`` holds a C0 control, DEL or a C1 control character."""
    return CONTROL_CHARACTERS.search(value) is not None


def _escape(match: re.Match[str]) -> str:
    return match.group().encode("unicode_escape").decode("ascii")


def escape_unsafe_characters(text: str) -> str:
    """Replace control and invisible characters by their visible escape (``\\x1b``, ``\\u202e``)."""
    return INVISIBLE_CHARACTERS.sub(_escape, CONTROL_CHARACTERS.sub(_escape, text))
