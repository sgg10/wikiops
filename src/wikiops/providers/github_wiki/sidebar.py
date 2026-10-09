"""Pure placement-hint model of the generated ``_Sidebar.md`` of ``github_wiki``.

A plugin may attach ``metadata.github_wiki.sidebar = {group?, order?, label?}`` to a page
operation (``child_metadata`` for child creates). This module validates those hints,
folds them into the placement of each page and keeps that placement sticky across
applies: a key that is absent keeps the previous value, a value overrides it and ``null``
clears it. A hint that is invalid in any way is ignored as a whole, never partially
applied, and never fails the page operation it rides on.

The module also renders the managed file. A generated sidebar starts with :data:`MARKER`
and lists one markdown link per page, each followed by an invisible HTML-comment record
that carries the page placement so a later apply can recover it. Labels and group names
are escaped for display and for the record, so a hint can neither inject markup nor
forge or break out of a record.

:func:`parse` reads those records back from a previous managed file. It is bounded (1 MiB)
and linear, never trusts an unmarked file, and skips any entry it cannot decode, so a
hand-edited or hostile sidebar only ever falls back to default placement.

Pure functions only (no filesystem, process, network or backend access). No operation or
document type from the SDK is used here, so the callers extract the metadata and hand it
over as a :class:`HintSource`.
"""

from __future__ import annotations

import json
import re
import string
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote

from wikiops.providers.github_wiki.errors import render_message
from wikiops.providers.github_wiki.layout import guard_link
from wikiops.providers.github_wiki.text import INVISIBLE_CHARACTERS, escape_unsafe_characters

MARKER = "<!-- wikiops:managed sidebar -->"
MAX_TEXT = 80
# ``order`` only ranks pages, so a sane range is plenty; it also keeps the integer small
# enough to serialize (CPython refuses to convert integers beyond 4300 digits to text).
MAX_ORDER = 1_000_000
# A previous sidebar larger than this (utf-8 bytes) is not parsed: placements default.
MAX_PARSE_BYTES = 1024 * 1024

_SIDEBAR_KEY = "sidebar"
_HINT_KEYS = frozenset({"group", "label", "order"})
_MARKDOWN_SUFFIX = ".md"
_EXCLUDED_PREFIX = "_"
_TEXT_SHAPE = f"a non-empty single-line string of at most {MAX_TEXT} characters"
_ORDER_SHAPE = f"an integer between -{MAX_ORDER} and {MAX_ORDER}"
# An unknown hint key is plugin-controlled, so it is bounded and escaped before it is
# echoed into a problem (and from there into a warning message).
_MAX_ECHO = 80
_ELLIPSIS = "..."
_HOME = "Home"
_RECORD_PREFIX = " <!-- wikiops:entry "
_RECORD_SUFFIX = " -->"
# JSON escapes for the characters that could end or open an HTML comment (``-->``,
# ``--!>``, ``<!--``) once the record sits inside one.
_RECORD_ESCAPES = {ord("-"): "\\u002d", ord("<"): "\\u003c", ord(">"): "\\u003e"}
_MARKDOWN_ESCAPES = {ord(char): f"\\{char}" for char in string.punctuation}
# Every character ``str.splitlines`` treats as a line boundary.
_LINE_BOUNDARY = re.compile(r"[\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029]")
# Lone surrogates cannot be encoded as utf-8, so no file could hold them.
_SURROGATE = re.compile(r"[\ud800-\udfff]")
_ENTRY_START = "- "


