"""Unit tests for the credential strategies of ``github_wiki``: remote and offline checks.

Covers GW-P5 (credential-free remote derivation), GW-P6 (offline checks) and
GW-A2 (no silent fallback between credential sources). The transport (per
command credentials) is covered by ``test_gw_auth_transport.py``.
"""

from __future__ import annotations

import builtins
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.support.fake_git_runner import FakeGitRunner
from wikiops.providers.github_wiki.auth import (
    AmbientStrategy,
    EnvTokenStrategy,
    GhTokenStrategy,
    SshStrategy,
    build_strategy,
)
from wikiops.providers.github_wiki.errors import GithubWikiError
from wikiops.providers.github_wiki.ports import CredentialStrategy
from wikiops.providers.github_wiki.settings import parse_settings

TOKEN = "ghp_secretvalue123"


def settings_for(auth: dict | None, *, host: str | None = None, repository: str = "acme/platform"):
    raw: dict = {"repository": repository, "provider_name": "wiki"}
    if auth is not None:
        raw["auth"] = auth
    if host is not None:
        raw["host"] = host
    return parse_settings(raw)


def strategy_for(auth: dict | None, *, host: str | None = None):
    return build_strategy(
        settings_for(auth, host=host),
        runner=FakeGitRunner(),
        environ={},
        which=lambda name: f"/usr/bin/{name}",
    )


# -- remote derivation (GW-P5) --------------------------------------------------

REMOTE_CASES = [
    ({"mode": "env", "variable": "GH_WIKI_TOKEN"}, "github.com", "https://github.com/acme/platform.wiki.git"),
    ({"mode": "gh", "account": "sgg10"}, "github.com", "https://github.com/acme/platform.wiki.git"),
    ({"mode": "gh", "account": "a"}, "ghe.acme.io", "https://ghe.acme.io/acme/platform.wiki.git"),
    (None, "github.com", "https://github.com/acme/platform.wiki.git"),
    ({"mode": "ambient"}, "ghe.acme.io", "https://ghe.acme.io/acme/platform.wiki.git"),
    ({"mode": "ssh"}, "github.com", "git@github.com:acme/platform.wiki.git"),
    ({"mode": "ssh", "key_path": "/k/wiki"}, "ghe.acme.io", "git@ghe.acme.io:acme/platform.wiki.git"),
]


@pytest.mark.parametrize(("auth", "host", "remote"), REMOTE_CASES)
def test_remote_is_derived_from_settings_only(auth: dict | None, host: str, remote: str) -> None:
    assert strategy_for(auth, host=host).remote_url == remote


@pytest.mark.parametrize(("auth", "host", "remote"), REMOTE_CASES)
def test_the_transport_carries_the_same_credential_free_remote(
    auth: dict | None, host: str, remote: str
) -> None:
    runner = FakeGitRunner()
    runner.script(["gh", "auth", "token"], stdout=f"{TOKEN}\n")
    strategy = build_strategy(
        settings_for(auth, host=host),
        runner=runner,
        environ={"GH_WIKI_TOKEN": TOKEN},
        which=lambda name: f"/usr/bin/{name}",
    )

    assert strategy.transport().remote_url == remote


@pytest.mark.parametrize(("auth", "host", "remote"), REMOTE_CASES)
def test_remote_never_contains_credentials(auth: dict | None, host: str, remote: str) -> None:
    strategy = strategy_for(auth, host=host)

    assert strategy.remote_url.count("@") == (1 if remote.startswith("git@") else 0)
    assert strategy.remote_url.startswith(("https://", "git@"))
    assert "x-access-token" not in strategy.remote_url
    assert TOKEN not in strategy.remote_url


@pytest.mark.parametrize(
    "auth",
    [
        {"mode": "env", "variable": "T"},
        {"mode": "gh", "account": "a"},
        {"mode": "ssh"},
        {"mode": "ambient"},
    ],
)
def test_strategies_expose_what_the_credential_strategy_port_declares(auth: dict) -> None:
    strategy: CredentialStrategy = strategy_for(auth)

    assert strategy.label.strip() != ""
    assert strategy.remote_url.endswith("/acme/platform.wiki.git") or strategy.remote_url.endswith(
        ":acme/platform.wiki.git"
    )
    assert callable(strategy.check_offline)
    assert callable(strategy.transport)


