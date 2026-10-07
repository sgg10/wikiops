"""Unit tests for the per-command transport of the ``github_wiki`` credential strategies.

Covers GW-A2..A6 and GW-A9: token injection through ``GIT_CONFIG_COUNT/KEY/VALUE``
(HTTPS modes), per-command ``gh`` token retrieval, ssh command pinning and
quoting, mode/transport coherence and isolation between interleaved profiles.
"""

from __future__ import annotations

import base64
import shlex
from collections.abc import Mapping

import pytest

from tests.support.fake_git_runner import FakeGitRunner
from wikiops.providers.github_wiki.auth import AmbientStrategy, build_strategy
from wikiops.providers.github_wiki.errors import GithubWikiError
from wikiops.providers.github_wiki.ports import CommandResult, GitTransport
from wikiops.providers.github_wiki.settings import parse_settings

TOKEN = "ghp_tok123SECRET"
OTHER_TOKEN = "ghp_other456SECRET"
BASIC = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode("ascii")


def which(name: str) -> str:
    return f"/usr/bin/{name}"


def make(auth: dict | None, *, environ: Mapping[str, str] | None = None, runner=None, host=None):
    raw: dict = {"repository": "acme/platform", "provider_name": "wiki"}
    if auth is not None:
        raw["auth"] = auth
    if host is not None:
        raw["host"] = host
    return build_strategy(
        parse_settings(raw),
        runner=runner or FakeGitRunner(),
        environ={} if environ is None else environ,
        which=which,
    )


def gh_runner(token: str = TOKEN) -> FakeGitRunner:
    runner = FakeGitRunner()
    runner.script(["gh", "auth", "token"], stdout=f"{token}\n")
    return runner


def config_entries(overrides: Mapping[str, str | None]) -> list[tuple[str, str]]:
    count = int(overrides["GIT_CONFIG_COUNT"] or 0)
    return [
        (overrides[f"GIT_CONFIG_KEY_{index}"], overrides[f"GIT_CONFIG_VALUE_{index}"])
        for index in range(count)
    ]


def serialized(transport: GitTransport) -> str:
    return repr((transport.remote_url, dict(transport.env_overrides), transport.label))


# -- HTTPS token injection: env and gh ------------------------------------------------

def build_env(*, host: str | None = None, inherited: Mapping[str, str] | None = None):
    environ = {"GH_WIKI_TOKEN": TOKEN, **(inherited or {})}
    return make({"mode": "env", "variable": "GH_WIKI_TOKEN"}, environ=environ, host=host)


def build_gh(*, host: str | None = None, inherited: Mapping[str, str] | None = None):
    return make(
        {"mode": "gh", "account": "sgg10"},
        environ=dict(inherited or {}),
        runner=gh_runner(),
        host=host,
    )


HTTPS_STRATEGIES = [pytest.param(build_env, id="env"), pytest.param(build_gh, id="gh")]


@pytest.mark.parametrize("build", HTTPS_STRATEGIES)
def test_https_transport_injects_the_token_as_a_host_scoped_extra_header(build) -> None:
    transport = build().transport()

    assert config_entries(transport.env_overrides) == [
        ("http.https://github.com/.extraheader", f"AUTHORIZATION: basic {BASIC}"),
        ("credential.helper", ""),
    ]
    assert transport.env_overrides["GIT_CONFIG_COUNT"] == "2"


@pytest.mark.parametrize("build", HTTPS_STRATEGIES)
def test_https_transport_scopes_the_header_to_the_configured_host(build) -> None:
    transport = build(host="ghe.acme.io").transport()

    keys = [key for key, _ in config_entries(transport.env_overrides)]
    assert keys == ["http.https://ghe.acme.io/.extraheader", "credential.helper"]
    assert transport.remote_url == "https://ghe.acme.io/acme/platform.wiki.git"


@pytest.mark.parametrize("build", HTTPS_STRATEGIES)
def test_https_transport_declares_the_token_and_derived_forms_as_secrets(build) -> None:
    transport = build().transport()

    assert set(transport.secrets) == {TOKEN, f"x-access-token:{TOKEN}", BASIC}


@pytest.mark.parametrize("build", HTTPS_STRATEGIES)
def test_the_token_never_appears_in_remote_label_or_argv_material(build) -> None:
    transport = build().transport()

    assert TOKEN not in transport.remote_url
    assert TOKEN not in transport.label
    assert TOKEN not in "".join(
        value or "" for key, value in transport.env_overrides.items() if "KEY" in key
    )


