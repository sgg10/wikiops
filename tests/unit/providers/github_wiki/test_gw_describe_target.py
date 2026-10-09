"""``GithubWikiProvider.describe_target``: the plan note (GW-P11, gap G2).

The note must name the credential-free remote, the absolute workdir, the branch,
the auth label and the commit/push/sync/backend flags, show pending and unpushed
counts when a clone exists, carry the ``sync.stale_plan`` warning for offline
plans, and never run a network command, issue a credential or leak a secret.
"""

from __future__ import annotations

import base64
import os
import re
import shlex
from pathlib import Path

import pytest
from wikiops_sdk.domain import DocumentRef, RefKind

from tests.support.provider_harness import TOKEN, ProviderHarness, build_provider
from wikiops.providers.github_wiki.errors import GithubWikiError
from wikiops.providers.github_wiki.manifest import PendingManifest
from wikiops.providers.github_wiki.workdir import manifest_path

DEFAULT_NOTE = (
    "remote='https://github.com/acme/platform.wiki.git' workdir='{workdir}' branch=auto "
    "auth=ambient auto_commit=true auto_push=false sync_on_plan=true backend=local_files sidebar=false"
)
READ_ONLY = {"status", "rev-parse", "config", "symbolic-ref", "rev-list"}


def page(path: str) -> DocumentRef:
    return DocumentRef(provider="", kind=RefKind.PATH, locator={"path": path})


def record_pending(harness: ProviderHarness, *paths: str) -> None:
    for path in paths:
        (harness.workdir / path).write_text(f"# {path}")
    PendingManifest(manifest_path(harness.fake.git_dir), workdir=harness.workdir).record(list(paths))


def fields_of(note: str) -> dict[str, str]:
    """``key=value`` pairs of the note; single-quoted values are decoded like a POSIX shell would."""
    tokens = shlex.split(note.split(" [github_wiki:")[0])
    return dict(token.split("=", 1) for token in tokens)


# -- the default note ------------------------------------------------------------------------


def test_the_default_note_is_exactly_the_documented_fields_in_order(tmp_path: Path) -> None:
    harness = build_provider(tmp_path)

    assert harness.provider.describe_target() == DEFAULT_NOTE.format(workdir=harness.workdir)


def test_the_workdir_in_the_note_is_absolute(tmp_path: Path) -> None:
    harness = build_provider(tmp_path, settings={"workdir": "relative-wiki"})  # resolved against the cwd

    workdir = fields_of(harness.provider.describe_target())["workdir"]

    assert os.path.isabs(workdir)
    assert workdir == str(harness.workdir)


def test_the_default_workdir_is_the_per_profile_cache_path_and_is_shown(tmp_path: Path) -> None:
    cache = tmp_path / "cache-base"
    harness = build_provider(tmp_path, settings={"workdir": None}, cache_dir=cache)

    workdir = fields_of(harness.provider.describe_target())["workdir"]

    assert workdir == str(harness.workdir)
    assert workdir.startswith(str(cache.resolve()))
    assert Path(workdir).parts[-4:] == ("github.com", "acme", "platform", "p-docs")


def test_the_sidebar_is_off_by_default_without_an_action_and_there_is_no_stale_warning(tmp_path: Path) -> None:
    note = build_provider(tmp_path).provider.describe_target()
    fields = fields_of(note)

    assert fields["sidebar"] == "false"
    assert "sidebar_action" not in fields
    assert "stale_plan" not in note
    assert "pending_paths" not in note


@pytest.mark.parametrize(
    "name",
    ["it's a wiki", "'", "''", "a'b'c", "it\\'s", "quote\"s and $HOME `x`", "with space"],
)
def test_a_workdir_with_quotes_is_unambiguous_and_decodes_to_the_exact_path(
    tmp_path: Path, name: str
) -> None:
    harness = build_provider(tmp_path, workdir=tmp_path / name)

    note = harness.provider.describe_target()
    fields = fields_of(note)

    assert fields["workdir"] == str(harness.workdir)
    assert fields["remote"] == "https://github.com/acme/platform.wiki.git"
    assert fields["backend"] == "local_files"  # the fields after the path are not swallowed by it
    assert fields["sidebar"] == "false"
    assert list(fields) == [
        "remote", "workdir", "branch", "auth", "auto_commit", "auto_push", "sync_on_plan", "backend", "sidebar",
    ]


