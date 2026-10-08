"""Pure placement-hint model of the generated ``_Sidebar.md`` of ``github_wiki``.

A plugin may attach ``metadata.github_wiki.sidebar = {group?, order?, label?}`` to a page
operation (``child_metadata`` for child creates). This module validates those hints,
folds them into the placement of each page and keeps that placement sticky across
applies: a key that is absent keeps the previous value, a value overrides it and ``null``
clears it. A hint that is invalid in any way is ignored as a whole, never partially
applied, and never fails the page operation it rides on.

Pure functions only (no filesystem, process, network or backend access). The module
imports the standard library alone: no operation or document type from the SDK is used
here, so the callers extract the metadata and hand it over as a :class:`HintSource`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

MAX_TEXT = 80

_SIDEBAR_KEY = "sidebar"
_HINT_KEYS = frozenset({"group", "label", "order"})
_MARKDOWN_SUFFIX = ".md"
_EXCLUDED_PREFIX = "_"
_TEXT_SHAPE = f"a non-empty single-line string of at most {MAX_TEXT} characters"
_ORDER_SHAPE = "an integer"
# Every character ``str.splitlines`` treats as a line boundary.
_LINE_BOUNDARY = re.compile(r"[\n\r\v\f\x1c\x1d\x1e\x85  ]")


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


def _check_text(field: str, value: Any) -> HintProblem | None:
    """Validate a ``group`` or ``label`` value (``None`` is a clear, hence valid)."""
    if value is None:
        return None
    valid = (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= MAX_TEXT
        and _LINE_BOUNDARY.search(value) is None
    )
    return None if valid else HintProblem(field, _TEXT_SHAPE)


def _check_order(value: Any) -> HintProblem | None:
    """Validate an ``order`` value: an integer, never a boolean (``None`` is a clear)."""
    if value is None or (isinstance(value, int) and not isinstance(value, bool)):
        return None
    return HintProblem("order", _ORDER_SHAPE)


def _first_unknown_key(mapping: Mapping[Any, Any], allowed: frozenset[str]) -> str | None:
    unknown = sorted(str(key) for key in mapping if key not in allowed)
    return unknown[0] if unknown else None


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
    return HintPatch(
        group=sidebar.get("group", UNSET),
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


def merge(
    previous: Mapping[str, Placement], patches: Sequence[tuple[str, HintPatch]]
) -> dict[str, Placement]:
    """Fold ``patches`` (in order) over the ``previous`` placements, keeping the rest."""
    merged = dict(previous)
    for stem, patch in patches:
        merged[stem] = apply_patch(merged.get(stem, Placement()), patch)
    return merged
