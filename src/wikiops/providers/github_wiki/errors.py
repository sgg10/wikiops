"""Error vocabulary of the ``github_wiki`` provider.

``CODES`` is the single, closed list of codes the provider can raise or warn
about. Every message follows one shape::

    [github_wiki:<code>] <summary>. k='v'... Hint: <action>.

Warning codes (kind ``W``) share the shape but are rendered with
``render_message`` and appended to notes or messages instead of being raised.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from wikiops.core.exceptions import ConfigurationError

NAMESPACE = "github_wiki"

_LINE_BREAKS = re.compile(r"\s*(?:\r\n|\r|\n)+\s*")


@dataclass(frozen=True)
class CodeSpec:
    """Kind (``E`` error, ``W`` warning) and default hint of one error code."""

    kind: Literal["E", "W"]
    default_hint: str


def _e(hint: str) -> CodeSpec:
    return CodeSpec("E", hint)


CODES: dict[str, CodeSpec] = {
    # -- configuration -----------------------------------------------------
    "config.invalid": _e("fix the setting named above; valid keys or variants are listed there"),
    "config.invalid_repository": _e(
        "use the form 'owner/repo' without '.wiki', a scheme or credentials"
    ),
    "config.invalid_host": _e(
        "use a bare hostname such as 'github.com' (no scheme, user-info, path or port)"
    ),
    "config.invalid_branch": _e("use a valid git branch name such as 'master'"),
    "config.invalid_message": _e(
        "use only the placeholders {plugin_id}, {provider_name} and {page_count}, "
        "and a non-empty message"
    ),
    "config.push_requires_commit": _e(
        "set allow_auto_commit: true or allow_auto_push: false"
    ),
    "config.identity_incomplete": _e(
        "provide both name and email for commit.identity mode 'custom'"
    ),
    "config.backend_recursion": _e(
        "choose a local file backend such as 'local_files' for local_backend.type"
    ),
    "config.backend_root_forbidden": _e(
        "remove root, provider_name and provider_api_version from local_backend; "
        "the root is injected from workdir"
    ),
    "config.backend_unknown": _e(
        "set local_backend.type to one of the registered provider ids"
    ),
    "config.backend_invalid": _e(
        "fix the local_backend options; the backend must accept a 'root' setting"
    ),
    "config.backend_incompatible": _e(
        "choose a backend that supports every required capability"
    ),
    "config.git_unavailable": _e("install git and make sure it is on PATH"),
    "config.key_path_missing": _e("fix auth.key_path so it points to an existing key file"),
    # -- authentication ----------------------------------------------------
    "auth.env_missing": _e(
        "export the variable with a non-empty value or pick another auth mode"
    ),
    "auth.gh_unavailable": _e("install the GitHub CLI (gh) or pick another auth mode"),
    "auth.gh_failed": _e("run 'gh auth login' and check the configured account and host"),
    "auth.rejected": _e(
        "check the credential, its scope or key, or pick an explicit auth mode "
        "(env, gh or ssh)"
    ),
    "auth.ssh_host_key": _e(
        "connect once with ssh to trust the host; wikiops never accepts host keys "
        "automatically"
    ),
    # -- remote / network --------------------------------------------------
    "wiki.not_initialized": _e(
        "create the first wiki page in the GitHub UI, enable the wiki feature, "
        "check access for the credential in use, or verify owner, repo and host"
    ),
    "network.unreachable": _e("check the network connection and the 'host' setting"),
    # -- sync --------------------------------------------------------------
    "sync.no_local_clone": _e("run once with sync_on_plan: true to create the local clone"),
    "sync.workdir_not_clone": _e("choose an empty directory or an existing clone as workdir"),
    "sync.remote_mismatch": _e(
        "use a different workdir, or change auth.mode back to the transport used "
        "by the existing clone"
    ),
    "sync.branch_mismatch": _e(
        "check out the expected branch in the workdir or change the 'branch' setting"
    ),
    "sync.branch_not_found": _e("set 'branch' to one of the branches available on the remote"),
    "sync.diverged": _e(
        "reconcile the local and remote history manually in the workdir; with "
        "auto-commit on and auto-push off, local commits accumulate and a web "
        "edit causes divergence"
    ),
    "sync.timeout": _e("raise git_timeout_seconds or check the network connection"),
    "sync.git_failed": _e("inspect the git output above and the state of the workdir"),
    "sync.stale_plan": CodeSpec(
        "W", "set sync_on_plan: true or run apply, which always syncs first"
    ),
    # -- workdir -----------------------------------------------------------
    "workdir.unusable": _e("choose another workdir path that is a writable directory"),
    "workdir.manifest_corrupt": _e(
        "delete the manifest file named above; formerly pending paths are then "
        "treated as foreign changes"
    ),
    "workdir.dirty": _e("commit, stash or discard the listed paths, or point workdir elsewhere"),
    "workdir.locked": _e(
        "wait for the other wikiops run to finish or, if none is running, retry"
    ),
    # -- page, ref and link policy ----------------------------------------
    "path.nested_not_supported": _e(
        "use a flat page name; replace path separators with '-'"
    ),
    "path.reserved": _e("choose a page path outside the reserved '.git' directory"),
    "path.not_markdown": _e("use a page path ending in '.md'"),
    "ref.unsupported_kind": _e("use a path reference (kind 'path') for wiki pages"),
    "ref.missing_path": _e("provide locator.path for the page reference"),
    "title.invalid": _e("use a title with at least one character and no path separators"),
    "asset.ref_unsupported": _e("use a path asset reference under the wiki root"),
    "link.root_anchored": _e("use a document-relative link without a leading '/'"),
    "link.raw_url": _e("use a relative link instead of a raw.githubusercontent.com URL"),
    # -- commit and push ---------------------------------------------------
    "commit.identity_missing": _e(
        "configure git user.name and user.email, or use commit.identity mode 'bot' or 'custom'"
    ),
    "commit.failed": _e(
        "inspect the workdir and the staged state; written paths stay pending "
        "and are committed by the next apply"
    ),
    "push.rejected": _e(
        "pull or rebase in the workdir manually, then push or re-run apply"
    ),
    "push.failed": _e("inspect the push output and the local commit in the workdir"),
}


def _one_line(value: object) -> str:
    return _LINE_BREAKS.sub(" | ", str(value).strip())


def render_message(
    code: str,
    summary: str,
    *,
    context: Mapping[str, object] | None = None,
    hint: str | None = None,
) -> str:
    """Render ``[github_wiki:<code>] <summary>. k='v'... Hint: <action>.``.

    Context entries whose value is ``None`` are skipped and multi-line values
    are folded onto one line, so the first logical message stays a single line.
    """
    spec = CODES.get(code)
    if spec is None:
        raise ValueError(f"Unknown {NAMESPACE} error code '{code}'")
    parts = [f"[{NAMESPACE}:{code}] {summary.strip().rstrip('.')}."]
    pairs = [
        f"{key}='{_one_line(value)}'"
        for key, value in (context or {}).items()
        if value is not None
    ]
    if pairs:
        parts.append(" ".join(pairs) + ".")
    parts.append(f"Hint: {(hint or spec.default_hint).strip().rstrip('.')}.")
    return " ".join(parts)


class GithubWikiError(ConfigurationError):
    """Typed, actionable ``github_wiki`` failure carrying a machine-readable code."""

    def __init__(
        self,
        code: str,
        summary: str,
        *,
        context: Mapping[str, object] | None = None,
        hint: str | None = None,
    ) -> None:
        message = render_message(code, summary, context=context, hint=hint)
        self.code = code
        self.summary = summary
        self.context = dict(context or {})
        self.hint = hint if hint is not None else CODES[code].default_hint
        super().__init__(message)
