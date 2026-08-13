# Changelog

## 0.16.1 — 2026-08-13

- Tab reminders no longer echo a `printf` command at the prompt: the message is written straight to the pane's tty (resolved via `pane process-info` + `/proc/<shell_pid>/fd/0`), styled dim so it reads as ambient guidance, followed by a bare Enter so the shell draws a fresh prompt below it. Hosts without `/proc` (macOS) automatically fall back to the previous printf-through-the-shell delivery.

## 0.16.0 — 2026-08-13

- Tab guidance is back, richer, and robust: the `server` tab always gets a message on creation — `hwt dev` + the leased port + local/remote URLs when a dev command is configured, or a pointer to `hwt init` when not — and the `shell` tab gets orientation (`hwt -h`, how to open the palette, plus the init suggestion for unconfigured repos).
- Reminder delivery fixed for non-`$` prompts: the prompt wait now matches `❯`, `%`, and `>` as well, and a failed wait or send can no longer break the bootstrap (previously the wait expected `$`/`#` only, so starship-style prompts timed it out).

## 0.15.3 — 2026-08-13

- Cleaning up or removing an open worktree no longer yanks Herdr's focus away: Herdr unconditionally switches to the removed worktree's parent repo workspace (verified in `app/worktrees.rs::close_removed_linked_worktree_workspace`), so Corral now records the focused workspace before removal and switches back afterward — unless you ran it from the workspace being removed.

## 0.15.2 — 2026-08-13

- Worktrees created by `hwt new` (and reopened by `hwt open`) now display in Herdr's sidebar exactly like right-click-created ones: just the branch name. We stopped passing `--label` — an explicit label becomes the workspace's `custom_name`, which disables Herdr's branch-derived display (verified in `ui/sidebar.rs::grouped_child_display_label`), and our `"repo: branch"` labels ate the whole panel width with the repo prefix.

## 0.15.1 — 2026-08-13

- Base-branch candidates fixed and reordered: the canonical checkout's own branch is no longer excluded (having `develop` checked out in the main clone was hiding `origin/develop` — the most wanted base), only branches checked out in *linked* worktrees are; and plain main-line names (`develop`, `main`, `staging`) sort before slash-namespaced topic branches so they always make the top-10 cut.

## 0.15.0 — 2026-08-13

- `hwt init` now asks for the repository's base branch (first question), offering the current setting plus the repository's remote branches; typed values are validated and must be remote-tracking refs (cleanup verifies merges against the base). Previously the installer's `origin/HEAD` guess was carried forward silently.
- Fixed `hwt new` failing with "command failed (herdr) with exit status 2": `herdr worktree create` rejects `--workspace` together with `--cwd` (verified from source), and we passed both. This call path had never succeeded.
- Failed external commands now include the first line of their stderr in the error message, so failures like the above diagnose themselves.

## 0.14.1 — 2026-08-13

- The dev-command suggestion in `hwt init` now wires the leased port properly per framework: vite/astro/webpack scripts get `-- --host {host} --port {port}`, Next.js gets `-- -H {host} -p {port}`, and frameworks that honor the PORT/HOST environment (CRA, Express-style, Nuxt) stay bare since `hwt dev` always exports those. The prompt now explains that the command is a template run by `hwt dev` on a per-worktree leased port.

## 0.14.0 — 2026-08-13

- The `hwt new` base picker also lists the repository's remote branches (up to 10, newest first, excluding `HEAD` and branches already checked out in a worktree).
- Base refs are validated: a typed "other" ref is checked on the spot with a re-prompt, and `hwt new`/`--base` verify the ref exists (after the fetch) before anything is created — no more half-created worktrees from a typo like `dev`.

## 0.13.0 — 2026-08-13

- `hwt new` with no arguments is now interactive and context-aware: inside a canonical repository it offers that checkout's current branch as the default base; inside a worktree it defaults to the configured base (a sibling worktree) with the option to stack the new branch on the current worktree's branch; outside any repository it offers a numbered repository picker, then a base picker, then prompts for the branch name.
- `hwt new <branch>` now also works from inside a worktree or a repo subdirectory (it maps to the canonical repository instead of erroring).
- The palette's `n`ew-worktree flow reuses the same interview.

