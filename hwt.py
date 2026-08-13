#!/usr/bin/env python3
"""Corral: safe, configuration-driven Git-worktree workflow for Herdr."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any, Callable

try:
    import fcntl

    def lock_file(handle) -> None:
        fcntl.flock(handle, fcntl.LOCK_EX)
except ImportError:  # Windows: byte-range lock; a lock past EOF is permitted.
    import msvcrt

    def lock_file(handle) -> None:
        handle.seek(0)
        while True:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                return
            except OSError:
                continue


class WorkflowError(RuntimeError):
    pass


class SafetyError(WorkflowError):
    pass


__version__ = "0.10.3"

GITHUB_REPO = "bfreed/herdr-corral"
DEFAULT_CONFIG = Path.home() / ".config/herdr-corral/config.toml"
DEFAULT_STATE = Path.home() / ".local/state/herdr-corral"
PLUGIN_ID = "corral"
# Herdr's built-in "new worktree" flow checks out under [worktrees].directory,
# default ~/.herdr/worktrees/<repo>/<slug>. Approving it makes those worktrees
# first-class Corral citizens.
HERDR_NATIVE_WORKTREES = Path.home() / ".herdr/worktrees"


def runtime_state_dir() -> Path:
    # The CLI and Herdr event hook must share one lease database. Herdr's
    # plugin-specific state directory is intentionally not used for this.
    return Path(os.environ.get("HWT_STATE_DIR", DEFAULT_STATE)).expanduser()


def log(message: str) -> None:
    print(message, file=sys.stderr)


def safe_relative(value: str) -> Path:
    p = PurePosixPath(value)
    if not value or p.is_absolute() or any(part in ("", ".", "..") for part in p.parts):
        raise SafetyError(f"unsafe relative path: {value!r}")
    return Path(*p.parts)


def resolve_existing(path: Path) -> Path:
    try:
        return path.expanduser().resolve(strict=True)
    except FileNotFoundError as exc:
        raise SafetyError(f"path does not exist: {path}") from exc


def is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def require_within(path: Path, root: Path, *, existing: bool = True) -> Path:
    root_real = resolve_existing(root)
    if existing:
        target = resolve_existing(path)
    else:
        parent = resolve_existing(path.expanduser().parent)
        target = parent / path.name
    if target == root_real or is_within(target, root_real):
        return target
    raise SafetyError(f"refusing path outside approved root {root_real}: {target}")


def contained_destination(root: Path, relative: str) -> Path:
    rel = safe_relative(relative)
    root_real = resolve_existing(root)
    cursor = root_real
    for part in rel.parts[:-1]:
        cursor = cursor / part
        if cursor.exists() or cursor.is_symlink():
            resolved = cursor.resolve(strict=True)
            if not is_within(resolved, root_real):
                raise SafetyError(f"symlink escape in destination: {relative}")
        else:
            cursor.mkdir(mode=0o700)
    destination = cursor / rel.parts[-1]
    if destination.is_symlink():
        resolved = destination.resolve(strict=False)
        if not is_within(resolved, root_real):
            raise SafetyError(f"symlink escape in destination: {relative}")
    return destination


def safe_source(root: Path, relative: str) -> Path:
    source = resolve_existing(root) / safe_relative(relative)
    resolved = source.resolve(strict=True)
    if not is_within(resolved, resolve_existing(root)):
        raise SafetyError(f"source escapes canonical checkout: {relative}")
    return source


def load_config(path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    with path.open("rb") as fh:
        cfg = tomllib.load(fh)
    # Per-repository overlays written by `hwt init`; each replaces the whole
    # matching repository entry so it must be self-contained.
    repos_d = path.parent / "repos.d"
    if repos_d.is_dir():
        for extra in sorted(repos_d.glob("*.toml")):
            with extra.open("rb") as fh:
                overlay = tomllib.load(fh)
            for name, repo in overlay.get("repositories", {}).items():
                cfg.setdefault("repositories", {})[name] = repo
    for key in ("canonical_root", "worktree_root"):
        if key not in cfg:
            raise WorkflowError(f"missing configuration key: {key}")
    cfg["canonical_root"] = str(resolve_existing(Path(cfg["canonical_root"])))
    cfg["worktree_root"] = str(resolve_existing(Path(cfg["worktree_root"])))
    cfg["additional_worktree_roots"] = [
        str(resolve_existing(Path(root))) for root in cfg.get("additional_worktree_roots", [])]
    return cfg


def resolve_configured_repo(cfg: dict[str, Any], path: Path) -> tuple[str, dict[str, Any]]:
    target = resolve_existing(path)
    canonical_root = Path(cfg.get("canonical_root", target.parent)).resolve()
    if not is_within(target, canonical_root):
        raise SafetyError(f"repository outside approved canonical root: {target}")
    for name, repo in cfg.get("repositories", {}).items():
        configured = resolve_existing(Path(repo["path"]))
        if target == configured:
            return name, repo
    raise SafetyError(f"repository is not explicitly configured: {target}")


def run(argv: list[str], *, cwd: Path | None = None, check: bool = True,
        env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, cwd=cwd, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and result.returncode:
        raise WorkflowError(f"command failed ({argv[0]}) with exit status {result.returncode}")
    return result


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(["git", "-C", str(cwd), *args], check=check)


def branch_slug(branch: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", branch.strip("/"))
    slug = slug.strip(".-")
    if not slug or slug in {".", ".."}:
        raise SafetyError(f"unsafe branch name: {branch!r}")
    digest = hashlib.sha256(branch.encode()).hexdigest()[:8]
    return f"{slug}-{digest}"


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def apply_file_operations(canonical: Path, worktree: Path, operations: list[dict[str, Any]],
                          logger: Callable[[str], None] = log) -> None:
    canonical = resolve_existing(canonical)
    worktree = resolve_existing(worktree)
    for op in operations:
        rel = op["path"]
        action = op["action"]
        required = bool(op.get("required", False))
        overwrite = op.get("overwrite", "never")
        try:
            source = safe_source(canonical, rel)
        except (FileNotFoundError, SafetyError):
            if required and action != "create":
                raise WorkflowError(f"required local source missing: {rel}")
            if action != "create":
                logger(f"optional local source absent: {rel}")
                continue
            source = canonical / safe_relative(rel)
        destination = contained_destination(worktree, rel)
        if action == "copy":
            if not source.is_file() and not source.is_dir():
                raise WorkflowError(f"copy source is not a regular file or directory: {rel}")
            if destination.exists() or destination.is_symlink():
                if source.is_file() and destination.is_file() and not destination.is_symlink() and hash_file(destination) == hash_file(source):
                    logger(f"copy already current: {rel}")
                    continue
                if overwrite == "skip":
                    logger(f"keeping existing local file: {rel}")
                    continue
                if overwrite != "always":
                    raise WorkflowError(f"refusing to overwrite modified destination: {rel}")
                if destination.is_dir() and not destination.is_symlink():
                    shutil.rmtree(destination)
                else:
                    destination.unlink()
            if source.is_dir():
                shutil.copytree(source, destination, copy_function=shutil.copy2)
            else:
                shutil.copy2(source, destination)
                os.chmod(destination, stat.S_IMODE(source.stat().st_mode))
            logger(f"copied local path: {rel}")
        elif action == "symlink":
            if destination.is_symlink() and destination.resolve() == source.resolve():
                logger(f"symlink already current: {rel}")
                continue
            if destination.exists() or destination.is_symlink():
                raise WorkflowError(f"refusing to replace destination with symlink: {rel}")
            destination.symlink_to(source, target_is_directory=source.is_dir())
            logger(f"linked local path: {rel}")
        elif action == "create":
            kind = op.get("kind", "directory")
            if destination.exists() or destination.is_symlink():
                valid = (kind == "directory" and destination.is_dir() and not destination.is_symlink()) or (kind == "file" and destination.is_file() and not destination.is_symlink())
                if valid:
                    continue
                raise WorkflowError(f"existing destination has wrong type for {kind}: {rel}")
            if kind == "directory":
                destination.mkdir(parents=True, mode=int(str(op.get("mode", "700")), 8))
            elif kind == "file":
                destination.touch(mode=int(str(op.get("mode", "600")), 8))
            else:
                raise WorkflowError(f"unsupported create kind: {kind}")
            logger(f"created local path: {rel}")
        else:
            raise WorkflowError(f"unsupported file action: {action}")


DEFAULT_ENV_GLOBS = (".env", ".env.*")

LOCKFILE_COMMANDS = (
    ("pnpm-lock.yaml", ["pnpm", "install"]),
    ("yarn.lock", ["yarn", "install"]),
    ("bun.lockb", ["bun", "install"]),
    ("bun.lock", ["bun", "install"]),
    ("package-lock.json", ["npm", "ci"]),
)


def default_env_operations(canonical: Path) -> list[dict[str, Any]]:
    """Repositories without a `files` list get untracked top-level env files copied."""
    operations = []
    seen: set[str] = set()
    for pattern in DEFAULT_ENV_GLOBS:
        for source in sorted(canonical.glob(pattern)):
            if not source.is_file() or source.is_symlink() or source.name in seen:
                continue
            seen.add(source.name)
            tracked = git(canonical, "ls-files", "--error-unmatch", source.name,
                          check=False).returncode == 0
            if tracked:
                continue
            operations.append({"path": source.name, "action": "copy",
                               "required": False, "overwrite": "skip"})
    return operations


def detect_install_command(worktree: Path) -> list[str] | None:
    if not (worktree / "package.json").is_file():
        return None
    for lockfile, command in LOCKFILE_COMMANDS:
        if (worktree / lockfile).is_file():
            return list(command)
    return ["npm", "install"]


def prepare_dependencies(canonical: Path, worktree: Path, dep: dict[str, Any]) -> str:
    policy = dep.get("policy", "independent")
    directory = safe_relative(dep.get("directory", "node_modules"))
    canonical_dir = canonical / directory
    # Validate the parent without following a possibly stale dependency link.
    destination = contained_destination(worktree, (directory.parent / ".hwt-path-check").as_posix()).parent / directory.name
    if policy == "auto":
        # Best-effort default: detect the package manager from the lockfile and
        # install. Failures warn instead of aborting the bootstrap.
        if destination.is_dir() and not destination.is_symlink():
            return "present"
        if destination.is_symlink():
            destination.unlink()
        command = detect_install_command(worktree)
        if not command:
            return "none"
        if shutil.which(command[0]) is None:
            log(f"warning: {command[0]} is not on PATH; skipping dependency install")
            return "skipped"
        log(f"installing dependencies: {' '.join(command)}")
        result = run(command, cwd=worktree, check=False)
        if result.returncode:
            log(f"warning: dependency install failed ({' '.join(command)}); run it manually")
            return "install-failed"
        return "installed"
    lock_name = dep.get("lockfile")
    matching = False
    if lock_name:
        c_lock = safe_source(canonical, lock_name)
        w_lock = safe_source(worktree, lock_name)
        matching = hash_file(c_lock) == hash_file(w_lock)
    share = policy == "shared" or (policy == "shared-if-lockfile-matches" and matching)
    if share:
        canonical_dir = safe_source(canonical, directory.as_posix())
        if not canonical_dir.is_dir(): raise WorkflowError(f"canonical dependency directory missing: {canonical_dir}")
        if destination.is_symlink() and destination.resolve() == canonical_dir.resolve():
            return "shared"
        if destination.is_symlink():
            destination.unlink()
        if destination.exists() or destination.is_symlink():
            raise WorkflowError(f"dependency destination already exists and is not the expected link: {destination}")
        destination.symlink_to(canonical_dir.resolve(), target_is_directory=True)
        return "shared"
    if destination.is_symlink():
        destination.unlink()
    command = dep.get("install_command", [])
    if command:
        run([str(x) for x in command], cwd=worktree)
    return "independent"


def prepare_local_cache_paths(worktree: Path, paths: list[str]) -> None:
    for relative in paths:
        destination = contained_destination(worktree, relative)
        if destination.is_symlink():
            raise SafetyError(f"worktree-local cache may not be a symlink: {relative}")
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)


@contextlib.contextmanager
def locked_json(state_dir: Path, name: str):
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = state_dir / f"{name}.lock"
    with lock_path.open("a+") as lock:
        lock_file(lock)
        data_path = state_dir / f"{name}.json"
        try:
            data = json.loads(data_path.read_text()) if data_path.exists() else {}
        except json.JSONDecodeError as exc:
            raise WorkflowError(f"invalid state file: {data_path}") from exc
        yield data
        fd, temp = tempfile.mkstemp(prefix=f".{name}.", dir=state_dir)
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
                fh.write("\n")
                fh.flush(); os.fsync(fh.fileno())
            os.replace(temp, data_path)
            os.chmod(data_path, 0o600)
        finally:
            if os.path.exists(temp): os.unlink(temp)


@contextlib.contextmanager
def worktree_lock(state_dir: Path, worktree: Path):
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    digest = hashlib.sha256(str(worktree.resolve()).encode()).hexdigest()
    with (state_dir / f"bootstrap-{digest}.lock").open("a+") as lock:
        lock_file(lock)
        yield


def port_is_free(port: int, host: str = "0.0.0.0") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def allocate_port(state_dir: Path, worktree: str, start: int, end: int,
                  requested: int | None = None) -> int:
    key = str(Path(worktree).resolve())
    with locked_json(state_dir, "ports") as leases:
        if key in leases:
            current = int(leases[key])
            if requested is None or requested == current:
                return current
            del leases[key]
        used = {int(v) for k, v in leases.items() if k != key}
        candidates = [requested] if requested is not None else list(range(start, end + 1))
        for port in candidates:
            if port is None or not start <= port <= end:
                raise WorkflowError(f"port must be in configured range {start}-{end}")
            if port in used:
                continue
            if not port_is_free(port):
                continue
            leases[key] = port
            return port
    raise WorkflowError(f"no available development port in {start}-{end}")


def release_port(state_dir: Path, worktree: str) -> None:
    key = str(Path(worktree).resolve())
    with locked_json(state_dir, "ports") as leases:
        leases.pop(key, None)


class Herdr:
    def __init__(self, binary: str | None = None):
        self.binary = binary or os.environ.get("HERDR_BIN_PATH", "herdr")

    def call(self, *args: str) -> dict[str, Any]:
        result = run([self.binary, *args])
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise WorkflowError(f"Herdr returned invalid JSON for {' '.join(args)}") from exc

    def tabs(self, workspace: str) -> list[dict[str, Any]]:
        tabs = self.call("tab", "list", "--workspace", workspace)["result"]["tabs"]
        panes = self.call("pane", "list", "--workspace", workspace)["result"]["panes"]
        root_by_tab = {}
        for pane in panes:
            root_by_tab.setdefault(pane["tab_id"], pane["pane_id"])
        for tab in tabs:
            tab["pane_id"] = root_by_tab.get(tab["tab_id"])
        return tabs

    def rename_tab(self, tab_id: str, label: str) -> None:
        self.call("tab", "rename", tab_id, label)

    def create_tab(self, workspace: str, cwd: Path, label: str, env: dict[str, str]) -> tuple[str, str]:
        args = ["tab", "create", "--workspace", workspace, "--cwd", str(cwd), "--label", label, "--no-focus"]
        for key, value in env.items(): args += ["--env", f"{key}={value}"]
        obj = self.call(*args)["result"]
        return obj["tab"]["tab_id"], obj["root_pane"]["pane_id"]

    def start_agent(self, pane_id: str, name: str, kind: str = "hermes") -> None:
        self.call("agent", "start", name, "--kind", kind, "--pane", pane_id, "--timeout", "120000")

    def has_agent(self, pane_id: str) -> bool:
        panes = self.call("agent", "list")["result"].get("agents", [])
        return any(item.get("pane_id") == pane_id for item in panes)

    def metadata(self, workspace: str, tokens: dict[str, str]) -> None:
        args = ["workspace", "report-metadata", workspace, "--source", PLUGIN_ID]
        for key, value in tokens.items(): args += ["--token", f"{key}={value}"]
        run([self.binary, *args])

    def show_reminder(self, pane_id: str, message: str) -> None:
        run([
            self.binary, "pane", "wait-output", pane_id, "--regex", r"[$#] ?$",
            "--source", "visible", "--lines", "5", "--timeout", "10000", "--raw",
        ])
        command = f"printf '\\n%s\\n\\n' {shlex.quote(message)}"
        run([self.binary, "pane", "run", pane_id, command])


class FakeHerdrForTests:
    def __init__(self):
        self.workspaces: dict[str, list[dict[str, str]]] = {}
        self.agent_starts = 0
        self.agent_panes: set[str] = set()
        self.reminders: list[tuple[str, str]] = []

    def seed_workspace(self, wid: str, cwd: str, labels: list[str]) -> None:
        self.workspaces[wid] = [{"tab_id": f"{wid}:t{i+1}", "label": x, "pane_id": f"{wid}:p{i+1}"} for i,x in enumerate(labels)]

    def tabs(self, workspace: str):
        return list(self.workspaces[workspace])

    def rename_tab(self, tab_id: str, label: str):
        for tab in sum(self.workspaces.values(), []):
            if tab["tab_id"] == tab_id: tab["label"] = label

    def create_tab(self, workspace: str, cwd: Path, label: str, env: dict[str,str]):
        n=len(self.workspaces[workspace])+1; tab={"tab_id":f"{workspace}:t{n}","label":label,"pane_id":f"{workspace}:p{n}"}
        self.workspaces[workspace].append(tab); return tab["tab_id"],tab["pane_id"]

    def start_agent(self, pane_id: str, name: str, kind: str = "hermes"):
        self.agent_starts += 1
        self.agent_panes.add(pane_id)
    def has_agent(self, pane_id: str) -> bool: return pane_id in self.agent_panes
    def metadata(self, workspace: str, tokens: dict[str,str]): pass
    def show_reminder(self, pane_id: str, message: str): self.reminders.append((pane_id, message))
    def labels(self, workspace: str): return [x["label"] for x in self.workspaces[workspace]]


def tab_pane(tab: dict[str, Any]) -> str:
    if "pane_id" in tab: return tab["pane_id"]
    # Live tab list omits root pane; caller resolves panes separately when needed.
    raise WorkflowError("pane id unavailable for tab")


def ensure_layout(herdr: Any, workspace: str, worktree: Path, port: int, host: str,
                  remote_host: str, start_agent: bool, repo: dict[str, Any] | None = None,
                  agent_kind: str = "hermes") -> None:
    env = {
        "HWT_WORKTREE": str(worktree), "HWT_DEV_PORT": str(port), "PORT": str(port),
        "HWT_DEV_HOST": host, "HOST": host,
        "HWT_LOCAL_URL": f"http://127.0.0.1:{port}",
        "HWT_REMOTE_URL": f"http://{remote_host}:{port}" if remote_host else "",
    }
    repo = repo or {}
    env.update({str(k): str(v) for k, v in repo.get("environment", {}).items()})
    tabs = herdr.tabs(workspace)
    by_label = {x["label"]: x for x in tabs}
    if "agent" not in by_label:
        unnamed = next((x for x in tabs if x["label"] not in {"shell", "server"}), None)
        if unnamed:
            herdr.rename_tab(unnamed["tab_id"], "agent"); unnamed["label"]="agent"; by_label["agent"]=unnamed
        else:
            tid,pid=herdr.create_tab(workspace,worktree,"agent",env); by_label["agent"]={"tab_id":tid,"pane_id":pid,"label":"agent"}
    for label in ("shell", "server"):
        if label not in by_label:
            tid,pid=herdr.create_tab(workspace,worktree,label,env); by_label[label]={"tab_id":tid,"pane_id":pid,"label":label}
            if label == "server":
                if repo.get("commands", {}).get("dev"):
                    herdr.show_reminder(pid, "To start the development server, run: hwt dev")
            if label == "shell" and repo.get("_suggest_init"):
                herdr.show_reminder(pid, "New repository for Corral: run 'hwt init' here to configure env files and a dev command.")
    if start_agent:
        agent = by_label["agent"]
        pane_id = agent.get("pane_id")
        if pane_id and not herdr.has_agent(pane_id):
            herdr.start_agent(pane_id, f"hwt-{hashlib.sha1(str(worktree).encode()).hexdigest()[:8]}", agent_kind)
    herdr.metadata(workspace, {"dev_port":str(port),"dev_url":env["HWT_REMOTE_URL"],"worktree":str(worktree)})


def ensure_canonical_layout(herdr: Any, workspace: str, repo_path: Path,
                            repo_name: str, start_agent: bool,
                            agent_kind: str = "hermes") -> None:
    env = {"HWT_REPOSITORY": repo_name, "HWT_WORKTREE": str(repo_path)}
    tabs = herdr.tabs(workspace)
    by_label = {x["label"]: x for x in tabs}
    if "agent" not in by_label:
        unnamed = next((x for x in tabs if x["label"] not in {"shell", "server"}), None)
        if unnamed:
            herdr.rename_tab(unnamed["tab_id"], "agent")
            unnamed["label"] = "agent"
            by_label["agent"] = unnamed
        else:
            tid, pid = herdr.create_tab(workspace, repo_path, "agent", env)
            by_label["agent"] = {"tab_id": tid, "pane_id": pid, "label": "agent"}
    for label in ("shell", "server"):
        if label not in by_label:
            tid, pid = herdr.create_tab(workspace, repo_path, label, env)
            by_label[label] = {"tab_id": tid, "pane_id": pid, "label": label}
    pane_id = by_label["agent"].get("pane_id")
    if start_agent and pane_id and not herdr.has_agent(pane_id):
        safe_name = re.sub(r"[^a-z0-9_-]", "-", repo_name.lower())[:20]
        herdr.start_agent(pane_id, f"hwt-{safe_name}", agent_kind)


def ensure_canonical_workspace(herdr: Any, repo_path: Path, repo_name: str,
                               start_agent: bool, agent_kind: str = "hermes") -> str:
    existing = find_workspace_for_path(herdr, repo_path, repo_name)
    if existing:
        ensure_canonical_layout(herdr, existing, repo_path, repo_name, start_agent, agent_kind)
        return existing
    obj = herdr.call("workspace", "create", "--cwd", str(repo_path),
                     "--label", repo_name, "--no-focus")
    workspace = obj["result"]["workspace"]["workspace_id"]
    ensure_canonical_layout(herdr, workspace, repo_path, repo_name, start_agent, agent_kind)
    return workspace


def check_remove_allowed(dirty: bool, force: bool, confirmation: str | None, branch: str) -> None:
    if not dirty: return
    if not force:
        raise WorkflowError("worktree has uncommitted changes; rerun with --force and --confirm BRANCH")
    if confirmation != branch:
        raise WorkflowError(f"force removal requires --confirm {branch!r}")


def sibling_worktree_dir(repo_path: Path) -> Path:
    """workmux-style convention: worktrees in <repo>__worktrees next to the repo."""
    return repo_path.parent / f"{repo_path.name}__worktrees"


def approved_worktree_roots(cfg: dict[str, Any]) -> list[Path]:
    roots = [Path(cfg["worktree_root"]), HERDR_NATIVE_WORKTREES,
             *map(Path, cfg.get("additional_worktree_roots", []))]
    roots += [sibling_worktree_dir(Path(repo["path"]))
              for repo in cfg.get("repositories", {}).values() if "path" in repo]
    return [root.resolve() for root in roots]


def is_in_approved_worktree_root(cfg: dict[str, Any], path: Path, *, strict: bool = True) -> bool:
    target = path.resolve(strict=strict)
    return any(is_within(target, root) for root in approved_worktree_roots(cfg))


def repo_for_worktree(cfg: dict[str, Any], path: Path) -> tuple[str, dict[str, Any], Path]:
    target = resolve_existing(path)
    # Strongest evidence first: a linked worktree's git common dir lives inside
    # its canonical repository, wherever the checkout itself happens to be.
    # Path conventions vary (sibling dirs, shared roots, flat ad-hoc names);
    # git's answer does not.
    common = git(target, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False)
    if common.returncode == 0 and common.stdout.strip():
        common_dir = Path(common.stdout.strip()).resolve()
        for name, repo in cfg.get("repositories", {}).items():
            if "path" not in repo:
                continue
            try:
                repo_path = Path(repo["path"]).expanduser().resolve(strict=True)
            except FileNotFoundError:
                continue
            if target != repo_path and is_within(common_dir, repo_path):
                return name, repo, repo_path
    # Path-convention fallback for checkouts git cannot identify here.
    approved = approved_worktree_roots(cfg)
    if any(is_within(target, root) for root in approved):
        for name, repo in cfg.get("repositories", {}).items():
            if "path" not in repo:
                continue
            sibling = sibling_worktree_dir(Path(repo["path"])).resolve()
            if sibling.exists() and is_within(target, sibling):
                return name, repo, resolve_existing(Path(repo["path"]))
        for root in approved:
            for name, repo in cfg.get("repositories", {}).items():
                repo_root = root / name
                if repo_root.exists() and is_within(target, repo_root.resolve()):
                    return name, repo, resolve_existing(Path(repo["path"]))
    raise SafetyError(f"path is not a linked worktree of any configured repository: {target}")


def validate_worktree_identity(cfg: dict[str, Any], worktree: Path) -> tuple[str, dict[str, Any], Path]:
    name, repo, canonical = repo_for_worktree(cfg, worktree)
    actual = Path(git(worktree, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if actual != worktree.resolve(): raise SafetyError("worktree path is not its Git top-level")
    common = Path(git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()).resolve()
    expected = Path(git(canonical, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()).resolve()
    if not is_within(expected, canonical.resolve()): raise SafetyError("configured canonical repository has a Git directory outside its approved path")
    if common != expected: raise SafetyError("worktree does not belong to configured canonical repository")
    return name, repo, canonical


def repo_is_unconfigured(repo: dict[str, Any]) -> bool:
    """True when the entry still has only installer defaults worth refining."""
    return ("files" not in repo and "dependencies" not in repo
            and not repo.get("commands", {}).get("dev"))


def bootstrap(cfg: dict[str, Any], state: Path, worktree: Path, workspace: str,
              herdr: Any | None = None, requested_port: int | None = None) -> dict[str, Any]:
    with worktree_lock(state, worktree):
        return _bootstrap_locked(cfg, state, worktree, workspace, herdr, requested_port)


def _bootstrap_locked(cfg: dict[str, Any], state: Path, worktree: Path, workspace: str,
                      herdr: Any | None = None, requested_port: int | None = None) -> dict[str, Any]:
    name, repo, canonical = validate_worktree_identity(cfg, worktree)
    ports=cfg.get("ports",{}); port=allocate_port(state,str(worktree),int(ports.get("start",4100)),int(ports.get("end",4199)),requested_port)
    try:
        file_ops = repo.get("files")
        if file_ops is None:
            file_ops = default_env_operations(canonical)
        apply_file_operations(canonical, worktree, file_ops)
        prepare_local_cache_paths(worktree, repo.get("dependencies", {}).get("local_cache_paths", []))
        h=herdr or Herdr()
        repo_view = dict(repo)
        if repo_is_unconfigured(repo):
            repo_view["_suggest_init"] = True
        ensure_layout(h,workspace,worktree,port,cfg.get("dev_host","127.0.0.1"),cfg.get("remote_host",""),bool(repo.get("start_agent",False)),repo_view,agent_kind=cfg.get("agent_kind","hermes"))
        # Tabs first, install second: the workspace appears immediately while a
        # potentially slow package-manager run happens afterwards.
        dep_status=prepare_dependencies(canonical,worktree,repo.get("dependencies",{"policy":"auto"}))
    except Exception:
        release_port(state, str(worktree))
        raise
    return {"repository":name,"worktree":str(worktree),"workspace_id":workspace,"port":port,"dependencies":dep_status}


def fetch_with_offline_fallback(repo: Path, remote: str) -> str:
    result=git(repo,"fetch",remote,check=False)
    if result.returncode==0: return "fetched"
    msg=(result.stderr+result.stdout).lower()
    network=("could not resolve host","network is unreachable","connection timed out","failed to connect","temporary failure in name resolution")
    if any(x in msg for x in network):
        log("warning: remote unavailable; proceeding from local refs")
        return "offline-local"
    raise WorkflowError("fetch failed and is not a recognized offline-network failure")


def json_result(obj: dict[str,Any], *keys: str) -> Any:
    cur: Any=obj
    for key in keys: cur=cur[key]
    return cur


def install_root() -> Path:
    return Path(__file__).resolve().parent


def version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", value)[:3])


def latest_remote_version(timeout: float = 3.0) -> str | None:
    """Version in the manifest on GitHub main, or None when offline. Never raises."""
    import urllib.request
    url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/herdr-plugin.toml"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            manifest = tomllib.loads(response.read().decode("utf-8"))
        version = str(manifest.get("version", "")).strip()
        return version or None
    except Exception:
        return None


def ask_yes_no(question: str, default: bool = True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    answer = input(f"{question} {suffix} ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def toml_quote(value: str) -> str:
    return json.dumps(value)


def package_manager_base(canonical: Path) -> str:
    for lockfile, command in LOCKFILE_COMMANDS:
        if (canonical / lockfile).is_file():
            return command[0]
    return "npm"


def suggest_dev_command(canonical: Path) -> list[str] | None:
    package = canonical / "package.json"
    if not package.is_file():
        return None
    try:
        scripts = json.loads(package.read_text()).get("scripts", {})
    except (json.JSONDecodeError, OSError):
        return None
    script = next((s for s in ("dev", "serve", "start") if s in scripts), None)
    if not script:
        return None
    return [package_manager_base(canonical), "run", script]


def render_repo_overlay(name: str, repo: dict[str, Any], files: list[dict[str, Any]],
                        dep_policy: str, dev: list[str] | None) -> str:
    lines = [
        "# Written by 'hwt init'. This file replaces the matching repository entry",
        "# in config.toml, so it must stay self-contained.",
        f"[repositories.{toml_quote(name)}]",
        f"path = {toml_quote(str(repo['path']))}",
        f"mode = {toml_quote(repo.get('mode', 'worktree'))}",
        f"base_branch = {toml_quote(repo.get('base_branch', 'origin/main'))}",
        f"remote = {toml_quote(repo.get('remote', 'origin'))}",
        f"fetch = {'true' if repo.get('fetch', True) else 'false'}",
        f"start_agent = {'true' if repo.get('start_agent', True) else 'false'}",
    ]
    environment = repo.get("environment")
    if environment:
        rendered = ", ".join(f"{toml_quote(str(k))} = {toml_quote(str(v))}" for k, v in environment.items())
        lines.append(f"environment = {{ {rendered} }}")
    ops = ", ".join(
        f'{{ path = {toml_quote(op["path"])}, action = "copy", required = false, overwrite = "skip" }}'
        for op in files)
    lines.append(f"files = [{ops}]")
    lines += [
        "",
        f"[repositories.{toml_quote(name)}.dependencies]",
        f"policy = {toml_quote(dep_policy)}",
        "",
        f"[repositories.{toml_quote(name)}.commands]",
    ]
    if dev:
        lines.append(f"dev = [{', '.join(toml_quote(x) for x in dev)}]")
    else:
        lines.append('# dev = ["npm", "run", "dev", "--", "--host", "{host}", "--port", "{port}"]')
    return "\n".join(lines) + "\n"


def run_init_interview(cfg: dict[str, Any], name: str, config_path: Path) -> Path:
    repo = cfg["repositories"][name]
    canonical = resolve_existing(Path(repo["path"]))
    print(f"Configuring repository {name} ({canonical})")
    env_ops = default_env_operations(canonical)
    files: list[dict[str, Any]] = []
    if env_ops:
        listing = ", ".join(op["path"] for op in env_ops)
        if ask_yes_no(f"Copy untracked env files into new worktrees ({listing})?", True):
            files = env_ops
    else:
        print("No untracked .env files found in the repository root.")
    dep_policy = "auto"
    install = detect_install_command(canonical)
    if install and not ask_yes_no(f"Run '{' '.join(install)}' automatically in new worktrees?", True):
        dep_policy = "independent"
    suggestion = suggest_dev_command(canonical)
    rendered = " ".join(suggestion) if suggestion else "none"
    raw = input(f"Dev server command [{rendered}]: ").strip()
    if not raw:
        dev = suggestion
    elif raw.lower() == "none":
        dev = None
    else:
        dev = shlex.split(raw)
    if dev:
        print("Tip: add {host}/{port} arguments if the server accepts them; 'hwt dev' substitutes them.")
    overlay_dir = config_path.parent / "repos.d"
    overlay_dir.mkdir(parents=True, exist_ok=True)
    overlay = overlay_dir / f"{name}.toml"
    overlay.write_text(render_repo_overlay(name, repo, files, dep_policy, dev))
    print(f"Wrote {overlay}")
    return overlay


def maybe_offer_init(args, cfg: dict[str, Any], name: str) -> dict[str, Any]:
    """One-time offer to configure a repository on its first worktree."""
    repo = cfg["repositories"][name]
    if not repo_is_unconfigured(repo) or not sys.stdin.isatty():
        return cfg
    with locked_json(runtime_state_dir(), "init-offers") as offers:
        if offers.get(name):
            return cfg
        offers[name] = True
    if ask_yes_no(f"First worktree for {name}. Configure env files and dev command now?", True):
        run_init_interview(cfg, name, args.config)
        return load_config(args.config)
    log("You can configure this repository later with: hwt init")
    return cfg


def cmd_init(args, cfg, state):
    if not sys.stdin.isatty():
        raise WorkflowError("hwt init is interactive; run it from a terminal")
    if args.repository:
        name = args.repository
        if name not in cfg.get("repositories", {}):
            raise WorkflowError(f"repository is not configured: {name}")
    else:
        top = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").stdout.strip())
        try:
            name, _ = resolve_configured_repo(cfg, top)
        except SafetyError:
            name, _, _ = repo_for_worktree(cfg, top)
    run_init_interview(cfg, name, args.config)


def cmd_update(args) -> None:
    root = install_root()
    if not (root / ".git").exists():
        raise WorkflowError(
            f"installation at {root} is not a git checkout; update it the way it was installed")
    previous = __version__
    result = run(["git", "-C", str(root), "pull", "--ff-only"], check=False)
    if result.returncode:
        raise WorkflowError(f"git pull failed: {result.stderr.strip() or result.stdout.strip()}")
    manifest = tomllib.loads((root / "herdr-plugin.toml").read_text())
    current = str(manifest.get("version", previous))
    if shutil.which("herdr"):
        subprocess.run(["herdr", "plugin", "unlink", PLUGIN_ID],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        relink = subprocess.run(["herdr", "plugin", "link", str(root), "--enabled"])
        if relink.returncode:
            raise WorkflowError(f"herdr plugin relink failed; run: herdr plugin link {root} --enabled")
    else:
        log("herdr is not on PATH; skipped plugin relink")
    print(json.dumps({"installed_from": str(root), "previous_version": previous,
                      "version": current, "up_to_date": current == previous}, indent=2))


def cmd_new(args, cfg, state, herdr: Herdr):
    repo_path=Path(args.repo) if args.repo else Path.cwd()
    name,repo=resolve_configured_repo(cfg,repo_path)
    if repo.get("mode")!="worktree": raise WorkflowError(f"{name} is configured for workspace opening only")
    cfg = maybe_offer_init(args, cfg, name)
    repo = cfg["repositories"][name]
    canonical=resolve_existing(Path(repo["path"])); branch=args.branch; base=args.base or repo.get("base_branch","main")
    run(["git","check-ref-format","--branch",branch])
    if repo.get("fetch",True): fetch_with_offline_fallback(canonical,repo.get("remote","origin"))
    canonical_workspace = ensure_canonical_workspace(herdr, canonical, name, False)
    placement = cfg.get("worktree_placement", "shared-root")
    if placement == "sibling":
        parent = sibling_worktree_dir(canonical)
    elif placement == "shared-root":
        parent = Path(cfg["worktree_root"]) / name
    else:
        raise WorkflowError(f"unknown worktree_placement: {placement!r} (use \"sibling\" or \"shared-root\")")
    path = parent / branch_slug(branch)
    created_namespace = not parent.exists()
    parent.mkdir(parents=True,exist_ok=True)
    if placement == "shared-root":
        require_within(parent,Path(cfg["worktree_root"]))
    command=["worktree","create","--workspace",canonical_workspace,"--cwd",str(canonical),"--branch",branch,"--base",base,"--path",str(path),"--label",f"{name}: {branch}","--no-focus" if args.background else "--focus"]
    try:
        obj=herdr.call(*command)
    except WorkflowError:
        # A failed create must not leave an empty namespace directory behind.
        if created_namespace:
            with contextlib.suppress(OSError):
                path.parent.rmdir()
        raise
    workspace=json_result(obj,"result","workspace","workspace_id")
    summary=bootstrap(cfg,state,path,workspace,herdr)
    if not args.background: herdr.call("workspace","focus",workspace)
    print(json.dumps({
        **summary,
        "branch": branch,
        "dev_url": f"http://{cfg.get('remote_host')}:{summary['port']}" if cfg.get("remote_host") else f"http://127.0.0.1:{summary['port']}",
        "commands": ["hwt dev", "hwt status"],
    }, indent=2))


PATH_KEYS = {"cwd", "path", "worktree_path", "checkout_path", "repo_root", "root",
             "root_path", "workspace_root", "repo_path", "checkout", "directory"}


def find_workspace_for_path(herdr: Herdr, path: Path, label: str | None = None) -> str | None:
    listing = herdr.call("workspace", "list")["result"]["workspaces"]
    target = path.resolve()

    def paths(value: Any, key: str = ""):
        if isinstance(value, dict):
            for k, v in value.items(): yield from paths(v, k)
        elif isinstance(value, list):
            for v in value: yield from paths(v, key)
        elif key in PATH_KEYS and isinstance(value, str):
            yield value

    def matches(value: str) -> bool:
        try:
            return Path(value).expanduser().resolve() == target
        except (OSError, ValueError):
            return False

    details = []
    for item in listing:
        detail = herdr.call("workspace", "get", item["workspace_id"])["result"]
        details.append((item, detail))
        if any(matches(value) for value in paths(item)) or any(matches(value) for value in paths(detail)):
            return item["workspace_id"]
    # Path-shape mismatches across Herdr versions must not create duplicate
    # workspaces, so fall back to the exact label when the caller knows it.
    if label is not None:
        for item, detail in details:
            labels = {item.get("label"), detail.get("label"),
                      detail.get("workspace", {}).get("label") if isinstance(detail.get("workspace"), dict) else None}
            if label in labels:
                return item["workspace_id"]
    return None


def cmd_open(args,cfg,state,herdr:Herdr):
    token=Path(args.target).expanduser()
    if token.exists(): path=resolve_existing(token)
    else:
        matches=[]
        wanted = {args.target}
        with contextlib.suppress(SafetyError):
            wanted.add(branch_slug(args.target))
        for name,repo in cfg["repositories"].items():
            if args.target==name: matches.append(Path(repo["path"]))
            candidates = [Path(cfg["worktree_root"])/name, HERDR_NATIVE_WORKTREES/name, sibling_worktree_dir(Path(repo["path"]))]
            for wtroot in candidates:
                if wtroot.exists(): matches += [p for p in wtroot.iterdir() if p.name in wanted]
        if len(matches)!=1: raise WorkflowError(f"target did not resolve uniquely: {args.target}")
        path=resolve_existing(matches[0])
    try:
        name,repo=resolve_configured_repo(cfg,path)
        canonical=True
    except SafetyError:
        name,repo,_=repo_for_worktree(cfg,path); canonical=False
    existing=find_workspace_for_path(herdr,path,name if canonical else None)
    if existing:
        if not canonical:
            summary = bootstrap(cfg, state, path, existing, herdr)
        else:
            ensure_canonical_layout(herdr, existing, path, name, bool(repo.get("start_agent", False)), cfg.get("agent_kind", "hermes"))
            summary = {"workspace_id": existing, "path": str(path), "repository": name}
        herdr.call("workspace", "focus", existing)
        print(json.dumps({**summary, "existing": True}, indent=2))
        return
    if canonical:
        obj=herdr.call("workspace","create","--cwd",str(path),"--label",name,"--focus")
        workspace=obj["result"]["workspace"]["workspace_id"]
        ensure_canonical_layout(herdr, workspace, path, name, bool(repo.get("start_agent", False)), cfg.get("agent_kind", "hermes"))
        print(json.dumps({"workspace_id":workspace,"path":str(path),"repository":name},indent=2))
    else:
        obj=herdr.call("worktree","open","--cwd",str(Path(repo["path"])),"--path",str(path),"--label",f"{name}: {path.name}","--focus")
        workspace=obj["result"]["workspace"]["workspace_id"]
        print(json.dumps(bootstrap(cfg,state,path,workspace,herdr),indent=2))


def current_worktree(cfg:dict[str,Any], cwd:Path)->tuple[str,dict[str,Any],Path]:
    top=Path(git(cwd,"rev-parse","--show-toplevel").stdout.strip()).resolve()
    name,repo,canonical=repo_for_worktree(cfg,top)
    return name,repo,top


def cmd_dev(args,cfg,state):
    name,repo,wt=current_worktree(cfg,Path.cwd()); ports=cfg.get("ports",{})
    port=allocate_port(state,str(wt),int(ports.get("start",4100)),int(ports.get("end",4199)),args.port)
    template = repo.get("commands", {}).get("dev")
    if not template: raise WorkflowError(f"no development command configured for {name}")
    host = cfg.get("dev_host", "127.0.0.1")
    command=[str(x).format(port=port,host=host,worktree=wt,repository=name) for x in template]
    env=os.environ.copy(); env.update({"PORT":str(port),"HOST":host,"HWT_DEV_PORT":str(port),"HWT_DEV_HOST":host})
    env.update({str(k): str(v) for k, v in repo.get("environment", {}).items()})
    os.chdir(wt)
    if os.name == "nt":
        raise SystemExit(subprocess.run(command, env=env).returncode)
    os.execvpe(command[0],command,env)


def cmd_status(args,cfg,state):
    leases={}
    p=state/"ports.json"
    if p.exists(): leases=json.loads(p.read_text())
    rows=[]
    for path,port in leases.items():
        exists=Path(path).exists(); mismatch=None
        if exists:
            try:
                name,repo,canonical=repo_for_worktree(cfg,Path(path)); dep=repo.get("dependencies",{}); lock=dep.get("lockfile")
                if lock and (Path(path)/lock).exists() and (canonical/lock).exists(): mismatch=hash_file(Path(path)/lock)!=hash_file(canonical/lock)
            except WorkflowError: mismatch="unknown"
        rows.append({"path":path,"port":port,"exists":exists,"lockfile_mismatch":mismatch,"listening":not port_is_free(int(port))})
    print(json.dumps({"leases":rows},indent=2))


def normalize_worktree_item(raw: dict[str, Any], repository: str) -> dict[str, Any]:
    """Herdr's worktree schema varies; canonicalize and let git fill the gaps."""
    item = dict(raw)
    nested = raw.get("worktree")
    if isinstance(nested, dict):
        for key, value in nested.items():
            item.setdefault(key, value)
    if not isinstance(item.get("path"), str):
        for key in ("worktree_path", "checkout_path", "cwd"):
            if isinstance(item.get(key), str):
                item["path"] = item[key]
                break
    path = item.get("path")
    on_disk = isinstance(path, str) and Path(path).is_dir()
    if not isinstance(item.get("branch"), str) and on_disk:
        found = git(Path(path), "branch", "--show-current", check=False)
        if found.returncode == 0 and found.stdout.strip():
            item["branch"] = found.stdout.strip()
    if "is_linked_worktree" not in item and on_disk:
        common = git(Path(path), "rev-parse", "--path-format=absolute", "--git-common-dir", check=False)
        gitdir = git(Path(path), "rev-parse", "--path-format=absolute", "--git-dir", check=False)
        if common.returncode == 0 and gitdir.returncode == 0:
            item["is_linked_worktree"] = common.stdout.strip() != gitdir.stdout.strip()
    item["repository"] = repository
    return item


