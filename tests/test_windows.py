"""Corral on Windows. The mocked tests run everywhere and pin the Windows code
paths (hwt.WINDOWS / install.WINDOWS stand in for os.name, which can't be
patched without breaking pathlib); the rest run only on a real Windows."""
import contextlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1]))

import hwt
import install

windows_only = unittest.skipUnless(os.name == "nt", "needs a real Windows")
ROOT = Path(__file__).parents[1]


def as_windows():
    return mock.patch.object(hwt, "WINDOWS", True)


def as_posix():
    return mock.patch.object(hwt, "WINDOWS", False)


class WindowsProgramTests(unittest.TestCase):
    def test_cmd_shims_are_found_the_way_a_shell_finds_them(self):
        found = {"npm": r"C:\nodejs\npm.CMD", "git": r"C:\Git\cmd\git.EXE"}
        with as_windows(), mock.patch.object(hwt.shutil, "which", side_effect=found.get):
            self.assertEqual(hwt.program(["npm", "ci"]), [r"C:\nodejs\npm.CMD", "ci"])
            self.assertEqual(hwt.program(["git", "status"]), ["git", "status"], ".exe files need no help")
            self.assertEqual(hwt.program(["missing"]), ["missing"])
            self.assertEqual(hwt.program([r"C:\x\npm", "ci"]), [r"C:\x\npm", "ci"], "a path is left alone")
        with as_posix(), mock.patch.object(hwt.shutil, "which", side_effect=found.get):
            self.assertEqual(hwt.program(["npm", "ci"]), ["npm", "ci"], "POSIX is unchanged")

    def test_login_shell_is_pwsh_then_powershell_then_comspec(self):
        tools = {"pwsh": r"C:\PS7\pwsh.exe", "powershell": r"C:\Windows\powershell.exe"}
        with as_windows(), mock.patch.object(hwt.shutil, "which", side_effect=tools.get):
            self.assertEqual(hwt.login_shell(), r"C:\PS7\pwsh.exe")
        del tools["pwsh"]
        with as_windows(), mock.patch.object(hwt.shutil, "which", side_effect=tools.get):
            self.assertEqual(hwt.login_shell(), r"C:\Windows\powershell.exe")
        with as_windows(), mock.patch.object(hwt.shutil, "which", return_value=None), \
                mock.patch.dict(os.environ, {"COMSPEC": r"C:\Windows\system32\cmd.exe"}):
            self.assertEqual(hwt.login_shell(), r"C:\Windows\system32\cmd.exe")

    def test_tab_environment_is_set_by_powershell_or_cmd(self):
        env = {"PORT": "4111", "HWT_WORKTREE": r"C:\w\it's"}
        with as_windows(), mock.patch.object(hwt, "login_shell", return_value=r"C:\PS7\pwsh.exe"):
            self.assertEqual(hwt.shell_with_env(env), [
                r"C:\PS7\pwsh.exe", "-NoLogo", "-NoExit", "-Command",
                "${env:PORT} = '4111'; ${env:HWT_WORKTREE} = 'C:\\w\\it''s'"])
        with as_windows(), mock.patch.object(hwt, "login_shell", return_value=r"C:\Windows\system32\cmd.exe"):
            self.assertEqual(hwt.shell_with_env(env), [
                r"C:\Windows\system32\cmd.exe", "/d", "/k", r"set PORT=4111&& set HWT_WORKTREE=C:\w\it's"])
        with as_posix(), mock.patch.object(hwt, "login_shell", return_value="/bin/zsh"):
            self.assertEqual(hwt.shell_with_env({"PORT": "1"}), ["env", "PORT=1", "/bin/zsh"])

    def test_a_windows_shell_in_the_foreground_is_not_an_agent(self):
        core = hwt.HerdrCore("hc")
        for name, agent in ((r"C:\PS7\pwsh.exe", False), ("powershell.exe", False), ("CMD.EXE", False), ("claude.exe", True)):
            with as_windows(), mock.patch.object(core, "_json", return_value={"foreground": {"name": name}}):
                self.assertIs(core.has_agent("t1"), agent, name)

    def test_hc_is_found_where_its_windows_installer_puts_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "Programs" / "herdr-core"
            folder.mkdir(parents=True)
            (folder / "hc.exe").write_bytes(b"")
            with as_windows(), mock.patch.object(hwt.shutil, "which", return_value=None), \
                    mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp}), mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("HC_BIN_PATH", None)
                self.assertEqual(hwt.hc_binary(), str(folder / "hc.exe"))

    def test_herdr_reads_its_config_from_appdata_on_windows(self):
        env = {"APPDATA": "/appdata"}
        with as_windows(), mock.patch.dict(os.environ, env):
            for key in ("XDG_CONFIG_HOME", "HERDR_CONFIG_PATH"):
                os.environ.pop(key, None)
            self.assertEqual(hwt.herdr_config_file(), Path("/appdata/herdr/config.toml"))
        with mock.patch.object(install, "WINDOWS", True), mock.patch.dict(os.environ, env):
            for key in ("XDG_CONFIG_HOME", "HERDR_CONFIG_PATH"):
                os.environ.pop(key, None)
            self.assertEqual(install.herdr_config_path(), Path("/appdata/herdr/config.toml"))
        with as_windows(), mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/xdg", "APPDATA": "/appdata"}):
            os.environ.pop("HERDR_CONFIG_PATH", None)
            self.assertEqual(hwt.herdr_config_file(), Path("/xdg/herdr/config.toml"), "Herdr honors XDG there too")


