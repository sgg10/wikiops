# WikiOps Documentation

This documentation describes the current implementation of `wikiops`, the host application of the WikiOps ecosystem.

It is written for:

- users running the CLI
- plugin authors integrating with the host
- provider authors integrating with the host
- maintainers evolving the runtime
- AI agents that need explicit repository boundaries and execution semantics

## What The Host Is

`wikiops` is the orchestration and runtime layer of the ecosystem.

It is responsible for:

- loading configuration
- discovering plugins and providers
- validating compatibility and runtime constraints
- resolving document references
- loading source documents
- building `ExecutionContext`
- invoking plugins to produce `ChangeSet` plans
- rendering previews
- applying changes through providers

## What The Host Is Not

`wikiops` is not the SDK contract layer.

It does not define the canonical public models for:

- `ChangeSet`
- `ExecutionContext`
- `DocumentRef`
- `DocumentationPlugin`
- `DocumentProvider`

Those belong to `wikiops-sdk`.

It also does not contain built-in documentation plugins. Plugin business logic is expected to live in separate packages.

## Documentation Structure

### Conceptual Documentation

- [`getting-started.md`](getting-started.md): project purpose, quickstart, and reading strategy
- [`architecture.md`](architecture.md): host responsibilities, ecosystem boundaries, and runtime structure
- [`execution-flow.md`](execution-flow.md): how plan and apply flows are executed
- [`configuration.md`](configuration.md): YAML configuration model and validation rules
- [`sdk-relationship.md`](sdk-relationship.md): host versus SDK boundaries and integration model

### Guides

- [`guides/using-the-cli.md`](guides/using-the-cli.md): command-line usage and output behavior
- [`guides/build-a-plugin.md`](guides/build-a-plugin.md): host-side expectations for plugin packages
- [`guides/build-a-provider.md`](guides/build-a-provider.md): host-side expectations for provider packages

### Reference

- [`reference/core-modules.md`](reference/core-modules.md): module-level map of the runtime
- [`reference/azure-devops-wiki.md`](reference/azure-devops-wiki.md): built-in Azure DevOps Wiki provider reference
- [`reference/local-files.md`](reference/local-files.md): built-in local Markdown files provider reference
- [`reference/internal-boundaries.md`](reference/internal-boundaries.md): public behavior versus internal implementation details

## Reading Paths

### If You Want To Use The CLI

1. [`getting-started.md`](getting-started.md)
2. [`configuration.md`](configuration.md)
3. [`guides/using-the-cli.md`](guides/using-the-cli.md)
4. [`execution-flow.md`](execution-flow.md)

### If You Are Writing A Plugin

1. [`architecture.md`](architecture.md)
2. [`sdk-relationship.md`](sdk-relationship.md)
3. [`guides/build-a-plugin.md`](guides/build-a-plugin.md)
4. [`reference/core-modules.md`](reference/core-modules.md)
5. [`wikiops-sdk plugin guide`](https://github.com/sgg10/wikiops-sdk/blob/main/docs/guides/write-a-plugin.md)

### If You Are Writing A Provider

1. [`architecture.md`](architecture.md)
2. [`sdk-relationship.md`](sdk-relationship.md)
3. [`guides/build-a-provider.md`](guides/build-a-provider.md)
4. [`reference/azure-devops-wiki.md`](reference/azure-devops-wiki.md)
5. [`reference/local-files.md`](reference/local-files.md)
6. [`wikiops-sdk provider guide`](https://github.com/sgg10/wikiops-sdk/blob/main/docs/guides/write-a-provider.md)

### If You Maintain The Host

1. [`architecture.md`](architecture.md)
2. [`execution-flow.md`](execution-flow.md)
3. [`reference/core-modules.md`](reference/core-modules.md)
4. [`reference/internal-boundaries.md`](reference/internal-boundaries.md)
5. [`wikiops-sdk architecture`](https://github.com/sgg10/wikiops-sdk/blob/main/docs/architecture.md)

## Documentation Scope

This documentation is implementation-focused.

It explains:

- how the current host works
- how it uses `wikiops-sdk`
- which runtime behaviors are stable enough to rely on
- which behaviors are host conventions rather than SDK guarantees

Where the SDK already defines the canonical contract, these docs link to the SDK rather than duplicating its full API reference.
