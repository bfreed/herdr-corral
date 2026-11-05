# Changelog

## 1.4.5 — 2026-11-05

- **A Desktop cleanup run from inside its own worktree finishes.** Run from one of the worktree's own tabs (as an agent in its agent tab does), `hwt cleanup` ended that tab right after deleting the checkout, which ended hwt before it deleted the branch, its remote copy and the lease, leaving the cleanup half done. hwt now ends its own terminal only after the whole command has finished and printed its result. A command that fails keeps it, so the error stays readable.

## 1.4.4 — 2026-11-05

- **`hwt doctor` crashed right after installing when no repositories were found.** If `--repos-root` (default `~/repos`) has no clones directly inside it, `install.py` writes a config with no `[repositories]` table. `hwt doctor`, the installer's suggested next step, then failed with `KeyError: 'repositories'`, and `hwt info` (the Desktop panel's view) did too. A config without repositories is now valid. `doctor` says to run `hwt init` inside a repository or `hwt add <path>`, and the installer warns when it found no repositories and says how to point it at the right folder.

## 1.4.3 — 2026-11-05

- **No traceback when a question gets no input.** Windows counts `NUL` as a terminal, so `hwt cleanup <name> < NUL` with an ambiguous name asked which worktree, got end-of-input, and crashed with an `EOFError` traceback (nothing was deleted). The number prompt now treats end-of-input as no terminal ("re-run with one of the listed names or paths"), and any other question that gets none ends hwt with a message and exit code 2.

## 1.4.2 — 2026-11-05

Two cleanup fixes from re-testing 1.4.1 on Windows.

- **A retry could clean up the wrong worktree.** After a cleanup is cut short, git no longer lists the worktree, so `hwt cleanup <branch>` matched another repository's worktree with the same branch name, if there was one, and cleaned that up instead. Half-done cleanups are now candidates like any worktree: a name matching several asks which (listing their paths; marked "cleanup cut short"), and outside a terminal it stops and lists them. The failure's message now gives the retry by path (`hwt cleanup <path>`).
- **A TUI retry failed the same way.** When Herdr's removal fails, the worktree's Herdr workspace stays open and its terminals keep the folder in use. The retry now closes that workspace first (on Desktop, ends the space), then deletes the empty folder, retrying for a few seconds on Windows. Run from inside that workspace, it says to run it from another one instead of closing itself.

## 1.4.1 — 2026-11-05

Fixes from a hands-on Windows 11 test of 1.3.0 (Git for Windows 2.49, Herdr 0.9.4, Python 3.14).