class WindowsReminderTests(unittest.TestCase):
    def test_tui_reminder_is_printed_by_powershell_without_reading_proc(self):
        api = hwt.Herdr("herdr")
        with as_windows(), mock.patch.object(hwt, "run") as command, \
                mock.patch.object(hwt.os, "readlink", side_effect=AssertionError("no /proc on Windows")):
            api.show_reminder("w2:p3", "Corral: 'hwt dev' starts it")
        self.assertEqual(command.call_args_list[-1], mock.call([
            "herdr", "pane", "run", "w2:p3",
            "Write-Host ''; Write-Host 'Corral: ''hwt dev'' starts it' -ForegroundColor DarkGray; Write-Host ''"]))
        self.assertEqual(len(command.call_args_list), 2, "wait for the prompt, then print; no process-info")

    def test_a_new_tabs_reminder_clears_the_typed_command_away(self):
        api = hwt.Herdr("herdr")
        with as_windows(), mock.patch.object(hwt, "run") as command:
            api.show_reminder("w2:p3", "hello", fresh=True)
        typed = command.call_args_list[-1].args[0][-1]
        self.assertTrue(typed.startswith("Clear-Host; "), typed)

    def test_new_tabs_get_fresh_reminders(self):
        fake = hwt.FakeHerdrForTests()
        fake.seed_workspace("w1", "/wt", ["agent"])
        with mock.patch.object(fake, "show_reminder") as shown:
            hwt.ensure_layout(fake, "w1", Path("/wt"), 4100, "127.0.0.1", "", False, {})
        self.assertEqual([c.kwargs for c in shown.call_args_list], [{"fresh": True}, {"fresh": True}])

    def test_desktop_reminder_is_skipped_cleanly(self):
        core = hwt.HerdrCore("hc")
        with as_windows(), mock.patch.object(hwt, "run") as command, \
                mock.patch.object(core, "_json", side_effect=AssertionError("nothing to look up")):
            core.show_reminder("t1", "hello")
        command.assert_not_called()


