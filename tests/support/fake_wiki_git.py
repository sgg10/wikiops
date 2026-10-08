"""A stateful, in-memory model of git for one wiki clone, driven through ``FakeGitRunner``.

``FakeGitRunner`` answers by argv prefix, which cannot express "the clone now
exists" or "the remote advanced". ``FakeWikiGit`` is a responder that keeps a
tiny model of one remote (branches as commit-id lists, the HEAD symref) and one
local clone (checked-out branch, history, remote-tracking history, dirty
entries, origin URL) and answers the git commands the sync layer issues from
that model. It never starts a process; the clone's ``.git`` directory is the
only thing it writes (an empty marker directory, so the workdir counts as
non-empty and ``rev-parse --absolute-git-dir`` has somewhere to point).

It also models the publishing side: ``add`` fills an index, ``diff --cached
--quiet`` reports whether the staged paths differ from HEAD, ``commit`` records a
``FakeCommit`` (message, paths, identity as git would resolve it) and cleans the
committed paths, and ``push --porcelain`` fast-forwards the remote branch or
answers with a ``[rejected]`` line when the remote moved on after the last fetch.

With ``track_files`` the model also looks at the workdir: a file below it (outside
``.git``) that is not in ``committed_files`` is untracked, one whose bytes differ
from the committed ones is modified, and a committed file that vanished is
deleted; ``commit`` snapshots the committed paths. That is what lets a real
backend write pages and the provider see them as dirty, exactly as git would.
Entries in ``dirty`` still add foreign changes by hand.

Failure injection: set ``fail[<subcommand>]`` to a ``CommandResult`` factory
input (returncode, stderr) and that subcommand answers with it instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from tests.support.fake_git_runner import FakeGitRunner, RecordedCall
from wikiops.providers.github_wiki.ports import CommandResult

GLOBAL_OPTIONS_WITH_VALUE = {"-c"}
GLOBAL_FLAGS = {"--literal-pathspecs"}


@dataclass(frozen=True)
class FakeCommit:
    """One commit made through the model: what was committed, with which identity."""

    message: str
    paths: tuple[str, ...]
    author: tuple[str, str]  # (name, email)
    committer: tuple[str, str]
    env: dict[str, str | None]


def sha_for(commit: str) -> str:
    """A stable 40-hex id for a commit label."""
    return commit.encode().hex().ljust(40, "0")[:40]


def subcommand_and_args(argv: tuple[str, ...]) -> tuple[str, list[str]]:
    """The git subcommand and its arguments, skipping the facade's global options."""
    rest = list(argv[1:])
    while rest and (rest[0] in GLOBAL_OPTIONS_WITH_VALUE or rest[0] in GLOBAL_FLAGS):
        rest = rest[2:] if rest[0] in GLOBAL_OPTIONS_WITH_VALUE else rest[1:]
    return rest[0], rest[1:]