## 0.12.0 — 2026-08-13

- `hwt open` works like `hwt cleanup` now: with no argument it offers a numbered picker (repositories, then worktrees grouped by repo); a target that doesn't resolve uniquely offers similar candidates instead of erroring.
- The `hwt cleanup` picker uses the palette's readable layout: repository group headers, two-line entries with colored safety annotations and dimmed, home-shortened paths.

## 0.11.1 — 2026-08-13

- `hwt -h` no longer shows `==SUPPRESS==` rows: the internal entry points (`event`, `cleanup-workspace`, `palette-open`, invoked by Herdr) are properly hidden from usage and the command table, and `list`/`status`/`doctor` gained help lines.

## 0.11.0 — 2026-08-13

- New default dependency policy `clone`: when the canonical checkout has a `node_modules` and its lockfile is byte-identical to the new worktree's, the directory is copied via copy-on-write (`cp --reflink=auto` on Linux, APFS clonefile on macOS, plain copy fallback) — near-instant and fully independent. Any mismatch or failure falls back to the lockfile-detected install (`auto` behavior). The init interview offers this as the default; existing `auto` configs behave as before.

## 0.10.3 — 2026-08-13

- Worktree identity is now decided by git, not path conventions: any checkout whose `git-common-dir` resolves inside a configured repository is recognized (cleanup, remove, open, event bootstrap), wherever it lives — including flat ad-hoc siblings like `<repos-root>/<repo>-<name>`. Approved-roots matching remains as fallback; worktrees of unconfigured repositories are still refused, and the huge roots list is gone from the error message.

## 0.10.2 — 2026-08-13

- Worktrees that are not open in Herdr can now be cleaned up and removed: Herdr's `worktree.remove` API is workspace-only (verified from source: `WorktreeRemoveParams { workspace_id, force }`), so closed checkouts are removed via `git worktree remove` directly. Fixes "Herdr worktree entry lacks an open workspace id or path" when picking a closed worktree in the palette.

## 0.10.1 — 2026-08-13

- Palette layout decluttered: worktrees grouped under repository headers, each as `N. branch [annotation]` with the path (home shortened to `~`) dimmed on its own indented line; annotations colored (green cleans-instantly, yellow unpublished, red dirty). Colors respect `NO_COLOR` and non-TTY output.

## 0.10.0 — 2026-08-13

- **`hwt sweep`**: batch cleanup — deletes every linked worktree that qualifies without questions (merged or nothing-unique, clean) and reports the kept ones with a ready-to-paste `--abandon` command each.
- **Corral palette**: a popup pane (`placement = "popup"`, real PTY) listing all worktrees with advisory safety annotations; number cleans, `n` creates a worktree, `s` sweeps, `q` closes. Open via a `[[keys.command]]` binding with `type = "plugin_action"` and `command = "corral.palette"`, or `herdr plugin action invoke corral.palette`. The install interview now offers to set the binding up (consent-gated, `herdr server reload-config` applied).
- Correction, verified against the Herdr source (0.8.x): the sidebar context menu is hardcoded and the plugin manifest's `contexts` field is parsed but unused by any UI — the previous README claim that the cleanup action appears in the right-click menu was wrong. `contexts` values are kept as forward-looking metadata.

## 0.9.7 — 2026-08-13