class WindowsProcessTests(unittest.TestCase):
    NETSTAT = """
Active Connections

  Proto  Local Address          Foreign Address        State           PID
  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       1044
  TCP    127.0.0.1:4111         0.0.0.0:0              ABHÖREN         7400
  TCP    127.0.0.1:4111         127.0.0.1:50123        ESTABLISHED     7400
  TCP    127.0.0.1:50123        127.0.0.1:4111         ESTABLISHED     9100
  TCP    [::]:4112              [::]:0                 LISTENING       7400
  UDP    0.0.0.0:5353           *:*                                    2200
"""

    def test_listeners_come_from_netstat_whatever_its_language(self):
        with mock.patch.object(hwt.subprocess, "run", return_value=mock.Mock(stdout=self.NETSTAT)) as ran:
            self.assertEqual(hwt.windows_listeners(), {1044: {135}, 7400: {4111, 4112}})
        self.assertEqual(ran.call_args.args[0], ["netstat", "-ano"])

    def test_stopping_asks_then_forces_the_survivors(self):
        procs = [{"pid": 7400, "command": "node.exe", "cwd": None, "ports": [4111]},
                 {"pid": 7401, "command": "esbuild.exe", "cwd": None, "ports": []}]
        alive = {7400, 7401}
        def kill(pid, force):
            if force or pid == 7401:
                alive.discard(pid)
            return True
        with as_windows(), mock.patch.object(hwt, "taskkill", side_effect=kill) as tk, \
                mock.patch.object(hwt, "_alive", side_effect=lambda pid: pid in alive):
            report = hwt.stop_processes(procs, grace=0.2)
        self.assertEqual([c.args for c in tk.call_args_list], [(7400,), (7401,), (7400,)])
        self.assertEqual([c.kwargs["force"] for c in tk.call_args_list], [False, False, True])
        self.assertEqual({r["pid"]: r["signal"] for r in report}, {7400: "KILL", 7401: "TERM"})

    def test_console_programs_that_refuse_a_polite_stop_are_forced_at_once(self):
        procs = [{"pid": 7400, "command": "node.exe", "cwd": None, "ports": [4111]}]
        alive = {7400}
        def kill(pid, force):
            if force:
                alive.discard(pid)
                return True
            return False  # "can only be terminated forcefully"
        started = time.monotonic()
        with as_windows(), mock.patch.object(hwt, "taskkill", side_effect=kill), \
                mock.patch.object(hwt, "_alive", side_effect=lambda pid: pid in alive):
            report = hwt.stop_processes(procs, grace=5.0)
        self.assertLess(time.monotonic() - started, 1.0, "no grace period to wait out")
        self.assertEqual(report[0]["signal"], "KILL")

    def test_a_stale_parent_id_does_not_make_a_child(self):
        # Windows keeps a dead parent's id; 20 was started before the server
        # that reused its parent's id, so it isn't the server's.
        procs = [{"pid": 10, "ppid": 1, "command": "node.exe", "cwd": None, "ports": [4111], "created": 500},
                 {"pid": 11, "ppid": 10, "command": "esbuild.exe", "cwd": None, "ports": [], "created": 600},
                 {"pid": 20, "ppid": 10, "command": "editor.exe", "cwd": None, "ports": [], "created": 100}]
        chosen = hwt.server_trees(procs, lambda p: 4111 in p["ports"])
        self.assertEqual(sorted(p["pid"] for p in chosen), [10, 11])

    def test_forced_processes_are_waited_for_before_the_worktree_goes(self):
        procs = [{"pid": 7400, "command": "node.exe", "cwd": None, "ports": [4111]}]
        checks = iter([True, True, True, False])  # still there right after /F, then gone
        with as_windows(), mock.patch.object(hwt, "taskkill", return_value=False), \
                mock.patch.object(hwt, "_alive", side_effect=lambda pid: next(checks, False)), \
                mock.patch.object(hwt.time, "sleep") as slept:
            hwt.stop_processes(procs)
        self.assertGreaterEqual(slept.call_count, 1)

    def test_without_working_folders_only_the_leased_port_selects_a_server(self):
        procs = [{"pid": 10, "ppid": 1, "command": "node.exe", "cwd": None, "cwd_deleted": False, "ports": [4111]},
                 {"pid": 11, "ppid": 1, "command": "vite.exe", "cwd": None, "cwd_deleted": False, "ports": [5173]}]
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(hwt, "leased_port", return_value=4111), \
                    mock.patch.object(hwt, "user_processes", return_value=procs), \
                    mock.patch.object(hwt, "stop_processes", side_effect=lambda ps: ps):
                stopped = hwt.stop_worktree_servers(Path(tmp), Path(tmp))
            self.assertEqual([p["pid"] for p in stopped], [10])
            cfg = {"worktree_root": tmp, "additional_worktree_roots": []}
            with mock.patch.object(hwt, "user_processes", return_value=procs):
                self.assertEqual(hwt.orphan_servers(cfg), [], "no orphans without working folders")


