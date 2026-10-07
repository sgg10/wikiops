# Configuration Reference

This file explains the host YAML configuration model.

## Top-level sections

The host recognizes these top-level sections:

- `providers`
- `profiles`

## `providers`

Each entry defines one configured provider instance.

Example:

```yaml
providers:
  azdo:
    type: azure_devops_wiki
    organization: GrupoBancolombia
    project: Nequi
    wiki: Nequi.wiki
    pat_token_env: AZDO_PAT
    api_version: "7.1"
    timeout_seconds: 30
```

Rules:

- `type` is required
- the provider key, such as `azdo`, becomes the runtime `provider_name`
- all fields except `type` become provider settings

Provider types are discovered at runtime; confirm them with `wikiops providers`. The built-in types are `azure_devops_wiki`, `local_files` and `github_wiki`.

Example for the built-in `local_files` provider:

```yaml
providers:
  local:
    type: local_files
    root: ./docs
    assets_dir: assets
    overwrite_existing: false
```

Rules for `local_files`:

- `root` is required, must already exist, and a relative value resolves against the working directory of the command, not the config file
- ref paths for this provider are relative to `root`, have no leading `/`, and end in `.md` (for example `guide/README.md`)
- unknown settings keys are rejected
- see [Local files provider reference](local-files-provider.md) before configuring or debugging it

Example for the built-in `github_wiki` provider:

```yaml
providers:
  wiki:
    type: github_wiki
    repository: acme/platform
    auth:
      mode: env
      variable: GITHUB_TOKEN
    allow_auto_commit: true
    allow_auto_push: false
```

Rules for `github_wiki`:

- `repository` (`owner/repo`) is required; `auth.mode` is one of `env`, `gh`, `ssh`, `ambient` (default) and `env` takes the variable name, never the token
- the provider works through a local clone: a relative `workdir` resolves against the working directory of the command, and the default is a per-profile directory in the user cache
- ref paths are flat root pages that end in `.md` (for example `Home.md`); a path with `/` is rejected
- `allow_auto_commit` defaults to `true` and `allow_auto_push` to `false`, so a run commits locally but does not publish
- unknown settings keys are rejected
- see [GitHub wiki provider reference](github-wiki-provider.md) before configuring or debugging it

## `profiles`

Each profile selects one provider and contains refs plus profile-scoped plugin config.

Example:

```yaml
profiles:
  test:
    provider: azdo
    refs:
      docs_root:
        provider: azdo
        kind: path
        locator:
          path: /Engineering/Teams
    plugins:
      nequi.datamind:
        default_tribe_parent_alias: docs_root
```

Rules:

- `provider` is required
- the value must match a key under `providers`
- `refs` and `plugins` are optional but common

## `refs`

`refs` is a mapping of logical aliases to SDK `DocumentRef` objects.

Example:

```yaml
refs:
  sample_dp:
    provider: azdo
    kind: path
    locator:
      path: /Tribus/Data/sample-dp
```

Use refs when:

- the same page will be read or updated repeatedly
- a plugin expects aliases through `required_ref_aliases(...)`
- you want `wikiops docs get --alias ...`

## `plugins`

`plugins` is a mapping of plugin identifiers to plugin-specific config payloads.

Example:

```yaml
plugins:
  nequi.datamind:
    tribe_template_path: resources/templates/datamind-new-tribe-page.jinja.md
    dp_template_path: resources/templates/datamind-new-dp-page.jinja.md
    inventory_block_name: inventory
```

Rules:

- plugin config is profile-scoped, not global
- prefer the manifest plugin ID as the config key
- do not guess the plugin config schema; use the plugin skill or plugin docs

## Complete examples using the built-in providers

See:

- [Azure DevOps config example](../assets/config.azure-devops.example.yaml)
- [Local files config example](../assets/config.local-files.example.yaml)
- [GitHub wiki config example](../assets/config.github-wiki.example.yaml)

## Practical guidance for agents

When the user asks to "configure wikiops":

1. Identify the desired provider
2. Create or update a provider entry under `providers`
3. Create or update a profile under `profiles`
4. Add only the refs needed by the workflow
5. Add plugin config only if the workflow is plugin-driven

If the user gives natural-language requirements rather than YAML, translate them into this structure.
