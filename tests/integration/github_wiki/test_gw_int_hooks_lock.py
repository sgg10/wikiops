"""Real git: hooks never see a credential, and one clone is never changed by two runs at once (GW-A10, GW-S15).

Network commands run with hooks disabled, so a hook that would fail or dump its
environment must not run; local commit hooks keep running but get no credentials.
The workdir lock is a real OS advisory lock: a second handle, and a separate
process that is killed while holding it, are exercised for real.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from wikiops_sdk.domain import OperationStatus

import wikiops
from tests.support.real_git import FAKE_TOKEN, SEED_PAGE, Wiki, files_containing, install_hook, run_git
from tests.support.write_ops import change_set, create, ref
from wikiops.providers.github_wiki.errors import GithubWikiError

pytestmark = [pytest.mark.git_integration, pytest.mark.skipif(os.name != "posix", reason="shell hooks and flock")]


def statuses(result) -> list[OperationStatus]:  # noqa: ANN001
    return [item.status for item in result.results]


def messages(result) -> str:  # noqa: ANN001
    return " | ".join(item.message or "" for item in result.results)


# -- hooks on network commands -----------------------------------------------------------------


def test_a_pre_push_hook_never_runs_and_no_secret_reaches_the_workdir(make_wiki) -> None:  # noqa: ANN001
    wiki: Wiki = make_wiki(token=FAKE_TOKEN, allow_auto_push=True)
    wiki.provider().exists(ref(SEED_PAGE))
    marker = wiki.root / "pre-push-ran"
    install_hook(wiki.git_dir, "pre-push", f"env > '{marker}'\nexit 1")

    result = wiki.provider().apply_changes(change_set(create("Guide.md", "# Guide\n")))

    assert statuses(result) == [OperationStatus.APPLIED] and "pushed to master" in messages(result)
    assert not marker.exists()  # the failing hook was never started
    assert wiki.remote.rev() == wiki.head()
    assert wiki.strategy.issued >= 3 and wiki.strategy.secrets  # credentials were requested per command
    assert files_containing(wiki.workdir, wiki.strategy.secrets) == []  # config, manifest, lock, objects
    assert files_containing(wiki.root / "remotes", wiki.strategy.secrets) == []


def test_the_same_hook_does_run_when_git_is_used_directly(wiki: Wiki) -> None:
    wiki.provider().exists(ref(SEED_PAGE))
    marker = wiki.root / "pre-push-ran"
    install_hook(wiki.git_dir, "pre-push", f"echo ran > '{marker}'\nexit 1")
    wiki.git("commit", "-q", "--allow-empty", "-m", "local work")

    wiki.git("push", "-q", "origin", "HEAD:refs/heads/master", check=False)

    assert marker.exists() and wiki.remote.subjects() == ["Initial Home page"]  # control: hooks work here


def test_a_template_post_checkout_hook_does_not_run_during_the_clone(
    make_wiki, tmp_path: Path  # noqa: ANN001
) -> None:
    template = tmp_path / "template"
    marker = tmp_path / "post-checkout-ran"
    install_hook(template, "post-checkout", f"echo ran >> '{marker}'")
    config = Path(os.environ["GIT_CONFIG_GLOBAL"])
    config.write_text(config.read_text() + f"[init]\n\ttemplateDir = {template}\n")
    wiki: Wiki = make_wiki()

    wiki.provider().exists(ref(SEED_PAGE))

    assert (wiki.git_dir / "hooks" / "post-checkout").exists()  # the template did reach the clone
    assert not marker.exists()  # and its hook never ran
    run_git("clone", "-q", wiki.remote.url, str(tmp_path / "plain"))
    assert marker.exists()  # control: a plain clone does run it


def test_a_user_hooks_path_in_the_environment_is_ignored_for_the_push(
    make_wiki, tmp_path: Path, monkeypatch: pytest.MonkeyPatch  # noqa: ANN001
) -> None:
    wiki: Wiki = make_wiki(allow_auto_push=True)
    marker = tmp_path / "evil-ran"
    evil = install_hook(tmp_path / "evil", "pre-push", f"echo ran > '{marker}'\nexit 1").parent
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", f"'core.hooksPath'='{evil}'")

    result = wiki.provider().apply_changes(change_set(create("Guide.md", "# Guide\n")))

    assert statuses(result) == [OperationStatus.APPLIED] and "pushed to master" in messages(result)
    assert not marker.exists() and wiki.remote.rev() == wiki.head()
    wiki.git("commit", "-q", "--allow-empty", "-m", "local work")
    wiki.git("push", "-q", "origin", "HEAD:refs/heads/master", check=False)
    assert marker.exists()  # control: the user's setting does take effect for plain git


def test_a_failing_pre_commit_hook_sees_no_credentials(make_wiki) -> None:  # noqa: ANN001
    wiki: Wiki = make_wiki(token=FAKE_TOKEN, allow_auto_push=True)
    wiki.provider().exists(ref(SEED_PAGE))
    dump = wiki.root / "pre-commit-env"
    install_hook(wiki.git_dir, "pre-commit", f"env > '{dump}'\nexit 1")

    result = wiki.provider().apply_changes(change_set(create("Guide.md", "# Guide\n")))

    assert statuses(result) == [OperationStatus.FAILED] and "commit.failed" in messages(result)
    environment = dump.read_text()
    assert "PATH=" in environment  # the hook ran with a real environment
    assert not any(secret in environment for secret in wiki.strategy.secrets)
    assert "GIT_CONFIG_COUNT" not in environment and "GIT_CONFIG_KEY" not in environment
    assert FAKE_TOKEN not in messages(result)
    assert wiki.remote.subjects() == ["Initial Home page"]  # a failed commit never pushes


# -- the workdir lock ---------------------------------------------------------------------------------


def test_a_held_lock_refuses_an_apply_and_a_plan_and_nothing_is_written(wiki: Wiki) -> None:
    wiki.provider().exists(ref(SEED_PAGE))

    with wiki.lock_handle().hold("another run"):
        result = wiki.provider().apply_changes(change_set(create("Guide.md", "# Guide\n")))
        with pytest.raises(GithubWikiError) as plan:
            wiki.provider().exists(ref(SEED_PAGE))

    assert statuses(result) == [OperationStatus.FAILED]
    assert "workdir.locked" in messages(result) and "another run" in messages(result)
    assert plan.value.code == "workdir.locked"
    assert not (wiki.workdir / "Guide.md").exists()
    assert wiki.status() == [] and wiki.commit_count() == 1
    assert wiki.provider().apply_changes(change_set(create("Guide.md", "# Guide\n"))).results[0].status is (
        OperationStatus.APPLIED
    )  # released: the next run proceeds


CHILD_HOLDS_THE_LOCK = """
import sys, time
from pathlib import Path
from wikiops.providers.github_wiki.lock import WorkdirLock

