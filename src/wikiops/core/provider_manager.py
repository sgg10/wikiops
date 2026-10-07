from typing import Any, Dict, List, Optional, Protocol, Type, runtime_checkable
from importlib.metadata import entry_points

from wikiops.core.exceptions import ConfigurationError
from wikiops_sdk import PROVIDER_ENTRYPOINT_GROUP, ensure_provider_api_compatible
from wikiops_sdk.compat import CompatibilityError
from wikiops_sdk.contracts import DocumentProvider, ProviderSettings


class ProviderFactory(Protocol):
    """Host-local factory contract for provider entry points."""

    provider_id: str
    settings_model: Type[ProviderSettings]

    def create(self, settings: Any) -> DocumentProvider: ...


@runtime_checkable
class TargetDescribingProvider(Protocol):
    """Optional host-local contract for providers that can describe their target.

    Not part of the SDK ``DocumentProvider`` contract: the orchestrator surfaces
    the returned text as a ``provider_target`` note so users can see where a
    provider will read and write before applying anything.
    """

    def describe_target(self) -> str: ...


class ProviderManager:
    """Loads provider factories and creates provider instances."""

    ENTRYPOINT_GROUP = PROVIDER_ENTRYPOINT_GROUP

    def __init__(self) -> None:
        self._factories: Dict[str, ProviderFactory] = {}
        self._loaded = False

    @staticmethod
    def _validate_settings(factory: ProviderFactory, settings: Dict[str, Any]) -> Any:
        settings_model = getattr(factory, "settings_model", None)
        if settings_model is None:
            return settings
        if not isinstance(settings_model, type) or not issubclass(
            settings_model, ProviderSettings
        ):
            raise ConfigurationError(
                "Provider factory settings_model must inherit from ProviderSettings."
            )

        typed_settings = settings_model.model_validate(settings)
        ensure_provider_api_compatible(typed_settings.provider_api_version)
        return typed_settings

    @staticmethod
    def _ensure_provider_contract(provider: Any, entry_point_name: str) -> DocumentProvider:
        if not isinstance(provider, DocumentProvider):
            raise ConfigurationError(
                f"Provider entry point '{entry_point_name}' does not create a DocumentProvider."
            )
        return provider

    @staticmethod
    def _ensure_provider_compatibility(provider: DocumentProvider) -> None:
        provider_settings = getattr(provider, "settings", None)
        if provider_settings is None:
            raise ConfigurationError(
                "Provider factories must expose settings_model or create providers with a 'settings' model."
            )
        if not isinstance(provider_settings, ProviderSettings):
            raise ConfigurationError(
                "Provider settings must inherit from ProviderSettings for compatibility validation."
            )
        ensure_provider_api_compatible(provider_settings.provider_api_version)

    def load(self) -> None:
        """Loads provider factories from entry points."""
        if self._loaded:
            return

        for entry_point in entry_points(group=self.ENTRYPOINT_GROUP):
            try:
                factory_cls = entry_point.load()
                factory = factory_cls()
            except CompatibilityError:
                raise
            except Exception as e:
                raise ConfigurationError(
                    f"Failed to load provider factory from entry point '{entry_point.name}': {e}"
                )

            provider_id = getattr(factory, "provider_id", entry_point.name)
            if provider_id in self._factories:
                raise ConfigurationError(
                    f"Duplicate provider ID '{provider_id}' found while loading entry point '{entry_point.name}'."
                )
            self._factories[provider_id] = factory

        self._loaded = True

    def create(self, provider_type: str, settings: Dict) -> DocumentProvider:
        self.load()
        factory = self._factories.get(provider_type)
        if not factory:
            raise ConfigurationError(
                f"No provider factory found for type '{provider_type}'"
            )

        typed_settings = self._validate_settings(factory, settings)
        try:
            provider = factory.create(typed_settings)
        except CompatibilityError:
            raise
        except Exception as e:
            raise ConfigurationError(
                f"Failed to create provider of type '{provider_type}': {e}"
            )

        provider = self._ensure_provider_contract(provider, provider_type)
        if typed_settings is settings:
            self._ensure_provider_compatibility(provider)
        provider.validate_settings()
        return provider

    def settings_model_for(self, provider_type: str) -> Optional[Type[ProviderSettings]]:
        """Return the settings model a provider type declares, without creating it.

        Read-only: loads the factories (once) but never validates settings or
        instantiates a provider. ``None`` when the type is unknown, its factory
        declares no ``settings_model`` (it validates settings itself) or the
        declared model is not a ``ProviderSettings`` subclass (``create`` reports
        that factory defect).
        """
        self.load()
        factory = self._factories.get(provider_type)
        settings_model = getattr(factory, "settings_model", None)
        if isinstance(settings_model, type) and issubclass(settings_model, ProviderSettings):
            return settings_model
        return None

    def list_types(self) -> List[str]:
        """Returns a list of available provider types."""
        self.load()
        return sorted(self._factories.keys())
