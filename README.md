# wikiops

`wikiops` is the host application and CLI runtime for the WikiOps ecosystem.

It orchestrates documentation automation workflows around `wikiops-sdk` by loading configuration, discovering plugins and providers, building execution context, rendering previews, and applying planned changes.

## What This Repository Contains

- A command-line interface for WikiOps execution workflows.
- The host orchestrator that coordinates planning and apply flows.
- Runtime loading for plugins and providers through Python entry points.
- YAML configuration loading for providers, profiles, refs, and plugin config.
- Document loading, diff rendering, and apply delegation.
- Built-in providers for Azure DevOps Wiki, for local Markdown files, and for GitHub wikis.
- A strict `pytest` suite with coverage enforcement.

## What This Repository Does Not Contain

- The public extension contracts and shared domain models.
- A stable SDK-level API surface for plugins and providers.
- Built-in documentation plugins.
- Project-specific documentation business logic.

Those concerns belong to `wikiops-sdk` and external plugin repositories.

## Relationship To `wikiops-sdk`

The WikiOps ecosystem is intentionally split across repositories:

```text
plugin -> sdk <- host
provider -> sdk <- host
```

- `wikiops-sdk`
  - defines the shared contracts, domain models, and compatibility helpers
- `wikiops`
  - orchestrates execution around those SDK contracts
- plugins
  - implement documentation planning logic
- providers
  - implement persistence and read-side infrastructure

Canonical SDK documentation lives in the separate SDK repository:

- [`wikiops-sdk README`](https://github.com/sgg10/wikiops-sdk/blob/main/README.md)
- [`wikiops-sdk architecture`](https://github.com/sgg10/wikiops-sdk/blob/main/docs/architecture.md)
- [`wikiops-sdk contracts API`](https://github.com/sgg10/wikiops-sdk/blob/main/docs/api/contracts.md)
- [`wikiops-sdk domain API`](https://github.com/sgg10/wikiops-sdk/blob/main/docs/api/domain.md)

## Quick Start

Install the host and its runtime dependencies:

```bash
poetry install --with test
```

Inspect the currently available extensions:

```bash
poetry run wikiops plugins
poetry run wikiops providers
```

The host currently ships with these built-in provider implementations:

- `azure_devops_wiki`
- `local_files`
- `github_wiki`

Plugins are expected to be installed separately through Python packages that expose the `wikiops.plugins` entry point group.

### Example Configuration

The host reads YAML configuration with provider definitions, profiles, refs, and plugin-specific settings.

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

To write documents as Markdown files in a local directory instead, use the `local_files` provider. Ref paths are relative to `root` and end in `.md`:

```yaml
providers:
  local:
    type: local_files
    root: ./docs

profiles:
  default:
    provider: local
    refs:
      docs_root:
        provider: local
        kind: path
        locator:
          path: README.md
```

A relative `root` resolves against the current working directory. Plans include a `provider_target` note with the resolved root; read it before applying. See [`docs/reference/local-files.md`](docs/reference/local-files.md).

To publish pages to the wiki of a GitHub repository, use the `github_wiki` provider. It works through a local git clone, commits what a run wrote, and pushes only when you enable it. Pages are flat (`Home.md`, never `guides/setup.md`):

```yaml
providers:
  wiki:
    type: github_wiki
    repository: acme/platform
    auth:
      mode: env
      variable: GITHUB_TOKEN
    allow_auto_push: false

profiles:
  default:
    provider: wiki
    refs:
      home:
        provider: wiki
        kind: path
        locator:
          path: Home.md
```

The wiki needs its first page created in the web UI. Plans include a `provider_target` note with the remote and the clone directory; read it before applying. See [`docs/reference/github-wiki.md`](docs/reference/github-wiki.md).

### Example Input

```yaml
team_name: Platform
```

### Plan A Run

Replace `acme.team-docs` with an installed plugin ID shown by `wikiops plugins`.

```bash
poetry run wikiops run \
  --config wikiops.yaml \
  --profile default \
  --plugin acme.team-docs \
  --input input.yaml
```

This prints:

- the planned `ChangeSet` as JSON
- a preview diff

### Apply A Run

```bash
poetry run wikiops run \
  --config wikiops.yaml \
  --profile default \
  --plugin acme.team-docs \
  --input input.yaml \
  --apply
```

On apply, the CLI also prints the provider `ApplyResult` and exits with code `1` if any operation failed.

## Plugin Resources

`wikiops` injects a `PluginResourceProvider` when loading plugin entry points.

- Resource paths are relative to the plugin package root.
- The host does not assume a fixed `resources/` directory.
- If a plugin stores assets under `resources/`, it should request them as `resources/...`.
- If a plugin already provides its own `resources` object, the host leaves it untouched.

## Documentation Map

Start here depending on your role:

- New to the host: [`docs/getting-started.md`](docs/getting-started.md)
- Need the host architecture: [`docs/architecture.md`](docs/architecture.md)
- Need the runtime execution lifecycle: [`docs/execution-flow.md`](docs/execution-flow.md)
- Need the YAML configuration model: [`docs/configuration.md`](docs/configuration.md)
- Need the host/SDK boundary: [`docs/sdk-relationship.md`](docs/sdk-relationship.md)
- Using the CLI: [`docs/guides/using-the-cli.md`](docs/guides/using-the-cli.md)
- Building a plugin for this host: [`docs/guides/build-a-plugin.md`](docs/guides/build-a-plugin.md)
- Building a provider for this host: [`docs/guides/build-a-provider.md`](docs/guides/build-a-provider.md)
- Need module-level runtime reference: [`docs/reference/core-modules.md`](docs/reference/core-modules.md)
- Need the built-in Azure DevOps provider reference: [`docs/reference/azure-devops-wiki.md`](docs/reference/azure-devops-wiki.md)
- Need the built-in local files provider reference: [`docs/reference/local-files.md`](docs/reference/local-files.md)
- Need the built-in GitHub wiki provider reference: [`docs/reference/github-wiki.md`](docs/reference/github-wiki.md)
- Need host internal boundaries: [`docs/reference/internal-boundaries.md`](docs/reference/internal-boundaries.md)

The full host documentation index lives at [`docs/index.md`](docs/index.md).

## Tests

This repository ships with a strict `pytest` suite and coverage threshold.

```bash
poetry run pytest
```

The tests are also useful as executable examples of the current host behavior.
