"""Settings models of the ``github_wiki`` provider.

Every model rejects unknown keys, except ``LocalBackendSettings`` whose extra
keys are options for the composed local backend and are validated by that
backend. ``auth`` and ``commit.identity`` are discriminated unions on ``mode``.
"""

from __future__ import annotations

import re
import string
import types
from collections.abc import Mapping
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic.fields import FieldInfo

from wikiops.providers.github_wiki.errors import GithubWikiError
from wikiops.providers.github_wiki.text import has_control_characters
from wikiops_sdk.contracts import ProviderSettings

_ENV_VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# GitHub login: alphanumeric first character (never '-', which gh would read as
# an option), then letters, digits, '-' and '_' (Enterprise Managed Users).
_GITHUB_LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,38}")
_EMAIL = re.compile(r"[^@\s<>]+@[^@\s<>]+")
_REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+")
_HOST_LABEL = r"(?!-)[A-Za-z0-9-]{1,63}(?<!-)"
_HOST = re.compile(rf"{_HOST_LABEL}(?:\.{_HOST_LABEL})*")
_BRANCH_FORBIDDEN_CHARACTERS = frozenset("~^:?*[\\")
_MESSAGE_PLACEHOLDERS = ("plugin_id", "provider_name", "page_count")
_BACKEND_INJECTED_KEYS = ("root", "provider_name", "provider_api_version")
_BACKEND_RECURSIVE_TYPE = "github_wiki"
_DEFAULT_MESSAGE = "docs(wiki): update via wikiops plugin {plugin_id}"
_BOT_NAME = "wikiops"
_BOT_EMAIL = "wikiops@users.noreply.github.com"


