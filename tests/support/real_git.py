"""Real-git wiring for the ``git_integration`` suite: bare remotes, a file:// transport, a provider.

Everything here talks to a real ``git`` executable against bare repositories on
the local disk (``file://`` URLs): no network, no credentials, no user git
configuration. ``hermetic_git_environment`` is what makes that true (a private
``HOME``, ``GIT_CONFIG_GLOBAL``/``GIT_CONFIG_NOSYSTEM``, a fixed identity) and is
applied by the suite's autouse fixture before any process starts, so the
provider's ``SubprocessGitRunner`` and the helpers below inherit it.

* ``Remote``: a bare repository whose ``HEAD`` is ``master`` (or ``main``), a
  seeded ``Home.md``, and a second "web user" clone (``edit``) that makes
  concurrent changes.
* ``FileTransportStrategy``: the test-only ``CredentialStrategy`` for ``file://``.
  It can inject a fake token through the real ``GIT_CONFIG_*`` shape so tests
  can prove that no secret reaches disk or a hook.
* ``Wiki``: the provider under test (a NEW instance per ``provider()`` call, like
  a plan and an apply run) plus read-only inspection of the clone.
"""

from __future__ import annotations

import base64
import functools
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tests.support.provider_harness import real_resolver
from wikiops.providers.github_wiki.lock import WorkdirLock
from wikiops.providers.github_wiki.ports import BackendResolver, GitTransport
from wikiops.providers.github_wiki.process import SubprocessGitRunner
from wikiops.providers.github_wiki.provider import GithubWikiProvider
from wikiops.providers.github_wiki.settings import parse_settings
from wikiops.providers.github_wiki.workdir import lock_path, manifest_path

MINIMUM_GIT = (2, 28)  # `git init --initial-branch`
FAKE_TOKEN = "ghp_integration_SECRET_42"
IDENTITY = ("Git User", "git.user@example.test")
SEED_PAGE = "Home.md"
SEED_CONTENT = "# Home\n"


@functools.cache
def installed_git_version() -> tuple[int, ...] | None:
    """The numeric version of the ``git`` on PATH, or ``None`` when git is missing."""
    executable = shutil.which("git")
    if executable is None:
        return None
    text = subprocess.run([executable, "--version"], capture_output=True, text=True, check=False).stdout
    found = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
    if found is None:
        return None
    return tuple(int(part) for part in found.groups() if part is not None)


def skip_reason() -> str | None:
    """Why the suite cannot run here (``None`` when it can)."""
    version = installed_git_version()
    if version is None:
        return "git is not installed"
    if version[:2] < MINIMUM_GIT:
        return f"git {'.'.join(map(str, version))} is older than 2.28"
    return None


def hermetic_git_environment(root: Path) -> tuple[dict[str, str], list[str]]:
    """The variables that isolate git from the user's machine, and the ones to remove.

    Returns ``(set, unset)``. The global configuration is one private file that
    holds only a fixed identity, so a user's templates, hooks, signing or
    ``push.default`` can never leak into a test.
    """
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    config = home / ".gitconfig"
    config.write_text(
        "[user]\n"
        f"\tname = {IDENTITY[0]}\n"
        f"\temail = {IDENTITY[1]}\n"
        "[init]\n\tdefaultBranch = main\n"
        "[commit]\n\tgpgsign = false\n"
        "[advice]\n\tdetachedHead = false\n",
        encoding="utf-8",
    )
    values = {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "GIT_CONFIG_GLOBAL": str(config),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
    }
    unset = [name for name in os.environ if name.startswith("GIT_") and name not in values]
    return values, unset