def repo_is_canonical_checkout(repo_path: Path) -> bool:
    if not repo_path.is_dir():
        return False
    common = git(repo_path, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False)
    if common.returncode:
        return False
    return is_within(Path(common.stdout.strip()).resolve(), repo_path.resolve())


def configured_worktree_items(cfg: dict[str, Any], herdr: Any) -> list[dict[str, Any]]:
    # Two configured repositories can alias the same repo (e.g. a linked
    # worktree mistakenly registered as a repository); dedupe by checkout path,
    # preferring the entry backed by the canonical checkout.
    items: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for name, repo in cfg["repositories"].items():
        if repo.get("mode") != "worktree":
            continue
        canonical_source = repo_is_canonical_checkout(Path(repo["path"]))
        result = herdr.call("worktree", "list", "--cwd", str(repo["path"]))["result"]
        raw_items = next((result[key] for key in ("worktrees", "items", "entries")
                          if isinstance(result.get(key), list)), [])
        for raw in raw_items:
            item = normalize_worktree_item(raw, name)
            item["_canonical_source"] = canonical_source
            path = item.get("path")
            key = str(Path(path).resolve()) if isinstance(path, str) and path else None
            if key is not None and key in seen:
                if canonical_source and not items[seen[key]].get("_canonical_source"):
                    items[seen[key]] = item
                continue
            if key is not None:
                seen[key] = len(items)
            items.append(item)
    for item in items:
        item.pop("_canonical_source", None)
    return items


