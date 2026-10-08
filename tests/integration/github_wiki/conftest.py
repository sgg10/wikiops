"""Fixtures of the real-git suite: a hermetic git environment and wiki builders.

Every test in this directory runs real ``git`` against ``file://`` bare
repositories below ``tmp_path``. The autouse fixture skips the suite when git is
missing or older than 2.28 and otherwise isolates the process from the user's
git configuration before any child process starts.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.support.real_git import Remote, Wiki, build_wiki, hermetic_git_environment, skip_reason


@pytest.fixture(autouse=True)
def hermetic_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reason = skip_reason()
    if reason is not None:
        pytest.skip(reason)
    values, unset = hermetic_git_environment(tmp_path)
    for name in unset:
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)


@pytest.fixture(scope="session")
def remote_templates(tmp_path_factory: pytest.TempPathFactory) -> Callable[[str], Remote]:
    """Finished seeded remotes (one per default-branch name) that tests copy instead of rebuilding."""
    built: dict[str, Remote] = {}

    def _template(head: str) -> Remote:
        if head not in built:
            root = tmp_path_factory.mktemp(f"template-{head}")
            reason = skip_reason()
            if reason is not None:
                pytest.skip(reason)
            values, unset = hermetic_git_environment(root)
            with pytest.MonkeyPatch.context() as patch:
                for name in unset:
                    patch.delenv(name, raising=False)
                for name, value in values.items():
                    patch.setenv(name, value)
                built[head] = Remote.create(root, head=head)
        return built[head]

    return _template


@pytest.fixture
def make_wiki(tmp_path: Path, remote_templates: Callable[[str], Remote]) -> Callable[..., Wiki]:
    """Build a wiki (remote + provider factory); keyword arguments reach ``build_wiki``."""

    def _make(**options: Any) -> Wiki:
        if "remote" not in options:
            options.setdefault("template", remote_templates(options.get("head", "master")))
        return build_wiki(tmp_path, **options)

    return _make


@pytest.fixture
def wiki(make_wiki: Callable[..., Wiki]) -> Wiki:
    return make_wiki()
