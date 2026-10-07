"""Unit tests for the ``github_wiki`` settings models (defaults, strictness, unions)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from wikiops.providers.github_wiki.settings import (
    AmbientAuth,
    BotIdentity,
    CustomIdentity,
    EnvAuth,
    GhAuth,
    GitIdentity,
    GithubWikiProviderSettings,
    SshAuth,
)


def make(**overrides: Any) -> dict[str, Any]:
    """Raw settings as the host hands them over (``provider_name`` is injected)."""
    return {"provider_name": "wiki", "repository": "acme/platform", **overrides}


def build(**overrides: Any) -> GithubWikiProviderSettings:
    return GithubWikiProviderSettings.model_validate(make(**overrides))


# -- defaults and strictness (S1.T2) ---------------------------------------


def test_minimal_settings_apply_every_default() -> None:
    settings = build()

    assert settings.repository == "acme/platform"
    assert settings.host == "github.com"
    assert settings.branch is None
    assert settings.workdir is None
    assert settings.sync_on_plan is True
    assert settings.auth == AmbientAuth(mode="ambient")
    assert settings.commit.identity == GitIdentity(mode="git")
    assert settings.commit.message == "docs(wiki): update via wikiops plugin {plugin_id}"
    assert settings.allow_auto_commit is True
    assert settings.allow_auto_push is False
    assert settings.local_backend.type == "local_files"
    assert settings.git_timeout_seconds == 120


def test_repository_is_the_only_required_setting() -> None:
    with pytest.raises(ValidationError) as caught:
        GithubWikiProviderSettings.model_validate({"provider_name": "wiki"})

    assert [error["loc"] for error in caught.value.errors()] == [("repository",)]


def test_default_instances_are_not_shared_between_settings() -> None:
    first, second = build(), build()

    assert first.auth is not second.auth
    assert first.commit is not second.commit
    assert first.local_backend is not second.local_backend


@pytest.mark.parametrize(
    "overrides",
    [
        {"allow_autocommit": True},
        {"generate_sidebar": True},
        {"auth": {"mode": "ambient", "extra": 1}},
        {"auth": {"mode": "env", "variable": "T", "account": "x"}},
        {"commit": {"unknown": 1}},
        {"commit": {"identity": {"mode": "git", "name": "x"}}},
    ],
    ids=[
        "root-typo",
        "generate-sidebar-not-shipped",
        "auth-ambient",
        "auth-env",
        "commit",
        "commit-identity",
    ],
)
def test_unknown_keys_are_rejected_at_every_nesting_level(
    overrides: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError) as caught:
        build(**overrides)

    assert "extra_forbidden" in {error["type"] for error in caught.value.errors()}


def test_local_backend_passes_unknown_options_through_for_the_backend_to_validate() -> None:
    settings = build(local_backend={"type": "local_files", "assets_dir": "media"})

    assert settings.local_backend.type == "local_files"
    assert settings.local_backend.model_extra == {"assets_dir": "media"}


@pytest.mark.parametrize("seconds", [5, 120, 3600])
def test_git_timeout_inside_the_range_is_accepted(seconds: int) -> None:
    assert build(git_timeout_seconds=seconds).git_timeout_seconds == seconds


@pytest.mark.parametrize("seconds", [4, 3601, 0, -1])
def test_git_timeout_outside_the_range_is_rejected(seconds: int) -> None:
    with pytest.raises(ValidationError) as caught:
        build(git_timeout_seconds=seconds)

    assert caught.value.errors()[0]["loc"] == ("git_timeout_seconds",)


def test_empty_workdir_is_rejected() -> None:
    with pytest.raises(ValidationError) as caught:
        build(workdir="")

    assert caught.value.errors()[0]["loc"] == ("workdir",)


# -- discriminated unions (S1.T3) ------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"mode": "env", "variable": "GH_WIKI_TOKEN"}, EnvAuth(mode="env", variable="GH_WIKI_TOKEN")),
        ({"mode": "gh", "account": "sgg10"}, GhAuth(mode="gh", account="sgg10")),
        ({"mode": "ssh"}, SshAuth(mode="ssh", key_path=None)),
        (
            {"mode": "ssh", "key_path": "~/.ssh/wiki_ed25519"},
            SshAuth(mode="ssh", key_path="~/.ssh/wiki_ed25519"),
        ),
        ({"mode": "ambient"}, AmbientAuth(mode="ambient")),
    ],
)
def test_all_auth_variants_validate(raw: dict[str, Any], expected: Any) -> None:
    assert build(auth=raw).auth == expected


@pytest.mark.parametrize(
    "raw",
    [
        {"mode": "env", "account": "x"},
        {"mode": "env"},
        {"mode": "gh"},
        {"mode": "gh", "account": ""},
        {"mode": "ssh", "variable": "T"},
        {"mode": "oauth"},
        {"variable": "T"},
    ],
    ids=[
        "env-with-gh-field",
        "env-without-variable",
        "gh-without-account",
        "gh-empty-account",
        "ssh-with-env-field",
        "unknown-mode",
        "missing-mode",
    ],
)
def test_auth_variants_reject_wrong_shapes(raw: dict[str, Any]) -> None:
    with pytest.raises(ValidationError) as caught:
        build(auth=raw)

    assert all(error["loc"][0] == "auth" for error in caught.value.errors())


def test_unknown_auth_mode_reports_the_union_tag_error() -> None:
    with pytest.raises(ValidationError) as caught:
        build(auth={"mode": "oauth"})

    assert caught.value.errors()[0]["type"] == "union_tag_invalid"
    assert caught.value.errors()[0]["loc"] == ("auth",)


@pytest.mark.parametrize("variable", ["GH_WIKI_TOKEN", "_x", "a1"])
def test_env_variable_names_that_are_valid_identifiers_are_accepted(variable: str) -> None:
    assert build(auth={"mode": "env", "variable": variable}).auth.variable == variable


@pytest.mark.parametrize("variable", ["1ABC", "A-B", "A B", "", "A=B", "A\nB"])
def test_env_variable_names_that_are_not_identifiers_are_rejected(variable: str) -> None:
    with pytest.raises(ValidationError):
        build(auth={"mode": "env", "variable": variable})


@pytest.mark.parametrize("key_path", ["/k/a\nb", "/k/a\x00b", ""])
def test_ssh_key_path_rejects_newline_nul_and_empty(key_path: str) -> None:
    with pytest.raises(ValidationError):
        build(auth={"mode": "ssh", "key_path": key_path})


def test_ssh_key_path_with_spaces_and_metacharacters_is_accepted_verbatim() -> None:
    path = "/home/me/my keys/wiki;key"

    assert build(auth={"mode": "ssh", "key_path": path}).auth.key_path == path


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"mode": "git"}, GitIdentity(mode="git")),
        (
            {"mode": "bot"},
            BotIdentity(mode="bot", name="wikiops", email="wikiops@users.noreply.github.com"),
        ),
        (
            {"mode": "bot", "name": "X", "email": "x@y"},
            BotIdentity(mode="bot", name="X", email="x@y"),
        ),
        (
            {"mode": "custom", "name": "N", "email": "e@x"},
            CustomIdentity(mode="custom", name="N", email="e@x"),
        ),
    ],
)
def test_identity_variants_validate(raw: dict[str, Any], expected: Any) -> None:
    assert build(commit={"identity": raw}).commit.identity == expected


@pytest.mark.parametrize(
    "raw",
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
def test_custom_identity_requires_name_and_email(raw: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        build(commit={"identity": raw})


@pytest.mark.parametrize(
    "identity",
    [
        {"mode": "custom", "name": "N", "email": "not-an-address"},
        {"mode": "custom", "name": "N", "email": "a b@x"},
        {"mode": "custom", "name": "N<x>", "email": "e@x"},
        {"mode": "bot", "name": "line\nbreak"},
        {"mode": "bot", "email": "no-at-sign"},
        {"mode": "oauth"},
    ],
    ids=["no-at", "space", "angle-brackets", "newline-name", "bot-email", "unknown-mode"],
)
def test_identity_values_that_would_corrupt_a_git_ident_are_rejected(
    identity: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        build(commit={"identity": identity})


def test_explicit_null_branch_and_key_path_mean_auto_detect_and_unpinned() -> None:
    settings = build(branch=None, auth={"mode": "ssh", "key_path": None})

    assert settings.branch is None
    assert settings.auth.key_path is None
