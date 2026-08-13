# Contributing to Corral

Thanks for helping! A few ground rules keep this codebase safe to hack on:

- **Standard library only.** `hwt.py` and `install.py` must run on a bare Python 3.11+ with no third-party packages.
- **No manager layers.** No retry frameworks, config abstraction layers, or logging frameworks. Small, direct functions.
- **Safety first.** Anything that deletes worktrees, branches, or files must keep the existing guarantees: path containment under approved roots, worktree/repository identity validation, fetch-then-prove-merge before deletion, exact `--confirm` for abandonment. Tests for these paths are mandatory.
- **Never surprise the user.** No auto-started dev servers, no test tabs, no writing outside `~/.config/herdr-corral`, `~/.local/state/herdr-corral`, `~/.local/bin`, and the worktrees themselves.
- **Tests.** `python3 -m unittest discover -s tests -v` must pass. POSIX-specific tests (symlinks, modes) are guarded so the suite also runs on Windows.
- **Version bumps.** `version` in `herdr-plugin.toml` and `__version__` in `hwt.py` must match (a test enforces this). Bump both in any behavior-changing PR and add a CHANGELOG entry.

## Wanted

- **Windows support** (Herdr for Windows is in beta): a `hwt.cmd`/PowerShell launcher, `python3` vs `python` handling in the manifest, and CI coverage. The locking and process code is already portable.
- Copy-on-write `node_modules` seeding (clonefile/reflink) as a faster alternative to `policy = "auto"` installs.
