#!/usr/bin/env python3
"""Install Corral for the current user. Run this from a git clone of the repo;
the clone itself is the installation, which is what lets `hwt update` work."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

PLUGIN_ID = "corral"

# Plain (unshifted) prefix+<key> bindings Herdr occupies by default, from
# KeysConfig::default in Herdr's src/config/model.rs. A user [[keys.command]]
# entry on one of these silently displaces the default, so the palette
# suggestion must avoid them as well as the user's own bindings.
HERDR_DEFAULT_PREFIX_KEYS = {
    "?", "s", "w", "g", "q", "o", "c", "p", "n", "e", "[", "h", "j", "k", "l",
    "tab", "v", "minus", "x", "z", "r", "b",
    "1", "2", "3", "4", "5", "6", "7", "8", "9",
}
PALETTE_KEY_CANDIDATES = "uiymfdat"
SHELL_PROBE_TIMEOUT = 8.0

CANDIDATE_REPO_PARENTS = ("repos", "code", "src", "dev", "projects",
                          "GitRepos", "Documents/GitRepos")


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


def note(message: str) -> None:
    print(message, file=sys.stderr)


def command_output(argv: list[str], timeout: float = 10.0) -> str | None:
    try:
        result = subprocess.run(argv, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def read_toml(path: Path) -> dict | None:
    """{} when the file is absent (defaults apply); None when it cannot be read.

    tomllib is stdlib only from Python 3.11, and preflight must still emit its
    report on older interpreters (where the Python prerequisite check is the
    finding), so the import is deferred and every failure degrades to None."""
    if not path.exists():
        return {}
    try:
        import tomllib
        return tomllib.loads(path.read_text())
    except Exception:
        return None


def herdr_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "herdr" / "config.toml"


def preflight_prerequisites() -> dict:
    return {
        "python_version": ".".join(map(str, sys.version_info[:3])),
        "python_ok": sys.version_info[:2] >= (3, 11),
        "git": shutil.which("git"),
        "herdr": shutil.which("herdr"),
        "herdr_version": command_output(["herdr", "--version"]) if shutil.which("herdr") else None,
    }


def count_git_children(parent: Path) -> int:
    try:
        return sum(1 for child in parent.iterdir() if child.is_dir()
                   and not child.name.startswith(".") and (child / ".git").exists())
    except OSError:
        return 0


def preflight_repo_parents(home: Path) -> list[dict]:
    # Conventional candidates count from one repository; other first-level
    # home directories need two so Downloads-style folders stay out. Sorted by
    # count so the interview's "recommended" ordering is deterministic.
    conventional = [home / rel for rel in CANDIDATE_REPO_PARENTS]
    try:
        extras = sorted(d for d in home.iterdir()
                        if d.is_dir() and not d.name.startswith("."))
    except OSError:
        extras = []
    seen, found = set(), []
    for parent, threshold in [(p, 1) for p in conventional] + [(p, 2) for p in extras]:
        if not parent.is_dir():
            continue
        resolved = parent.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        count = count_git_children(parent)
        if count >= threshold:
            found.append({"path": str(parent), "repositories": count})
    return sorted(found, key=lambda item: (-item["repositories"], item["path"]))


def preflight_worktrees(home: Path, herdr_cfg: dict | None, parents: list[dict]) -> dict:
    configured = (herdr_cfg or {}).get("worktrees", {}).get("directory")
    herdr_dir = Path(configured).expanduser() if configured else home / ".herdr/worktrees"
    siblings = []
    for entry in parents:
        try:
            siblings += sorted(str(d) for d in Path(entry["path"]).iterdir()
                               if d.is_dir() and d.name.endswith("__worktrees"))
        except OSError:
            pass
    corral = read_toml(home / ".config/herdr-corral/config.toml") or {}
    return {
        "herdr_directory": str(herdr_dir),
        "herdr_directory_exists": herdr_dir.is_dir(),
        "herdr_directory_configured": bool(configured),
        "sibling_collections": siblings,
        "previous_corral_root": corral.get("worktree_root"),
    }


def preflight_network() -> dict:
    report = {"hostname": socket.getfqdn(),
              "tailscale_dns_name": None, "tailscale_ip": None}
    if shutil.which("tailscale"):
        raw = command_output(["tailscale", "status", "--json"])
        if raw:
            try:
                report["tailscale_dns_name"] = (
                    json.loads(raw).get("Self", {}).get("DNSName") or "").rstrip(".") or None
            except json.JSONDecodeError:
                pass
        addresses = command_output(["tailscale", "ip", "-4"])
        if addresses:
            report["tailscale_ip"] = addresses.splitlines()[0].strip()
    return report


def herdr_agent_kinds() -> list[str]:
    # The accepted kinds come from Herdr's own help output; a format change
    # degrades this to an empty list rather than failing preflight.
    help_text = command_output(["herdr", "agent", "start", "--help"]) or ""
    match = re.search(r"\[possible values: ([^\]]+)\]", help_text)
    return [kind.strip() for kind in match.group(1).split(",")] if match else []


def probe_agent_clis(kinds: list[str], home: Path) -> dict[str, str]:
    """Three-way usability probe: PATH, login+interactive shell, vendor dirs.

    Agent CLIs commonly live in per-vendor bin directories whose PATH entry
    only exists in the user's rc files, so a plain which() miss is not
    conclusive. The shell probe gets a timeout: rc files may prompt."""
    found = {}
    for kind in kinds:
        path = shutil.which(kind)
        if path:
            found[kind] = path
    remaining = [k for k in kinds if k not in found]
    if remaining:
        shell = os.environ.get("SHELL") or "/bin/sh"
        try:
            probe = subprocess.run([shell, "-lic", "command -v " + " ".join(remaining)],
                                   text=True, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, timeout=SHELL_PROBE_TIMEOUT)
            for line in probe.stdout.splitlines():
                line = line.strip()
                if line.startswith("/") and Path(line).name in remaining:
                    found[Path(line).name] = line
        except (OSError, subprocess.TimeoutExpired):
            pass
    for kind in kinds:
        if kind not in found:
            hits = sorted(home.glob(f".*/bin/{kind}")) + sorted(home.glob(f".local/bin/{kind}"))
            if hits:
                found[kind] = str(hits[0])
    return found


def preflight_keybindings(herdr_cfg: dict | None) -> dict:
    if herdr_cfg is None:
        return {"available": False}
    keys = herdr_cfg.get("keys", {})
    commands = [e for e in keys.get("command", []) if isinstance(e, dict)]
    user_keys = {str(e.get("key", "")).removeprefix("prefix+")
                 for e in commands if str(e.get("key", "")).startswith("prefix+")}
    occupied = HERDR_DEFAULT_PREFIX_KEYS | user_keys
    palette = next((e for e in commands
                    if e.get("command") == f"{PLUGIN_ID}.palette"), None)
    suggestion = next((f"prefix+{key}" for key in PALETTE_KEY_CANDIDATES
                       if key not in occupied), None)
    return {
        "available": True,
        "prefix": keys.get("prefix", "ctrl+b"),
        "palette_binding": palette.get("key") if palette else None,
        "command_bindings": [{"key": e.get("key"), "command": e.get("command")}
                             for e in commands],
        "suggested_palette_key": suggestion,
    }


def cmd_preflight() -> int:
    home = Path.home()
    herdr_cfg = read_toml(herdr_config_path())
    prereqs = preflight_prerequisites()
    parents = preflight_repo_parents(home)
    kinds = herdr_agent_kinds() if prereqs["herdr"] else []
    report = {
        "prerequisites": prereqs,
        "repository_parents": parents,
        "worktrees": preflight_worktrees(home, herdr_cfg, parents),
        "network": preflight_network(),
        "agents": {"kinds": kinds, "usable": probe_agent_clis(kinds, home)},
        "keybindings": preflight_keybindings(herdr_cfg),
    }
    print(json.dumps(report, indent=2))
    note(f"Python {prereqs['python_version']} ({'ok' if prereqs['python_ok'] else 'needs 3.11+'});"
         f" git {'found' if prereqs['git'] else 'MISSING'};"
         f" herdr {prereqs['herdr_version'] or 'MISSING'}")
    note("Repository parents: " + (", ".join(
        f"{p['path']} ({p['repositories']})" for p in report["repository_parents"]) or "(none found)"))
    note(f"Worktrees: {report['worktrees']['herdr_directory']}"
         f" ({'exists' if report['worktrees']['herdr_directory_exists'] else 'not created yet'})")
    network = report["network"]
    note("Network names: " + ", ".join(
        x for x in (network["hostname"], network["tailscale_dns_name"], network["tailscale_ip"]) if x))
    note("Usable agents: " + (", ".join(sorted(report["agents"]["usable"])) or "(none detected)"))
    binding = report["keybindings"]
    if binding.get("available"):
        note(f"Palette binding: {binding['palette_binding'] or 'unbound'}"
             f" (suggestion: {binding['suggested_palette_key']}, prefix {binding['prefix']})")
    else:
        note("Palette binding: Herdr config unreadable; skipped")
    return 0


def verify_plugin_registration() -> tuple[bool, str]:
    if shutil.which("herdr") is None:
        return False, "herdr is not on PATH"
    raw = command_output(["herdr", "plugin", "list", "--json"], timeout=15)
    if not raw:
        return False, "herdr plugin list --json failed"
    try:
        plugins = json.loads(raw).get("result", {}).get("plugins", [])
    except json.JSONDecodeError:
        return False, "herdr plugin list returned invalid JSON"
    # Match loosely on the fields that matter, not the envelope shape.
    plugin = next((p for p in plugins
                   if PLUGIN_ID in (p.get("plugin_id"), p.get("id"))), None)
    if plugin is None:
        return False, f"plugin {PLUGIN_ID!r} is not registered"
    if not plugin.get("enabled", False):
        return False, f"plugin {PLUGIN_ID!r} is disabled"
    actions = {a.get("id") for a in plugin.get("actions", [])}
    missing = {"cleanup", "palette"} - actions
    if missing:
        return False, f"plugin actions missing: {', '.join(sorted(missing))}"
    return True, "linked and enabled with cleanup + palette actions"


def cmd_verify(source: Path, tests_dir: Path | None) -> int:
    checks = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    suite = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", str(tests_dir or source / "tests")],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=str(source))
    # Quiet on success: the per-test output only appears when something failed.
    check("unit-tests", suite.returncode == 0,
          "" if suite.returncode == 0 else suite.stdout[-4000:])
    launcher = Path.home() / ".local/bin/hwt"
    launcher_ok = launcher.exists() and subprocess.run(
        [str(launcher), "--help"], stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL).returncode == 0
    check("hwt-launcher", launcher_ok, str(launcher))
    config = Path.home() / ".config/herdr-corral/config.toml"
    check("configuration", config.exists(), str(config))
    plugin_ok, plugin_detail = verify_plugin_registration()
    check("herdr-plugin", plugin_ok, plugin_detail)
    for entry in checks:
        note(f"[{'ok' if entry['ok'] else 'FAIL'}] {entry['check']}"
             + (f" — {entry['detail']}" if entry["detail"] and (not entry["ok"] or entry["check"] != "unit-tests") else ""))
    verdict = all(entry["ok"] for entry in checks)
    print(json.dumps({"ok": verdict, "checks": checks}, indent=2))
    return 0 if verdict else 1


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
    parser.add_argument("--preflight", action="store_true",
                        help="read-only machine detection report for the guided install")
    parser.add_argument("--verify", action="store_true",
                        help="one-command post-install verification (tests, launcher, plugin)")
    parser.add_argument("--verify-tests-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--uninstall", action="store_true",
                        help="remove the hwt launcher and unlink the plugin")
    parser.add_argument("--purge", action="store_true",
                        help="with --uninstall: also delete configuration and runtime state")
    parser.add_argument("--skip-plugin", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    source = Path(__file__).resolve().parent
    if args.preflight:
        return cmd_preflight()
    if args.verify:
        return cmd_verify(source, args.verify_tests_dir)
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
