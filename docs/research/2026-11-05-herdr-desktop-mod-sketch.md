# Corral for Herdr Desktop — sketch

Status: sketch, not built. Read against herdr-app `e77f112` (2026-11-04) and Corral v1.1.0.

## The problem

Herdr Desktop (`herdr-app`, built on herdr-core) has its own "New worktree" in the space menu and the **+** menu. It does a bare `git worktree add` into `~/.herdr/worktrees/<repo>/<branch>` from the current `HEAD`, then opens an empty space. Nothing else happens.

Corral does not run there:

- Desktop has no TUI plugins. Its extensions are JavaScript "mods" in `~/.config/herdr-app/mods/` on the computer where the window runs.
- Desktop sends no "worktree created" event that anything could listen to.
- `hwt` drives the TUI's `herdr` binary, so its workspaces and tabs land in the TUI, not in Desktop.

What we lose: `.env` copies, `node_modules` setup, the agent/shell/server tabs, the agent start, the port lease, branching from the configured base (`origin/develop` for katapult-lidar) and merge-checked cleanup.

The setup this is for: the Desktop window runs on a laptop, and each dev PC or VPS is added under Settings → Machines. Spaces for a machine run on that machine's herdr-core, reached over SSH. `herdr-app` isn't installed on those machines.

## The shape

- **`hwt` does all the work, on the machine where the code is.** Agents create worktrees by running `hwt new … --background` from a shell, so the command line has to work without the window. If the logic lived in the mod, agents and the panel would behave differently.
- **A Corral panel in the Desktop window is the GUI.** It follows the focused space, shows its Corral state, and has buttons for everything. It runs `hwt` on that space's machine, and installs and sets up Corral there the first time.

## Piece 1: `hwt` drives herdr-core (`frontend = "desktop"`)

Config, in `~/.config/herdr-corral/config.toml`:

```toml
frontend = "desktop"   # "tui" (today's behavior, the default) or "desktop"
```

There's also a `--frontend` flag for testing. Make it explicit, not auto-detected: both programs can be installed and running at once.

A `HerdrCore` class with the methods `cmd_new`, `cmd_open`, `bootstrap`/`ensure_layout` and cleanup already call on `Herdr`. It works only through `hc`, the machine's own herdr-core, so it works with the window closed or on another computer.

The route: Desktop shows any core terminal labelled `herdr.space=<id>` and `herdr.tab=<id>`, and makes a new space or tab for an id it doesn't know. Seen on a live Desktop terminal (`hc get <t> --json`): `"herdr.space": "sp<32 hex>"`, `"herdr.tab": "tb<32 hex>"`. `hc ls --json` doesn't print labels; use `hc get` or `hc ls --label k=v`.