def test_an_apostrophe_is_closed_escaped_and_reopened_in_the_note(tmp_path: Path) -> None:
    harness = build_provider(tmp_path, workdir=tmp_path / "it's")

    note = harness.provider.describe_target()

    quoted = "'" + str(harness.workdir).replace("'", "'\\''") + "'"
    assert quoted.endswith("it'\\''s'")
    assert f"workdir={quoted} branch=auto" in note


def test_control_characters_in_the_workdir_cannot_break_the_note_line(tmp_path: Path) -> None:
    harness = build_provider(tmp_path, workdir=tmp_path / "a\nb\x1b[31m")

    note = harness.provider.describe_target()

    assert "\n" not in note
    assert "\x1b" not in note
    assert "a\\nb\\x1b[31m" in note


def test_the_stale_warning_still_follows_a_workdir_with_an_apostrophe(tmp_path: Path) -> None:
    harness = build_provider(tmp_path, workdir=tmp_path / "it's", settings={"sync_on_plan": False})

    note = harness.provider.describe_target()

    assert fields_of(note)["workdir"] == str(harness.workdir)
    assert note.count("[github_wiki:sync.stale_plan]") == 1


# -- flags, auth labels, remotes ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("settings", "environ", "expected"),
    [
        ({"auth": {"mode": "gh", "account": "sgg10"}}, {}, "gh:sgg10"),
        ({"auth": {"mode": "env", "variable": "WIKI_T"}}, {"WIKI_T": TOKEN}, "env:WIKI_T"),
        ({"auth": {"mode": "ssh"}}, {}, "ssh"),
        ({"auth": {"mode": "ssh", "key_path": "~/.ssh/wiki_ed25519"}}, {}, "ssh:~/.ssh/wiki_ed25519"),
        ({"auth": {"mode": "ambient"}}, {}, "ambient"),
        ({}, {}, "ambient"),
    ],
)
def test_the_auth_label_names_the_mode(
    tmp_path: Path, settings: dict[str, object], environ: dict[str, str], expected: str
) -> None:
    harness = build_provider(tmp_path, settings=settings, environ=environ)

    assert fields_of(harness.provider.describe_target())["auth"] == expected


def test_ssh_describes_the_ssh_remote_and_ghe_the_enterprise_host(tmp_path: Path) -> None:
    ssh = build_provider(tmp_path / "a", settings={"auth": {"mode": "ssh"}})
    ghe = build_provider(tmp_path / "b", settings={"host": "ghe.acme.io"})

    assert fields_of(ssh.provider.describe_target())["remote"] == "git@github.com:acme/platform.wiki.git"
    assert fields_of(ghe.provider.describe_target())["remote"] == "https://ghe.acme.io/acme/platform.wiki.git"


def test_the_flags_and_backend_follow_the_settings(tmp_path: Path) -> None:
    harness = build_provider(
        tmp_path,
        settings={
            "allow_auto_commit": False,
            "sync_on_plan": False,
            "local_backend": {"type": "fake_files"},
        },
    )

    fields = fields_of(harness.provider.describe_target())

    assert fields["auto_commit"] == "false"
    assert fields["auto_push"] == "false"
    assert fields["sync_on_plan"] == "false"
    assert fields["backend"] == "fake_files"


def test_auto_push_is_shown_when_enabled(tmp_path: Path) -> None:
    harness = build_provider(tmp_path, settings={"allow_auto_push": True})

    fields = fields_of(harness.provider.describe_target())

    assert (fields["auto_commit"], fields["auto_push"]) == ("true", "true")


# -- the sidebar keys (SB14, GWP-D3) -------------------------------------------------------------------

MANAGED = "<!-- wikiops:managed sidebar -->\n\n- [Home](Home)\n"


def test_an_enabled_sidebar_without_a_clone_says_create_and_runs_no_command(tmp_path: Path) -> None:
    harness = build_provider(tmp_path, settings={"generate_sidebar": True})

    fields = fields_of(harness.provider.describe_target())

    assert (fields["sidebar"], fields["sidebar_action"]) == ("true", "create")
    assert harness.runner.calls == []
    assert list(fields)[-2:] == ["sidebar", "sidebar_action"]


def test_an_enabled_sidebar_in_a_clone_without_the_file_says_create(tmp_path: Path) -> None:
    harness = build_provider(tmp_path, cloned=True, settings={"generate_sidebar": True})

    assert fields_of(harness.provider.describe_target())["sidebar_action"] == "create"