lock = WorkdirLock(Path(sys.argv[1]), workdir=Path(sys.argv[2]))
lock.acquire("child run")
Path(sys.argv[3]).write_text("holding")
time.sleep(60)
"""


def test_a_killed_holder_does_not_block_the_next_run(wiki: Wiki) -> None:
    wiki.provider().exists(ref(SEED_PAGE))
    ready = wiki.root / "child-ready"
    source = str(Path(wikiops.__file__).resolve().parent.parent)
    child = subprocess.Popen(
        [sys.executable, "-S", "-c", CHILD_HOLDS_THE_LOCK, str(wiki.git_dir / "wikiops" / "lock"), str(wiki.workdir), str(ready)],
        env={**os.environ, "PYTHONPATH": source},
    )
    try:
        deadline = time.monotonic() + 20
        while not ready.exists():
            assert child.poll() is None and time.monotonic() < deadline, "the child never took the lock"
            time.sleep(0.02)

        blocked = wiki.provider().apply_changes(change_set(create("Guide.md", "# Guide\n")))
        assert statuses(blocked) == [OperationStatus.FAILED]
        assert "workdir.locked" in messages(blocked) and f"pid={child.pid}" in messages(blocked)
        assert "child run" in messages(blocked)
    finally:
        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=10)

    result = wiki.provider().apply_changes(change_set(create("Guide.md", "# Guide\n")))

    assert statuses(result) == [OperationStatus.APPLIED]  # the OS dropped the dead holder's lock
    assert wiki.last_commit_files() == ["Guide.md"]


def test_sequential_plan_and_apply_instances_never_contend_and_state_files_stay_out_of_git(wiki: Wiki) -> None:
    wiki.provider().exists(ref(SEED_PAGE))  # plan
    first = wiki.provider().apply_changes(change_set(create("One.md", "# one\n")))  # apply
    wiki.provider().exists(ref("One.md"))  # plan again
    second = wiki.provider().apply_changes(change_set(create("Two.md", "# two\n")))  # apply again

    assert statuses(first) + statuses(second) == [OperationStatus.APPLIED] * 2
    assert (wiki.git_dir / "wikiops" / "lock").exists()  # the lock file persists inside .git ...
    assert wiki.status() == []  # ... and neither it nor the manifest ever shows up as a change
    assert wiki.committed_files() == sorted([SEED_PAGE, "One.md", "Two.md"])