- **No more directory junctions (data loss).** Git for Windows before 2.54 deletes *through* a junction when it removes a worktree, and Herdr's own Delete runs `git worktree remove --force`: with `policy = "shared"` and no Developer Mode, deleting a worktree emptied the main checkout's `node_modules`. CI didn't catch it because its runner has Git 2.56, which skips junctions. Corral now never makes one: where Windows can't make a symlink, `shared` gives the worktree its own `node_modules` (copied when the lockfiles match, else installed) and says why, and a `symlink` folder is copied. Junctions an older Corral made are replaced when the worktree is opened again, and `hwt doctor` lists any left. Directory symlinks are safe: Git for Windows removes the link, not its target (checked in 2.49's source, and tested in CI).
- **Cleanup deletes branches last.** The checkout goes first; only then the local branch, the remote branch and the port lease, so a removal that fails loses nothing. Running the same `hwt cleanup` again finishes a half-done one: it deletes the empty folder git left, the branch and its remote copy, and the lease. On Windows a removal that fails because the folder is in use (`Permission denied`, Herdr's `worktree_remove_failed`) says a program has it open. Leases whose worktree no longer exists are reclaimed.
- **Desktop cleanup ends the space first.** Its terminals (and what runs in them) held the folder open on Windows, so the panel's Clean up always failed with `Permission denied`. They're ended, and waited for, before `git worktree remove`; the terminal `hwt` itself runs in goes last. A removal git would refuse is refused before the space is ended.
- **A new worktree's agent starts last**, after its dependencies and metadata, and `agent_not_ready` (Claude's "trust this folder?" question, in every new folder) no longer aborts the bootstrap and releases the port.
- **`hwt update` on a release checkout.** The panel's Install checks out a release tag, which has no branch to pull, so `hwt update` (and the panel's Update Corral) failed. It now fetches tags and checks out the newest release. A 1.3.0 installed by the panel can't update itself this once; run `git -C ~/.local/share/herdr-corral fetch --tags` and `git -C ~/.local/share/herdr-corral checkout v1.4.1`, then `hwt update`.
- **A `symlink` files entry applied a second time** (opening the worktree again) failed with `symlink escape`, on every OS: Corral's own link was taken for an escape. It's now accepted when it points at its source.
- **A shared `node_modules` link made the worktree dirty.** A `node_modules/` ignore line matches only a real folder, so git listed the link as untracked and cleanup refused. Corral's own links no longer count, and are removed before the checkout.
- **Lockfiles are compared ignoring line endings** (`core.autocrlf=true` is Git for Windows' default), so `clone` copies `node_modules` instead of reinstalling.
- `install.ps1` no longer exits with herdr-app's exit code after printing the manual layout step. A malformed `repos.d` overlay (or config) names the file instead of a traceback. The TUI's Windows reminders clear the typed `Write-Host` command away in a new tab.
## 1.4.0 — 2026-11-05

- **HTTPS dev servers over Tailscale.** From another computer, `http://<machine>:<port>` isn't a secure context, so browsers withhold Web Locks, service workers, the clipboard API and `crypto.subtle`, and apps that need them break. With `[https] provider = "tailscale"`, each leased port P is also served at `https://<machine>.<tailnet>.ts.net:<P + 1000>` (`tailscale serve`, tailnet only, real certificate). The mapping is made when the port is leased (`hwt new`, `hwt open`, Herdr's own new-worktree dialog, `hwt dev`) and removed with the lease (`cleanup`, `remove`, Herdr's delete). That HTTPS URL is now the `dev_url` in `hwt new`, `hwt info` and the panel, and `HWT_REMOTE_URL` in the tabs. `HWT_HTTPS_URL` is new, and `hwt status` lists it. Corral only touches mappings that are exactly its own. `hwt doctor` reports mappings left by released ports and `hwt reap` removes them. Without Tailscale, or without permission to change Serve, it warns and stays on HTTP.
- **`start_dev`.** With `start_dev = true` (global, or per repository to override it) and a dev command, a new worktree's server tab runs `hwt dev` once dependencies are in place. It doesn't run after a failed install, when the port is already serving, or when the worktree is reopened.
- `hwt dev` prints the URL it serves at. `install.py` takes `--https tailscale` and `--start-dev`, and a new config has both settings with comments.

## 1.3.0 — 2026-11-05

- **Windows.** Corral runs on Windows 10/11, both as the computer running Herdr Desktop's window and as the machine a space runs on, and with the Herdr TUI. Linux and macOS behave as before.
  - **Install:** `install.py` accepts Windows. It writes `~/.local/bin/hwt.cmd`, which runs `hwt.py` with the Python that installed it, and adds that folder to the user PATH when it's missing. `hwt doctor` and `install.py --verify` check the launcher.
  - **TUI plugin:** Herdr runs hook commands as given, and `python3` is rarely on a Windows PATH. Herdr refuses duplicate action ids even per platform, so a Windows twin can't sit in one manifest. Instead, on Windows, `install.py` and `hwt update` link a generated copy of the manifest (`.windows-plugin/`, gitignored) whose commands use full paths. `herdr-plugin.toml` itself is unchanged.
  - **Herdr's config** is read where Herdr reads it on Windows, `%APPDATA%\herdr\config.toml`. `HERDR_CONFIG_PATH` is now honored on every OS, as Herdr does.
  - **Dev servers:** listeners are found with `netstat -ano` and the Windows process list (the user's own processes only). They're stopped with `taskkill /T`, then `/F`. A process's working folder can't be read on Windows, so servers there are matched by their leased port only, and `hwt reap` finds no orphans.
  - **Ports:** the free-port check uses exclusive binding on Windows. There, `SO_REUSEADDR` let a check bind to a port another program was listening on, so every port looked free.
  - **Links:** without Developer Mode, a linked folder becomes a directory junction and a linked file a copy. Junctions Corral made are removed before a worktree is deleted.
  - **Dependencies:** the `clone` policy makes a plain copy (no copy-on-write on Windows) and skips trees holding links (pnpm), installing instead. `npm`/`pnpm`/`yarn` `.cmd` shims run for installs and `hwt dev`.
  - **Tabs:** PowerShell (`pwsh`, `powershell`, else `%COMSPEC%`) sets the per-tab environment. TUI reminders print with `Write-Host`; reminders that wrote to the shell's tty through `/proc` are skipped cleanly on Windows. `hc` is also looked for in `%LOCALAPPDATA%\Programs\herdr-core`.
  - **Text:** git's and hc's output is read as UTF-8, and `hwt.cmd` and the plugin run Python in UTF-8 mode, so a user folder like `C:\Users\Jürgen` works. Piped output is UTF-8.
  - **Process trees:** Windows keeps a dead parent's id and reuses ids, so a process started before its supposed parent isn't counted as its child. After forcing a server, cleanup waits up to 5 seconds for it to exit before deleting the worktree, since its files stay locked until then.
  - **File copies:** a file Corral copied in place of a symlink, which later differs from the main checkout's, is left as is with a note. It used to fail every later open of the worktree.
- **The Corral panel on Windows machines.** The panel decides per machine, not by the window's system. It tries `sh`; with none there (or only Git's), it runs programs directly. The probe and Install run as Python scripts, `hwt` runs as `python <clone>\hwt.py` (Herdr's exec starts only `.exe` files by name, so `hwt.cmd` can't be used), and `hc` runs by its path. It remembers what it learned for each machine. Windows paths are compared without regard to case or separator, and the self-update builds paths with the home folder's own separator.
- **Desktop install docks the panel.** Herdr never docks a new mod into its own read-only layouts (`main`, the default, `terminals`, `win98`). It waits silently instead, so a fresh install showed no panel. `desktop/install.sh` and the new `desktop/install.ps1` (PowerShell, Windows) now ask the open window for a copy of the shown layout when it's a shipped one. They run `herdr-app do layout.clone --name main --as main-corral`, which also switches to the copy, and Herdr docks the panel there. With the window closed, the installer says what to run. Both honor `HERDR_APP_HOME` and `XDG_CONFIG_HOME`, as Herdr does.
- CI also runs the panel tests and both Desktop installers on Linux, macOS and Windows. Earlier Windows CI failures are fixed: `os.O_NOCTTY` in the reminder path, and the installer's OS check.

## 1.2.1 — 2026-11-05

- **`hwt init` checks the dev command before saving it.** A command that could never run in the repository used to be saved silently and only failed later in `hwt dev`, often as an unhelpful package-manager error (for example `npx quasar dev` in a repository with no `package.json` gives "npm error could not determine executable to run"). The interview now warns when a package runner (`npm`, `npx`, `pnpm`, `yarn`, `bun`, …) is used without a `package.json`, when the executable is not on `PATH`, or when a relative script path does not exist, then asks whether to save it anyway (default no, which re-prompts). The prompt also names the repository being configured.
- Clearer `hwt init` wording: the dev server step now says what the command is for, when to use `{port}` (with `npm run dev` and Quasar examples), how to answer `none`, and that Enter accepts the bracketed value. The env-file message says plainly that there is nothing to copy.

## 1.2.0 — 2026-11-05

- **Herdr Desktop.** Corral now also works in Herdr Desktop, through the machine's herdr-core (`hc`). `hwt` drives whichever Herdr it runs in: the TUI from a TUI pane or plugin hook, Desktop from a Desktop terminal, otherwise `frontend` in config.toml (default `tui`); `--frontend` overrides. A new worktree's space appears in Desktop with its agent, shell and server tabs, the agent started, and `PORT`/`HWT_*` set in the shell and server tabs. `cleanup`, `remove`, `open` and `list` work the same; Desktop has no workspace metadata or focus to set, so those are skipped.
- **The Corral panel** (`desktop/corral`), a Herdr Desktop mod. It shows the focused space's repository and worktrees with their ports and dev servers. Its buttons create, open and clean up worktrees and start the dev server, running `hwt` on that space's machine. When Corral or the repository isn't set up there, it offers **Install Corral** or **Set up** instead. Any worktree in its list can be cleaned up (merge-checked); when Corral keeps one for unmerged or uncommitted work, it says why and offers **Delete anyway**. It updates itself when a machine's Corral ships a newer panel, installs from Herdr's mod gallery or `desktop/install.sh`, folds to a rail like Herdr's sidebar, and also names and orders Corral's tabs, which herdr-core can't do.
- **`hwt add <path> [--base]`** configures a repository with Corral's defaults, no questions. **`hwt info [folder]`** prints a folder's repository, worktrees and port leases as JSON. **`hwt --version`.**
- **`install.py --desktop`** skips the TUI plugin, doesn't need the TUI's `herdr`, and writes `frontend = "desktop"`.
## 1.1.0 — 2026-09-30

- **Squash-merged branches clean up without `--abandon --confirm`.** A squash or rebase merge leaves the branch's own commits nowhere else, so the ancestry check reported them as unmerged work. `cleanup`, `sweep`, the palette, and the teardown popup now also accept a branch whose changes provably landed on base: merging it would change nothing (`git merge-tree --write-tree`), or one base commit's patch equals the branch's whole diff (`git patch-id --stable`, which still holds after base later edits the same lines). Either proof is enough. A commit made after the merge, a partly landed branch, or commits that cancel out still require confirmation, and a failed probe still blocks deletion. The palette shows `squash-merged — cleans instantly`, and the cleanup result reports `reason: "squash-merged"`. Requires Git 2.38+ for the merge-tree check; on older Git the patch-id check alone applies.
- **Cleanup stops the worktree's dev servers.** Previously only the port lease was released, and servers kept running from the deleted folder. Before removing a checkout, `cleanup` (and the `worktree.removed` hook, for deletions made in Herdr's UI) stops every same-user process listening from inside it or on its leased port, along with their child processes: SIGTERM, then SIGKILL after 5 seconds. Wrapper, shell, and agent processes are left alone. Results appear under `stopped_servers`.
- **`hwt reap`** stops servers already orphaned: listeners whose working directory is a deleted folder under an approved worktree root with no surviving checkout around it. `--dry-run` lists them. `hwt sweep` reaps too (`stopped_orphan_servers`), and `hwt doctor` reports them without failing.

