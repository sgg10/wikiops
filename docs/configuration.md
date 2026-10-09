# Configuration

This page documents the current YAML configuration model used by the host.

## Purpose

The host uses YAML configuration to describe:

- which provider instances exist
- which execution profiles exist
- which document references are available in each profile
- which plugin configuration payloads should be applied for a profile

The current configuration model is implemented by `wikiops.core.config_loader.ConfigLoader`.

## Top-Level Sections

The host currently recognizes these top-level sections:

- `providers`
- `profiles`

## Providers

Each provider entry describes one configured provider instance.

Example:

```yaml
providers:
  azdo:
    type: azure_devops_wiki
    organization: acme
    project: engineering
    wiki: platform
    pat_token_env: AZDO_PAT
```

### Required Fields

- `type`

The value of `type` is the provider implementation ID resolved through `ProviderManager`.

### Settings Normalization

All provider fields except `type` are normalized into the provider settings payload.

The host also injects:

- `provider_name`

using the key of the provider definition itself.

For the example above, the effective provider settings include:

```yaml
provider_name: azdo
organization: acme
project: engineering
wiki: platform
pat_token_env: AZDO_PAT
```

### Built-In Provider Types

The host currently ships three provider implementations. Run `wikiops providers` to confirm which types are discoverable in the current environment.

| `type` | Stores documents in | Reference |
| --- | --- | --- |
| `azure_devops_wiki` | an Azure DevOps Wiki | [`reference/azure-devops-wiki.md`](reference/azure-devops-wiki.md) |
| `local_files` | Markdown files below a local root directory | [`reference/local-files.md`](reference/local-files.md) |
| `github_wiki` | the wiki of a GitHub repository, through a local git clone | [`reference/github-wiki.md`](reference/github-wiki.md) |

### Local Files Example

```yaml
providers:
  local:
    type: local_files
    root: ./docs
    assets_dir: assets
    overwrite_existing: false

profiles:
  default:
    provider: local
    refs:
      guide:
        provider: local
        kind: path
        locator:
          path: guide/README.md
```

`local_files` settings:

- `root` (required): directory that holds every document; `~` is expanded and a relative value resolves against the working directory of the process, not against the config file
- `assets_dir` (optional, default `assets`): root-relative directory for stored assets
- `overwrite_existing` (optional, default `false`): allow create operations to replace an existing file with different content

Unknown keys are rejected. Ref paths for this provider are relative to `root`, use `/` separators, have no leading `/`, and end in `.md`; this differs from Azure DevOps paths such as `/Engineering/Teams`. The host prints a `provider_target` note on every plan showing the resolved root and working directory; check it before applying. See [`reference/local-files.md`](reference/local-files.md) for the full behavior.

### GitHub Wiki Example

```yaml
providers:
  wiki:
    type: github_wiki
    repository: acme/platform
    auth:
      mode: env
      variable: GITHUB_TOKEN
    commit:
      identity:
        mode: bot
    allow_auto_commit: true
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

`github_wiki` settings:

- `repository` (required): `owner/repo` of the repository that owns the wiki
- `host` (optional, default `github.com`): bare hostname, for GitHub Enterprise Server
- `branch` (optional): defaults to the remote's default branch
- `workdir` (optional): local clone directory; a relative value resolves against the working directory of the process, and the default is a per-profile directory in the user cache
- `sync_on_plan` (optional, default `true`): fetch on plan; `false` reads the existing clone offline
- `auth` (optional, default `ambient`): `mode` is `env` (`variable`), `gh` (`account`), `ssh` (optional `key_path`), or `ambient`; a token is never written in the config
- `commit` (optional): `identity` (`git`, `bot` or `custom`) and `message` template with `{plugin_id}`, `{provider_name}`, `{page_count}`
- `allow_auto_commit` (optional, default `true`) and `allow_auto_push` (optional, default `false`; requires auto commit)
- `generate_sidebar` (optional, default `false`): maintain a managed `_Sidebar.md` that links every root page; an existing sidebar without the managed marker is never overwritten (see [`reference/github-wiki.md`](reference/github-wiki.md#managed-sidebar))
- `local_backend` (optional, default `{type: local_files}`): backend that writes the files into the clone
- `git_timeout_seconds` (optional, default `120`)

Unknown keys are rejected. Ref paths are flat root pages that end in `.md` (`Home.md`); a path with `/` fails with `path.nested_not_supported`. The host prints a `provider_target` note on every plan showing the remote and the clone directory; check it before applying. See [`reference/github-wiki.md`](reference/github-wiki.md) for the full behavior, including error codes.

## Profiles

Each profile defines one execution context selection for a run.

Example:

```yaml
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

### Required Fields

- `provider`

This value must match a key defined under `providers`.

### Optional Fields

- `refs`
- `plugins`

## Refs

`refs` is a mapping of logical aliases to SDK `DocumentRef` objects.

Example:

```yaml
refs:
  docs_root:
    provider: azdo
    kind: path
    locator:
      path: /Engineering/Teams
```

The host validates each ref using the SDK domain model, which means refs must match the `DocumentRef` shape expected by `wikiops-sdk`.

## Plugin Configuration

`plugins` is a mapping of plugin identifiers to plugin-specific config payloads.

Example:

```yaml
plugins:
  acme.team-docs:
    parent_alias: docs_root
    section_title: Platform
```

During execution, the host resolves plugin config in this order:

1. `profile.plugins[plugin.manifest.plugin_id]`
2. `profile.plugins[plugin_id_from_command]`
3. `{}`

### Recommended Practice

Use the manifest plugin ID as the configuration key.

The host still supports lookup by the requested plugin ID, but the manifest ID is the more stable convention.

## Complete Example

```yaml
providers:
  azdo:
    type: azure_devops_wiki
    organization: acme
    project: engineering
    wiki: platform
    timeout_seconds: 30

profiles:
  default:
    provider: azdo
    refs:
      docs_root:
        provider: azdo
        kind: path
        locator:
          path: /Engineering/Teams
      inventory_page:
        provider: azdo
        kind: path
        locator:
          path: /Engineering/Inventory
    plugins:
      acme.team-docs:
        parent_alias: docs_root
      acme.inventory-sync:
        inventory_ref_alias: inventory_page
```

## Validation Rules

The host currently validates these cases explicitly:

- configuration path must exist
- configuration path must be a file
- provider definitions must include `type`
- profiles must include `provider`
- refs must validate as SDK `DocumentRef` objects

## Host Conventions

The current host uses these conventions:

- the provider config key is the runtime `provider_name`
- `providers.<name>.type` selects the provider implementation
- profile refs are declared upfront and plugins ask for a subset through alias requirements
- plugin config is profile-scoped rather than global

## Related Documentation

- [`getting-started.md`](getting-started.md)
- [`execution-flow.md`](execution-flow.md)
- [`guides/using-the-cli.md`](guides/using-the-cli.md)
- [`reference/core-modules.md`](reference/core-modules.md)
