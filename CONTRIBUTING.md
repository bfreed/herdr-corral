# Contributing to Corral

Thanks for helping! A few ground rules keep this codebase safe to hack on:

- **Standard library only.** `hwt.py` and `install.py` must run on a bare Python 3.11+ with no third-party packages.
- **No manager layers.** No retry frameworks, config abstraction layers, or logging frameworks. Small, direct functions.
- **Safety first.** Anything that deletes worktrees, branches, or files must keep the existing guarantees: path containment under approved roots, worktree/repository identity validation, fetch-then-prove-merge before deletion, exact `--confirm` for abandonment. Tests for these paths are mandatory.
- **Never surprise the user.** No auto-started dev servers, no test tabs, no writing outside `~/.config/herdr-corral`, `~/.local/state/herdr-corral`, `~/.local/bin`, the clone, and the worktrees themselves. (On Windows the installer also adds `~/.local/bin` to the user PATH and writes `.windows-plugin/` in the clone.)
- **Tests.** `python3 -m unittest discover -s tests -v` and `node --test desktop/tests/panel.test.mjs` must pass, on Linux, macOS and Windows (CI runs all three). POSIX-specific tests (symlinks, modes, `/proc`) are guarded so the suite also runs on Windows; `tests/test_windows.py` covers the Windows paths, with mocks everywhere and for real on Windows. To test a Windows branch on Linux, patch `hwt.WINDOWS` / `install.WINDOWS`, never `os.name` (pathlib reads it).
- **Version bumps.** `version` in `herdr-plugin.toml`, `__version__` in `hwt.py` and `VERSION` in the panel's `main.js` must match (tests enforce this). Bump all three in any behavior-changing PR and add a CHANGELOG entry.

## Wanted

- **A process's working folder on Windows** (reading another process's PEB), so cleanup can also stop servers there that don't hold the leased port, and `hwt reap` can find orphans.
- Copy-on-write `node_modules` seeding (clonefile/reflink) as a faster alternative to `policy = "auto"` installs.