## 1.0.0 — 2026-08-14

Corral's first stable release. The 0.18.0 changes below shipped hours earlier unversioned-as-stable and are re-tagged here as 1.0.0; nothing else changed.

## 0.18.0 — 2026-08-13

- The guided install now takes ~4 approved commands instead of dozens. `install.py --preflight` performs every prerequisite check and machine detection in one read-only invocation (repository parents with counts, Herdr's worktree directory and collections, tailscale/hostname candidates, usable agent CLIs via a three-way probe, keybindings with a free palette-key suggestion that also avoids Herdr's built-in defaults), emitting JSON for the agent and a summary for the human. `install.py --verify` replaces the four verification commands: quiet test suite (output only on failure), launcher, configuration, and plugin-linkage checks with one pass/fail verdict.
- The live workspace check is gone from the install flow; the final report tells the user their first `hwt open` is the live check and what to expect from it. `AGENT_INSTRUCTIONS.md` rewritten accordingly — agents no longer improvise detection commands.
- Preflight degrades instead of blocking: no tailscale, an unreadable Herdr config (reported distinctly from an unconfigured one), a hanging login shell, or Python older than 3.11 (tomllib is imported lazily) each produce a partial report, never a crash. It also gates on OS and Herdr's minimum version (read from the plugin manifest), honors `HERDR_BIN_PATH` like `hwt` does, and verify's checks are bounded by timeouts so a failure names itself instead of hanging or tracebacking.

