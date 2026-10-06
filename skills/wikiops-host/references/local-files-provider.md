# Local Files Provider Reference

This file explains how to configure and operate the built-in `local_files` provider, and how to react when it reports an error.

Provider ID (the `type` value in config): `local_files`. Verify it is installed with `wikiops providers`.

## When to use it

Use `local_files` when the user wants documents written as Markdown files in a local directory instead of a remote wiki. Example config: [local files config example](../assets/config.local-files.example.yaml).

## Settings

- `root` (required): directory that holds every document. It must already exist. `~` is expanded. A relative value resolves against the **working directory of the command**, not against the config file.
- `assets_dir` (optional, default `assets`): root-relative directory for stored assets.
- `overwrite_existing` (optional, default `false`): lets create operations replace an existing file whose content differs.

Unknown keys are rejected; a typo fails validation.

## Refs

- `kind: path` with `locator.path` is the only supported ref shape.
- The path is relative to `root`, uses `/`, has no leading `/`, and must end in `.md`. Write `guide/README.md`, not `/guide/README.md`.
- Paths are never auto-corrected. Every rejection includes a `Hint:` with the corrected value; use it.
- Do not reuse Azure DevOps style paths such as `/Engineering/Teams`.

## Before `--apply`: read the `provider_target` note

Every plan (and every apply, which plans first) starts `ChangeSet.notes` with a `provider_target` note:

```text
Provider 'local' (local_files) target: root='/abs/project/docs' configured='./docs' cwd='/abs/project' assets_dir='/abs/project/docs/assets'
```

Always check `root=` and `cwd=` before running `--apply`. If `root=` is not the directory the user intended, stop and fix the working directory or `root`; do not apply and do not "fix" it by moving files afterwards. If a `provider_target_unavailable` warning appears instead of the note, the target could not be confirmed: investigate before applying.

## Behavior to expect

- An explicit `ref` on a create operation is written verbatim and wins over `parent_ref` and the title. Plugins decide the layout. Without `ref` and `parent_ref` the file is `<root>/<Title>.md`.
- Create is idempotent: same content gives `SKIPPED` (`noop.unchanged`); different content fails with `conflict.exists` unless `overwrite_existing: true`.
- Update never creates a file, and a stale `expected_version` fails with `conflict.version_mismatch`.
- The plan shows no diff for creates, so a create conflict only surfaces at apply time. Check target files yourself with `wikiops docs get` when it matters.
- Asset links inside written files are relative to each document (for example `../../assets/x--<hash>.png`). `resolved_asset_reference` in `=== APPLY RESULT ===` still shows the generic `/assets/...` value. Verify links by reading the file, not that field.
- A failed nested create can leave empty directories inside the root. This is expected and harmless.
- Create is check-then-write: a file created by another process between the check and the write can be replaced even with `overwrite_existing: false`. Do not run two WikiOps applies against the same root concurrently.

## Reading a failure

Failures appear as `FAILED` results in `=== APPLY RESULT ===` (and the CLI exits with `1`), or as configuration errors during planning. Messages have this shape:

```text
[local_files:<code>] <summary>. path='<root-relative path>' root='<absolute root>'. Hint: <action>.
```

Match the code with `^\[(?P<ns>[a-z_]+):(?P<code>[a-z_]+(\.[a-z_]+)*)\]`, then react as below. Always read the `Hint:` first; it is specific to the failure.

### Settings

| Code | How to react |
| --- | --- |
| `settings.root_missing` | `root` is empty or the directory does not exist. Compare `configured`, `cwd`, and the resolved path in the message. Create the directory or fix `root`. Never point `root` at a directory the user did not name. |
| `settings.root_not_directory` | `root` is a file. Point it at a directory. |
| `settings.assets_dir_invalid` | `assets_dir` is absolute, contains `..`, or escapes the root. Use a root-relative directory such as `assets`. |

### Paths and refs

| Code | How to react |
| --- | --- |
| `path.empty` / `path.absolute` | Give a non-empty, root-relative path with no leading `/`. |
| `path.traversal` | Remove `..`. Do not try to reach outside the root; change `root` only if the user asks. |
| `path.invalid_chars` | Replace backslashes, control characters, and `: * ? " < > \|`. Use `/`. |
| `path.non_canonical` | Use the canonical form from the hint (no `./`, `//`, trailing `/`). |
| `path.reserved` | `.git` segments are not accessible. Pick another location. |
| `path.not_markdown` | Use the `.md` name from the hint; do not strip the check. |
| `path.symlink_escape` | A symlink leads outside the root. Remove or retarget the link inside the root; do not widen `root` just to bypass it. |
| `path.unresolvable` | Symlink loop, unreadable component, or the path resolves to the root itself. Fix the loop or permissions, or choose a path inside the root. |
| `path.not_a_file` | A directory is where the file should be. Point the ref at a `.md` file or ask before removing the directory. |
| `path.parent_not_directory` | A file occupies a parent path segment (named in `path=`). Choose another location or ask the user before moving that file. |
| `ref.unsupported_kind` / `ref.missing_path` | Use `kind: path` with `locator.path`. |

### Documents and titles

| Code | How to react |
| --- | --- |
| `document.not_found` | The file does not exist. For reads, fix the ref. For updates, the plugin must create it first. |
| `document.decode_error` | The file is not valid UTF-8. Ask the user to re-save it as UTF-8; do not rewrite it blindly. |
| `document.encode_error` | The new content has characters that cannot be written as UTF-8 (such as an unpaired surrogate). Nothing was written. Fix the generated content or the plugin input, then re-run. |
| `title.invalid` | The title cannot be a file name (empty, leading `.`, `/ \ : * ? " < > \|`, control characters). Give a plain title or set an explicit `ref` on the operation. |

### Conflicts

| Code | How to react |
| --- | --- |
| `conflict.exists` | A different file already exists at the create target. Ask the user: use an update operation, pick another `ref`, or set `overwrite_existing: true` on that provider. Never flip `overwrite_existing` on your own. |
| `conflict.version_mismatch` | The file changed after the plan. Re-run the plan, review it, and apply again. |
| `conflict.asset_mismatch` | A stored asset with the same hashed name has different bytes. Ask before deleting or renaming the stored file, then upload again. |

### Assets

| Code | How to react |
| --- | --- |
| `asset.missing_name` | The asset has no file name. Fix the plugin input. |
| `asset.invalid_name` | The asset name contains `/`, `\`, or `..`. Pass only a file name such as `diagram.png`. |

### Other outcomes

| Code | How to react |
| --- | --- |
| `io.error` | The OS refused the read or write (permissions, space) or something unexpected happened. Fix the cause named in the message, then re-run. |
| `noop.unchanged` | Not a failure. The file already has that content (`SKIPPED`). |
| `applied.overwritten` | Not a failure. An existing file was replaced because `overwrite_existing` is `true`. Mention it to the user. |
| `op.unsupported` | Not a failure. The operation type is not handled by this provider (`SKIPPED`). Mention it to the user. |

## After apply

1. Inspect `=== APPLY RESULT ===`; any `FAILED` entry means the run failed.
2. Read the result back with `wikiops docs get --alias <alias>` or `--path <root-relative .md path>`.
3. Re-running the same apply should be a no-op (`SKIPPED` / `noop.unchanged`). If it is not, investigate before telling the user the run is done.
