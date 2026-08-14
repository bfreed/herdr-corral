# Agent instructions: install Corral on this machine

You are an AI coding agent installing Corral — a Herdr plugin and `hwt` CLI for Git-worktree workflows — for the user who pointed you at this file.

These instructions define **what** to accomplish and the safety rules that are non-negotiable. For **how** to converse, use your harness's best affordances and your own judgment: ask one question at a time, prefer structured pickers over freeform prompts, and investigate the machine first so you offer detected options instead of making the user type. Where the instructions say to ask the user, ask instead of guessing.

## What you are installing

For every configured repository and each of its worktrees, Herdr gets exactly these tabs:

- `agent` — the user's coding agent, started automatically when `start_agent = true`
- `shell` — interactive shell
- `server` — interactive shell; no server and no tests are ever started automatically

`hwt` creates worktrees (copying env files and installing dependencies), leases stable dev ports, and performs merge-verified cleanup of worktrees and branches.

## Safety boundaries

1. Do not copy credentials, `.env` contents, Git credential settings, cloud profiles, secrets, or runtime state from another machine. Corral copies env files *locally* from a repository into its own worktrees; that is the only env-file movement allowed.
2. Never print the contents of `.env` or other secret-bearing files.
3. Do not overwrite an existing Corral configuration without inspecting it first; the installer preserves it by default.
4. Do not guess a repository's dev command. Inspect its package/build files; if uncertain, leave it unset — everything else still works.
5. Never add a tests tab. Never auto-start a dev server.
6. Do not run `hwt cleanup` to "test" the installation; it deletes branches. Use `hwt open` for verification.

## Step 0 — outline the plan before your first command

Before running anything, send the user a brief outline (about 6 lines — no more) of what is coming, flagging what their harness may ask them to approve:

> Here's the plan:
> 1. Clone Corral and run its read-only preflight — one command covers prerequisites and all machine detection.
> 2. Ask you five setup questions, one at a time, using what preflight found.
> 3. Run the installer — writes only to `~/.local/bin`, `~/.config/herdr-corral`, and registers the Herdr plugin.
> 4. Run one verification command — tests and plugin checks, quiet unless something fails.
> 5. Only with your OK: edit Herdr's config for a custom worktree location or a palette keybinding.
>
> About four commands will need your approval as we go.

From then on, whenever a command is likely to trigger an approval prompt, say in one short line what it does and whether it changes anything — e.g. "Running Corral's preflight — read-only." — before running it.

## Step 1 — clone and preflight

```bash
git clone https://github.com/bfreed/herdr-corral ~/.local/share/herdr-corral
python3 ~/.local/share/herdr-corral/install.py --preflight
```

The clone location matters: the clone **is** the installation (`hwt update` runs `git pull` in it). `~/.local/share/herdr-corral` is the convention; honor the user's preference if they have one — but do **not** clone into their repositories root. The clone is tooling, not one of their projects.

