---
name: wikiops-host
description: Use this skill when the user wants to configure or operate the WikiOps host in natural language: inspect installed plugins or providers, create or update a WikiOps YAML config, configure the built-in Azure DevOps Wiki, local files or GitHub wiki provider, read the current content of a page with `wikiops docs get`, or execute a plugin with an input YAML through `wikiops run` in plan or apply mode. If a companion plugin skill exists, use it for plugin-specific input or business rules, then return to this skill for host execution and verification.
compatibility: Requires a shell with the `wikiops` CLI available directly or through `poetry run wikiops`. Azure DevOps flows require `AZDO_PAT` or the configured PAT env var. GitHub wiki flows require `git` and the credentials of the chosen auth mode.
metadata:
  author: sgg10
  scope: host-runtime
  version: "1.0.0"
---

# WikiOps Host

## Use this skill when

Use this skill when the user wants help with the WikiOps host runtime itself:

- configure `wikiops`
- create or update `config.yaml` or `wikiops.yaml`
- inspect installed plugins or providers
- configure the built-in `azure_devops_wiki` provider
- configure the built-in `local_files` provider (Markdown files in a local directory)
- configure the built-in `github_wiki` provider (pages of a GitHub repository wiki, published through a local git clone)
- read the current content of an existing page
- run a plugin with an input YAML
- plan changes first, then optionally apply them

## Do not use this skill for

Do not use this skill as the primary source of truth for plugin business logic.

If the task depends on a plugin-specific input schema, template, managed blocks, examples, or domain rules, load the corresponding plugin skill first. Then return to this skill to run `wikiops` commands with the generated YAML.

Examples:

- `nequi.datamind` input design belongs to the plugin skill
- provider authoring code does not belong to this skill
- SDK contract design does not belong to this skill

## Companion plugin skills

Some plugins may publish their own companion skills. Treat the relationship like this:

- this host skill owns CLI usage, config structure, provider setup, `docs get`, `run`, plan/apply flow, and verification
- a plugin skill owns plugin-specific input shape, business rules, templates, examples, managed blocks, and domain-specific defaults

When both skills exist, use this handoff pattern:

1. Start in `wikiops-host` to inspect providers, plugins, config, refs, and current page state
2. Switch to the plugin skill to build or update the correct input YAML
3. Return to `wikiops-host` to execute `wikiops run`, inspect the `ChangeSet` and diff, and optionally apply changes
4. Use `wikiops-host` again to verify the final page content with `wikiops docs get`

If a companion plugin skill exists, prefer it over reconstructing the plugin input schema from memory.

## Default operating workflow

1. Confirm the runtime surface first.
   - Run `wikiops providers`
   - Run `wikiops plugins`
   - If `wikiops` is not available directly, use `poetry run wikiops`

2. If the user wants to configure WikiOps.
   - Read or create the target YAML config
   - Define `providers`
   - Define `profiles`
   - Define `refs` only for pages the workflow needs
   - Define profile-scoped plugin config under `profiles.<profile>.plugins.<plugin_id>` only when the workflow is plugin-driven
   - Prefer plugin defaults over explicit overrides unless the user really needs custom behavior

3. If the user wants current page content.
   - Prefer `wikiops docs get --alias` when the page already exists in `profile.refs`
   - Use `wikiops docs get --path` only when an ad hoc path is needed
   - Default to JSON unless the user only needs the markdown body

4. If the user wants to run a plugin.
     - Confirm the plugin is installed via `wikiops plugins`
     - Always include `--plugin <plugin_id>` in `wikiops run`
     - If a companion plugin skill exists and the input YAML is plugin-specific, load that skill first
     - If no companion plugin skill exists, derive the input only from the plugin repo, examples, or validated docs already present in the workspace
     - Generate or update the input YAML
     - Run `wikiops run` in plan mode first
     - Inspect `=== CHANGESET ===` and `=== DIFF ===`
     - If `ChangeSet.notes` starts with a `provider_target` note, read it (resolved root, working directory) before any `--apply`
    - Only use `--apply` when the user explicitly wants persistence

5. After apply.
   - Inspect `=== APPLY RESULT ===`
   - Treat any failed operation as a failed run
   - Only after the apply process has completed, read the page again with `wikiops docs get`

## Command mapping

For exact command syntax and examples, read:

- [CLI reference](references/cli.md)

For config structure, read:

- [Configuration reference](references/configuration.md)

For the built-in Azure DevOps Wiki provider, read:

- [Azure DevOps provider reference](references/azure-devops-provider.md)

For the built-in local files provider, including how to react to each `[local_files:<code>]` error, read:

- [Local files provider reference](references/local-files-provider.md)

For the built-in GitHub wiki provider, including auth modes, the clone and commit/push behavior, and how to react to each `[github_wiki:<code>]` error, read:

