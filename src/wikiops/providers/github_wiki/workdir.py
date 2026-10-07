"""Where the clone of one profile lives, and where wikiops keeps its state inside it.

Without an explicit ``workdir`` every profile gets its own clone under the
per-user cache::

    <cache>/wikiops/github_wiki/<host>/<owner>/<repo>/<key(provider_name)>

Host, owner and repo are separate lowercase segments (an ``<owner>-<repo>`` join
would map ``a-b/c`` and ``a/b-c`` onto one directory). ``key`` is injective and
safe for case-insensitive file systems, so two profiles can never share a clone
by accident. Resolution never creates anything: it only decides the path and
proves it is usable; ``prepare_parent`` creates the parents when a clone is
about to be made.

An explicit workdir never silently means "wherever the process happens to be":
an empty value, the user's home directory and the file system root are refused.

The manifest and the lock live in ``<git dir>/wikiops/`` and are therefore never
versioned and never part of the working tree.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import sys
from collections.abc import Mapping
from pathlib import Path

from wikiops.providers.github_wiki.errors import GithubWikiError
from wikiops.providers.github_wiki.text import has_control_characters

_PROVIDER_SEGMENTS = ("wikiops", "github_wiki")
STATE_DIRECTORY_NAME = "wikiops"
MANIFEST_FILENAME = "pending.json"
LOCK_FILENAME = "lock"

_KEY_PREFIX = "p-"
_KEY_KEPT = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_-")
# A key stays far below the 255-byte file name limit: longer names keep a
# readable prefix and end in a digest ("~" can only come from this branch,
# every literal "~" is escaped, so the two forms never collide).
_KEY_MAX_ESCAPED = 100
_KEY_DIGEST_CHARS = 24
_PARTIAL_ESCAPE = re.compile(r"%[0-9A-F]?$")


def _unusable(summary: str, *, hint: str | None = None, **context: object) -> GithubWikiError:
    return GithubWikiError("workdir.unusable", summary, context=context, hint=hint)


# -- cache location -----------------------------------------------------------


def _user_home() -> Path:
    return Path(os.path.expanduser("~"))


def cache_base(
    *,
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """The per-user cache directory of ``platform`` (the running OS by default).

    Linux honours an absolute ``XDG_CACHE_HOME``, macOS uses
    ``~/Library/Caches``, Windows an absolute ``%LOCALAPPDATA%``; relative
    environment values are ignored so the cache can never depend on the working
    directory. A base that is still not absolute (no usable home) is refused.
    """
    system = sys.platform if platform is None else platform
    env = os.environ if environ is None else environ
    user_home = _user_home() if home is None else home
    if system.startswith("win"):
        local = env.get("LOCALAPPDATA", "")
        base = Path(local) if local and Path(local).is_absolute() else user_home / "AppData" / "Local"
    elif system == "darwin":
        base = user_home / "Library" / "Caches"
    else:
        xdg = env.get("XDG_CACHE_HOME", "")
        base = Path(xdg) if xdg and Path(xdg).is_absolute() else user_home / ".cache"
    if not base.is_absolute():
        raise _unusable(
            "The per-user cache directory cannot be determined (no absolute home directory)",
            hint="set 'workdir' explicitly, or make HOME (or XDG_CACHE_HOME) an absolute path",
        )
    return base


def profile_key(name: str) -> str:
    """Return the injective, case-fold-safe directory name of a profile.

    Lowercase letters, digits, ``_`` and ``-`` are kept; every other character
    (uppercase letters, ``.``, ``/``, ``%``, non-ASCII, ...) becomes ``%XX`` per
    UTF-8 byte with uppercase hex. A ``%`` always consumes exactly two hex
    characters, so decoding is unambiguous even after case folding
    (``Docs`` -> ``p-%44ocs`` while ``docs`` -> ``p-docs``). The ``p-`` prefix
    rules out empty, dotted and Windows-reserved names.
    """
    escaped = "".join(
        char
        if char in _KEY_KEPT
        else "".join(f"%{byte:02X}" for byte in char.encode("utf-8", "surrogatepass"))
        for char in name
    )
    if len(escaped) <= _KEY_MAX_ESCAPED:
        return _KEY_PREFIX + escaped
    # Cut inside an escape at worst; drop the dangling "%" or "%X" so the
    # readable prefix stays decodable.
    prefix = _PARTIAL_ESCAPE.sub("", escaped[:_KEY_MAX_ESCAPED])
    digest = hashlib.sha256(name.encode("utf-8", "surrogatepass")).hexdigest()
    return f"{_KEY_PREFIX}{prefix}~{digest[:_KEY_DIGEST_CHARS]}"


def _segment(value: str, what: str) -> str:
    if (
        not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or has_control_characters(value)
    ):
        raise _unusable(
            f"The {what} is not usable as a directory name",
            hint=f"fix the {what} setting, or set 'workdir' explicitly",
            **{what: value},
        )
    return value.lower()


def default_workdir(
    *,
    host: str,
    repository: str,
    provider_name: str,
    cache_dir: Path | None = None,
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """The cache path of one profile; ``cache_dir`` replaces the per-user cache base."""
    owner, _, repo = repository.partition("/")
    base = cache_dir if cache_dir is not None else cache_base(
        platform=platform, environ=environ, home=home
    )
    if not base.is_absolute():
        raise _unusable(
            "The cache directory must be an absolute path",
            cache_dir=str(base),
            hint="pass an absolute cache directory, or set 'workdir' explicitly",
        )
    return base.joinpath(
        *_PROVIDER_SEGMENTS,
        _segment(host, "host"),
        _segment(owner, "repository owner"),
        _segment(repo, "repository name"),
        profile_key(provider_name),
    )


# -- resolution and usability -------------------------------------------------


def _expand(raw: str, home: Path) -> Path:
    if raw == "~" or raw.startswith(("~/", "~\\")):
        return home / raw[2:]
    expanded = os.path.expanduser(raw)
    if raw.startswith("~") and expanded == raw:
        raise _unusable(
            "The home directory in 'workdir' cannot be expanded",
            workdir=raw,
            hint="use an absolute path, or '~/' for your own home directory",
        )
    return Path(expanded)


def _resolve_explicit(raw: str, *, cwd: Path, home: Path) -> Path:
    text = raw.strip()
    if not text:
        raise _unusable(
            "The configured workdir is empty",
            hint="set 'workdir' to a directory, or remove it to use the per-user cache",
        )
    try:
        resolved = (cwd / _expand(text, home)).resolve(strict=False)
        home_real = home.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise _unusable(
            "The workdir path could not be resolved (symlink loop or unreadable component)",
            workdir=raw,
            hint="fix the symlink loop or the permissions along the path",
        ) from exc
    if resolved == home_real:
        raise _unusable(
            "The workdir is the user's home directory",
            workdir=resolved,
            hint="choose a dedicated directory such as '~/wikis/<name>'",
        )
    if resolved.parent == resolved:
        raise _unusable(
            "The workdir is the file system root",
            workdir=resolved,
            hint="choose a dedicated directory for the wiki clone",
        )
    return resolved


def _check_usable(workdir: Path) -> None:
    """Prove ``workdir`` is, or can be created as, a writable directory."""
    probe = workdir
    try:
        while not os.path.lexists(probe):
            if probe.parent == probe:
                raise _unusable("The workdir has no existing ancestor", workdir=workdir)
            probe = probe.parent
        info = os.stat(probe)
    except OSError as exc:
        raise _unusable(
            f"The workdir cannot be inspected ({type(exc).__name__}: {exc.strerror or exc})",
            workdir=workdir,
            hint="fix the permissions or the symlinks along the path",
        ) from exc
    if not stat.S_ISDIR(info.st_mode):
        summary = (
            "The workdir exists and is not a directory"
            if probe == workdir
            else f"The workdir cannot be created: '{probe}' is not a directory"
        )
        raise _unusable(summary, workdir=workdir, hint="choose a path that is, or can become, a directory")
    if not os.access(probe, os.W_OK | os.X_OK):
        raise _unusable(
            f"The workdir is not writable ('{probe}' denies write access)"
            if probe == workdir
            else f"The workdir cannot be created: '{probe}' is not writable",
            workdir=workdir,
            hint="fix the directory permissions or choose another workdir",
        )


def resolve_workdir(
    *,
    configured: str | None,
    host: str,
    repository: str,
    provider_name: str,
    cwd: Path | None = None,
    cache_dir: Path | None = None,
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Return the absolute, usable workdir of a profile; never creates anything.

    An explicit ``configured`` value has ``~`` expanded, relative values resolved
    against ``cwd`` (the process working directory by default) and symlinks
    resolved. Otherwise the per-profile cache path is used. A path that exists as
    a file, cannot be created or is not writable raises ``workdir.unusable``.
    """
    user_home = _user_home() if home is None else home
    if configured is not None:
        workdir = _resolve_explicit(
            configured, cwd=Path.cwd() if cwd is None else cwd, home=user_home
        )
    else:
        workdir = default_workdir(
            host=host,
            repository=repository,
            provider_name=provider_name,
            cache_dir=cache_dir,
            platform=platform,
            environ=environ,
            home=user_home,
        )
        try:
            workdir = workdir.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise _unusable(
                "The cache workdir could not be resolved",
                workdir=workdir,
                hint="fix the symlinks in the cache directory or set 'workdir' explicitly",
            ) from exc
    _check_usable(workdir)
    return workdir