class WindowsOutputTests(unittest.TestCase):
    def test_child_output_is_read_as_utf8_on_windows(self):
        done = mock.Mock(returncode=0, stdout="C:/Users/Jürgen\n", stderr="")
        with as_windows(), mock.patch.object(hwt.subprocess, "run", return_value=done) as ran:
            hwt.run(["git", "rev-parse", "--show-toplevel"])
        self.assertEqual((ran.call_args.kwargs["encoding"], ran.call_args.kwargs["errors"]), ("utf-8", "replace"))
        with as_posix(), mock.patch.object(hwt.subprocess, "run", return_value=done) as ran:
            hwt.run(["git", "status"])
        self.assertNotIn("encoding", ran.call_args.kwargs, "POSIX is unchanged")


class WindowsCopyTests(unittest.TestCase):
    def test_a_tree_with_links_is_not_copied_on_windows(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, destination = Path(tmp) / "src", Path(tmp) / "dst"
            (source / "pkg").mkdir(parents=True)
            (source / "pkg" / "index.js").write_text("x")
            with as_windows(), mock.patch.object(hwt, "is_link", side_effect=lambda p: p.name == "pkg"):
                self.assertFalse(hwt.clone_tree(source, destination))
            self.assertFalse(destination.exists())
            with as_windows(), mock.patch.object(hwt, "run", side_effect=AssertionError("no cp on Windows")):
                self.assertTrue(hwt.clone_tree(source, destination))
            self.assertEqual((destination / "pkg" / "index.js").read_text(), "x")

    def test_a_file_is_copied_where_windows_cant_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, destination = Path(tmp) / "a.cfg", Path(tmp) / "b.cfg"
            source.write_text("x")
            with as_windows(), mock.patch.object(Path, "symlink_to", side_effect=OSError(1314, "privilege")):
                self.assertEqual(hwt.make_link(destination, source, directory=False), "copy")
            self.assertEqual(destination.read_text(), "x")
            self.assertFalse(destination.is_symlink())
            with as_posix(), mock.patch.object(Path, "symlink_to", side_effect=OSError(1, "nope")):
                with self.assertRaises(OSError, msg="POSIX never substitutes"):
                    hwt.make_link(Path(tmp) / "c.cfg", source, directory=False)

    def test_a_folder_is_copied_never_junctioned_where_windows_cant_symlink(self):
        # Git for Windows before 2.54 deletes through a junction when it removes
        # a worktree (Herdr's Delete), emptying the main checkout's folder.
        with tempfile.TemporaryDirectory() as tmp:
            source, destination = Path(tmp) / "shared", Path(tmp) / "wt" / "shared"
            (source / "sub").mkdir(parents=True)
            (source / "sub" / "a.txt").write_text("x")
            destination.parent.mkdir()
            with as_windows(), mock.patch.object(Path, "symlink_to", side_effect=OSError(1314, "privilege")):
                self.assertEqual(hwt.make_link(destination, source, directory=True), "copy")
            self.assertFalse(hasattr(hwt, "create_junction"), "Corral makes no junctions")
            self.assertEqual((destination / "sub" / "a.txt").read_text(), "x")
            self.assertFalse(destination.is_symlink())

    def test_shared_dependencies_without_symlinks_get_their_own_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            canon, wt = Path(tmp) / "canon", Path(tmp) / "wt"
            (canon / "node_modules" / "pkg").mkdir(parents=True)
            (canon / "node_modules" / "pkg" / "index.js").write_text("keep")
            wt.mkdir()
            (canon / "package.json").write_text("{}")
            (wt / "package.json").write_text("{}")
            (canon / "package-lock.json").write_bytes(b'{\n  "a": 1\n}\n')
            (wt / "package-lock.json").write_bytes(b'{\r\n  "a": 1\r\n}\r\n')  # core.autocrlf
            dep = {"policy": "shared"}
            with as_windows(), mock.patch.object(Path, "symlink_to", side_effect=OSError(1314, "privilege")), \
                    contextlib.redirect_stderr(io.StringIO()) as said:
                self.assertEqual(hwt.prepare_dependencies(canon, wt, dep), "cloned")
                self.assertEqual(hwt.prepare_dependencies(canon, wt, dep), "present")
            self.assertIn("Developer Mode", said.getvalue())
            self.assertFalse(hwt.is_link(wt / "node_modules"))
            self.assertEqual((wt / "node_modules" / "pkg" / "index.js").read_text(), "keep")

    def test_a_removal_that_fails_on_a_folder_in_use_says_so(self):
        failure = hwt.WorkflowError("command failed (git) with exit status 255: "
                                    "error: failed to delete 'C:/wt/x': Permission denied")
        with as_windows(), self.assertRaises(hwt.FolderInUse) as caught:
            with hwt.explained_removal(Path("C:/wt/x"), "hwt cleanup feature/x"):
                raise failure
        self.assertIn("a program probably has the folder open", str(caught.exception))
        self.assertIn("run 'hwt cleanup feature/x' again", str(caught.exception))
        herdr_failure = hwt.WorkflowError('command failed (herdr) with exit status 1: {"error":{"code":"worktree_remove_failed"}}')
        with as_windows(), self.assertRaises(hwt.FolderInUse):
            with hwt.explained_removal(Path("C:/wt/x"), "hwt cleanup feature/x"):
                raise herdr_failure
        with as_posix(), self.assertRaises(hwt.WorkflowError) as caught:
            with hwt.explained_removal(Path("/wt/x"), "hwt cleanup feature/x"):
                raise failure
        self.assertIs(caught.exception, failure, "POSIX is unchanged")

    def test_a_symlinked_file_copied_on_windows_counts_as_current_next_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            canon, wt = Path(tmp) / "canon", Path(tmp) / "wt"
            canon.mkdir(); wt.mkdir()
            (canon / "local.cfg").write_text("secret")
            ops = [{"path": "local.cfg", "action": "symlink"}]
            logs = []
            with as_windows(), mock.patch.object(Path, "symlink_to", side_effect=OSError(1314, "privilege")):
                hwt.apply_file_operations(canon, wt, ops, logger=logs.append)
                hwt.apply_file_operations(canon, wt, ops, logger=logs.append)
            self.assertEqual((wt / "local.cfg").read_text(), "secret")
            self.assertEqual(logs, ["copied local path (this Windows can't make symlinks): local.cfg",
                                    "copy already current: local.cfg"])

    def test_a_copy_that_no_longer_matches_its_source_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            canon, wt = Path(tmp) / "canon", Path(tmp) / "wt"
            canon.mkdir(); wt.mkdir()
            (canon / "local.cfg").write_text("one")
            ops = [{"path": "local.cfg", "action": "symlink"}]
            logs = []
            with as_windows(), mock.patch.object(Path, "symlink_to", side_effect=OSError(1314, "privilege")):
                hwt.apply_file_operations(canon, wt, ops, logger=logs.append)
                (canon / "local.cfg").write_text("two")
                hwt.apply_file_operations(canon, wt, ops, logger=logs.append)  # no error: bootstrap goes on
            self.assertEqual((wt / "local.cfg").read_text(), "one")
            self.assertEqual(logs[-1], "copy differs from local.cfg in the main checkout; left as is")

    def test_corrals_links_and_old_junctions_go_before_a_worktree_is_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            wt = Path(tmp)
            for name in ("node_modules", "shared", "old", "mine"):
                (wt / name).mkdir()
            repo = {"files": [{"path": "shared", "action": "symlink"}, {"path": ".env", "action": "copy"}]}
            removed = []
            with as_windows(), mock.patch.object(hwt, "is_link", side_effect=lambda p: p.name != ".env"), \
                    mock.patch.object(hwt, "is_junction", side_effect=lambda p: p.name == "old"), \
                    mock.patch.object(hwt, "remove_link", side_effect=removed.append):
                hwt.detach_links(wt, repo)
            self.assertEqual(removed, [wt / "node_modules", wt / "shared", wt / "old"],
                             "a link of the person's own (mine) stays")
            removed.clear()
            with as_posix(), mock.patch.object(hwt, "is_link", return_value=True), \
                    mock.patch.object(hwt, "remove_link", side_effect=removed.append):
                hwt.detach_links(wt, repo)
            self.assertEqual(removed, [wt / "node_modules", wt / "shared"])


class WindowsInstallTests(unittest.TestCase):
    def test_the_launcher_runs_hwt_with_the_installing_python(self):
        self.assertEqual(install.windows_launcher(r"C:\Py313\python.exe", Path("C:/x/100%/hwt.py")),
                         f'@"C:\\Py313\\python.exe" -X utf8 "{Path("C:/x/100%%/hwt.py")}" %*\r\n')

    def test_the_windows_plugin_runs_every_hook_with_full_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            shutil.copy(ROOT / "herdr-plugin.toml", source / "herdr-plugin.toml")
            folder = install.write_windows_plugin(source, r"C:\Py313\python.exe")
            generated = tomllib.loads((folder / "herdr-plugin.toml").read_text(encoding="utf-8"))
        original = tomllib.loads((ROOT / "herdr-plugin.toml").read_text(encoding="utf-8"))
        self.assertEqual(folder.name, install.WINDOWS_PLUGIN_DIR)
        self.assertEqual(generated["platforms"], ["windows"])
        for key in ("id", "name", "version", "min_herdr_version", "description"):
            self.assertEqual(generated[key], original[key])
        for section in ("events", "actions", "panes"):
            self.assertEqual(len(generated[section]), len(original[section]))
            for made, item in zip(generated[section], original[section]):
                self.assertEqual(item["command"][:2], ["python3", "hwt.py"])
                self.assertEqual(made["command"], [r"C:\Py313\python.exe", "-X", "utf8", str(source / "hwt.py"), *item["command"][2:]])
                self.assertEqual({k: v for k, v in made.items() if k != "command"},
                                 {k: v for k, v in item.items() if k != "command"})

    def test_the_generated_manifest_is_valid_toml_for_any_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "clone \U0001f600 é"
            source.mkdir()
            shutil.copy(ROOT / "herdr-plugin.toml", source / "herdr-plugin.toml")
            folder = install.write_windows_plugin(source, r"C:\Py\python.exe")
            generated = tomllib.loads((folder / "herdr-plugin.toml").read_text(encoding="utf-8"))
        self.assertEqual(generated["events"][0]["command"][3], str(source / "hwt.py"))

    def fake_winreg(self, path=None):
        store = {} if path is None else {"Path": (path, 2)}
        reg = types.SimpleNamespace(HKEY_CURRENT_USER=1, KEY_READ=1, KEY_WRITE=2, REG_SZ=1, REG_EXPAND_SZ=2, store=store)
        class Key:
            def __enter__(self): return self
            def __exit__(self, *a): return False
        reg.OpenKey = lambda *a: Key()
        def query(_key, name):
            if name not in store:
                raise FileNotFoundError(name)
            return store[name]
        reg.QueryValueEx = query
        reg.SetValueEx = lambda _key, name, _r, kind, value: store.__setitem__(name, (value, kind))
        return reg

    def test_the_bin_folder_joins_the_user_path_once(self):
        reg = self.fake_winreg(r"%USERPROFILE%\AppData\Local\Microsoft\WindowsApps")
        with mock.patch.dict(sys.modules, {"winreg": reg}), mock.patch.dict(os.environ, {"PATH": "/usr/bin"}), \
                mock.patch.object(install, "broadcast_environment") as told:
            self.assertTrue(install.add_to_user_path(Path("/home/b/.local/bin")))
            self.assertFalse(install.add_to_user_path(Path("/home/b/.local/bin")))
        told.assert_called_once()
        self.assertEqual(reg.store["Path"], (r"%USERPROFILE%\AppData\Local\Microsoft\WindowsApps;" + str(Path("/home/b/.local/bin")), 2))
        empty = self.fake_winreg()
        with mock.patch.dict(sys.modules, {"winreg": empty}), mock.patch.dict(os.environ, {"PATH": ""}), \
                mock.patch.object(install, "broadcast_environment"):
            self.assertTrue(install.add_to_user_path(Path("/b")))
        self.assertEqual(empty.store["Path"], (str(Path("/b")), 2))


def junction(target, at):
    """What Corral 1.3.0 made where Windows couldn't symlink a folder."""
    import _winapi
    _winapi.CreateJunction(str(target), str(at))


def can_symlink(folder):
    try:
        (folder / ".probe").symlink_to(folder, target_is_directory=True)
    except OSError:
        return False
    (folder / ".probe").rmdir()
    return True


def git_version():
    out = subprocess.run(["git", "--version"], capture_output=True, text=True).stdout
    return hwt.version_tuple(out)


@windows_only
class RealWindowsTests(unittest.TestCase):
    def test_install_writes_hwt_cmd_and_it_runs_hwt(self):
        with tempfile.TemporaryDirectory() as tmp:
            launcher = install.write_launcher(Path(tmp), ROOT)
            self.assertEqual(launcher.name, "hwt.cmd")
            result = subprocess.run([str(launcher), "--version"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), f"hwt {hwt.__version__}")
            failed = subprocess.run([str(launcher), "no-such-command"], capture_output=True, text=True)
            self.assertNotEqual(failed.returncode, 0, "hwt's exit code comes through hwt.cmd")

    def test_a_junction_is_a_link_and_removing_it_keeps_its_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            target, link = Path(tmp) / "canon_modules", Path(tmp) / "node_modules"
            target.mkdir()
            (target / "keep.js").write_text("x")
            junction(target, link)
            self.assertTrue(hwt.is_link(link))
            self.assertTrue(hwt.is_junction(link))
            self.assertFalse(hwt.is_link(target))
            self.assertEqual(link.resolve(), target.resolve())
            hwt.remove_link(link)
            self.assertFalse(link.exists())
            self.assertEqual((target / "keep.js").read_text(), "x")

    def repo_with_worktree(self, tmp):
        repo, wt = Path(tmp) / "repo", Path(tmp) / "wt"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@x", "-c", "user.name=T",
                        "commit", "-q", "--allow-empty", "-m", "x"], check=True)
        (repo / "node_modules" / "pkg").mkdir(parents=True)
        (repo / "node_modules" / "pkg" / "index.js").write_text("keep")
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", str(wt)], check=True)
        return repo, wt

    def test_shared_dependencies_are_never_a_junction(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, wt = self.repo_with_worktree(tmp)
            dep = {"policy": "shared"}
            with mock.patch.object(Path, "symlink_to", side_effect=OSError(1314, "privilege")), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertNotEqual(hwt.prepare_dependencies(repo, wt, dep), "shared")
            self.assertFalse(hwt.is_link(wt / "node_modules"), "no symlinks here: no link at all")
            subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)], check=True)
            self.assertEqual((repo / "node_modules" / "pkg" / "index.js").read_text(), "keep")

    def test_an_old_corrals_junction_is_replaced_and_the_main_checkout_survives_git(self):
        # Herdr's Delete is git worktree remove --force, without Corral's detach_links.
        with tempfile.TemporaryDirectory() as tmp:
            repo, wt = self.repo_with_worktree(tmp)
            junction(repo / "node_modules", wt / "node_modules")
            with contextlib.redirect_stderr(io.StringIO()):
                hwt.prepare_dependencies(repo, wt, {"policy": "shared"})
            self.assertFalse(hwt.is_junction(wt / "node_modules"))
            subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)], check=True)
            self.assertEqual((repo / "node_modules" / "pkg" / "index.js").read_text(), "keep")

    def test_git_deletes_a_directory_symlink_without_following_it(self):
        # Shared dependencies are a symlink where Windows allows one (Developer
        # Mode, or an administrator such as CI): git, and so Herdr's Delete, must
        # remove only the link.
        with tempfile.TemporaryDirectory() as tmp:
            repo, wt = self.repo_with_worktree(tmp)
            if not can_symlink(Path(tmp)):
                self.skipTest("this Windows can't make symlinks")
            self.assertEqual(hwt.prepare_dependencies(repo, wt, {"policy": "shared"}), "shared")
            self.assertTrue((wt / "node_modules").is_symlink())
            subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)], check=True)
            self.assertEqual((repo / "node_modules" / "pkg" / "index.js").read_text(), "keep")

    def test_git_older_than_2_54_deletes_through_a_junction(self):
        # Why Corral makes no junctions; and why CI (newer Git) never saw it.
        print(f"git {'.'.join(map(str, git_version()))}", file=sys.stderr)
        with tempfile.TemporaryDirectory() as tmp:
            repo, wt = self.repo_with_worktree(tmp)
            junction(repo / "node_modules", wt / "node_modules")
            subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)], check=True)
            kept = (repo / "node_modules" / "pkg" / "index.js").exists()
            self.assertEqual(kept, git_version() >= (2, 54), "Git for Windows added the junction check in 2.54")

    def test_a_tree_holding_a_junction_is_not_copied(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, destination = Path(tmp) / "src", Path(tmp) / "dst"
            (source / "real").mkdir(parents=True)
            junction(source / "real", source / "linked")
            self.assertFalse(hwt.clone_tree(source, destination))
            self.assertFalse(destination.exists())

    def test_checkout_paths_use_windows_separators(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "x"], check=True)
            paths = [c["path"] for c in hwt.checkout_list(repo)]
            self.assertEqual(len(paths), 1)
            self.assertNotIn("/", paths[0])
            self.assertEqual(Path(paths[0]).resolve(), repo.resolve())

    def test_a_listening_port_is_not_free(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            self.assertFalse(hwt.port_is_free(server.getsockname()[1]))

    SERVER = ("import socket, subprocess, sys, time\n"
              "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
              "s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen()\n"
              "print(child.pid, s.getsockname()[1], flush=True); time.sleep(60)\n")

    def test_the_server_on_a_leased_port_and_its_child_are_stopped(self):
        server = subprocess.Popen([sys.executable, "-c", self.SERVER], stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (server.kill(), server.wait()))
        child, port = map(int, server.stdout.readline().split())
        self.addCleanup(lambda: hwt._alive(child) and hwt.taskkill(child, force=True))
        self.assertTrue(hwt._alive(server.pid))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            procs = hwt.user_processes()
            mine = next((p for p in procs if p["pid"] == server.pid), None)
            if mine and port in mine["ports"]:
                break
            time.sleep(0.1)
        else:
            self.fail("the test server's listening port was never found")
        self.assertTrue(any(p["pid"] == os.getpid() for p in procs), "this process is the user's own")
        self.assertTrue(all(p["cwd"] is None for p in procs))
        with tempfile.TemporaryDirectory() as tmp:
            state, wt = Path(tmp) / "state", Path(tmp) / "wt"
            wt.mkdir()
            with mock.patch.object(hwt, "leased_port", return_value=port):
                report = hwt.stop_worktree_servers(state, wt)
        server.wait(timeout=10)
        self.assertEqual({r["pid"] for r in report}, {server.pid, child})
        self.assertLessEqual({r["signal"] for r in report}, {"TERM", "KILL"})
        self.assertFalse(hwt._alive(child))
        self.assertFalse(hwt._alive(server.pid))


if __name__ == "__main__":
    unittest.main()