def run_git(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    """Run real git (inheriting the hermetic environment) and return its stdout."""
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed ({result.returncode}): {result.stderr}")
    return result.stdout


def install_hook(git_dir: Path, name: str, script: str) -> Path:
    """Write an executable hook ``script`` (a shell body) into ``<git_dir>/hooks``."""
    hooks = git_dir / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / name
    hook.write_text(f"#!/bin/sh\n{script}\n", encoding="utf-8")
    hook.chmod(0o755)
    return hook


# -- remote ------------------------------------------------------------------------------------


class Remote:
    """A bare repository on disk plus a second clone that edits it like a web user.

    ``create`` builds one from scratch (a seeded ``Home.md``); ``copy_to`` clones
    that finished pair with plain file copies, which is how every test gets its
    own remote without paying for a dozen git processes each time. The web clone
    always names the remote URL explicitly, so a copy never points at the original.
    """

    def __init__(self, bare: Path, web: Path, head: str) -> None:
        self.head = head
        self.bare = bare
        self.url = bare.as_uri()
        self._web = web

    @classmethod
    def create(cls, root: Path, *, head: str = "master", name: str = "platform") -> Remote:
        remote = cls(root / "remotes" / f"{name}.wiki.git", root / "remotes" / f"{name}-web", head)
        remote.bare.parent.mkdir(parents=True, exist_ok=True)
        run_git("init", "-q", "--bare", f"--initial-branch={head}", str(remote.bare))
        run_git("init", "-q", f"--initial-branch={head}", str(remote._web))
        (remote._web / SEED_PAGE).write_text(SEED_CONTENT, encoding="utf-8")
        run_git("add", "--", SEED_PAGE, cwd=remote._web)
        run_git("commit", "-q", "-m", "Initial Home page", cwd=remote._web)
        run_git("push", "-q", remote.url, f"{head}:{head}", cwd=remote._web)
        return remote

    def copy_to(self, root: Path, *, name: str = "platform") -> Remote:
        """An independent remote (own bare repository and web clone) with the same history."""
        copy = Remote(root / "remotes" / f"{name}.wiki.git", root / "remotes" / f"{name}-web", self.head)
        copy.bare.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.bare, copy.bare, symlinks=True)
        shutil.copytree(self._web, copy._web, symlinks=True)
        return copy

    # -- changes made by "someone else" -----------------------------------------------

    def edit(
        self,
        path: str,
        content: str,
        *,
        branch: str | None = None,
        message: str = "edit on the web",
    ) -> str:
        """Commit ``path`` on top of the remote ``branch`` (created if absent) and push it."""
        branch = branch or self.head
        if branch in self.branches():
            run_git("fetch", "-q", self.url, f"+refs/heads/{branch}:refs/remotes/origin/{branch}", cwd=self._web)
            run_git("checkout", "-q", "-B", branch, f"origin/{branch}", cwd=self._web)
        else:
            run_git("checkout", "-q", "-B", branch, cwd=self._web)
        target = self._web / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        run_git("add", "--", path, cwd=self._web)
        run_git("commit", "-q", "-m", message, cwd=self._web)
        run_git("push", "-q", self.url, f"{branch}:{branch}", cwd=self._web)
        return self.rev(branch)

    # -- what the remote holds ----------------------------------------------------------

    def branches(self) -> list[str]:
        out = run_git("for-each-ref", "--format=%(refname:short)", "refs/heads", cwd=self.bare)
        return sorted(out.split())

    def rev(self, branch: str | None = None) -> str:
        return run_git("rev-parse", f"refs/heads/{branch or self.head}", cwd=self.bare).strip()

    def subjects(self, branch: str | None = None) -> list[str]:
        out = run_git("log", "--format=%s", f"refs/heads/{branch or self.head}", cwd=self.bare)
        return out.splitlines()

    def files(self, branch: str | None = None) -> list[str]:
        out = run_git("ls-tree", "-r", "--name-only", f"refs/heads/{branch or self.head}", cwd=self.bare)
        return sorted(out.splitlines())

    def show(self, path: str, branch: str | None = None) -> str:
        return run_git("show", f"refs/heads/{branch or self.head}:{path}", cwd=self.bare)

    def commit_author(self, branch: str | None = None) -> str:
        return run_git("log", "-1", "--format=%an <%ae>", f"refs/heads/{branch or self.head}", cwd=self.bare).strip()


# -- runner --------------------------------------------------------------------------------------


class RecordingRunner(SubprocessGitRunner):
    """The real runner that also remembers every argv it executed."""

    def __init__(self) -> None:
        super().__init__()
        self.argvs: list[tuple[str, ...]] = []

    def run(self, argv, **options):  # noqa: ANN001, ANN003, ANN201
        self.argvs.append(tuple(argv))
        return super().run(argv, **options)

    def with_subcommand(self, subcommand: str) -> list[tuple[str, ...]]:
        """The argvs of one git subcommand (``-c k=v`` and ``--literal-pathspecs`` skipped)."""
        found = []
        for argv in self.argvs:
            index = 1
            while index < len(argv) and (argv[index] == "-c" or argv[index].startswith("--literal")):
                index += 2 if argv[index] == "-c" else 1
            if index < len(argv) and argv[index] == subcommand:
                found.append(argv)
        return found


# -- transport ---------------------------------------------------------------------------------


