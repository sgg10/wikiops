# CLI Reference

This file documents the current `wikiops` CLI behavior that an agent should rely on.

## Preferred command prefix

Use `wikiops` if the executable is already installed in the current environment.

If the project is being run from a Poetry-managed checkout and `wikiops` is not on `PATH`, use:

```bash
poetry run wikiops
```

The command surface is the same either way.

## Available commands

- `wikiops plugins`
- `wikiops providers`
- `wikiops docs get`
- `wikiops run`

## `wikiops plugins`

Lists discovered plugins.

Example:

```bash
wikiops plugins
```

Current output shape:

```text
- <plugin_id> :: <display_name> (<version>)
```

Use this command before attempting to run a plugin.

## `wikiops providers`

Lists discovered provider types.

Example:

```bash
wikiops providers
```

Use this command to confirm which provider types are available. The built-in ones are `azure_devops_wiki`, `local_files` and `github_wiki`.

## `wikiops docs get`

Fetches the current state of a single page through the provider configured for a profile.

Required options:

- `--config`, `-c`
- `--profile`, `-p`
- exactly one of `--alias` or `--path`

Optional flags:

- `--output json|markdown`

### Read by alias

Prefer alias-based reads when the page already exists in `profile.refs`.

```bash
wikiops docs get -c config.yaml -p test --alias sample_dp
```

### Read by path

Use this when the config does not already define a ref alias for the page.

```bash
wikiops docs get -c config.yaml -p test --path "/Engineering/Platform/Runbook"
```

### Output modes

- `json` is the default and is better for agents
- `markdown` prints only the page content body

Examples:

```bash
wikiops docs get -c config.yaml -p test --alias sample_dp --output json
wikiops docs get -c config.yaml -p test --alias sample_dp --output markdown
```

## `wikiops run`

Executes a plugin planning flow and optionally applies the changes.

Required options:

- `--config`, `-c`
- `--profile`, `-p`
- `--plugin`
- `--input`, `-i`

Optional flags:

- `--apply`

### Plan mode

Without `--apply`, the host only plans.

```bash
wikiops run -c config.yaml -p test --plugin nequi.datamind -i datamind_commands_inputs/create_dp_input.yaml
```

Expected sections:

- `=== CHANGESET ===`
- `=== DIFF ===`

When the provider can describe its target (for example `local_files` or `github_wiki`), the first entry of `ChangeSet.notes` has code `provider_target` and reads `Provider '<name>' (<provider id>) target: <description>`. Read it before applying. If the provider fails to describe its target, the plan continues without the note and `ChangeSet.warnings` gets a `provider_target_unavailable` warning.

### Apply mode

With `--apply`, the host also persists changes.

```bash
wikiops run -c config.yaml -p test --plugin nequi.datamind -i datamind_commands_inputs/create_dp_input.yaml --apply
```

Expected sections:

- `=== CHANGESET ===`
- `=== DIFF ===`
- `=== APPLY RESULT ===`

### Exit codes

- plan mode returns `0` on success
- apply mode returns `0` when no operation failed
- apply mode returns `1` when any operation failed

## Recommended runtime sequence

1. `wikiops providers`
2. `wikiops plugins`
3. `wikiops docs get` if current page state matters
4. `wikiops run` in plan mode
5. Inspect `ChangeSet`, diff, and the `provider_target` note
6. `wikiops run ... --apply` only if persistence is desired and the plan is acceptable