def cmd_list(args,cfg,state,herdr):
    print(json.dumps({"configured_repositories":cfg["repositories"],"worktrees":configured_worktree_items(cfg,herdr)},indent=2))


def worktree_item_ids(item: dict[str, Any]) -> tuple[str | None, Path]:
    """Workspace id is None when the worktree exists on disk but is not open in Herdr."""
    workspace = item.get("open_workspace_id") or item.get("workspace_id") or item.get("workspace", {}).get("workspace_id")
    raw_path = item.get("path") or item.get("worktree_path") or item.get("cwd")
    if not raw_path:
        raise WorkflowError("Herdr worktree entry lacks a checkout path")
    return workspace or None, Path(raw_path)


def match_removal_items(items: list[dict[str, Any]], target: str) -> list[dict[str, Any]]:
    matches = []
    for item in items:
        if not item.get("is_linked_worktree", False):
            continue
        path = Path(item.get("path", ""))
        exact = {item.get("branch"), item.get("open_workspace_id"), item.get("workspace_id"), str(path), path.name}
        if target in exact:
            matches.append(item)
    return matches


def worktree_match_keys(item: dict[str, Any]) -> list[str]:
    keys = [item.get("branch"), item.get("label")]
    path = item.get("path")
    if isinstance(path, str) and path:
        keys.append(Path(path).name)
    return [key for key in keys if isinstance(key, str) and key]


