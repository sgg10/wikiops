"""A stateful, in-memory model of git for one wiki clone, driven through ``FakeGitRunner``.

``FakeGitRunner`` answers by argv prefix, which cannot express "the clone now
exists" or "the remote advanced". ``FakeWikiGit`` is a responder that keeps a
tiny model of one remote (branches as commit-id lists, the HEAD symref) and one
local clone (checked-out branch, history, remote-tracking history, dirty
entries, origin URL) and answers the git commands the sync layer issues from
that model. It never starts a process; the clone's ``.git`` directory is the
only thing it writes (an empty marker directory, so the workdir counts as
non-empty and ``rev-parse --absolute-git-dir`` has somewhere to point).

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
    dirty: list[str] = field(default_factory=list)  # raw porcelain entries ("?? Home.md")
    top_level: Path | None = None  # what `rev-parse --show-toplevel` prints (default: workdir)
    git_dir_path: Path | None = None  # what `rev-parse --absolute-git-dir` prints (default: workdir/.git)
    head_valid: bool = True  # False: a partial clone with no commit
    fail: dict[str, tuple[int, str]] = field(default_factory=dict)  # subcommand -> (rc, stderr)
    timeout_on: set[str] = field(default_factory=set)
    rev_list_output: str | None = None  # replaces the computed unpushed count (to inject garbage)

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

    def _cmd_rev_parse(self, args, call):
        error = self._repo_or_error()
        if error:
            return error
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

    def _cmd_status(self, args, call):
        error = self._repo_or_error()
        if error:
            return error
        assert args == ["--porcelain=v1", "-z", "--untracked-files=all"], args
        return 0, "".join(f"{entry}\0" for entry in self.dirty), ""

    def _cmd_rev_list(self, args, call):
        error = self._repo_or_error()
        if error:
            return error
        assert args[0] == "--count" and args[1].endswith("..HEAD"), args
        if self.rev_list_output is not None:
            return 0, self.rev_list_output, ""
        return 0, f"{len([commit for commit in self.local if commit not in self.tracking])}\n", ""
