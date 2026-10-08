"""Unit tests for the ``github_wiki`` error vocabulary (``errors.py``)."""

from __future__ import annotations

import re
import unicodedata

import pytest

from wikiops.core.exceptions import ConfigurationError
from wikiops.providers.github_wiki.errors import (
    CODES,
    NAMESPACE,
    CodeSpec,
    GithubWikiError,
    render_message,
)

# Hard-coded copy of the FINAL spec-codes table, minus the three deferred
# ``sidebar.*`` codes. Changing the vocabulary must be a deliberate edit here.
EXPECTED_CODES = frozenset(
    {
        "config.invalid",
        "config.invalid_repository",
        "config.invalid_host",
        "config.invalid_branch",
        "config.invalid_message",
        "config.push_requires_commit",
        "config.identity_incomplete",
        "config.backend_recursion",
        "config.backend_root_forbidden",
        "config.backend_unknown",
        "config.backend_invalid",
        "config.backend_incompatible",
        "config.git_unavailable",
        "config.key_path_missing",
        "auth.env_missing",
        "auth.gh_unavailable",
        "auth.gh_failed",
        "auth.rejected",
        "auth.ssh_host_key",
        "wiki.not_initialized",
        "network.unreachable",
        "sync.no_local_clone",
        "sync.workdir_not_clone",
        "sync.remote_mismatch",
        "sync.branch_mismatch",
        "sync.branch_not_found",
        "sync.diverged",
        "sync.timeout",
        "sync.git_failed",
        "sync.stale_plan",
        "workdir.unusable",
        "workdir.manifest_corrupt",
        "workdir.dirty",
        "workdir.locked",
        "path.nested_not_supported",
        "path.reserved",
        "path.invalid_name",
        "path.not_markdown",
        "ref.unsupported_kind",
        "ref.missing_path",
        "title.invalid",
        "asset.ref_unsupported",
        "link.root_anchored",
        "link.raw_url",
        "commit.identity_missing",
        "commit.failed",
        "push.rejected",
        "push.failed",
    }
)

MESSAGE_SHAPE = re.compile(r"^\[github_wiki:[a-z_]+\.[a-z_]+\] .+ Hint: .+\.$")


def test_code_table_matches_final_spec_exactly() -> None:
    assert len(EXPECTED_CODES) == 48
    assert set(CODES) == EXPECTED_CODES


def test_sidebar_codes_are_deferred() -> None:
    assert [code for code in CODES if code.startswith("sidebar.")] == []


def test_only_stale_plan_is_a_warning() -> None:
    warnings = {code for code, spec in CODES.items() if spec.kind == "W"}
    errors = {code for code, spec in CODES.items() if spec.kind == "E"}

    assert warnings == {"sync.stale_plan"}
    assert len(errors) == 47


@pytest.mark.parametrize("code", sorted(EXPECTED_CODES))
def test_every_code_has_a_default_hint(code: str) -> None:
    spec = CODES[code]

    assert isinstance(spec, CodeSpec)
    assert spec.default_hint.strip() != ""


@pytest.mark.parametrize("code", sorted(EXPECTED_CODES))
def test_every_code_renders_the_gw_p12_shape(code: str) -> None:
    error = GithubWikiError(code, "Something went wrong")

    assert MESSAGE_SHAPE.match(str(error)), str(error)
    assert str(error).startswith(f"[{NAMESPACE}:{code}] Something went wrong.")
    assert str(error).endswith(f"Hint: {CODES[code].default_hint.rstrip('.')}.")


def test_error_is_a_configuration_error_carrying_its_parts() -> None:
    error = GithubWikiError(
        "sync.diverged",
        "Local and remote histories diverged",
        context={"workdir": "/w", "sha": "abc1234"},
        hint="reconcile in the workdir",
    )

    assert isinstance(error, ConfigurationError)
    assert error.code == "sync.diverged"
    assert error.summary == "Local and remote histories diverged"
    assert error.context == {"workdir": "/w", "sha": "abc1234"}
    assert error.hint == "reconcile in the workdir"


def test_context_renders_as_quoted_key_value_pairs_in_order() -> None:
    error = GithubWikiError(
        "sync.diverged",
        "Histories diverged.",
        context={"workdir": "/w", "sha": "abc1234"},
        hint="reconcile",
    )

    assert str(error) == (
        "[github_wiki:sync.diverged] Histories diverged. "
        "workdir='/w' sha='abc1234'. Hint: reconcile."
    )


def test_without_context_no_empty_segment_is_rendered() -> None:
    error = GithubWikiError("auth.rejected", "Credentials rejected", hint="check scope")

    assert str(error) == (
        "[github_wiki:auth.rejected] Credentials rejected. Hint: check scope."
    )