def fuzzy_worktree_matches(items: list[dict[str, Any]], target: str) -> list[dict[str, Any]]:
    import difflib
    needle = target.lower()
    hits = [item for item in items
            if any(needle in key.lower() or key.lower() in needle
                   for key in worktree_match_keys(item))]
    if hits:
        return hits
    universe: dict[str, dict[str, Any]] = {}
    for item in items:
        for key in worktree_match_keys(item):
            universe.setdefault(key, item)
    ordered = []
    for key in difflib.get_close_matches(target, list(universe), n=5, cutoff=0.5):
        if universe[key] not in ordered:
            ordered.append(universe[key])
    return ordered


def describe_worktree_item(item: dict[str, Any]) -> str:
    branch = item.get("branch") or "(detached)"
    return f"{item.get('repository', '?')}  {branch}  {item.get('path', '?')}"


def choose_worktree_item(items: list[dict[str, Any]], target: str | None) -> dict[str, Any]:
    linked = [item for item in items if item.get("is_linked_worktree", False)]
    if target:
        exact = match_removal_items(items, target)
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            pool, header = exact, f"target {target!r} matches several worktrees"
        else:
            pool, header = fuzzy_worktree_matches(linked, target), f"no exact match for {target!r}; similar worktrees"
    else:
        pool, header = linked, "linked worktrees"
    if not pool:
        raise WorkflowError("no linked worktrees found; run 'hwt list' to inspect Herdr's view")
    log(f"{header}:")
    for index, item in enumerate(pool, 1):
        log(f"  {index}. {describe_worktree_item(item)}")
    if not sys.stdin.isatty():
        raise WorkflowError("re-run with one of the listed branches or paths as the target")
    answer = input("Number (Enter cancels): ").strip()
    if not answer:
        raise WorkflowError("cancelled")
    try:
        return pool[int(answer) - 1]
    except (ValueError, IndexError):
        raise WorkflowError(f"invalid selection: {answer!r}") from None