@pytest.mark.parametrize(
    ("content", "action"),
    [
        (MANAGED.encode(), "regenerate"),
        (MANAGED.replace("\n", "\r\n").encode(), "regenerate"),
        (b"# my own sidebar\n", "skipped-unmanaged"),
        (b" " + MANAGED.encode(), "skipped-unmanaged"),
        (b"\xff\xfe not utf-8\n", "skipped-unmanaged"),
    ],
    ids=["managed", "managed-crlf", "unmarked", "near-miss-marker", "undecodable"],
)
def test_an_enabled_sidebar_action_follows_the_file_in_the_clone(tmp_path: Path, content: bytes, action: str) -> None:
    harness = build_provider(tmp_path, cloned=True, settings={"generate_sidebar": True})
    (harness.workdir / "_Sidebar.md").write_bytes(content)

    assert fields_of(harness.provider.describe_target())["sidebar_action"] == action


def test_a_directory_in_place_of_the_sidebar_is_skipped_in_the_plan(tmp_path: Path) -> None:
    harness = build_provider(tmp_path, cloned=True, settings={"generate_sidebar": True})
    (harness.workdir / "_Sidebar.md").mkdir()

    assert fields_of(harness.provider.describe_target())["sidebar_action"] == "skipped-unmanaged"


def test_the_sidebar_plan_reads_only_the_clone_and_runs_no_extra_command(tmp_path: Path) -> None:
    harness = build_provider(tmp_path, cloned=True, settings={"generate_sidebar": True})
    (harness.workdir / "_Sidebar.md").write_text(MANAGED)
    off = build_provider(tmp_path / "off", cloned=True)
    off.provider.describe_target()

    harness.provider.describe_target()

    assert harness.git_subcommands() == off.git_subcommands()
    assert harness.network_calls() == [] and harness.resolver.created == []


def test_a_disabled_sidebar_never_looks_at_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    harness = build_provider(tmp_path, cloned=True)
    (harness.workdir / "_Sidebar.md").write_text(MANAGED)

    def forbidden(workdir: Path) -> str:
        raise AssertionError("plan_action must not run when the sidebar is off")

    monkeypatch.setattr("wikiops.providers.github_wiki.sidebar_io.plan_action", forbidden)

    fields = fields_of(harness.provider.describe_target())

    assert fields["sidebar"] == "false" and "sidebar_action" not in fields


def test_the_sidebar_keys_keep_the_existing_keys_and_their_order(tmp_path: Path) -> None:
    harness = build_provider(tmp_path, cloned=True, settings={"generate_sidebar": True})

    assert list(fields_of(harness.provider.describe_target())) == [
        "remote", "workdir", "branch", "auth", "auto_commit", "auto_push", "sync_on_plan", "backend",
        "sidebar", "sidebar_action", "pending_paths", "unpushed_commits",
    ]


def test_the_sidebar_note_carries_no_hint_validation_secret_or_environment_value(tmp_path: Path) -> None:
    harness = build_provider(
        tmp_path,
        cloned=True,
        settings={"generate_sidebar": True, "auth": {"mode": "env", "variable": "WIKI_T"}},
        environ={"WIKI_T": TOKEN},
    )

    note = harness.provider.describe_target()

    assert TOKEN not in note and "sidebar.invalid_hint" not in note


# -- no secrets, no network, nothing run before the clone exists ------------------------------------


def test_the_env_note_names_the_variable_and_never_its_value(tmp_path: Path) -> None:
    harness = build_provider(
        tmp_path, settings={"auth": {"mode": "env", "variable": "WIKI_T"}}, environ={"WIKI_T": TOKEN}
    )

    note = harness.provider.describe_target()

    encoded = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
    assert "env:WIKI_T" in note
    for secret in (TOKEN, encoded, "x-access-token", "Authorization", "AUTHORIZATION"):
        assert secret not in note


def test_describing_a_gh_profile_never_runs_gh_or_issues_a_transport(tmp_path: Path) -> None:
    harness = build_provider(
        tmp_path, settings={"auth": {"mode": "gh", "account": "sgg10"}}, gh_token=TOKEN
    )

    note = harness.provider.describe_target()

    assert TOKEN not in note
    assert harness.runner.calls == []


def test_before_a_clone_exists_nothing_runs_and_nothing_is_created(tmp_path: Path) -> None:
    harness = build_provider(tmp_path)

    harness.provider.describe_target()

    assert harness.runner.calls == []
    assert harness.resolver.created == []
    assert list(tmp_path.iterdir()) == []