def test_none_context_values_are_skipped() -> None:
    error = GithubWikiError(
        "sync.diverged",
        "Histories diverged",
        context={"workdir": "/w", "sha": None},
        hint="reconcile",
    )

    assert "sha=" not in str(error)
    assert "workdir='/w'" in str(error)


def test_explicit_hint_overrides_default_and_default_is_used_otherwise() -> None:
    explicit = GithubWikiError("auth.rejected", "Rejected", hint="try something else")
    default = GithubWikiError("auth.rejected", "Rejected")

    assert explicit.hint == "try something else"
    assert default.hint == CODES["auth.rejected"].default_hint
    assert str(explicit).endswith("Hint: try something else.")


def test_trailing_periods_are_normalised_in_summary_and_hint() -> None:
    error = GithubWikiError("auth.rejected", "Rejected...", hint="Fix it.")

    assert str(error) == "[github_wiki:auth.rejected] Rejected. Hint: Fix it."


def test_multi_line_context_values_stay_on_one_logical_line() -> None:
    error = GithubWikiError(
        "sync.git_failed",
        "git failed",
        context={"stderr": "fatal: first\nfatal: second\r\nthird"},
    )

    assert "\n" not in str(error)
    assert "stderr='fatal: first | fatal: second | third'" in str(error)
    assert MESSAGE_SHAPE.match(str(error))


def test_unknown_code_is_rejected() -> None:
    with pytest.raises(ValueError, match="sidebar.unmanaged_exists"):
        GithubWikiError("sidebar.unmanaged_exists", "Not shipped in this change")


def test_render_message_serves_warnings_that_are_never_raised() -> None:
    message = render_message(
        "sync.stale_plan",
        "Plan read the local clone without fetching",
        context={"last_sync": "2026-01-01T00:00:00Z"},
    )

    assert MESSAGE_SHAPE.match(message)
    assert message.startswith("[github_wiki:sync.stale_plan] Plan read the local clone")
    assert "last_sync='2026-01-01T00:00:00Z'" in message
    assert message.endswith(f"Hint: {CODES['sync.stale_plan'].default_hint.rstrip('.')}.")


# -- empty hint rule: the rendered text and `.hint` always agree (S4.F3) ------

EMPTY_HINTS = ["", "   ", "\n", "\t \r\n"]


@pytest.mark.parametrize("hint", EMPTY_HINTS)
def test_an_empty_or_blank_hint_falls_back_to_the_code_default(hint: str) -> None:
    default = CODES["auth.rejected"].default_hint
    error = GithubWikiError("auth.rejected", "Rejected", hint=hint)

    assert error.hint == default
    assert str(error).endswith(f"Hint: {default.rstrip('.')}.")
    assert MESSAGE_SHAPE.fullmatch(str(error))


@pytest.mark.parametrize("hint", EMPTY_HINTS)
def test_render_message_applies_the_same_empty_hint_rule(hint: str) -> None:
    error = GithubWikiError("sync.stale_plan", "Stale", hint=hint)
    rendered = render_message("sync.stale_plan", "Stale", hint=hint)

    assert rendered == str(error)
    assert error.hint == CODES["sync.stale_plan"].default_hint


def test_a_hint_with_content_is_kept_and_none_still_means_default() -> None:
    kept = GithubWikiError("auth.rejected", "Rejected", hint="  try this  ")
    absent = GithubWikiError("auth.rejected", "Rejected", hint=None)

    assert kept.hint == "  try this  "
    assert str(kept).endswith("Hint: try this.")
    assert absent.hint == CODES["auth.rejected"].default_hint


# -- single-line guarantee for user-controlled summary and hint (S3.F1) ------

HOSTILE_TEXTS = [
    "first\nsecond",
    "first\r\nsecond",
    "first\rsecond",
    "first\n\n\nsecond",
    "first\vsecond\fthird",
    "first\x85second",
    "first\u2028second\u2029third",
]
EDGE_LINE_BREAKS = ["trailing newline\n", "\nleading newline", "\r\nboth\r\n"]


def assert_single_logical_line(message: str) -> None:
    assert len(message.splitlines()) == 1, repr(message)
    assert not any(unicodedata.category(char) == "Cc" for char in message), repr(message)
    assert MESSAGE_SHAPE.fullmatch(message), repr(message)


@pytest.mark.parametrize("text", HOSTILE_TEXTS)
def test_line_breaks_in_the_summary_are_folded_onto_one_line(text: str) -> None:
    message = render_message("auth.rejected", text, hint="fix it")

    assert_single_logical_line(message)
    assert " | " in message
    assert message.startswith("[github_wiki:auth.rejected] ")


