# Changelog

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
