# Gotchas

These are runtime facts the agent should keep in mind.

## 1. The host ships no built-in plugins

`wikiops` includes the CLI and the built-in providers (Azure DevOps Wiki and local files), but not business plugins.

Always verify plugin availability with:

```bash
wikiops plugins
```

## 2. Use the plugin skill for plugin input shape

The host validates plugin input, but it does not define plugin business schemas.

If the user asks for a plugin-specific YAML and the structure is not already known from the repo, load the plugin skill first.

## 3. `docs get` requires exactly one selector

`wikiops docs get` must receive exactly one of:

- `--alias`
- `--path`

Do not pass both.

## 4. Prefer alias-based reads when possible

If the config already defines a ref alias, use it instead of duplicating the path on the command line.

This keeps the workflow more stable and easier to reuse.

## 5. Azure DevOps requires a PAT env var

The built-in provider fails in `validate_settings()` if the configured PAT variable does not exist.

Default:

```text
AZDO_PAT
```

## 6. Plan before apply

`wikiops run --apply` mutates the remote documentation system.

Default behavior for agents should be:

1. run plan mode
2. inspect `ChangeSet` and diff
3. apply only if the user intends persistence

## 7. `wikiops run` always needs `--plugin`

If the workflow is plugin-driven, do not forget:

```bash
wikiops run --plugin <plugin_id> ...
```

The host cannot infer the plugin from the input file alone.

## 8. Asset workflows may depend on plugin policy

If a plugin uses local assets, the plugin config may require:

- `asset_policy.allowed_asset_roots`

Without that, local file assets can fail before apply or during apply.

## 9. `wikiops` and `wikiops-sdk` must stay aligned

If the host is loaded from a local checkout but `wikiops-sdk` comes from an older wheel in the virtualenv, runtime imports can fail.

If you see host/SDK API drift, reinstall the local SDK into the same environment.

## 10. Package-relative plugin resources are a host behavior

Plugin resource paths are package-relative, not implicitly rooted at `resources/` unless the plugin chose that convention itself.

Do not guess resource paths from host intuition alone; inspect the plugin repo or load its skill.

## 11. Do not verify before apply completes

If you run `wikiops run --apply`, wait for the command to finish and inspect `=== APPLY RESULT ===` before trying to read the resulting page. Reading too early can produce false 404s or stale content.

## 12. `wikiops` can be executed directly or through Poetry

In installed environments, prefer `wikiops ...`.

In source checkouts, it may be necessary to use:

```bash
poetry run wikiops ...
```

## 13. Read the `provider_target` note before `--apply`

Providers that can describe their target, such as `local_files`, add a `provider_target` note as the first entry of `ChangeSet.notes`. It shows the resolved root and the working directory. A relative `root` resolves against the directory the command is launched from, so the same config can write to a different place from a different directory.

Before `--apply`, confirm `root=` is the directory the user intended. If a `provider_target_unavailable` warning appears instead, the target could not be confirmed.

## 14. `local_files` paths are root-relative and end in `.md`

Refs for `local_files` look like `guide/README.md`: no leading `/`, `/` separators, `.md` suffix. Azure-style paths such as `/Engineering/Teams` fail with `path.absolute`. Paths are never auto-corrected; use the `Hint:` in the error.

## 15. Plans show no diff for `local_files` creates

The diff covers updates. A create conflict (`conflict.exists`) only appears when applying, and a create is not atomic with respect to other processes: do not run two applies against the same root at once. See [Local files provider reference](local-files-provider.md).

## 16. `resolved_asset_reference` is not what a `local_files` document contains

With `local_files`, `=== APPLY RESULT ===` shows the generic `/assets/...` reference for an uploaded asset, while the written Markdown files contain links relative to each document. Verify links by reading the file with `wikiops docs get`.
