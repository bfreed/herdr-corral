# Corral on Windows: hands-on test plan

For the Windows 11 test machine (Herdr Desktop, herdr 0.9.4, hc, git, Python 3.13), on the `fix-windows` branch (Corral 1.3.0). CI covers the unit tests, `hwt.cmd`, junctions, port checks, stopping a real process tree, and both Desktop installers. These steps cover what CI can't: Herdr itself, Desktop's `exec`, the panel, and remote machines.

Record for each step: pass/fail, the exact error text, and `hwt --version`, `herdr --version`, `hc --version` and `herdr-app --version` once at the start. Use PowerShell (Windows PowerShell 5.1 unless a step says otherwise). Use a throwaway repository with an `origin` you can push to and delete branches on, for example a fork or a scratch repo: `C:\Users\<you>\repos\demo`, with a `package.json` whose `dev` script listens on `$env:PORT` (`npx http-server -p $env:PORT` is enough) and a `package-lock.json`.

## Re-run for 1.4.1 (and 1.4.2)

1.4.1 fixes what the first run on this machine found (Git for Windows 2.49.0, Python 3.14). It also carries 1.4.0 (HTTPS dev servers over Tailscale, `start_dev`), which is off unless configured. Re-run these, in this order. Use the throwaway `demo` repository: step R2 deliberately tests the data-loss case.

- **R0. Get to 1.4.2.** The panel-installed 1.3.0 is a tag checkout, so its own `hwt update` can't move it this once. Run `git -C $HOME\.local\share\herdr-corral fetch --tags`, then `git -C $HOME\.local\share\herdr-corral checkout v1.4.2`, then `hwt update`. (From 1.4.1, `hwt update` alone does it.)
  - `hwt --version` should print `hwt 1.4.2`.
  - `hwt doctor`: note any `junctions` check. It lists worktrees 1.3.0 left a junction in.
  - Then, to check bug 5, run `hwt update` again on that tag checkout. It must print `"up_to_date": true`, with no "git pull failed". (Moving to a newer release is tested in CI; no release is newer than v1.4.2 yet.)
- **R1. CI's blind spot.** Run `py -3 -m unittest discover -s $HOME\.local\share\herdr-corral\tests`. It must pass, including `test_git_older_than_2_54_deletes_through_a_junction`, which expects Git 2.49 to delete through a junction (that's why Corral no longer makes them). Report the `git 2.49.0` line it prints, and whether `test_git_deletes_a_directory_symlink_without_following_it` ran or was skipped (it needs Developer Mode).
- **R2. Shared dependencies (bug 1).** With `policy = "shared"` and Developer Mode **off**:
  - Create a worktree. The bootstrap says "sharing node_modules needs a symlink … the worktree gets its own instead".
  - `Get-Item <wt>\node_modules | Select LinkType` must be empty: a real folder, not a junction.
  - Count the files in the main checkout's `node_modules`: `(Get-ChildItem -Recurse -File <main>\node_modules).Count`.
  - Delete the worktree with **Herdr's own Delete** (not `hwt`). Count again: the two counts must match.
  - Then, with Developer Mode **on**: a new worktree's `node_modules` shows `SymbolicLink`. Herdr's Delete must again leave the count unchanged.
