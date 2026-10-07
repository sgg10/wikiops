"""Unit tests for the ``github_wiki`` error vocabulary (``errors.py``)."""

from __future__ import annotations

import re

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
    assert len(EXPECTED_CODES) == 47
    assert set(CODES) == EXPECTED_CODES


def test_sidebar_codes_are_deferred() -> None:
    assert [code for code in CODES if code.startswith("sidebar.")] == []


def test_only_stale_plan_is_a_warning() -> None:
    warnings = {code for code, spec in CODES.items() if spec.kind == "W"}
    errors = {code for code, spec in CODES.items() if spec.kind == "E"}

    assert warnings == {"sync.stale_plan"}
    assert len(errors) == 46


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
