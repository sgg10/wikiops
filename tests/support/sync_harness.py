"""Wiring shared by the sync-layer tests: a ``WikiSync`` over ``FakeWikiGit``.

``SyncHarness`` builds the real ``Git`` facade over a ``FakeGitRunner`` that is
answered by a ``FakeWikiGit`` model, a stub credential strategy that counts the
transports it issues (so tests can prove an offline run asked for none), and a
lock factory that records when each lock was taken and released relative to the
git commands issued so far. Every ``sync()`` call returns a NEW ``WikiSync``
over the same runner, model and workdir: a plan instance and an apply instance.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from tests.support.fake_git_runner import FakeGitRunner
from tests.support.fake_wiki_git import FakeWikiGit
from wikiops.providers.github_wiki.git import Git
from wikiops.providers.github_wiki.lock import WorkdirLock
from wikiops.providers.github_wiki.ports import GitTransport
from wikiops.providers.github_wiki.sync import WikiSync

TOKEN = "ghp_sync_SECRET_7"
REMOTE = "https://github.com/acme/platform.wiki.git"
SSH_REMOTE = "git@github.com:acme/platform.wiki.git"
TIMEOUT = 30.0


@dataclass
class StubStrategy:
    """Credential strategy double; counts issued transports."""

    label: str = "env:T"
    remote_url: str = REMOTE
    secrets: tuple[str, ...] = (TOKEN,)
    issued: int = 0

    def check_offline(self) -> None:
        return None

    def transport(self) -> GitTransport:
        self.issued += 1
        overrides = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "k", "GIT_CONFIG_VALUE_0": TOKEN}
        return GitTransport(self.remote_url, overrides, self.secrets, self.label)


@dataclass
class LockEvent:
    event: str  # "acquire" | "release"
    calls_before: int  # git commands issued before the event
    clones_before: int  # of which clones


@dataclass
class SyncHarness:
    workdir: Path
    runner: FakeGitRunner
    fake: FakeWikiGit
    strategy: StubStrategy
    lock_events: list[LockEvent] = field(default_factory=list)

    def _lock_factory(self) -> Callable[..., WorkdirLock]:
        harness = self

        class RecordingLock(WorkdirLock):
            def _record(self, event: str) -> None:
                clones = len([argv for argv in harness.runner.argvs if "clone" in argv])
                harness.lock_events.append(LockEvent(event, len(harness.runner.calls), clones))

            def acquire(self, purpose: str = "") -> None:
                super().acquire(purpose)
                self._record("acquire")

            def release(self) -> None:
                super().release()
                self._record("release")

        return RecordingLock

    def git(self) -> Git:
        return Git(self.runner, self.strategy, workdir=self.workdir, timeout=TIMEOUT)

    def sync(self, *, branch: str | None = None, sync_on_plan: bool = True) -> WikiSync:
        return WikiSync(
            self.git(),
            self.strategy,
            branch=branch,
            sync_on_plan=sync_on_plan,
            lock_factory=self._lock_factory(),
        )

    # -- inspection helpers -------------------------------------------------

    def subcommands(self) -> list[str]:
        from tests.support.fake_wiki_git import subcommand_and_args

        return [subcommand_and_args(call.argv)[0] for call in self.runner.calls]

    def network_calls(self) -> list[str]:
        return [
            subcommand
            for subcommand, call in zip(self.subcommands(), self.runner.calls, strict=True)
            if any(part.startswith("core.hooksPath=") for part in call.argv)
        ]


def build(
    tmp_path: Path,
    *,
    cloned: bool = False,
    workdir: Path | None = None,
    remote_url: str = REMOTE,
    **model: object,
) -> SyncHarness:
    """A harness whose workdir is ``tmp_path/cache/wiki`` (parent NOT created) unless given."""
    target = workdir or tmp_path / "cache" / "wiki"
    runner = FakeGitRunner()
    fake = FakeWikiGit(workdir=target, remote_url=remote_url, **model).attach(runner)  # type: ignore[arg-type]
    if cloned:
        target.mkdir(parents=True, exist_ok=True)
        fake.install_clone()
    return SyncHarness(target, runner, fake, StubStrategy(remote_url=remote_url))
