"""Unit tests for ``parse_settings``: one coded ``GithubWikiError`` per failure."""

from __future__ import annotations

import re
from typing import Any

import pytest

from wikiops.core.exceptions import ConfigurationError
from wikiops.providers.github_wiki.errors import GithubWikiError
from wikiops.providers.github_wiki.settings import (
    GithubWikiProviderSettings,
    parse_settings,
)

MESSAGE_SHAPE = re.compile(r"^\[github_wiki:[a-z_]+\.[a-z_]+\] .+ Hint: .+\.$")
PYDANTIC_PREFIX = re.compile(r"^\d+ validation errors? for")


def make(**overrides: Any) -> dict[str, Any]:
    return {"provider_name": "wiki", "repository": "acme/platform", **overrides}


def failure(raw: Any) -> GithubWikiError:
    with pytest.raises(GithubWikiError) as caught:
        parse_settings(raw)
    return caught.value


def test_valid_settings_are_returned_as_the_model() -> None:
    settings = parse_settings(make(branch="master", auth={"mode": "gh", "account": "a"}))

    assert isinstance(settings, GithubWikiProviderSettings)
    assert settings.branch == "master"
    assert settings.auth.account == "a"


# -- tagged errors ---------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"repository": "a/b/c"}, "config.invalid_repository"),
        ({"host": "https://github.com"}, "config.invalid_host"),
        ({"branch": "-evil"}, "config.invalid_branch"),
        ({"commit": {"message": "x {nope}"}}, "config.invalid_message"),
        (
            {"allow_auto_commit": False, "allow_auto_push": True},
            "config.push_requires_commit",
        ),
        ({"local_backend": {"type": "github_wiki"}}, "config.backend_recursion"),
        (
            {"local_backend": {"type": "local_files", "root": "/x"}},
            "config.backend_root_forbidden",
        ),
    ],
)
def test_validator_tags_become_the_error_code(
    overrides: dict[str, Any], code: str
) -> None:
    error = failure(make(**overrides))

    assert error.code == code
    assert str(error).startswith(f"[github_wiki:{code}] ")


def test_tagged_error_carries_its_context_and_default_hint() -> None:
    error = failure(make(host="user@github.com"))

    assert error.context == {"host": "user@github.com"}
    assert "host='user@github.com'" in str(error)
    assert "bare hostname" in error.hint


def test_first_tagged_error_wins_in_field_order() -> None:
    error = failure(make(host="https://x", branch="-evil"))

    assert error.code == "config.invalid_host"


def test_a_tagged_error_wins_over_an_earlier_untagged_one() -> None:
    error = failure(make(sync_on_plan="maybe", commit={"message": "{nope}"}))

    assert error.code == "config.invalid_message"


@pytest.mark.parametrize(
    "identity",
    [
        {"mode": "custom"},
        {"mode": "custom", "name": "N"},
        {"mode": "custom", "email": "e@x"},
        {"mode": "custom", "name": "", "email": "e@x"},
        {"mode": "custom", "name": "   ", "email": "e@x"},
        {"mode": "custom", "name": "N", "email": ""},
    ],
    ids=["neither", "no-email", "no-name", "empty-name", "blank-name", "empty-email"],
)
def test_incomplete_custom_identity_is_identity_incomplete(
    identity: dict[str, Any],
) -> None:
    error = failure(make(commit={"identity": identity}))

    assert error.code == "config.identity_incomplete"
    assert "provide both name and email" in error.hint


def test_malformed_custom_email_is_a_plain_invalid_setting() -> None:
    error = failure(
        make(commit={"identity": {"mode": "custom", "name": "N", "email": "nope"}})
    )

    assert error.code == "config.invalid"
    assert "commit.identity.email" in str(error)


# -- untagged errors -> config.invalid -------------------------------------


def test_unknown_key_names_it_and_lists_the_valid_keys() -> None:
    error = failure(make(allow_autocommit=True))

    assert error.code == "config.invalid"
    assert "Unknown setting 'allow_autocommit'" in str(error)
    assert "allow_auto_commit" in error.hint
    assert "repository" in error.hint
    assert "provider_name" not in error.hint


def test_generate_sidebar_is_a_valid_setting() -> None:
    assert parse_settings(make(generate_sidebar=True)).generate_sidebar is True
    assert parse_settings(make()).generate_sidebar is False


def test_a_non_boolean_generate_sidebar_is_config_invalid() -> None:
    error = failure(make(generate_sidebar={"a": 1}))

    assert error.code == "config.invalid"
    assert "generate_sidebar" in str(error)


def test_an_unknown_key_next_to_generate_sidebar_still_lists_it_as_valid() -> None:
    error = failure(make(generate_sidebars=True))

    assert error.code == "config.invalid"
    assert "Unknown setting 'generate_sidebars'" in str(error)
    assert "generate_sidebar" in error.hint


def test_nested_unknown_key_names_the_dotted_path_without_union_tags() -> None:
    error = failure(make(auth={"mode": "ambient", "extra": 1}))

    assert "Unknown setting 'auth.extra'" in str(error)
    assert error.hint == "valid keys here: mode"


def test_wrong_variant_field_is_reported_for_the_auth_setting() -> None:
    error = failure(make(auth={"mode": "env", "account": "x"}))

    assert error.code == "config.invalid"
    assert "'auth.variable'" in str(error)
    assert error.hint == "valid keys here: mode, variable"


