# Local Files Provider

This page documents the built-in `local_files` provider shipped by the host.

## Purpose

The `local_files` provider reads and writes Markdown documents as plain files below a configured root directory. It is useful for repositories that keep documentation next to code, for generating documentation into a folder that another tool publishes, and for trying a plugin without a remote wiki.

It is implemented in:

- `wikiops.providers.local_files.provider.LocalFilesProvider`
- `wikiops.providers.local_files.provider.LocalFilesProviderFactory`

Filesystem safety (root resolution, path validation, symlink confinement, atomic writes) lives in the provider-agnostic internal module `wikiops.providers._fs`. That module is an internal implementation detail, not an extension API.

## Provider ID

```text
local_files
```

This is the `type` value used in host configuration.

## Settings Model

Required settings:

- `root`: the directory that contains every document and asset

Optional settings:

- `assets_dir`: root-relative directory where assets are stored (default `assets`)
- `overwrite_existing`: allow `create` operations to replace an existing file whose content differs (default `false`)

Inherited host/runtime fields:

- `provider_name`
- `provider_api_version`

Unknown keys are rejected, so a typo such as `overwrite_exising` fails validation instead of being ignored.

Example:

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

See also the bundled [host skill example](../../skills/wikiops-host/assets/config.local-files.example.yaml).

## Root Resolution And The Working Directory

`root` is resolved when the provider is first used:

- a leading `~` is expanded
- a relative value is resolved against the **current working directory of the process**, not against the config file
- symlinks in `root` are followed and the real directory is used
- an empty value is rejected with `settings.root_missing`; it never falls back to the working directory
- the directory must already exist; the provider never creates `root`

Because a relative `root` depends on where the command is launched, the host adds a `provider_target` note to every plan and apply run. For `local_files` the note looks like:

```text
Provider 'local' (local_files) target: root='/abs/project/docs' configured='./docs' cwd='/abs/project' assets_dir='/abs/project/docs/assets'
```