class FileTransportStrategy:
    """Test-only ``CredentialStrategy`` that talks to a ``file://`` remote.

    With ``token`` set, every network command also carries the real HTTPS
    ``GIT_CONFIG_*`` credential shape (inert for a file remote), so a hook or a
    file that ever sees the token is detectable. ``issued`` counts transports.
    """

    def __init__(self, remote_url: str, *, token: str | None = None) -> None:
        self.remote_url = remote_url
        self.label = "file"
        self._token = token
        self.issued = 0

    @property
    def secrets(self) -> tuple[str, ...]:
        if self._token is None:
            return ()
        pair = f"x-access-token:{self._token}"
        return (self._token, pair, base64.b64encode(pair.encode()).decode())

    def check_offline(self) -> None:
        return None

    def transport(self) -> GitTransport:
        self.issued += 1
        overrides: dict[str, str | None] = {}
        if self._token is not None:
            header = f"AUTHORIZATION: basic {self.secrets[2]}"
            overrides = {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                "GIT_CONFIG_VALUE_0": header,
            }
        return GitTransport(self.remote_url, overrides, self.secrets, self.label)


# -- the provider under test -------------------------------------------------------------------


@dataclass
class Wiki:
    """A real-git provider setup: remote, workdir, credentials and a provider factory."""

    root: Path
    remote: Remote
    workdir: Path
    raw: dict[str, Any]
    strategy: FileTransportStrategy
    resolver: BackendResolver
    runner: RecordingRunner = field(default_factory=RecordingRunner)

    def provider(self, **settings: Any) -> GithubWikiProvider:
        """A NEW provider instance (a fresh plan or apply run) over the same clone."""
        return GithubWikiProvider(
            parse_settings({**self.raw, **settings}),
            runner=self.runner,
            credentials=self.strategy,
            backends=self.resolver,
            cache_dir=self.root / "cache",
        )

    # -- the clone, read with real git -------------------------------------------------

    @property
    def git_dir(self) -> Path:
        return self.workdir / ".git"

    def git(self, *args: str, check: bool = True) -> str:
        return run_git(*args, cwd=self.workdir, check=check)

    def status(self) -> list[str]:
        return [line for line in self.git("status", "--porcelain=v1", "--untracked-files=all").splitlines()]

    def subjects(self, ref: str = "HEAD") -> list[str]:
        return self.git("log", "--format=%s", ref).splitlines()

    def head(self) -> str:
        return self.git("rev-parse", "HEAD").strip()

    def branch(self) -> str:
        return self.git("symbolic-ref", "--short", "HEAD").strip()

    def committed_files(self, ref: str = "HEAD") -> list[str]:
        return sorted(self.git("ls-tree", "-r", "--name-only", ref).splitlines())

    def last_commit_files(self) -> list[str]:
        return sorted(self.git("show", "--name-only", "--format=", "HEAD").splitlines())

    def commit_count(self, ref: str = "HEAD") -> int:
        return int(self.git("rev-list", "--count", ref).strip())

    # -- state files ----------------------------------------------------------------------

    def lock_handle(self) -> WorkdirLock:
        """A second, independent handle on the clone's lock file (like another process)."""
        return WorkdirLock(lock_path(self.git_dir), workdir=self.workdir)

    @property
    def manifest_file(self) -> Path:
        return manifest_path(self.git_dir)


def build_wiki(
    root: Path,
    *,
    head: str = "master",
    workdir: Path | None = None,
    token: str | None = None,
    resolver: BackendResolver | None = None,
    remote: Remote | None = None,
    template: Remote | None = None,
    **settings: Any,
) -> Wiki:
    """A remote with ``head`` as its default branch and a provider rooted at ``workdir``.

    ``template`` (a finished ``Remote.create``) is copied instead of rebuilding the
    remote with git; without it the remote is created from scratch.
    """
    if remote is None:
        remote = template.copy_to(root) if template is not None else Remote.create(root, head=head)
    target = workdir or root / "work" / "wiki"
    target.parent.mkdir(parents=True, exist_ok=True)
    raw: dict[str, Any] = {
        "provider_name": "docs",
        "repository": "acme/platform",
        "workdir": str(target),
        **settings,
    }
    return Wiki(
        root=root,
        remote=remote,
        workdir=target.resolve(),
        raw=raw,
        strategy=FileTransportStrategy(remote.url, token=token),
        resolver=resolver or real_resolver(),
    )


def files_containing(root: Path, needles: tuple[str, ...]) -> list[Path]:
    """Every file below ``root`` (``.git`` included) whose bytes hold any of ``needles``."""
    encoded = [needle.encode() for needle in needles if needle]
    hits: list[Path] = []
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            data = path.read_bytes()
            if any(needle in data for needle in encoded):
                hits.append(path)
    return hits