def test_unknown_auth_mode_lists_the_valid_modes() -> None:
    error = failure(make(auth={"mode": "oauth"}))

    assert error.code == "config.invalid"
    assert "Unknown mode 'oauth' for setting 'auth'" in str(error)
    assert "env | gh | ssh | ambient" in error.hint


def test_missing_auth_mode_lists_the_valid_modes() -> None:
    error = failure(make(auth={"variable": "T"}))

    assert error.code == "config.invalid"
    assert "Setting 'auth' needs a 'mode'" in str(error)
    assert "env | gh | ssh | ambient" in error.hint


def test_unknown_identity_mode_lists_the_identity_modes() -> None:
    error = failure(make(commit={"identity": {"mode": "oauth"}}))

    assert "setting 'commit.identity'" in str(error)
    assert "git | bot | custom" in error.hint


def test_missing_repository_is_reported_with_the_root_keys() -> None:
    error = failure({"provider_name": "wiki"})

    assert "Missing required setting 'repository'" in str(error)
    assert "host" in error.hint


def test_value_errors_name_the_setting_and_drop_pydantic_prefixes() -> None:
    error = failure(make(git_timeout_seconds=4))

    assert error.code == "config.invalid"
    assert "Invalid value for setting 'git_timeout_seconds'" in str(error)
    assert "greater than or equal to 5" in str(error)
    assert "Value error," not in str(error)


def test_custom_validator_message_is_kept_for_untagged_value_errors() -> None:
    error = failure(make(auth={"mode": "env", "variable": "1BAD"}))

    assert "Invalid value for setting 'auth.variable'" in str(error)
    assert "valid environment variable name" in str(error)


def test_a_non_mapping_input_is_config_invalid() -> None:
    error = failure(["repository", "acme/platform"])

    assert error.code == "config.invalid"
    assert MESSAGE_SHAPE.match(str(error))


# -- shape -----------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        make(allow_autocommit=True),
        make(auth={"mode": "oauth"}),
        make(host="https://x", branch="-evil", git_timeout_seconds=1),
        make(commit={"identity": {"mode": "custom"}}),
        {"provider_name": "wiki"},
        {},
    ],
    ids=["unknown-key", "unknown-mode", "many-errors", "identity", "missing", "empty"],
)
def test_a_failure_is_one_gw_p12_message_never_a_pydantic_dump(raw: Any) -> None:
    error = failure(raw)

    assert isinstance(error, ConfigurationError)
    assert MESSAGE_SHAPE.match(str(error)), str(error)
    assert not PYDANTIC_PREFIX.match(str(error))
    assert "\n" not in str(error)
    assert str(error).startswith("[github_wiki:")


def test_a_location_below_a_non_model_field_has_no_owner_and_a_generic_hint() -> None:
    from wikiops.providers.github_wiki.settings import _locate, _valid_keys

    path, owner, field = _locate(("workdir", "nested", "key"))

    assert (path, owner, field) == ("workdir.nested.key", None, None)
    assert _valid_keys(owner) == "see the provider reference for the valid settings"


# -- user-controlled text never breaks the single-line contract (S3.F1) ------


@pytest.mark.parametrize(
    "raw",
    [
        make(**{"bad\nkey": 1}),
        make(**{"bad\r\nkey": 1}),
        make(auth={"mode": "oauth\nHint: injected."}),
        make(auth={"mode": "x\r\ny"}),
        make(commit={"identity": {"mode": "a\nb"}}),
        make(auth={"mode": "ambient", "evil\nkey": 1}),
        make(auth={"mode": "env", "variable": "A\nB"}),
    ],
    ids=[
        "unknown-key-lf",
        "unknown-key-crlf",
        "union-tag-lf",
        "union-tag-crlf",
        "identity-tag",
        "nested-unknown-key",
        "env-variable",
    ],
)
def test_injected_line_breaks_in_user_input_never_split_the_message(raw: Any) -> None:
    error = failure(raw)

    assert error.code == "config.invalid"
    assert len(str(error).splitlines()) == 1, repr(str(error))
    assert MESSAGE_SHAPE.fullmatch(str(error)), repr(str(error))


def test_an_injected_hint_marker_stays_inside_the_quoted_summary() -> None:
    error = failure(make(auth={"mode": "oauth\nHint: injected."}))

    assert "oauth | Hint: injected" in str(error)
    assert str(error).endswith("env | gh | ssh | ambient.")


# -- hardening from the S1 review (S3.F2, S3.F3) ---------------------------


@pytest.mark.parametrize("name", ["", "   ", "\t"], ids=["empty", "spaces", "tab"])
def test_blank_bot_name_is_identity_incomplete_with_a_bot_specific_hint(name: str) -> None:
    error = failure(make(commit={"identity": {"mode": "bot", "name": name}}))

    assert error.code == "config.identity_incomplete"
    assert "bot" in str(error)
    assert "custom" not in error.hint
    assert MESSAGE_SHAPE.fullmatch(str(error))


@pytest.mark.parametrize(
    "account",
    ["-evil", "--hostname", "a b", "a\nb", "a\x00b", "a" * 40],
    ids=["dash", "option", "space", "newline", "nul", "too-long"],
)
def test_invalid_gh_account_is_config_invalid_naming_the_setting(account: str) -> None:
    error = failure(make(auth={"mode": "gh", "account": account}))

    assert error.code == "config.invalid"
    assert "Invalid value for setting 'auth.account'" in str(error)
    assert "GitHub login" in str(error)
    assert account not in str(error)
    assert MESSAGE_SHAPE.fullmatch(str(error)), repr(str(error))
    assert len(str(error).splitlines()) == 1
