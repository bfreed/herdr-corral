# Corral

Herd your Git worktrees in [Herdr](https://herdr.dev). Corral is a Herdr plugin plus a small `hwt` CLI that replaces a [workmux](https://github.com/raine/workmux)-style workflow for people who moved from tmux to Herdr:

- **New worktree, ready to work.** Creating a worktree copies your untracked `.env` / `.env.*` files, sets up dependencies (copy-on-write clone of the main checkout's `node_modules` when lockfiles match — near-instant on btrfs/XFS/APFS — otherwise an install via the lockfile-detected package manager), and opens exactly three tabs: `agent` (your coding agent, started automatically), `shell`, and `server`.
- **Stable dev ports.** Every worktree leases a port from a configured range (default 4100–4199), so parallel dev servers never collide and URLs stay predictable.
- **HTTPS dev URLs over Tailscale, optionally started for you.** With `[https] provider = "tailscale"`, each leased port is also served at `https://<machine>.<tailnet>.ts.net:<port + 1000>` with a real certificate, so browser APIs that need a secure context work from any computer on your tailnet. With `start_dev = true`, a new worktree's dev server starts by itself. See [HTTPS dev servers](#https-dev-servers).
- **Merge-safe cleanup.** `hwt cleanup` deletes the worktree, the local branch, and the remote branch — without questions when the branch is provably merged into your base branch, and only with an explicit `--abandon --confirm BRANCH` when it is not.
- **Agent-first.** Built for running one coding agent per worktree (Hermes, Claude Code, Codex, …) with isolated checkouts.

## Install (the easy way)

On the machine that runs Herdr, open your agent harness (Hermes, Claude Code, Codex, OpenCode, …) and say:

> Read https://raw.githubusercontent.com/bfreed/herdr-corral/main/AGENT_INSTRUCTIONS.md and follow it.

The agent will ask where your repositories live, install Corral, and verify the setup.

## Install (by hand)

Requirements: Linux, macOS or Windows 10/11; Python 3.11+; Git; [Herdr](https://herdr.dev) 0.8.0+ (the TUI, or Herdr Desktop with herdr-core). Standard library only, nothing to `pip install`.

```bash
git clone https://github.com/bfreed/herdr-corral ~/.local/share/herdr-corral
python3 ~/.local/share/herdr-corral/install.py --repos-root ~/repos
hwt doctor
```

On Windows (PowerShell), run the installer with the Python you want Corral to use (`py -3`, or `python`):

```powershell
git clone https://github.com/bfreed/herdr-corral $HOME\.local\share\herdr-corral
py -3 $HOME\.local\share\herdr-corral\install.py --repos-root $HOME\repos
hwt doctor   # in a new terminal, once ~/.local/bin is on your PATH
```

The clone is the installation — `hwt update` later updates it in place. The installer discovers the Git repositories directly under `--repos-root`, writes a conservative config, links the plugin, and puts an `hwt` launcher in `~/.local/bin` (on Windows `hwt.cmd`, which runs `hwt.py` with the Python that ran the installer; the installer adds `%USERPROFILE%\.local\bin` to your user PATH if it isn't there). See [Windows](#windows) for what differs there.

> Corral is also listed in the Herdr plugin marketplace. `herdr plugin install bfreed/herdr-corral` registers the event hooks, but the full experience (CLI, config, updates) comes from the clone + `install.py` flow above.

## Daily use

```bash
hwt new [feature/my-task]   # worktree + env files + deps + tabs, focused and ready
                            # omit the branch to pick repo, base, and name interactively;
                            # inside a worktree, offers to stack on its branch
hwt open [target]           # (re)open with the standard layout; omit to pick from a list
hwt dev                     # start the repo's dev server on the leased port
hwt list                    # configured repos and live worktrees
hwt status                  # port leases, lockfile drift, listening state
hwt reap [--dry-run]        # stop dev servers left running from deleted worktrees
hwt init                    # (re)configure the current repo interactively
```

Finishing work:

```bash
hwt remove feature/my-task                    # remove worktree, KEEP the branch
hwt cleanup feature/my-task                   # merged? delete worktree + local + remote branch
hwt cleanup feature/my-task --abandon --confirm feature/my-task
                                              # not merged / dirty? affirmatively discard it
```

`cleanup` fetches first and proves the merge before touching anything, refuses protected branches (`main`, `master`, `develop`, your configured base), stops the worktree's dev servers, and releases the port lease. A merge is proven by `git merge-base --is-ancestor`, or, for **squash and rebase merges** (which put different commits on base), by either of two content checks: merging the branch into base would change nothing (`git merge-tree`), or a base commit carries exactly the branch's whole diff (`git patch-id`, which still matches after base later edits the same lines). A commit made after the squash merge fails both checks, so the branch is kept. The guard protects **work that exists nowhere else**, not branch names: an unmerged branch whose every commit is reachable from some other ref — a fresh worktree branch with no commits of its own, or one fully rebased/cherry-picked elsewhere — is also cleaned up without questions. (The branch's own remote copy doesn't count as "elsewhere", since cleanup deletes that too.)

**Dev servers.** Before removing a checkout, `cleanup` stops every process listening from inside it or on its leased port, whether started by `hwt dev` in the server tab or by an agent in the background, along with their child processes. Each gets SIGTERM, then SIGKILL after 5 seconds, and the JSON result lists them under `stopped_servers`. Wrapper processes (`npm run`, `sh -c`) exit on their own, and shells, agents, and other users' processes are never touched. Deleting a checkout from Herdr's UI does the same through the `worktree.removed` hook. Servers that outlived an earlier removal keep running from the deleted folder: `hwt sweep` stops them, `hwt doctor` reports them, and `hwt reap` stops just those (`--dry-run` lists them). Process inspection uses `/proc` on Linux, `lsof` on macOS, and `netstat` plus the Windows process list on Windows (by port only there; see [Windows](#windows)).

No typing required, two ways:

- **`hwt sweep`** — cleans *every* worktree that qualifies without questions in one shot, and reports the kept ones with the exact `--abandon` command for each.
- **The Corral palette** — a popup (bind it to a key, below) listing every worktree with a safety annotation (`merged — cleans instantly`, `squash-merged — cleans instantly`, `nothing unique — cleans instantly`, `has unpublished commits`, `dirty`). Pick a number to clean it, `n` to create a worktree, `s` to sweep, `q` to close.

## Herdr Desktop

Corral also works with [Herdr Desktop](https://herdr.dev/), through each machine's herdr-core. Desktop's window can run on another computer: a space's worktree, ports and dev server live on the machine the space runs on.

1. Add the panel on the computer where Desktop's window runs, once. Either ask an agent in a Desktop space on that computer:

   > Add the Herdr mod at github.com/bfreed/herdr-corral/tree/main/desktop/corral

   or run, on macOS or Linux:

   ```sh
   curl -fsSL https://raw.githubusercontent.com/bfreed/herdr-corral/main/desktop/install.sh | sh
   ```

   or on Windows (PowerShell):

   ```powershell
   irm https://raw.githubusercontent.com/bfreed/herdr-corral/main/desktop/install.ps1 | iex
   ```

   The panel goes in `~/.config/herdr-app/mods/corral` (`$HERDR_APP_HOME` or `$XDG_CONFIG_HOME/herdr-app` if set; on Windows `%USERPROFILE%\.config\herdr-app`). Herdr docks a new panel on the left the first time it sees it, **but never into its own layouts** (`main`, the default, `terminals` and `win98`): those are read-only, and while one is showing the panel silently waits. So when one of them is showing, the installers first ask the open window for your own copy, `herdr-app do layout.clone --name main --as main-corral`, which also switches to it, and Herdr docks the panel there. The copy keeps using Herdr's panels, which keep updating; only its arrangement stops following Herdr's. If the window isn't open, the installer says to run that command (or **Duplicate layout** in Herdr's palette) later. To dock it by hand instead, add `{"id":"corral","type":"block","size":{"px":260},"content":{"mod":"corral"}}` as the first child of the root row in your own layout's `layouts/<name>.json`.

   After that the panel updates itself: when a machine's Corral ships a newer panel (after `hwt update` there), it offers **Update panel**.
2. Focus a space. If Corral isn't installed on that space's machine, the panel offers **Install Corral**. That needs git and Python 3.11+ there (on Windows, `py`, `python` or `python3`), clones this repository's release into `~/.local/share/herdr-corral`, and runs `install.py --desktop` with the repository's parent folder as the repositories folder. If the repository isn't set up, the panel offers **Set up** with a base-branch choice, plus **More settings…**, which opens `hwt init` in a new tab.
3. On a repository's main checkout, type a branch name and click **New worktree**. On a worktree, the panel shows its port and dev URL, starts the dev server, and offers **Clean up…** (merge-checked, as `hwt cleanup`). Every worktree in the list has a **×** for the same Clean up. When Corral keeps a branch because of unmerged commits or uncommitted changes, the panel says why and offers **Delete *branch* anyway** (`hwt cleanup --abandon`). The panel's sidebar button folds it to a narrow rail.

A space's machine may run Windows whether or not the window's computer does, and the reverse. The panel finds out per machine: it tries `sh`, and where there is none (or it is Git's), it runs programs directly and `hwt` as `python <clone>\hwt.py`, and remembers that for the machine.

TUI and Desktop work side by side; there's nothing to switch. `hwt` drives whichever Herdr it runs in: the TUI from a TUI pane (and for the TUI plugin's own hooks), Desktop from a Desktop terminal. Only in a shell outside both (ssh, cron) does `frontend` in `~/.config/herdr-corral/config.toml` decide (default `tui`). `--frontend` overrides it all; the panel always passes `--frontend desktop`.

Panel tests: `node --test desktop/tests/panel.test.mjs`. Check it as Herdr loads it: `herdr-app check ~/.config/herdr-app/mods/corral`. The panel ships as the same version as `hwt` (a test checks); `desktop/corral/` is laid out for Herdr's mod gallery (`RECIPE.md`, `preview.png`, `mods/corral/`).

## HTTPS dev servers

Opened from another computer as `http://<machine>:<port>`, a dev server isn't a [secure context](https://developer.mozilla.org/en-US/docs/Web/Security/Secure_Contexts). The browser then withholds Web Locks, service workers, the clipboard API, `crypto.subtle` and more, and apps that depend on them break. Only `http://localhost` is exempt. If Herdr runs on a machine in your [Tailscale](https://tailscale.com) tailnet, Corral can serve every worktree over HTTPS:

```toml
# ~/.config/herdr-corral/config.toml (top-level keys go before the first [table])
start_dev = true          # start hwt dev in the server tab of each new worktree

[https]
provider = "tailscale"    # default "off"
port_offset = 1000        # HTTPS port = leased port + 1000 (4120 -> 5120)
```

- **When a port is leased** (`hwt new`, `hwt open`, Herdr's own new-worktree dialog, `hwt dev`), Corral runs `tailscale serve --bg --https=<port+1000> http://127.0.0.1:<port>`. The URL `https://<machine>.<tailnet>.ts.net:<port+1000>` goes in the output of `hwt new` and `hwt status`, in the server tab, in `$HWT_HTTPS_URL` and `$HWT_REMOTE_URL`, and in the Corral panel. It uses the tailnet's own certificate and is reachable from the tailnet only (it is not Funnel). Vite's HMR websocket follows the page's origin, so live reload works through it.
- **It needs its own port.** Serve listens on the machine's Tailscale address, which a dev server bound to `0.0.0.0` already holds on its own port. That's why the HTTPS port is offset. The dev server must answer on `127.0.0.1`, which servers bound to `0.0.0.0` or `127.0.0.1` do. If your framework checks the Host header (Vite's `server.allowedHosts`), allow the `*.ts.net` name.
- **Cleanup** (`hwt cleanup`, `hwt remove`, deleting a worktree in Herdr) removes the mapping with the lease. `hwt doctor` reports mappings left by released ports and `hwt reap` removes them. Corral only adds or removes a mapping that is exactly its own (one `/` proxy to `127.0.0.1:<leased port>` at the offset). It leaves anything else you serve alone, and says so if one is in the way.
- **Setup, once:** HTTPS certificates must be enabled for the tailnet (admin console → DNS → HTTPS Certificates). On Linux your user must be allowed to change Serve without sudo: `sudo tailscale set --operator=$USER`. Without either, Corral warns and keeps serving HTTP, and nothing fails.
- **Starting the dev server:** with `start_dev` (global, or per repository to override it) and a configured dev command, Corral types `hwt dev` into a server tab it just opened, after dependencies are installed. It skips this when the install failed or the port is already in use, and reopening a worktree never starts a second server.

## Keybindings

Herdr's sidebar right-click menu is not extensible by plugins (as of 0.8.0 the plugin manifest's `contexts` field is parsed but not used by any UI — verified against the Herdr source), so Corral's interactive surface is the palette popup plus keybindings. Add to `~/.config/herdr/config.toml` and run `herdr server reload-config`:

```toml
[[keys.command]]
key = "prefix+w"
type = "plugin_action"
command = "corral.palette"
description = "Corral: worktree palette"

# optional: merge-checked cleanup of the worktree you're focused on, no UI
[[keys.command]]
key = "prefix+backspace"
type = "plugin_action"
command = "corral.cleanup"
description = "Corral: clean up the focused worktree"
```

Pick any free keys; the install agent offers to set this up. To try it without a binding: `herdr plugin action invoke corral.palette`. For `corral.cleanup`, refusals (unmerged/dirty) delete nothing and are shown in the worktree's `shell` tab.

## Configuration

- `~/.config/herdr-corral/config.toml` — global settings and discovered repositories.
- `~/.config/herdr-corral/repos.d/<repo>.toml` — per-repository overlays written by `hwt init`.

The first time you create a worktree for a repository, Corral offers to configure it (which env files to copy, whether to auto-install dependencies, the dev-server command). Repositories without explicit settings get safe defaults: copy untracked `.env` / `.env.*`, auto-detect the package manager, no dev server assumed, never a test tab, and no auto-started server unless you set `start_dev`.

**Worktree placement — unified by default:** Herdr's right-click "new worktree" and `hwt new` should create worktrees in the same place. Out of the box they do: `worktree_placement = "shared-root"` with `worktree_root` defaulting to `~/.herdr/worktrees` — the same directory Herdr's own `[worktrees].directory` uses — so both entry points produce `<root>/<repo>/<slug>` checkouts, and either kind gets the full bootstrap and `hwt cleanup` support. Prefer a visible location? Set Herdr's `[worktrees].directory` (in `~/.config/herdr/config.toml`, then `herdr server reload-config`) and Corral's `worktree_root` to the same folder. The workmux-style `worktree_placement = "sibling"` (`<repo>__worktrees/` next to each repo) remains available for `hwt new`, but note Herdr's own dialog can't follow a per-repo layout; Corral recognizes and bootstraps all of these locations regardless.

## Updating

```bash
hwt update      # git pull --ff-only + plugin relink (Windows: rewrites .windows-plugin first)
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

Linux, macOS and Windows 10/11, with the Herdr TUI (Herdr's Windows plugin support is a preview) or Herdr Desktop. CI runs the tests on all three.

### Windows

What's different there, and why:

- **The launcher** is `~/.local/bin/hwt.cmd`, which runs `"<python>" "<clone>\hwt.py"`. `hwt doctor` checks that `hwt` on PATH is that file.
- **The TUI plugin.** Herdr runs a hook's command as given, from the plugin's folder, and `python3` is rarely on a Windows PATH. One manifest can't name a different command per OS (Herdr refuses duplicate action ids even per platform), so on Windows `install.py` (and `hwt update`) writes a copy of `herdr-plugin.toml` to `<clone>\.windows-plugin\` whose commands run the installing Python and `hwt.py` by full path, and links that. Link the clone itself only on Linux and macOS.
- **Herdr's config** is read from `%APPDATA%\herdr\config.toml` (or `$XDG_CONFIG_HOME\herdr`, or `$HERDR_CONFIG_PATH`), as Herdr does. `%LOCALAPPDATA%\herdr` is Herdr's state, not its config. Herdr's worktrees default to `%USERPROFILE%\.herdr\worktrees`, as Corral's do.
- **Dev servers are found by port only.** Windows doesn't let one process read another's working folder, so cleanup stops whatever listens on the worktree's leased port (found with `netstat -ano`), with its child processes, but not a server on some other port started from inside the worktree, and `hwt reap` finds no orphans. Stopping is `taskkill /T`, then `taskkill /T /F` after 5 seconds; console programs (node, python) can only be ended forcefully, so they are at once. Windows also won't delete a folder a running program is in: stop such servers first if a cleanup reports the folder busy.
- **No copy-on-write.** The `clone` dependency policy makes a plain copy of `node_modules`. A `node_modules` holding links (pnpm's) isn't copied, since copying would follow them; the lockfile install runs instead. Very deep `node_modules` paths need Windows' long-path support turned on.
- **Links.** Symlinks need Developer Mode on Windows. Without it, a `shared` dependency folder or a `symlink` folder becomes a directory junction and a `symlink` file a copy. Before deleting a worktree, Corral removes the junctions it made, so the deletion can't reach through one into the main checkout. (Deleting a worktree some other way, such as Herdr's own Delete, doesn't get that step.)
- **Tabs.** New tabs run PowerShell (`pwsh`, else `powershell`, else `%COMSPEC%`), which sets the tab's `PORT`/`HWT_*` values. Herdr TUI reminders are printed with `Write-Host`; Herdr Desktop tabs get none (the panel shows the same).

## License

[MIT](LICENSE)
