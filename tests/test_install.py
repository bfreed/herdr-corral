import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1]))

import install


def write_script(path: Path, body: str) -> Path:
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def fake_repo(parent: Path, name: str) -> None:
    (parent / name / ".git").mkdir(parents=True)


def tree_snapshot(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


posix_only = unittest.skipUnless(os.name == "posix", "shell stubs require POSIX")


class PreflightDetectionTests(unittest.TestCase):
    def test_repo_parents_are_counted_deduped_and_ranked(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            fake_repo(home / "code", "one")
            fake_repo(home / "code", "two")
            fake_repo(home / "projects", "solo")
            fake_repo(home / "stuff", "a")   # non-conventional, one repo: below threshold
            fake_repo(home / "work", "x")
            fake_repo(home / "work", "y")
            fake_repo(home / "work", "z")
            parents = install.preflight_repo_parents(home)
        self.assertEqual([(Path(p["path"]).name, p["repositories"]) for p in parents],
                         [("work", 3), ("code", 2), ("projects", 1)])

    def test_worktree_report_reads_configured_herdr_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            chosen = home / "wt"
            chosen.mkdir()
            (home / "repos" / "demo__worktrees").mkdir(parents=True)
            report = install.preflight_worktrees(
                home, {"worktrees": {"directory": str(chosen)}},
                [{"path": str(home / "repos"), "repositories": 0}])
        self.assertEqual(report["herdr_directory"], str(chosen))
        self.assertTrue(report["herdr_directory_exists"])
        self.assertTrue(report["herdr_directory_configured"])
        self.assertEqual(len(report["sibling_collections"]), 1)

    def test_network_without_tailscale_reports_hostname_only(self):
        with mock.patch.object(install.shutil, "which", return_value=None), \
             mock.patch.object(install.socket, "gethostname", return_value="box"):
            report = install.preflight_network()
        self.assertEqual(report, {"hostname": "box",
                                  "tailscale_dns_name": None, "tailscale_ip": None})

    def test_herdr_binary_override_is_honored(self):
        with mock.patch.dict(os.environ, {"HERDR_BIN_PATH": "/opt/herdr/bin/herdr"}):
            self.assertEqual(install.herdr_binary(), "/opt/herdr/bin/herdr")

    def test_prerequisites_gate_os_and_herdr_version(self):
        with mock.patch.object(install.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(install, "command_output", return_value="herdr 0.7.9"):
            prereqs = install.preflight_prerequisites()
        self.assertTrue(prereqs["os_ok"])
        self.assertIs(prereqs["herdr_version_ok"], False)
        self.assertEqual(prereqs["herdr_minimum"], "0.8.0")


class PreflightKeybindingTests(unittest.TestCase):
    def test_empty_config_suggests_a_key_outside_herdr_defaults(self):
        report = install.preflight_keybindings({})
        self.assertEqual(report["suggested_palette_key"], "prefix+u")
        suggested = report["suggested_palette_key"].removeprefix("prefix+")
        self.assertNotIn(suggested, install.HERDR_DEFAULT_PREFIX_KEYS)

    def test_suggestion_skips_user_bound_keys(self):
        cfg = {"keys": {"prefix": "ctrl+a", "command": [
            {"key": "prefix+u", "type": "plugin_action", "command": "other.thing"}]}}
        report = install.preflight_keybindings(cfg)
        self.assertEqual(report["prefix"], "ctrl+a")
        self.assertEqual(report["suggested_palette_key"], "prefix+i")

    def test_existing_palette_binding_is_reported(self):
        cfg = {"keys": {"command": [
            {"key": "prefix+w", "type": "plugin_action", "command": "corral.palette"}]}}
        self.assertEqual(install.preflight_keybindings(cfg)["palette_binding"], "prefix+w")

    def test_palette_match_requires_plugin_action_type_like_hwt(self):
        cfg = {"keys": {"command": [
            {"key": "prefix+w", "type": "shell", "command": "corral.palette"}]}}
        self.assertIsNone(install.preflight_keybindings(cfg)["palette_binding"])

    def test_exhausted_preferred_candidates_fall_back_past_letters(self):
        commands = [{"key": f"prefix+{k}", "type": "plugin_action", "command": "x"}
                    for k in install.PALETTE_KEY_CANDIDATES]
        report = install.preflight_keybindings({"keys": {"command": commands}})
        # Herdr's default letters + the preferred set cover a-z entirely, so
        # the fallback must land on a non-letter key.
        self.assertEqual(report["suggested_palette_key"], "prefix+0")

    def test_full_exhaustion_yields_none_not_an_invented_key(self):
        commands = [{"key": f"prefix+{k}", "type": "plugin_action", "command": "x"}
                    for k in install.PALETTE_KEY_CANDIDATES + "0.,;'"]
        self.assertIsNone(
            install.preflight_keybindings({"keys": {"command": commands}})["suggested_palette_key"])

    def test_unreadable_config_degrades_to_unavailable(self):
        self.assertEqual(install.preflight_keybindings(None), {"available": False})


class PreflightAgentProbeTests(unittest.TestCase):
    @posix_only
    def test_vendor_bin_directory_is_probed_when_path_misses(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            vendor = home / ".grok" / "bin"
            vendor.mkdir(parents=True)
            write_script(vendor / "grok", "exit 0")
            shell = write_script(home / "no-op-shell", "exit 1")
            with mock.patch.object(install.shutil, "which", return_value=None), \
                 mock.patch.dict(os.environ, {"SHELL": str(shell)}):
                found = install.probe_agent_clis(["grok"], home)
        self.assertEqual(found, {"grok": str(vendor / "grok")})

    @posix_only
    def test_hanging_login_shell_is_bounded_by_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            shell = write_script(home / "hanging-shell", "sleep 5")
            with mock.patch.object(install.shutil, "which", return_value=None), \
                 mock.patch.object(install, "SHELL_PROBE_TIMEOUT", 0.2), \
                 mock.patch.dict(os.environ, {"SHELL": str(shell)}):
                found = install.probe_agent_clis(["ghost"], home)
        self.assertEqual(found, {})


class PreflightEndToEndTests(unittest.TestCase):
    def run_preflight(self, home: Path, shell: Path) -> tuple[int, dict, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        env = {"HOME": str(home), "XDG_CONFIG_HOME": str(home / "xdg"),
               "SHELL": str(shell)}
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(install.shutil, "which", return_value=None), \
             mock.patch.object(install.socket, "getfqdn", return_value="box.local"), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = install.cmd_preflight()
        return status, json.loads(stdout.getvalue()), stderr.getvalue()

    @posix_only
    def test_report_covers_every_field_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            fake_repo(home / "code", "demo")
            (home / "xdg" / "herdr").mkdir(parents=True)
            (home / "xdg" / "herdr" / "config.toml").write_text(
                '[worktrees]\ndirectory = "~/wt"\n')
            shell = write_script(home / "no-op-shell", "exit 1")
            before = tree_snapshot(home)
            status, report, summary = self.run_preflight(home, shell)
            after = tree_snapshot(home)
        self.assertEqual(status, 0)
        self.assertEqual(before, after)
        for field in ("prerequisites", "repository_parents", "worktrees",
                      "network", "agents", "keybindings"):
            self.assertIn(field, report)
        self.assertEqual(report["repository_parents"][0]["repositories"], 1)
        self.assertTrue(report["worktrees"]["herdr_directory_configured"])
        self.assertIn("Repository parents:", summary)

    @posix_only
    def test_missing_tomllib_degrades_but_still_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "xdg" / "herdr").mkdir(parents=True)
            (home / "xdg" / "herdr" / "config.toml").write_text("[keys]\n")
            shell = write_script(home / "no-op-shell", "exit 1")
            with mock.patch.dict(sys.modules, {"tomllib": None}):
                status, report, _ = self.run_preflight(home, shell)
        self.assertEqual(status, 0)
        self.assertEqual(report["keybindings"], {"available": False})
        self.assertIn("python_ok", report["prerequisites"])


class VerifyTests(unittest.TestCase):
    def run_verify(self, home: Path, tests_dir: Path) -> tuple[int, dict, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": str(home)}), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = install.cmd_verify(Path(__file__).parents[1], tests_dir)
        return status, json.loads(stdout.getvalue()), stderr.getvalue()

    @staticmethod
    def plugin_payload():
        return json.dumps({"result": {"plugins": [{
            "plugin_id": "corral", "enabled": True,
            "actions": [{"id": "cleanup"}, {"id": "palette"}]}]}})

    @posix_only
    def test_failing_stub_suite_fails_verdict_with_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            stub = home / "stub-tests"
            stub.mkdir()
            (stub / "test_broken.py").write_text(
                "import unittest\n\nclass T(unittest.TestCase):\n"
                "    def test_no(self):\n        self.fail('boom')\n")
            status, verdict, _ = self.run_verify(home, stub)
        self.assertEqual(status, 1)
        self.assertFalse(verdict["ok"])
        suite = next(c for c in verdict["checks"] if c["check"] == "unit-tests")
        self.assertFalse(suite["ok"])
        self.assertIn("boom", suite["detail"])

    @posix_only
    def test_passing_stub_suite_is_quiet_and_verdict_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            stub = home / "stub-tests"
            stub.mkdir()
            (stub / "test_fine.py").write_text(
                "import unittest\n\nclass T(unittest.TestCase):\n"
                "    def test_yes(self):\n        pass\n")
            bin_dir = home / ".local" / "bin"
            bin_dir.mkdir(parents=True)
            write_script(bin_dir / "hwt", "exit 0")
            config_dir = home / ".config" / "herdr-corral"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text("")
            with mock.patch.object(install.shutil, "which", return_value="/usr/bin/herdr"), \
                 mock.patch.object(install, "command_output",
                                   return_value=self.plugin_payload()):
                status, verdict, stderr = self.run_verify(home, stub)
        self.assertEqual(status, 0)
        self.assertTrue(verdict["ok"])
        self.assertNotIn("test_yes", stderr)
        suite = next(c for c in verdict["checks"] if c["check"] == "unit-tests")
        self.assertEqual(suite["detail"], "")

    @posix_only
    def test_configured_repository_that_is_not_a_git_repo_fails_verify(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            stub = home / "stub-tests"
            stub.mkdir()
            (stub / "test_fine.py").write_text(
                "import unittest\n\nclass T(unittest.TestCase):\n"
                "    def test_yes(self):\n        pass\n")
            bin_dir = home / ".local" / "bin"
            bin_dir.mkdir(parents=True)
            write_script(bin_dir / "hwt", "exit 0")
            config_dir = home / ".config" / "herdr-corral"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                f'[repositories."gone"]\npath = "{home / "nowhere"}"\n')
            with mock.patch.object(install.shutil, "which", return_value="/usr/bin/herdr"), \
                 mock.patch.object(install, "command_output",
                                   return_value=self.plugin_payload()):
                status, verdict, _ = self.run_verify(home, stub)
        self.assertEqual(status, 1)
        repos = next(c for c in verdict["checks"] if c["check"] == "repositories")
        self.assertFalse(repos["ok"])
        self.assertIn("gone", repos["detail"])

    def test_unregistered_plugin_fails_the_plugin_check(self):
        empty = json.dumps({"result": {"plugins": []}})
        with mock.patch.object(install.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(install, "command_output", return_value=empty):
            ok, detail = install.verify_plugin_registration()
        self.assertFalse(ok)
        self.assertIn("not registered", detail)

    def test_missing_herdr_fails_with_message_not_traceback(self):
        with mock.patch.object(install.shutil, "which", return_value=None):
            ok, detail = install.verify_plugin_registration()
        self.assertFalse(ok)
        self.assertEqual(detail, "herdr is not on PATH")


if __name__ == "__main__":
    unittest.main()