| Corral needs | TUI today | Desktop (through `hc`) |
|---|---|---|
| make the worktree | `herdr worktree create --base …` | Corral runs `git worktree add -b <branch> <path> <base>` itself, after the same fetch and base check |
| make the space and its agent tab | (part of the above) | `hc run --keep --cwd <path> --label herdr.space=sp<new> --label herdr.tab=tb<new> -- env PORT=… claude`. Unnamed, so it nests under its repo in the sidebar |
| shell and server tabs | `herdr tab create --label shell` | the same `hc run`, same space id, a new tab id each, running a shell |
| start the agent | `herdr agent start` | the command on the first tab |
| find an existing space for a path | `herdr workspace list` / `get` | `hc ls --json`, then `hc get` on each terminal; match `cwd` or `foreground.cwd` and read its `herdr.space` |
| focus | `herdr workspace focus` | none from `hc`: focus belongs to each screen. The panel selects the new space itself (`space.select`) when you created it there |
| close on cleanup | TUI workspace close | `hc kill` every terminal labelled with that space; the last one closing closes the space |
| per-tab env (`PORT`, `HWT_DEV_PORT`, `HWT_REMOTE_URL`, the repo's `environment`) | `--env` on `tab create` | **Not supported**: `hc run` has no `--env`, and Desktop terminals get the core's environment. Wrap the agent's command as `env PORT=… HWT_DEV_PORT=… claude`. The shell and server tabs go without; `hwt dev` and `hwt status` read the port lease from Corral's state, so they still work |
| reminder lines in the shell/server panes | `show_reminder` | none; the panel shows the port, dev URL and commands instead |
| workspace metadata (`dev_port`, `dev_url`) | `herdr workspace metadata` | none; the lease in Corral's state is the record |

Worktree path: no change. `worktree_root/<repo>/<slug>-<hash>` already sits under `~/.herdr/worktrees`, and Desktop's "Open worktree" lists every `git worktree list` entry, so Corral's worktrees appear there too.

Other `hwt` changes the panel needs:

- **`install.py --desktop`**: skips the TUI plugin link, doesn't require the TUI's `herdr` binary, and writes `frontend = "desktop"`. Today's installer checks for the TUI and links the plugin.
- **`hwt add <path> [--base <ref>]`**: adds a repo with the existing safe defaults, without questions. Today `hwt init` refuses to run outside a terminal, and `hwt new` refuses repos that aren't set up.
- **`--json` on `hwt doctor`, `hwt list` and `hwt status`**, so the panel reads state instead of parsing text.
- **A version the panel can check** (`hwt --version`), so it can offer `hwt update` instead of failing.

## Piece 2: the Corral panel

`~/.config/herdr-app/mods/corral/` on the laptop, under our own name so Herdr updates never replace it. (Updates replace shipped mods whole and move any edits aside to `<name>-edited`.)

`mod.json`:

```json
{ "place": "left", "size": 220, "title": "Corral" }
```

The first time Herdr finds the folder, it docks one copy into the current layout; after that it stays wherever the person drags or resizes it. `"left"` puts it in the sidebar area with Spaces and Agents; `"right"` or `"bottom"` work too.

The panel shows the **focused space** (`focused().space`, its machine, its first terminal's folder). Every `exec` passes `on: space.machine`, so `git` and `hwt` run on that machine. It runs commands through a login shell (`sh -lc`) so `~/.local/bin` is on the PATH, and calls `hwt` by its full path after an install.

What it shows, by state:

1. **Not a git folder.** "Corral works in git repositories." Nothing else.
2. **Corral isn't installed on this machine.** "Corral isn't installed on hermes" and an **Install Corral** button. Before running, it shows what will happen: clone Corral from its repo, pinned to a release tag, into `~/.local/share/herdr-corral`; put `hwt` in `~/.local/bin`; set the repos folder to the parent of this space's repo (e.g. `~/Documents/GitRepos`); run `install.py --desktop`. It checks for `python3` 3.11+ and `git` first; if either is missing, it says so and stops. It never installs system packages.
3. **Installed, but this repo isn't set up.** "Set up katapult-lidar" with one question: the base branch, suggested from the remote's default (`origin/HEAD`), changeable (for example to `origin/develop`). **Set up** runs `hwt add`. **More settings…** opens a new tab in this space running the existing `hwt init` questions.
4. **Corral is out of date.** An **Update Corral** button runs `hwt update`.
5. **Ready, on the repo's main checkout.** A branch-name box with **New worktree** (`hwt new <branch> --repo <top> --background`, then select the new space), a list of this repo's worktrees (click to open or focus), and the base branch shown.
6. **Ready, on a worktree.** Branch, port, dev URL (click to open), dev server running or not, and buttons: **Start dev server** (runs `hwt dev` in the server tab), **Clean up** (merge-checked `hwt cleanup`, with a `Dialog`-style confirm; refusals show why and delete nothing).

Messages use Desktop's `notify` toasts; failures stay in the panel in the error color until dismissed.

Long jobs: `exec` waits at most 10 minutes. Corral opens the tabs before installing dependencies, so `hwt new` returns well inside that.

A key also opens the same view as a dialog for people who hide the panel: `keys.json` `"ctrl-shift-u": "mod.open:corral"` with an `overlay` entry in `mod.json`.

## Piece 3: the right-click menus

The built-in space menu, **+** menu, tab-bar panel buttons (Files, Changes) and command palette can't take extra items:

- The `menu` mod is opened by name from `spaces`, `tabs` and `terminals-bar`, and its items are written in its code.
- The tab-bar panel buttons are written in the `tabs` mod (`files` and `changes` only).
- The palette lists a fixed set plus app commands; it hides `mod.open` and doesn't list mod commands.

Two things are open without copying Herdr's mods:

- **The terminal right-click menu.** A layout's root can name its own mod (`"terminal_menu": "corral-menu"`). This replaces the whole terminal menu, so it repeats Herdr's few items (split, zoom, close…) and adds Corral's. It needs a personal copy of the layout (`layout.clone main`). That copy keeps using Herdr's mods, which keep updating; only the layout tree stops getting Herdr's changes.
- **A floating drawer** (`floating` with `peek` on a layout node), as an alternative to the docked panel. It also needs a personal layout copy.

Recommendation: the docked panel first; it needs no copies of anything. Ask Herdr upstream for a way to add items to the space menu, e.g. menu items contributed by mods. Treat the terminal-menu copy as optional.

## Coworker setup

1. Install Herdr Desktop.
2. Add their machines under Settings → Machines.
3. Add the Corral mod on the laptop, once. Desktop can add a mod from a URL (its agent reads the files first, then installs the mod and its keys); if that flow can't take a repo subfolder, copy `desktop-mod/` to `~/.config/herdr-app/mods/corral`.

Then, in a space on any machine, the Corral panel offers to install Corral there and set up the repo. If they also write code on the laptop itself, the same panel installs Corral there.

## To check before building

1. **Docking persists.** That Herdr's first-sight docking into the shipped `main` layout survives restarts and updates (the guide says the layouts decide after the first time; shipped layouts are replaced on every start, and clicks are kept separately).
2. **Tab names.** `hc` can't rename tabs, so Corral's tabs show as 1, 2, 3, not agent/shell/server. Find out whether a label sets the name, or the panel renames them with `tab.rename` after `hwt new` returns the tab ids.
3. **Made-up space ids.** Whether `sp` + 32 random hex is accepted as-is, and whether all its tabs land in one space when started a moment apart.
4. **Remote PATH.** Whether `exec(…, { on })` runs a login shell; if not, keep the `sh -lc` wrapping.
5. **Add mod from URL.** Whether it can take a subfolder of the herdr-corral repo.

## Not changing

- Worktree folders and the naming slug.
- `hwt cleanup`, `hwt sweep`, `hwt reap`, port ranges, repo overlays in `repos.d/`.
- The TUI path: `frontend = "tui"` stays the default for TUI users.