def test_an_existing_directory_without_a_clone_is_not_inspected(tmp_path: Path) -> None:
    harness = build_provider(tmp_path)
    harness.workdir.mkdir(parents=True)
    (harness.workdir / "notes.txt").write_text("not a clone")

    note = harness.provider.describe_target()

    assert "pending_paths" not in note
    assert harness.runner.calls == []


# -- pending and unpushed visibility ---------------------------------------------------------------


def test_a_clone_shows_the_branch_pending_paths_and_unpushed_commits(tmp_path: Path) -> None:
    harness = build_provider(tmp_path, cloned=True, local=["c1", "c2"], tracking=["c1"])
    record_pending(harness, "Home.md", "Other.md")

    fields = fields_of(harness.provider.describe_target())

    assert fields["branch"] == "master"
    assert fields["pending_paths"] == "2"
    assert fields["unpushed_commits"] == "1"


def test_the_detected_branch_follows_the_override_when_set(tmp_path: Path) -> None:
    harness = build_provider(
        tmp_path,
        cloned=True,
        settings={"branch": "wiki"},
        remote={"master": ["c1"], "wiki": ["c1"]},
        local_branch="wiki",
    )

    assert fields_of(harness.provider.describe_target())["branch"] == "wiki"


def test_a_clean_clone_reports_zero_counts(tmp_path: Path) -> None:
    harness = build_provider(tmp_path, cloned=True)

    fields = fields_of(harness.provider.describe_target())

    assert (fields["pending_paths"], fields["unpushed_commits"]) == ("0", "0")


def test_describing_a_clone_uses_local_read_only_git_and_no_network(tmp_path: Path) -> None:
    harness = build_provider(tmp_path, cloned=True)

    harness.provider.describe_target()

    assert set(harness.git_subcommands()) <= READ_ONLY
    assert harness.network_calls() == []
    assert harness.resolver.created == []  # the inner backend is not touched


def test_after_a_sync_the_note_comes_from_the_cached_state_without_new_commands(tmp_path: Path) -> None:
    harness = build_provider(tmp_path, cloned=True, local=["c1", "c2"], tracking=["c1"])
    harness.provider.resolve_ref(page("Home.md"))
    seen = len(harness.runner.calls)

    fields = fields_of(harness.provider.describe_target())

    assert fields["unpushed_commits"] == "1"
    assert len(harness.runner.calls) == seen


def test_a_clone_of_another_wiki_is_reported_not_described_silently(tmp_path: Path) -> None:
    harness = build_provider(tmp_path, cloned=True, origin_url="https://github.com/acme/other.wiki.git")

    with pytest.raises(GithubWikiError) as error:  # the orchestrator turns this into a plan warning
        harness.provider.describe_target()

    assert error.value.code == "sync.remote_mismatch"


# -- offline plans carry the stale warning -------------------------------------------------------------


def test_sync_on_plan_false_appends_the_stale_plan_warning_in_the_standard_shape(tmp_path: Path) -> None:
    harness = build_provider(tmp_path, cloned=True, settings={"sync_on_plan": False})

    note = harness.provider.describe_target()

    warning = note[note.index("[github_wiki:sync.stale_plan]") :]
    assert re.fullmatch(r"\[github_wiki:sync\.stale_plan\] .+ Hint: .+\.", warning)
    assert "may be stale" in warning
    assert "sync_on_plan: true" in warning
    assert "last_sync" not in warning  # no fetch has happened in this clone
    assert note.startswith("remote='https://github.com/acme/platform.wiki.git'")


def test_the_stale_warning_names_the_last_fetch_when_it_is_known(tmp_path: Path) -> None:
    harness = build_provider(tmp_path, cloned=True, settings={"sync_on_plan": False})
    fetch_head = harness.workdir / ".git" / "FETCH_HEAD"
    fetch_head.write_text("")
    os.utime(fetch_head, (1_700_000_000, 1_700_000_000))

    note = harness.provider.describe_target()

    assert "last_sync='2023-11-14T22:13:20+00:00'" in note


def test_the_stale_warning_is_there_even_without_a_clone(tmp_path: Path) -> None:
    harness = build_provider(tmp_path, settings={"sync_on_plan": False})

    note = harness.provider.describe_target()

    assert "[github_wiki:sync.stale_plan]" in note
    assert harness.runner.calls == []


def test_the_stale_warning_never_leaks_into_online_plans(tmp_path: Path) -> None:
    harness = build_provider(tmp_path, cloned=True, settings={"sync_on_plan": True})
    (harness.workdir / ".git" / "FETCH_HEAD").write_text("")

    assert "stale_plan" not in harness.provider.describe_target()
