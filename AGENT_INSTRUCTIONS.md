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

## Step 1 — prerequisites

Verify, and report anything missing to the user before continuing:

- Linux or macOS (Windows is not supported yet)
- Python 3.11+ (`python3 --version`)
- `git` and `herdr` on PATH; Herdr 0.8.0 or newer
- The user's agent CLI (default `hermes`) on PATH if they want auto-started agents

If Herdr is missing, install it per its current official instructions (https://herdr.dev). Do not invent commands.

## Step 2 — interview the user

How you ask matters as much as what you ask:

- Ask **one question per turn**. Wait for the answer before the next question.
- If your harness has a structured question tool (a multiple-choice picker with an "other/custom" option), use it for every question. Without one, present short numbered options the user can answer with a number.
- **Investigate before you ask**, so every question offers concrete detected options instead of a freeform prompt. The user must see what you detected in the question itself — never make them ask "what?" to get the options.
- Phrase questions in the user's terms, not Corral's internals: "What parent folder are your repositories in?", not "Which Git repository parent should Corral manage?". Where a suggested wording is given below, use it (adapted to your findings).

The questions, each with the detective work to do first:

1. **Repositories parent folder.** Scan before asking: `~/repos`, `~/code`, `~/src`, `~/dev`, `~/projects`, `~/GitRepos`, `~/Documents/GitRepos`, and anything else under `$HOME` whose immediate children include several Git repositories. Then ask, with your findings already in the question:

   > What parent folder are your repositories in?
   > 1. `/home/user/Documents/GitRepos` — 21 repositories found (recommended)
   > 2. `/home/user/code` — 3 repositories found
   > 3. Somewhere else (tell me the path)
2. **Where should worktrees go?** The goal is one location and one convention for *both* entry points: Herdr's right-click "new worktree" and `hwt new`. Investigate first:
   - Read Herdr's current `[worktrees].directory` from `~/.config/herdr/config.toml` (default when absent: `~/.herdr/worktrees`; Herdr organizes checkouts as `<directory>/<repo>/<branch-slug>`).
   - Scan for existing worktree collections: that Herdr directory, `<repo>__worktrees/` siblings next to repositories (the workmux layout), and any previous Corral `worktree_root`.

   Then ask, showing the user what each layout literally looks like on disk (substitute their real paths and a real repo name you found):

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
3. **Should dev servers be reachable from other machines?** Detect the machine's names first: if `tailscale` is on PATH, get the DNS name from `tailscale status --json` (`.Self.DNSName`) or `tailscale ip -4`; also consider `hostname -f`. Offer: localhost only (default), each detected name, or a custom hostname. Anything except localhost-only → `--dev-host 0.0.0.0 --remote-host <name>`.
4. **Which agent should start in the `agent` tab?** The authoritative list is Herdr's, not PATH: read the kinds `herdr agent start --kind` accepts from `herdr agent start --help` (Herdr also has its own agent detection — use whatever it reports). To judge which kinds are actually usable, remember your own shell probably has a reduced PATH: agent CLIs are typically installed into per-product home directories (`~/.grok/bin/grok`, and the same `~/.<vendor>/bin/<tool>` pattern for others) whose PATH entry lives in the user's rc files. Probe three ways before declaring an agent absent: plain `command -v`, the user's login+interactive shell (`$SHELL -lic 'command -v hermes claude codex grok opencode'`, with a timeout in case the rc file prompts), and a direct glob like `ls ~/.*/bin/ ~/.local/bin/ 2>/dev/null`. A product's CLI binary also may not be named after its brand — never conclude an agent is missing from one failed name lookup. Offer every usable kind (default `hermes` when present), plus "other" and "none". If the user says they use an agent you did not detect, believe them and verify the kind against Herdr's help output. For "none", install with the default and then set `start_agent = false` on the repositories in `config.toml`.

## Step 3 — install

```bash
git clone https://github.com/bfreed/herdr-corral ~/.local/share/herdr-corral
python3 ~/.local/share/herdr-corral/install.py \
  --repos-root <ANSWER-1> \
  --worktree-root <ANSWER-2> \
  --agent-kind <ANSWER-4>
```

Add `--dev-host 0.0.0.0 --remote-host <name>` only if the user opted in at question 3, and `--worktree-placement sibling` only if they chose the `__worktrees`-sibling layout in question 2. If they chose a shared folder other than Herdr's current one, also make the approved Herdr config edit now and run `herdr server reload-config`.

The clone location matters: the clone **is** the installation (`hwt update` runs `git pull` in it). `~/.local/share/herdr-corral` is the convention; honor the user's preference if they have one — but do **not** clone into their repositories root. The clone is tooling, not one of their projects.

If `~/.local/bin` is not on PATH, add it using the user's shell conventions, then verify in a fresh shell.

## Step 4 — verify

```bash
python3 -m unittest discover -s ~/.local/share/herdr-corral/tests -v
hwt --help
hwt doctor
herdr plugin list
```

Confirm plugin `corral` is linked and enabled. `hwt doctor` failing only on a missing agent CLI is acceptable if the user declined auto-started agents.

Then **ask the user** which repository to use for a live workspace check, and tell them it will create one Herdr workspace with three tabs (and start their agent in it if `start_agent` is enabled). With their approval:

```bash
cd <repos-root>/<repository>
hwt open <repository>
```

Using Herdr CLI output, verify: tabs are exactly `agent`, `shell`, `server`; the agent starts only in `agent`; re-running `hwt open <repository>` reuses the same workspace. **If a second run creates a duplicate workspace, stop immediately** — do not retry — and report it as a bug at https://github.com/bfreed/herdr-corral/issues, including the output of `herdr workspace list` and `herdr workspace get <workspace-id>`. When the check is done, offer to close the verification workspace (find the close/remove subcommand in `herdr workspace --help`; do not guess it).

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
policy = "auto"              # auto | independent | shared | shared-if-lockfile-matches

[repositories."example".commands]
dev = ["npm", "run", "dev", "--", "--host", "{host}", "--port", "{port}"]
```

Only add a `dev` command you verified in that repository's own files; supported placeholders are `{port}`, `{host}`, `{worktree}`, `{repository}`.

## Step 6 — report

Tell the user: which repositories were configured and how, test results, plugin state, the workspace verification performed, and how to update later (`hwt update`; `hwt doctor` announces available updates).

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
