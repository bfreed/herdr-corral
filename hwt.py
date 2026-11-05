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
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
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


WINDOWS = os.name == "nt"  # patched in tests; never patch os.name (pathlib reads it)

__version__ = "1.4.1"

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


def contained_destination(root: Path, relative: str, *, link_to: Path | None = None) -> Path:
    """Where relative lands in root, refusing any link out of root on the way.
    link_to: the source of a symlink Corral makes there, so that link, already
    made, is accepted (it points out of root by design)."""
    rel = safe_relative(relative)
    root_real = resolve_existing(root)
    cursor = root_real
    for part in rel.parts[:-1]:
        cursor = cursor / part
        if cursor.exists() or is_link(cursor):
            resolved = cursor.resolve(strict=True)
            if not is_within(resolved, root_real):
                raise SafetyError(f"symlink escape in destination: {relative}")
        else:
            cursor.mkdir(mode=0o700)
    destination = cursor / rel.parts[-1]
    if is_link(destination):
        resolved = destination.resolve(strict=False)
        if not is_within(resolved, root_real) and not (
                link_to is not None and resolved == link_to.resolve()):
            raise SafetyError(f"symlink escape in destination: {relative}")
    return destination


def safe_source(root: Path, relative: str) -> Path:
    source = resolve_existing(root) / safe_relative(relative)
    resolved = source.resolve(strict=True)
    if not is_within(resolved, resolve_existing(root)):
        raise SafetyError(f"source escapes canonical checkout: {relative}")
    return source


def is_link(path: Path) -> bool:
    """A symlink, or a Windows directory junction (which Path.is_symlink misses)."""
    if path.is_symlink():
        return True
    if not WINDOWS:
        return False
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return (bool(getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)
            and getattr(st, "st_reparse_tag", None) == stat.IO_REPARSE_TAG_MOUNT_POINT)


def is_junction(path: Path) -> bool:
    """A Windows directory junction. Corral 1.3.0 made these where Windows can't
    make symlinks; Git for Windows before 2.54 deletes through them when it
    removes a worktree (git worktree remove, and Herdr's Delete), emptying
    whatever they point to. Corral no longer makes them, and replaces any it finds."""
    return is_link(path) and not path.is_symlink()


def remove_link(path: Path) -> None:
    """Remove a symlink or junction itself, never what it points to."""
    if WINDOWS:
        with contextlib.suppress(OSError):
            if os.lstat(path).st_file_attributes & stat.FILE_ATTRIBUTE_DIRECTORY:
                os.rmdir(path)  # a directory link: rmdir removes only the link
                return
    path.unlink()


def make_link(destination: Path, source: Path, *, directory: bool) -> str:
    """Link destination to source with a symlink. Windows allows those only with
    Developer Mode (or as administrator); without, destination gets a copy:
    never a junction, which older Git deletes through. Returns "symlink" or "copy"."""
    try:
        destination.symlink_to(source, target_is_directory=directory)
        return "symlink"
    except OSError:
        if not WINDOWS:
            raise
    if directory:
        if not clone_tree(source, destination):
            raise WorkflowError(f"couldn't copy {source} (this Windows can't make symlinks)")
    else:
        shutil.copy2(source, destination)
    return "copy"


def corral_link_paths(repo: dict[str, Any]) -> list[str]:
    """Where Corral may put links in a worktree: the dependency folder and
    each files entry with action "symlink" (relative, POSIX form)."""
    relatives = [str(repo.get("dependencies", {}).get("directory", "node_modules"))]
    relatives += [str(op.get("path", "")) for op in repo.get("files") or [] if op.get("action") == "symlink"]
    found = []
    for relative in relatives:
        with contextlib.suppress(SafetyError):
            found.append(safe_relative(relative).as_posix())
    return found


def detach_links(worktree: Path, repo: dict[str, Any]) -> None:
    """Remove the links Corral made in a worktree before deleting it. git counts
    them as untracked files (a node_modules/ ignore line matches only a real
    folder), and on Windows nothing may delete through one. Also any junction
    an older Corral left at the worktree's top level."""
    paths = [worktree / Path(relative) for relative in corral_link_paths(repo)]
    if WINDOWS:
        with contextlib.suppress(OSError):
            paths += [p for p in worktree.iterdir() if is_junction(p)]
    for path in paths:
        with contextlib.suppress(OSError):
            if is_link(path):
                remove_link(path)


def worktree_dirty(worktree: Path, repo: dict[str, Any]) -> bool:
    """Whether git sees uncommitted changes, apart from Corral's own links."""
    own = {r for r in corral_link_paths(repo) if is_link(worktree / Path(r))}
    for line in git(worktree, "status", "--porcelain").stdout.splitlines():
        if line.startswith("?? ") and line[3:].rstrip("/") in own:
            continue
        if line.strip():
            return True
    return False


def read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise WorkflowError(f"{path} isn't valid TOML ({exc}); fix it, or move it away") from None


