"""Table-driven tests: one row per ``config.*`` code raised by settings validators."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from wikiops.providers.github_wiki.settings import (
    CodedValueError,
    GithubWikiProviderSettings,
)


def make(**overrides: Any) -> dict[str, Any]:
    return {"provider_name": "wiki", "repository": "acme/platform", **overrides}


def code_of(**overrides: Any) -> str:
    """Validate and return the code carried by the first validator error."""
    with pytest.raises(ValidationError) as caught:
        GithubWikiProviderSettings.model_validate(make(**overrides))
    error = caught.value.errors()[0]
    original = error.get("ctx", {}).get("error")
    assert isinstance(original, CodedValueError), error
    return original.code


def is_valid(**overrides: Any) -> bool:
    GithubWikiProviderSettings.model_validate(make(**overrides))
    return True


# -- config.push_requires_commit -------------------------------------------


def test_push_without_commit_is_rejected() -> None:
    assert (
        code_of(allow_auto_commit=False, allow_auto_push=True)
        == "config.push_requires_commit"
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"allow_auto_push": True},
        {"allow_auto_commit": False},
        {"allow_auto_commit": True, "allow_auto_push": True},
        {"allow_auto_commit": False, "allow_auto_push": False},
    ],
    ids=["only-push", "only-commit-off", "both-on", "both-off"],
)
def test_valid_commit_and_push_combinations(overrides: dict[str, Any]) -> None:
    assert is_valid(**overrides)


def test_only_push_enabled_keeps_commit_on_by_default() -> None:
    settings = GithubWikiProviderSettings.model_validate(make(allow_auto_push=True))

    assert settings.allow_auto_commit is True
    assert settings.allow_auto_push is True


# -- config.backend_* ------------------------------------------------------


def test_backend_type_github_wiki_is_recursion() -> None:
    assert code_of(local_backend={"type": "github_wiki"}) == "config.backend_recursion"


@pytest.mark.parametrize("forbidden", ["root", "provider_name", "provider_api_version"])
def test_backend_injected_keys_are_forbidden(forbidden: str) -> None:
    backend = {"type": "local_files", forbidden: "/tmp/x"}

    assert code_of(local_backend=backend) == "config.backend_root_forbidden"


def test_recursion_is_reported_before_forbidden_keys() -> None:
    backend = {"type": "github_wiki", "root": "/tmp/x"}

    assert code_of(local_backend=backend) == "config.backend_recursion"


@pytest.mark.parametrize(
    "backend",
    [
        {},
        {"type": "local_files"},
        {"type": "local_files", "assets_dir": "media", "overwrite_existing": True},
        {"type": "fake_files"},
    ],
)
def test_valid_backend_selections(backend: dict[str, Any]) -> None:
    assert is_valid(local_backend=backend)


# -- config.invalid_message ------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "x {nope}",
        "{}",
        "{0}",
        "{plugin_id.__class__}",
        "{plugin_id[0]}",
        "{plugin_id!r}",
        "{plugin_id:>10}",
        "unbalanced {",
        "",
        "   ",
        "bad \x00 nul",
    ],
    ids=[
        "unknown-placeholder",
        "positional",
        "indexed",
        "attribute",
        "item",
        "conversion",
        "format-spec",
        "malformed",
        "empty",
        "blank",
        "nul",
    ],
)
def test_invalid_commit_messages(message: str) -> None:
    assert code_of(commit={"message": message}) == "config.invalid_message"


@pytest.mark.parametrize(
    "message",
    [
        "docs(wiki): {plugin_id}",
        "sync {provider_name}",
        "update {page_count} pages via {plugin_id} on {provider_name}",
        "literal {{braces}} are fine",
        "no placeholders at all",
    ],
)
def test_valid_commit_messages(message: str) -> None:
    assert is_valid(commit={"message": message})


# -- config.invalid_branch -------------------------------------------------


@pytest.mark.parametrize(
    "branch",
    [
        "",
        " ",
        "my branch",
        "a\tb",
        "a..b",
        "-evil",
        "a~b",
        "a^b",
        "a:b",
        "a?b",
        "a*b",
        "a[b",
        "a\\b",
        "a\x01b",
        "a\x7fb",
        "a\x85b",
        "a\x9fb",
        "feature/",
        "/feature",
        "a//b",
        "topic.lock",
        "dir.lock/x",
        "a@{b",
        "@",
        ".hidden",
        "a/.hidden",
        "ends.",
    ],
)
def test_invalid_branch_names(branch: str) -> None:
    assert code_of(branch=branch) == "config.invalid_branch"


@pytest.mark.parametrize(
    "branch", ["master", "main", "feature/wiki-update", "release-1.2", "v1.0.0", "a@b"]
)
def test_valid_branch_names(branch: str) -> None:
    assert is_valid(branch=branch)


# -- config.invalid_repository ---------------------------------------------


@pytest.mark.parametrize(
    "repository",
    [
        "a/b/c",
        "platform",
        "",
        "acme/",
        "/platform",
        "acme/platform.wiki",
        "acme/platform.WIKI",
        "acme/platform.git",
        "acme/platform.wiki.git",
        "https://github.com/acme/platform",
        "git@github.com:acme/platform",
        "user:pw@acme/platform",
        "acme/plat form",
        "-acme/platform",
        "acme/..",
        "acme/.",
    ],
)
def test_invalid_repositories(repository: str) -> None:
    assert code_of(repository=repository) == "config.invalid_repository"


@pytest.mark.parametrize(
    "repository", ["acme/platform", "Acme-Corp/my.repo_1", "a/.github", "a1/b-c"]
)
def test_valid_repositories(repository: str) -> None:
    assert is_valid(repository=repository)


# -- config.invalid_host ---------------------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "https://github.com",
        "http://github.com",
        "user@github.com",
        "github.com/acme",
        "github.com:443",
        "git hub.com",
        "github.com\n",
        "-oProxyCommand=evil",
        "-github.com",
        "",
        "a..b",
        ".github.com",
        "github.com.",
        "a.-b.com",
        "github.com-",
    ],
)
def test_invalid_hosts(host: str) -> None:
    assert code_of(host=host) == "config.invalid_host"


@pytest.mark.parametrize(
    "host", ["github.com", "ghe.acme.io", "GitHub.COM", "localhost", "wiki-1.example.org"]
)
def test_valid_hosts(host: str) -> None:
    assert is_valid(host=host)


# -- coded errors carry a summary and hint ---------------------------------


def test_coded_errors_carry_summary_context_and_hint() -> None:
    with pytest.raises(ValidationError) as caught:
        GithubWikiProviderSettings.model_validate(make(branch="-evil"))

    original = caught.value.errors()[0]["ctx"]["error"]
    assert isinstance(original, CodedValueError)
    assert original.code == "config.invalid_branch"
    assert "starts with '-'" in original.summary
    assert original.context == {"branch": "-evil"}