# -- build_strategy picks the strategy of the configured mode -----------------------


@pytest.mark.parametrize(
    ("auth", "expected"),
    [
        ({"mode": "env", "variable": "T"}, EnvTokenStrategy),
        ({"mode": "gh", "account": "a"}, GhTokenStrategy),
        ({"mode": "ssh"}, SshStrategy),
        ({"mode": "ambient"}, AmbientStrategy),
        (None, AmbientStrategy),
    ],
)
def test_build_strategy_selects_the_configured_mode(auth: dict | None, expected: type) -> None:
    assert type(strategy_for(auth)) is expected


def test_an_unsupported_auth_mode_fails_loudly_instead_of_falling_back_to_ambient() -> None:
    # model_construct bypasses validation: the guard must be an explicit coded
    # error, not an `assert` that `python -O` strips (silent ambient fallback).
    settings = settings_for({"mode": "ambient"}).model_construct(
        repository="acme/platform", host="github.com", auth=SimpleNamespace(mode="oauth")
    )

    with pytest.raises(GithubWikiError) as caught:
        build_strategy(settings, runner=FakeGitRunner())

    assert caught.value.code == "config.invalid"
    assert "oauth" in str(caught.value)


@pytest.mark.parametrize("auth", [None, object(), "ambient"])
def test_any_non_strategy_auth_value_is_rejected(auth: object) -> None:
    settings = settings_for({"mode": "ambient"}).model_construct(
        repository="acme/platform", host="github.com", auth=auth
    )

    with pytest.raises(GithubWikiError) as caught:
        build_strategy(settings, runner=FakeGitRunner())

    assert caught.value.code == "config.invalid"


# -- gh token lookup has its own short budget ----------------------------------------


@pytest.mark.parametrize(
    ("git_timeout", "expected"),
    [(120, 15.0), (3600, 15.0), (15, 15.0), (10, 10.0), (5, 5.0)],
)
def test_the_gh_token_lookup_uses_a_short_timeout_never_above_the_git_budget(
    git_timeout: int, expected: float
) -> None:
    runner = FakeGitRunner()
    runner.script(["gh", "auth", "token"], stdout=f"{TOKEN}\n")
    raw = {
        "repository": "acme/platform",
        "provider_name": "wiki",
        "auth": {"mode": "gh", "account": "sgg10"},
        "git_timeout_seconds": git_timeout,
    }
    strategy = build_strategy(
        parse_settings(raw), runner=runner, environ={}, which=lambda name: f"/usr/bin/{name}"
    )

    strategy.transport()

    assert [call.timeout for call in runner.calls] == [expected]


# -- labels ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("auth", "label"),
    [
        ({"mode": "env", "variable": "GH_WIKI_TOKEN"}, "env:GH_WIKI_TOKEN"),
        ({"mode": "gh", "account": "sgg10"}, "gh:sgg10"),
        ({"mode": "ssh"}, "ssh"),
        ({"mode": "ssh", "key_path": "~/.ssh/wiki_ed25519"}, "ssh:~/.ssh/wiki_ed25519"),
        ({"mode": "ambient"}, "ambient"),
        (None, "ambient"),
    ],
)
def test_label_names_the_credential_source_never_its_value(auth: dict | None, label: str) -> None:
    strategy = build_strategy(
        settings_for(auth),
        runner=FakeGitRunner(),
        environ={"GH_WIKI_TOKEN": TOKEN},
        which=lambda name: f"/usr/bin/{name}",
    )

    assert strategy.label == label
    assert TOKEN not in strategy.label


# -- offline checks: env (GW-P6, GW-A2) ---------------------------------------------