- [GitHub wiki provider reference](references/github-wiki-provider.md)

For end-to-end workflows, read:

- [Host workflows](references/workflows.md)

For pitfalls and runtime constraints, read:

- [Gotchas](references/gotchas.md)

## Behavioral rules

- Prefer the host's documented behavior over guessing internals
- Prefer the manifest plugin ID as the config key under `profiles.<profile>.plugins`
- Prefer alias-based reads when possible
- Prefer plan mode before apply
- Do not invent plugin input schemas when a companion plugin skill or plugin source of truth is available
- Do not add plugin config or plugin refs to host config unless the current workflow actually needs them
- Do not override plugin template paths or block names unless there is a concrete reason to diverge from plugin defaults
- Do not assume plugins are built into the host; verify with `wikiops plugins`
- Do not assume which providers are installed; verify with `wikiops providers`
- Before `wikiops run --apply`, read the `provider_target` note in the plan output (when present) and confirm the resolved root and working directory are the ones the user intends; a `provider_target_unavailable` warning means the target could not be confirmed
- For `local_files` failures, read the `Hint:` in the `[local_files:<code>]` message and follow [Local files provider reference](references/local-files-provider.md); never change `overwrite_existing` or `root` on your own to get past an error
- For `github_wiki` failures, read the `Hint:` in the `[github_wiki:<code>]` message and follow [GitHub wiki provider reference](references/github-wiki-provider.md); never put a token in the config, never enable `allow_auto_push` or change `workdir`, `branch`, or `auth` on your own to get past an error, and tell the user when a run only committed locally (the wiki is not published until pushed)
- If the Azure DevOps PAT env var is missing, stop and ask the user to provide or export it
- If the workflow uses local assets, ensure the plugin config allows the asset roots or intentionally disables that protection
- Do not start post-run verification reads before `wikiops run --apply` has fully finished

## Companion skill boundary

If the plugin has its own skill, this host skill should not try to replace it.

Use the plugin skill for:

- building plugin-specific `input.yaml`
- deciding which command variant the plugin needs
- choosing plugin-specific defaults
- understanding plugin-managed sections or assets

Use `wikiops-host` for:

- editing or validating `config.yaml`
- configuring providers and profiles
- reading current page content
- executing `wikiops run`
- reviewing `ChangeSet`, diff, and `ApplyResult`
- post-run verification

## Coordination with plugin skills

When the user asks for a plugin-driven documentation update:

1. Use this skill to inspect the current runtime configuration and current page content
2. Load the relevant companion plugin skill, if one exists, to build the correct input YAML
3. Return to this skill to execute:
   - `wikiops run ...` in plan mode
   - inspect output
   - optionally `wikiops run ... --apply`
4. Use this skill to verify the resulting page state after apply

## Example natural-language intents this skill should handle

- "Configura WikiOps para Azure DevOps Wiki con este organization, project y wiki"
- "Muéstrame qué plugins y providers tengo instalados"
- "Lee el contenido actual de la página `sample_dp`"
- "Ejecuta el plugin `nequi.datamind` con este input yaml"
- "Crea o actualiza mi `config.yaml` para que use el provider de Azure DevOps"
- "Configura WikiOps para escribir la documentación como archivos Markdown en la carpeta `./docs`"
- "Configura WikiOps para publicar la documentación en la wiki de GitHub del repositorio acme/platform"
- "Dado que ya existe una página, lee su contenido actual y luego corre WikiOps con el input que produzca la skill del plugin"

## Validation loop

1. Inspect providers and plugins first.
2. Validate the config path and input path exist.
3. Prefer `wikiops run` without `--apply`.
4. Review the `ChangeSet` and diff before persisting, including the `provider_target` note when present.
5. If apply was requested, inspect `ApplyResult` before declaring success.
6. When the run updates or creates a page, verify the resulting content with `wikiops docs get`.

## Bundled files

- [CLI reference](references/cli.md)
- [Configuration reference](references/configuration.md)
- [Azure DevOps provider reference](references/azure-devops-provider.md)
- [Local files provider reference](references/local-files-provider.md)
- [GitHub wiki provider reference](references/github-wiki-provider.md)
- [Host workflows](references/workflows.md)
- [Gotchas](references/gotchas.md)
- [Azure DevOps config example](assets/config.azure-devops.example.yaml)
- [Local files config example](assets/config.local-files.example.yaml)
- [GitHub wiki config example](assets/config.github-wiki.example.yaml)
- [Activation eval queries](assets/eval-queries.json)

## Design note

This skill intentionally does not bundle wrapper scripts in `scripts/`.

Reason:

- the `wikiops` CLI is already the stable execution interface
- wrapper scripts would duplicate host behavior and become a second contract to maintain
- the highest-value guidance here is workflow, config structure, command selection, and plugin-skill handoff