## 0.17.4 — 2026-08-13

- Fixed the fallback prefix used when rendering the palette keybinding: Herdr's default `keys.prefix` is ctrl+b (per `validated_keybinds` in its source), not ctrl+a. 0.17.3 told users with an unconfigured prefix to press a dead chord.

## 0.17.3 — 2026-08-13

- The shell-tab reminder now names the actual palette keystrokes ("palette: ctrl+a w") instead of "via your Corral keybinding". Corral reads the binding from Herdr's own config.toml (respecting XDG_CONFIG_HOME) — the `[[keys.command]]` entry invoking `corral.palette` — and expands a `prefix+X` binding using the configured prefix, defaulting to Herdr's ctrl+a. When no binding exists, the reminder falls back to the explicit `herdr plugin action invoke corral.palette` command.

## 0.17.2 — 2026-08-13

- The `worktree.removed` hook now releases the port lease for any removed checkout, not just those under approved worktree roots — deleting an ad-hoc-located worktree from Herdr's UI no longer strands a lease (which counted against the port range until manually cleared). Releasing is a no-op for paths that never held one.

## 0.17.1 — 2026-08-13

- Internal, no behavior change: the merged / unique-commits verdict that existed in three copies (palette safety labels, merge-checked cleanup, and the teardown popup) is now single-sourced in `branch_disposition`, so the advice shown always matches what the deletion paths actually enforce.

## 0.17.0 — 2026-08-13

- New GUI teardown hook: deleting a worktree checkout from Herdr's right-click menu ("Delete worktree checkout...") now opens a Corral popup for the branch left behind. It fetches with `--prune`, reports whether the remote branch has already been deleted (the GitHub merge-then-delete flow), whether the branch is merged or all its commits exist elsewhere, then asks about deleting the local branch — defaulting to delete when safe, and requiring the branch name typed back when commits exist nowhere else. If the remote branch still exists and the local one was deleted, it offers to delete the remote too (default follows merge state).
- Corral's own removal flows (`hwt remove`, `hwt cleanup`, sweep, palette) mark their removals in state so the event hook never opens the popup for them; protected branches, detached checkouts, and branches already gone are skipped.

