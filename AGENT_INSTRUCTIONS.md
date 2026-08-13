# Agent instructions: install Corral on this machine

You are an AI coding agent installing Corral — a Herdr plugin and `hwt` CLI for Git-worktree workflows — for the user who pointed you at this file. Follow these instructions exactly; where they say to ask the user, ask instead of guessing.

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

## Step 2 — ask the user

Ask these questions (suggest the defaults):

1. **Where do your Git repositories live?** (default `~/repos` — one parent directory whose immediate children are repositories)
2. **Where should worktrees go?** (default `<repos-root>/.worktrees`)
3. **Should dev servers be reachable from other machines?** If yes, ask for the hostname/DNS name others should use → `--dev-host 0.0.0.0 --remote-host <name>`. If no, omit both (localhost only).
4. **Which agent should start in the `agent` tab?** (default `hermes`; any kind accepted by `herdr agent start --kind`)

## Step 3 — install

```bash
git clone https://github.com/bfreed/herdr-corral ~/.local/share/herdr-corral
python3 ~/.local/share/herdr-corral/install.py \
  --repos-root <ANSWER-1> \
  --worktree-root <ANSWER-2> \
  --agent-kind <ANSWER-4>
```

Add `--dev-host 0.0.0.0 --remote-host <name>` only if the user opted in at question 3.

The clone location matters: the clone **is** the installation (`hwt update` runs `git pull` in it). `~/.local/share/herdr-corral` is the convention; honor the user's preference if they have one.

If `~/.local/bin` is not on PATH, add it using the user's shell conventions, then verify in a fresh shell.

## Step 4 — verify

```bash
python3 -m unittest discover -s ~/.local/share/herdr-corral/tests -v
hwt --help
hwt doctor
herdr plugin list
```

Confirm plugin `corral` is linked and enabled. `hwt doctor` failing only on a missing agent CLI is acceptable if the user declined auto-started agents.

Then pick one configured repository and do a non-destructive workspace check:

```bash
cd <repos-root>/<repository>
hwt open <repository>
```

Using Herdr CLI output, verify: tabs are exactly `agent`, `shell`, `server`; the agent starts only in `agent`; re-running `hwt open` creates no duplicates.

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

## Troubleshooting

- Config: `~/.config/herdr-corral/config.toml` + `repos.d/` overlays
- Installation: the git clone (default `~/.local/share/herdr-corral`)
- Runtime state (port leases): `~/.local/state/herdr-corral/`
- Relink: `herdr plugin unlink corral`, then `herdr plugin link <clone-dir> --enabled`
- Plugin logs: `herdr plugin log --help`
