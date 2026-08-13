# Corral

Herd your Git worktrees in [Herdr](https://herdr.dev). Corral is a Herdr plugin plus a small `hwt` CLI that replaces a [workmux](https://github.com/raine/workmux)-style workflow for people who moved from tmux to Herdr:

- **New worktree, ready to work.** Creating a worktree copies your untracked `.env` / `.env.*` files, installs dependencies (package manager detected from the lockfile), and opens exactly three tabs: `agent` (your coding agent, started automatically), `shell`, and `server`.
- **Stable dev ports.** Every worktree leases a port from a configured range (default 4100–4199), so parallel dev servers never collide and URLs stay predictable.
- **Merge-safe cleanup.** `hwt cleanup` deletes the worktree, the local branch, and the remote branch — without questions when the branch is provably merged into your base branch, and only with an explicit `--abandon --confirm BRANCH` when it is not.
- **Agent-first.** Built for running one coding agent per worktree (Hermes, Claude Code, Codex, …) with isolated checkouts.

## Install (the easy way)

On the machine that runs Herdr, open your agent harness (Hermes, Claude Code, Codex, OpenCode, …) and say:

> Read https://raw.githubusercontent.com/bfreed/herdr-corral/main/AGENT_INSTRUCTIONS.md and follow it.

The agent will ask where your repositories live, install Corral, and verify the setup.

## Install (by hand)

Requirements: Linux or macOS, Python 3.11+, Git, [Herdr](https://herdr.dev) 0.8.0+.

```bash
git clone https://github.com/bfreed/herdr-corral ~/.local/share/herdr-corral
python3 ~/.local/share/herdr-corral/install.py --repos-root ~/repos
hwt doctor
```

The clone is the installation — `hwt update` later updates it in place. The installer discovers the Git repositories directly under `--repos-root`, writes a conservative config, links the plugin, and puts an `hwt` launcher in `~/.local/bin`.

> Corral is also listed in the Herdr plugin marketplace. `herdr plugin install bfreed/herdr-corral` registers the event hooks, but the full experience (CLI, config, updates) comes from the clone + `install.py` flow above.

## Daily use

```bash
hwt new feature/my-task     # worktree + env files + deps + tabs, focused and ready
hwt open <repo-or-worktree> # (re)open with the standard layout, idempotently
hwt dev                     # start the repo's dev server on the leased port
hwt list                    # configured repos and live worktrees
hwt status                  # port leases, lockfile drift, listening state
hwt init                    # (re)configure the current repo interactively
```

Finishing work:

```bash
hwt remove feature/my-task                    # remove worktree, KEEP the branch
hwt cleanup feature/my-task                   # merged? delete worktree + local + remote branch
hwt cleanup feature/my-task --abandon --confirm feature/my-task
                                              # not merged / dirty? affirmatively discard it
```

`cleanup` fetches first and proves the merge with `git merge-base --is-ancestor` before touching anything, refuses protected branches (`main`, `master`, `develop`, your configured base), and releases the port lease.

## Configuration

- `~/.config/herdr-corral/config.toml` — global settings and discovered repositories.
- `~/.config/herdr-corral/repos.d/<repo>.toml` — per-repository overlays written by `hwt init`.

The first time you create a worktree for a repository, Corral offers to configure it (which env files to copy, whether to auto-install dependencies, the dev-server command). Repositories without explicit settings get safe defaults: copy untracked `.env` / `.env.*`, auto-detect the package manager, no dev server assumed, never a test tab, never an auto-started server.

**Worktree placement:** by default (`worktree_placement = "sibling"`) `hwt new` puts worktrees in `<repo>__worktrees/` next to each repository (the workmux layout, so pre-Corral worktrees are recognized, bootstrapped, and cleanable). Set `worktree_placement = "shared-root"` to collect them under `worktree_root/<repo>/` instead.

Worktrees created through **Herdr's own UI** (right-click → new worktree) land under Herdr's `[worktrees].directory` — default `~/.herdr/worktrees/<repo>/<slug>` — which Corral approves automatically, so those get the full bootstrap (env files, dependencies, tabs, port lease) and work with `hwt cleanup` too. To keep everything in one place, point Herdr's `[worktrees].directory` at Corral's `worktree_root` and set `worktree_placement = "shared-root"`.

## Updating

```bash
hwt update      # git pull --ff-only + plugin relink
hwt doctor      # also tells you when an update is available
```

## Uninstalling

```bash
python3 ~/.local/share/herdr-corral/install.py --uninstall --purge
rm -rf ~/.local/share/herdr-corral
```

Drop `--purge` to keep `~/.config/herdr-corral` for a later reinstall.

## Prior art

Corral overlaps with several fine single-purpose plugins — use them if you only need a slice: [herdr-worktree-seed](https://github.com/jlimas/herdr-worktree-seed) (env files + node_modules seeding), [herdr-plugin-workspace-manager](https://github.com/razajamil/herdr-plugin-workspace-manager) (declarative layouts), [herdr-plugin-git-worktree-hooks](https://github.com/freethinkel/herdr-plugin-git-worktree-hooks) and [herdr-worktree-lifecycle](https://github.com/qdentity/herdr-worktree-lifecycle) (generic lifecycle hooks). Corral's reason to exist is the integrated workflow plus merge-safe cleanup.

## Platform support

Linux and macOS. Windows is not supported yet (Herdr's own Windows support is beta); the Python code is written to be portable and contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
