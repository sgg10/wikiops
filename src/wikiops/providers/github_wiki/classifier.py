"""Turns failed git commands into one coded ``GithubWikiError``.

Classification is a tolerant, case-insensitive substring match over stderr
captured under ``LC_ALL=C``: the same class maps to the same code on HTTPS,
SSH and ``file://`` remotes, and surrounding noise is ignored. Push outcomes
are read from ``git push --porcelain`` first, never from localized text. Every
text that reaches the message is redacted and limited to the last lines.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from wikiops.providers.github_wiki.errors import GithubWikiError
from wikiops.providers.github_wiki.ports import CommandResult
from wikiops.providers.github_wiki.redaction import Redactor

TAIL_LINES = 10

_HOST_KEY = "auth.ssh_host_key"
_NOT_FOUND = "wiki.not_initialized"
_AUTH = "auth.rejected"
_NETWORK = "network.unreachable"
# The one definition of the ssh "key refused" marker: it classifies the failure
# as ``auth.rejected`` and selects the ssh-specific hint.
_PUBLICKEY_DENIED = r"permission denied \(publickey"


def _patterns(*sources: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(source, re.IGNORECASE) for source in sources)


# Checked in this order: a not-found text never carries an auth marker, and a
# connectivity text carries neither.
_REMOTE_CLASSES: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = (
    (_HOST_KEY, _patterns(r"host key verification failed")),
    (
        _NOT_FOUND,
        _patterns(
            r"repository not found",
            r"repository '[^']*' not found",
            r"does not appear to be a git repository",
        ),
    ),
    (
        _AUTH,
        _patterns(
            r"authentication failed",
            r"could not read username",
            r"terminal prompts disabled",
            r"invalid username or password",
            r"requested url returned error: 40[13]",
            r"http basic: access denied",
            _PUBLICKEY_DENIED,
        ),
    ),
    (
        _NETWORK,
        _patterns(
            r"could not resolve host",
            r"connection timed out",
            r"connection refused",
            r"ssh: connect to host",
            r"failed to connect to",
            r"network is unreachable",
        ),
    ),
)
_IDENTITY_MISSING = _patterns(
    r"please tell me who you are",
    r"empty ident name",
    r"unable to auto-detect email address",
    r"author identity unknown",
)
_SSH_KEY_DENIED = re.compile(_PUBLICKEY_DENIED, re.IGNORECASE)

_SUMMARIES = {
    _HOST_KEY: "SSH host key verification failed",
    _NOT_FOUND: (
        "Wiki repository not found. Possible causes: the wiki is not initialized "
        "(create its first page in the GitHub UI), the wiki feature is disabled, "
        "the credential or account in use has no access, or owner, repo or host is wrong"
    ),
    _AUTH: "The remote rejected the credentials",
    _NETWORK: "Could not reach the remote",
}
_SSH_AUTH_HINT = (
    "check the ssh key or agent and, to pin one identity, set auth.key_path; "
    "or pick another auth mode (env or gh)"
)

_PORCELAIN_LINE = re.compile(r"(?P<flag>[ +\-*=!])\t(?P<ref>[^\t]*)\t(?P<summary>.*)")


@dataclass(frozen=True)
class PushRefResult:
    """One ref line of ``git push --porcelain``: flag, ``from:to`` ref, summary."""

    flag: str
    ref: str
    summary: str


def parse_push_porcelain(stdout: str) -> tuple[PushRefResult, ...]:
    """Parse the ref lines of ``git push --porcelain``, ignoring every other line."""
    results = []
    for line in stdout.splitlines():
        match = _PORCELAIN_LINE.fullmatch(line)
        if match:
            results.append(PushRefResult(match["flag"], match["ref"], match["summary"]))
    return tuple(results)


def stderr_tail(stderr: str, redactor: Redactor, *, limit: int = TAIL_LINES) -> str:
    """Redact ``stderr`` and keep its last ``limit`` non-blank lines."""
    lines = [line.rstrip() for line in redactor.redact(stderr).splitlines() if line.strip()]
    return "\n".join(lines[-limit:])


def _remote_class(text: str) -> str | None:
    for code, patterns in _REMOTE_CLASSES:
        if any(pattern.search(text) for pattern in patterns):
            return code
    return None


def _context(
    operation: str,
    result: CommandResult,
    redactor: Redactor,
    context: Mapping[str, object] | None,
    extra: Mapping[str, object] | None = None,
) -> dict[str, object]:
    merged: dict[str, object] = {"op": operation, **(context or {}), **(extra or {})}
    safe = {
        key: value if value is None else redactor.redact(str(value))
        for key, value in merged.items()
    }
    safe["stderr"] = stderr_tail(result.stderr, redactor) or None
    return safe


def _remote_error(code: str, stderr: str, details: dict[str, object]) -> GithubWikiError:
    hint = _SSH_AUTH_HINT if code == _AUTH and _SSH_KEY_DENIED.search(stderr) else None
    return GithubWikiError(code, _SUMMARIES[code], context=details, hint=hint)


def _classify_push(
    result: CommandResult,
    redactor: Redactor,
    context: Mapping[str, object] | None,
) -> GithubWikiError:
    refused = [ref for ref in parse_push_porcelain(result.stdout) if ref.flag == "!"]
    for ref in refused:
        if ref.summary.startswith("[rejected]"):
            details = _context("push", result, redactor, context, {"reason": ref.summary})
            return GithubWikiError(
                "push.rejected",
                "Push rejected: the remote branch has commits that are not in the local branch",
                context=details,
            )
    if refused:
        details = _context("push", result, redactor, context, {"reason": refused[0].summary})
        return GithubWikiError("push.failed", "The remote refused the push", context=details)
    details = _context("push", result, redactor, context)
    code = _remote_class(result.stderr)
    if code is not None:
        return _remote_error(code, result.stderr, details)
    return GithubWikiError("push.failed", "git push failed", context=details)


def classify(
    operation: str,
    result: CommandResult,
    *,
    redactor: Redactor,
    context: Mapping[str, object] | None = None,
) -> GithubWikiError:
    """Classify a FAILED command of kind ``operation`` into a ``GithubWikiError``.

    ``operation`` is the git subcommand (``ls-remote``, ``clone``, ``fetch``,
    ``push``, ``commit``, ...). ``context`` (workdir, remote, auth label, sha)
    is rendered in the message after redaction; the stderr tail is added.
    """
    if result.returncode == 0 and not result.timed_out:
        raise ValueError("classify() needs a failed command, got a successful one")
    if result.timed_out:
        details = _context(operation, result, redactor, context)
        if operation == "push":
            return GithubWikiError("push.failed", "git push timed out", context=details)
        return GithubWikiError("sync.timeout", f"git {operation} timed out", context=details)
    if operation == "push":
        return _classify_push(result, redactor, context)
    details = _context(operation, result, redactor, context)
    if operation == "commit":
        if any(pattern.search(result.stderr) for pattern in _IDENTITY_MISSING):
            return GithubWikiError(
                "commit.identity_missing",
                "git has no author identity to commit with",
                context=details,
            )
        return GithubWikiError("commit.failed", "git commit failed", context=details)
    code = _remote_class(result.stderr)
    if code is not None:
        return _remote_error(code, result.stderr, details)
    return GithubWikiError("sync.git_failed", f"git {operation} failed", context=details)
