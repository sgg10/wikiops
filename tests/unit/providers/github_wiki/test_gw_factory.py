"""``GithubWikiProviderFactory``: the entry point of the provider (GW-P1, GW-P14).

The factory declares no ``settings_model`` and validates raw settings itself, so every
configuration problem reaches the user as ONE coded ``[github_wiki:config.*]`` error.
Creating a provider is offline: a fake runner proves no process is started.
"""

from __future__ import annotations

import importlib
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import pytest
from wikiops_sdk.contracts import DocumentProvider, ProviderSettings

from tests.support.fake_file_backend import manager_with
from tests.support.fake_git_runner import FakeGitRunner
from tests.support.provider_harness import real_resolver
from wikiops.cli import app as cli_app
from wikiops.core.exceptions import ConfigurationError
from wikiops.core.provider_manager import ProviderManager, TargetDescribingProvider
from wikiops.providers.github_wiki import GithubWikiProviderFactory
from wikiops.providers.github_wiki.errors import GithubWikiError
from wikiops.providers.github_wiki.provider import GithubWikiProvider
from wikiops.providers.github_wiki.settings import GithubWikiProviderSettings
from wikiops.providers.local_files import LocalFilesProviderFactory

REPO_ROOT = Path(__file__).resolve().parents[4]
ENTRY_POINT = "github_wiki"
ENTRY_POINT_TARGET = "wikiops.providers.github_wiki:GithubWikiProviderFactory"
RAW: dict[str, Any] = {"provider_name": "docs", "repository": "acme/platform"}


def factory(runner: FakeGitRunner | None = None) -> GithubWikiProviderFactory:
    return GithubWikiProviderFactory(runner=runner or FakeGitRunner(), backends=real_resolver())


def manager(runner: FakeGitRunner | None = None) -> ProviderManager:
    return manager_with(factory(runner), LocalFilesProviderFactory())


# -- the factory ------------------------------------------------------------------------------------


def test_the_factory_identifies_the_provider_and_leaves_validation_to_itself() -> None:
    assert GithubWikiProviderFactory.provider_id == "github_wiki"
    assert GithubWikiProviderFactory.settings_model is None
    assert manager().settings_model_for("github_wiki") is None


def test_create_returns_a_provider_over_the_parsed_settings_without_running_anything() -> None:
    runner = FakeGitRunner()

    provider = factory(runner).create(dict(RAW))

    assert isinstance(provider, GithubWikiProvider)
    assert isinstance(provider.settings, GithubWikiProviderSettings)
    assert provider.settings.repository == "acme/platform"
    assert provider.settings.provider_name == "docs"
    assert runner.calls == []  # nothing ran: no git, no gh, no network


def test_create_accepts_already_parsed_settings() -> None:
    parsed = GithubWikiProviderSettings.model_validate(RAW)

    assert factory().create(parsed).settings is parsed


def test_the_production_wiring_needs_no_arguments() -> None:
    provider = GithubWikiProviderFactory().create(dict(RAW))

    assert isinstance(provider, DocumentProvider)
    assert isinstance(provider, TargetDescribingProvider)


# -- through the host's provider manager --------------------------------------------------------------------


def test_a_provider_created_through_the_manager_passes_the_host_checks() -> None:
    created = manager().create("github_wiki", dict(RAW))

    assert isinstance(created, DocumentProvider)
    assert isinstance(created.settings, ProviderSettings)  # the compatibility check ran on it
    assert created.provider_id == "github_wiki"


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ({"provider_name": "docs"}, "config.invalid"),
        ({**RAW, "repository": "not a repo"}, "config.invalid_repository"),
        ({**RAW, "sync_on_plna": True}, "config.invalid"),
        ({**RAW, "allow_auto_commit": False, "allow_auto_push": True}, "config.push_requires_commit"),
        ({**RAW, "auth": {"mode": "env"}}, "config.invalid"),
    ],
    ids=["no-repository", "bad-repository", "unknown-key", "push-without-commit", "env-without-variable"],
)
def test_configuration_errors_surface_with_their_code_through_the_manager(
    raw: dict[str, Any], code: str
) -> None:
    with pytest.raises(ConfigurationError) as caught:
        manager().create("github_wiki", raw)

    text = str(caught.value)
    assert text.startswith("Failed to create provider of type 'github_wiki': [github_wiki:")
    assert f"[github_wiki:{code}]" in text


def test_an_offline_validation_error_keeps_its_code_through_the_manager() -> None:
    with pytest.raises(GithubWikiError) as caught:
        manager().create("github_wiki", {**RAW, "local_backend": {"type": "nope"}})

    assert caught.value.code == "config.backend_unknown"
    assert str(caught.value).startswith("[github_wiki:config.backend_unknown]")


def test_a_setting_error_runs_nothing_either() -> None:
    runner = FakeGitRunner()

    with pytest.raises(ConfigurationError):
        manager(runner).create("github_wiki", {**RAW, "repository": "x"})

    assert runner.calls == []


# -- registration -----------------------------------------------------------------------------


def test_pyproject_registers_the_entry_point_and_the_target_imports() -> None:
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    header = '[project.entry-points."wikiops.providers"]'
    section = pyproject.split(header, 1)[1].split("\n[", 1)[0]

    assert f'{ENTRY_POINT} = "{ENTRY_POINT_TARGET}"' in section
    module_name, _, attribute = ENTRY_POINT_TARGET.partition(":")
    assert getattr(importlib.import_module(module_name), attribute) is GithubWikiProviderFactory


def test_the_installed_distribution_lists_the_entry_point() -> None:
    names = {item.name for item in entry_points(group=ProviderManager.ENTRYPOINT_GROUP)}

    assert {"azure_devops_wiki", "local_files", "github_wiki"} <= names


def test_the_providers_command_lists_github_wiki(cli_runner) -> None:  # noqa: ANN001
    result = cli_runner.invoke(cli_app.app, ["providers"])

    assert result.exit_code == 0
    assert "- github_wiki" in result.stdout
    assert "- local_files" in result.stdout