class CodedValueError(ValueError):
    """Validator failure tagged with a ``github_wiki`` error code.

    Pydantic wraps it in a ``ValidationError``; ``parse_settings`` turns the
    first tagged error back into a ``GithubWikiError``.
    """

    def __init__(
        self,
        code: str,
        summary: str,
        *,
        context: Mapping[str, object] | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(f"[{code}] {summary}")
        self.code = code
        self.summary = summary
        self.context = dict(context or {})
        self.hint = hint


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _no_control_characters(value: str, *, label: str) -> str:
    if has_control_characters(value):
        raise ValueError(f"{label} must not contain control characters")
    return value


# -- auth ------------------------------------------------------------------


class EnvAuth(_Strict):
    """Token read from an environment variable at every network command."""

    mode: Literal["env"]
    variable: str

    @field_validator("variable")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        if not _ENV_VARIABLE.fullmatch(value):
            raise ValueError("variable must be a valid environment variable name")
        return value


class GhAuth(_Strict):
    """Token requested from the GitHub CLI for one explicit account."""

    mode: Literal["gh"]
    account: str = Field(..., min_length=1)

    @field_validator("account")
    @classmethod
    def _valid_login(cls, value: str) -> str:
        if not _GITHUB_LOGIN.fullmatch(value):
            raise ValueError(
                "account must be a GitHub login: up to 39 letters, digits, '-' or '_', "
                "starting with a letter or digit"
            )
        return value


class SshAuth(_Strict):
    """SSH remote; ``key_path`` optionally pins one identity file."""

    mode: Literal["ssh"]
    key_path: str | None = Field(None, min_length=1)

    @field_validator("key_path")
    @classmethod
    def _no_control_characters_in_path(cls, value: str | None) -> str | None:
        if value is not None:
            _no_control_characters(value, label="key_path")
        return value


class AmbientAuth(_Strict):
    """No credentials added: git uses the user's own configuration."""

    mode: Literal["ambient"]


Auth = Annotated[
    EnvAuth | GhAuth | SshAuth | AmbientAuth, Field(discriminator="mode")
]


# -- commit identity -------------------------------------------------------


def _ident_name(value: str) -> str:
    _no_control_characters(value, label="name")
    if "<" in value or ">" in value:
        raise ValueError("name must not contain '<' or '>'")
    return value


def _non_blank_ident_name(value: str, *, mode: str, hint: str | None = None) -> str:
    if not value.strip():
        raise CodedValueError(
            "config.identity_incomplete",
            f"commit.identity mode '{mode}' needs a non-blank 'name'",
            hint=hint,
        )
    return _ident_name(value)


def _ident_email(value: str) -> str:
    if not _EMAIL.fullmatch(value):
        raise ValueError("email must look like 'user@host'")
    return value


class GitIdentity(_Strict):
    """Use the repository or global git ``user.name`` and ``user.email``."""

    mode: Literal["git"]


class BotIdentity(_Strict):
    """Commit as a bot; defaults to ``wikiops <wikiops@users.noreply.github.com>``."""

    mode: Literal["bot"]
    name: str = _BOT_NAME
    email: str = _BOT_EMAIL

    @field_validator("name")
    @classmethod
    def _non_blank_name(cls, value: str) -> str:
        return _non_blank_ident_name(
            value,
            mode="bot",
            hint="set a non-blank commit.identity.name, or omit it to use 'wikiops'",
        )

    _check_email = field_validator("email")(_ident_email)


class CustomIdentity(_Strict):
    """Commit with an explicit name and email."""

    mode: Literal["custom"]
    name: str = Field(..., min_length=1)
    email: str = Field(..., min_length=1)

    @field_validator("name")
    @classmethod
    def _non_blank_name(cls, value: str) -> str:
        return _non_blank_ident_name(value, mode="custom")

    _check_email = field_validator("email")(_ident_email)


Identity = Annotated[
    GitIdentity | BotIdentity | CustomIdentity, Field(discriminator="mode")
]


# -- value rules -----------------------------------------------------------


def _branch_problem(branch: str) -> str | None:
    """Return why ``branch`` is not a valid git branch name, or ``None``."""
    if not branch.strip():
        return "it is empty"
    if any(char.isspace() for char in branch):
        return "it contains whitespace"
    if has_control_characters(branch):
        return "it contains control characters"
    if branch.startswith("-"):
        return "it starts with '-'"
    if any(char in _BRANCH_FORBIDDEN_CHARACTERS for char in branch):
        return "it contains one of ~ ^ : ? * [ \\"
    if ".." in branch or "@{" in branch or branch == "@":
        return "it contains '..', '@{' or is '@'"
    if branch.startswith("/") or branch.endswith("/") or "//" in branch:
        return "it has an empty path component"
    if branch.endswith("."):
        return "it ends with '.'"
    for component in branch.split("/"):
        if component.startswith(".") or component.endswith(".lock"):
            return "a component starts with '.' or ends with '.lock'"
    return None


def _message_problem(message: str) -> str | None:
    """Return why ``message`` is not a valid commit template, or ``None``."""
    if not message.strip():
        return "it is empty"
    if "\x00" in message:
        return "it contains a NUL character"
    try:
        fields = list(string.Formatter().parse(message))
    except ValueError as exc:
        return f"it is malformed ({exc})"
    for _, field_name, format_spec, conversion in fields:
        if field_name is None:
            continue
        if field_name not in _MESSAGE_PLACEHOLDERS:
            return f"unknown placeholder '{{{field_name}}}'"
        if format_spec or conversion:
            return f"placeholder '{{{field_name}}}' does not take a format"
    return None


def check_repository(value: str) -> str:
    """Return ``value`` if it is an ``owner/repo`` name, else raise ``config.invalid_repository``."""
    name = value.rsplit("/", 1)[-1].lower()
    if (
        not _REPOSITORY.fullmatch(value)
        or name in {".", ".."}
        or name.endswith((".wiki", ".git"))
    ):
        raise CodedValueError(
            "config.invalid_repository",
            "repository must look like 'owner/repo'",
            context={"repository": value},
        )
    return value


def check_host(value: str) -> str:
    """Return ``value`` if it is a bare hostname, else raise ``config.invalid_host``."""
    if not _HOST.fullmatch(value):
        raise CodedValueError(
            "config.invalid_host",
            "host must be a bare hostname",
            context={"host": value},
        )
    return value


# -- aggregate models ------------------------------------------------------


class CommitSettings(_Strict):
    """How wikiops commits: identity and message template."""

    identity: Identity = Field(default_factory=lambda: GitIdentity(mode="git"))
    message: str = _DEFAULT_MESSAGE

    @field_validator("message")
    @classmethod
    def _valid_template(cls, value: str) -> str:
        problem = _message_problem(value)
        if problem is not None:
            raise CodedValueError(
                "config.invalid_message",
                f"commit.message is not a valid template: {problem}",
                context={"message": value},
            )
        return value


class LocalBackendSettings(BaseModel):
    """Local backend selection; extra keys are the backend's own options."""

    model_config = ConfigDict(extra="allow")

    type: str = Field("local_files", min_length=1)

    @model_validator(mode="after")
    def _no_recursion_and_no_injected_keys(self) -> "LocalBackendSettings":
        if self.type == _BACKEND_RECURSIVE_TYPE:
            raise CodedValueError(
                "config.backend_recursion",
                "local_backend.type cannot be 'github_wiki'",
            )
        forbidden = [key for key in _BACKEND_INJECTED_KEYS if key in (self.model_extra or {})]
        if forbidden:
            raise CodedValueError(
                "config.backend_root_forbidden",
                "local_backend must not set " + ", ".join(f"'{key}'" for key in forbidden),
                context={"keys": ", ".join(forbidden)},
            )
        return self


class GithubWikiProviderSettings(ProviderSettings):
    """Settings for the ``github_wiki`` provider; unknown keys are rejected."""

    model_config = ConfigDict(extra="forbid")

    repository: str
    host: str = "github.com"
    branch: str | None = None
    workdir: str | None = Field(None, min_length=1)
    sync_on_plan: bool = True
    auth: Auth = Field(default_factory=lambda: AmbientAuth(mode="ambient"))
    commit: CommitSettings = Field(default_factory=CommitSettings)
    allow_auto_commit: bool = True
    allow_auto_push: bool = False
    local_backend: LocalBackendSettings = Field(default_factory=LocalBackendSettings)
    git_timeout_seconds: int = Field(120, ge=5, le=3600)

    _check_repository = field_validator("repository")(check_repository)
    _check_host = field_validator("host")(check_host)

    @field_validator("branch")
    @classmethod
    def _valid_branch(cls, value: str | None) -> str | None:
        if value is not None:
            problem = _branch_problem(value)
            if problem is not None:
                raise CodedValueError(
                    "config.invalid_branch",
                    f"branch is not a valid git branch name: {problem}",
                    context={"branch": value},
                )
        return value

    @model_validator(mode="after")
    def _push_requires_commit(self) -> "GithubWikiProviderSettings":
        if self.allow_auto_push and not self.allow_auto_commit:
            raise CodedValueError(
                "config.push_requires_commit",
                "allow_auto_push needs allow_auto_commit",
            )
        return self


# -- pydantic errors -> one GithubWikiError --------------------------------

_HOST_INJECTED_KEYS = ("provider_name", "provider_api_version")


def _union_variants(info: FieldInfo) -> dict[str, type[BaseModel]]:
    """Map each discriminator tag to its model for a discriminated union field."""
    if get_origin(info.annotation) not in (Union, types.UnionType) or not info.discriminator:
        return {}
    variants: dict[str, type[BaseModel]] = {}
    for member in get_args(info.annotation):
        tag_field = member.model_fields[info.discriminator]
        variants[get_args(tag_field.annotation)[0]] = member
    return variants


def _locate(loc: tuple[Any, ...]) -> tuple[str, type[BaseModel] | None, FieldInfo | None]:
    """Resolve an error location into a display path, owning model and field.

    Union tags inside ``loc`` are dropped from the display path; the owning
    model is the one whose fields the last location element belongs to.
    """
    if not loc:
        return "settings", GithubWikiProviderSettings, None
    owner: type[BaseModel] | None = GithubWikiProviderSettings
    path: list[str] = []
    parents = loc[:-1]
    index = 0
    while index < len(parents) and owner is not None:
        key = parents[index]
        path.append(str(key))
        info = owner.model_fields.get(key)
        variants = _union_variants(info) if info is not None else {}
        if variants:
            tag = parents[index + 1] if index + 1 < len(parents) else None
            owner = variants.get(tag)
            index += 2
        else:
            nested = info.annotation if info is not None else None
            owner = nested if isinstance(nested, type) and issubclass(nested, BaseModel) else None
            index += 1
    path.extend(str(key) for key in parents[index:])
    path.append(str(loc[-1]))
    last = owner.model_fields.get(loc[-1]) if owner is not None else None
    return ".".join(path), owner, last


def _valid_keys(owner: type[BaseModel] | None) -> str:
    if owner is None:
        return "see the provider reference for the valid settings"
    skipped = _HOST_INJECTED_KEYS if owner is GithubWikiProviderSettings else ()
    return "valid keys here: " + ", ".join(key for key in owner.model_fields if key not in skipped)


def _tagged_error(error: Mapping[str, Any]) -> GithubWikiError | None:
    """Return the coded error carried by, or implied by, one pydantic error."""
    original = (error.get("ctx") or {}).get("error")
    if isinstance(original, CodedValueError):
        return GithubWikiError(
            original.code, original.summary, context=original.context, hint=original.hint
        )
    _, owner, _ = _locate(tuple(error["loc"]))
    if owner is CustomIdentity and error["type"] in {"missing", "string_too_short"}:
        return GithubWikiError(
            "config.identity_incomplete",
            f"commit.identity mode 'custom' needs a non-empty '{error['loc'][-1]}'",
        )
    return None


def _generic_error(error: Mapping[str, Any]) -> GithubWikiError:
    """Translate an untagged pydantic error into ``config.invalid``."""
    path, owner, _ = _locate(tuple(error["loc"]))
    kind = error["type"]
    if kind in {"union_tag_invalid", "union_tag_not_found"}:
        info = owner.model_fields.get(error["loc"][-1]) if owner is not None else None
        variants = _union_variants(info) if info is not None else {}
        discriminator = info.discriminator if info is not None else "mode"
        if kind == "union_tag_invalid":
            given = (error.get("ctx") or {}).get("tag")
            summary = f"Unknown {discriminator} '{given}' for setting '{path}'"
        else:
            summary = f"Setting '{path}' needs a '{discriminator}'"
        hint = f"set '{path}.{discriminator}' to one of: " + " | ".join(variants)
    else:
        if kind == "extra_forbidden":
            summary = f"Unknown setting '{path}'"
        elif kind == "missing":
            summary = f"Missing required setting '{path}'"
        else:
            message = str(error["msg"]).removeprefix("Value error, ")
            summary = f"Invalid value for setting '{path}': {message}"
        hint = _valid_keys(owner)
    return GithubWikiError("config.invalid", summary, hint=hint)


def parse_settings(raw: Any) -> GithubWikiProviderSettings:
    """Validate raw provider settings, failing with ONE coded ``GithubWikiError``.

    The first error carrying a code tag wins; otherwise the first error becomes
    ``config.invalid`` naming its location and the valid keys or union tags.
    """
    try:
        return GithubWikiProviderSettings.model_validate(raw)
    except ValidationError as exc:
        errors = exc.errors()
        for error in errors:
            tagged = _tagged_error(error)
            if tagged is not None:
                raise tagged from exc
        raise _generic_error(errors[0]) from exc