Preflight is read-only and writes nothing. It prints a JSON report on stdout — the machine-readable source for every interview question below — and a short human summary on stderr. It covers: `prerequisites` (Python 3.11+, `git`, `herdr` and its version; Linux or macOS only — Windows is not supported yet), `repository_parents` (candidate folders with repository counts), `worktrees` (Herdr's configured directory, existing collections), `network` (tailscale DNS name/IP, hostname), `agents` (Herdr's accepted kinds and which are actually usable — probed via PATH, the user's login shell, and vendor bin directories), and `keybindings` (current bindings plus a suggested free palette key).

Report anything failing in `prerequisites` before continuing. If Herdr is missing, install it per its current official instructions (https://herdr.dev). Do not invent commands. Do not run detection commands of your own — if preflight did not surface something, ask the user instead.

## Step 2 — interview the user

How you ask matters as much as what you ask:

- Ask **one question per turn**. Wait for the answer before the next question.
- If your harness has a structured question tool (a multiple-choice picker with an "other/custom" option), use it for every question. Without one, present short numbered options the user can answer with a number.
- **Every question's options come from the preflight JSON** — the user must see what was detected in the question itself, never have to ask "what?" to get the options. Do not run additional detection commands.
- Phrase questions in the user's terms, not Corral's internals: "What parent folder are your repositories in?", not "Which Git repository parent should Corral manage?". Where a suggested wording is given below, use it (adapted to preflight's findings).

The questions, each with the preflight fields that feed it:

1. **Repositories parent folder.** Options come from `repository_parents` (already sorted by repository count; recommend the first). Ask with the findings in the question:

   > What parent folder are your repositories in?
   > 1. `/home/user/Documents/GitRepos` — 21 repositories found (recommended)
   > 2. `/home/user/code` — 3 repositories found
   > 3. Somewhere else (tell me the path)
2. **Where should worktrees go?** The goal is one location and one convention for *both* entry points: Herdr's right-click "new worktree" and `hwt new`. Preflight's `worktrees` field carries Herdr's current directory (`herdr_directory`, with whether it was explicitly configured and exists), any `<repo>__worktrees/` sibling collections (the workmux layout), and a previous Corral `worktree_root` if one exists. If `herdr_config_readable` is false, the directory shown is only Herdr's default — tell the user their Herdr config could not be parsed and have them confirm the real location instead of presenting the default as current.

   Ask, showing the user what each layout literally looks like on disk (substitute their real paths and a real repo name from preflight):

   > Where should new worktrees go? Both Herdr's right-click "new worktree" and Corral's `hwt new` will use this.
   >
   > 1. **One shared folder, Herdr's current one** (recommended — no config changes):
   >    ```
   >    ~/.herdr/worktrees/
   >      myapp/feature-login/        <- worktree of myapp
   >      myapp/fix-nav-bug/
   >      otherapp/spike-cache/
   >    ```
   > 2. **One shared folder somewhere visible**, e.g. `~/Documents/GitRepos/.worktrees/` — same shape as option 1, just where you can see it. I would also update Herdr's own worktree setting to match, so right-click agrees.
   > 3. **A `__worktrees` folder next to each repository** (workmux's convention — pick this to keep your existing habit):
   >    ```
   >    ~/Documents/GitRepos/
   >      myapp/                      <- the repository
   >      myapp__worktrees/feature-login/
   >      otherapp/
   >      otherapp__worktrees/spike-cache/
   >    ```
   >    Caveat: Herdr's right-click dialog can only use one shared folder, so right-click worktrees would still go to Herdr's directory — only `hwt new` follows this layout.

   Mapping the answer to actions:
   - **(1)** → `--worktree-root <Herdr's current directory>` (placement `shared-root` is the default). No Herdr config change.
   - **(2)** → `--worktree-root <folder>`, **and tell the user you will change Herdr's `[worktrees].directory` to the same folder** — with their OK, edit `~/.config/herdr/config.toml` and apply it with `herdr server reload-config`.
   - **(3)** → add `--worktree-placement sibling`. Repeat the caveat when confirming.

   Existing worktrees are never moved; all known layouts stay recognized.
3. **Should dev servers be reachable from other machines?** Preflight's `network` field carries the detected names (tailscale DNS name, tailscale IPv4, hostname). Offer: localhost only (default), each detected name, or a custom hostname. Anything except localhost-only → `--dev-host 0.0.0.0 --remote-host <name>`.
4. **Which agent should start in the `agent` tab?** Preflight's `agents` field carries `kinds` (the authoritative list Herdr accepts) and `usable` (which of those were actually found — probed via PATH, the user's login shell, and per-vendor bin directories). Offer every usable kind (default `hermes` when present), plus "other" and "none". If the user says they use an agent preflight did not detect, believe them and verify the kind is in `kinds`. For "none", install with the default and then set `start_agent = false` on the repositories in `config.toml`.

5. **Keyboard shortcut for the Corral palette?** The palette popup (worktree list with safety annotations; clean up / new worktree / sweep) is Corral's main interactive surface — Herdr's right-click menu cannot host plugin entries. Preflight's `keybindings` field carries the user's current bindings, any existing palette binding, and `suggested_palette_key` — a key that is genuinely free, avoiding both the user's bindings and Herdr's built-in defaults (never propose a key outside that computation: defaults like `prefix+w` are silently displaced by user bindings). With their OK, append to `~/.config/herdr/config.toml`, substituting the suggested key:

   ```toml
   [[keys.command]]
   key = "<suggested_palette_key>"
   type = "plugin_action"
   command = "corral.palette"
   description = "Corral: worktree palette"
   ```

   then run `herdr server reload-config`. Optionally offer a second binding for `corral.cleanup` (merge-checked cleanup of the focused worktree, no popup). Fine to skip: everything remains reachable via `hwt` and `herdr plugin action invoke corral.palette`. If `suggested_palette_key` is null or `keybindings` reports `available: false`, do not invent a key — offer to skip the binding, or let the user name one and use it as given.

## Step 3 — install

```bash
python3 ~/.local/share/herdr-corral/install.py \
  --repos-root <ANSWER-1> \
  --worktree-root <ANSWER-2> \
  --agent-kind <ANSWER-4>
```

Add `--dev-host 0.0.0.0 --remote-host <name>` only if the user opted in at question 3, and `--worktree-placement sibling` only if they chose the `__worktrees`-sibling layout in question 2. If they chose a shared folder other than Herdr's current one, also make the approved Herdr config edit now and run `herdr server reload-config`.

If `~/.local/bin` is not on PATH, add it using the user's shell conventions, then verify in a fresh shell.

## Step 4 — verify

```bash
python3 ~/.local/share/herdr-corral/install.py --verify
```

One command replaces the old four: it runs Corral's test suite quietly (per-test output appears only on failure), checks the `hwt` launcher and configuration, and confirms plugin `corral` is linked and enabled with the `cleanup` and `palette` actions. The JSON verdict lands on stdout; a nonzero exit names the failing check — report it to the user with the captured detail.

Do **not** open a live workspace to test the installation. The user's first real `hwt open <repository>` is the live check; Step 6 tells them exactly what to expect from it.

## Step 5 — configure repositories (only as far as the user wants)

The installer already registered every repository with safe defaults: untracked `.env` / `.env.*` files are copied into new worktrees, dependencies install via the lockfile-detected package manager, and no dev command is set.

For repositories the user actively works on, refine the config either by running `hwt init` in the repository (interactive) or by writing `~/.config/herdr-corral/repos.d/<repo>.toml` yourself after inspecting the repository:

```toml
[repositories."example"]
path = "/home/user/repos/example"
mode = "worktree"            # or "open-only" to forbid worktree creation
base_branch = "origin/main"
remote = "origin"
fetch = true
start_agent = true
files = [
  { path = ".env.local", action = "copy", required = false, overwrite = "skip" },
]

[repositories."example".dependencies]
policy = "clone"             # clone (default) | auto | independent | shared | shared-if-lockfile-matches
# clone: copy-on-write copy of the main checkout's node_modules when lockfiles
# match, else the lockfile-detected install (= auto). Prefer clone/auto over
# the shared symlink policies, which can break build tools and native modules.

[repositories."example".commands]
dev = ["npm", "run", "dev", "--", "--host", "{host}", "--port", "{port}"]
```

Only add a `dev` command you verified in that repository's own files; supported placeholders are `{port}`, `{host}`, `{worktree}`, `{repository}`.

## Step 6 — report

Tell the user: which repositories were configured and how, the verify verdict, plugin state, and how to update later (`hwt update`; `hwt doctor` announces available updates).

Also set the expectation for first use — it doubles as the live check this flow no longer performs: their first `hwt open <repository>` will open one Herdr workspace with exactly three tabs (`agent`, `shell`, `server`), starting their agent only in `agent` when `start_agent` is enabled, and re-running the same `hwt open` reuses that workspace. If a rerun ever creates a duplicate workspace, they should report it at https://github.com/bfreed/herdr-corral/issues with the output of `herdr workspace list`.

## Uninstall

```bash
python3 ~/.local/share/herdr-corral/install.py --uninstall          # keep config
python3 ~/.local/share/herdr-corral/install.py --uninstall --purge  # delete config + state too
rm -rf ~/.local/share/herdr-corral                                  # remove the clone
```

## Troubleshooting

- Config: `~/.config/herdr-corral/config.toml` + `repos.d/` overlays
- Installation: the git clone (default `~/.local/share/herdr-corral`)
- Runtime state (port leases): `~/.local/state/herdr-corral/`
- Relink: `herdr plugin unlink corral`, then `herdr plugin link <clone-dir> --enabled`
- Plugin logs: `herdr plugin log --help`