def cmd_remove(args,cfg,state,herdr):
    target=args.target
    candidates=match_removal_items(configured_worktree_items(cfg, herdr), target)
    if len(candidates)!=1: raise WorkflowError("removal target must identify exactly one Herdr worktree")
    item=candidates[0]; workspace,path=worktree_item_ids(item)
    with worktree_lock(state, path):
        _, _, canonical = validate_worktree_identity(cfg, path)
        branch=git(path,"branch","--show-current").stdout.strip(); dirty=bool(git(path,"status","--porcelain").stdout.strip())
        check_remove_allowed(dirty,args.force,args.confirm,branch)
        # Re-read immediately before the destructive call while holding our workflow lock.
        branch2=git(path,"branch","--show-current").stdout.strip(); dirty2=bool(git(path,"status","--porcelain").stdout.strip())
        if branch2 != branch or dirty2 != dirty: raise WorkflowError("worktree changed while removal was being validated")
        if workspace:
            cmd=["worktree","remove","--workspace",workspace]
            if args.force: cmd.append("--force")
            herdr.call(*cmd)
        else:
            cmd=["worktree","remove"]
            if args.force: cmd.append("--force")
            git(canonical, *cmd, str(path))
        release_port(state,str(path))
    print(json.dumps({"removed":str(path),"branch_preserved":branch},indent=2))