## 0.16.1 — 2026-08-13

- Tab reminders no longer echo a `printf` command at the prompt: the message is written straight to the pane's tty (resolved via `pane process-info` + `/proc/<shell_pid>/fd/0`), styled dim so it reads as ambient guidance, followed by a bare Enter so the shell draws a fresh prompt below it. Hosts without `/proc` (macOS) automatically fall back to the previous printf-through-the-shell delivery.

## 0.16.0 — 2026-08-13

- Tab guidance is back, richer, and robust: the `server` tab always gets a message on creation — `hwt dev` + the leased port + local/remote URLs when a dev command is configured, or a pointer to `hwt init` when not — and the `shell` tab gets orientation (`hwt -h`, how to open the palette, plus the init suggestion for unconfigured repos).
- Reminder delivery fixed for non-`$` prompts: the prompt wait now matches `❯`, `%`, and `>` as well, and a failed wait or send can no longer break the bootstrap (previously the wait expected `$`/`#` only, so starship-style prompts timed it out).

## 0.15.3 — 2026-08-13

- Cleaning up or removing an open worktree no longer yanks Herdr's focus away: Herdr unconditionally switches to the removed worktree's parent repo workspace (verified in `app/worktrees.rs::close_removed_linked_worktree_workspace`), so Corral now records the focused workspace before removal and switches back afterward — unless you ran it from the workspace being removed.

## 0.15.2 — 2026-08-13

- Worktrees created by `hwt new` (and reopened by `hwt open`) now display in Herdr's sidebar exactly like right-click-created ones: just the branch name. We stopped passing `--label` — an explicit label becomes the workspace's `custom_name`, which disables Herdr's branch-derived display (verified in `ui/sidebar.rs::grouped_child_display_label`), and our `"repo: branch"` labels ate the whole panel width with the repo prefix.

## 0.15.1 — 2026-08-13

- Base-branch candidates fixed and reordered: the canonical checkout's own branch is no longer excluded (having `develop` checked out in the main clone was hiding `origin/develop` — the most wanted base), only branches checked out in *linked* worktrees are; and plain main-line names (`develop`, `main`, `staging`) sort before slash-namespaced topic branches so they always make the top-10 cut.

## 0.15.0 — 2026-08-13

- `hwt init` now asks for the repository's base branch (first question), offering the current setting plus the repository's remote branches; typed values are validated and must be remote-tracking refs (cleanup verifies merges against the base). Previously the installer's `origin/HEAD` guess was carried forward silently.
- Fixed `hwt new` failing with "command failed (herdr) with exit status 2": `herdr worktree create` rejects `--workspace` together with `--cwd` (verified from source), and we passed both. This call path had never succeeded.
- Failed external commands now include the first line of their stderr in the error message, so failures like the above diagnose themselves.

## 0.14.1 — 2026-08-13

- The dev-command suggestion in `hwt init` now wires the leased port properly per framework: vite/astro/webpack scripts get `-- --host {host} --port {port}`, Next.js gets `-- -H {host} -p {port}`, and frameworks that honor the PORT/HOST environment (CRA, Express-style, Nuxt) stay bare since `hwt dev` always exports those. The prompt now explains that the command is a template run by `hwt dev` on a per-worktree leased port.

## 0.14.0 — 2026-08-13

- The `hwt new` base picker also lists the repository's remote branches (up to 10, newest first, excluding `HEAD` and branches already checked out in a worktree).
- Base refs are validated: a typed "other" ref is checked on the spot with a re-prompt, and `hwt new`/`--base` verify the ref exists (after the fetch) before anything is created — no more half-created worktrees from a typo like `dev`.

## 0.13.0 — 2026-08-13

- `hwt new` with no arguments is now interactive and context-aware: inside a canonical repository it offers that checkout's current branch as the default base; inside a worktree it defaults to the configured base (a sibling worktree) with the option to stack the new branch on the current worktree's branch; outside any repository it offers a numbered repository picker, then a base picker, then prompts for the branch name.
- `hwt new <branch>` now also works from inside a worktree or a repo subdirectory (it maps to the canonical repository instead of erroring).
- The palette's `n`ew-worktree flow reuses the same interview.

## 0.12.0 — 2026-08-13

- `hwt open` works like `hwt cleanup` now: with no argument it offers a numbered picker (repositories, then worktrees grouped by repo); a target that doesn't resolve uniquely offers similar candidates instead of erroring.
- The `hwt cleanup` picker uses the palette's readable layout: repository group headers, two-line entries with colored safety annotations and dimmed, home-shortened paths.

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