def env_strategy(environ: dict[str, str], runner: FakeGitRunner | None = None):
    return build_strategy(
        settings_for({"mode": "env", "variable": "GH_WIKI_TOKEN"}),
        runner=runner or FakeGitRunner(),
        environ=environ,
        which=lambda name: f"/usr/bin/{name}",
    )


def test_env_check_passes_when_the_variable_has_a_value() -> None:
    env_strategy({"GH_WIKI_TOKEN": TOKEN}).check_offline()


@pytest.mark.parametrize("environ", [{}, {"GH_WIKI_TOKEN": ""}, {"GH_WIKI_TOKEN": " \t\n"}])
def test_env_check_fails_naming_the_variable(environ: dict[str, str]) -> None:
    with pytest.raises(GithubWikiError) as caught:
        env_strategy(environ).check_offline()

    assert caught.value.code == "auth.env_missing"
    assert caught.value.context == {"variable": "GH_WIKI_TOKEN"}
    assert "GH_WIKI_TOKEN" in str(caught.value)


def test_env_check_never_prints_the_value_of_other_credential_variables() -> None:
    environ = {"GITHUB_TOKEN": TOKEN, "GH_TOKEN": TOKEN}
    with pytest.raises(GithubWikiError) as caught:
        env_strategy(environ).check_offline()

    assert caught.value.code == "auth.env_missing"
    assert TOKEN not in str(caught.value)


def test_env_check_consults_no_other_credential_source_and_never_runs_gh() -> None:
    runner = FakeGitRunner()
    environ = {"GITHUB_TOKEN": TOKEN, "GH_TOKEN": TOKEN}

    with pytest.raises(GithubWikiError):
        env_strategy(environ, runner).check_offline()
    with pytest.raises(GithubWikiError):
        env_strategy(environ, runner).transport()

    assert runner.calls == []


# -- offline checks: gh --------------------------------------------------------------


def gh_strategy(which, runner: FakeGitRunner | None = None):
    return build_strategy(
        settings_for({"mode": "gh", "account": "sgg10"}),
        runner=runner or FakeGitRunner(),
        environ={},
        which=which,
    )


def test_gh_check_passes_when_gh_is_on_path_without_running_it() -> None:
    runner = FakeGitRunner()

    gh_strategy(lambda name: "/usr/bin/gh" if name == "gh" else None, runner).check_offline()

    assert runner.calls == []


def test_gh_check_fails_when_gh_is_not_installed() -> None:
    with pytest.raises(GithubWikiError) as caught:
        gh_strategy(lambda name: None).check_offline()

    assert caught.value.code == "auth.gh_unavailable"


# -- offline checks: ssh -------------------------------------------------------------


def ssh_strategy(key_path: str | None):
    auth: dict = {"mode": "ssh"}
    if key_path is not None:
        auth["key_path"] = key_path
    return strategy_for(auth)


def test_ssh_check_passes_without_a_key_path() -> None:
    ssh_strategy(None).check_offline()


def test_ssh_check_passes_for_an_existing_key_file_without_reading_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = tmp_path / "wiki_ed25519"
    key.write_text("PRIVATE KEY MATERIAL")
    strategy = ssh_strategy(str(key))

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("the key file must never be opened")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)

    strategy.check_offline()


def test_ssh_check_expands_the_home_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "key").write_text("x")
    monkeypatch.setenv("HOME", str(tmp_path))

    ssh_strategy("~/key").check_offline()


@pytest.mark.parametrize("kind", ["missing", "directory"])
def test_ssh_check_fails_when_the_key_is_not_a_file(tmp_path: Path, kind: str) -> None:
    key = tmp_path / "wiki_key"
    if kind == "directory":
        key.mkdir()

    with pytest.raises(GithubWikiError) as caught:
        ssh_strategy(str(key)).check_offline()

    assert caught.value.code == "config.key_path_missing"
    assert caught.value.context["key_path"] == str(key)


# -- ambient -------------------------------------------------------------------------


def test_ambient_check_always_passes() -> None:
    strategy_for({"mode": "ambient"}).check_offline()
