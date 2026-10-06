# Using The CLI

This guide documents the current `wikiops` command-line interface.

## Command Surface

The host currently exposes four command entries:

- `wikiops plugins`
- `wikiops providers`
- `wikiops docs get`
- `wikiops run`

## `wikiops plugins`

Lists discovered plugin instances.

Current output format:

```text
- <plugin_id> :: <display_name> (<version>)
```

Example:

```bash
poetry run wikiops plugins
```

Use this command to confirm that an external plugin package is installed and discoverable before trying to execute it.

## `wikiops providers`

Lists available provider implementation IDs.

Example:

```bash
poetry run wikiops providers
```

Use this command to confirm that the built-in provider or any external provider package is discoverable.

## `wikiops docs get`

Fetches the current state of a single document through the provider configured for a profile.

Required options:

- `--config`, `-c`
- `--profile`, `-p`
- exactly one of `--alias` or `--path`

Optional flags:

- `--output` with `json` or `markdown`

### Select By Alias

Use `--alias` when the target document is already declared in `profile.refs`.

Example:

```bash
poetry run wikiops docs get \
  --config wikiops.yaml \
  --profile default \
  --alias handbook
```

### Select By Path

Use `--path` for ad hoc reads when the selected provider supports path resolution.

Example:

```bash
poetry run wikiops docs get \
  --config wikiops.yaml \
  --profile default \
  --path "/engineering/platform/runbook"
```

### Output

Default output is JSON so callers such as agents can consume the result predictably.

Current JSON payload includes:

- `profile_name`
- `provider_name`
- `selector_kind`
- `selector_value`
- `link`
- `document`

Use `--output markdown` when you only want the current page content.

## `wikiops run`

Executes a planning flow and optionally applies the resulting change set.

Required options:

- `--config`, `-c`
- `--profile`, `-p`
- `--plugin`
- `--input`, `-i`

Optional flags:

- `--apply`

## Plan Mode

Without `--apply`, the host runs in plan mode.

Example:

```bash
poetry run wikiops run \
  --config wikiops.yaml \
  --profile default \
  --plugin acme.team-docs \
  --input input.yaml
```

Current output sections:

- `=== CHANGESET ===`
- `=== DIFF ===`

The `ChangeSet` is printed as JSON. The diff is printed as text.

### Provider Target Note

When the selected provider can describe where it reads and writes, the host adds a note with code `provider_target` as the first entry of `ChangeSet.notes`, before any note emitted by the plugin. Its message has the form `Provider '<provider name>' (<provider id>) target: <description>`. The note appears in plan mode and again in apply mode, because `--apply` plans first. Providers that cannot describe their target, such as `azure_devops_wiki`, add no note. The `local_files` provider does describe its target (resolved root, working directory, and assets directory); see [`../reference/local-files.md`](../reference/local-files.md).

Read this note before running with `--apply`, especially when a provider is configured with a relative path: the description shows the resolved location and the working directory used to resolve it.

If a provider supports the hook but fails while describing its target, the plan still succeeds. No `provider_target` note is added and the host appends a warning with code `provider_target_unavailable` to `ChangeSet.warnings` instead. Its message names the provider and the exception, for example `Provider 'local' (local_files) could not describe its target (<ExceptionType>: <text>).`, and its details carry `provider`, `provider_id`, and `error_type`. Treat that warning as a sign that the target cannot be confirmed before `--apply`.

## Apply Mode

With `--apply`, the host also persists changes through the selected provider.

Example:

```bash
poetry run wikiops run \
  --config wikiops.yaml \
  --profile default \
  --plugin acme.team-docs \
  --input input.yaml \
  --apply
```

Current output sections:

- `=== CHANGESET ===`
- `=== DIFF ===`
- `=== APPLY RESULT ===`

## Exit Codes

Current behavior:

- plan mode returns `0` on success
- apply mode returns `0` when no operation failed
- apply mode returns `1` when any operation failed

This makes the CLI suitable for CI or scripted automation flows that need to fail fast on apply errors.

## Input Files

The input file is loaded as YAML and normalized into a plain dictionary before plugin input validation happens.

Example input file:

```yaml
team_name: Platform
```

## Recommended Workflow

1. Run `wikiops plugins`.
2. Run `wikiops providers`.
3. Use `wikiops docs get` to inspect the current document state when needed.
4. Prepare configuration and input YAML files.
5. Run `wikiops run` in plan mode first.
6. Inspect the `ChangeSet` and diff.
7. Rerun with `--apply` when ready.

## Related Documentation

- [`../getting-started.md`](../getting-started.md)
- [`../configuration.md`](../configuration.md)
- [`../execution-flow.md`](../execution-flow.md)
