from pathlib import Path
from uuid import uuid4
from typing import Dict, Tuple

from wikiops_sdk import ensure_python_compatible
from wikiops_sdk.domain import (
    ApplyResult,
    AssetPolicy,
    ChangeSet,
    CreateChildDocumentOperation,
    CreateDocumentOperation,
    ExecutionContext,
    LocalFileAssetSource,
    NoteMessage,
    PluginResourceAssetSource,
    PutAssetOperation,
    ProviderCapability,
    UpdateDocumentOperation,
    WarningMessage,
)

from wikiops.core.asset_source_resolver import AssetSourceResolver
from wikiops.core.asset_reference_rewriter import AssetReferenceRewriter
from wikiops.core.diff_engine import DiffEngine
from wikiops.core.apply_engine import ApplyEngine
from wikiops.core.plugin_manager import PluginManager
from wikiops.core.document_loader import DocumentLoader
from wikiops.core.provider_manager import ProviderManager, TargetDescribingProvider
from wikiops.core.reference_resolver import ReferenceResolver
from wikiops.core.config_loader import AppConfig, ConfigLoader
from wikiops.core.exceptions import ConfigurationError, ProviderCompatibilityError


class DefaultDocumentationOrchestrator:
    """Default implementation of the orchestration flow."""

    def __init__(self) -> None:
        ensure_python_compatible()
        self.config_loader = ConfigLoader()
        self.plugin_manager = PluginManager()
        self.provider_manager = ProviderManager()
        self.reference_resolver = ReferenceResolver()
        self.document_loader = DocumentLoader()
        self.diff_engine = DiffEngine()
        self.apply_engine = ApplyEngine()
        self.asset_source_resolver = AssetSourceResolver()
        self.asset_reference_rewriter = AssetReferenceRewriter()

    def _plan_internal(
        self,
        config: AppConfig,
        profile_name: str,
        plugin_id: str,
        raw_input: Dict,
        dry_run: bool = True,
        config_path: str | None = None,
        input_path: str | None = None,
    ) -> Tuple[AppConfig, ExecutionContext, ChangeSet]:
        profile = config.profiles.get(profile_name)
        if not profile:
            raise ConfigurationError(
                f"Profile '{profile_name}' not found in configuration."
            )

        provider_definition = config.providers.get(profile.provider)
        if not provider_definition:
            raise ConfigurationError(
                f"Provider '{profile.provider}' referenced by profile '{profile_name}' does not exist."
            )

        # Validate provider capabilities against plugin requirements
        provider = self.provider_manager.create(
            provider_definition.type, provider_definition.settings
        )

        plugin = self.plugin_manager.get(plugin_id)

        missing_capabilities = (
            plugin.manifest.required_capabilities - provider.capabilities()
        )
        if missing_capabilities:
            missing = ", ".join(
                sorted(capability.value for capability in missing_capabilities)
            )
            raise ProviderCompatibilityError(
                f"Provider '{provider.provider_id}' does not satisfy plugin '{plugin.manifest.plugin_id}'. "
                f"Missing capabilities: {missing}."
            )

        # Determine plugin config for the profile, falling back to empty dict if not defined
        profile_plugin_config = (
            profile.plugins.get(plugin.manifest.plugin_id)
            or profile.plugins.get(plugin_id)
            or {}
        )
        plugin_config_model = plugin.get_config_model().model_validate(
            profile_plugin_config
        )
        input_model = plugin.get_input_model().model_validate(raw_input)

        # Resolve references required by the plugin
        required_aliases = plugin.required_ref_aliases(plugin_config_model, input_model)
        resolved_refs = {
            alias: provider.resolve_ref(
                self.reference_resolver.resolve_alias(profile, alias)
            )
            for alias in required_aliases
        }

        # Load documents based on resolved references
        documents = self.document_loader.load(provider, resolved_refs)

        input_data = input_model.model_dump(exclude_unset=True)

        ctx = ExecutionContext(
            run_id=str(uuid4()),
            profile_name=profile_name,
            provider_name=profile.provider,
            dry_run=dry_run,
            refs=resolved_refs,
            documents=documents,
            plugin_config=plugin_config_model.model_dump(),
            input_data=input_data,
            runtime_vars=self._build_runtime_vars(
                plugin.manifest.plugin_id,
                provider.provider_id,
                config_path=config_path,
                input_path=input_path,
            ),
        )

        change_set = plugin.plan(ctx)
        planned_change_set = ChangeSet.model_validate(change_set.model_dump())
        self._validate_operation_capabilities(provider, planned_change_set)
        self._validate_asset_keys(planned_change_set)
        self._validate_asset_references(planned_change_set)
        self._validate_asset_sources(
            ctx,
            getattr(plugin, "resources", None),
            planned_change_set,
        )
        self._append_asset_policy_warnings(ctx, planned_change_set)
        self._prepend_provider_target_note(
            profile.provider, provider, planned_change_set
        )

        return config, ctx, planned_change_set

    def plan(
        self, profile_name: str, plugin_id: str, raw_input: Dict, dry_run: bool = True
    ) -> ChangeSet:
        raise NotImplementedError(
            "Planning without config file is not supported. Use plan_from_file instead."
        )

    def plan_from_file(
        self,
        config_path: str,
        profile_name: str,
        plugin_id: str,
        raw_input: Dict,
        dry_run: bool = True,
        input_path: str | None = None,
    ) -> Tuple[AppConfig, ExecutionContext, ChangeSet, str]:
        config = self.config_loader.load(config_path)
        plan = self._plan_internal(
            config,
            profile_name,
            plugin_id,
            raw_input,
            dry_run=dry_run,
            config_path=config_path,
            input_path=input_path,
        )
        diff = self.diff_engine.render(plan[1].documents, plan[2])
        return config, plan[1], plan[2], diff

    def apply(self, profile_name: str, plugin_id: str, raw_input: Dict) -> ApplyResult:
        raise NotImplementedError(
            "Apply without config file is not supported. Use apply_from_file instead."
        )

    def apply_from_file(
        self,
        config_path: str,
        profile_name: str,
        plugin_id: str,
        raw_input: Dict,
        input_path: str | None = None,
    ) -> Tuple[ChangeSet, ApplyResult, str]:
        config, ctx, change_set, diff = self.plan_from_file(
            config_path=config_path,
            profile_name=profile_name,
            plugin_id=plugin_id,
            raw_input=raw_input,
            dry_run=False,
            input_path=input_path,
        )

        profile = config.profiles[profile_name]
        plugin = self.plugin_manager.get(plugin_id)

        provider_definition = config.providers[profile.provider]
        provider = self.provider_manager.create(
            provider_definition.type, provider_definition.settings
        )

        apply_result = self.apply_engine.apply(
            provider,
            change_set,
            ctx,
            plugin.resources,
        )

        return change_set, apply_result, diff

    @staticmethod
    def _build_runtime_vars(
        plugin_id: str,
        provider_id: str,
        *,
        config_path: str | None,
        input_path: str | None,
    ) -> dict[str, str]:
        runtime_vars = {
            "plugin_id": plugin_id,
            "provider_id": provider_id,
        }
        if config_path:
            runtime_vars["config_dir"] = str(Path(config_path).resolve().parent)
        if input_path:
            runtime_vars["input_dir"] = str(Path(input_path).resolve().parent)
        return runtime_vars

    @staticmethod
    def _validate_operation_capabilities(provider, change_set: ChangeSet) -> None:
        if any(
            isinstance(operation, PutAssetOperation) for operation in change_set.operations
        ) and ProviderCapability.PUT_ASSET not in provider.capabilities():
            raise ProviderCompatibilityError(
                f"Provider '{provider.provider_id}' does not support asset uploads."
            )

    @staticmethod
    def _validate_asset_keys(change_set: ChangeSet) -> None:
        asset_keys = [
            operation.asset_key
            for operation in change_set.operations
            if isinstance(operation, PutAssetOperation)
        ]
        duplicates = sorted(
            {asset_key for asset_key in asset_keys if asset_keys.count(asset_key) > 1}
        )
        if duplicates:
            repeated = ", ".join(duplicates)
            raise ConfigurationError(
                f"Duplicate asset keys were planned in the change set: {repeated}."
            )

    def _validate_asset_references(self, change_set: ChangeSet) -> None:
        uploaded_asset_keys = {
            operation.asset_key
            for operation in change_set.operations
            if isinstance(operation, PutAssetOperation)
        }
        referenced_asset_keys: set[str] = set()
        for operation in change_set.operations:
            content = self._content_for_operation(operation)
            if content:
                referenced_asset_keys.update(
                    self.asset_reference_rewriter.collect_keys(content)
                )

        missing = sorted(referenced_asset_keys - uploaded_asset_keys)
        if missing:
            missing_keys = ", ".join(missing)
            raise ConfigurationError(
                f"Document content references asset keys that are not uploaded in the change set: {missing_keys}."
            )

    def _validate_asset_sources(self, ctx, resources, change_set: ChangeSet) -> None:
        asset_operations = [
            operation
            for operation in change_set.operations
            if isinstance(operation, PutAssetOperation)
        ]
        if not asset_operations:
            return
        if resources is None and any(
            isinstance(operation.source, PluginResourceAssetSource)
            for operation in asset_operations
        ):
            raise ConfigurationError(
                "Plugins that plan asset uploads must expose a resource provider."
            )
        for operation in asset_operations:
            self.asset_source_resolver.resolve(operation, ctx, resources)

    @staticmethod
    def _content_for_operation(operation) -> str | None:
        if isinstance(operation, UpdateDocumentOperation):
            return operation.new_content
        if isinstance(operation, CreateDocumentOperation):
            return operation.content
        if isinstance(operation, CreateChildDocumentOperation):
            return operation.child_content
        return None

    @staticmethod
    def _prepend_provider_target_note(
        provider_name: str, provider, change_set: ChangeSet
    ) -> None:
        if not isinstance(provider, TargetDescribingProvider):
            return

        change_set.notes.insert(
            0,
            NoteMessage(
                code="provider_target",
                message=(
                    f"Provider '{provider_name}' ({provider.provider_id}) "
                    f"target: {provider.describe_target()}"
                ),
            ),
        )

    @staticmethod
    def _append_asset_policy_warnings(
        ctx: ExecutionContext, change_set: ChangeSet
    ) -> None:
        if not any(
            isinstance(operation, PutAssetOperation)
            and isinstance(operation.source, LocalFileAssetSource)
            for operation in change_set.operations
        ):
            return

        policy = AssetPolicy.model_validate(ctx.plugin_config.get("asset_policy") or {})
        if not policy.deactivate_allowed_asset_roots:
            return

        change_set.warnings.append(
            WarningMessage(
                code="asset_policy_unrestricted_local_files",
                message=(
                    "Local asset root enforcement is disabled. This is not recommended "
                    "for production or agent-driven environments. Prefer trusted "
                    "allowed_asset_roots whenever possible."
                ),
                details={
                    "deactivate_allowed_asset_roots": True,
                    "allowed_asset_roots": policy.allowed_asset_roots,
                },
            )
        )