def cmd_cleanup(args, cfg, state, herdr):
    item = choose_worktree_item(configured_worktree_items(cfg, herdr), args.target)
    result = cleanup_worktree_item(cfg, state, herdr, item,
                                   abandon=args.abandon, confirm=args.confirm)
    print(json.dumps(result, indent=2))


SAFETY_LABELS = {
    "merged": "merged — cleans instantly",
    "no-unique-commits": "nothing unique — cleans instantly",
    "unpublished-work": "has unpublished commits",
    "dirty": "dirty",
    "detached": "detached HEAD",
}


def worktree_safety(cfg: dict[str, Any], item: dict[str, Any]) -> str:
    """Advisory annotation from local refs; cleanup itself re-verifies after a fetch."""
    branch = item.get("branch")
    if not branch:
        return "detached"
    path = Path(item.get("path", ""))
    if path.is_dir() and git(path, "status", "--porcelain", check=False).stdout.strip():
        return "dirty"
    repo = cfg.get("repositories", {}).get(item.get("repository"), {})
    repo_path = repo.get("path")
    if not repo_path:
        return "unknown"
    canonical = Path(repo_path)
    remote = repo.get("remote", "origin")
    base_branch = repo.get("base_branch", f"{remote}/main")
    branch_ref = f"refs/heads/{branch}"
    if "/" in base_branch and git(canonical, "merge-base", "--is-ancestor", branch_ref,
                                  f"refs/remotes/{base_branch}", check=False).returncode == 0:
        return "merged"
    probe = git(canonical, "rev-list", "-n", "1", branch_ref, "--not",
                f"--exclude={branch_ref}", f"--exclude=refs/remotes/{remote}/{branch}",
                "--all", check=False)
    if probe.returncode == 0 and not probe.stdout.strip():
        return "no-unique-commits"
    return "unpublished-work"


