# GitHub Wiki Provider

This page documents the built-in `github_wiki` provider shipped by the host.

## Purpose

The `github_wiki` provider publishes Markdown pages to the wiki of a GitHub repository (github.com or GitHub Enterprise Server). A GitHub wiki is a plain git repository (`<owner>/<repo>.wiki.git`), so the provider works through a **local clone**: it keeps the clone in sync, lets a local file backend write the pages into it, then commits and optionally pushes exactly the files it wrote.

It is implemented in the package `wikiops.providers.github_wiki`; the entry point is `wikiops.providers.github_wiki.GithubWikiProviderFactory`. The provider does not use the GitHub REST or GraphQL API (there is no content API for wikis); it only runs `git` (and, for one auth mode, the GitHub CLI `gh`).

The provider owns what is specific to a GitHub wiki: the flat page namespace and link policy, the lifecycle of the clone, credentials, commits, and pushes. The file I/O itself is delegated to a **local backend**, by default the built-in [`local_files`](local-files.md) provider rooted at the clone.

## Provider ID

```text
github_wiki
```

This is the `type` value used in host configuration.

## Prerequisites

- `git` on `PATH` (checked offline when the provider is validated: `config.git_unavailable`)
- the wiki feature enabled for the repository, with **at least one page created in the web UI** (see [Uninitialized Wikis](#uninitialized-wikis))
- credentials for the chosen `auth` mode (see [Authentication](#authentication)); the default `ambient` mode needs none in the wikiops configuration
- `gh` on `PATH` only for `auth.mode: gh`

## Settings Model

Required settings:

- `repository`: `owner/repo` of the repository that owns the wiki; no `.wiki` or `.git` suffix, no scheme, no credentials

Optional settings:

| Setting | Default | Meaning |
| --- | --- | --- |
| `host` | `github.com` | Bare hostname of the GitHub instance (no scheme, user-info, path or port); set it for GitHub Enterprise Server |
| `branch` | remote default | Branch to use. When omitted it is read from the remote HEAD (`master` for wikis created by the web UI); never assumed |
| `workdir` | per-profile cache directory | Directory of the local clone (see [Workdir And Cache](#workdir-and-cache)) |
| `sync_on_plan` | `true` | Fetch and fast-forward the clone on plan. `false` makes a plan read the existing clone without network access |
| `auth` | `{mode: ambient}` | How credentials reach git: `env`, `gh`, `ssh` or `ambient` |
| `commit.identity` | `{mode: git}` | Identity of the commits: `git`, `bot` or `custom` |
| `commit.message` | `docs(wiki): update via wikiops plugin {plugin_id}` | Commit message template; placeholders `{plugin_id}`, `{provider_name}`, `{page_count}` |
| `allow_auto_commit` | `true` | Commit what an apply wrote |
| `allow_auto_push` | `false` | Push the commits after an apply; requires `allow_auto_commit: true` |
| `local_backend` | `{type: local_files}` | Backend that writes the files into the clone; extra keys are the backend's own options |
| `git_timeout_seconds` | `120` | Time limit of every git command, from `5` to `3600` |

Inherited host/runtime fields: `provider_name` and `provider_api_version`.

Unknown keys are rejected with `config.invalid`, which names the setting and lists the valid keys, so a typo such as `allow_auto_psuh` fails validation instead of being ignored. Every configuration problem is reported as one coded `[github_wiki:config.*]` error.

Example:

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
      message: "docs(wiki): update via wikiops plugin {plugin_id}"
    allow_auto_commit: true
    allow_auto_push: false
    local_backend:
      type: local_files
      assets_dir: assets

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

See also the bundled [host skill example](../../skills/wikiops-host/assets/config.github-wiki.example.yaml).

## Authentication

Exactly one credential strategy serves a provider. A fresh credential is built for **every network command**; nothing is cached, so two profiles in one process never share a token.

| Mode | Settings | Remote | Credential |
| --- | --- | --- | --- |
| `env` | `variable` (required): name of the environment variable | `https://<host>/<owner>/<repo>.wiki.git` | The token is read from the variable at each network command. Unset, empty or blank is `auth.env_missing` |
| `gh` | `account` (required): GitHub login | `https://<host>/<owner>/<repo>.wiki.git` | The token is requested from the GitHub CLI for that account at each network command (`auth.gh_unavailable` when `gh` is missing, `auth.gh_failed` when it cannot produce a token) |
| `ssh` | `key_path` (optional): identity file | `git@<host>:<owner>/<repo>.wiki.git` | The SSH agent or the given key. Host keys are never accepted automatically (`auth.ssh_host_key`). A missing key file is `config.key_path_missing` |
| `ambient` (default) | none | `https://<host>/<owner>/<repo>.wiki.git` | Nothing is added: git uses the user's own configuration (credential helper, SSH config, and so on) |

`env` and `gh` tokens apply to HTTPS only. A clone created with one transport is not reused by a profile configured for another (`sync.remote_mismatch`).

### Token Handling And Redaction

- The token never appears in argv, a URL, or a file git writes. It reaches git through `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_n`/`GIT_CONFIG_VALUE_n` environment entries scoped to the configured host and appended after the user's own entries.
- Git never prompts on a terminal (`GIT_TERMINAL_PROMPT=0`): a missing or rejected credential fails fast with `auth.rejected`. Inherited `GIT_ASKPASS`, `SSH_ASKPASS`, and `GIT_TRACE*` variables are dropped from network commands.
- Every message, hint, and context value is redacted before it is shown: the token, its `x-access-token:<token>` pair and base64 form, `Authorization` headers, and `user:password@` URL user-info are replaced with `***`.
- `describe_target` and the plan notes name the auth mode (`env:GITHUB_TOKEN`, `gh:octocat`, `ssh`, `ambient`), never a value.

## Workdir And Cache

Without `workdir` every profile gets its own clone below the per-user cache:

```text
<cache>/wikiops/github_wiki/<host>/<owner>/<repo>/<key>
```

- `<cache>` is `~/Library/Caches` on macOS, `$XDG_CACHE_HOME` (else `~/.cache`) on Linux, and `%LOCALAPPDATA%` on Windows. Relative environment values are ignored.
- `<host>`, `<owner>` and `<repo>` are separate lowercase segments, and `<key>` is derived from the profile's provider name (`Docs` and `docs` map to different directories on case-insensitive file systems), so two profiles never share a clone by accident.

An explicit `workdir` may be absolute, start with `~`, or be relative to the **current working directory of the process** (not the config file). A blank value, the user's home directory, and the filesystem root are refused with `workdir.unusable`. The directory is created by the clone when it does not exist; an existing directory must be empty or already a clone of exactly this wiki.

Read the `provider_target` note of every plan (see [Plan Note](#plan-note)) before `--apply`: it shows the resolved `workdir`.

## The Clone And Sync

On the first use, or whenever `sync_on_plan` is `true`, the provider makes sure the workdir holds a clone of exactly the configured wiki:

1. **Identity first.** An existing directory must be the root of a complete clone whose `origin` is the credential-free remote derived from the settings. Anything else is refused untouched (`sync.workdir_not_clone`, `sync.remote_mismatch`); `origin` is never rewritten and nothing is deleted. A nested repository below the workdir is never mistaken for it.
2. **Branch.** The branch comes from the remote HEAD (`git ls-remote --symref`), unless `branch` overrides it. A different checked-out branch is `sync.branch_mismatch`; the provider never checks out or resets anything. An unknown branch is `sync.branch_not_found`, and the message lists the available branches.
3. **Clone.** A missing workdir is cloned into a hidden sibling directory that is renamed onto the workdir only after git succeeded, so a failed or interrupted clone never leaves a partial workdir behind.
4. **Dirty check.** Foreign changes refuse the run before any fetch (see [Dirty Workdir](#dirty-workdir-pending-manifest-and-lock)).
5. **Fetch and fast-forward.** `git fetch` then `git merge --ff-only`. A history that cannot be fast-forwarded is `sync.diverged`: there is no rebase, no merge commit, and no data loss. Local commits ahead of the remote are kept.

Apply **always** syncs. With `sync_on_plan: false` a plan makes no network call at all: it reads the existing clone as it was last fetched (`sync.no_local_clone` when there is none, `sync.branch_not_found` when the branch has no remote-tracking ref in the clone) and the plan note carries a `sync.stale_plan` warning with the time of the last fetch.

### Uninitialized Wikis

A GitHub wiki repository does not exist until its first page is created. Cloning an uninitialized wiki fails with `wiki.not_initialized`, and the same code is used when the wiki feature is disabled, the repository name is wrong, or the credential has no access (GitHub answers all of them alike). **Create the first page in the web UI** (repository > Wiki > "Create the first page"), make sure Wikis are enabled in the repository settings, then run again. The provider does not bootstrap an empty wiki by pushing.

### Dirty Workdir, Pending Manifest, And Lock

The clone belongs to wikiops while it runs, but a user may edit it. Before every write the provider classifies the dirty paths reported by `git status`:

- **Pending (wikiops' own):** listed in the manifest with a hash that still matches the bytes on disk. They may be written over and ride the next commit.
- **Foreign:** every other dirty path, including a pending path that was edited, deleted, or replaced since. Any foreign path refuses the run with `workdir.dirty`, which names the workdir, up to 20 paths, and how many more there are.

To continue, commit, stash, or discard the listed paths, or point `workdir` elsewhere.

State files live inside `.git`, so they are never versioned and never show up in `git status`:

| File | Purpose |
| --- | --- |
| `<git dir>/wikiops/pending.json` | Pending manifest: `{"version": 1, "paths": {"<path>": "sha256:<hex>"}}`, written atomically. Kept in both `allow_auto_commit` modes. An unreadable or invalid manifest is `workdir.manifest_corrupt`; delete it as the message says (its former paths are then treated as foreign) |
| `<git dir>/wikiops/lock` | Advisory OS lock of the clone. A run holds it for its whole sequence and never waits: a concurrent run fails at once with `workdir.locked`, naming the holder's pid, host, start time, and purpose. The OS drops the lock when its holder dies, so a killed run never leaves a stale lock |

An operation the provider rejects after the backend already wrote its file (for example a reported reference that breaks the page policy) is rolled back under the lock, but only for the paths **that operation wrote**. Right before delegating to the backend the provider takes a `git status` snapshot; after a rejection it touches only a path that appeared since the snapshot and that the rejected operation targets (the page it carried, or the content-hashed file or reported path of an asset upload, also when the upload is part of a change set): a tracked file is restored from HEAD and an untracked one is removed. Any other new path, such as a file a user created in the clone while the run held the lock, is left untouched and reported as foreign (`left untouched: ... not part of it`). Paths that were already pending are never touched. Whatever the rollback could not restore or chose to leave is named in the failed result.

Publishing decides from `git status` before it stages anything: an apply whose pages are identical to HEAD stages nothing, makes no commit, and never needs the commit template. A written page or asset that a `.gitignore` or `.git/info/exclude` rule in the wiki repository hides from git would never be committed, so its operation fails with `commit.failed`, naming the path and saying it is ignored by git, with the hint to remove the ignore rule or rename the page. The other pages of the same apply are still committed.

## Pages: A Flat Namespace

A GitHub wiki is a flat set of root-level Markdown pages. Only path refs are supported:

- `kind` must be `path` (`ref.unsupported_kind` otherwise) and `locator.path` is required (`ref.missing_path`)
- the path is a root page name that **ends in `.md`**, case-insensitive (`path.not_markdown`); the provider never appends the extension
- the path must not contain separators: `guides/setup.md` fails with `path.nested_not_supported`, and the hint proposes the flat name `guides-setup.md`. The provider never flattens names on its own
- a `.git` segment is reserved (`path.reserved`)
- a name that exists but cannot be used is `path.invalid_name`: a blank stem such as `.md` or ` .md`, control characters (including NUL), and leading or trailing whitespace (also right before `.md`). The Hint states the exact problem and, when one exists, a corrected name (control-character runs become `-`, surrounding whitespace is dropped)

Operations without a `ref` derive a root page from the title: whitespace runs become `-`, case and Unicode are preserved, and `.md` is appended (`Release Notes` becomes `Release-Notes.md`). A title that would need a separator, starts with `.`, or contains control or reserved characters (`/ \ : * ? " < > |`) fails with `title.invalid`. A `create_child_document` operation creates a **root page** too: wiki pages have no hierarchy, so the provider does not advertise `HIERARCHICAL_PAGES`.

Special wiki pages such as `_Sidebar.md` and `_Footer.md` are ordinary flat pages to the provider. Sidebar generation is not supported yet (see [Current Implementation Boundaries](#current-implementation-boundaries)).

`build_link(ref)` returns the wiki URL `https://<host>/<owner>/<repo>/wiki/<page-stem>` (percent-encoded).

### Supported Capabilities

`READ_DOCUMENT`, `CHECK_EXISTS`, `CREATE_DOCUMENT`, `UPDATE_DOCUMENT`, `CREATE_CHILD_DOCUMENT`, `BUILD_LINK`, `PUT_ASSET`, `RESOLVE_BY_PATH`, and `VERSION_CHECK`. There is no delete, move, or rename.

Read-side behavior (`exists`, `get_document`, version tokens) and the write semantics of creates and updates (conflict policy, idempotency, atomic writes) are those of the local backend; see [`local-files.md`](local-files.md). Errors from the backend pass through unchanged as `[local_files:<code>]` messages.

## Assets And Links

An asset is stored by the backend below `assets_dir` (default `assets`, set through `local_backend.assets_dir`) with a content-hashed name, for example `assets/diagram--3f2a9c1d0b7e4a65.png`. The asset reference issued to plugins is **document-relative** (`assets/diagram--3f2a9c1d0b7e4a65.png`): every page of a flat wiki lives at the root, so the same relative link works everywhere.

Two forms are refused because they break on private wikis:

- root-anchored references such as `/assets/diagram.png` (`link.root_anchored`)
- `raw.githubusercontent.com` URLs (`link.raw_url`)

Page content authored by a plugin is never scanned or rewritten by the provider; the guard applies to the references the backend reports. A backend asset reference that is not a canonical root-relative path is `asset.ref_unsupported`.

Uploading an asset never commits: the file stays pending (recorded in the manifest) and rides the next committing apply, together with the pages that use it.

## Commit Behavior

With `allow_auto_commit: true` (default) an apply makes **at most one commit**:

- **Exact staging.** The stage set is the paths wikiops wrote plus its own earlier pending paths. They are staged with `git --literal-pathspecs add -- <paths>` and committed with the same paths, so a foreign staged, ignored, or untracked file never rides along. There is no `add -A`, `add .`, or `commit -a`, and glob characters in a page name are literal.
- **Idempotent.** Content identical to HEAD produces no commit. The commit message template is rendered only when there is something to commit.
- **Identity.** `git` mode uses the clone's or the user's `user.name` and `user.email` (`commit.identity_missing` when none is configured). `bot` commits as `wikiops <wikiops@users.noreply.github.com>` unless `name` or `email` is given. `custom` requires a non-empty `name` and `email` (`config.identity_incomplete`). `bot` and `custom` pass the identity for that one commit only; the clone's git configuration is never modified.
- **Message.** `commit.message` accepts only `{plugin_id}`, `{provider_name}`, and `{page_count}`; any other placeholder, a format spec, or an empty message is `config.invalid_message`.
- **Hooks.** Local commands keep the user's own git hooks (for example `pre-commit`). A failing hook is `commit.failed`: the written paths stay pending and are committed by the next apply.

With `allow_auto_commit: false` the pages are written into the clone and left uncommitted; each result says `written to '<workdir>', not committed (allow_auto_commit=false)` and the paths stay pending for a later committing apply.

## Push Behavior

`allow_auto_push` defaults to `false`: with commit on and push off, **local commits accumulate** in the clone and wikiops never publishes them. If someone edits the wiki in the web UI meanwhile, the next sync reports `sync.diverged`. Either enable `allow_auto_push`, or push yourself before other people edit, or reconcile in the workdir.

With `allow_auto_push: true` (which requires `allow_auto_commit: true`, otherwise `config.push_requires_commit`) the apply pushes with `git push --porcelain origin HEAD:refs/heads/<branch>` after committing, including earlier unpushed commits. A push is:

- never forced, never retried, and never preceded by a rebase or pull
- skipped when there is nothing new and nothing unpushed

When the remote advanced after the sync, the push fails with `push.rejected`. The local commit is kept (the message names its short sha and the workdir), every operation written in that apply is reported as failed, and the next run stops with `sync.diverged` until you reconcile the history in the workdir.

## Hooks On Network Commands

Network commands (`ls-remote`, `clone`, `fetch`, `push`) run with git hooks **disabled** (`core.hooksPath` points to an empty temporary directory) and with any inherited `GIT_CONFIG_PARAMETERS` dropped, so no hook or `-c` entry can observe or replace a credential. Local commands (`status`, `add`, `commit`, `merge`, `rev-parse`) never receive credentials and keep the user's hooks.

All commands run with the repository-selection variables (`GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`) unset, the locale pinned to `C`, and `GIT_CEILING_DIRECTORIES` at the workdir's parent, so git cannot wander into an enclosing repository.

## Plan Note

When the provider can describe its target, the host adds a `provider_target` note at the top of every plan and apply (see [`../guides/using-the-cli.md`](../guides/using-the-cli.md#provider-target-note)):

```text
Provider 'wiki' (github_wiki) target: remote='https://github.com/acme/platform.wiki.git' workdir='/home/me/.cache/wikiops/github_wiki/github.com/acme/platform/p-wiki' branch=master auth=env:GITHUB_TOKEN auto_commit=true auto_push=false sync_on_plan=true backend=local_files pending_paths=0 unpushed_commits=0
```

`branch=auto` appears until the branch is known; `pending_paths` and `unpushed_commits` appear once a clone exists. Building the note never touches the network, never issues a credential, and creates nothing. With `sync_on_plan: false` it also carries the `sync.stale_plan` warning. Read it before `--apply`: if `workdir=` or `remote=` is not what you meant, stop and fix the configuration.

## Multiple Profiles

State is per provider instance. Two profiles use separate clones, manifests, and locks, and never share a token; foreign changes in one clone do not block the other. Two profiles that point at the same explicit `workdir` are serialized by the lock, and a profile with a different transport or branch is refused (`sync.remote_mismatch`, `sync.branch_mismatch`).

## Error Codes

Every provider error is rendered as:

```text
[github_wiki:<code>] <summary>. key='value'.... Hint: <action>.
```

Context entries (`workdir`, `path`, `sha`, ...) appear only when they apply, and summaries, hints and values are folded onto one line, so the first line of a message is always a single logical message. Automation can match the code with:

```text
^\[(?P<ns>[a-z_]+):(?P<code>[a-z_]+(\.[a-z_]+)*)\]
```

Failures during apply appear as `FAILED` entries in `=== APPLY RESULT ===`; the CLI then exits with code `1`. `sync.stale_plan` is a warning (kind `W`) appended to a plan note; every other code is an error.

| Code | Meaning | Fix |
| --- | --- | --- |
| `config.invalid` | A setting is unknown, missing, or has an invalid value | Fix the setting named in the message; valid keys or variants are listed |
| `config.invalid_repository` | `repository` is not `owner/repo` (or carries `.wiki`/`.git`) | Use `owner/repo` without suffix, scheme, or credentials |
| `config.invalid_host` | `host` is not a bare hostname | Use a hostname such as `github.com` |
| `config.invalid_branch` | `branch` is not a valid git branch name | Use a valid branch name such as `master` |
| `config.invalid_message` | `commit.message` is empty, malformed, or uses an unknown placeholder | Use only `{plugin_id}`, `{provider_name}`, `{page_count}` |
| `config.push_requires_commit` | `allow_auto_push: true` with `allow_auto_commit: false` | Enable auto commit or disable auto push |
| `config.identity_incomplete` | `commit.identity` mode `custom` (or a blank `bot` name) lacks a name or email | Provide both |
| `config.backend_recursion` | `local_backend.type` is `github_wiki` | Use a file backend such as `local_files` |
| `config.backend_root_forbidden` | `local_backend` sets `root`, `provider_name`, or `provider_api_version` | Remove them; the root is injected from `workdir` |
| `config.backend_unknown` | `local_backend.type` is not a registered provider | Use one of the ids listed in the hint |
| `config.backend_invalid` | The backend options are invalid or the backend has no `root` setting | Fix the `local_backend` options |
| `config.backend_incompatible` | The backend lacks capabilities the wiki needs | Choose another backend |
| `config.git_unavailable` | `git` is not on `PATH` | Install git |
| `config.key_path_missing` | `auth.key_path` does not exist | Point it to an existing key file |
| `auth.env_missing` | The token variable is unset, empty, or blank | Export it, or pick another auth mode |
| `auth.gh_unavailable` | `gh` is not on `PATH` | Install the GitHub CLI or pick another mode |
| `auth.gh_failed` | `gh` could not produce a token (not logged in, wrong account or host, timeout) | Run `gh auth login`; check the account and host |
| `auth.rejected` | The remote refused the credential | Check the credential, its scope or key, or choose an explicit mode |
| `auth.ssh_host_key` | The SSH host key is unknown or changed | Connect once with `ssh` to trust the host; wikiops never accepts host keys |
| `wiki.not_initialized` | The wiki repository was not found | Create the first page in the web UI, enable Wikis, check access, owner, repo, and host |
| `network.unreachable` | The host could not be reached | Check the network and the `host` setting |
| `sync.no_local_clone` | An offline plan found no clone | Run once with `sync_on_plan: true` (or apply) |
| `sync.workdir_not_clone` | The workdir is non-empty and not a clone of this wiki | Choose an empty directory or an existing clone |
| `sync.remote_mismatch` | The clone's `origin` is another wiki or transport | Use another `workdir`, or restore the auth mode of the clone |
| `sync.branch_mismatch` | Another branch is checked out | Check out the expected branch or change `branch` |
| `sync.branch_not_found` | The branch does not exist on the remote or in the clone | Set `branch` to an available one |
| `sync.diverged` | Local and remote history cannot be fast-forwarded | Reconcile manually in the workdir |
| `sync.timeout` | A git command exceeded `git_timeout_seconds` | Raise the limit or check the network |
| `sync.git_failed` | Another git command failed | Inspect the git output and the workdir |
| `sync.stale_plan` | Warning: the plan read the clone without fetching | Set `sync_on_plan: true` or apply |
| `workdir.unusable` | The workdir (or its state directory) cannot be used | Choose a writable directory |
| `workdir.manifest_corrupt` | The pending manifest is unreadable | Delete the manifest file named in the message |
| `workdir.dirty` | Foreign changes exist in the clone | Commit, stash, or discard the listed paths, or change `workdir` |
| `workdir.locked` | Another wikiops run holds the clone's lock | Wait for it and retry |
| `path.nested_not_supported` | The page path contains a separator | Use the flat name from the hint |
| `path.reserved` | A `.git` segment in the path | Choose another page name |
| `path.invalid_name` | The name is blank before `.md`, has control characters, or has leading or trailing whitespace | Use the corrected name from the hint |
| `path.not_markdown` | The page path does not end in `.md` | Use the name from the hint |
| `ref.unsupported_kind` | The ref is not `kind: path` | Use path refs |
| `ref.missing_path` | `locator.path` is missing or blank, or the backend reported no page reference | Set `locator.path`; a backend must report `resolved_ref` |
| `title.invalid` | The title cannot become a page name | Use a plain title or set an explicit `ref` |
| `asset.ref_unsupported` | The asset reference is not a canonical root-relative path | Use a path asset reference under the wiki root |
| `link.root_anchored` | A reported link starts with `/` | Use a document-relative link |
| `link.raw_url` | A reported link is a `raw.githubusercontent.com` URL | Use a relative link |
| `commit.identity_missing` | git has no `user.name`/`user.email` | Configure them or use `bot`/`custom` identity |
| `commit.failed` | `git add` or `git commit` failed (for example a hook), or a written path is ignored by git | Inspect the workdir; the paths stay pending |
| `push.rejected` | The remote advanced since the sync | Reconcile in the workdir, then re-run |
| `push.failed` | The push failed for another reason | Inspect the push output and the local commit |

## Current Implementation Boundaries

- path refs only, Markdown pages (`.md`) at the wiki root
- no sidebar or footer generation; `generate_sidebar` is not available and is planned as a later release
- no delete, move, or rename operations
- no plan-time warning for a create that conflicts with an existing page
- no rebase, merge commit, or force push, ever; history problems are reported and left to you
- no wiki bootstrap by push
- no real-network credential path is covered by the automated tests (git is exercised against local `file://` repositories; real hosts are covered by unit tests of the classifier and strategies)

## Related Documentation

- [`../configuration.md`](../configuration.md)
- [`../guides/using-the-cli.md`](../guides/using-the-cli.md)
- [`../guides/build-a-provider.md`](../guides/build-a-provider.md)
- [`internal-boundaries.md`](internal-boundaries.md)
- [`local-files.md`](local-files.md)
- [`azure-devops-wiki.md`](azure-devops-wiki.md)