@dataclass
class FakeWikiGit:
    """Model of one remote wiki and (optionally) one local clone of it."""

    workdir: Path
    remote_url: str
    # remote: branch -> commit labels, oldest first; HEAD symref (None = none)
    remote: dict[str, list[str]] = field(default_factory=lambda: {"master": ["c1"]})
    head_symref: str | None = "master"
    # local clone
    cloned: bool = False
    origin_url: str | None = None
    local_branch: str | None = "master"  # None = detached HEAD
    local: list[str] = field(default_factory=lambda: ["c1"])
    tracking: list[str] = field(default_factory=lambda: ["c1"])
    origin_head: str | None = "master"  # refs/remotes/origin/HEAD target
    # remote-tracking branches present locally; None = every remote branch plus the
    # checked-out branch and the origin/HEAD target (the lenient default)
    tracking_refs: set[str] | None = None
    dirty: list[str] = field(default_factory=list)  # raw porcelain entries ("?? Home.md")
    track_files: bool = False  # derive dirty entries from the files below the workdir as well
    committed_files: dict[str, bytes] = field(default_factory=dict)  # path -> committed bytes
    top_level: Path | None = None  # what `rev-parse --show-toplevel` prints (default: workdir)
    git_dir_path: Path | None = None  # what `rev-parse --absolute-git-dir` prints (default: workdir/.git)
    head_valid: bool = True  # False: a partial clone with no commit
    fail: dict[str, tuple[int, str]] = field(default_factory=dict)  # subcommand -> (rc, stderr)
    timeout_on: set[str] = field(default_factory=set)
    rev_list_output: str | None = None  # replaces the computed unpushed count (to inject garbage)
    # publishing side
    git_identity: tuple[str, str] | None = ("Git User", "git.user@example.test")  # configured user
    staged: set[str] = field(default_factory=set)
    commits: list[FakeCommit] = field(default_factory=list)
    pushes: list[tuple[str, ...]] = field(default_factory=list)  # argv of every push

    # -- wiring -------------------------------------------------------------

    def attach(self, runner: FakeGitRunner) -> FakeWikiGit:
        runner.script_with(["git"], self)
        return self

    def install_clone(self) -> FakeWikiGit:
        """Make the workdir a clone as if an earlier run had cloned it."""
        (self.workdir / ".git").mkdir(parents=True, exist_ok=True)
        self.git_dir.mkdir(parents=True, exist_ok=True)
        self.cloned = True
        self.origin_url = self.origin_url or self.remote_url
        return self

    @property
    def git_dir(self) -> Path:
        return self.git_dir_path or self.workdir / ".git"

    # -- the responder --------------------------------------------------------

    def __call__(self, call: RecordedCall) -> CommandResult:
        command, args = subcommand_and_args(call.argv)
        if command in self.timeout_on:
            return CommandResult(call.argv, 0, "", "", True)
        if command in self.fail:
            returncode, stderr = self.fail[command]
            return CommandResult(call.argv, returncode, "", stderr)
        handler = getattr(self, f"_cmd_{command.replace('-', '_')}", None)
        if handler is None:
            raise AssertionError(f"FakeWikiGit has no model for: {list(call.argv)}")
        returncode, stdout, stderr = handler(args, call)
        return CommandResult(call.argv, returncode, stdout, stderr)

    # -- remote commands --------------------------------------------------------

    def _cmd_ls_remote(self, args, call):
        lines = []
        if self.head_symref is not None:
            lines.append(f"ref: refs/heads/{self.head_symref}\tHEAD")
            tip = self.remote.get(self.head_symref, ["c0"])[-1]
            lines.append(f"{sha_for(tip)}\tHEAD")
        lines += [f"{sha_for(commits[-1])}\trefs/heads/{name}" for name, commits in self.remote.items()]
        return 0, "\n".join(lines) + ("\n" if lines else ""), ""

    def _cmd_clone(self, args, call):
        branch = args[args.index("--branch") + 1]
        destination = Path(args[-1])
        (destination / ".git").mkdir(parents=True, exist_ok=True)
        self.cloned = True
        self.origin_url = args[-2]
        self.local_branch = branch
        self.local = list(self.remote[branch])
        self.tracking = list(self.remote[branch])
        self.origin_head = branch
        return 0, "", ""

    def _cmd_fetch(self, args, call):
        branch = args[-1].split(":")[0].removeprefix("+refs/heads/")
        self.tracking = list(self.remote[branch])
        return 0, "", ""

    def _cmd_merge(self, args, call):
        remote = self.tracking
        if self.local[: len(remote)] == remote:  # remote is an ancestor (or equal): nothing to do
            return 0, "Already up to date.\n", ""
        if remote[: len(self.local)] == self.local:  # fast-forward
            self.local = list(remote)
            return 0, "Fast-forward\n", ""
        return 128, "", "fatal: Not possible to fast-forward, aborting.\n"

    # -- local commands -----------------------------------------------------------

    def _repo_or_error(self):
        if not self.cloned:
            return (128, "", "fatal: not a git repository (or any parent up to mount point)\n")
        return None

    def tracking_branches(self) -> set[str]:
        if self.tracking_refs is not None:
            return set(self.tracking_refs)
        return {name for name in [*self.remote, self.origin_head, self.local_branch] if name}

    def _cmd_for_each_ref(self, args, call):
        error = self._repo_or_error()
        if error:
            return error
        assert args == ["--format=%(refname:lstrip=3)", "refs/remotes/origin"], args
        return 0, "".join(f"{name}\n" for name in sorted(self.tracking_branches())), ""

    def _cmd_rev_parse(self, args, call):
        error = self._repo_or_error()
        if error:
            return error
        if args[:2] == ["--verify", "--quiet"] and len(args) == 3:
            branch = args[2].removeprefix("refs/remotes/origin/")
            if args[2].startswith("refs/remotes/origin/") and branch in self.tracking_branches():
                return 0, f"{sha_for(branch)}\n", ""
            return 1, "", ""
        if args == ["--show-toplevel"]:
            return 0, f"{self.top_level or self.workdir}\n", ""
        if args == ["--absolute-git-dir"]:
            return 0, f"{self.git_dir}\n", ""
        if args == ["--verify", "HEAD"]:
            if not self.head_valid:
                return 128, "", "fatal: Needed a single revision\n"
            return 0, f"{sha_for(self.local[-1])}\n", ""
        raise AssertionError(f"unmodelled rev-parse: {args}")

    def _cmd_config(self, args, call):
        error = self._repo_or_error()
        if error:
            return error
        assert args == ["--get", "remote.origin.url"], args
        if self.origin_url is None:
            return 1, "", ""
        return 0, f"{self.origin_url}\n", ""

    def _cmd_symbolic_ref(self, args, call):
        error = self._repo_or_error()
        if error:
            return error
        if args == ["--quiet", "--short", "HEAD"]:
            if self.local_branch is None:
                return 1, "", ""
            return 0, f"{self.local_branch}\n", ""
        if args == ["--quiet", "refs/remotes/origin/HEAD"]:
            if self.origin_head is None:
                return 1, "", ""
            return 0, f"refs/remotes/origin/{self.origin_head}\n", ""
        raise AssertionError(f"unmodelled symbolic-ref: {args}")

    def dirty_entries(self) -> list[str]:
        """Porcelain entries of the manual ``dirty`` list plus, when tracking, the files on disk."""
        entries = list(self.dirty)
        if not self.track_files:
            return entries
        known = {self._dirty_path(entry) for entry in entries}
        on_disk: dict[str, bytes] = {}
        for found in sorted(self.workdir.rglob("*")):
            relative = found.relative_to(self.workdir)
            if ".git" in relative.parts or not found.is_file():
                continue
            on_disk[relative.as_posix()] = found.read_bytes()
        for path, data in on_disk.items():
            if path in known:
                continue
            if path not in self.committed_files:
                entries.append(f"?? {path}")
            elif self.committed_files[path] != data:
                entries.append(f" M {path}")
        entries += [
            f" D {path}"
            for path in sorted(self.committed_files)
            if path not in on_disk and path not in known
        ]
        return entries

    def _cmd_status(self, args, call):
        error = self._repo_or_error()
        if error:
            return error
        assert args == ["--porcelain=v1", "-z", "--untracked-files=all"], args
        return 0, "".join(f"{entry}\0" for entry in self.dirty_entries()), ""

    # -- publishing commands ------------------------------------------------------

    @staticmethod
    def _dirty_path(entry: str) -> str:
        return entry[3:]

    def _cmd_add(self, args, call):
        assert args and args[0] == "--" and len(args) > 1, f"add must name explicit paths: {args}"
        self.staged |= set(args[1:])
        return 0, "", ""

    def _cmd_diff(self, args, call):
        assert args[:3] == ["--cached", "--quiet", "--"] and len(args) > 3, args
        changed = {self._dirty_path(entry) for entry in self.dirty_entries()}
        differs = any(path in self.staged and path in changed for path in args[3:])
        return (1 if differs else 0), "", ""

    def _identity_from(self, env, prefix):
        name, email = env.get(f"GIT_{prefix}_NAME"), env.get(f"GIT_{prefix}_EMAIL")
        if name and email:
            return (name, email)
        return self.git_identity

    def _cmd_commit(self, args, call):
        assert args[0] == "-m" and args[2] == "--" and len(args) > 3, args
        author = self._identity_from(call.env_overrides, "AUTHOR")
        committer = self._identity_from(call.env_overrides, "COMMITTER")
        if author is None or committer is None:
            return (
                128,
                "",
                "Author identity unknown\n\n*** Please tell me who you are.\n"
                "fatal: unable to auto-detect email address\n",
            )
        paths = tuple(args[3:])
        self.commits.append(FakeCommit(args[1], paths, author, committer, dict(call.env_overrides)))
        self.local.append(f"w{len(self.commits)}")
        self.dirty = [entry for entry in self.dirty if self._dirty_path(entry) not in paths]
        for path in paths:
            target = self.workdir / path
            if target.is_file():
                self.committed_files[path] = target.read_bytes()
            else:
                self.committed_files.pop(path, None)
        self.staged -= set(paths)
        return 0, f"[{self.local_branch} {sha_for(self.local[-1])[:7]}] {args[1]}\n", ""

    def _cmd_push(self, args, call):
        self.pushes.append(call.argv)
        target = args[-1].removeprefix("HEAD:refs/heads/")
        remote = self.remote.get(target, [])
        if self.local[: len(remote)] != remote:
            return (
                1,
                f"To {self.remote_url}\n!\tHEAD:refs/heads/{target}\t[rejected] (fetch first)\nDone\n",
                "error: failed to push some refs\n",
            )
        self.remote[target] = list(self.local)
        self.tracking = list(self.local)
        return 0, f"To {self.remote_url}\n*\tHEAD:refs/heads/{target}\t[new branch]\nDone\n", ""

    def _cmd_rev_list(self, args, call):
        error = self._repo_or_error()
        if error:
            return error
        assert args[0] == "--count" and args[1].endswith("..HEAD"), args
        if self.rev_list_output is not None:
            return 0, self.rev_list_output, ""
        return 0, f"{len([commit for commit in self.local if commit not in self.tracking])}\n", ""