SAFETY_COLORS = {"merged": "32", "no-unique-commits": "32",
                 "unpublished-work": "33", "dirty": "31", "detached": "33"}


def colorize(text: str, code: str) -> str:
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return text
    return f"\x1b[{code}m{text}\x1b[0m"


def shorten_home(path: str) -> str:
    home = str(Path.home())
    if path.startswith(home):
        return "~" + path[len(home):]
    return path


def palette_lines(cfg: dict[str, Any], items: list[dict[str, Any]]) -> list[str]:
    lines = [colorize("Corral — worktrees", "1")]
    if not items:
        lines.append("  (none)")
    current_repo = None
    for index, item in enumerate(items, 1):
        repo = item.get("repository", "?")
        if repo != current_repo:
            lines.append("")
            lines.append(colorize(repo, "1"))
            current_repo = repo
        safety = worktree_safety(cfg, item)
        label = colorize(f"[{SAFETY_LABELS.get(safety, safety)}]", SAFETY_COLORS.get(safety, "0"))
        branch = item.get("branch") or "(detached)"
        lines.append(f"  {index:>2}. {branch}  {label}")
        lines.append(colorize(f"      {shorten_home(str(item.get('path', '?')))}", "2"))
    return lines


def palette_new_worktree(args, cfg, state, herdr) -> None:
    repos = [name for name, repo in cfg["repositories"].items() if repo.get("mode") == "worktree"]
    if not repos:
        print("no repositories configured for worktrees")
        return
    if len(repos) == 1:
        name = repos[0]
    else:
        for index, repo_name in enumerate(repos, 1):
            print(f"  {index}. {repo_name}")
        try:
            name = repos[int(input("Repository number: ").strip()) - 1]
        except (ValueError, IndexError):
            print("cancelled")
            return
    branch = input("Branch name (empty cancels): ").strip()
    if not branch:
        print("cancelled")
        return
    new_args = type("Args", (), {"branch": branch, "base": None,
                                 "repo": cfg["repositories"][name]["path"],
                                 "background": True, "config": args.config})()
    try:
        cmd_new(new_args, cfg, state, herdr)
    except WorkflowError as exc:
        print(f"failed: {exc}")


def cmd_palette(args, cfg, state, herdr):
    """Interactive popup: annotated worktree list + one-key operations."""
    if not sys.stdin.isatty():
        raise WorkflowError("palette is interactive; open it via the Corral popup or a terminal")
    while True:
        items = [i for i in configured_worktree_items(cfg, herdr) if i.get("is_linked_worktree", False)]
        print()
        print("\n".join(palette_lines(cfg, items)))
        choice = input("\n[number] clean up   [n]ew worktree   [s]weep   [q]uit > ").strip().lower()
        if choice in ("", "q", "quit"):
            return
        if choice == "s":
            cmd_sweep(args, cfg, state, herdr)
        elif choice == "n":
            palette_new_worktree(args, cfg, state, herdr)
        else:
            try:
                item = items[int(choice) - 1]
            except (ValueError, IndexError):
                print(f"unrecognized choice: {choice!r}")
                continue
            try:
                print(json.dumps(cleanup_worktree_item(cfg, state, herdr, item,
                                                       abandon=False, confirm=None), indent=2))
            except WorkflowError as exc:
                print(f"refused: {exc}")
        input("Enter to continue ")


def cmd_palette_open(args) -> None:
    """Action entry point: open the palette popup via the Herdr CLI."""
    binary = os.environ.get("HERDR_BIN_PATH", "herdr")
    result = subprocess.run([binary, "plugin", "pane", "open",
                             "--plugin", PLUGIN_ID, "--entrypoint", "palette"])
    raise SystemExit(result.returncode)


def cmd_sweep(args, cfg, state, herdr):
    """Clean every linked worktree that qualifies without questions; report the rest."""
    cleaned, skipped = [], []
    for item in configured_worktree_items(cfg, herdr):
        if not item.get("is_linked_worktree", False):
            continue
        try:
            cleaned.append(cleanup_worktree_item(cfg, state, herdr, item, abandon=False, confirm=None))
        except WorkflowError as exc:
            skipped.append({"worktree": item.get("path"), "branch": item.get("branch"),
                            "repository": item.get("repository"), "reason": str(exc)})
    print(json.dumps({"cleaned": cleaned, "skipped": skipped}, indent=2))
    if skipped:
        log(f"{len(skipped)} worktree(s) kept; each reason above includes the command to discard it deliberately")


def cmd_cleanup_workspace(args, cfg, state, herdr):
    """Herdr action entry point: clean up the worktree of the invoking workspace."""
    workspace = os.environ.get("HERDR_WORKSPACE_ID", "")
    if not workspace:
        raise WorkflowError("cleanup-workspace requires HERDR_WORKSPACE_ID (invoke it via the Herdr action)")
    matches = [item for item in configured_worktree_items(cfg, herdr)
               if item.get("is_linked_worktree", False)
               and workspace in (item.get("open_workspace_id"), item.get("workspace_id"))]
    if len(matches) != 1:
        raise WorkflowError("current workspace is not a configured linked worktree")
    try:
        result = cleanup_worktree_item(cfg, state, herdr, matches[0], abandon=False, confirm=None)
    except WorkflowError as exc:
        # The action runs headless; surface the refusal where the user can see it.
        with contextlib.suppress(Exception):
            pane = next((t.get("pane_id") for t in herdr.tabs(workspace)
                         if t.get("label") == "shell"), None)
            if pane:
                herdr.show_reminder(pane, f"Corral cleanup refused: {exc}")
        raise
    print(json.dumps(result, indent=2))


