# Getting Started

## Purpose

`wikiops` is the host application for documentation-as-code workflows in the WikiOps ecosystem.

If you are doing any of the following, this repository is the runtime you use:

- running documentation automation from a CLI
- loading and executing documentation plugins
- applying changes through document providers
- integrating profile-based configuration into documentation workflows

## What The Host Gives You

The host is organized around a small runtime surface:

- `wikiops.cli`
  - command-line interface
- `wikiops.core`
  - orchestration, configuration loading, reference resolution, diffing, and apply delegation
- `wikiops.providers`
  - built-in provider implementations and provider factories

## What The Host Does Not Do

The host intentionally avoids SDK-level contract ownership.

It does not define:

- public plugin protocols
- public provider protocols
- canonical domain models such as `ChangeSet` or `ExecutionContext`
- plugin-specific documentation business logic

Those belong to `wikiops-sdk` and external extension packages.

## Installation

Install the project and test dependencies with Poetry:

```bash
poetry install --with test
```

Run the CLI through Poetry:

```bash
poetry run wikiops --help
```

## Current Built-In Components

The current host ships with:

- the `wikiops` CLI
- the default documentation orchestrator
- the built-in `azure_devops_wiki` provider factory
- the built-in `local_files` provider factory (see [Local Files Provider](reference/local-files.md))
- the built-in `github_wiki` provider factory (see [GitHub Wiki Provider](reference/github-wiki.md))

The current host does not ship with built-in plugins. Plugins are expected to be installed separately and discovered through the `wikiops.plugins` entry point group.

## Minimal Execution Example

### Step 1: Inspect Available Extensions

```bash
poetry run wikiops plugins
poetry run wikiops providers
```

Use the plugin list to find the exact plugin ID you want to execute.

### Step 2: Create A Config File

```yaml
providers:
  azdo:
    type: azure_devops_wiki
    organization: acme
    project: engineering
    wiki: platform

profiles:
  default:
    provider: azdo
    refs:
      docs_root:
        provider: azdo
        kind: path
        locator:
          path: /Engineering/Teams
    plugins:
      acme.team-docs:
        parent_alias: docs_root
```

### Step 3: Create An Input File

```yaml
team_name: Platform
```

### Step 4: Run In Plan Mode

```bash
poetry run wikiops run \
  --config wikiops.yaml \
  --profile default \
  --plugin acme.team-docs \
  --input input.yaml
```

This produces:

- a normalized `ChangeSet`
- a preview diff

### Step 5: Run In Apply Mode

```bash
poetry run wikiops run \
  --config wikiops.yaml \
  --profile default \
  --plugin acme.team-docs \
  --input input.yaml \
  --apply
```

This also produces:

- an `ApplyResult`
- an exit code of `1` if any operation fails

## Important Note

The example above assumes that:

- an external plugin with ID `acme.team-docs` is installed
- `AZDO_PAT` or the configured PAT env var is available for the Azure DevOps provider

The built-in provider is present in this repository. Plugins are not.

## What To Read Next

- To understand the host structure, continue with [`architecture.md`](architecture.md).
- To understand configuration, continue with [`configuration.md`](configuration.md).
- To understand the execution lifecycle, continue with [`execution-flow.md`](execution-flow.md).
- To understand the host/SDK boundary, continue with [`sdk-relationship.md`](sdk-relationship.md).
