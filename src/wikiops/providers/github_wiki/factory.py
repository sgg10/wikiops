"""Entry-point factory of the ``github_wiki`` provider (GW-P1, GW-P14).

The factory declares no ``settings_model`` (design D2b): the host would validate raw
settings with a generic pydantic model and surface its raw messages, while the provider
reports every configuration problem as ONE coded ``[github_wiki:config.*]`` error. So
``create`` receives the raw settings and validates them itself with ``parse_settings``.

Creating a provider is offline and cheap: it validates, builds the credential strategy
(which never runs ``gh`` or reads a token until a command needs it) and wires the
production collaborators; no process starts and the network is not touched until the
provider is asked to read or write.

This module and ``backend`` are the only places that know the host's ``ProviderManager``.
"""

from __future__ import annotations

from typing import Any

from wikiops_sdk.contracts import ProviderSettings

from wikiops.core.provider_manager import ProviderManager
from wikiops.providers.github_wiki.auth import build_strategy
from wikiops.providers.github_wiki.backend import EntryPointBackendResolver
from wikiops.providers.github_wiki.ports import BackendResolver, GitRunner
from wikiops.providers.github_wiki.process import SubprocessGitRunner
from wikiops.providers.github_wiki.provider import PROVIDER_ID, GithubWikiProvider
from wikiops.providers.github_wiki.settings import GithubWikiProviderSettings, parse_settings


class GithubWikiProviderFactory:
    """Creates ``github_wiki`` providers from raw settings."""

    provider_id = PROVIDER_ID
    settings_model: type[ProviderSettings] | None = None

    def __init__(
        self, *, runner: GitRunner | None = None, backends: BackendResolver | None = None
    ) -> None:
        """``runner`` and ``backends`` replace the production wiring (tests inject fakes)."""
        self._runner = runner
        self._backends = backends

    def create(self, settings: GithubWikiProviderSettings | dict[str, Any]) -> GithubWikiProvider:
        parsed = (
            settings
            if isinstance(settings, GithubWikiProviderSettings)
            else parse_settings(settings)
        )
        runner = self._runner or SubprocessGitRunner()
        return GithubWikiProvider(
            parsed,
            runner=runner,
            credentials=build_strategy(parsed, runner=runner),
            backends=self._backends or EntryPointBackendResolver(ProviderManager()),
        )
