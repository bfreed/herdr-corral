# Corral for Herdr Desktop — sketch

Status: sketch, not built. Read against herdr-app `e77f112` (2026-11-04) and Corral v1.1.0.

## The problem

Herdr Desktop (`herdr-app`, built on herdr-core) has its own "New worktree" in the space menu and the **+** menu. It does a bare `git worktree add` into `~/.herdr/worktrees/<repo>/<branch>` from the current `HEAD`, then opens an empty space. Nothing else happens.

Corral does not run there:

- Desktop has no TUI plugins. Its extensions are JavaScript "mods" in `~/.config/herdr-app/mods/`.
- Desktop sends no "worktree created" event that anything could listen to.
- `hwt` drives the TUI's `herdr` binary, so its workspaces and tabs land in the TUI, not in Desktop.

What we lose: `.env` copies, `node_modules` setup, the agent/shell/server tabs, the agent start, the port lease, branching from the configured base (`origin/develop` for katapult-lidar) and merge-checked cleanup.

## The shape

**`hwt` keeps doing all the work; the mod is only a dialog.** Agents create worktrees by running `hwt new … --background` from a shell, so the command line has to work in Desktop anyway. If the logic lived in the mod, agents and the dialog would behave differently.

Two pieces:

1. **Corral learns to drive Desktop.** A second adapter beside the existing `Herdr` class, picked by a config setting.
2. **A `corral` mod** that asks for a branch name, runs `hwt new`, and shows the result as a toast. It's opened with a key, like `prefix+u` today.

## Piece 1: Corral's Desktop adapter

Config, in `~/.config/herdr-corral/config.toml`:

```toml
frontend = "desktop"   # "tui" (today's behavior, the default) or "desktop"
```

There's also a `--frontend` flag for testing. Make it explicit, not auto-detected: both programs can be installed and running at once.

A `HerdrApp` class with the methods `cmd_new`, `cmd_open`, `bootstrap`/`ensure_layout` and cleanup already call on `Herdr`. Each maps to a `herdr-app do <command>` or `hc` call:

| Corral needs | TUI today | Desktop |
|---|---|---|
| make the worktree | `herdr worktree create --base …` | Corral runs `git worktree add -b <branch> <path> <base>` itself, after the same fetch and base check |
| make the space | (part of the above) | `herdr-app do space.new --folder <path> --tab agent --argv '[…claude]'`, returns `{space, tab, terminal}`. Leave it unnamed so it nests under its repo in the sidebar |
| shell and server tabs | `herdr tab create --label shell` | `herdr-app do tab.new --space <id> --name shell --folder <path>` (and `server`) |
| start the agent | `herdr agent start` | the `--argv` on the first tab |
| find an existing space for a path | `herdr workspace list` / `get` | `herdr-app ls`, matching each space's folder and its panes' working folders |
| focus | `herdr workspace focus` | `herdr-app do space.select --id <id>` |
| close on cleanup | TUI workspace close | `herdr-app do space.close --id <id> --confirmed true` |
| per-tab env (`PORT`, `HWT_DEV_PORT`, `HWT_REMOTE_URL`, the repo's `environment`) | `--env` on `tab create` | **Not supported**: Desktop terminals get the core's environment. Wrap the agent's argv as `env PORT=… HWT_DEV_PORT=… claude`. The shell and server tabs go without; `hwt dev` and `hwt status` read the port lease from Corral's state, so they still work |
| reminder lines in the shell/server panes | `show_reminder` | none from the command line; `hwt new` prints them in its JSON and the mod shows them as a toast |
| workspace metadata (`dev_port`, `dev_url`) | `herdr workspace metadata` | none; the lease in Corral's state is the record |

Worktree path: no change. `worktree_root/<repo>/<slug>-<hash>` already sits under `~/.herdr/worktrees`, and Desktop's "Open worktree" lists every `git worktree list` entry, so Corral's worktrees appear there too.

## Piece 2: the `corral` mod

`~/.config/herdr-app/mods/corral/`, under our own name, so Herdr updates never replace it. (Updates replace shipped mods whole and move any edits aside to `<name>-edited`.)

`mod.json`:

```json
{ "place": "none", "title": "Corral",
  "overlay": { "kind": "dialog", "width": 460, "height": 300 } }
```

`keys.json` entry (any free key):

```json
"ctrl-shift-u": "mod.open:corral"
```

What the dialog does:

- Works out the repo from the focused space's first terminal folder, the way the shipped `worktree` mod does.
- **New**: branch name input; Enter runs `hwt new <branch> --repo <repo top> --background`. Shows the base it will use, from a new `hwt list --json`.
- **Clean up** (only on a worktree space): runs `hwt cleanup <path>`. That's merge-checked, same as the TUI action.
- **Open**: runs `hwt open` on a picked worktree. Corral's open re-runs bootstrap (tabs, lease) for a worktree that has no space yet.
- On success: `notify({ title: "Corral", body: "<repo>/<branch>: port 4112 — http://hermes…:4112" })` and close.
- On failure: the error stays in the dialog, red.

Untested draft of the core of `main.js`:

```js
import { View, div } from "gpui-kit";
import { v_flex, InputState, Input } from "gpui-base";
import { exec, focused, panes, spaces, notify, closeOverlay, props, theme } from "herdr";

export default class Corral extends View {
  init(_props, cx) {
    this.error = ""; this.busy = false; this.repo = null;
    this.input = InputState.new({ placeholder: "branch name" });
    this.input.on("submit", (_e, cx) => this.create(cx));
    this.input.focus();
    cx.spawn(async (cx) => {
      const folder = this.folder();
      const r = folder && await exec("git", ["rev-parse", "--show-toplevel"], { cwd: folder });
      this.repo = r?.code === 0 ? r.stdout.trim() : null;
      cx.notify();
    });
  }

  // Where the focused space is now: its first terminal's live folder.
  folder() {
    const s = spaces().find((x) => x.id === focused()?.space);
    const tabs = new Set((s?.tabs ?? []).map((t) => t.id));
    return panes().find((p) => tabs.has(p.tab) && p.cwd)?.cwd ?? s?.folder ?? null;
  }

  create(cx) {
    const branch = this.input.value().trim();
    if (!branch || !this.repo || this.busy) return;
    this.busy = true; this.error = ""; cx.notify();
    cx.spawn(async (cx) => {
      // 10 minutes is exec's cap; Corral sets up tabs before installing dependencies.
      const r = await exec("hwt", ["new", branch, "--repo", this.repo, "--background"], { timeout: 600_000 })
        .catch((e) => ({ code: 1, stderr: String(e) }));
      this.busy = false;
      if (r.code !== 0) { this.error = (r.stderr || r.stdout || "hwt new failed").trim(); cx.notify(); return; }
      const out = JSON.parse(r.stdout);
      notify({ title: "Corral", body: `${out.repository}/${out.branch}: port ${out.port} — ${out.dev_url}` });
      closeOverlay(props().overlay);
    });
  }

  render() {
    const t = theme();
    return v_flex().size_full().p(16).gap(10).bg(t.popover).text_color(t.foreground)
      .child(div().font_semibold().child(this.repo ? `New worktree of ${this.repo.split("/").pop()}` : "Not in a Corral repository"))
      .child(Input.new(this.input))
      .when(this.busy, (el) => el.child(div().text_color(t.dim).frames(["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"], 90)))
      .when(Boolean(this.error), (el) => el.child(div().text_color(t.blocked).child(this.error)));
  }
}
```

Check with `herdr-app check ~/.config/herdr-app/mods/corral` before using it.

## Piece 3: the right-click menu

The space menu and the **+** menu open the shipped `worktree` dialog by name. That menu (`menu`) is opened by name from `spaces`, `tabs` and `terminals-bar`. There's no hook for adding items. The choices:

- **a. Key only (recommended to start).** `ctrl-shift-u` opens Corral; the built-in "New worktree" stays as plain git. No upkeep.
- **b. Clone the menus.** Clone `menu` and `new-menu` so "New worktree" opens `corral`, then clone `spaces`, `tabs`, `terminals-bar` and the layout so they open the clones. That's five or six forked mods to re-merge on every Herdr release.
- **c. Ask Herdr for a hook.** For example, a setting that sends "New worktree" to a named mod, or the `worktree` mod running a command after `git worktree add`. That would make **b** unnecessary.

Recommendation: start with **a**, and file **c** upstream.

## To check before building

1. Does `herdr-app do` work with the window closed? The guide says structural commands fail while a machine's layout is unavailable. If it needs the window, agents running `hwt new` with Desktop closed get a clear error, not a half-made worktree.
2. The output format of `herdr-app ls`, and whether it reports pane folders (for finding an existing space).
3. Whether `space.new --tab agent --argv …` names the first tab "agent", as the shipped mod's `{branch, tab, argv}` suggests.
4. Whether `mod.open` opens the mod as the dialog its `mod.json` `overlay` describes; the shipped `picker` does it this way.
5. Desktop isn't installed on this machine yet (no `herdr-app`, no `~/.config/herdr-app`; `hc` is present).

## Not changing

- Worktree folders and the naming slug.
- `hwt cleanup`, `hwt sweep`, `hwt reap`, port ranges, repo overlays in `repos.d/`.
- The TUI path: `frontend = "tui"` stays the default until Desktop is the daily driver.