def load_config(path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    cfg = read_toml(path)
    # Per-repository overlays written by `hwt init`; each replaces the whole
    # matching repository entry so it must be self-contained.
    repos_d = path.parent / "repos.d"
    if repos_d.is_dir():
        for extra in sorted(repos_d.glob("*.toml")):
            overlay = read_toml(extra)
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


def program(argv: list[str]) -> list[str]:
    """argv with its program found the way a shell finds it. Windows' CreateProcess
    looks only for .exe files on PATH, and npm, pnpm, yarn (and Corral's own hwt)
    are .cmd shims there."""
    if WINDOWS and argv and not os.path.dirname(argv[0]):
        found = shutil.which(argv[0])
        if found and found.lower().endswith((".cmd", ".bat")):
            return [found, *argv[1:]]
    return list(argv)


def run(argv: list[str], *, cwd: Path | None = None, check: bool = True,
        env: dict[str, str] | None = None, input: str | None = None) -> subprocess.CompletedProcess[str]:
    # Windows decodes in the ANSI code page by default; git and hc write UTF-8.
    encoding = {"encoding": "utf-8", "errors": "replace"} if WINDOWS else {}
    result = subprocess.run(program(argv), cwd=cwd, env=env, text=True, input=input, **encoding,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and result.returncode:
        detail = (result.stderr or result.stdout or "").strip()
        suffix = f": {detail.splitlines()[0][:200]}" if detail else ""
        raise WorkflowError(f"command failed ({argv[0]}) with exit status {result.returncode}{suffix}")
    return result


def git(cwd: Path, *args: str, check: bool = True,
        input: str | None = None) -> subprocess.CompletedProcess[str]:
    return run(["git", "-C", str(cwd), *args], check=check, input=input)


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


def lockfile_digest(path: Path) -> str:
    """A lockfile's content, ignoring line endings: with core.autocrlf (Git for
    Windows' default) a checkout's copy has CRLF where another's may have LF."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


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
        destination = contained_destination(worktree, rel, link_to=source if action == "symlink" else None)
        if action == "copy":
            if not source.is_file() and not source.is_dir():
                raise WorkflowError(f"copy source is not a regular file or directory: {rel}")
            if destination.exists() or is_link(destination):
                if source.is_file() and destination.is_file() and not is_link(destination) and hash_file(destination) == hash_file(source):
                    logger(f"copy already current: {rel}")
                    continue
                if overwrite == "skip":
                    logger(f"keeping existing local file: {rel}")
                    continue
                if overwrite != "always":
                    raise WorkflowError(f"refusing to overwrite modified destination: {rel}")
                if destination.is_dir() and not is_link(destination):
                    shutil.rmtree(destination)
                else:
                    remove_link(destination)
            if source.is_dir():
                shutil.copytree(source, destination, copy_function=shutil.copy2)
            else:
                shutil.copy2(source, destination)
                os.chmod(destination, stat.S_IMODE(source.stat().st_mode))
            logger(f"copied local path: {rel}")
        elif action == "symlink":
            if is_junction(destination):
                remove_link(destination)  # an older Corral's; see is_junction
            elif is_link(destination) and destination.resolve() == source.resolve():
                logger(f"symlink already current: {rel}")
                continue
            # Windows without Developer Mode copies instead of linking.
            if WINDOWS and not is_link(destination) and (
                    (source.is_file() and destination.is_file()) or (source.is_dir() and destination.is_dir())):
                # This Windows copied it last time. A copy that differs now may
                # hold the worktree's own edits, so it stays.
                if source.is_dir():
                    logger(f"keeping the copy of {rel}")
                elif hash_file(destination) == hash_file(source):
                    logger(f"copy already current: {rel}")
                else:
                    logger(f"copy differs from {rel} in the main checkout; left as is")
                continue
            if destination.exists() or is_link(destination):
                raise WorkflowError(f"refusing to replace destination with symlink: {rel}")
            how = make_link(destination, source, directory=source.is_dir())
            logger(f"linked local path: {rel}" if how != "copy"
                   else f"copied local path (this Windows can't make symlinks): {rel}")
        elif action == "create":
            kind = op.get("kind", "directory")
            if destination.exists() or is_link(destination):
                valid = (kind == "directory" and destination.is_dir() and not is_link(destination)) or (kind == "file" and destination.is_file() and not is_link(destination))
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
            if not source.is_file() or is_link(source) or source.name in seen:
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


class _TreeHasLinks(Exception):
    pass


def _refuse_links(directory: str, names: list[str]) -> list[str]:
    for name in names:
        if is_link(Path(directory) / name):
            raise _TreeHasLinks(os.path.join(directory, name))
    return []


def clone_tree(source: Path, destination: Path) -> bool:
    """Copy a directory tree, copy-on-write where the platform allows. True on success.

    Windows has no copy-on-write copy, so this is a plain copy there. A tree
    holding symlinks or junctions (pnpm's node_modules) isn't copied on
    Windows: copying would follow them, so the install runs instead."""
    if WINDOWS:
        try:
            shutil.copytree(source, destination, symlinks=True, ignore=_refuse_links)
            return True
        except (OSError, _TreeHasLinks) as exc:
            if isinstance(exc, _TreeHasLinks):
                log(f"not copying {source}: it holds links ({exc})")
            shutil.rmtree(destination, ignore_errors=True)
            return False
    if sys.platform == "darwin":
        candidates = [["cp", "-c", "-R", str(source), str(destination)]]  # APFS clonefile
    elif os.name == "posix":
        candidates = [["cp", "-R", "--reflink=auto", str(source), str(destination)]]
    else:
        candidates = []
    for command in candidates:
        if run(command, check=False).returncode == 0:
            return True
        if destination.exists():
            shutil.rmtree(destination, ignore_errors=True)
    try:
        shutil.copytree(source, destination, symlinks=True)
        return True
    except OSError:
        shutil.rmtree(destination, ignore_errors=True)
        return False


def prepare_dependencies(canonical: Path, worktree: Path, dep: dict[str, Any]) -> str:
    policy = dep.get("policy", "independent")
    directory = safe_relative(dep.get("directory", "node_modules"))
    canonical_dir = canonical / directory
    # Validate the parent without following a possibly stale dependency link.
    destination = contained_destination(worktree, (directory.parent / ".hwt-path-check").as_posix()).parent / directory.name
    if policy in ("auto", "clone"):
        # Best-effort defaults. "clone": copy the canonical checkout's modules
        # (copy-on-write where the filesystem supports it) when lockfiles are
        # identical, else fall through to the lockfile-detected install, same
        # as "auto". Failures warn instead of aborting the bootstrap.
        if destination.is_dir() and not is_link(destination):
            return "present"
        if is_link(destination):
            remove_link(destination)
        if policy == "clone":
            clone_lock = dep.get("lockfile") or next(
                (name for name, _ in LOCKFILE_COMMANDS if (canonical / name).is_file()), None)
            if clone_lock:
                try:
                    source_dir = safe_source(canonical, directory.as_posix())
                except (SafetyError, FileNotFoundError):
                    source_dir = None
                worktree_lockfile = worktree / clone_lock
                if (source_dir is not None and source_dir.is_dir() and worktree_lockfile.is_file()
                        and lockfile_digest(canonical / clone_lock) == lockfile_digest(worktree_lockfile)):
                    if clone_tree(source_dir, destination):
                        log(f"cloned {directory} from the canonical checkout")
                        return "cloned"
                    log(f"warning: cloning {directory} failed; falling back to install")
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
        matching = lockfile_digest(c_lock) == lockfile_digest(w_lock)
    share = policy == "shared" or (policy == "shared-if-lockfile-matches" and matching)
    if share:
        canonical_dir = safe_source(canonical, directory.as_posix())
        if not canonical_dir.is_dir(): raise WorkflowError(f"canonical dependency directory missing: {canonical_dir}")
        if is_junction(destination):
            remove_link(destination)  # an older Corral's; see is_junction
        if is_link(destination) and destination.resolve() == canonical_dir.resolve():
            return "shared"
        if is_link(destination):
            remove_link(destination)
        if WINDOWS and destination.is_dir():
            return "present"  # its own copy, made because this Windows couldn't share
        if destination.exists() or is_link(destination):
            raise WorkflowError(f"dependency destination already exists and is not the expected link: {destination}")
        try:
            destination.symlink_to(canonical_dir.resolve(), target_is_directory=True)
            return "shared"
        except OSError:
            if not WINDOWS:
                raise
        # A junction would share it too, but Herdr's Delete (git worktree
        # remove) empties a junction's target with Git for Windows before 2.54.
        log(f"warning: sharing {directory} needs a symlink, which this Windows can't make "
            "(turn on Developer Mode); the worktree gets its own instead")
        return prepare_dependencies(canonical, worktree, {**dep, "policy": "clone"})
    if is_link(destination):
        remove_link(destination)
    command = dep.get("install_command", [])
    if command:
        run([str(x) for x in command], cwd=worktree)
    return "independent"


def prepare_local_cache_paths(worktree: Path, paths: list[str]) -> None:
    for relative in paths:
        destination = contained_destination(worktree, relative)
        if is_link(destination):
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
            data = json.loads(data_path.read_text(encoding="utf-8")) if data_path.exists() else {}
        except json.JSONDecodeError as exc:
            raise WorkflowError(f"invalid state file: {data_path}") from exc
        yield data
        fd, temp = tempfile.mkstemp(prefix=f".{name}.", dir=state_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
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
        # On Windows SO_REUSEADDR lets a bind share a port another program
        # listens on, so every port would look free; exclusive use can't.
        if WINDOWS:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
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
        # A lease whose worktree is gone (deleted outside Corral, or a cleanup
        # that failed partway) holds its port for nothing.
        for stale in [k for k in leases if k != key and not Path(k).exists()]:
            del leases[stale]
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


def release_port(state_dir: Path, worktree: str, cfg: dict[str, Any] | None = None) -> None:
    """Return a worktree's port to the pool, and with `cfg`, stop serving it over HTTPS."""
    key = str(Path(worktree).resolve())
    with locked_json(state_dir, "ports") as leases:
        port = leases.pop(key, None)
    if cfg is not None and port is not None:
        stop_https(cfg, int(port))


# HTTPS over Tailscale Serve. Browsers allow some APIs (Web Locks, service
# workers, clipboard, crypto.subtle...) only in a secure context: https, or
# http://localhost. A dev server opened at http://<machine>:<port> from another
# computer gets none of them. With [https] provider = "tailscale", each leased
# port P is also served at https://<machine>.<tailnet>.ts.net:<P + offset>,
# with the tailnet's real certificate. Serve listens only on the Tailscale
# address, so the HTTPS port can't share P with a server bound to 0.0.0.0.
HTTPS_PORT_OFFSET = 1000


def https_settings(cfg: dict[str, Any]) -> dict[str, Any] | None:
    """[https] when it is on, else None."""
    https = cfg.get("https") or {}
    provider = str(https.get("provider", "off")).lower()
    if provider == "off":
        return None
    if provider != "tailscale":
        raise WorkflowError(f"unknown [https] provider: {provider!r} (use \"tailscale\" or \"off\")")
    return {"provider": provider, "port_offset": int(https.get("port_offset", HTTPS_PORT_OFFSET))}


def https_port(cfg: dict[str, Any], port: int) -> int | None:
    settings = https_settings(cfg)
    return port + settings["port_offset"] if settings else None


def tailscale_binary() -> str | None:
    found = shutil.which("tailscale")
    if found:
        return found
    app = Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale")  # macOS app without the CLI linked
    return str(app) if app.is_file() else None


def tailscale(*args: str, timeout: float = 20.0) -> subprocess.CompletedProcess[str] | None:
    """The tailscale CLI's result; None when it is missing or doesn't answer in
    time (`serve` waits indefinitely when the tailnet hasn't enabled HTTPS)."""
    binary = tailscale_binary()
    if not binary:
        return None
    try:
        return subprocess.run([binary, *args], text=True, encoding="utf-8", errors="replace",
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def serve_snapshot() -> dict[str, Any] | None:
    """This machine's tailnet name and Serve config, or None without Tailscale."""
    status = tailscale("status", "--self", "--peers=false", "--json", timeout=10)
    serve = tailscale("serve", "status", "--json", timeout=10)
    if not status or status.returncode or not serve or serve.returncode:
        return None
    try:
        info = json.loads(status.stdout)
        dns = (info.get("Self") or {}).get("DNSName", "").rstrip(".")
        config = json.loads(serve.stdout or "{}") or {}
    except (json.JSONDecodeError, AttributeError):
        return None
    if not dns:
        return None
    # No cert domains: the tailnet hasn't enabled HTTPS certificates, and
    # `tailscale serve` would wait for someone to approve that in a browser.
    certs = bool(info.get("CertDomains"))
    return {"dns": dns, "certs": certs, "tcp": config.get("TCP") or {}, "web": config.get("Web") or {}}


def serve_target(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def served_by_corral(snapshot: dict[str, Any], https_port_: int) -> int | None:
    """The dev port an HTTPS port forwards to, when it is exactly Corral's
    mapping (a single "/" proxy to 127.0.0.1); None for anything else."""
    handlers = (snapshot["web"].get(f"{snapshot['dns']}:{https_port_}") or {}).get("Handlers") or {}
    proxy = (handlers.get("/") or {}).get("Proxy", "") if len(handlers) == 1 else ""
    prefix = "http://127.0.0.1:"
    if not (proxy.startswith(prefix) and proxy[len(prefix):].isdigit()):
        return None
    return int(proxy[len(prefix):])


def https_url(snapshot: dict[str, Any], https_port_: int) -> str:
    return f"https://{snapshot['dns']}:{https_port_}"


def ensure_https(cfg: dict[str, Any], port: int) -> str | None:
    """Serve a leased port over HTTPS and return its URL. Never fails a caller:
    without Tailscale, or when the HTTPS port is taken, it warns and returns None."""
    hp = https_port(cfg, port)
    if hp is None:
        return None
    snapshot = serve_snapshot()
    if snapshot is None:
        log("warning: [https] is on, but Tailscale isn't running or its CLI isn't on PATH; "
            "the dev server is HTTP only")
        return None
    if served_by_corral(snapshot, hp) == port:
        return https_url(snapshot, hp)
    if not snapshot["certs"]:
        log("warning: [https] is on, but this tailnet hasn't enabled HTTPS certificates "
            "(admin console: DNS -> HTTPS Certificates); the dev server is HTTP only")
        return None
    if str(hp) in snapshot["tcp"]:
        log(f"warning: Tailscale Serve already uses port {hp} for something else; leaving it alone "
            f"(the dev server on {port} is HTTP only)")
        return None
    result = tailscale("serve", "--bg", f"--https={hp}", serve_target(port))
    if result is None or result.returncode:
        detail = ((result.stderr or result.stdout).strip().splitlines() or [""])[0][:200] if result else \
            "no answer (is HTTPS enabled for the tailnet? https://login.tailscale.com/admin/dns)"
        hint = ""
        if "access denied" in detail.lower() or "permission" in detail.lower():
            hint = "; allow it once with: sudo tailscale set --operator=$USER"
        log(f"warning: couldn't serve port {port} over HTTPS on {hp}: {detail}{hint}")
        return None
    return https_url(snapshot, hp)


def stop_https(cfg: dict[str, Any], port: int) -> None:
    """Remove a port's HTTPS mapping, only if it is still Corral's own."""
    hp = https_port(cfg, port)
    if hp is None:
        return
    snapshot = serve_snapshot()
    if snapshot is not None and served_by_corral(snapshot, hp) == port:
        tailscale("serve", f"--https={hp}", "off")


def https_urls(cfg: dict[str, Any]) -> dict[int, str]:
    """{dev port: HTTPS URL} for every port Corral serves right now."""
    settings = https_settings(cfg)
    snapshot = serve_snapshot() if settings else None
    if snapshot is None:
        return {}
    urls = {}
    for key in snapshot["web"]:
        host, _, hp = key.rpartition(":")
        if host == snapshot["dns"] and hp.isdigit():
            port = served_by_corral(snapshot, int(hp))
            if port is not None and port + settings["port_offset"] == int(hp):
                urls[port] = https_url(snapshot, int(hp))
    return urls


def orphan_https(cfg: dict[str, Any], state_dir: Path) -> list[dict[str, Any]]:
    """Corral HTTPS mappings for dev ports in the lease range no worktree holds."""
    served = https_urls(cfg)
    if not served:
        return []
    ports = cfg.get("ports", {})
    start, end = int(ports.get("start", 4100)), int(ports.get("end", 4199))
    with locked_json(state_dir, "ports") as leases:
        leased = {int(v) for v in leases.values()}
    return [{"port": port, "url": url} for port, url in sorted(served.items())
            if start <= port <= end and port not in leased]


def leased_port(state_dir: Path, worktree: str) -> int | None:
    key = str(Path(worktree).resolve())
    with locked_json(state_dir, "ports") as leases:
        port = leases.get(key)
    return int(port) if port is not None else None


DELETED_SUFFIX = " (deleted)"


def _linux_processes() -> list[dict[str, Any]]:
    listening: dict[str, int] = {}  # socket inode -> TCP port in LISTEN state
    for table in ("tcp", "tcp6"):
        with contextlib.suppress(OSError):
            for line in Path(f"/proc/net/{table}").read_text().splitlines()[1:]:
                fields = line.split()
                if len(fields) > 9 and fields[3] == "0A":
                    listening[fields[9]] = int(fields[1].rsplit(":", 1)[1], 16)
    uid = os.getuid()
    procs = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
            cwd = os.readlink(entry / "cwd")
            ppid = int((entry / "stat").read_text().rsplit(")", 1)[1].split()[1])
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
            ports = set()
            for fd in (entry / "fd").iterdir():
                with contextlib.suppress(OSError):
                    link = os.readlink(fd)
                    if link.startswith("socket:[") and link[8:-1] in listening:
                        ports.add(listening[link[8:-1]])
        except (OSError, ValueError, IndexError):
            continue  # exited mid-scan, or not ours to inspect
        deleted = cwd.endswith(DELETED_SUFFIX)
        procs.append({"pid": int(entry.name), "ppid": ppid, "command": command,
                      "cwd": cwd[:-len(DELETED_SUFFIX)] if deleted else cwd,
                      "cwd_deleted": deleted, "ports": sorted(ports)})
    return procs


def _lsof_processes() -> list[dict[str, Any]]:
    uid = str(os.getuid())
    procs: dict[int, dict[str, Any]] = {}
    for line in run(["ps", "-axo", "pid=,ppid=,uid=,command="], check=False).stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) >= 3 and parts[2] == uid:
            procs[int(parts[0])] = {"pid": int(parts[0]), "ppid": int(parts[1]),
                                    "command": parts[3] if len(parts) > 3 else "",
                                    "cwd": None, "cwd_deleted": False, "ports": []}
    for selection, field in ((["-d", "cwd"], "cwd"), (["-P", "-iTCP", "-sTCP:LISTEN"], "ports")):
        pid = None
        for line in run(["lsof", "-n", "-a", "-u", uid, *selection, "-Fpn"], check=False).stdout.splitlines():
            if line.startswith("p"):
                pid = int(line[1:])
            elif line.startswith("n") and pid in procs:
                if field == "cwd":
                    procs[pid]["cwd"] = line[1:]
                elif line.rsplit(":", 1)[-1].isdigit():
                    procs[pid]["ports"].append(int(line.rsplit(":", 1)[-1]))
    for proc in procs.values():
        if proc["cwd"] and not Path(proc["cwd"]).exists():
            proc["cwd_deleted"] = True
    return [p for p in procs.values() if p["cwd"]]


class _Win32:
    """The few Windows calls process discovery needs (ctypes, standard library)."""

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    SYNCHRONIZE = 0x100000
    TOKEN_QUERY = 0x8
    WAIT_TIMEOUT = 0x102
    ERROR_ACCESS_DENIED = 5

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ctypes, self.wintypes = ctypes, wintypes
        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        k, a = self.k32, self.advapi
        k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.GetCurrentProcess.restype = wintypes.HANDLE
        k.WaitForSingleObject.restype = wintypes.DWORD
        k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        a.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        a.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                          wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        a.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        k.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.c_void_p] * 4

        class Entry(ctypes.Structure):  # PROCESSENTRY32W
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]
        self.Entry = Entry
        k.Process32FirstW.argtypes = k.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]

    def processes(self) -> list[tuple[int, int, str]]:
        """(pid, parent pid, program name) for every process."""
        snapshot = self.k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
        if not snapshot or snapshot == self.ctypes.c_void_p(-1).value:
            raise OSError("CreateToolhelp32Snapshot failed")
        found = []
        try:
            entry = self.Entry()
            entry.dwSize = self.ctypes.sizeof(self.Entry)
            ok = self.k32.Process32FirstW(snapshot, self.ctypes.byref(entry))
            while ok:
                found.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile))
                ok = self.k32.Process32NextW(snapshot, self.ctypes.byref(entry))
        finally:
            self.k32.CloseHandle(snapshot)
        return found

    def _user(self, process) -> Any:
        """The token user (TOKEN_USER buffer) of an open process handle, or None."""
        token = self.wintypes.HANDLE()
        if not self.advapi.OpenProcessToken(process, self.TOKEN_QUERY, self.ctypes.byref(token)):
            return None
        try:
            size = self.wintypes.DWORD()
            self.advapi.GetTokenInformation(token, 1, None, 0, self.ctypes.byref(size))  # TokenUser
            if not size.value:
                return None
            buffer = self.ctypes.create_string_buffer(size.value)
            if not self.advapi.GetTokenInformation(token, 1, buffer, size, self.ctypes.byref(size)):
                return None
            return buffer
        finally:
            self.k32.CloseHandle(token)

    def mine(self, pid: int, me: Any) -> int | None:
        """The process's creation time (FILETIME ticks) when it is this user's, else None."""
        process = self.k32.OpenProcess(self.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not process:
            return None  # not ours to inspect
        try:
            user = self._user(process)
            if user is None:
                return None
            sid = self.ctypes.c_void_p.from_buffer(user).value  # TOKEN_USER.User.Sid
            mine = self.ctypes.c_void_p.from_buffer(me).value
            if not self.advapi.EqualSid(sid, mine):
                return None
            times = [self.ctypes.c_ulonglong() for _ in range(4)]  # created, exited, kernel, user
            if not self.k32.GetProcessTimes(process, *[self.ctypes.byref(t) for t in times]):
                return 0
            return times[0].value
        finally:
            self.k32.CloseHandle(process)

    def my_user(self) -> Any:
        return self._user(self.k32.GetCurrentProcess())

    def alive(self, pid: int) -> bool:
        process = self.k32.OpenProcess(self.SYNCHRONIZE | self.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not process:
            return self.ctypes.get_last_error() == self.ERROR_ACCESS_DENIED
        try:
            return self.k32.WaitForSingleObject(process, 0) == self.WAIT_TIMEOUT
        finally:
            self.k32.CloseHandle(process)


_WIN32: _Win32 | None = None


def win32() -> _Win32:
    global _WIN32
    if _WIN32 is None:
        _WIN32 = _Win32()
    return _WIN32


def windows_listeners() -> dict[int, set[int]]:
    """pid -> the TCP ports it listens on, from `netstat -ano`. A listening row's
    remote port is 0; the state word is localized, so it isn't read."""
    output = subprocess.run(["netstat", "-ano"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, errors="replace").stdout
    ports: dict[int, set[int]] = {}
    for line in output.splitlines():
        fields = line.split()
        if (len(fields) == 5 and fields[0].upper() == "TCP" and fields[2].rsplit(":", 1)[-1] == "0"
                and fields[4].isdigit() and fields[1].rsplit(":", 1)[-1].isdigit()):
            ports.setdefault(int(fields[4]), set()).add(int(fields[1].rsplit(":", 1)[-1]))
    return ports


def _windows_processes() -> list[dict[str, Any]]:
    """Windows: the user's processes and their listening ports. A process's
    working folder can't be read there, so cwd is None: servers are matched by
    their leased port only, and never found as orphans."""
    win = win32()
    me = win.my_user()
    if me is None:
        return []
    listening = windows_listeners()
    found = []
    for pid, ppid, name in win.processes():
        created = win.mine(pid, me) if pid else None
        if created is not None:
            found.append({"pid": pid, "ppid": ppid, "command": name, "cwd": None, "cwd_deleted": False,
                          "ports": sorted(listening.get(pid, ())), "created": created})
    return found


def user_processes() -> list[dict[str, Any]]:
    """The current user's processes: pid, ppid, command, cwd (cwd_deleted when
    its directory is gone; None on Windows) and TCP listening ports. Other users'
    processes are never returned, so never stopped. Empty where they cannot be
    inspected."""
    try:
        if WINDOWS:
            return _windows_processes()
        if Path("/proc/net/tcp").exists():
            return _linux_processes()
        if os.name == "posix" and shutil.which("lsof"):
            return _lsof_processes()
    except OSError:
        pass
    return []


def server_trees(procs: list[dict[str, Any]], is_target: Callable[[dict[str, Any]], bool]) -> list[dict[str, Any]]:
    """Listening processes selected by is_target, plus their descendants (an
    esbuild child holds no socket but dies with its server). Wrappers above a
    server (npm run, sh -c) exit on their own once it does, and are left alone
    so a shell or agent that launched the server is never touched — nor is
    this process or anything it runs under."""
    by_pid = {p["pid"]: p for p in procs}
    roots = [p["pid"] for p in procs if p["ports"] and is_target(p)]
    return [by_pid[pid] for pid in process_tree(procs, roots)]


def process_tree(procs: list[dict[str, Any]], roots: list[int]) -> list[int]:
    """roots and their descendants, never this process or what it runs under."""
    by_pid = {p["pid"]: p for p in procs}
    protected, pid = set(), os.getpid()
    while pid in by_pid and pid not in protected:
        protected.add(pid)
        pid = by_pid[pid]["ppid"]
    children: dict[int, list[int]] = {}
    for proc in procs:
        parent = by_pid.get(proc["ppid"])
        # Windows keeps a dead parent's id and reuses ids: a "parent" started
        # after its child is someone else (only Windows reports "created").
        if parent and proc.get("created") and parent.get("created") and proc["created"] < parent["created"]:
            continue
        children.setdefault(proc["ppid"], []).append(proc["pid"])
    chosen: list[int] = []
    pending = [pid for pid in roots if pid in by_pid]
    while pending:
        pid = pending.pop()
        if pid in chosen or pid in protected or pid == os.getpid():
            continue
        chosen.append(pid)
        pending.extend(children.get(pid, []))
    return chosen


def _alive(pid: int) -> bool:
    if WINDOWS:
        # Never os.kill(pid, 0) here: on Windows that terminates the process.
        return win32().alive(pid)
    with contextlib.suppress(OSError, IndexError):
        # A zombie has exited; only its parent's wait() is pending.
        if Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z":
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def taskkill(pid: int, force: bool) -> bool:
    """Windows: end a process tree (`taskkill /T`); force adds /F. True when it went."""
    argv = ["taskkill", "/PID", str(pid), "/T", *(["/F"] if force else [])]
    with contextlib.suppress(OSError):
        return subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    return False


def stop_processes(procs: list[dict[str, Any]], grace: float = 5.0) -> list[dict[str, Any]]:
    """SIGTERM, wait up to `grace` seconds, then SIGKILL whatever remains. On
    Windows: taskkill /T, then taskkill /T /F; a console program can only be
    ended forcefully, so when no polite request went through there is no wait."""
    if WINDOWS:
        asked = [taskkill(proc["pid"], force=False) for proc in procs]
        if not any(asked):
            grace = 0.0
    else:
        for proc in procs:
            with contextlib.suppress(OSError):
                os.kill(proc["pid"], signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and any(_alive(p["pid"]) for p in procs):
        time.sleep(0.1)
    report = []
    forced = []
    for proc in procs:
        killed = _alive(proc["pid"])
        if killed:
            if WINDOWS:
                taskkill(proc["pid"], force=True)
                forced.append(proc["pid"])
            else:
                with contextlib.suppress(OSError):
                    os.kill(proc["pid"], signal.SIGKILL)
        report.append({"pid": proc["pid"], "command": proc["command"][:200], "cwd": proc["cwd"],
                       "ports": proc["ports"], "signal": "KILL" if killed else "TERM"})
    # Windows: a process's open files stay locked until it has gone, and the
    # worktree is deleted next.
    deadline = time.monotonic() + 5.0
    while forced and time.monotonic() < deadline and any(_alive(pid) for pid in forced):
        time.sleep(0.1)
    return report


def stop_worktree_servers(state_dir: Path, worktree: Path) -> list[dict[str, Any]]:
    """Stop dev servers belonging to a worktree being removed: anything
    listening from inside it, plus whatever holds its leased port — whether
    started by `hwt dev` in the server tab or by an agent in the background."""
    root = Path(os.path.realpath(worktree))
    port = leased_port(state_dir, str(worktree))
    def belongs(proc: dict[str, Any]) -> bool:
        inside = proc["cwd"] is not None and is_within(Path(proc["cwd"]), root)
        return inside or (port is not None and port in proc["ports"])
    return stop_processes(server_trees(user_processes(), belongs))


def checkout_gone(cwd: Path, root: Path) -> bool:
    """No checkout (a directory holding .git) still encloses cwd below root;
    a deleted build folder inside a live checkout is not an orphan."""
    for parent in [cwd, *cwd.parents]:
        if parent == root or not is_within(parent, root):
            return True
        if (parent / ".git").exists():
            return False
    return True


def orphan_servers(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Servers still running from a deleted directory under an approved
    worktree root whose checkout is gone: survivors of a removal that never
    stopped them (a plain `git worktree remove`, a pre-1.1 cleanup)."""
    roots = approved_worktree_roots(cfg)
    def orphaned(proc: dict[str, Any]) -> bool:
        if not proc["cwd_deleted"]:
            return False
        cwd = Path(proc["cwd"])
        return any(is_within(cwd, r) and checkout_gone(cwd, r) for r in roots)
    return server_trees(user_processes(), orphaned)


TEARDOWN_SUPPRESS_TTL = 600.0


def suppress_teardown(state_dir: Path, worktree: str) -> None:
    # Corral's own removal flows settle the branch themselves; the marker keeps
    # the worktree.removed event hook from opening the teardown popup for them.
    # Event delivery is asynchronous, so a state marker with a TTL is used
    # instead of anything scoped to the removing process's lifetime.
    with locked_json(state_dir, "teardown-suppress") as marks:
        marks[str(Path(worktree).resolve())] = time.time()


def teardown_suppressed(state_dir: Path, worktree: str) -> bool:
    now = time.time()
    with locked_json(state_dir, "teardown-suppress") as marks:
        stamp = marks.pop(str(Path(worktree).resolve()), None)
        for key in [k for k, v in marks.items() if now - float(v) > TEARDOWN_SUPPRESS_TTL]:
            del marks[key]
        return stamp is not None and now - float(stamp) <= TEARDOWN_SUPPRESS_TTL


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
        result = run([self.binary, "agent", "start", name, "--kind", kind, "--pane", pane_id,
                      "--timeout", "120000"], check=False)
        if not result.returncode:
            return
        detail = (result.stderr or result.stdout or "").strip()
        if "agent_not_ready" in detail:
            # It started and is waiting for an answer, as Claude does in every new
            # folder ("Do you trust the files in this folder?"). Not a failure.
            log("the agent started and is waiting for an answer in its tab")
            return
        raise WorkflowError(f"herdr agent start failed: {detail.splitlines()[0][:200] if detail else result.returncode}")

    def run_command(self, pane_id: str, command: str) -> None:
        run([self.binary, "pane", "run", pane_id, command])

    def has_agent(self, pane_id: str) -> bool:
        panes = self.call("agent", "list")["result"].get("agents", [])
        return any(item.get("pane_id") == pane_id for item in panes)

    def metadata(self, workspace: str, tokens: dict[str, str]) -> None:
        args = ["workspace", "report-metadata", workspace, "--source", PLUGIN_ID]
        for key, value in tokens.items(): args += ["--token", f"{key}={value}"]
        run([self.binary, *args])

    def show_reminder(self, pane_id: str, message: str, *, fresh: bool = False) -> None:
        # Wait for a shell prompt (covering $, #, %, ❯ and > prompts); a missed
        # or slow reminder must never break the bootstrap, so failures are
        # swallowed and delivery is attempted regardless. fresh: the tab was
        # just made, so nothing on its screen matters yet.
        with contextlib.suppress(WorkflowError):
            run([
                self.binary, "pane", "wait-output", pane_id, "--regex", "[$#%❯>] ?$",
                "--source", "visible", "--lines", "5", "--timeout", "10000", "--raw",
            ])
        if self._write_reminder_to_tty(pane_id, message):
            return
        if WINDOWS:
            # Herdr's Windows panes run PowerShell (pwsh, else powershell). It
            # has no tty to write to, so the command is typed; in a new tab it
            # clears the screen first, so the typed command doesn't stay there.
            quoted = "'" + message.replace("'", "''") + "'"
            command = ("Clear-Host; " if fresh else "Write-Host ''; ") + \
                f"Write-Host {quoted} -ForegroundColor DarkGray; Write-Host ''"
        else:
            command = f"printf '\\n%s\\n\\n' {shlex.quote(message)}"
        with contextlib.suppress(WorkflowError):
            run([self.binary, "pane", "run", pane_id, command])

    def _write_reminder_to_tty(self, pane_id: str, message: str) -> bool:
        # Writing straight to the pane's tty displays the message without a
        # command being typed and echoed at the prompt. Resolving the tty needs
        # /proc, so non-Linux hosts return False and use the printf fallback
        # (Windows has no ttys to write to at all).
        if WINDOWS:
            return False
        try:
            info = self.call("pane", "process-info", "--pane", pane_id)
            shell_pid = int(info["result"]["process_info"]["shell_pid"])
            tty = os.readlink(f"/proc/{shell_pid}/fd/0")
            if not tty.startswith("/dev/"):
                return False
            fd = os.open(tty, os.O_WRONLY | os.O_NOCTTY)
            try:
                os.write(fd, f"\r\n\x1b[2m{message}\x1b[0m\r\n".encode())
            finally:
                os.close(fd)
        except (WorkflowError, OSError, KeyError, TypeError, ValueError):
            return False
        # The shell doesn't know the cursor moved; a bare Enter makes it draw
        # a fresh prompt below the message.
        with contextlib.suppress(WorkflowError):
            run([self.binary, "pane", "send-keys", pane_id, "enter"])
        return True


class FakeHerdrForTests:
    def __init__(self):
        self.workspaces: dict[str, list[dict[str, str]]] = {}
        self.agent_starts = 0
        self.agent_panes: set[str] = set()
        self.reminders: list[tuple[str, str]] = []
        self.commands: list[tuple[str, str]] = []

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
    def show_reminder(self, pane_id: str, message: str, *, fresh: bool = False): self.reminders.append((pane_id, message))
    def run_command(self, pane_id: str, command: str): self.commands.append((pane_id, command))
    def labels(self, workspace: str): return [x["label"] for x in self.workspaces[workspace]]


def is_desktop(herdr: Any) -> bool:
    # `is True`: tests pass mock.Mock(), whose every attribute is truthy.
    return getattr(herdr, "desktop", False) is True


SHELL_NAMES = {"bash", "zsh", "fish", "sh", "dash", "ksh", "tcsh", "csh", "nu", "pwsh", "xonsh"}
WINDOWS_SHELL_NAMES = SHELL_NAMES | {"powershell", "cmd"}


def program_name(program: str) -> str:
    """A program's name as SHELL_NAMES has it (Windows: no .exe, any case)."""
    if WINDOWS:
        return re.split(r"[\\/]", program)[-1].lower().removesuffix(".exe")
    return Path(program).name


def login_shell() -> str:
    if WINDOWS:
        return (shutil.which("pwsh") or shutil.which("powershell")
                or os.environ.get("COMSPEC") or "cmd.exe")
    with contextlib.suppress(ImportError, KeyError):
        import pwd
        return os.environ.get("SHELL") or pwd.getpwuid(os.getuid()).pw_shell
    return os.environ.get("SHELL") or "/bin/sh"


def shell_with_env(env: dict[str, str]) -> list[str]:
    """The login shell, started with env set (hc run has no --env)."""
    shell = login_shell()
    if not WINDOWS:
        return ["env", *[f"{k}={v}" for k, v in env.items()], shell]
    if program_name(shell) in ("pwsh", "powershell"):
        sets = "; ".join("${env:%s} = '%s'" % (k, v.replace("'", "''")) for k, v in env.items())
        return [shell, "-NoLogo", "-NoExit", "-Command", sets]
    return [shell, "/d", "/k", "&& ".join(f"set {k}={v}" for k, v in env.items())]


def hc_binary() -> str:
    found = os.environ.get("HC_BIN_PATH") or shutil.which("hc") or shutil.which("herdr-core")
    if found:
        return found
    local = Path.home() / ".local/bin/hc"
    if WINDOWS:
        # hc's Windows installer puts it in %LOCALAPPDATA%\Programs\herdr-core.
        folder = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local") / "Programs/herdr-core"
        found = next((p for p in (folder / "hc.exe", folder / "herdr-core.exe") if p.exists()), None)
        return str(found) if found else "hc"
    return str(local) if local.exists() else "hc"


class HerdrCore:
    """Herdr Desktop, through this machine's herdr-core (`hc`).

    Desktop shows every core terminal labelled herdr.space=<id> and
    herdr.tab=<id>, and makes a space or tab for an id it doesn't know, so a
    workspace here is just the terminals sharing a space id. Tab names can't
    be set from hc: each terminal carries corral.tab=<name> and the Corral
    panel names the tab from it. Focus belongs to each screen, so focusing is
    a no-op. Speaks the subset of the TUI's `herdr` CLI that Corral calls, so
    only creating and removing a checkout need Desktop branches in the commands."""

    desktop = True

    def __init__(self, binary: str | None = None):
        self.binary = binary or hc_binary()

    def _json(self, *args: str) -> Any:
        result = run([self.binary, *args, "--json"])
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise WorkflowError(f"hc returned invalid JSON for {' '.join(args)}") from exc

    def _terminals(self, *filters: str) -> list[dict[str, Any]]:
        # `hc ls` omits labels; `hc get` has them.
        listing = self._json("ls", *[x for f in filters for x in ("--label", f)])
        found = []
        for item in listing.get("terminals", []):
            with contextlib.suppress(WorkflowError):
                found.append(self._json("get", item["address"]))
        return sorted(found, key=lambda t: t.get("created_ms") or 0)

    def _spaces(self) -> dict[str, list[dict[str, Any]]]:
        spaces: dict[str, list[dict[str, Any]]] = {}
        for term in self._terminals():
            space = (term.get("labels") or {}).get("herdr.space")
            if space:
                spaces.setdefault(space, []).append(term)
        return spaces

    @staticmethod
    def _folders(terms: list[dict[str, Any]]) -> list[str]:
        # Where its terminals started and where their programs are now: a
        # Desktop-made space's shell often starts at home and cd's in.
        folders = []
        for term in terms:
            for folder in (term.get("cwd"), (term.get("foreground") or {}).get("cwd")):
                if isinstance(folder, str) and folder and folder not in folders:
                    folders.append(folder)
        return folders

    def _start(self, cwd: Path, space: str, label: str | None,
               env: dict[str, str] | None = None) -> tuple[str, str]:
        tab = "tb" + os.urandom(16).hex()
        args = [self.binary, "run", "--bg", "--json", "--keep", "--no-parent", "--no-notify",
                "--cwd", str(cwd), "--label", f"herdr.space={space}", "--label", f"herdr.tab={tab}"]
        if label:
            args += ["--label", f"corral.tab={label}"]
        if env:
            # hc run has no --env; the tab's shell inherits these instead.
            args += ["--", *shell_with_env(env)]
        try:
            address = json.loads(run(args).stdout)["address"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise WorkflowError("hc run did not report the new terminal") from exc
        return tab, address

    def new_space(self, cwd: Path) -> str:
        space = "sp" + os.urandom(16).hex()
        self._start(cwd, space, None)
        return space

    def call(self, *args: str) -> dict[str, Any]:
        verb = args[:2]
        if verb == ("workspace", "list"):
            return {"result": {"workspaces": [
                {"workspace_id": space, "cwd": self._folders(terms), "focused": False}
                for space, terms in self._spaces().items()]}}
        if verb == ("workspace", "get"):
            terms = self._spaces().get(args[2], [])
            return {"result": {"workspace_id": args[2], "cwd": self._folders(terms)}}
        if verb == ("workspace", "create"):
            cwd = Path(args[args.index("--cwd") + 1])
            return {"result": {"workspace": {"workspace_id": self.new_space(cwd)}}}
        if verb == ("workspace", "focus"):
            return {"result": {}}
        if verb == ("worktree", "open"):
            path = Path(args[args.index("--path") + 1])
            return {"result": {"workspace": {"workspace_id": self.new_space(path)}}}
        if verb == ("worktree", "list"):
            return {"result": {"worktrees": self.worktrees(Path(args[args.index("--cwd") + 1]))}}
        raise WorkflowError(f"not available in Herdr Desktop: herdr {' '.join(args[:2])}")

    def worktrees(self, repo: Path) -> list[dict[str, Any]]:
        items = [{"path": c["path"], "branch": c["branch"], "is_detached": c["branch"] is None,
                  "is_linked_worktree": not c["main"]} for c in checkout_list(repo)]
        spaces = self._spaces()
        for item in items:
            # The space showing a checkout: one of its folders is in it, and not
            # in a deeper checkout (worktree roots may nest under a repo).
            here = Path(item["path"]).resolve()
            deeper = [Path(i["path"]).resolve() for i in items
                      if i is not item and is_within(Path(i["path"]).resolve(), here)]
            for space, terms in spaces.items():
                folders = [Path(f).resolve() for f in self._folders(terms)]
                if any(is_within(f, here) and not any(is_within(f, d) for d in deeper) for f in folders):
                    item["workspace_id"] = space
                    break
        return items

    def create_worktree(self, canonical: Path, branch: str, base: str, path: Path) -> str:
        exists = git(canonical, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}",
                     check=False).returncode == 0
        add = ["worktree", "add", str(path), branch] if exists else \
              ["worktree", "add", "-b", branch, str(path), base]
        git(canonical, *add)
        return self.new_space(path)

    def remove_worktree(self, canonical: Path, workspace: str | None, path: Path, force: bool) -> None:
        # The space's terminals first: their shells have the folder open, and
        # Windows won't delete a folder in use. The space goes with them.
        # What git would refuse, refused before the space is ended.
        if not force and git(path, "status", "--porcelain", "--ignore-submodules=none",
                             check=False).stdout.strip():
            raise WorkflowError(f"{path} has modified or untracked files; removing it needs --force")
        terms = self._terminals(f"herdr.space={workspace}") if workspace else []
        # hwt itself may run in one of them (the space's shell tab): that one last.
        me = os.environ.get("HC_TERMINAL_ID")
        own = [t for t in terms if me and me in (t.get("id"), t.get("address"))]
        others = [t for t in terms if t not in own]
        shells = [t["pid"] for t in others if isinstance(t.get("pid"), int)]
        # Their shells and whatever runs in them (the agent): until those exit,
        # Windows keeps the folder in use.
        held = process_tree(user_processes(), shells) if WINDOWS and shells else []
        for term in others:
            with contextlib.suppress(WorkflowError):
                run([self.binary, "kill", term["address"]])
        wait_for_exit(held)
        remove_checkout(canonical, path, force)
        for term in own:
            with contextlib.suppress(WorkflowError):
                run([self.binary, "kill", term["address"]])

    def tabs(self, workspace: str) -> list[dict[str, Any]]:
        tabs: dict[str, dict[str, Any]] = {}
        for term in self._terminals(f"herdr.space={workspace}"):
            labels = term.get("labels") or {}
            tab = labels.get("herdr.tab")
            if tab and tab not in tabs:
                tabs[tab] = {"tab_id": tab, "label": labels.get("corral.tab", ""),
                             "pane_id": term["address"]}
        return list(tabs.values())

    def rename_tab(self, tab_id: str, label: str) -> None:
        for term in self._terminals(f"herdr.tab={tab_id}"):
            run([self.binary, "label", term["address"], f"corral.tab={label}"])

    def create_tab(self, workspace: str, cwd: Path, label: str, env: dict[str, str]) -> tuple[str, str]:
        return self._start(cwd, workspace, label, env)

    def start_agent(self, pane_id: str, name: str, kind: str = "hermes") -> None:
        # Types the agent's command into the tab's shell, as a person would.
        run([self.binary, "send", pane_id, kind])

    def run_command(self, pane_id: str, command: str) -> None:
        run([self.binary, "send", pane_id, command])

    def has_agent(self, pane_id: str) -> bool:
        term = self._json("get", pane_id)
        if term.get("status"):
            return True
        program = (term.get("foreground") or {}).get("name") or ""
        shells = WINDOWS_SHELL_NAMES if WINDOWS else SHELL_NAMES
        return bool(program) and program_name(program) not in shells

    def metadata(self, workspace: str, tokens: dict[str, str]) -> None:
        pass  # Desktop has no workspace metadata; the lease in Corral's state is the record.

    def show_reminder(self, pane_id: str, message: str, *, fresh: bool = False) -> None:
        # Straight to the shell's tty (Linux /proc), so nothing is typed at the
        # prompt; a missed reminder must never break the bootstrap. Windows has
        # no ttys, so no reminder there: the Corral panel shows the same.
        if WINDOWS:
            return
        with contextlib.suppress(WorkflowError, OSError, KeyError, TypeError, ValueError):
            tty = os.readlink(f"/proc/{int(self._json('get', pane_id)['pid'])}/fd/0")
            if not tty.startswith("/dev/"):
                return
            fd = os.open(tty, os.O_WRONLY | os.O_NOCTTY)
            try:
                os.write(fd, f"\r\n\x1b[2m{message}\x1b[0m\r\n".encode())
            finally:
                os.close(fd)
            run([self.binary, "keys", pane_id, "enter"])


def tab_pane(tab: dict[str, Any]) -> str:
    if "pane_id" in tab: return tab["pane_id"]
    # Live tab list omits root pane; caller resolves panes separately when needed.
    raise WorkflowError("pane id unavailable for tab")


def herdr_config_file() -> Path:
    """Where the Herdr TUI reads config.toml (its src/config/io.rs): HERDR_CONFIG_PATH,
    else XDG_CONFIG_HOME/herdr, else ~/.config/herdr; on Windows %APPDATA%\\herdr."""
    if os.environ.get("HERDR_CONFIG_PATH"):
        return Path(os.environ["HERDR_CONFIG_PATH"])
    base = os.environ.get("XDG_CONFIG_HOME")
    if not base and WINDOWS:
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData/Roaming")
    return Path(base or Path.home() / ".config") / "herdr" / "config.toml"


def corral_palette_keybinding() -> str | None:
    """The user's palette binding from Herdr's own config, or None when unbound.

    Reads the same file Herdr does (see herdr_config_file) and renders
    'prefix+X' as the real keystrokes by expanding the configured prefix
    (Herdr's default is ctrl+b, per validated_keybinds in its source)."""
    try:
        keys = tomllib.loads(herdr_config_file().read_text(encoding="utf-8")).get("keys", {})
    except (OSError, tomllib.TOMLDecodeError):
        return None
    for entry in keys.get("command", []):
        if entry.get("type") == "plugin_action" and entry.get("command") == f"{PLUGIN_ID}.palette":
            key = str(entry.get("key", ""))
            if key.startswith("prefix+"):
                return f"{keys.get('prefix', 'ctrl+b')} {key.removeprefix('prefix+')}"
            return key or None
    return None


def ensure_layout(herdr: Any, workspace: str, worktree: Path, port: int, host: str,
                  remote_host: str, start_agent: bool, repo: dict[str, Any] | None = None,
                  agent_kind: str = "hermes", https: str | None = None,
                  start_dev: bool = False, before_agent: Callable[[], Any] | None = None) -> str | None:
    """The worktree's agent, shell and server tabs, then before_agent() (its
    dependencies: the tabs show at once, while the agent waits for a worktree
    that's ready), then metadata and the agent. Returns the server tab's pane
    when this call created it (the place to start the dev server)."""
    env = {
        "HWT_WORKTREE": str(worktree), "HWT_DEV_PORT": str(port), "PORT": str(port),
        "HWT_DEV_HOST": host, "HOST": host,
        "HWT_LOCAL_URL": f"http://127.0.0.1:{port}",
        "HWT_REMOTE_URL": https or (f"http://{remote_host}:{port}" if remote_host else ""),
        "HWT_HTTPS_URL": https or "",
    }
    new_server = None
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
                new_server = pid
                if repo.get("commands", {}).get("dev"):
                    verb = "starting the dev server" if start_dev else "'hwt dev' starts the dev server"
                    message = f"Corral: {verb} on port {port} — {env['HWT_LOCAL_URL']}"
                    if env.get("HWT_REMOTE_URL"):
                        message += f" / {env['HWT_REMOTE_URL']}"
                else:
                    message = "Corral: no dev command configured for this repository — set one with 'hwt init'"
                herdr.show_reminder(pid, message, fresh=True)
            if label == "shell":
                if is_desktop(herdr):
                    palette = "the Corral panel has buttons for them"
                else:
                    binding = corral_palette_keybinding()
                    palette = (f"palette: {binding}" if binding
                               else "palette via 'herdr plugin action invoke corral.palette'")
                message = f"Corral: 'hwt -h' lists commands; {palette}"
                if repo.get("_suggest_init"):
                    message += " — run 'hwt init' to configure this repository"
                herdr.show_reminder(pid, message, fresh=True)
    if before_agent:
        before_agent()
    herdr.metadata(workspace, {"dev_port":str(port),"dev_url":env["HWT_REMOTE_URL"],"worktree":str(worktree)})
    if start_agent:
        agent = by_label["agent"]
        pane_id = agent.get("pane_id")
        if pane_id and not herdr.has_agent(pane_id):
            herdr.start_agent(pane_id, f"hwt-{hashlib.sha1(str(worktree).encode()).hexdigest()[:8]}", agent_kind)
    return new_server


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


IN_USE = ("permission denied", "being used by another process", "device or resource busy",
          "directory not empty", "access is denied", "worktree_remove_failed")


class FolderInUse(WorkflowError):
    pass


def folder_in_use(path: Path, error: Exception, retry: str) -> FolderInUse:
    """A failed removal, said plainly: on Windows, nearly always a program
    (a terminal cd'd into it, an editor, a server) has the folder open."""
    detail = str(error).removeprefix("command failed (git) with exit status 255: ")
    return FolderInUse(
        f"couldn't delete {path}: a program probably has the folder open (a terminal in it, an "
        f"editor, a dev server). Close it, then run '{retry}' again. ({detail})")


def remove_checkout(canonical: Path, path: Path, force: bool) -> None:
    git(canonical, "worktree", "remove", *(["--force"] if force else []), str(path))


@contextlib.contextmanager
def explained_removal(path: Path, retry: str):
    """Windows: a removal that failed because the folder is in use, said plainly.
    git unregisters a worktree even when it can't delete the folder, so the
    retry is how Corral finishes the cleanup."""
    try:
        yield
    except WorkflowError as exc:
        if WINDOWS and not isinstance(exc, FolderInUse) and any(w in str(exc).lower() for w in IN_USE):
            raise folder_in_use(path, exc, retry) from exc
        raise


def wait_for_exit(pids: list[int], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while any(_alive(pid) for pid in pids) and time.monotonic() < deadline:
        time.sleep(0.1)


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


def protected_branches(repo: dict[str, Any]) -> set[str]:
    remote = repo.get("remote", "origin")
    base = repo.get("base_branch", f"{remote}/main")
    base_name = base.split("/", 1)[1] if "/" in base else base
    return {base_name, "main", "master", "develop", "development"}


def repo_for_removed_worktree(cfg: dict[str, Any], path: Path) -> tuple[str, dict[str, Any], Path] | None:
    """Best-effort repository lookup for a checkout that no longer exists.

    Git cannot answer for a deleted path, so only the path conventions from
    repo_for_worktree's fallback apply; unrecognized paths return None."""
    target = path.expanduser().resolve()
    for name, repo in cfg.get("repositories", {}).items():
        if "path" in repo and is_within(target, sibling_worktree_dir(Path(repo["path"])).resolve()):
            return name, repo, Path(repo["path"])
    for root in approved_worktree_roots(cfg):
        for name, repo in cfg.get("repositories", {}).items():
            if "path" in repo and is_within(target, (root / name).resolve()):
                return name, repo, Path(repo["path"])
    return None


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
        https = ensure_https(cfg, port)
        start_dev = bool(repo.get("commands", {}).get("dev")) and bool(repo.get("start_dev", cfg.get("start_dev", False)))
        # Tabs first, so the workspace appears at once; then the possibly slow
        # dependency install; the agent last, in a worktree that's ready.
        deps: list[str] = []
        server_pane = ensure_layout(
            h, workspace, worktree, port, cfg.get("dev_host", "127.0.0.1"), cfg.get("remote_host", ""),
            bool(repo.get("start_agent", False)), repo_view, agent_kind=cfg.get("agent_kind", "hermes"),
            https=https, start_dev=start_dev,
            before_agent=lambda: deps.append(
                prepare_dependencies(canonical, worktree, repo.get("dependencies", {"policy": "clone"}))))
        dep_status = deps[0]
    except Exception:
        release_port(state, str(worktree), cfg)
        raise
    # The dev server starts once dependencies are in place, in a server tab
    # this bootstrap opened (reopening a worktree never starts a second one).
    dev_started = False
    if start_dev and server_pane and dep_status != "install-failed" and port_is_free(port):
        try:
            h.run_command(server_pane, "hwt dev")
            dev_started = True
        except WorkflowError as exc:
            log(f"warning: couldn't start the dev server in the server tab: {exc}")
    return {"repository":name,"worktree":str(worktree),"workspace_id":workspace,"port":port,"dependencies":dep_status,
            "https_url":https,"dev_started":dev_started}


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
    """Template for `hwt dev`. Frameworks that ignore the PORT/HOST env vars
    (vite and friends) get explicit {port}/{host} flags; the rest run bare,
    because `hwt dev` always exports PORT and HOST."""
    package = canonical / "package.json"
    if not package.is_file():
        return None
    try:
        scripts = json.loads(package.read_text(encoding="utf-8")).get("scripts", {})
    except (json.JSONDecodeError, OSError):
        return None
    script = next((s for s in ("dev", "serve", "start") if s in scripts), None)
    if not script:
        return None
    base = [package_manager_base(canonical), "run", script]
    text = str(scripts.get(script, ""))
    if any(tool in text for tool in ("vite", "astro", "webpack")):
        return base + ["--", "--host", "{host}", "--port", "{port}"]
    if text.startswith("next") or "next dev" in text:
        return base + ["--", "-H", "{host}", "-p", "{port}"]
    return base


PACKAGE_RUNNERS = {"npm", "npx", "pnpm", "pnpx", "yarn", "bun", "bunx"}


def dev_command_problem(canonical: Path, dev: list[str]) -> str | None:
    """Why `hwt dev` could not run this command in the repository, or None.
    Placeholder-bearing executables are left alone: they only resolve per worktree."""
    executable = dev[0]
    if "{" in executable:
        return None
    if Path(executable).name in PACKAGE_RUNNERS and not (canonical / "package.json").is_file():
        return f"{canonical} has no package.json, so '{executable}' has nothing to run"
    if "/" in executable or os.sep in executable:
        target = Path(executable) if Path(executable).is_absolute() else canonical / executable
        if not target.is_file():
            return f"'{executable}' does not exist in {canonical}"
        return None
    if shutil.which(executable) is None:
        return f"'{executable}' is not on PATH"
    return None


def ask_dev_command(canonical: Path, suggestion: list[str] | None) -> list[str] | None:
    rendered = " ".join(suggestion) if suggestion else "none"
    while True:
        raw = input(f"Dev server command for {canonical.name} [{rendered}]: ").strip()
        if not raw:
            dev = suggestion
        elif raw.lower() == "none":
            dev = None
        else:
            dev = shlex.split(raw)
        problem = dev_command_problem(canonical, dev) if dev else None
        if not problem:
            return dev
        print(f"Warning: {problem}.")
        if ask_yes_no("Save it anyway?", False):
            return dev


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


def ask_base_branch(canonical: Path, remote: str, configured: str) -> str:
    options: list[tuple[str, str]] = [(configured, "current setting")]
    for candidate in remote_base_candidates(canonical, remote)[:10]:
        if all(candidate != ref for ref, _ in options):
            options.append((candidate, "remote branch"))
    lines = [f"  {index:>2}. {ref}  " + colorize(f"({note})", "2")
             for index, (ref, note) in enumerate(options, 1)]
    lines.append(f"  {len(options) + 1:>2}. other (type a ref)")
    index = choose_indexed(lines, len(options) + 1,
                           "base branch for new worktrees (also cleanup's merge target)")
    if index < len(options):
        return options[index][0]
    while True:
        typed = input("Base ref (empty keeps current): ").strip()
        if not typed:
            return configured
        if not typed.startswith(f"{remote}/"):
            print(f"use a remote-tracking ref like {remote}/develop — cleanup verifies merges against it")
            continue
        if ref_exists(canonical, typed):
            return typed
        print(f"no such ref here: {typed!r}")


def run_init_interview(cfg: dict[str, Any], name: str, config_path: Path) -> Path:
    repo = dict(cfg["repositories"][name])
    canonical = resolve_existing(Path(repo["path"]))
    print(f"Configuring repository {name} ({canonical})")
    remote = repo.get("remote", "origin")
    repo["base_branch"] = ask_base_branch(canonical, remote,
                                          repo.get("base_branch", "origin/main"))
    env_ops = default_env_operations(canonical)
    files: list[dict[str, Any]] = []
    if env_ops:
        listing = ", ".join(op["path"] for op in env_ops)
        if ask_yes_no(f"Copy untracked env files into new worktrees ({listing})?", True):
            files = env_ops
    else:
        print("No .env files to copy into new worktrees.")
    dep_policy = "clone"
    install = detect_install_command(canonical)
    if install:
        if (canonical / "node_modules").is_dir():
            description = ("clone node_modules from this checkout when lockfiles match, "
                           f"otherwise run '{' '.join(install)}'")
        else:
            description = f"run '{' '.join(install)}'"
        if not ask_yes_no(f"Set up dependencies automatically in new worktrees ({description})?", True):
            dep_policy = "independent"
    print()
    print("Dev server: the command 'hwt dev' runs to start this project in a worktree.")
    print("Each worktree gets its own port. If the server ignores $PORT, put {port}")
    print("where the command takes a port, for example:")
    print("  npm run dev -- --port {port}     (Vite and most npm projects)")
    print("  npx quasar dev --port {port}")
    print("Type 'none' if this repository has no dev server, or press Enter to accept")
    print("the value in brackets.")
    dev = ask_dev_command(canonical, suggest_dev_command(canonical))
    overlay_dir = config_path.parent / "repos.d"
    overlay_dir.mkdir(parents=True, exist_ok=True)
    overlay = overlay_dir / f"{name}.toml"
    overlay.write_text(render_repo_overlay(name, repo, files, dep_policy, dev), encoding="utf-8")
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
    on_branch = run(["git", "-C", str(root), "symbolic-ref", "-q", "HEAD"], check=False).returncode == 0
    if on_branch:
        result = run(["git", "-C", str(root), "pull", "--ff-only"], check=False)
        if result.returncode:
            raise WorkflowError(f"git pull failed: {result.stderr.strip() or result.stdout.strip()}")
    else:
        # The Corral panel's Install checks out a release tag, which leaves no
        # branch to pull: move to the newest release instead.
        result = run(["git", "-C", str(root), "fetch", "-q", "--tags", "--force", "origin"], check=False)
        if result.returncode:
            raise WorkflowError(f"git fetch failed: {result.stderr.strip() or result.stdout.strip()}")
        tag = newest_release_tag(root)
        if tag and version_tuple(tag) > version_tuple(previous):
            result = run(["git", "-C", str(root), "checkout", "-q", tag], check=False)
            if result.returncode:
                raise WorkflowError(f"git checkout {tag} failed: {result.stderr.strip() or result.stdout.strip()}")
    manifest = tomllib.loads((root / "herdr-plugin.toml").read_text(encoding="utf-8"))
    current = str(manifest.get("version", previous))
    if shutil.which("herdr"):
        plugin = root
        if WINDOWS:
            # Windows links a generated manifest (see install.py's
            # write_windows_plugin); the updated installer rewrites it.
            plugin = Path(run([sys.executable, str(root / "install.py"), "--windows-plugin"]).stdout.strip())
        subprocess.run(["herdr", "plugin", "unlink", PLUGIN_ID],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        relink = subprocess.run(["herdr", "plugin", "link", str(plugin), "--enabled"])
        if relink.returncode:
            raise WorkflowError(f"herdr plugin relink failed; run: herdr plugin link {plugin} --enabled")
    else:
        log("herdr is not on PATH; skipped plugin relink")
    print(json.dumps({"installed_from": str(root), "previous_version": previous,
                      "version": current, "up_to_date": current == previous}, indent=2))


def newest_release_tag(root: Path) -> str | None:
    tags = run(["git", "-C", str(root), "tag", "--list", "v*"], check=False).stdout.split()
    releases = [t for t in tags if re.fullmatch(r"v\d+\.\d+\.\d+", t)]
    return max(releases, key=version_tuple, default=None)


def ref_exists(repo: Path, ref: str) -> bool:
    return git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False).returncode == 0


def remote_base_candidates(canonical: Path, remote: str) -> list[str]:
    """Remote branches, main-line names first, then topic branches by recency.

    Branches checked out in *linked* worktrees are excluded (they're being
    worked on); the canonical checkout's own branch is NOT — it's often exactly
    the base people want (e.g. develop checked out in the main clone)."""
    result = git(canonical, "for-each-ref", "--sort=-committerdate",
                 "--format=%(refname:short)", f"refs/remotes/{remote}", check=False)
    if result.returncode:
        return []
    branches = [line.strip() for line in result.stdout.splitlines()
                if line.strip() and not line.strip().endswith("/HEAD")]
    used: set[str] = set()
    listing = git(canonical, "worktree", "list", "--porcelain", check=False)
    if listing.returncode == 0:
        canonical_real = canonical.resolve()
        current_path: Path | None = None
        for line in listing.stdout.splitlines():
            if line.startswith("worktree "):
                current_path = Path(line.removeprefix("worktree ").strip())
            elif line.startswith("branch refs/heads/") and current_path is not None:
                if current_path.resolve() != canonical_real:
                    used.add(line.removeprefix("branch refs/heads/").strip())
    names = [branch for branch in branches
             if "/" in branch and branch.split("/", 1)[1] not in used]
    plain = [branch for branch in names if "/" not in branch.split("/", 1)[1]]
    slashed = [branch for branch in names if "/" in branch.split("/", 1)[1]]
    return plain + slashed


def interactive_new_setup(args, cfg: dict[str, Any]) -> None:
    """Fill in branch/base/repo by asking, using where we're standing as context."""
    if not sys.stdin.isatty():
        raise WorkflowError("branch name required: hwt new <branch> (or run interactively from a terminal)")
    top = git(Path.cwd(), "rev-parse", "--show-toplevel", check=False)
    repo_name = None
    context = "outside"
    context_branch = None
    if top.returncode == 0 and top.stdout.strip():
        top_path = Path(top.stdout.strip())
        try:
            repo_name, _ = resolve_configured_repo(cfg, top_path)
            context = "canonical"
        except (SafetyError, WorkflowError):
            with contextlib.suppress(WorkflowError):
                repo_name, _, _ = repo_for_worktree(cfg, top_path)
                context = "worktree"
        if repo_name:
            current = git(top_path, "branch", "--show-current", check=False)
            context_branch = current.stdout.strip() or None
    if repo_name is None:
        repos = [(name, repo) for name, repo in cfg["repositories"].items()
                 if repo.get("mode") == "worktree"]
        if not repos:
            raise WorkflowError("no repositories are configured for worktrees")
        lines = [f"  {index:>2}. {name}  " + colorize(shorten_home(str(repo["path"])), "2")
                 for index, (name, repo) in enumerate(repos, 1)]
        repo_name = repos[choose_indexed(lines, len(repos), "repositories")][0]
    repo = cfg["repositories"][repo_name]
    configured_base = repo.get("base_branch", "origin/main")
    options: list[tuple[str, str]] = []

    def offer(ref: str | None, note: str) -> None:
        if ref and all(ref != existing for existing, _ in options):
            options.append((ref, note))

    if context == "canonical":
        offer(context_branch, "current branch here")
        offer(configured_base, "configured base")
    elif context == "worktree":
        offer(configured_base, "configured base — a sibling of this worktree")
        offer(context_branch, "this worktree's branch — stack on top of it")
    else:
        offer(configured_base, "configured base")
    canonical_path = Path(repo["path"])
    if canonical_path.is_dir():
        for candidate in remote_base_candidates(canonical_path, repo.get("remote", "origin"))[:10]:
            offer(candidate, "remote branch")
    lines = [f"  {index:>2}. {ref}  " + colorize(f"({note})", "2")
             for index, (ref, note) in enumerate(options, 1)]
    lines.append(f"  {len(options) + 1:>2}. other (type a ref)")
    base_index = choose_indexed(lines, len(options) + 1,
                                f"base branch for the new worktree in {repo_name}")
    if base_index == len(options):
        while True:
            base = input("Base ref (empty cancels): ").strip()
            if not base:
                raise WorkflowError("cancelled")
            if not canonical_path.is_dir() or ref_exists(canonical_path, base):
                break
            print(f"no such ref here: {base!r} — check the listing above for the exact name")
    else:
        base = options[base_index][0]
    branch = input("New branch name: ").strip()
    if not branch:
        raise WorkflowError("cancelled")
    args.branch = branch
    args.base = base
    args.repo = repo["path"]


def cmd_new(args, cfg, state, herdr: Herdr):
    if not args.branch:
        interactive_new_setup(args, cfg)
    if args.repo:
        repo_path = Path(args.repo)
    else:
        top = git(Path.cwd(), "rev-parse", "--show-toplevel", check=False)
        repo_path = Path(top.stdout.strip()) if top.returncode == 0 and top.stdout.strip() else Path.cwd()
    try:
        name,repo=resolve_configured_repo(cfg,repo_path)
    except SafetyError:
        # Standing in a worktree: create a sibling for its repository.
        try:
            name, repo, _ = repo_for_worktree(cfg, repo_path)
        except WorkflowError:
            raise WorkflowError(
                "not inside a configured repository; run 'hwt new' with no arguments to pick one, or pass --repo") from None
    if repo.get("mode")!="worktree": raise WorkflowError(f"{name} is configured for workspace opening only")
    cfg = maybe_offer_init(args, cfg, name)
    repo = cfg["repositories"][name]
    canonical=resolve_existing(Path(repo["path"])); branch=args.branch; base=args.base or repo.get("base_branch","main")
    run(["git","check-ref-format","--branch",branch])
    if repo.get("fetch",True): fetch_with_offline_fallback(canonical,repo.get("remote","origin"))
    # Validate before anything is created; a typo'd base would otherwise fail
    # somewhere mid-creation, possibly after the workspace exists.
    if not ref_exists(canonical, base):
        raise WorkflowError(f"base ref does not exist (even after fetch): {base!r}")
    desktop = is_desktop(herdr)
    # Desktop nests a worktree's space under its repo's by itself; the TUI
    # anchors the worktree to the repo's workspace.
    canonical_workspace = None if desktop else ensure_canonical_workspace(herdr, canonical, name, False)
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
    # herdr rejects --workspace together with --cwd; the workspace anchors the repo.
    # No --label: a label becomes the workspace's custom_name, which disables
    # Herdr's branch-derived sidebar display (strip_prefix("worktree/")).
    command=["worktree","create","--workspace",canonical_workspace,"--branch",branch,"--base",base,"--path",str(path),"--no-focus" if args.background else "--focus"]
    try:
        if desktop:
            workspace = herdr.create_worktree(canonical, branch, base, path)
        else:
            obj=herdr.call(*command)
            workspace=json_result(obj,"result","workspace","workspace_id")
    except WorkflowError:
        # A failed create must not leave an empty namespace directory behind.
        if created_namespace:
            with contextlib.suppress(OSError):
                path.parent.rmdir()
        raise
    summary=bootstrap(cfg,state,path,workspace,herdr)
    if not args.background: herdr.call("workspace","focus",workspace)
    summary.update(desktop_tabs(herdr, workspace))
    print(json.dumps({
        **summary,
        "branch": branch,
        "dev_url": summary.get("https_url") or (f"http://{cfg.get('remote_host')}:{summary['port']}" if cfg.get("remote_host") else f"http://127.0.0.1:{summary['port']}"),
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


def open_candidates(cfg: dict[str, Any], herdr: Any) -> list[dict[str, Any]]:
    entries = []
    for name, repo in cfg["repositories"].items():
        if "path" in repo:
            entries.append({"kind": "repository", "name": name, "path": repo["path"]})
    for item in configured_worktree_items(cfg, herdr):
        if item.get("is_linked_worktree", False) and item.get("path"):
            entries.append({"kind": "worktree", "name": item.get("branch") or Path(item["path"]).name,
                            "branch": item.get("branch"), "path": item["path"],
                            "repository": item.get("repository")})
    return entries


def open_entry_keys(entry: dict[str, Any]) -> list[str]:
    keys = [entry.get("name"), entry.get("branch"), entry.get("repository")]
    path = entry.get("path")
    if isinstance(path, str) and path:
        keys.append(Path(path).name)
    return [key for key in keys if isinstance(key, str) and key]


def fuzzy_open_matches(entries: list[dict[str, Any]], target: str) -> list[dict[str, Any]]:
    import difflib
    needle = target.lower()
    hits = [entry for entry in entries
            if any(needle in key.lower() or key.lower() in needle for key in open_entry_keys(entry))]
    if hits:
        return hits
    universe: dict[str, dict[str, Any]] = {}
    for entry in entries:
        for key in open_entry_keys(entry):
            universe.setdefault(key, entry)
    ordered = []
    for key in difflib.get_close_matches(target, list(universe), n=5, cutoff=0.5):
        if universe[key] not in ordered:
            ordered.append(universe[key])
    return ordered


def open_listing_lines(entries: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    section = None
    repo_header = None
    for index, entry in enumerate(entries, 1):
        if entry["kind"] != section:
            section = entry["kind"]
            if lines:
                lines.append("")
            lines.append(colorize("repositories" if section == "repository" else "worktrees", "1"))
            repo_header = None
        if section == "repository":
            lines.append(f"  {index:>2}. {entry['name']}  " + colorize(shorten_home(str(entry["path"])), "2"))
        else:
            if entry.get("repository") != repo_header:
                repo_header = entry.get("repository")
                lines.append(colorize(f"  {repo_header}", "1"))
            lines.append(f"  {index:>2}. {entry.get('branch') or '(detached)'}")
            lines.append(colorize(f"      {shorten_home(str(entry['path']))}", "2"))
    return lines


def resolve_open_target(args, cfg: dict[str, Any], herdr: Any) -> Path:
    target = args.target
    if target:
        token = Path(target).expanduser()
        if token.exists():
            return resolve_existing(token)
        matches = []
        wanted = {target}
        with contextlib.suppress(SafetyError):
            wanted.add(branch_slug(target))
        for name, repo in cfg["repositories"].items():
            if target == name:
                matches.append(Path(repo["path"]))
            candidates = [Path(cfg["worktree_root"]) / name, HERDR_NATIVE_WORKTREES / name,
                          sibling_worktree_dir(Path(repo["path"]))]
            for wtroot in candidates:
                if wtroot.exists():
                    matches += [p for p in wtroot.iterdir() if p.name in wanted]
        if len(matches) == 1:
            return resolve_existing(matches[0])
    entries = open_candidates(cfg, herdr)
    header = None
    if target:
        entries = fuzzy_open_matches(entries, target)
        if len(entries) == 1:
            return resolve_existing(Path(entries[0]["path"]))
        if not entries:
            raise WorkflowError(
                f"nothing matches {target!r}; run 'hwt open' with no argument to pick from the list")
        header = f"no exact match for {target!r}; similar"
    if not entries:
        raise WorkflowError("nothing to open; no repositories are configured")
    index = choose_indexed(open_listing_lines(entries), len(entries), header)
    return resolve_existing(Path(entries[index]["path"]))


def desktop_tabs(herdr: Any, workspace: str) -> dict[str, Any]:
    """Herdr Desktop: the tab ids by Corral name, for the Corral panel to name and order."""
    if not is_desktop(herdr):
        return {}
    return {"tabs": {t["label"]: t["tab_id"] for t in herdr.tabs(workspace) if t["label"]}}


def cmd_open(args,cfg,state,herdr:Herdr):
    path = resolve_open_target(args, cfg, herdr)
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
        obj=herdr.call("worktree","open","--cwd",str(Path(repo["path"])),"--path",str(path),"--focus")
        workspace=obj["result"]["workspace"]["workspace_id"]
        print(json.dumps({**bootstrap(cfg,state,path,workspace,herdr),**desktop_tabs(herdr,workspace)},indent=2))


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
    https = ensure_https(cfg, port)
    if https:
        env["HWT_HTTPS_URL"] = https
    env.update({str(k): str(v) for k, v in repo.get("environment", {}).items()})
    remote = cfg.get("remote_host")
    log(f"Corral: dev server for {name} on port {port} — "
        + (https or (f"http://{remote}:{port}" if remote else f"http://127.0.0.1:{port}")))
    os.chdir(wt)
    if WINDOWS:
        raise SystemExit(subprocess.run(program(command), env=env).returncode)
    os.execvpe(command[0],command,env)


def cmd_status(args,cfg,state):
    leases={}
    p=state/"ports.json"
    if p.exists(): leases=json.loads(p.read_text(encoding="utf-8"))
    rows=[]
    served=https_urls(cfg)
    for path,port in leases.items():
        exists=Path(path).exists(); mismatch=None
        if exists:
            try:
                name,repo,canonical=repo_for_worktree(cfg,Path(path)); dep=repo.get("dependencies",{}); lock=dep.get("lockfile")
                if lock and (Path(path)/lock).exists() and (canonical/lock).exists(): mismatch=lockfile_digest(Path(path)/lock)!=lockfile_digest(canonical/lock)
            except WorkflowError: mismatch="unknown"
        rows.append({"path":path,"port":port,"exists":exists,"lockfile_mismatch":mismatch,"listening":not port_is_free(int(port)),
                     "https_url":served.get(int(port))})
    print(json.dumps({"leases":rows},indent=2))


def remote_head(canonical: Path, remote: str = "origin") -> str | None:
    """The remote's default branch as a remote-tracking ref (origin/develop), if git knows it."""
    found = git(canonical, "symbolic-ref", "--quiet", f"refs/remotes/{remote}/HEAD", check=False)
    return found.stdout.strip().removeprefix("refs/remotes/") or None if found.returncode == 0 else None


def checkout_list(canonical: Path) -> list[dict[str, Any]]:
    """`git worktree list`: the main checkout first, then linked ones."""
    listing = git(canonical, "worktree", "list", "--porcelain", check=False)
    items: list[dict[str, Any]] = []
    for block in listing.stdout.strip().split("\n\n") if listing.returncode == 0 else []:
        lines = block.splitlines()
        path = next((l.removeprefix("worktree ") for l in lines if l.startswith("worktree ")), None)
        if not path or any(l == "bare" or l.startswith("prunable") for l in lines):
            continue
        branch = next((l.removeprefix("branch refs/heads/") for l in lines
                       if l.startswith("branch refs/heads/")), None)
        if WINDOWS:
            path = str(Path(path))  # git's C:/x/y, as Corral's other paths: C:\x\y
        items.append({"path": path, "branch": branch, "main": not items})
    return items


PANEL_DIR = Path("desktop/corral/mods/corral")


def panel_files() -> dict[str, Any] | None:
    """The Corral panel this install ships, for the panel to update itself from."""
    folder = install_root() / PANEL_DIR
    if not folder.is_dir():
        return None
    return {"version": __version__, "dir": str(folder),
            "files": sorted(p.name for p in folder.iterdir() if p.is_file())}


def cmd_info(args, cfg, state):
    """Everything the Corral panel shows for one folder, as JSON."""
    folder = resolve_existing(Path(args.folder or Path.cwd()))
    out: dict[str, Any] = {"version": __version__, "frontend": args.frontend,
                           "panel": panel_files(), "folder": str(folder), "git": False}
    top = git(folder, "rev-parse", "--show-toplevel", check=False)
    common = git(folder, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False)
    if top.returncode or common.returncode:
        print(json.dumps(out, indent=2))
        return
    top_path = Path(top.stdout.strip()).resolve()
    common_dir = Path(common.stdout.strip()).resolve()
    main = common_dir.parent if common_dir.name == ".git" else common_dir
    root = Path(cfg["canonical_root"])
    out.update(git=True, top=str(top_path), main=str(main), linked=top_path != main,
               branch=git(top_path, "branch", "--show-current", check=False).stdout.strip() or None,
               canonical_root=str(root), under_root=is_within(main, root))
    name = next((n for n, r in cfg["repositories"].items()
                 if "path" in r and Path(r["path"]).expanduser().resolve() == main), None)
    out["repository"] = name
    if name is None:
        out["suggested_base"] = remote_head(main)
        out["base_candidates"] = remote_base_candidates(main, "origin")[:6]
        print(json.dumps(out, indent=2))
        return
    repo = cfg["repositories"][name]
    host = cfg.get("remote_host") or "127.0.0.1"
    out.update(mode=repo.get("mode"), base_branch=repo.get("base_branch", "origin/main"),
               dev_command=bool(repo.get("commands", {}).get("dev")),
               defaults_only=repo_is_unconfigured(repo))
    worktrees = []
    served = https_urls(cfg)
    for item in checkout_list(main):
        port = leased_port(state, item["path"])
        worktrees.append({**item, "port": port,
                          "listening": bool(port) and not port_is_free(port),
                          "dev_url": (served.get(port) or f"http://{host}:{port}") if port else None})
    out["worktrees"] = worktrees
    print(json.dumps(out, indent=2))


def cmd_add(args, cfg, state):
    """Configure a repository with safe defaults, no questions (the panel's Set up)."""
    path = resolve_existing(Path(args.path))
    top = git(path, "rev-parse", "--show-toplevel", check=False)
    if top.returncode or Path(top.stdout.strip()).resolve() != path:
        raise WorkflowError(f"not the top folder of a git repository: {path}")
    if not repo_is_canonical_checkout(path):
        raise WorkflowError("that is a linked worktree; add its repository's main checkout")
    root = Path(cfg["canonical_root"])
    if not is_within(path, root):
        raise SafetyError(f"repository outside approved canonical root {root}: {path}")
    name = path.name
    for other, repo in cfg["repositories"].items():
        if other == name or ("path" in repo and Path(repo["path"]).expanduser().resolve() == path):
            raise WorkflowError(f"already configured as {other!r}")
    remote = "origin"
    base = args.base or remote_head(path, remote) or f"{remote}/main"
    if base.split("/", 1)[0] != remote or "/" not in base:
        raise WorkflowError(f"base must be a remote-tracking branch like {remote}/develop")
    if not ref_exists(path, base):
        raise WorkflowError(f"no such ref here: {base!r}")
    overlay = args.config.parent / "repos.d" / f"{name}.toml"
    if overlay.exists():
        raise WorkflowError(f"{overlay} already exists")
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text("\n".join([
        "# Written by 'hwt add': Corral's defaults. Refine with 'hwt init'.",
        f"[repositories.{toml_quote(name)}]",
        f"path = {toml_quote(str(path))}",
        'mode = "worktree"',
        f"base_branch = {toml_quote(base)}",
        f"remote = {toml_quote(remote)}",
        "fetch = true",
        "start_agent = true",
    ]) + "\n", encoding="utf-8")
    print(json.dumps({"repository": name, "path": str(path), "base_branch": base,
                      "overlay": str(overlay)}, indent=2))


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


def choose_worktree_item(cfg: dict[str, Any], items: list[dict[str, Any]],
                         target: str | None) -> dict[str, Any]:
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
    return pool[choose_indexed(worktree_listing_lines(cfg, pool), len(pool), header)]


def cmd_remove(args,cfg,state,herdr):
    target=args.target
    candidates=match_removal_items(configured_worktree_items(cfg, herdr), target)
    if len(candidates)!=1: raise WorkflowError("removal target must identify exactly one Herdr worktree")
    item=candidates[0]; workspace,path=worktree_item_ids(item)
    with worktree_lock(state, path):
        _, repo, canonical = validate_worktree_identity(cfg, path)
        branch=git(path,"branch","--show-current").stdout.strip(); dirty=worktree_dirty(path, repo)
        check_remove_allowed(dirty,args.force,args.confirm,branch)
        # Re-read immediately before the destructive call while holding our workflow lock.
        branch2=git(path,"branch","--show-current").stdout.strip(); dirty2=worktree_dirty(path, repo)
        if branch2 != branch or dirty2 != dirty: raise WorkflowError("worktree changed while removal was being validated")
        suppress_teardown(state, str(path))
        detach_links(path, repo)
        with explained_removal(path, f"hwt remove {target}"):
            if is_desktop(herdr):
                herdr.remove_worktree(canonical, workspace, path, args.force)
            elif workspace:
                focused = focused_workspace_id(herdr)
                cmd=["worktree","remove","--workspace",workspace]
                if args.force: cmd.append("--force")
                herdr.call(*cmd)
                restore_focus_after_removal(herdr, workspace, focused)
            else:
                remove_checkout(canonical, path, args.force)
        release_port(state,str(path),cfg)
    print(json.dumps({"removed":str(path),"branch_preserved":branch},indent=2))


def cmd_cleanup(args, cfg, state, herdr):
    items = configured_worktree_items(cfg, herdr)
    if args.target and not match_removal_items(items, args.target):
        # Not a worktree (any more): perhaps a cleanup that couldn't finish.
        found = pending_cleanup(state, args.target)
        if found:
            print(json.dumps(resume_cleanup(cfg, state, *found, abandon=bool(args.abandon),
                                            confirm=args.confirm), indent=2))
            return
    item = choose_worktree_item(cfg, items, args.target)
    result = cleanup_worktree_item(cfg, state, herdr, item,
                                   abandon=args.abandon, confirm=args.confirm)
    print(json.dumps(result, indent=2))


SAFETY_LABELS = {
    "merged": "merged — cleans instantly",
    "squash-merged": "squash-merged — cleans instantly",
    "no-unique-commits": "nothing unique — cleans instantly",
    "unpublished-work": "has unpublished commits",
    "dirty": "dirty",
    "detached": "detached HEAD",
}

# Plumbing-stable diff output for patch-id: the branch diff and the base log
# must be rendered identically, whatever the user's diff configuration.
PATCH_FLAGS = ("--no-color", "--no-ext-diff", "--no-textconv", "--no-renames")


def squash_merged(canonical: Path, branch_ref: str, base_ref: str) -> bool:
    """Whether the branch's changes already landed on base as different
    commits — a squash merge, or a rebase-and-merge.

    Two independent proofs, either sufficient: merging the branch into base
    would change nothing (merge-tree), or a single base commit carries exactly
    the branch's whole diff (patch-id — this one survives base later editing
    the same lines, which makes merge-tree conflict). A branch with no net
    change proves nothing, so commits that cancel out still need confirmation.
    Any failed probe proves nothing."""
    probe = git(canonical, "merge-base", base_ref, branch_ref, check=False)
    if probe.returncode:
        return False
    fork = probe.stdout.strip()
    diff = git(canonical, "diff", *PATCH_FLAGS, fork, branch_ref, check=False)
    if diff.returncode or not diff.stdout.strip():
        return False
    merged = git(canonical, "merge-tree", "--write-tree", base_ref, branch_ref, check=False)
    base_tree = git(canonical, "rev-parse", f"{base_ref}^{{tree}}", check=False)
    if merged.returncode == 0 and base_tree.returncode == 0 and \
            merged.stdout.split()[:1] == base_tree.stdout.split()[:1]:
        return True
    branch_id = git(canonical, "patch-id", "--stable", input=diff.stdout, check=False)
    log_out = git(canonical, "log", "-p", "--no-merges", "--pretty=medium", *PATCH_FLAGS,
                  f"{fork}..{base_ref}", check=False)
    if branch_id.returncode or log_out.returncode or not branch_id.stdout.strip():
        return False
    base_ids = git(canonical, "patch-id", "--stable", input=log_out.stdout, check=False)
    if base_ids.returncode:
        return False
    wanted = branch_id.stdout.split()[0]
    return any(line.split()[:1] == [wanted] for line in base_ids.stdout.splitlines())


def branch_disposition(repo: dict[str, Any], canonical: Path, branch: str) -> dict[str, Any]:
    """Merge/uniqueness verdict for a branch, from currently-known refs.

    Fetching first is the caller's business: the palette annotates from local
    refs, while cleanup and teardown fetch before deciding. unique_commits
    counts only commits that would survive nowhere if the local branch and its
    own remote copy both vanished — cleanup deletes the pair, and teardown
    offers the remote copy as a follow-up deletion — so a fresh, fully
    cherry-picked, or rebased-elsewhere branch loses nothing. Unique commits
    whose changes provably landed on base anyway (squash_merged) count as
    merged. A failed probe counts as unique: errors may block a deletion,
    never excuse one."""
    remote = repo.get("remote", "origin")
    base_branch = repo.get("base_branch", f"{remote}/main")
    branch_ref = f"refs/heads/{branch}"
    merged = "/" in base_branch and git(canonical, "merge-base", "--is-ancestor", branch_ref,
                                        f"refs/remotes/{base_branch}", check=False).returncode == 0
    unique = squashed = False
    if not merged:
        probe = git(canonical, "rev-list", "-n", "1", branch_ref, "--not",
                    f"--exclude={branch_ref}", f"--exclude=refs/remotes/{remote}/{branch}",
                    "--all", check=False)
        unique = bool(probe.returncode) or bool(probe.stdout.strip())
        if unique and "/" in base_branch:
            squashed = merged = squash_merged(canonical, branch_ref, f"refs/remotes/{base_branch}")
    return {"remote": remote, "base_branch": base_branch, "branch_ref": branch_ref,
            "merged": merged, "squash_merged": squashed, "unique_commits": unique}


def worktree_safety(cfg: dict[str, Any], item: dict[str, Any]) -> str:
    """Advisory annotation from local refs; cleanup itself re-verifies after a fetch."""
    branch = item.get("branch")
    if not branch:
        return "detached"
    path = Path(item.get("path", ""))
    repo = cfg.get("repositories", {}).get(item.get("repository"), {})
    with contextlib.suppress(WorkflowError):
        if path.is_dir() and worktree_dirty(path, repo):
            return "dirty"
    repo_path = repo.get("path")
    if not repo_path:
        return "unknown"
    verdict = branch_disposition(repo, Path(repo_path), branch)
    if verdict["squash_merged"]:
        return "squash-merged"
    if verdict["merged"]:
        return "merged"
    return "unpublished-work" if verdict["unique_commits"] else "no-unique-commits"


SAFETY_COLORS = {"merged": "32", "squash-merged": "32", "no-unique-commits": "32",
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


def worktree_listing_lines(cfg: dict[str, Any], items: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    if not items:
        lines.append("  (none)")
    current_repo = None
    for index, item in enumerate(items, 1):
        repo = item.get("repository", "?")
        if repo != current_repo:
            if current_repo is not None:
                lines.append("")
            lines.append(colorize(repo, "1"))
            current_repo = repo
        safety = worktree_safety(cfg, item)
        label = colorize(f"[{SAFETY_LABELS.get(safety, safety)}]", SAFETY_COLORS.get(safety, "0"))
        branch = item.get("branch") or "(detached)"
        lines.append(f"  {index:>2}. {branch}  {label}")
        lines.append(colorize(f"      {shorten_home(str(item.get('path', '?')))}", "2"))
    return lines


def palette_lines(cfg: dict[str, Any], items: list[dict[str, Any]]) -> list[str]:
    return [colorize("Corral — worktrees", "1"), ""] + worktree_listing_lines(cfg, items)


def choose_indexed(lines: list[str], count: int, header: str | None) -> int:
    """Show a numbered listing (on stderr) and return the chosen zero-based index."""
    if header:
        log(f"{header}:")
    for line in lines:
        log(line)
    if not sys.stdin.isatty():
        raise WorkflowError("re-run with one of the listed names or paths as the target")
    answer = input("Number (Enter cancels): ").strip()
    if not answer:
        raise WorkflowError("cancelled")
    try:
        index = int(answer) - 1
        if not 0 <= index < count:
            raise ValueError
    except ValueError:
        raise WorkflowError(f"invalid selection: {answer!r}") from None
    return index


def palette_new_worktree(args, cfg, state, herdr) -> None:
    new_args = type("Args", (), {"branch": None, "base": None, "repo": None,
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
    stopped = stop_processes(orphan_servers(cfg))
    print(json.dumps({"cleaned": cleaned, "skipped": skipped, "stopped_orphan_servers": stopped}, indent=2))
    if skipped:
        log(f"{len(skipped)} worktree(s) kept; each reason above includes the command to discard it deliberately")


def cmd_reap(args, cfg, state):
    """Stop dev servers left running from deleted worktree checkouts."""
    orphans = orphan_servers(cfg)
    stale_https = orphan_https(cfg, state)
    if args.dry_run:
        print(json.dumps({"orphan_servers": [{k: p[k] for k in ("pid", "command", "cwd", "ports")}
                                             for p in orphans],
                          "orphan_https": stale_https}, indent=2))
    else:
        for item in stale_https:
            stop_https(cfg, item["port"])
        print(json.dumps({"stopped": stop_processes(orphans), "stopped_https": stale_https}, indent=2))


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


def focused_workspace_id(herdr: Any) -> str | None:
    with contextlib.suppress(Exception):
        for workspace in herdr.call("workspace", "list")["result"]["workspaces"]:
            if workspace.get("focused"):
                return workspace.get("workspace_id")
    return None


def restore_focus_after_removal(herdr: Any, removed_workspace: str, focused: str | None) -> None:
    # Herdr unconditionally switches focus to the removed worktree's parent repo
    # workspace (close_removed_linked_worktree_workspace); jump back to wherever
    # the user actually was.
    if focused and focused != removed_workspace:
        with contextlib.suppress(Exception):
            herdr.call("workspace", "focus", focused)


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
    base_remote = base_branch.split("/", 1)[0]
    if base_remote != remote:
        raise WorkflowError("cleanup base_branch remote does not match configured remote")
    if branch in protected_branches(repo):
        raise SafetyError(f"refusing to clean up protected branch: {branch}")
    workspace, path = worktree_item_ids(item)
    with worktree_lock(state, path):
        validate_worktree_identity(cfg, path)
        dirty = worktree_dirty(path, repo)
        if dirty and not abandon:
            raise WorkflowError(
                f"worktree has uncommitted changes; rerun with --abandon --confirm {branch} to discard them")
        # Fetch and prove the merge before any deletion. Authentication failures
        # therefore leave the remote, checkout, branch, and lease untouched.
        git(canonical, "fetch", remote, "--prune")
        verdict = branch_disposition(repo, canonical, branch)
        merged = verdict["merged"]
        if not merged and not abandon and verdict["unique_commits"]:
            raise WorkflowError(
                f"branch {branch} is not merged into {base_branch} and has commits not "
                f"available on any other branch; rerun with --abandon --confirm {branch} to delete it anyway")
        remote_ref = f"refs/remotes/{remote}/{branch}"
        remote_exists = git(canonical, "show-ref", "--verify", "--quiet", remote_ref, check=False).returncode == 0
        suppress_teardown(state, str(path))
        entry = {"repository": repository, "branch": branch, "merged": merged,
                 "merged_into": base_branch if merged else None, "abandoned": bool(abandon),
                 "reason": ("squash-merged" if verdict["squash_merged"] else "merged") if merged
                           else ("abandoned" if abandon else "no-unique-commits")}
        # Decided: written down before anything goes, so that if the removal
        # fails partway (git unregisters a worktree even when it can't delete
        # the folder), running cleanup again finishes it.
        record_cleanup(state, path, entry)
        # Before removal: a server outlives its checkout (and Herdr's tab
        # closing never reaches one an agent started in the background).
        stopped = stop_worktree_servers(state, path)
        detach_links(path, repo)
        with explained_removal(path, f"hwt cleanup {branch}"):
            if is_desktop(herdr):
                herdr.remove_worktree(canonical, workspace, path, dirty)
            elif workspace:
                focused = focused_workspace_id(herdr)
                remove = ["worktree", "remove", "--workspace", workspace]
                if dirty:
                    remove.append("--force")
                herdr.call(*remove)
                restore_focus_after_removal(herdr, workspace, focused)
            else:
                # Not open in Herdr (its remove API is workspace-only); use git directly.
                remove_checkout(canonical, path, dirty)
        # Only now the branches: a failed removal leaves them, and the remote copy, as they were.
        done = finish_cleanup(cfg, state, canonical, remote, path, entry, remote_exists=remote_exists)
    return {**done, "stopped_servers": stopped}


def record_cleanup(state: Path, path: Path, entry: dict[str, Any]) -> None:
    with locked_json(state, "cleanups") as pending:
        pending[str(path.resolve())] = entry


def forget_cleanup(state: Path, path: Path) -> None:
    with locked_json(state, "cleanups") as pending:
        pending.pop(str(path.resolve()), None)


def pending_cleanup(state: Path, target: str) -> tuple[Path, dict[str, Any]] | None:
    """A cleanup that decided, then couldn't finish, matching a branch or path."""
    with locked_json(state, "cleanups") as pending:
        entries = dict(pending)
    wanted = str(Path(target).expanduser().resolve()) if ("/" in target or "\\" in target) else None
    for key, entry in entries.items():
        if target in (entry.get("branch"), key, Path(key).name) or key == wanted:
            return Path(key), entry
    return None


def finish_cleanup(cfg: dict[str, Any], state: Path, canonical: Path, remote: str, path: Path,
                   entry: dict[str, Any], *, remote_exists: bool, local_exists: bool = True) -> dict[str, Any]:
    """After the checkout is gone: its branch, the remote copy, the port lease."""
    branch = entry["branch"]
    if local_exists:
        git(canonical, "update-ref", "-d", f"refs/heads/{branch}")
    if remote_exists:
        git(canonical, "push", remote, "--delete", branch)
    release_port(state, str(path), cfg)
    forget_cleanup(state, path)
    return {
        "removed": str(path),
        "local_branch_deleted": branch if local_exists else None,
        "remote_branch_deleted": branch if remote_exists else None,
        "remote": remote,
        "merged": entry["merged"],
        "merged_into": entry["merged_into"],
        "abandoned": entry["abandoned"],
        "reason": entry["reason"],
    }


def holds_files(folder: Path) -> bool:
    """Whether anything but empty folders is left in folder (links count)."""
    for item in folder.iterdir():
        if is_link(item) or not item.is_dir() or holds_files(item):
            return True
    return False


def resume_cleanup(cfg: dict[str, Any], state: Path, path: Path, entry: dict[str, Any], *,
                   abandon: bool = False, confirm: str | None = None) -> dict[str, Any]:
    """Finish a cleanup whose removal failed partway: the folder git couldn't
    delete (when it's empty), the branch, its remote copy, the lease. The
    merge check was done, or the abandon confirmed, when it started."""
    repo = cfg.get("repositories", {}).get(entry.get("repository"))
    if not repo:
        raise WorkflowError(f"repository {entry.get('repository')!r} is no longer configured")
    canonical = resolve_existing(Path(repo["path"]))
    remote = repo.get("remote", "origin")
    branch = entry["branch"]
    if abandon:
        if confirm != branch:
            raise WorkflowError(f"abandoning requires exact confirmation: --abandon --confirm {branch}")
        entry = {**entry, "abandoned": True, "reason": "abandoned"}
    with worktree_lock(state, path):
        if any(Path(c["path"]).resolve() == path.resolve() for c in checkout_list(canonical)):
            raise WorkflowError(f"{path} is still a worktree; run 'hwt list' to see Herdr's view of it")
        leftover = None
        if path.is_dir():
            if holds_files(path):
                leftover = str(path)
                log(f"{path} still holds files git couldn't delete; delete the folder once nothing has it open")
            else:
                try:
                    shutil.rmtree(path)
                except OSError as exc:
                    raise folder_in_use(path, exc, f"hwt cleanup {branch}") from exc
        local_exists = git(canonical, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}",
                           check=False).returncode == 0
        git(canonical, "fetch", remote, "--prune", check=False)
        if local_exists and not entry.get("abandoned"):
            # The branch may have gained commits since the first run decided.
            verdict = branch_disposition(repo, canonical, branch)
            if not verdict["merged"] and verdict["unique_commits"]:
                raise WorkflowError(
                    f"branch {branch} now has commits not merged into {verdict['base_branch']}; "
                    f"kept it. Delete it anyway with: hwt cleanup {branch} --abandon --confirm {branch}")
        remote_exists = git(canonical, "show-ref", "--verify", "--quiet", f"refs/remotes/{remote}/{branch}",
                            check=False).returncode == 0
        done = finish_cleanup(cfg, state, canonical, remote, path, entry,
                              remote_exists=remote_exists, local_exists=local_exists)
    return {**done, "resumed": True, "leftover_folder": leftover}


def teardown_status(repo: dict[str, Any], canonical: Path, branch: str) -> dict[str, Any]:
    """Assess a branch whose checkout was just deleted, from the canonical repo.

    remote_state distinguishes a branch whose remote copy was deleted (the
    tracking ref existed before `fetch --prune` and is gone after — the
    GitHub merge-then-delete flow) from one that was never pushed. Offline,
    the remote cannot be checked and the state is "unknown"."""
    remote = repo.get("remote", "origin")
    tracking_ref = f"refs/remotes/{remote}/{branch}"
    had_tracking = git(canonical, "show-ref", "--verify", "--quiet", tracking_ref, check=False).returncode == 0
    fetched = git(canonical, "fetch", remote, "--prune", check=False).returncode == 0
    if fetched:
        remote_exists = git(canonical, "show-ref", "--verify", "--quiet", tracking_ref, check=False).returncode == 0
    else:
        remote_exists = had_tracking
    if not fetched:
        remote_state = "unknown"
    elif remote_exists:
        remote_state = "exists"
    elif had_tracking:
        remote_state = "deleted"
    else:
        remote_state = "never-pushed"
    return {**branch_disposition(repo, canonical, branch),
            "remote_state": remote_state, "fetched": fetched}


def teardown_advice(branch: str, status: dict[str, Any]) -> list[str]:
    remote = status["remote"]
    if status["merged"]:
        how = "squash-merged" if status.get("squash_merged") else "merged"
        lines = [colorize(f"{how} into {status['base_branch']} — safe to delete", "32")]
    elif not status["unique_commits"]:
        lines = [colorize("all of its commits exist on other branches — safe to delete", "32")]
    else:
        lines = [colorize(f"NOT merged into {status['base_branch']}; has commits found nowhere else", "33")]
    if status["remote_state"] == "deleted":
        lines.append(f"{remote}/{branch} has already been deleted on the remote (e.g. after a PR merge)")
    elif status["remote_state"] == "exists":
        lines.append(f"{remote}/{branch} still exists on the remote")
    elif status["remote_state"] == "never-pushed":
        lines.append(f"never pushed to {remote}")
    else:
        lines.append(colorize(f"{remote} unreachable — remote branch state unknown", "33"))
    return lines


def cmd_teardown(args, cfg, state):
    """Interactive popup opened by the worktree.removed hook: settle the branch."""
    branch = os.environ.get("HWT_TEARDOWN_BRANCH", "")
    repo = cfg.get("repositories", {}).get(os.environ.get("HWT_TEARDOWN_REPO", ""))
    if not branch or not repo:
        raise WorkflowError("teardown opens automatically after a checkout is deleted in Herdr")
    if not sys.stdin.isatty():
        raise WorkflowError("teardown is interactive; it runs inside the Corral popup")
    if branch in protected_branches(repo):
        raise SafetyError(f"refusing to tear down protected branch: {branch}")
    canonical = resolve_existing(Path(repo["path"]))
    print(colorize("Corral — branch teardown", "1"))
    print()
    print(f"Checkout deleted: {shorten_home(os.environ.get('HWT_TEARDOWN_PATH', '?'))}")
    if os.environ.get("HWT_TEARDOWN_FORCED") == "1":
        print(colorize("It was force-deleted; any uncommitted changes in it are gone.", "33"))
    print(f"Local branch {branch!r} still exists:")
    status = teardown_status(repo, canonical, branch)
    for line in teardown_advice(branch, status):
        print(f"  • {line}")
    print()
    deletable = status["merged"] or not status["unique_commits"]
    if not ask_yes_no(f"Delete local branch {branch!r}?", default=deletable):
        print("Kept the branch.")
    else:
        if not deletable:
            confirm = input("It has commits found nowhere else. Type the branch name to confirm: ").strip()
            if confirm != branch:
                print("Confirmation did not match; kept the branch.")
                input("Enter to close ")
                return
        git(canonical, "update-ref", "-d", f"refs/heads/{branch}")
        print(f"Deleted local branch {branch!r}.")
        if status["remote_state"] == "exists":
            print()
            if ask_yes_no(f"Also delete {status['remote']}/{branch} on the remote?", default=status["merged"]):
                git(canonical, "push", status["remote"], "--delete", branch)
                print(f"Deleted {status['remote']}/{branch}.")
            else:
                print(f"Kept {status['remote']}/{branch}.")
    input("Enter to close ")


def old_junctions(cfg: dict[str, Any]) -> list[str]:
    """Junctions in configured repositories' worktrees (see is_junction)."""
    found = []
    for repo in cfg.get("repositories", {}).values():
        if repo.get("mode") != "worktree" or not Path(repo.get("path", "")).is_dir():
            continue
        for checkout in checkout_list(Path(repo["path"]))[1:]:
            wt = Path(checkout["path"])
            places = [wt / Path(r) for r in corral_link_paths(repo)]
            with contextlib.suppress(OSError):
                places += list(wt.iterdir())
            found += sorted({str(p) for p in places if is_junction(p)})
    return found


def cmd_doctor(args,cfg,state):
    checks=[]
    for key in ("canonical_root","worktree_root"):
        p=Path(cfg[key]); checks.append({"check":key,"ok":p.is_dir(),"value":str(p)})
    desktop = getattr(args, "frontend", None) == "desktop"
    if not desktop:
        checks.append({"check":"HERDR_ENV","ok":os.environ.get("HERDR_ENV")=="1"})
    for tool in ("git", "hc" if desktop else "herdr", cfg.get("agent_kind", "hermes")):
        checks.append({"check":f"command:{tool}","ok":shutil.which(tool) is not None})
    configured_tools = sorted({str(cmd[0]) for repo in cfg["repositories"].values() for cmd in repo.get("commands", {}).values() if cmd})
    for tool in configured_tools:
        checks.append({"check":f"command:{tool}","ok":shutil.which(tool) is not None})
    for name,repo in cfg["repositories"].items():
        p=Path(repo["path"]); checks.append({"check":f"repo:{name}","ok":p.is_dir() and (p/".git").exists(),"value":str(p)})
    if WINDOWS:
        launcher = Path.home() / ".local/bin/hwt.cmd"
        found = shutil.which("hwt")
        checks.append({"check": "launcher", "value": str(launcher),
                       "ok": launcher.is_file() and found is not None
                             and Path(found).resolve() == launcher.resolve()})
    junctions = old_junctions(cfg) if WINDOWS else []
    if junctions:
        checks.append({"check": "junctions", "ok": False, "value": junctions})
        log("These worktrees hold junctions an older Corral made, which Herdr's Delete can empty the "
            "main checkout through. 'hwt open <worktree>' replaces the ones Corral manages; "
            "'rmdir' removes any junction without touching its target: "
            + ", ".join(junctions))
    latest = latest_remote_version()
    update = {"installed": __version__, "latest": latest,
              "update_available": bool(latest and version_tuple(latest) > version_tuple(__version__))}
    stale_https = []
    if https_settings(cfg):
        snapshot = serve_snapshot()
        checks.append({"check": "https:tailscale", "ok": bool(snapshot and snapshot["certs"]),
                       "value": (snapshot["dns"] if snapshot["certs"] else
                                 f"{snapshot['dns']}: HTTPS certificates not enabled for the tailnet")
                                if snapshot else "tailscale CLI missing or not running"})
        stale_https = orphan_https(cfg, state) if snapshot else []
    orphans = [{k: p[k] for k in ("pid", "command", "cwd", "ports")} for p in orphan_servers(cfg)]
    print(json.dumps({"ok":all(x["ok"] for x in checks),"update":update,"checks":checks,
                      "orphan_servers":orphans, "orphan_https":stale_https},indent=2))
    if orphans:
        log(f"{len(orphans)} process(es) still running from deleted worktrees; run: hwt reap")
    if stale_https:
        log(f"{len(stale_https)} HTTPS mapping(s) left from released ports; run: hwt reap")
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
        # Only the removed linked checkout itself, never other paths in the
        # payload (the workspace's), and before its lease is released.
        info = payload.get("data", {}).get("worktree", {})
        if info.get("path") and info.get("is_linked_worktree", False):
            stop_worktree_servers(state, Path(info["path"]))
        # No approved-root guard: releasing is a no-op for paths that never
        # held a lease, and ad-hoc worktree locations must get theirs back too.
        for p in values(payload, {"path", "cwd", "worktree_path"}):
            release_port(state, p, cfg)
        maybe_open_teardown(cfg, state, payload)


def maybe_open_teardown(cfg: dict[str, Any], state: Path, payload: dict[str, Any]) -> None:
    """After a checkout deletion Corral did not run itself (Herdr UI's
    "Delete worktree checkout..."), offer to settle the surviving branch."""
    data = payload.get("data", {})
    info = data.get("worktree", {})
    branch, path = info.get("branch"), info.get("path")
    if not branch or not path or not info.get("is_linked_worktree", False):
        return
    if teardown_suppressed(state, path):
        return
    found = repo_for_removed_worktree(cfg, Path(path))
    if not found:
        return
    name, repo, canonical = found
    if branch in protected_branches(repo) or not canonical.is_dir():
        return
    if git(canonical, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode != 0:
        return
    binary = os.environ.get("HERDR_BIN_PATH", "herdr")
    # A failed popup must never fail the event hook; the branch simply stays.
    with contextlib.suppress(WorkflowError, OSError):
        run([binary, "plugin", "pane", "open", "--plugin", PLUGIN_ID, "--entrypoint", "teardown",
             "--env", f"HWT_TEARDOWN_REPO={name}",
             "--env", f"HWT_TEARDOWN_BRANCH={branch}",
             "--env", f"HWT_TEARDOWN_PATH={path}",
             "--env", f"HWT_TEARDOWN_FORCED={'1' if data.get('forced') else '0'}"])


def detect_frontend(cfg: dict[str, Any]) -> str:
    """Which Herdr runs this hwt: the one it was started from, else config.

    A TUI pane, or a TUI plugin hook or action, means the TUI, even when the
    TUI itself was started inside a Desktop terminal. A herdr-core terminal
    (Desktop's) means Desktop. Anywhere else (an ssh shell, cron) the
    config's `frontend` decides."""
    if (os.environ.get("HERDR_ENV") == "1" or "HERDR_PLUGIN_EVENT" in os.environ
            or "HERDR_WORKSPACE_ID" in os.environ):
        return "tui"
    if os.environ.get("HC_TERMINAL_ID"):
        return "desktop"
    return cfg.get("frontend", "tui")


def parser() -> argparse.ArgumentParser:
    p=argparse.ArgumentParser(prog="hwt",description="Corral: safe Git-worktree workflow for Herdr")
    p.add_argument("--config",type=Path,default=DEFAULT_CONFIG,help=argparse.SUPPRESS)
    p.add_argument("--version",action="version",version=f"hwt {__version__}")
    p.add_argument("--frontend",choices=("tui","desktop"),
                   help="Herdr TUI or Herdr Desktop (default: the one hwt runs in, else config 'frontend', else tui)")
    # metavar hides the internal entry points (event, cleanup-workspace,
    # palette-open, teardown, invoked by Herdr) from usage; omitting their
    # help keeps them out of the table below it.
    sub=p.add_subparsers(dest="command",required=True,
                         metavar="{new,open,init,add,info,list,status,dev,remove,cleanup,sweep,reap,palette,doctor,update}")
    n=sub.add_parser("new",help="create a worktree for a new branch"); n.add_argument("branch",nargs="?",help="omit to choose repository, base, and name interactively"); n.add_argument("--base"); n.add_argument("--repo"); n.add_argument("--background",action="store_true")
    o=sub.add_parser("open",help="open a repository or existing worktree"); o.add_argument("target",nargs="?",help="repository, branch, or worktree name; omit to pick from a list")
    i=sub.add_parser("init",help="interactively configure a repository"); i.add_argument("repository",nargs="?")
    a=sub.add_parser("add",help="configure a repository with defaults, no questions"); a.add_argument("path"); a.add_argument("--base",help="remote-tracking base branch (default: the remote's default branch)")
    f=sub.add_parser("info",help="a folder's repository, worktrees and ports, as JSON"); f.add_argument("folder",nargs="?")
    sub.add_parser("list",help="configured repositories and live worktrees")
    sub.add_parser("status",help="port leases and dev-server status")
    d=sub.add_parser("dev",help="start the configured dev server on a leased port"); d.add_argument("--port",type=int)
    r=sub.add_parser("remove",help="remove a worktree, keeping its branch"); r.add_argument("target"); r.add_argument("--force",action="store_true"); r.add_argument("--confirm")
    c=sub.add_parser("cleanup",help="delete worktree and branch (merged: no questions)"); c.add_argument("target",nargs="?",help="branch, path, or worktree name; omit to pick from a list"); c.add_argument("--abandon",action="store_true",help="delete even if unmerged or dirty (requires --confirm BRANCH)"); c.add_argument("--confirm")
    sub.add_parser("sweep",help="clean up every worktree that qualifies without questions")
    rp=sub.add_parser("reap",help="stop dev servers left running from deleted worktrees"); rp.add_argument("--dry-run",action="store_true",help="list them without stopping anything")
    sub.add_parser("palette",help="interactive worktree palette (runs inside the Corral popup)")
    sub.add_parser("doctor",help="environment checks and update availability")
    sub.add_parser("update",help="update Corral in place (git pull)")
    sub.add_parser("event")
    sub.add_parser("cleanup-workspace")
    sub.add_parser("palette-open")
    sub.add_parser("teardown")
    return p


def utf8_output() -> None:
    """Windows writes piped output in the ANSI code page; the Corral panel and
    Herdr read UTF-8. A console already gets Unicode."""
    if not WINDOWS:
        return
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, OSError, ValueError):
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None=None) -> int:
    utf8_output()
    args=parser().parse_args(argv)
    try:
        if args.command == "update":
            cmd_update(args)
            return 0
        if args.command == "palette-open":
            cmd_palette_open(args)
            return 0
        cfg=load_config(args.config); state=runtime_state_dir()
        args.frontend = args.frontend or detect_frontend(cfg)
        herdr = HerdrCore() if args.frontend == "desktop" else Herdr()
        commands={"add":cmd_add,"info":cmd_info,"new":cmd_new,"open":cmd_open,"init":cmd_init,"list":cmd_list,"status":cmd_status,"dev":cmd_dev,"remove":cmd_remove,"cleanup":cmd_cleanup,"cleanup-workspace":cmd_cleanup_workspace,"sweep":cmd_sweep,"reap":cmd_reap,"palette":cmd_palette,"doctor":cmd_doctor,"event":cmd_event,"teardown":cmd_teardown}
        fn=commands[args.command]
        if args.command in ("new","open","list","remove","cleanup","cleanup-workspace","sweep","palette","event"): fn(args,cfg,state,herdr)
        else: fn(args,cfg,state)
        return 0
    except WorkflowError as exc:
        print(f"hwt: {exc}",file=sys.stderr); return 2


if __name__=="__main__": raise SystemExit(main())