@pytest.mark.parametrize("build", HTTPS_STRATEGIES)
def test_https_transport_appends_after_the_users_existing_config_entries(build) -> None:
    overrides = build(
        inherited={
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "user.one",
            "GIT_CONFIG_VALUE_0": "1",
            "GIT_CONFIG_KEY_1": "user.two",
            "GIT_CONFIG_VALUE_1": "2",
        }
    ).transport().env_overrides

    assert overrides["GIT_CONFIG_COUNT"] == "4"
    assert overrides["GIT_CONFIG_KEY_2"] == "http.https://github.com/.extraheader"
    assert overrides["GIT_CONFIG_VALUE_2"] == f"AUTHORIZATION: basic {BASIC}"
    assert overrides["GIT_CONFIG_KEY_3"] == "credential.helper"
    assert overrides["GIT_CONFIG_VALUE_3"] == ""
    assert not {"GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0", "GIT_CONFIG_KEY_1"} & set(overrides)


@pytest.mark.parametrize("count", ["", "abc", "-1", "1.5"])
def test_an_unusable_inherited_config_count_is_treated_as_zero(count: str) -> None:
    strategy = make(
        {"mode": "env", "variable": "T"}, environ={"T": TOKEN, "GIT_CONFIG_COUNT": count}
    )

    assert strategy.transport().env_overrides["GIT_CONFIG_COUNT"] == "2"


@pytest.mark.parametrize("build", HTTPS_STRATEGIES)
def test_https_transport_drops_askpass_and_tracing_variables(build) -> None:
    overrides = build(
        inherited={
            "GIT_TRACE": "1",
            "GIT_TRACE_CURL": "1",
            "GIT_TRACE_PACKET": "1",
            "GIT_TRACE_FUTURE_KIND": "1",
            "GIT_TRACE2_EVENT": "/tmp/x",
            "GIT_ASKPASS": "/bin/askpass",
            "SSH_ASKPASS": "/bin/askpass",
            "GIT_CURL_VERBOSE": "1",
        }
    ).transport().env_overrides

    for name in (
        "GIT_ASKPASS",
        "SSH_ASKPASS",
        "GIT_CURL_VERBOSE",
        "GIT_TRACE",
        "GIT_TRACE_CURL",
        "GIT_TRACE_PACKET",
        "GIT_TRACE_FUTURE_KIND",
        "GIT_TRACE2_EVENT",
    ):
        assert name in overrides, name
        assert overrides[name] is None, name


def test_https_transport_drops_trace_variables_even_when_not_inherited() -> None:
    overrides = make({"mode": "env", "variable": "T"}, environ={"T": TOKEN}).transport().env_overrides

    assert overrides["GIT_TRACE_CURL"] is None
    assert overrides["GIT_TRACE"] is None


# -- env mode: variable re-read per command ---------------------------------------------


def test_env_token_is_read_again_for_every_command() -> None:
    environ = {"T": TOKEN}
    strategy = make({"mode": "env", "variable": "T"}, environ=environ)

    first = strategy.transport()
    environ["T"] = OTHER_TOKEN
    second = strategy.transport()

    assert TOKEN in first.secrets and OTHER_TOKEN not in first.secrets
    assert OTHER_TOKEN in second.secrets and TOKEN not in second.secrets


def test_env_token_is_trimmed_of_surrounding_whitespace() -> None:
    transport = make({"mode": "env", "variable": "T"}, environ={"T": f"  {TOKEN}\n"}).transport()

    assert transport.secrets[0] == TOKEN
    assert dict(config_entries(transport.env_overrides))["http.https://github.com/.extraheader"] == (
        f"AUTHORIZATION: basic {BASIC}"
    )


def test_env_transport_fails_when_the_variable_disappeared() -> None:
    environ = {"T": TOKEN}
    strategy = make({"mode": "env", "variable": "T"}, environ=environ)
    del environ["T"]

    with pytest.raises(GithubWikiError) as caught:
        strategy.transport()

    assert caught.value.code == "auth.env_missing"


# -- gh mode: token fetched per command -------------------------------------------------


