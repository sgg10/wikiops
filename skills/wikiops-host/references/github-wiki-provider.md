# GitHub Wiki Provider Reference

This file explains how to configure and operate the built-in `github_wiki` provider, and how to react when it reports an error.

Provider ID (the `type` value in config): `github_wiki`. Verify it is installed with `wikiops providers`. Example config: [GitHub wiki config example](../assets/config.github-wiki.example.yaml). Full reference: `docs/reference/github-wiki.md` in the WikiOps repository.

## When to use it

Use `github_wiki` when the user wants documentation published to the wiki of a GitHub repository. The provider works through a **local git clone** of `<owner>/<repo>.wiki.git`: it syncs the clone, writes the pages into it through a local file backend, then commits (and optionally pushes) exactly the files it wrote. It does not use the GitHub API.

## Settings

- `repository` (required): `owner/repo`, without `.wiki`, `.git`, a scheme, or credentials.
- `host` (default `github.com`): bare hostname; set it for GitHub Enterprise Server.
- `branch` (default: the remote's default branch): override only if the wiki really uses another branch.
- `workdir` (default: a per-profile directory in the user cache): where the clone lives. A relative value resolves against the **working directory of the command**.
- `sync_on_plan` (default `true`): `false` makes plans read the existing clone without network access; apply always syncs.
- `auth` (default `ambient`): `env` (`variable`), `gh` (`account`), `ssh` (optional `key_path`), or `ambient`.
- `commit.identity` (default `git`): `git`, `bot`, or `custom` (needs `name` and `email`); `commit.message` accepts only `{plugin_id}`, `{provider_name}`, `{page_count}`.
- `allow_auto_commit` (default **`true`**) and `allow_auto_push` (default **`false`**; needs auto commit).
- `local_backend` (default `{type: local_files}`): extra keys are the backend's options such as `assets_dir`.
- `git_timeout_seconds` (default `120`, 5 to 3600).

Unknown keys are rejected with `config.invalid`; the message lists the valid keys.

## Rules you must follow

- Never put a token in the config file. For `auth.mode: env` write the **variable name** and ask the user to export it. If the variable is missing (`auth.env_missing`), stop and ask the user; do not switch auth modes on your own.
- Refs are **flat**: `Home.md`, `Release-Notes.md`. A path with `/` fails with `path.nested_not_supported`; use the flat name from the hint (`guides/setup.md` becomes `guides-setup.md`) only if the user agrees, because it changes the plugin's page names. Do not use Azure-style paths such as `/Engineering/Teams`.
- Do not set `allow_auto_push: true` on your own. Pushing publishes the wiki; enable it only when the user asks. Do not change `workdir`, `branch`, or `auth` to get past an error without asking.
- Never edit, stage, or commit files in the clone by hand while a run is in progress, and never delete the manifest or lock files without the user's agreement.
- Sidebar generation is not supported yet: do not configure or promise `generate_sidebar`.

## Before `--apply`: read the `provider_target` note

Every plan (and every apply, which plans first) starts `ChangeSet.notes` with a `provider_target` note:

```text
Provider 'wiki' (github_wiki) target: remote='https://github.com/acme/platform.wiki.git' workdir='/abs/cache/.../p-wiki' branch=master auth=env:GITHUB_TOKEN auto_commit=true auto_push=false sync_on_plan=true backend=local_files pending_paths=0 unpushed_commits=0
```

Check `remote=`, `workdir=`, `auto_commit=` and `auto_push=` before running `--apply`. Non-zero `pending_paths` or `unpushed_commits` mean earlier work is waiting in the clone and will ride the next commit or push. A `sync.stale_plan` warning means the plan did not fetch. If a `provider_target_unavailable` warning appears instead of the note, the target could not be confirmed (often `sync.remote_mismatch`); investigate before applying.

## Behavior to expect

- Apply makes at most one commit containing exactly the files it wrote plus its own earlier pending files. Identical content makes no commit. Local git hooks (for example `pre-commit`) run on the commit; network commands run with hooks disabled.
- With `allow_auto_push: false` (default) commits stay local. Each result says `committed locally at <sha> in '<workdir>'; not pushed`. Tell the user the wiki is not published yet. If the wiki is edited in the web UI meanwhile, the next run reports `sync.diverged`.
- With `allow_auto_commit: false` pages are only written to the clone and stay pending.
- A page the provider rejects after the backend wrote it is rolled back, confined to the paths that operation wrote (found by a `git status` snapshot taken before the write). Any other new path in the clone is left untouched and reported as foreign, and leftovers the rollback could not restore are named in the failed result; read them before re-running.
- Apply decides from `git status`: pages identical to HEAD stage and commit nothing. A page or asset a `.gitignore` or `.git/info/exclude` rule hides is never committed silently: its operation fails with `commit.failed`, names the path and says it is ignored by git. Remove the ignore rule or rename the page; the other pages of the apply are still committed.
- Assets are stored under `assets_dir` and linked document-relative (`assets/x--<hash>.png`); root-anchored links and `raw.githubusercontent.com` URLs are refused. An asset upload alone never commits.
- Concurrent runs on one clone fail fast with `workdir.locked`; do not run two applies against the same `workdir`.
- The first wiki page must exist already (created in the web UI); the provider cannot create an empty wiki.

## Reading a failure

Failures appear as `FAILED` results in `=== APPLY RESULT ===` (and the CLI exits with `1`), or as configuration errors during planning. Messages have this shape:

```text
[github_wiki:<code>] <summary>. key='value'.... Hint: <action>.
```

Match the code with `^\[(?P<ns>[a-z_]+):(?P<code>[a-z_]+(\.[a-z_]+)*)\]`, then react as below. Always read the `Hint:` first. Errors from the file backend keep their own `[local_files:<code>]` prefix; use the [Local files provider reference](local-files-provider.md) for those.

### Configuration

| Code | How to react |
| --- | --- |
| `config.invalid` | A setting is unknown, missing, or invalid. Fix the setting named in the message. |
| `config.invalid_repository` / `config.invalid_host` / `config.invalid_branch` | Use `owner/repo`, a bare hostname, or a valid branch name respectively. |
| `config.invalid_message` | Use only `{plugin_id}`, `{provider_name}`, `{page_count}` and a non-empty message. |
| `config.push_requires_commit` | `allow_auto_push` needs `allow_auto_commit: true`. Ask the user which one they want. |
| `config.identity_incomplete` | `commit.identity` mode `custom` needs both `name` and `email` (a `bot` name cannot be blank). |
| `config.backend_recursion` / `config.backend_root_forbidden` | `local_backend.type` cannot be `github_wiki`, and `local_backend` must not set `root`, `provider_name`, or `provider_api_version`. |
| `config.backend_unknown` / `config.backend_invalid` / `config.backend_incompatible` | Check `wikiops providers` and the `local_backend` options; the backend must accept a `root` and support page and asset writes. |
| `config.git_unavailable` | `git` is not on `PATH`. Ask the user to install it. |
| `config.key_path_missing` | `auth.key_path` does not exist. Fix the path. |

### Authentication and network

| Code | How to react |
| --- | --- |
| `auth.env_missing` | The token variable is unset, empty, or blank. Ask the user to export it. Never print or request the token value in chat. |
| `auth.gh_unavailable` / `auth.gh_failed` | Install the GitHub CLI, or ask the user to run `gh auth login` for the configured account and host. |
| `auth.rejected` | The remote refused the credential. Ask the user to check its scope (it must grant wiki write access) or key; do not retry in a loop. |
| `auth.ssh_host_key` | The SSH host key is unknown or changed. Ask the user to connect once with `ssh` to verify and trust it; wikiops never accepts host keys. |
| `wiki.not_initialized` | The wiki repository was not found: the wiki has no first page yet, Wikis are disabled for the repository, the owner/repo/host is wrong, or the credential has no access. Ask the user to **create the first page in the web UI** and check the other causes. |
| `network.unreachable` | Check connectivity and `host`. |
| `sync.timeout` | A git command exceeded `git_timeout_seconds`. Check the network before raising the limit. |
| `sync.git_failed` | Another git command failed. Read the redacted git output in the message. |

### Sync and workdir

| Code | How to react |
| --- | --- |
| `sync.no_local_clone` | An offline plan found no clone. Run once with `sync_on_plan: true`, or apply. |
| `sync.workdir_not_clone` | The workdir is non-empty and not a clone of this wiki. Choose an empty directory; never delete the directory yourself. |
| `sync.remote_mismatch` | The clone belongs to another wiki or transport. Use another `workdir` or restore the original auth mode. |
| `sync.branch_mismatch` / `sync.branch_not_found` | Another branch is checked out, or `branch` does not exist. Fix `branch` with the user; wikiops never switches branches. |
| `sync.diverged` | Local and remote history cannot be fast-forwarded (often after local unpushed commits plus a web edit, or a rejected push). Stop and tell the user to reconcile in the workdir; do not rebase, reset, or force-push for them without their explicit request. |
| `sync.stale_plan` | Warning: the plan did not fetch. Set `sync_on_plan: true` or apply, which always syncs. |
| `workdir.unusable` | The workdir cannot be used (blank, the home directory, the filesystem root, not writable, a file in the way). Choose a writable directory. |
| `workdir.dirty` | Foreign changes exist in the clone (paths are listed). Ask the user to commit, stash, or discard them, or use another `workdir`; never discard them yourself. |
| `workdir.manifest_corrupt` | The pending manifest is unreadable. Ask before deleting the file named in the message; its paths then count as foreign changes. |
| `workdir.locked` | Another wikiops run holds the clone. Wait and retry; the message names the holder. |

### Pages, titles, assets, and links

| Code | How to react |
| --- | --- |
| `path.nested_not_supported` | The path contains `/`. Use the flat name from the hint after the user agrees. |
| `path.reserved` | A `.git` segment in the path. Pick another name. |
| `path.invalid_name` | The page name is blank before `.md`, has a control character, or has leading or trailing whitespace. Use the corrected name from the hint after the user agrees. |
| `path.not_markdown` | Use the `.md` name from the hint. |
| `ref.unsupported_kind` / `ref.missing_path` | Use `kind: path` with a non-blank `locator.path`. |
| `title.invalid` | The title cannot become a page name. Give a plain title or set an explicit `ref`. |
| `asset.ref_unsupported` / `link.root_anchored` / `link.raw_url` | The backend reported an asset reference or link the wiki cannot use. Check the `local_backend` options; do not work around it with raw URLs. |

### Commit and push

| Code | How to react |
| --- | --- |
| `commit.identity_missing` | git has no `user.name`/`user.email`. Ask the user to configure them or choose `commit.identity` mode `bot` or `custom`. |
| `commit.failed` | `git add`/`git commit` failed (often a hook). The pages are written but not committed and stay pending; fix the cause and re-run. A message saying the path is ignored by git means a `.gitignore` or `info/exclude` rule hides it: remove the rule or rename the page. |
| `push.rejected` | The remote advanced after the sync. The local commit is kept. Tell the user to reconcile in the workdir; the next run reports `sync.diverged` until they do. |
| `push.failed` | The push failed for another reason. Read the redacted output; the local commit is kept. |

## After apply

1. Inspect `=== APPLY RESULT ===`; any `FAILED` entry means the run failed.
2. Read each message: `committed locally ... not pushed` means the wiki is **not published yet**.
3. Read pages back with `wikiops docs get --alias <alias>` (it syncs the clone first unless `sync_on_plan: false`).
4. Re-running the same apply should create no new commit. If it does, investigate before telling the user the run is done.
