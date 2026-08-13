# Changelog

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