def test_gh_token_is_requested_for_the_explicit_account_and_host() -> None:
    runner = gh_runner()

    make({"mode": "gh", "account": "sgg10"}, runner=runner, host="ghe.acme.io").transport()

    assert runner.argvs == [
        ("gh", "auth", "token", "--user", "sgg10", "--hostname", "ghe.acme.io")
    ]
    call = runner.calls[0]
    assert call.env_overrides == {"GH_PROMPT_DISABLED": "1"}
    assert call.cwd is None
    assert call.stdin is None


def test_gh_runs_once_per_transport_and_the_token_is_never_cached() -> None:
    runner = FakeGitRunner()
    tokens = iter([TOKEN, OTHER_TOKEN])
    runner.script_with(
        ["gh", "auth", "token"],
        lambda call: CommandResult(call.argv, 0, f"{next(tokens)}\n", ""),
    )
    strategy = make({"mode": "gh", "account": "sgg10"}, runner=runner)

    first = strategy.transport()
    second = strategy.transport()

    assert len(runner.calls_matching(["gh", "auth", "token"])) == 2
    assert TOKEN in first.secrets and OTHER_TOKEN not in first.secrets
    assert OTHER_TOKEN in second.secrets and TOKEN not in second.secrets


@pytest.mark.parametrize(
    "script",
    [
        {"returncode": 1, "stderr": "no oauth token found for acct"},
        {"returncode": 0, "stdout": "\n"},
        {"returncode": 0, "stdout": "not logged in to github.com\n"},
        {"returncode": 127, "stderr": "gh: No such file or directory"},
        {"returncode": 0, "timed_out": True},
    ],
    ids=["nonzero", "empty", "prose-output", "missing-binary", "timeout"],
)
def test_gh_failure_is_auth_gh_failed_naming_account_and_host(script: dict) -> None:
    runner = FakeGitRunner()
    runner.script(["gh", "auth", "token"], **script)
    strategy = make({"mode": "gh", "account": "ghost"}, runner=runner, host="ghe.acme.io")

    with pytest.raises(GithubWikiError) as caught:
        strategy.transport()

    assert caught.value.code == "auth.gh_failed"
    assert caught.value.context["account"] == "ghost"
    assert caught.value.context["host"] == "ghe.acme.io"
    assert len(runner.calls) == 1  # no fallback to another credential source


def test_gh_failure_redacts_a_token_echoed_in_gh_output() -> None:
    runner = FakeGitRunner()
    runner.script(
        ["gh", "auth", "token"],
        returncode=1,
        stdout=f"{TOKEN}\n",
        stderr=f"failed for token {TOKEN} and x-access-token:{TOKEN}\nAuthorization: basic {BASIC}",
    )
    strategy = make({"mode": "gh", "account": "ghost"}, runner=runner)

    with pytest.raises(GithubWikiError) as caught:
        strategy.transport()

    rendered = str(caught.value)
    assert TOKEN not in rendered and BASIC not in rendered
    assert TOKEN not in repr(caught.value.context)
    assert "***" in rendered


# -- ssh mode ------------------------------------------------------------------------


def ssh_command(transport: GitTransport) -> list[str]:
    return shlex.split(str(transport.env_overrides["GIT_SSH_COMMAND"]))


def test_unpinned_ssh_uses_batch_mode_and_a_connect_timeout_only() -> None:
    transport = make({"mode": "ssh"}).transport()

    command = ssh_command(transport)
    assert command[0] == "ssh"
    assert "BatchMode=yes" in command
    assert any(part.startswith("ConnectTimeout=") for part in command)
    assert "-i" not in command
    assert "IdentitiesOnly=yes" not in command
    assert transport.env_overrides["GIT_SSH_VARIANT"] == "ssh"
    assert transport.secrets == ()
    assert transport.remote_url == "git@github.com:acme/platform.wiki.git"


def test_pinned_ssh_offers_only_the_configured_key() -> None:
    transport = make({"mode": "ssh", "key_path": "/k/wiki"}).transport()

    command = ssh_command(transport)
    assert command[command.index("-i") + 1] == "/k/wiki"
    assert "IdentitiesOnly=yes" in command
    assert "BatchMode=yes" in command


@pytest.mark.parametrize(
    "key_path",
    ["/k/my key", "/k/a;touch pwned", "/k/$(id)", "/k/it's", "/k/a&b|c", "/k/`id`", "/k/a\"b"],
)
def test_ssh_key_path_is_shell_quoted_so_it_stays_one_argument(key_path: str) -> None:
    transport = make({"mode": "ssh", "key_path": key_path}).transport()

    command = ssh_command(transport)
    assert command[command.index("-i") + 1] == key_path
    assert str(transport.env_overrides["GIT_SSH_COMMAND"]).count(shlex.quote(key_path)) == 1


