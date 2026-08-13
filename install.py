#!/usr/bin/env python3
"""Install Corral for the current user. Run this from a git clone of the repo;
the clone itself is the installation, which is what lets `hwt update` work."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

PLUGIN_ID = "corral"


def quote(value: str) -> str:
    return json.dumps(value)


def git_output(repo: Path, *args: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], text=True,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def discover_repositories(root: Path, exclude: Path | None = None) -> list[tuple[str, Path, str]]:
    found = []
    if not root.is_dir():
        return found
    for path in sorted(root.iterdir()):
        if not path.is_dir() or path.name.startswith("."):
            continue
        if exclude is not None and path.resolve() == exclude:
            continue
        top = git_output(path, "rev-parse", "--show-toplevel")
        if not top or Path(top).resolve() != path.resolve():
            continue
        # A linked worktree is its own toplevel but keeps its git dir in the
        # canonical repository; registering it would duplicate that repository.
        common = git_output(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if common:
            try:
                Path(common).resolve().relative_to(path.resolve())
            except ValueError:
                continue
        remote_head = git_output(path, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
        base = remote_head.removeprefix("refs/remotes/") if remote_head else None
        if not base:
            current = git_output(path, "branch", "--show-current") or "main"
            base = f"origin/{current}"
        found.append((path.name, path.resolve(), base))
    return found


def render_config(repos_root: Path, worktree_root: Path, repos: list[tuple[str, Path, str]],
                  dev_host: str, remote_host: str, agent_kind: str,
                  placement: str = "shared-root") -> str:
    lines = [
        f"canonical_root = {quote(str(repos_root.resolve()))}",
        f"worktree_root = {quote(str(worktree_root.resolve()))}",
        "additional_worktree_roots = []",
        f"dev_host = {quote(dev_host)}",
        f"remote_host = {quote(remote_host)}",
        f"agent_kind = {quote(agent_kind)}",
        '# "shared-root": hwt new uses worktree_root/<repo>/ — keep worktree_root equal',
        "# to Herdr's [worktrees].directory (~/.config/herdr/config.toml) so right-click",
        '# and hwt create worktrees in the same place. "sibling": <repo>__worktrees/',
        "# (workmux layout; Herdr's own dialog cannot follow a per-repo layout).",
        f"worktree_placement = {quote(placement)}",
        "",
        "[ports]",
        "start = 4100",
        "end = 4199",
        "",
        "# Repositories without an explicit `files` list get untracked .env / .env.*",
        "# files copied into new worktrees, and dependencies default to policy",
        '# "clone": copy-on-write copy of the main checkout\'s node_modules when',
        "# lockfiles match, else a lockfile-detected install. Run `hwt init` inside",
        "# a repository to refine its settings; that writes an overlay under repos.d/.",
    ]
    for name, path, base in repos:
        lines += [
            "",
            f"[repositories.{quote(name)}]",
            f"path = {quote(str(path))}",
            'mode = "worktree"',
            f"base_branch = {quote(base)}",
            'remote = "origin"',
            "fetch = true",
            "start_agent = true",
        ]
    return "\n".join(lines) + "\n"


def run_checked(argv: list[str]) -> None:
    result = subprocess.run(argv)
    if result.returncode:
        raise SystemExit(f"command failed ({result.returncode}): {' '.join(argv)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repos-root", type=Path, default=Path.home() / "repos")
    parser.add_argument("--worktree-root", type=Path,
                        help="shared worktree tree (default: ~/.herdr/worktrees, matching Herdr)")
    parser.add_argument("--worktree-placement", choices=("shared-root", "sibling"),
                        default="shared-root")
    parser.add_argument("--dev-host", default="127.0.0.1")
    parser.add_argument("--remote-host", default="")
    parser.add_argument("--agent-kind", default="hermes",
                        help="agent started in the 'agent' tab (default: hermes)")
    parser.add_argument("--force-config", action="store_true")
    parser.add_argument("--uninstall", action="store_true",
                        help="remove the hwt launcher and unlink the plugin")
    parser.add_argument("--purge", action="store_true",
                        help="with --uninstall: also delete configuration and runtime state")
    parser.add_argument("--skip-plugin", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    source = Path(__file__).resolve().parent
    if args.uninstall:
        if shutil.which("herdr"):
            subprocess.run(["herdr", "plugin", "unlink", PLUGIN_ID],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        launcher = Path.home() / ".local/bin/hwt"
        if launcher.exists():
            launcher.unlink()
        if args.purge:
            shutil.rmtree(Path.home() / ".config/herdr-corral", ignore_errors=True)
            shutil.rmtree(Path.home() / ".local/state/herdr-corral", ignore_errors=True)
        kept = "configuration kept" if not args.purge else "configuration and state deleted"
        print(f"Corral uninstalled ({kept}). Delete the clone itself if desired: {source}")
        return 0
    if not (source / ".git").exists():
        print("warning: this is not a git clone; `hwt update` will not work", file=sys.stderr)
    repos_root = args.repos_root.expanduser().resolve()
    if not repos_root.is_dir():
        parser.error(f"repository root does not exist: {repos_root}")
    worktree_root = (args.worktree_root or (Path.home() / ".herdr/worktrees")).expanduser()
    worktree_root.mkdir(parents=True, exist_ok=True)
    worktree_root = worktree_root.resolve()

    bin_dir = Path.home() / ".local/bin"
    config_dir = Path.home() / ".config/herdr-corral"
    bin_dir.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "repos.d").mkdir(exist_ok=True)

    launcher = bin_dir / "hwt"
    launcher.write_text(f'#!/bin/sh\nexec python3 "{source / "hwt.py"}" "$@"\n')
    launcher.chmod(0o755)

    config = config_dir / "config.toml"
    repos = discover_repositories(repos_root, exclude=source)
    if config.exists() and not args.force_config:
        config_result = "preserved existing configuration"
    else:
        if config.exists():
            backup = config.with_suffix(".toml.bak")
            shutil.copy2(config, backup)
        config.write_text(render_config(repos_root, worktree_root, repos,
                                        args.dev_host, args.remote_host, args.agent_kind,
                                        args.worktree_placement))
        config.chmod(0o600)
        config_result = f"configured {len(repos)} repositories"

    if not args.skip_plugin:
        if shutil.which("herdr") is None:
            raise SystemExit("Herdr is not on PATH; install Herdr, then rerun this installer")
        subprocess.run(["herdr", "plugin", "unlink", PLUGIN_ID], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        run_checked(["herdr", "plugin", "link", str(source), "--enabled"])

    print(f"Installed the hwt launcher at {launcher} (running from {source})")
    print(f"Configuration: {config} ({config_result})")
    print(f"Discovered repositories: {', '.join(name for name, _, _ in repos) or '(none)'}")
    if str(bin_dir) not in os.environ.get("PATH", "").split(os.pathsep):
        print(f"Add {bin_dir} to PATH before using hwt")
    print("Next: review config.toml, then run: hwt doctor")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
