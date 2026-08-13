# Changelog

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