def cleanup_worktree_item(cfg, state, herdr, item, *, abandon: bool, confirm: str | None):
    branch = item.get("branch", "")
    if not branch or item.get("is_detached", False):
        raise WorkflowError("cleanup requires an attached branch")
    # A merged, clean branch is deleted without questions; abandoning unmerged
    # or dirty work is allowed but demands an exact confirmation.
    if abandon and confirm != branch:
        raise WorkflowError(f"abandoning requires exact confirmation: --abandon --confirm {branch}")
    repository = item.get("repository")
    repo = cfg.get("repositories", {}).get(repository, {})
    canonical = resolve_existing(Path(repo["path"]))
    remote = repo.get("remote", "origin")
    base_branch = repo.get("base_branch", f"{remote}/main")
    if "/" not in base_branch:
        raise WorkflowError("cleanup base_branch must name a remote-tracking branch")
    base_remote, base_name = base_branch.split("/", 1)
    if base_remote != remote:
        raise WorkflowError("cleanup base_branch remote does not match configured remote")
    protected = {base_name, "main", "master", "develop", "development"}
    if branch in protected:
        raise SafetyError(f"refusing to clean up protected branch: {branch}")
    workspace, path = worktree_item_ids(item)
    with worktree_lock(state, path):
        validate_worktree_identity(cfg, path)
        dirty = bool(git(path, "status", "--porcelain").stdout.strip())
        if dirty and not abandon:
            raise WorkflowError(
                f"worktree has uncommitted changes; rerun with --abandon --confirm {branch} to discard them")
        # Fetch and prove the merge before any deletion. Authentication failures
        # therefore leave the remote, checkout, branch, and lease untouched.
        git(canonical, "fetch", remote, "--prune")
        branch_ref = f"refs/heads/{branch}"
        base_ref = f"refs/remotes/{remote}/{base_name}"
        merged = git(canonical, "merge-base", "--is-ancestor", branch_ref, base_ref, check=False).returncode == 0
        if not merged and not abandon:
            # The guard protects commits that exist nowhere else. A branch whose
            # every commit is reachable from some other ref (fresh branch, or
            # fully cherry-picked/rebased elsewhere) loses nothing when deleted.
            # Its own remote copy does not count: cleanup deletes that too.
            probe = git(canonical, "rev-list", "-n", "1", branch_ref, "--not",
                        f"--exclude={branch_ref}", f"--exclude=refs/remotes/{remote}/{branch}",
                        "--all", check=False)
            if probe.returncode or probe.stdout.strip():
                raise WorkflowError(
                    f"branch {branch} is not merged into {base_branch} and has commits not "
                    f"available on any other branch; rerun with --abandon --confirm {branch} to delete it anyway")
        remote_ref = f"refs/remotes/{remote}/{branch}"
        remote_exists = git(canonical, "show-ref", "--verify", "--quiet", remote_ref, check=False).returncode == 0
        if remote_exists:
            git(canonical, "push", remote, "--delete", branch)
        if workspace:
            remove = ["worktree", "remove", "--workspace", workspace]
            if dirty:
                remove.append("--force")
            herdr.call(*remove)
        else:
            # Not open in Herdr (its remove API is workspace-only); use git directly.
            remove = ["worktree", "remove"]
            if dirty:
                remove.append("--force")
            git(canonical, *remove, str(path))
        git(canonical, "update-ref", "-d", branch_ref)
        release_port(state, str(path))
    return {
        "removed": str(path),
        "local_branch_deleted": branch,
        "remote_branch_deleted": branch if remote_exists else None,
        "remote": remote,
        "merged": merged,
        "merged_into": base_branch if merged else None,
        "abandoned": bool(abandon),
        "reason": "merged" if merged else ("abandoned" if abandon else "no-unique-commits"),
    }


def cmd_doctor(args,cfg,state):
    checks=[]
    for key in ("canonical_root","worktree_root"):
        p=Path(cfg[key]); checks.append({"check":key,"ok":p.is_dir(),"value":str(p)})
    checks.append({"check":"HERDR_ENV","ok":os.environ.get("HERDR_ENV")=="1"})
    for tool in ("git", "herdr", cfg.get("agent_kind", "hermes")):
        checks.append({"check":f"command:{tool}","ok":shutil.which(tool) is not None})
    configured_tools = sorted({str(cmd[0]) for repo in cfg["repositories"].values() for cmd in repo.get("commands", {}).values() if cmd})
    for tool in configured_tools:
        checks.append({"check":f"command:{tool}","ok":shutil.which(tool) is not None})
    for name,repo in cfg["repositories"].items():
        p=Path(repo["path"]); checks.append({"check":f"repo:{name}","ok":p.is_dir() and (p/".git").exists(),"value":str(p)})
    latest = latest_remote_version()
    update = {"installed": __version__, "latest": latest,
              "update_available": bool(latest and version_tuple(latest) > version_tuple(__version__))}
    print(json.dumps({"ok":all(x["ok"] for x in checks),"update":update,"checks":checks},indent=2))
    if update["update_available"]:
        log(f"Update available ({__version__} -> {latest}); run: hwt update")
    if not all(x["ok"] for x in checks): raise SystemExit(1)


def cmd_event(args,cfg,state,herdr):
    event=os.environ.get("HERDR_PLUGIN_EVENT",""); raw=os.environ.get("HERDR_PLUGIN_EVENT_JSON","{}")
    try: payload=json.loads(raw)
    except json.JSONDecodeError: raise WorkflowError("invalid HERDR_PLUGIN_EVENT_JSON")
    def values(value: Any, accepted: set[str], key: str = ""):
        if isinstance(value, dict):
            for k, v in value.items(): yield from values(v, accepted, k)
        elif isinstance(value, list):
            for v in value: yield from values(v, accepted, key)
        elif key in accepted and isinstance(value, str):
            yield value
    if event in ("worktree.created","worktree.opened"):
        paths=[Path(x) for x in values(payload, {"path", "cwd", "worktree_path"})]
        def recognized(p: Path) -> bool:
            if not p.exists():
                return False
            if is_in_approved_worktree_root(cfg, p):
                return True
            try:
                repo_for_worktree(cfg, p)
                return True
            except WorkflowError:
                return False
        worktree=next((p for p in paths if recognized(p)),None)
        workspace=os.environ.get("HERDR_WORKSPACE_ID") or next(iter(values(payload, {"workspace_id"})),None)
        if worktree and workspace: bootstrap(cfg,state,worktree,workspace,herdr)
    elif event=="worktree.removed":
        paths=values(payload, {"path", "cwd", "worktree_path"})
        for p in paths:
            if is_in_approved_worktree_root(cfg, Path(p), strict=False): release_port(state,p)


def parser() -> argparse.ArgumentParser:
    p=argparse.ArgumentParser(prog="hwt",description="Corral: safe Git-worktree workflow for Herdr")
    p.add_argument("--config",type=Path,default=DEFAULT_CONFIG,help=argparse.SUPPRESS)
    sub=p.add_subparsers(dest="command",required=True)
    n=sub.add_parser("new",help="create a worktree for a new branch"); n.add_argument("branch"); n.add_argument("--base"); n.add_argument("--repo"); n.add_argument("--background",action="store_true")
    o=sub.add_parser("open",help="open a repository or existing worktree"); o.add_argument("target")
    i=sub.add_parser("init",help="interactively configure a repository"); i.add_argument("repository",nargs="?")
    sub.add_parser("list"); sub.add_parser("status")
    d=sub.add_parser("dev",help="start the configured dev server on a leased port"); d.add_argument("--port",type=int)
    r=sub.add_parser("remove",help="remove a worktree, keeping its branch"); r.add_argument("target"); r.add_argument("--force",action="store_true"); r.add_argument("--confirm")
    c=sub.add_parser("cleanup",help="delete worktree and branch (merged: no questions)"); c.add_argument("target",nargs="?",help="branch, path, or worktree name; omit to pick from a list"); c.add_argument("--abandon",action="store_true",help="delete even if unmerged or dirty (requires --confirm BRANCH)"); c.add_argument("--confirm")
    sub.add_parser("sweep",help="clean up every worktree that qualifies without questions")
    sub.add_parser("palette",help="interactive worktree palette (runs inside the Corral popup)")
    sub.add_parser("doctor"); sub.add_parser("update",help="update Corral in place (git pull)")
    e=sub.add_parser("event",help=argparse.SUPPRESS)
    sub.add_parser("cleanup-workspace",help=argparse.SUPPRESS)
    sub.add_parser("palette-open",help=argparse.SUPPRESS)
    return p


def main(argv: list[str] | None=None) -> int:
    args=parser().parse_args(argv)
    try:
        if args.command == "update":
            cmd_update(args)
            return 0
        if args.command == "palette-open":
            cmd_palette_open(args)
            return 0
        cfg=load_config(args.config); state=runtime_state_dir(); herdr=Herdr()
        commands={"new":cmd_new,"open":cmd_open,"init":cmd_init,"list":cmd_list,"status":cmd_status,"dev":cmd_dev,"remove":cmd_remove,"cleanup":cmd_cleanup,"cleanup-workspace":cmd_cleanup_workspace,"sweep":cmd_sweep,"palette":cmd_palette,"doctor":cmd_doctor,"event":cmd_event}
        fn=commands[args.command]
        if args.command in ("new","open","list","remove","cleanup","cleanup-workspace","sweep","palette","event"): fn(args,cfg,state,herdr)
        else: fn(args,cfg,state)
        return 0
    except WorkflowError as exc:
        print(f"hwt: {exc}",file=sys.stderr); return 2


if __name__=="__main__": raise SystemExit(main())