`configured=` appears only when `root` is relative or starts with `~`. Read this note before running with `--apply`: if `root=` is not the directory you meant, stop and fix the working directory or the config. See [`../guides/using-the-cli.md`](../guides/using-the-cli.md#provider-target-note) for the host side of this note.

## Supported Capabilities

The provider advertises:

- `READ_DOCUMENT`
- `CHECK_EXISTS`
- `CREATE_DOCUMENT`
- `UPDATE_DOCUMENT`
- `CREATE_CHILD_DOCUMENT`
- `BUILD_LINK`
- `PUT_ASSET`
- `RESOLVE_BY_PATH`
- `HIERARCHICAL_PAGES`
- `VERSION_CHECK`

It does not advertise `RESOLVE_BY_ID`. There is no delete, move, or rename operation.

## Reference Model

Only path refs are supported:

- `kind` must be `path` (`ref.unsupported_kind` otherwise)
- `locator.path` is required (`ref.missing_path` otherwise)
- the path is **relative to the root**, uses `/` as separator, and has no leading `/`: write `guide/README.md`, not `/guide/README.md`
- the path must be canonical (no `./`, `//`, or trailing `/`; the error hint shows the canonical form)
- the path must not contain `..` segments, backslashes, control characters, or any of `: * ? " < > |`
- a `.git` segment (any case) is reserved
- the path must end in `.md` (case-insensitive)

The path is never normalized or completed silently. When a path is rejected, the `Hint:` in the message contains a corrected value you can use.

### The `.md` Rule

A document path that does not end in `.md` fails with `path.not_markdown`. The provider does not append the extension on its own. The hint proposes the fix: `docs/README` suggests `docs/README.md`, and a `.markdown`, `.mdx`, or `.txt` extension is replaced by `.md`.

## Read-Side Behavior

### `exists(ref)`

Returns `true` only for a regular file. A missing path and a directory both return `false`.

### `get_document(ref)`

Returns an SDK `Document` with:

- `title`: the file name without extension
- `content`: the file decoded as strict UTF-8, with no newline translation (CRLF is preserved) and a BOM kept as `U+FEFF`
- `version`: `token` and `etag` both set to `sha256:<hex>` of the file bytes
- `metadata`: `path` (root-relative), `absolute_path`, and `size_bytes`

A missing file raises `document.not_found`, invalid UTF-8 raises `document.decode_error`, and a directory raises `path.not_a_file`.

### `build_link(ref)`

Returns the `file://` URI of the real file path.

## Apply Behavior

The provider applies three operation types: `create_document`, `create_child_document`, and `update_document`. Operations are applied one by one in order; a failure never stops later operations. Any other operation type is reported as `SKIPPED` with `op.unsupported`.

Every write is atomic: the bytes go to a temporary file in the target directory, which then replaces the target. A failed write leaves the original file untouched and removes the temporary file. Updates keep the existing permission bits; new files follow the process umask. Missing parent directories are created one component at a time.

### Layout Belongs To Plugins

The provider does not impose a document layout. The plugin decides where each document lives:

- **An explicit `ref` always wins.** It is used verbatim as the target path. When `ref` is set, `parent_ref` and the title are ignored, even if they are absent or invalid.
- Plugins that need a deterministic location should set `ref` on every create operation.
- The derivation below is only a fallback for operations that carry no `ref`.

### Fallback Derivation (No `ref`)

| Situation | Target path |
| --- | --- |
| No `ref` and no `parent_ref` | `<root>/<Title>.md` |
| `parent_ref` is a `README.md` or `index.md` (case-insensitive) | a sibling of the parent: `<parent-dir>/<Title>.md` |
| `parent_ref` is any other document | `<parent-dir>/<parent-stem>/<Title>.md` |

For `create_child_document` the title is `child_title`; for `create_document` it is `title`. `parent_ref` is validated (it must be a valid Markdown path inside the root) but does not have to exist.

### Title Naming

The title becomes a file name as follows:

- leading and trailing whitespace is trimmed
- every run of whitespace becomes a single `-`
- case and Unicode characters are preserved (`Architecture Overview` becomes `Architecture-Overview.md`)
- `.md` is appended unless the name already ends in `.md` in any case (`Setup.MD` stays `Setup.MD`)

A title that cannot be a file name fails with `title.invalid`: empty, `.` or `..`, starting with `.`, containing a control character, or containing any of `/ \ : * ? " < > |`. Titles are only checked when the target is derived from them.

### Create: Conflict Policy And Idempotency

When the target file already exists:

| Existing file | `overwrite_existing: false` (default) | `overwrite_existing: true` |
| --- | --- | --- |
| Same bytes as the new content | `SKIPPED`, `noop.unchanged`, file not touched | `SKIPPED`, `noop.unchanged` |
| Different bytes | `FAILED`, `conflict.exists` | `APPLIED`, `applied.overwritten` |
| A directory | `FAILED`, `path.not_a_file` | `FAILED`, `path.not_a_file` |

Re-applying the same plan is therefore safe: unchanged documents are skipped and keep their modification time.

### Update

An update runs these checks in order:

1. the ref and the content are valid
2. the file exists, otherwise `document.not_found` (an update never creates a file)
3. the content is identical to the file, in which case the result is `SKIPPED` with `noop.unchanged` even if the expected version is stale
4. `expected_version` (the `token`, else the `etag`) matches the current `sha256:<hex>`, otherwise `conflict.version_mismatch`; without an expected version no check is made
5. the file is written atomically

An update writes through an in-root symlinked file: the link is preserved and its target is rewritten.

### Result Messages

Successful creates and updates use plain messages (`Document created at '<absolute path>'`, `Document updated at '<absolute path>'`) and report `resolved_ref` and `resulting_version`. Skips, overwrites, and every failure use the coded shape described next.

## Error Codes

Every provider error is rendered as:

```text
[local_files:<code>] <summary>. path='<root-relative path>' root='<absolute root>'. Hint: <action>.
```

`path=`, `root=`, and `Hint:` are omitted when they do not apply. Automation can match the code with:

```text
^\[(?P<ns>[a-z_]+):(?P<code>[a-z_]+(\.[a-z_]+)*)\]
```

Failures during apply appear as `FAILED` entries in `=== APPLY RESULT ===`; the CLI then exits with code `1`.

| Code | Meaning | Fix |
| --- | --- | --- |
| `settings.root_missing` | `root` is empty or the directory does not exist | Create the directory or correct `root`; check `cwd` in the message for relative values |
| `settings.root_not_directory` | `root` points to a file | Point `root` to a directory |
| `settings.assets_dir_invalid` | `assets_dir` is absolute, contains `..`, or escapes the root | Use a root-relative directory such as `assets` |
| `path.empty` | Path is empty | Pass a root-relative path |
| `path.absolute` | Path starts with `/` or a drive letter | Remove the leading `/`; paths are relative to the root |
| `path.traversal` | Path contains a `..` segment | Remove `..`; paths cannot leave the root |
| `path.invalid_chars` | Backslash, control character, or one of `: * ? " < > \|` | Use `/` as separator and drop the characters |
| `path.non_canonical` | `./`, `//`, or trailing `/` in the path | Use the canonical form shown in the hint |
| `path.reserved` | A `.git` segment | Choose another location |
| `path.not_markdown` | Path does not end in `.md` | Use the name suggested in the hint |
| `path.symlink_escape` | The path resolves through a symlink to a location outside the root | Remove the link or retarget it inside the root |
| `path.unresolvable` | Symlink loop, unreadable component, or the path resolves to the root itself | Fix the loop or permissions; choose a path inside the root |
| `path.not_a_file` | The target is a directory or special file | Point the ref at a Markdown file or remove the directory in the way |
| `path.parent_not_directory` | A parent segment exists but is a file | Move or remove that file, or choose another location |
| `ref.unsupported_kind` | The ref is not `kind: path` | Use path refs with `locator.path` |
| `ref.missing_path` | `locator.path` is missing | Set `locator.path` |
| `document.not_found` | The document does not exist (read or update) | Create it first, or fix the ref |
| `document.decode_error` | The file is not valid UTF-8 | Re-save it as UTF-8 |
| `document.encode_error` | The new content cannot be encoded as UTF-8 (for example an unpaired surrogate character) | Remove or replace the offending characters; nothing is written |
| `title.invalid` | The title cannot be a file name | Use a plain title or set an explicit `ref` |
| `conflict.exists` | Create target exists with different content | Use an update operation, or set `overwrite_existing: true` |
| `conflict.version_mismatch` | The file changed since the plan | Re-run the plan, then apply again |
| `conflict.asset_mismatch` | A stored asset with the same hashed name has different bytes | Delete or rename the stored file, then upload again |
| `asset.missing_name` | The asset has no file name | Give the asset a name such as `diagram.png` |
| `asset.invalid_name` | The asset name contains `/`, `\`, or `..` | Pass a plain file name, not a path |
| `io.error` | The operating system refused the read or write, or an unexpected error occurred | Check permissions, free space, and the state of the target, then re-run |
| `noop.unchanged` | Not a failure: the content is already on disk (`SKIPPED`) | None |
| `applied.overwritten` | Not a failure: an existing file was replaced (`APPLIED`) | None |
| `op.unsupported` | Not a failure: the operation type is not handled (`SKIPPED`) | None |

## Symlink Policy

Symlinks inside the root are allowed, for files and for directories. Every path is re-resolved before use, and a symlink that resolves outside the root (including a dangling one, or a swapped parent directory) is rejected with `path.symlink_escape`. Writes also re-check the target immediately before the file is replaced.

## Assets

When a plugin attaches assets, the host stores them through the provider before applying documents:

- the file is saved as `<assets_dir>/<safe-stem>--<16 hex digits of sha256><suffix>`, for example `assets/diagram--3f2a9c1d0b7e4a65.png`
- characters outside `A-Za-z0-9._-` in the stem become `-`
- uploading the same bytes again is idempotent and does not rewrite the file
- different bytes at the same hashed name are refused with `conflict.asset_mismatch`
- the asset ref is a path ref, and its embeddable reference is root-anchored (`/assets/diagram--3f2a9c1d0b7e4a65.png`, percent-encoded)

### Document-Relative Links

Files on disk must work when opened from a checkout, so before a document is written the provider rewrites each asset reference it issued into a link **relative to that document**:

| Document | Asset reference issued by the host | Link written in the file |
| --- | --- | --- |
| `Notes.md` | `/assets/diagram--3f2a9c1d0b7e4a65.png` | `assets/diagram--3f2a9c1d0b7e4a65.png` |
| `docs/guide/Setup.md` | `/assets/diagram--3f2a9c1d0b7e4a65.png` | `../../assets/diagram--3f2a9c1d0b7e4a65.png` |

The rewrite applies to Markdown link destinations (`![alt](ref)`) and to quoted HTML `src` and `href` values, and it preserves a trailing `#fragment` or `?query`. Only references issued in the current run are rewritten.

Consequences worth knowing:

- `ApplyResult.resolved_asset_reference` for the asset step still shows the generic root-anchored value (`/assets/...`), while the written files contain the relative link. Compare file contents, not that field, when checking links.
- A user-authored link that equals an issued reference is rewritten too. References embed a content hash, so a match means the same asset.
- Re-applying is idempotent: relative links never start with `/`, so a second run produces the same bytes and the documents are skipped.

## Accepted Limitations

These behaviors are known and intentional for a local, single-user trust model:

- **Plan shows no diff for creates.** The host diff covers updates. A create conflict (`conflict.exists`) therefore surfaces at apply time, not in the plan. There is no plan-time conflict warning.
- **Create is check-then-write.** The provider reads the target, decides, and then writes. A file created by another process between the check and the write can be replaced, even with `overwrite_existing: false`.
- **Check/replace race.** The final symlink confinement check happens just before the atomic replace; a swap in that tiny window is not detected. The provider does not defend against another local actor racing the filesystem.
- **Empty directories after a failed nested create.** Directories are created one component at a time, so a create that fails afterwards (for example by `path.parent_not_directory` deeper in the path) can leave empty directories inside the root. No file is left behind.

## Current Implementation Boundaries

- path refs only
- Markdown files only (`.md`)
- UTF-8 text only
- no delete, move, or rename operations
- no plan-time conflict warnings
- the root must exist before the run

## Related Documentation

- [`../configuration.md`](../configuration.md)
- [`../guides/using-the-cli.md`](../guides/using-the-cli.md)
- [`../guides/build-a-provider.md`](../guides/build-a-provider.md)
- [`internal-boundaries.md`](internal-boundaries.md)
- [`azure-devops-wiki.md`](azure-devops-wiki.md)
