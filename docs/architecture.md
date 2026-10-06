# Architecture

## WikiOps Ecosystem Overview

The WikiOps ecosystem is intentionally split across different concerns:

- `wikiops-sdk`
  - public contracts
  - portable domain models
  - compatibility rules
- `wikiops` host
  - orchestration
  - extension discovery and loading
  - configuration handling
  - execution lifecycle management
  - preview and apply workflows
- plugins
  - documentation business logic
- providers
  - infrastructure adapters for concrete wiki systems

The design goal is to keep the SDK stable and reusable while allowing the host to evolve execution policy independently.

## Responsibility Boundaries

### The SDK

The SDK defines the common language only.

Examples:

- what a `DocumentRef` looks like
- what a `ChangeSet` contains
- how a plugin declares required capabilities
- how a provider reports apply results
- how extension compatibility is checked

### Plugins

Plugins own documentation planning logic.

Examples:

- reading validated input and plugin config
- deciding what content should exist
- deciding whether to create, update, or create child pages
- emitting warnings and notes during planning

Plugins do not persist content directly. They produce a `ChangeSet`.

### Providers

Providers own infrastructure behavior.

Examples:

- resolving document references for a concrete backend
- reading current document content
- checking whether a document exists
- building user-facing links
- applying `ChangeSet` operations and returning results

Providers do not decide what should change. They execute requested changes.

### The Host

The host is responsible for orchestration and policy.

Examples:

- reading YAML configuration
- discovering plugins and providers through entry points
- validating SDK and runtime compatibility
- validating plugin config and input payloads
- building `ExecutionContext`
- deciding how plan mode and apply mode are exposed through the CLI
- rendering previews and surfacing failures

## Intended Dependency Direction

```text
plugin -> sdk <- host
provider -> sdk <- host
```

This means:

- plugins depend on the SDK, not on host internals
- providers depend on the SDK, not on plugin internals
- the host depends on the SDK to speak to both plugins and providers

## Host Runtime Structure

The current host repository is organized around these runtime modules:

```text
src/wikiops/
  cli/
    app.py
  core/
    apply_engine.py
    config_loader.py
    diff_engine.py
    document_loader.py
    exceptions.py
    orchestrator.py
    plugin_manager.py
    plugin_resources.py
    provider_manager.py
    reference_resolver.py
  providers/
    _fs.py
    azure_devops/
      provider.py
    local_files/
      _layout.py
      provider.py
```

`providers/_fs.py` holds the provider-agnostic filesystem safety helpers used by `local_files`; it is internal and not an extension API.

## High-Level Execution Model

At a high level, the host executes this lifecycle:

```text
load config -> load provider -> load plugin -> validate compatibility
-> resolve refs -> load documents -> build execution context
-> plan changes -> render diff -> optionally apply changes
```

That flow is implemented primarily by `wikiops.core.orchestrator.DefaultDocumentationOrchestrator`.

## Design Principles

### Centralized Orchestration

The orchestrator belongs to the host so that plugin logic remains portable and provider logic remains infrastructure-focused.

### Provider-Agnostic Planning

Plugins emit SDK operations and `ChangeSet` objects rather than making provider-specific write calls.

### Runtime Validation At The Host Layer

The host validates:

- Python runtime compatibility
- plugin API compatibility
- provider API compatibility
- provider capabilities against plugin requirements

### Thin Execution Helpers

Several core modules are intentionally narrow:

- `DocumentLoader` delegates provider reads
- `ApplyEngine` delegates provider apply behavior
- `ReferenceResolver` resolves aliases from profiles
- `DiffEngine` focuses on preview generation

This keeps the execution flow easy to reason about and easy to test.

## Current Implementation Boundaries

The current host is intentionally focused:

- it ships two built-in providers, Azure DevOps Wiki and local Markdown files
- it does not ship built-in plugins
- configuration is YAML-based
- preview diffs are strongest for update operations
- dry-run policy is controlled at the host level rather than inside the SDK

## Related Documentation

- [`execution-flow.md`](execution-flow.md)
- [`configuration.md`](configuration.md)
- [`sdk-relationship.md`](sdk-relationship.md)
- [`reference/core-modules.md`](reference/core-modules.md)
- [`wikiops-sdk architecture`](https://github.com/sgg10/wikiops-sdk/blob/main/docs/architecture.md)