- `hwt cleanup` no longer demands `--abandon` for unmerged branches that have no commits of their own: if every commit on the branch is reachable from another ref (excluding the branch's own remote copy, which cleanup also deletes), it is deleted without questions. Fresh, never-committed worktree branches now clean up in one step. Cleanup output gains a `reason` field (`merged` / `no-unique-commits` / `abandoned`).

## 0.9.6 — 2026-08-13

- Repository discovery no longer registers linked worktrees found in the repos root as separate repositories (their git dir lives in the canonical repo).
- Worktree listings dedupe entries that appear under multiple configured repositories aliasing the same repo, preferring the canonical checkout — fixes the same worktree showing twice in the `hwt cleanup` picker.

## 0.9.5 — 2026-08-13

- `hwt cleanup` is now usable by humans: with no argument it lists linked worktrees as a numbered menu; with a target that has no exact match it lists similar worktrees (substring + close-match) the same way. Enter a number instead of typing branch names.
- Worktree identification no longer depends on Herdr's exact JSON field names: entries are normalized (nested `worktree` objects, `checkout_path`, alternative list keys) and missing branch/linked-ness is derived from git directly. This fixes `hwt cleanup <exact-branch>` failing with "must identify exactly one Herdr worktree".

## 0.9.4 — 2026-08-13

- Unified worktree placement is now the default: `worktree_placement = "shared-root"` with `worktree_root` defaulting to `~/.herdr/worktrees`, so Herdr's right-click dialog and `hwt new` create worktrees in the same `<root>/<repo>/<slug>` tree. The installer gained `--worktree-placement`; the agent interview now reads Herdr's `[worktrees].directory`, offers detected locations, and only edits Herdr's config with the user's explicit OK (applied via `herdr server reload-config`).
- New workspace-context action "Corral: clean up this worktree": merge-checked cleanup without typing the worktree name, resolved from `HERDR_WORKSPACE_ID`. Refusals (unmerged/dirty) delete nothing and are surfaced in the worktree's `shell` tab.

## 0.9.3 — 2026-08-13

- Herdr's native worktree directory (`~/.herdr/worktrees/<repo>/<slug>`, from its right-click "new worktree" flow) is approved automatically: those worktrees now get the full Corral bootstrap and work with `hwt open`/`remove`/`cleanup`.
- Docs: to unify locations entirely, set Herdr's `[worktrees].directory` to Corral's `worktree_root` with `worktree_placement = "shared-root"`. (Correction: the `<repo>__worktrees/` sibling layout is workmux's convention, not Herdr's.)

## 0.9.2 — 2026-08-13

- Corral now understands the workmux worktree layout: `<repo>__worktrees/` directories next to each configured repository are approved and repo-mapped automatically, so pre-existing worktrees there get bootstrapped by events and are addressable by `hwt open`/`remove`/`cleanup`.
- New global setting `worktree_placement` (default `"sibling"`): `hwt new` creates worktrees in `<repo>__worktrees/`; `"shared-root"` restores the `worktree_root/<repo>/` layout.
- `hwt open <name>` also matches worktree directory names verbatim, not only hwt's hashed slugs.

## 0.9.1 — 2026-08-13

Fixes from the first field install.

- Workspace discovery is hardened against Herdr JSON-shape differences (more path keys, list-level fields) and falls back to an exact label match, so re-running `hwt open`/`hwt new` no longer creates duplicate workspaces.
- A failed worktree creation no longer leaves an empty namespace directory behind.
- `install.py` excludes its own clone from repository discovery and gains `--uninstall` (and `--uninstall --purge`).
- Agent instructions now require user approval before the live workspace verification, and tell agents to stop and file a bug (not retry) if a duplicate workspace ever appears.

## 0.9.0 — 2026-08-13

Initial public release, evolved from a private worktree-workflow bundle.

- New worktrees copy untracked `.env` / `.env.*` files by default (`overwrite = "skip"`).
- Dependencies default to `policy = "auto"`: package manager detected from the lockfile (pnpm/yarn/bun/npm), installed non-fatally after tabs open.
- `hwt cleanup`: merged + clean branches are deleted (worktree, local and remote branch, port lease) without confirmation; unmerged or dirty work requires an explicit `--abandon --confirm BRANCH`.
- `hwt init`: interactive per-repository configuration written to `repos.d/` overlays; offered automatically on a repository's first worktree.
- `hwt update`: in-place `git pull --ff-only` + plugin relink; `hwt doctor` reports available updates.
- Configurable agent kind (`agent_kind`, default `hermes`).
- Portable file locking (Linux/macOS/Windows); plugin manifest targets Linux and macOS.