- **R3. A 1.3.0 junction.** If R0's `hwt doctor` listed one, run `hwt open <that worktree>`, then `Get-Item <wt>\node_modules | Select LinkType`: it must no longer say `Junction`. Then `hwt doctor` must not list it.
- **R4. Desktop Clean up (bug 2).** In the panel, use **Clean up…** then **Delete worktree and branch** on a merged worktree whose agent, shell and server tabs are all open. The space must close, then the worktree folder, the branch and its remote copy must all be gone. Expected: no "Permission denied".
- **R5. A folder in use (bug 3; redone for 1.4.2).**
  - In a separate PowerShell window (not one of the space's tabs), `cd` into a merged worktree's folder. Then run `hwt cleanup <branch>` from another folder.
  - Expected message: "couldn't delete … a program probably has the folder open … Run 'hwt cleanup <path>' again: it closes the worktree's own Herdr workspace first". The hint names the folder's path, not the branch.
  - Then check:
    - `git branch --list <branch>`: still there.
    - `git ls-remote origin <branch>`: still there.
    - `hwt status`: still shows the lease.
  - `cd` out of the folder, then run the `hwt cleanup <path>` from the message. It must print `"resumed": true`, and remove the empty folder, the branch, the remote branch and the lease.
- **R5b. TUI: the worktree's workspace still open (1.4.2).** In the Herdr TUI, `cd` into a merged worktree from a terminal *outside* Herdr, then run `hwt cleanup <branch>` from a Herdr tab in another workspace. It fails, and the worktree's workspace stays open. Close the outside terminal, then run the `hwt cleanup <path>` from the message. The worktree's workspace must close by itself and the cleanup finish with `"resumed": true`. Then repeat the retry from a tab *inside* that workspace: it must refuse with "has to close first; run … from another workspace" and delete nothing.
- **R5c. Same branch name in two repositories (1.4.2).**
  - Make worktrees with the same branch name in two configured repositories, then cut one's cleanup short as in R5.
  - Run `hwt cleanup <branch>`. It must list both (the half-done one marked "cleanup cut short") and ask, not pick one.
  - Run it from the Desktop panel, or with no terminal, and it must stop with "re-run with one of the listed names or paths".
  - Finally `hwt cleanup <path>` finishes the half-done one only, and the other repository's worktree is still there.
- **R6. New worktree with Claude (bug 4).** In a folder Claude hasn't seen, run `hwt new` (TUI, `start_agent = true`, agent `claude`).
  - The bootstrap finishes after "the agent started and is waiting for an answer in its tab", with no error.
  - `hwt status` shows the lease.
  - The agent tab shows Claude's trust question, which appeared only after `node_modules` was ready.
- **R7. Re-opening (bugs 6 and 8).**
  - Bug 6: with `{ path = "local.cfg", action = "symlink" }`, run `hwt open <wt>` twice. No "symlink escape".
  - Bug 8: with `policy = "clone"` and `core.autocrlf=true`, the bootstrap log says "cloned node_modules", not an install.
- **R8. Dirty check (bug 7).** With Developer Mode on and `policy = "shared"`, `hwt cleanup` of a merged worktree must not say "uncommitted changes".
- **R9. Small ones (bug 9).**
  - Run `irm …/install.ps1 | iex` with Herdr closed, then `$LASTEXITCODE`: it must be 0.
  - Put a broken `repos.d\x.toml` in place, run `hwt list`, and check that the error names `x.toml` with no traceback. Remove it afterwards.
  - In the TUI, a new worktree's server and shell tabs show the dim reminder with no `Write-Host …` command left above it.

## 0. Before you start

1. `where.exe python py python3 git hc herdr herdr-app`: note which exist and where. The Store's `python.exe`/`python3.exe` stubs in `WindowsApps` count as missing.
2. Is Developer Mode on? (Settings → System → For developers.) Run the junction steps (3.3, 3.4) once with it off. If it's on, also run 3.3 with it off.
3. If you can, also run sections 1, 2 and 4 once as a Windows user whose folder name has a non-ASCII letter (`C:\Users\Jürgen`).
4. Remove any earlier Corral: `py -3 $HOME\.local\share\herdr-corral\install.py --uninstall --purge`, then delete the clone.

## 1. Install (TUI flavor) and doctor

1. `git clone -b fix-windows https://github.com/bfreed/herdr-corral $HOME\.local\share\herdr-corral`
2. `py -3 $HOME\.local\share\herdr-corral\install.py --preflight`. Expect `os_ok: true` and Python 3.13. The `keybindings` section should read `%APPDATA%\herdr\config.toml`. If you have a palette binding there, it must be reported.
3. `py -3 $HOME\.local\share\herdr-corral\install.py --repos-root $HOME\repos --agent-kind claude`. Expect `Installed the hwt launcher at ...\.local\bin\hwt.cmd`. If the folder wasn't on PATH, also expect "Added … to your user PATH".
4. Open a **new** terminal. Check `Get-Content $HOME\.local\bin\hwt.cmd`: it should be one line, `@"<python>" "<clone>\hwt.py" %*`. Then check that `hwt --version` prints `hwt 1.3.0` and that `hwt bogus; $LASTEXITCODE` is `2`.
5. `hwt doctor` must report `"check": "launcher", "ok": true` and `command:herdr` ok.
6. `py -3 $HOME\.local\share\herdr-corral\install.py --verify` must be all `[ok]`. This includes the unit tests and `herdr-plugin`.
7. `herdr plugin list --json`: `corral` must be enabled, and its root must be `<clone>\.windows-plugin`. Open that folder's `herdr-plugin.toml`: every command must start with the full python path, then `<clone>\hwt.py`.

## 2. Herdr TUI

1. In Herdr, use its own **new worktree** dialog on `demo`. Check the following:
   - The `worktree.created` hook ran: there are `agent`, `shell` and `server` tabs, the agent started, and `.env` was copied.
   - `node_modules` was copied or installed.
   - In the `server` tab, `$env:PORT` is the leased port, and a dim "Corral: 'hwt dev' starts the dev server…" line is printed by `Write-Host` (not a `printf` error).
2. In the server tab, run `hwt dev`. It must start npm (a `.cmd` shim) on the leased port.
3. Run the palette action (`herdr plugin action invoke corral.palette`, or your binding). The popup must open with the worktree listed.
4. With the dev server still running, run `hwt cleanup <branch>` after merging the branch (or use `--abandon --confirm <branch>`).
   - Expect `stopped_servers` to list node, with `"signal": "KILL"`.
   - The worktree folder, the local branch and the remote branch must all be gone.
   - Nothing may be left in `netstat -ano | findstr :<port>`.
5. Run cleanup again with a second terminal `cd`'d into the worktree, where nothing listens on the port. Note what happens: Windows won't delete a folder that's in use. Expected: git reports it can't remove the folder, and Corral says so. Record the exact message.

## 3. Dependencies and links

1. `policy = "clone"` (the default) with matching lockfiles: the new worktree's `node_modules` is a full copy. The bootstrap log says "cloned node_modules".
2. With a pnpm repo (`pnpm-lock.yaml`, whose `node_modules` holds junctions), expect "not copying … it holds links". `pnpm install` should run instead.
3. Set `[repositories.demo.dependencies] policy = "shared"` in an overlay, then create a worktree.
   - `Get-Item <wt>\node_modules | Select LinkType, Target` should show `Junction` with Developer Mode off, or `SymbolicLink` with it on.
   - Then `hwt cleanup` the worktree. The **main checkout's** `node_modules` must still be complete: compare the file counts before and after. This is the most important check in this plan.
4. A `files` entry `{ path = "local.cfg", action = "symlink" }` with Developer Mode off: the worktree gets a copy, the log says "copied local path (this Windows can't make symlinks)", and `hwt open` again doesn't error.

## 4. Herdr Desktop: this computer as the space's machine

1. Install the panel: `irm https://raw.githubusercontent.com/bfreed/herdr-corral/fix-windows/desktop/install.ps1 | iex` with `$env:CORRAL_REF = "fix-windows"` set first. Have Herdr Desktop open and showing `main`.
   - Expect: "Herdr now shows main-corral …", and the panel appears on the left within a few seconds.
   - Then `herdr-app status`: no errors.
2. Repeat step 1 with Herdr closed, using a fresh `HERDR_APP_HOME`. It must print the `herdr-app do layout.clone --name main --as main-corral` instruction. Run that command with Herdr open: the panel must dock.
3. Run `py -3 install.py --uninstall`, then focus a space in `C:\Users\<you>\repos\demo`.
   - The panel must show **Install Corral**.
   - Click it, then check that `%USERPROFILE%\.local\share\herdr-corral` is checked out at the release tag (needs a `v1.3.0` tag; until then, check it out by hand) and that `hwt.cmd` exists.
   - The panel must then move on to **Set up** or the repository.
4. **Set up** with a base branch, then **More settings…**: a `corral setup` tab must open running `hwt init` (as `python …\hwt.py init`).
5. **New worktree**:
   - The space must appear, be selected, and have agent/shell/server tabs, in that order and with those names.
   - `$env:PORT` must be set in the shell and server tabs, and the agent must have started.
6. On that worktree, **Start dev server**. It types `& '<python>' '<clone>\hwt.py' dev` into the server tab, in full because that PowerShell may predate the PATH Install set. The dot must turn green. Then click the dev URL.
7. Clicking a worktree row must select its space, even after you `cd` into a subfolder that uses different case or separators.
8. Use **Clean up…** and **Delete worktree and branch** on a merged worktree. Then do it on an unmerged one: the panel must show Corral's reason and **Delete *branch* anyway**.
9. Raise the version in the clone's `desktop/corral/mods/corral/main.js` (a local commit) and refresh. **Update panel** must copy it over `%USERPROFILE%\.config\herdr-app\mods\corral\main.js`, and the panel must reload.

## 5. Mixed machines

1. Window on Windows, space on a Linux or macOS machine added under Machines: the whole of section 4 must work unchanged. The probe goes through `sh -lc`.
2. Window on Linux or macOS, space on this Windows machine (added with `hc machine add`):
   - The panel must show the Windows paths and run New worktree, Start dev server and Clean up.
   - After the first refresh, look for an `sh` attempt in the window's log. Expect one only for the first probe of that machine.
3. A Windows machine with Git for Windows' `sh` on PATH (`C:\Program Files\Git\usr\bin` added) must still be treated as Windows: Install and hwt must run through Python, not `sh`.
4. A Windows machine whose `py` is too old (3.10) while `python` is 3.11+ must use `python`. One with only `python` (no `py`) must work, and so must one with only `python3`. With neither, the panel must say "Corral needs Python 3.11+ on <machine>".

## 6. Updating

1. `hwt update` from a terminal. It must pull, rewrite `.windows-plugin\herdr-plugin.toml` and relink, and `herdr plugin list` must still show `corral` enabled.
2. From the panel on an older install: **Update Corral**.

## Report back

For each failure, give:

- the step number;
- the exact output, including the text of any `hwt` JSON or error;
- `herdr-app status`, for panel issues;
- `Get-Content "$env:APPDATA\herdr\herdr.log" -Tail 50`, for plugin hook issues.