def prepare_parent(workdir: Path) -> None:
    """Create the missing parents of ``workdir`` (not the workdir itself).

    The git facade never creates directories; the sync layer calls this right
    before the first clone.
    """
    try:
        workdir.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _unusable(
            f"The parent of the workdir cannot be created ({type(exc).__name__}: {exc.strerror or exc})",
            workdir=workdir,
            hint="remove the file in the way or choose another workdir",
        ) from exc


# -- state files inside the git directory -------------------------------------


def state_directory(git_dir: Path) -> Path:
    """``<git dir>/wikiops``: holds the manifest and the lock, never versioned."""
    return git_dir / STATE_DIRECTORY_NAME


def manifest_path(git_dir: Path) -> Path:
    return state_directory(git_dir) / MANIFEST_FILENAME


def lock_path(git_dir: Path) -> Path:
    return state_directory(git_dir) / LOCK_FILENAME


def ensure_state_directory(directory: Path, *, workdir: Path) -> None:
    """Create ``directory`` (``<git dir>/wikiops``) inside a git directory that already exists.

    Only the state directory itself is created, never its parents: a missing git
    directory means the workdir is not a clone, and inventing ``.git/wikiops``
    would leave a stray tree that later looks like a half-made clone. Other
    ``OSError`` causes (the git directory is a file, permissions) propagate for
    the caller to report as ``workdir.unusable``.
    """
    try:
        directory.mkdir(exist_ok=True)
    except FileNotFoundError as exc:
        raise GithubWikiError(
            "sync.workdir_not_clone",
            "The workdir has no git directory to keep wikiops state in",
            context={"workdir": workdir, "git_dir": directory.parent},
        ) from exc
