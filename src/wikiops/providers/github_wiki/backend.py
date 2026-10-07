"""Composition of the inner local backend of ``github_wiki`` (design D4, GW-P7, GW-LB1).

The provider owns wiki policy and git; reading and writing the clone's files is
delegated to a ``DocumentProvider`` rooted at the workdir, chosen by
``local_backend.type`` (default ``local_files``). ``EntryPointBackendResolver``
creates it through the host's ``ProviderManager`` so any registered provider
that accepts a ``root`` setting can serve, without this package importing it.

Selection is validated offline (registered type, settings model with a ``root``
field, options that validate) without touching the filesystem or creating a
provider. Creating injects ``root`` and ``provider_name`` and checks that the
backend supports every capability the wiki needs.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from wikiops_sdk.contracts import DocumentProvider, ProviderSettings
from wikiops_sdk.domain import ProviderCapability

from wikiops.core.provider_manager import ProviderManager
from wikiops.providers.github_wiki.errors import GithubWikiError
from wikiops.providers.github_wiki.redaction import Redactor
from wikiops.providers.github_wiki.settings import LocalBackendSettings

# What github_wiki needs from its backend, in the order missing ones are listed.
REQUIRED_BACKEND_CAPS: tuple[ProviderCapability, ...] = (
    ProviderCapability.READ_DOCUMENT,
    ProviderCapability.CHECK_EXISTS,
    ProviderCapability.CREATE_DOCUMENT,
    ProviderCapability.UPDATE_DOCUMENT,
    ProviderCapability.PUT_ASSET,
    ProviderCapability.RESOLVE_BY_PATH,
)
_ROOT_FIELD = "root"
# Stand-ins for the injected settings while validating options offline.
_PLACEHOLDER_ROOT = "wikiops-offline-check-root"
_PLACEHOLDER_PROVIDER_NAME = "wikiops-offline-check"
_REDACTOR = Redactor()


def _options(backend: LocalBackendSettings) -> dict[str, Any]:
    """The backend's own options: everything but the ``type`` selector."""
    return dict(backend.model_extra or {})


def _validation_summary(exc: ValidationError) -> str:
    """One line naming each invalid setting and why (never the rejected values)."""
    problems = [
        f"{'.'.join(str(part) for part in error['loc']) or 'settings'}: {error['msg']}"
        for error in exc.errors()
    ]
    return "; ".join(problems)


def _invalid(backend_type: str, summary: str, *, hint: str | None = None) -> GithubWikiError:
    return GithubWikiError(
        "config.backend_invalid",
        _REDACTOR.redact(summary),
        context={"type": backend_type},
        hint=hint,
    )


class EntryPointBackendResolver:
    """``BackendResolver`` over the host's registered providers."""

    def __init__(self, manager: ProviderManager) -> None:
        self._manager = manager

    def check_offline(self, backend: LocalBackendSettings) -> None:
        """Validate the selection and options without writing or creating anything."""
        model = self._settings_model(backend.type)
        placeholders: Mapping[str, Any] = {
            **_options(backend),
            _ROOT_FIELD: _PLACEHOLDER_ROOT,
            "provider_name": _PLACEHOLDER_PROVIDER_NAME,
        }
        try:
            model.model_validate(placeholders)
        except ValidationError as exc:
            raise _invalid(
                backend.type, f"The options of backend '{backend.type}' are invalid: {_validation_summary(exc)}"
            ) from exc

    def create(
        self, backend: LocalBackendSettings, *, root: Path, provider_name: str
    ) -> DocumentProvider:
        """Create the backend rooted at ``root`` and check it can serve a wiki."""
        self._settings_model(backend.type)
        settings = {**_options(backend), _ROOT_FIELD: str(root), "provider_name": provider_name}
        try:
            provider = self._manager.create(backend.type, settings)
        except ValidationError as exc:
            raise _invalid(
                backend.type, f"The options of backend '{backend.type}' are invalid: {_validation_summary(exc)}"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - any backend failure is one coded error
            raise _invalid(
                backend.type, f"Backend '{backend.type}' could not be created: {exc}"
            ) from exc
        missing = [cap for cap in REQUIRED_BACKEND_CAPS if cap not in provider.capabilities()]
        if missing:
            raise GithubWikiError(
                "config.backend_incompatible",
                f"Backend '{backend.type}' lacks capabilities the wiki needs",
                context={"type": backend.type, "missing": ", ".join(cap.name for cap in missing)},
            )
        return provider

    def _settings_model(self, backend_type: str) -> type[ProviderSettings]:
        """The registered backend's settings model, which must accept ``root`` (GW-LB1)."""
        registered = self._manager.list_types()
        if backend_type not in registered:
            raise GithubWikiError(
                "config.backend_unknown",
                f"local_backend.type '{backend_type}' is not a registered provider",
                context={"type": backend_type},
                hint="set local_backend.type to one of: " + ", ".join(registered),
            )
        model = self._manager.settings_model_for(backend_type)
        if model is None or _ROOT_FIELD not in model.model_fields:
            raise _invalid(
                backend_type,
                f"Backend '{backend_type}' cannot be used: its settings have no '{_ROOT_FIELD}' field",
                hint=f"choose a backend whose settings accept a '{_ROOT_FIELD}' directory",
            )
        return model