@pytest.mark.parametrize("text", HOSTILE_TEXTS)
def test_line_breaks_in_the_hint_are_folded_onto_one_line(text: str) -> None:
    message = render_message("auth.rejected", "Rejected", hint=text)

    assert_single_logical_line(message)
    assert "Hint: " in message


@pytest.mark.parametrize("text", EDGE_LINE_BREAKS)
def test_line_breaks_at_the_edges_are_trimmed_not_folded(text: str) -> None:
    summary = render_message("auth.rejected", text, hint="fix it")
    hint = render_message("auth.rejected", "Rejected", hint=text)

    for message in (summary, hint):
        assert_single_logical_line(message)
        assert " | " not in message


def test_summary_folding_keeps_the_surrounding_text_in_order() -> None:
    message = render_message("auth.rejected", "Unknown setting 'a\nb'", hint="fix\r\nit")

    assert message == (
        "[github_wiki:auth.rejected] Unknown setting 'a | b'. Hint: fix | it."
    )


@pytest.mark.parametrize(
    ("raw", "escaped"),
    [
        ("tab\there", "tab\\there"),
        ("esc\x1b[31mred", "esc\\x1b[31mred"),
        ("nul\x00byte", "nul\\x00byte"),
        ("del\x7fchar", "del\\x7fchar"),
    ],
    ids=["tab", "ansi-escape", "nul", "del"],
)
def test_other_control_characters_are_escaped_in_summary_hint_and_context(
    raw: str, escaped: str
) -> None:
    message = render_message(
        "auth.rejected", f"Rejected {raw}", context={"value": raw}, hint=f"try {raw}"
    )

    assert_single_logical_line(message)
    assert f"Rejected {escaped}." in message
    assert f"value='{escaped}'" in message
    assert f"Hint: try {escaped}." in message


@pytest.mark.parametrize(
    ("raw", "escaped"),
    [
        ("a\u202eb", "a\\u202eb"),  # right-to-left override
        ("a\u202db", "a\\u202db"),  # left-to-right override
        ("a\u202ab", "a\\u202ab"),  # left-to-right embedding
        ("a\u2066b\u2069", "a\\u2066b\\u2069"),  # isolates
        ("a\u200eb\u200f", "a\\u200eb\\u200f"),  # directional marks
        ("a\u061cb", "a\\u061cb"),  # Arabic letter mark
        ("a\u200bb", "a\\u200bb"),  # zero-width space
        ("a\u200cb\u200d", "a\\u200cb\\u200d"),  # zero-width (non-)joiner
        ("a\u2060b", "a\\u2060b"),  # word joiner
        ("a\ufeffb", "a\\ufeffb"),  # zero-width no-break space / BOM
        ("a\x85b", "a\\x85b"),  # C1 next-line is folded by the line-break rule
    ],
    ids=[
        "rlo",
        "lro",
        "lre",
        "isolates",
        "marks",
        "alm",
        "zwsp",
        "zwj-zwnj",
        "word-joiner",
        "bom",
        "nel",
    ],
)
def test_bidi_and_zero_width_characters_are_escaped_in_summary_hint_and_context(
    raw: str, escaped: str
) -> None:
    message = render_message(
        "auth.rejected", f"Rejected {raw}", context={"value": raw}, hint=f"try {raw}"
    )

    assert_single_logical_line(message)
    for invisible in "\u202e\u202d\u202a\u2066\u2069\u200e\u200f\u061c\u200b\u200c\u200d\u2060\ufeff":
        assert invisible not in message
    if raw != "a\x85b":
        assert f"Rejected {escaped}." in message
        assert f"value='{escaped}'" in message
        assert f"Hint: try {escaped}." in message


def test_a_spoofed_trailing_hint_cannot_hide_behind_a_bidi_override() -> None:
    hostile = "ok\u202e .tnih ebyam"

    message = render_message("auth.rejected", "Rejected", context={"path": hostile})

    assert "\u202e" not in message
    assert "path='ok\\u202e .tnih ebyam'" in message


def test_ordinary_non_ascii_text_is_not_escaped() -> None:
    message = render_message("auth.rejected", "Página ñandú 日本語 \U0001f600")

    assert "Página ñandú 日本語 \U0001f600." in message


def test_the_error_class_exposes_the_folded_message_and_the_raw_parts() -> None:
    error = GithubWikiError("auth.rejected", "bad\ninput", hint="do\nthis")

    assert_single_logical_line(str(error))
    assert error.summary == "bad\ninput"
    assert error.hint == "do\nthis"


def test_folding_never_changes_an_already_clean_message() -> None:
    message = render_message(
        "sync.diverged", "Histories diverged", context={"workdir": "/w"}, hint="reconcile"
    )

    assert message == (
        "[github_wiki:sync.diverged] Histories diverged. workdir='/w'. Hint: reconcile."
    )