class _Unset:
    """Type of :data:`UNSET`: the key was absent from the hint (keep the current value)."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNSET"


UNSET = _Unset()


@dataclass(frozen=True)
class Placement:
    """Where one page sits in the sidebar; ``None`` is the default for every key."""

    group: str | None = None
    order: int | None = None
    label: str | None = None


@dataclass(frozen=True)
class HintPatch:
    """A validated hint: ``UNSET`` keeps a key, a value overrides it, ``None`` clears it."""

    group: str | None | _Unset = UNSET
    order: int | None | _Unset = UNSET
    label: str | None | _Unset = UNSET


@dataclass(frozen=True)
class HintProblem:
    """The first reason a hint is invalid: the offending field and the shape it expects."""

    field: str
    expected: str


@dataclass(frozen=True)
class HintSource:
    """The metadata of one page operation; ``page`` is the file name, e.g. ``Install.md``."""

    operation_id: str
    page: str
    metadata: Mapping[str, Any]


@dataclass(frozen=True)
class RejectedHint:
    """An ignored hint, with the operation and page (stem) it rode on."""

    operation_id: str
    page: str
    problem: HintProblem


def page_name(file_name: str) -> str:
    """Return ``file_name`` without its final ``.md`` (any letter case)."""
    if file_name.lower().endswith(_MARKDOWN_SUFFIX):
        return file_name[: -len(_MARKDOWN_SUFFIX)]
    return file_name


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _is_edge(char: str) -> bool:
    """Whether ``char`` is whitespace or invisible: it shows nothing at the edge of a text."""
    return char.isspace() or INVISIBLE_CHARACTERS.fullmatch(char) is not None


def _trim(text: str) -> str:
    """Return ``text`` without the whitespace and invisible characters at either edge.

    Plain ``str.strip`` misses zero-width, bidi and BOM padding, so a group padded with
    one would be a second, identical-looking group. Inner characters are kept. One scan
    from each end, so the cost is linear.
    """
    start, end = 0, len(text)
    while start < end and _is_edge(text[start]):
        start += 1
    while end > start and _is_edge(text[end - 1]):
        end -= 1
    return text[start:end]


def _is_blank(text: str) -> bool:
    """Whether ``text`` shows nothing: only whitespace and invisible characters.

    One definition for validation and rendering, so a value the hints accept can never
    render as an empty link text or an empty group heading.
    """
    return not _trim(text)


def _check_text(field: str, value: Any) -> HintProblem | None:
    """Validate a ``group`` or ``label`` value (``None`` is a clear, hence valid)."""
    if value is None:
        return None
    valid = (
        isinstance(value, str)
        and not _is_blank(value)
        and len(value) <= MAX_TEXT
        and _LINE_BOUNDARY.search(value) is None
        and _SURROGATE.search(value) is None
    )
    return None if valid else HintProblem(field, _TEXT_SHAPE)


def _check_order(value: Any) -> HintProblem | None:
    """Validate an ``order`` value: a bounded integer, never a boolean (``None`` is a clear)."""
    if value is None:
        return None
    valid = isinstance(value, int) and not isinstance(value, bool) and abs(value) <= MAX_ORDER
    return None if valid else HintProblem("order", _ORDER_SHAPE)


def _visible(char: str) -> str:
    """Return ``char``, or its visible escape when it is a control, invisible or line break."""
    if _LINE_BOUNDARY.fullmatch(char):
        return char.encode("unicode_escape").decode("ascii")
    return escape_unsafe_characters(char)


def _echo(key: str) -> str:
    """Return ``key`` as one bounded line of visible characters, safe to show in a message.

    Characters are escaped one by one and the output stops before it would exceed
    :data:`_MAX_ECHO`, so an escape sequence is never cut in half and the work stays
    bounded however long the key is.
    """
    shown: list[str] = []
    size = 0
    for char in key:
        piece = _visible(char)
        if size + len(piece) > _MAX_ECHO:
            return "".join(shown) + _ELLIPSIS
        shown.append(piece)
        size += len(piece)
    return "".join(shown)


def _first_unknown_key(mapping: Mapping[Any, Any], allowed: frozenset[str]) -> str | None:
    """Return the first (sorted) key outside ``allowed``, bounded and escaped for echoing."""
    unknown = sorted(str(key) for key in mapping if key not in allowed)
    return _echo(unknown[0]) if unknown else None


def _validate_sidebar(sidebar: Any) -> HintPatch | HintProblem:
    if sidebar is None:
        return HintPatch(None, None, None)
    if not isinstance(sidebar, Mapping):
        return HintProblem(_SIDEBAR_KEY, "a mapping or null")
    unknown = _first_unknown_key(sidebar, _HINT_KEYS)
    if unknown is not None:
        return HintProblem(unknown, "only group, order and label are allowed")
    for field in ("group", "label"):
        if field in sidebar and (problem := _check_text(field, sidebar[field])):
            return problem
    if "order" in sidebar and (problem := _check_order(sidebar["order"])):
        return problem
    group = sidebar.get("group", UNSET)
    return HintPatch(
        group=_trim(group) if isinstance(group, str) else group,
        order=sidebar.get("order", UNSET),
        label=sidebar.get("label", UNSET),
    )


def validate_hint(metadata: Mapping[str, Any]) -> HintPatch | HintProblem | None:
    """Validate the sidebar hint of one operation's metadata.

    ``None`` means the operation carries no hint. The first problem found wins, in a
    deterministic order (unknown keys sorted, then ``group``, ``label``, ``order``).
    """
    if "github_wiki" not in metadata:
        return None
    namespace = metadata["github_wiki"]
    if not isinstance(namespace, Mapping):
        return HintProblem("github_wiki", "a mapping whose only key is 'sidebar'")
    unknown = _first_unknown_key(namespace, frozenset({_SIDEBAR_KEY}))
    if unknown is not None:
        return HintProblem(
            f"github_wiki.{unknown}",
            "the only valid key under metadata.github_wiki is 'sidebar'",
        )
    if _SIDEBAR_KEY not in namespace:
        return None
    return _validate_sidebar(namespace[_SIDEBAR_KEY])


# ---------------------------------------------------------------------------
# Patching and merging
# ---------------------------------------------------------------------------


def apply_patch(current: Placement, patch: HintPatch) -> Placement:
    """Return ``current`` with the keys ``patch`` sets or clears (``UNSET`` keeps)."""
    return Placement(
        group=current.group if isinstance(patch.group, _Unset) else patch.group,
        order=current.order if isinstance(patch.order, _Unset) else patch.order,
        label=current.label if isinstance(patch.label, _Unset) else patch.label,
    )


def collect(
    sources: Iterable[HintSource],
) -> tuple[tuple[tuple[str, HintPatch], ...], tuple[RejectedHint, ...]]:
    """Split the hints of ``sources`` (in operation order) into patches and rejections.

    Patches are ``(page stem, patch)`` pairs; operations without a hint contribute
    nothing. An invalid hint, or a hint on an excluded page (name starting with ``_``),
    is rejected whole and yields no patch.
    """
    patches: list[tuple[str, HintPatch]] = []
    rejected: list[RejectedHint] = []
    for source in sources:
        outcome = validate_hint(source.metadata)
        if outcome is None:
            continue
        stem = page_name(source.page)
        if stem.startswith(_EXCLUDED_PREFIX):
            outcome = HintProblem(
                _SIDEBAR_KEY,
                f"a page whose name does not start with '{_EXCLUDED_PREFIX}'",
            )
        if isinstance(outcome, HintProblem):
            rejected.append(RejectedHint(source.operation_id, stem, outcome))
        else:
            patches.append((stem, outcome))
    return tuple(patches), tuple(rejected)


def render_hint_warning(rejected: RejectedHint) -> str:
    """Render the ``sidebar.invalid_hint`` warning of one ignored hint, on a single line.

    It names the operation, the page, the offending field and the shape that field
    expects, so the plugin author can fix the hint. The text is folded to one line and
    stripped of control characters by :func:`render_message`.
    """
    problem = rejected.problem
    return render_message(
        "sidebar.invalid_hint",
        f"Sidebar hint ignored: '{problem.field}' is invalid, expected {problem.expected}",
        context={"op": rejected.operation_id, "page": rejected.page, "field": problem.field},
    )


def merge(
    previous: Mapping[str, Placement], patches: Sequence[tuple[str, HintPatch]]
) -> dict[str, Placement]:
    """Fold ``patches`` (in order) over the ``previous`` placements, keeping the rest."""
    merged = dict(previous)
    for stem, patch in patches:
        merged[stem] = apply_patch(merged.get(stem, Placement()), patch)
    return merged


# ---------------------------------------------------------------------------
# Ownership and ordering
# ---------------------------------------------------------------------------


def classify(text: str) -> Literal["managed", "unmanaged"]:
    """Say whether ``text`` is a generated sidebar: its first line is exactly the marker.

    The first line is the text before the first ``\\n``, minus one trailing ``\\r`` (a
    CRLF checkout). A leading space, trailing text, a BOM or a second ``\\r`` make the
    file unmanaged, and an unmanaged file is never parsed, written or staged.
    """
    first_line = text.partition("\n")[0].removesuffix("\r")
    return "managed" if first_line == MARKER else "unmanaged"


def display_label(page: str, placement: Placement) -> str:
    """Return the link text of ``page``: its ``label`` hint, else the name with spaces.

    A blank label counts as no label, and a name that turns blank once its hyphens become
    spaces (``-``) is shown as it is, so the link text is never empty.
    """
    if placement.label is not None and not _is_blank(placement.label):
        return placement.label
    spaced = page.replace("-", " ")
    return page if _is_blank(spaced) else spaced


def _group_name(placement: Placement) -> str | None:
    """Return the group a placement belongs to: trimmed, ``None`` when blank or unset.

    Spellings that differ only in edge whitespace or invisible padding are one group, and a
    blank group is no group.
    """
    if placement.group is None:
        return None
    return _trim(placement.group) or None


def _normalized(placement: Placement) -> Placement:
    """Return ``placement`` as it is ranked, shown and recorded: no blank label or group."""
    label = placement.label
    return Placement(
        group=_group_name(placement),
        order=placement.order,
        label=None if label is None or _is_blank(label) else label,
    )


def _entry_key(stem: str, placement: Placement) -> tuple[object, ...]:
    """Sort key of a page (its normalized placement) in one list: Home pin, ordered, rest."""
    label = display_label(stem, placement).casefold()
    if stem == _HOME and placement.group is None and placement.order is None:
        return (0,)
    if placement.order is not None:
        return (1, placement.order, label, stem)
    return (2, label, stem)


def _group_key(group: str, members: Iterable[Placement]) -> tuple[object, ...]:
    """Sort key of a group: its lowest page ``order`` (none ranks last), then its name."""
    orders = [member.order for member in members if member.order is not None]
    lowest = min(orders) if orders else None
    return (lowest is None, lowest or 0, group.casefold(), group)


def order(
    pages: Iterable[str], placements: Mapping[str, Placement]
) -> tuple[tuple[str | None, tuple[str, ...]], ...]:
    """Arrange ``pages`` (stems) into the ungrouped list followed by each group, in order.

    Inside a list, pages with an ``order`` come first (ascending, ties by display label
    then name) and the rest follow alphabetically; ``Home`` without a group or an order
    stays first. Groups are ranked by the lowest ``order`` of their pages (a group of
    unordered pages ranks last), ties by group name. Empty lists are omitted. A group is
    named without its surrounding whitespace, so padded and unpadded spellings are one
    group, and a blank group or label counts as unset.
    """
    def placement_of(stem: str) -> Placement:
        return _normalized(placements.get(stem, Placement()))

    lists: dict[str | None, list[str]] = {}
    for stem in dict.fromkeys(pages):
        lists.setdefault(placement_of(stem).group, []).append(stem)

    def arranged(group: str | None) -> tuple[str | None, tuple[str, ...]]:
        stems = sorted(lists[group], key=lambda stem: _entry_key(stem, placement_of(stem)))
        return group, tuple(stems)

    groups = sorted(
        (group for group in lists if group is not None),
        key=lambda group: _group_key(group, map(placement_of, lists[group])),
    )
    return tuple(arranged(group) for group in ([None] if None in lists else []) + groups)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _md(text: str) -> str:
    """Return ``text`` safe to show as markdown: escape unsafe characters and punctuation.

    Control and invisible characters become visible escapes, then every ASCII
    punctuation character is backslash-escaped (CommonMark allows it for all of them),
    which neutralizes links, ``[[wiki]]`` links, tables, emphasis, HTML and entities.
    Only the display is stripped; the record keeps the exact value.
    """
    return escape_unsafe_characters(text.strip()).translate(_MARKDOWN_ESCAPES)


def _json_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=True).translate(_RECORD_ESCAPES)


def _record(stem: str, placement: Placement) -> str:
    """Return the JSON record of one entry: sorted keys, ASCII only, comment-safe.

    Inside string values ``-``, ``<`` and ``>`` become JSON escapes, so the record can
    never hold ``--``, a comment opener or a closer; an integer ``order`` is left alone.
    """
    fields: dict[str, str] = {"page": _json_string(stem)}
    if placement.group is not None:
        fields["group"] = _json_string(placement.group)
    if placement.label is not None:
        fields["label"] = _json_string(placement.label)
    if placement.order is not None:
        fields["order"] = json.dumps(placement.order)
    return "{" + ",".join(f'"{key}":{fields[key]}' for key in sorted(fields)) + "}"


def _entry_line(stem: str, placement: Placement) -> str:
    placement = _normalized(placement)
    target = guard_link(quote(stem, safe=""))
    label = _md(display_label(stem, placement))
    return f"- [{label}]({target}){_RECORD_PREFIX}{_record(stem, placement)}{_RECORD_SUFFIX}"


def render(pages: Iterable[str], placements: Mapping[str, Placement]) -> str:
    """Render the managed sidebar for ``pages`` (stems) with their ``placements``.

    Output is deterministic: the marker line, then the ungrouped list and one
    ``**group**`` block per group, separated by blank lines, ending in one newline.
    """
    lines = [MARKER]
    for group, stems in order(pages, placements):
        lines.append("")
        if group is not None:
            lines.extend((f"**{_md(group)}**", ""))
        lines.extend(_entry_line(stem, placements.get(stem, Placement())) for stem in stems)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _decode_entry(line: str) -> tuple[str, Placement] | None:
    """Recover ``(page, placement)`` from one entry line, or ``None`` when it is not one.

    An entry starts with ``- `` and ends with the record suffix; the record is whatever
    sits between the LAST record prefix and that suffix, so a prefix-like text inside an
    earlier part of the line can never be taken for the record. Plain string operations
    only, so the cost is linear in the line length. The markdown text and link target are
    ignored: the record is the only source of the placement, and it must satisfy the very
    same validators as a plugin hint.
    """
    if not (line.startswith(_ENTRY_START) and line.endswith(_RECORD_SUFFIX)):
        return None
    start = line.rfind(_RECORD_PREFIX)
    end = len(line) - len(_RECORD_SUFFIX)
    if start < len(_ENTRY_START):
        return None
    try:
        record = json.loads(line[start + len(_RECORD_PREFIX) : end])
    except (ValueError, RecursionError):
        return None
    if not isinstance(record, dict):
        return None
    page = record.get("page")
    if not isinstance(page, str) or not page or _SURROGATE.search(page):
        return None
    patch = _validate_sidebar({key: value for key, value in record.items() if key != "page"})
    if isinstance(patch, HintProblem):
        return None
    return page, apply_patch(Placement(), patch)


def parse(text: str) -> dict[str, Placement]:
    """Recover the placement of each page from a previous managed sidebar.

    Only a managed file (see :func:`classify`) of at most :data:`MAX_PARSE_BYTES` utf-8
    bytes is read; anything else yields ``{}`` and every page falls back to its default.
    Lines are separated by ``\\n`` (one trailing ``\\r`` is dropped, for CRLF checkouts).
    An entry that cannot be decoded (hand-edited, malformed or invalid) is skipped, never
    an error, and the first valid entry of a page wins.
    """
    if classify(text) == "unmanaged":
        return {}
    if len(text.encode("utf-8", "surrogatepass")) > MAX_PARSE_BYTES:
        return {}
    placements: dict[str, Placement] = {}
    for line in text.split("\n"):
        entry = _decode_entry(line.removesuffix("\r"))
        if entry is not None:
            placements.setdefault(*entry)
    return placements
