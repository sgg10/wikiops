"""The documentation and the host skill agree with the provider (S12).

Passive documents get structural checks: the error-code tables list exactly the closed
vocabulary of ``errors.CODES``, the documented defaults are the real ones, the pages
mention the behaviors users must know about, and the example config of the host skill is
loadable and valid for the provider (offline: nothing runs).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.support.fake_git_runner import FakeGitRunner
from tests.support.provider_harness import real_resolver
from wikiops.core.config_loader import ConfigLoader
from wikiops.providers.github_wiki import GithubWikiProviderFactory
from wikiops.providers.github_wiki.errors import CODES
from wikiops.providers.github_wiki.settings import GithubWikiProviderSettings

ROOT = Path(__file__).resolve().parents[4]
REFERENCE = ROOT / "docs" / "reference" / "github-wiki.md"
SKILL = ROOT / "skills" / "wikiops-host"
SKILL_REFERENCE = SKILL / "references" / "github-wiki-provider.md"
EXAMPLE = SKILL / "assets" / "config.github-wiki.example.yaml"
CODE = re.compile(r"[a-z_]+\.[a-z_]+")


def section(text: str, heading: str) -> str:
    """The text below ``heading`` up to the next heading of the same or a higher level."""
    level = len(heading) - len(heading.lstrip("#"))
    start = text.index(f"{heading}\n")
    following = re.search(rf"^#{{1,{level}}} ", text[start + len(heading) :], re.MULTILINE)
    return text[start : start + len(heading) + (following.start() if following else len(text))]


def table_codes(text: str) -> set[str]:
    """Every code in the first column of a Markdown table row."""
    found: set[str] = set()
    for row in re.findall(r"^\| ([^|]+) \|", text, re.MULTILINE):
        found.update(re.findall(rf"`({CODE.pattern})`", row))
    return found


def test_the_reference_code_table_lists_exactly_the_closed_vocabulary() -> None:
    documented = table_codes(section(REFERENCE.read_text(encoding="utf-8"), "## Error Codes"))

    assert documented  # the table was found and parsed
    assert documented == set(CODES)


def test_the_skill_reaction_tables_cover_exactly_the_closed_vocabulary() -> None:
    documented = table_codes(section(SKILL_REFERENCE.read_text(encoding="utf-8"), "## Reading a failure"))

    assert documented == set(CODES)


@pytest.mark.parametrize("code", sorted(CODES))
def test_every_documented_code_is_a_real_code(code: str) -> None:
    text = REFERENCE.read_text(encoding="utf-8") + SKILL_REFERENCE.read_text(encoding="utf-8")

    assert f"`{code}`" in text


def test_the_reference_states_the_behaviors_users_must_know() -> None:
    text = REFERENCE.read_text(encoding="utf-8")

    assert "hooks" in text and "disabled" in text  # network commands run with hooks disabled
    assert "generate_sidebar" in text and "not available" in text  # sidebar is not supported yet
    assert "create the first page in the web ui" in text.lower().replace("**", "")
    assert "allow_auto_commit` | `true`" in text and "allow_auto_push` | `false`" in text
    for mode in ("env", "gh", "ssh", "ambient"):
        assert f"| `{mode}`" in text
    for part in ("pending.json", "workdir.dirty", "workdir.locked", "push.rejected"):
        assert part in text


def test_the_documented_defaults_are_the_real_defaults() -> None:
    settings = GithubWikiProviderSettings.model_validate(
        {"provider_name": "docs", "repository": "acme/platform"}
    )

    assert settings.allow_auto_commit is True and settings.allow_auto_push is False
    assert settings.sync_on_plan is True and settings.host == "github.com"
    assert settings.auth.mode == "ambient" and settings.commit.identity.mode == "git"
    assert settings.git_timeout_seconds == 120 and settings.local_backend.type == "local_files"
    assert settings.commit.message == "docs(wiki): update via wikiops plugin {plugin_id}"
    text = REFERENCE.read_text(encoding="utf-8")
    assert "`docs(wiki): update via wikiops plugin {plugin_id}`" in text
    assert "| `git_timeout_seconds` | `120` |" in text


def test_the_skill_never_offers_the_sidebar() -> None:
    text = SKILL_REFERENCE.read_text(encoding="utf-8")

    assert "generate_sidebar" in text and "not supported yet" in text


def test_the_example_config_loads_and_the_factory_validates_it_offline(tmp_path: Path) -> None:
    config = ConfigLoader().load(str(EXAMPLE))
    runner = FakeGitRunner()
    factory = GithubWikiProviderFactory(runner=runner, backends=real_resolver())

    definition = config.providers["wiki"]
    provider = factory.create(dict(definition.settings))

    assert definition.type == "github_wiki"
    assert provider.settings.repository == "acme/platform"
    assert provider.settings.allow_auto_commit is True and provider.settings.allow_auto_push is False
    assert provider.settings.auth.mode == "env" and provider.settings.provider_name == "wiki"
    assert config.profiles["default"].refs["home"].locator == {"path": "Home.md"}
    assert runner.calls == []


def test_the_skill_eval_queries_are_valid_json_with_github_wiki_cases() -> None:
    queries = json.loads((SKILL / "assets" / "eval-queries.json").read_text(encoding="utf-8"))

    assert all(set(item) == {"query", "should_trigger"} for item in queries)
    assert sum("github_wiki" in item["query"] for item in queries if item["should_trigger"]) >= 2