def test_ssh_key_path_is_expanded_and_made_absolute(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    home_key = ssh_command(make({"mode": "ssh", "key_path": "~/keys/wiki"}).transport())
    relative_key = ssh_command(make({"mode": "ssh", "key_path": "keys/wiki"}).transport())

    assert home_key[home_key.index("-i") + 1] == str(tmp_path / "keys" / "wiki")
    assert relative_key[relative_key.index("-i") + 1] == str(tmp_path / "keys" / "wiki")


@pytest.mark.parametrize("key_path", [None, "/k/wiki"])
def test_ssh_never_weakens_host_key_checking(key_path: str | None) -> None:
    auth: dict = {"mode": "ssh"}
    if key_path:
        auth["key_path"] = key_path

    command = " ".join(ssh_command(make(auth).transport())).lower()

    assert "stricthostkeychecking" not in command
    assert "userknownhostsfile" not in command
    assert "/dev/null" not in command


def test_ssh_never_carries_a_token_even_when_one_is_in_the_environment() -> None:
    environ = {"GITHUB_TOKEN": TOKEN, "GH_TOKEN": TOKEN, "T": TOKEN}
    transport = make({"mode": "ssh"}, environ=environ).transport()

    assert "GIT_CONFIG_COUNT" not in transport.env_overrides
    assert not any("GIT_CONFIG_KEY" in name for name in transport.env_overrides)
    assert TOKEN not in serialized(transport)
    assert "extraheader" not in serialized(transport)


# -- ambient mode -------------------------------------------------------------------


def test_ambient_adds_nothing_but_a_non_interactive_credential_manager() -> None:
    transport = make({"mode": "ambient"}, environ={"GITHUB_TOKEN": TOKEN}).transport()

    assert dict(transport.env_overrides) == {"GCM_INTERACTIVE": "never"}
    assert transport.secrets == ()
    assert transport.label == "ambient"
    assert transport.remote_url == "https://github.com/acme/platform.wiki.git"


def test_ambient_is_the_default_when_auth_is_omitted() -> None:
    assert isinstance(make(None), AmbientStrategy)


# -- isolation between interleaved profiles (GW-A9) -----------------------------------


def test_interleaved_env_and_ssh_profiles_never_cross() -> None:
    env_profile = make({"mode": "env", "variable": "A_T"}, environ={"A_T": TOKEN})
    ssh_profile = make({"mode": "ssh", "key_path": "/k/b"})

    calls = [env_profile.transport(), ssh_profile.transport(), env_profile.transport(), ssh_profile.transport()]

    for transport in calls[0::2]:
        assert "GIT_SSH_COMMAND" not in transport.env_overrides
        assert TOKEN in transport.secrets
    for transport in calls[1::2]:
        assert "GIT_CONFIG_COUNT" not in transport.env_overrides
        assert TOKEN not in serialized(transport)


def test_two_gh_profiles_on_one_host_each_get_their_own_account_token() -> None:
    runner = FakeGitRunner()
    runner.script(["gh", "auth", "token", "--user", "acct-a"], stdout=f"{TOKEN}\n")
    runner.script(["gh", "auth", "token", "--user", "acct-b"], stdout=f"{OTHER_TOKEN}\n")
    profile_a = make({"mode": "gh", "account": "acct-a"}, runner=runner)
    profile_b = make({"mode": "gh", "account": "acct-b"}, runner=runner)

    a_first, b_first, a_second = profile_a.transport(), profile_b.transport(), profile_a.transport()

    assert OTHER_TOKEN not in serialized(a_first) + serialized(a_second)
    assert TOKEN not in serialized(b_first)
    assert b_first.label == "gh:acct-b"


def test_strategies_hold_no_module_level_token_state() -> None:
    from wikiops.providers.github_wiki import auth

    first = make({"mode": "env", "variable": "T"}, environ={"T": TOKEN})
    second = make({"mode": "env", "variable": "T"}, environ={"T": OTHER_TOKEN})
    first.transport()
    second.transport()

    assert TOKEN in first.transport().secrets
    assert not any(isinstance(value, str) and TOKEN in value for value in vars(auth).values())
