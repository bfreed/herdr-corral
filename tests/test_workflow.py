import json
import os
import socket
import tempfile
import time
import unittest
import contextlib
import io
from pathlib import Path
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).parents[1]))

import hwt


def _symlinks_supported() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "target"
        target.mkdir()
        try:
            (Path(tmp) / "link").symlink_to(target, target_is_directory=True)
            return True
        except OSError:
            return False


needs_symlinks = unittest.skipUnless(_symlinks_supported(), "symlinks unavailable on this system")
posix_only = unittest.skipUnless(os.name == "posix", "POSIX permissions required")


class PathSafetyTests(unittest.TestCase):
    def test_repo_for_worktree_accepts_explicit_additional_root(self):
        native = self.root / "native"
        wt = native / "demo" / "task"
        wt.mkdir(parents=True)
        cfg = {
            "worktree_root": str(self.worktrees),
            "additional_worktree_roots": [str(native)],
            "repositories": {"demo": {"path": str(self.canonical)}},
        }
        name, _, canonical = hwt.repo_for_worktree(cfg, wt)
        self.assertEqual(name, "demo")
        self.assertEqual(canonical, self.canonical.resolve())

    def test_repo_for_worktree_rejects_unconfigured_parallel_root(self):
        outside = self.root / "outside" / "demo" / "task"
        outside.mkdir(parents=True)
        cfg = {
            "worktree_root": str(self.worktrees),
            "additional_worktree_roots": [],
            "repositories": {"demo": {"path": str(self.canonical)}},
        }
        with self.assertRaises(hwt.SafetyError):
            hwt.repo_for_worktree(cfg, outside)

    def test_distinct_branches_have_distinct_slugs(self):
        self.assertNotEqual(hwt.branch_slug("feature/alpha"), hwt.branch_slug("feature-alpha"))

    def test_sibling_worktrees_directory_is_recognized(self):
        wt = self.root / "repos" / "demo__worktrees" / "hermes-task"
        wt.mkdir(parents=True)
        cfg = {
            "worktree_root": str(self.worktrees),
            "additional_worktree_roots": [],
            "repositories": {"demo": {"path": str(self.canonical)}},
        }
        name, _, canonical = hwt.repo_for_worktree(cfg, wt)
        self.assertEqual(name, "demo")
        self.assertEqual(canonical, self.canonical.resolve())

    def test_herdr_native_worktree_root_is_recognized(self):
        native = self.root / "herdr-native-worktrees"
        wt = native / "demo" / "worktree-green-river"
        wt.mkdir(parents=True)
        cfg = {
            "worktree_root": str(self.worktrees),
            "additional_worktree_roots": [],
            "repositories": {"demo": {"path": str(self.canonical)}},
        }
        with mock.patch.object(hwt, "HERDR_NATIVE_WORKTREES", native):
            name, _, canonical = hwt.repo_for_worktree(cfg, wt)
        self.assertEqual(name, "demo")
        self.assertEqual(canonical, self.canonical.resolve())

    def test_flat_ad_hoc_worktree_maps_via_git_common_dir(self):
        self._make_canonical_a_git_repo_with_commit(self.canonical)
        flat = self.root / "repos" / "demo-fix-pr9"
        hwt.git(self.canonical, "worktree", "add", "-q", str(flat), "-b", "fix-pr9")
        cfg = {
            "worktree_root": str(self.worktrees),
            "additional_worktree_roots": [],
            "repositories": {"demo": {"path": str(self.canonical)}},
        }
        name, _, canonical = hwt.repo_for_worktree(cfg, flat)
        self.assertEqual(name, "demo")
        self.assertEqual(canonical, self.canonical.resolve())

    def test_worktree_of_unconfigured_repository_is_rejected(self):
        other = self.root / "repos" / "other"
        other.mkdir()
        self._make_canonical_a_git_repo_with_commit(other)
        stray = self.root / "repos" / "other-fix"
        hwt.git(other, "worktree", "add", "-q", str(stray), "-b", "fix")
        cfg = {
            "worktree_root": str(self.worktrees),
            "additional_worktree_roots": [],
            "repositories": {"demo": {"path": str(self.canonical)}},
        }
        with self.assertRaises(hwt.SafetyError):
            hwt.repo_for_worktree(cfg, stray)

    def test_sibling_directory_of_unconfigured_repo_is_rejected(self):
        stray = self.root / "repos" / "other__worktrees" / "task"
        stray.mkdir(parents=True)
        cfg = {
            "worktree_root": str(self.worktrees),
            "additional_worktree_roots": [],
            "repositories": {"demo": {"path": str(self.canonical)}},
        }
        with self.assertRaises(hwt.SafetyError):
            hwt.repo_for_worktree(cfg, stray)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name).resolve()
        self.canonical = self.root / "repos" / "demo"
        self.worktrees = self.root / "repos" / ".worktrees"
        self.canonical.mkdir(parents=True)
        self.worktrees.mkdir()

    def _make_canonical_a_git_repo_with_commit(self, repo: Path) -> None:
        hwt.git(repo.parent, "init", "-q", str(repo))
        (repo / "f.txt").write_text("x")
        hwt.git(repo, "add", "f.txt")
        hwt.git(repo, "-c", "user.email=t@example.invalid", "-c", "user.name=T",
                "-c", "commit.gpgsign=false", "commit", "-q", "-m", "c")

    def tearDown(self):
        self.tmp.cleanup()

    def test_relative_path_rejects_absolute_and_traversal(self):
        for value in ("/etc/passwd", "../secret", "a/../../secret"):
            with self.subTest(value=value), self.assertRaises(hwt.SafetyError):
                hwt.safe_relative(value)

    @needs_symlinks
    def test_destination_rejects_symlink_escape(self):
        outside = self.root / "outside"
        outside.mkdir()
        wt = self.worktrees / "demo" / "task"
        wt.mkdir(parents=True)
        (wt / "escape").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(hwt.SafetyError):
            hwt.contained_destination(wt, "escape/secret")

    def test_repository_must_be_explicitly_configured(self):
        cfg = {"repositories": {"demo": {"path": str(self.canonical)}}}
        self.assertEqual(hwt.resolve_configured_repo(cfg, self.canonical)[0], "demo")
        with self.assertRaises(hwt.SafetyError):
            hwt.resolve_configured_repo(cfg, self.root / "repos" / "other")

    def test_worktree_is_not_misclassified_as_canonical_checkout(self):
        wt = self.worktrees / "demo" / "task"
        wt.mkdir(parents=True)
        cfg = {
            "canonical_root": str(self.root / "repos"),
            "worktree_root": str(self.worktrees),
            "repositories": {"demo": {"path": str(self.canonical)}},
        }
        with self.assertRaises(hwt.SafetyError):
            hwt.resolve_configured_repo(cfg, wt)


class PortLeaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_two_worktrees_receive_distinct_stable_ports_and_release(self):
        with mock.patch.object(hwt, "port_is_free", return_value=True):
            a1 = hwt.allocate_port(self.state, "/wt/a", 4100, 4102)
            b = hwt.allocate_port(self.state, "/wt/b", 4100, 4102)
            a2 = hwt.allocate_port(self.state, "/wt/a", 4100, 4102)
        self.assertNotEqual(a1, b)
        self.assertEqual(a1, a2)
        hwt.release_port(self.state, "/wt/a")
        leases = json.loads((self.state / "ports.json").read_text())
        self.assertNotIn(str(Path("/wt/a").resolve()), leases)
        self.assertIn(str(Path("/wt/b").resolve()), leases)

    def test_manual_port_collision_is_rejected(self):
        with mock.patch.object(hwt, "port_is_free", return_value=True):
            hwt.allocate_port(self.state, "/wt/a", 4100, 4102, requested=4101)
            with self.assertRaises(hwt.WorkflowError):
                hwt.allocate_port(self.state, "/wt/b", 4100, 4102, requested=4101)

    def test_plugin_specific_state_env_does_not_split_cli_leases(self):
        with mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": "/wrong/plugin/state"}, clear=False):
            self.assertEqual(hwt.runtime_state_dir(), hwt.DEFAULT_STATE)
        with mock.patch.dict(os.environ, {"HWT_STATE_DIR": str(self.state)}, clear=False):
            self.assertEqual(hwt.runtime_state_dir(), self.state)


class DependencyTests(unittest.TestCase):
    @needs_symlinks
    def test_matching_lockfile_replaces_stale_dependency_symlink(self):
        (self.canon / "yarn.lock").write_text("same")
        (self.wt / "yarn.lock").write_text("same")
        (self.wt / "node_modules").symlink_to(self.root / "moved-away")
        result = hwt.prepare_dependencies(self.canon, self.wt, {
            "policy": "shared-if-lockfile-matches", "lockfile": "yarn.lock",
            "directory": "node_modules", "install_command": ["false"]})
        self.assertEqual(result, "shared")
        self.assertEqual((self.wt / "node_modules").resolve(),
                         (self.canon / "node_modules").resolve())

    @needs_symlinks
    def test_canonical_dependency_symlink_cannot_escape_repository(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.canon / "node_modules").rmdir()
        (self.canon / "node_modules").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(hwt.SafetyError):
            hwt.prepare_dependencies(self.canon, self.wt, {
                "policy": "shared", "directory": "node_modules"})

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.canon = self.root / "canon"
        self.wt = self.root / "wt"
        self.canon.mkdir(); self.wt.mkdir()
        (self.canon / "node_modules").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    @needs_symlinks
    def test_matching_lockfile_shares_dependencies(self):
        (self.canon / "yarn.lock").write_text("same")
        (self.wt / "yarn.lock").write_text("same")
        result = hwt.prepare_dependencies(self.canon, self.wt, {
            "policy": "shared-if-lockfile-matches", "lockfile": "yarn.lock",
            "directory": "node_modules", "install_command": ["false"]})
        self.assertEqual(result, "shared")
        self.assertTrue((self.wt / "node_modules").is_symlink())
        self.assertEqual((self.wt / "node_modules").resolve(), (self.canon / "node_modules").resolve())

    def test_changed_lockfile_never_shares_and_installs_independently(self):
        (self.canon / "yarn.lock").write_text("one")
        (self.wt / "yarn.lock").write_text("two")
        marker = self.wt / "installed"
        result = hwt.prepare_dependencies(self.canon, self.wt, {
            "policy": "shared-if-lockfile-matches", "lockfile": "yarn.lock",
            "directory": "node_modules",
            "install_command": [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"]})
        self.assertEqual(result, "independent")
        self.assertFalse((self.wt / "node_modules").is_symlink())
        self.assertTrue(marker.exists())

    @needs_symlinks
    def test_install_is_never_run_through_dependency_symlink(self):
        (self.canon / "yarn.lock").write_text("one")
        (self.wt / "yarn.lock").write_text("two")
        (self.wt / "node_modules").symlink_to(self.canon / "node_modules")
        result = hwt.prepare_dependencies(self.canon, self.wt, {
            "policy": "shared-if-lockfile-matches", "lockfile": "yarn.lock",
            "directory": "node_modules", "install_command": [sys.executable, "-c", "pass"]})
        self.assertEqual(result, "independent")
        self.assertFalse((self.wt / "node_modules").is_symlink())

    def test_declared_mutable_cache_paths_are_worktree_local(self):
        hwt.prepare_local_cache_paths(self.wt, [".quasar", "dist/cache"])
        self.assertTrue((self.wt / ".quasar").is_dir())
        self.assertTrue((self.wt / "dist/cache").is_dir())


class FileOperationTests(unittest.TestCase):
    def test_create_rejects_existing_wrong_type(self):
        (self.wt / "cache").write_text("not a directory")
        with self.assertRaises(hwt.WorkflowError):
            hwt.apply_file_operations(self.canon, self.wt, [{
                "path": "cache", "action": "create", "kind": "directory"}])

    def test_copy_rejects_source_destination_type_mismatch_cleanly(self):
        (self.canon / "local").mkdir()
        (self.wt / "local").write_text("file")
        with self.assertRaises(hwt.WorkflowError):
            hwt.apply_file_operations(self.canon, self.wt, [{
                "path": "local", "action": "copy", "required": True}])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.canon = self.root / "canon"; self.wt = self.root / "wt"
        self.canon.mkdir(); self.wt.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    @posix_only
    def test_secret_copy_preserves_restrictive_mode_without_logging_content(self):
        src = self.canon / ".env.local"
        src.write_text("TOP_SECRET=never-log-this")
        src.chmod(0o600)
        messages = []
        hwt.apply_file_operations(self.canon, self.wt, [{
            "path": ".env.local", "action": "copy", "required": True,
            "overwrite": "never"}], logger=messages.append)
        dst = self.wt / ".env.local"
        self.assertEqual(dst.stat().st_mode & 0o777, 0o600)
        self.assertFalse(any("never-log-this" in m for m in messages))

    def test_existing_modified_copy_is_not_overwritten(self):
        (self.canon / "local.cfg").write_text("source")
        (self.wt / "local.cfg").write_text("changed")
        with self.assertRaises(hwt.WorkflowError):
            hwt.apply_file_operations(self.canon, self.wt, [{
                "path": "local.cfg", "action": "copy", "required": True,
                "overwrite": "if-identical"}])


class LayoutTests(unittest.TestCase):
    def test_event_bootstraps_worktree_in_additional_approved_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary = root / "primary"; native = root / "native"; wt = native / "demo" / "task"
            primary.mkdir(); wt.mkdir(parents=True)
            cfg = {
                "worktree_root": str(primary),
                "additional_worktree_roots": [str(native)],
            }
            args = object(); fake = mock.Mock()
            payload = json.dumps({"workspace_id": "w9", "worktree_path": str(wt)})
            with mock.patch.dict(os.environ, {
                "HERDR_PLUGIN_EVENT": "worktree.created",
                "HERDR_PLUGIN_EVENT_JSON": payload,
                "HERDR_WORKSPACE_ID": "",
            }, clear=False), mock.patch.object(hwt, "bootstrap") as bootstrap:
                hwt.cmd_event(args, cfg, root / "state", fake)
            bootstrap.assert_called_once_with(cfg, root / "state", wt, "w9", fake)

    def test_removed_event_releases_lease_in_additional_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); primary = root / "primary"; native = root / "native"
            primary.mkdir(); native.mkdir()
            wt = native / "demo" / "gone"
            cfg = {"worktree_root": str(primary), "additional_worktree_roots": [str(native)]}
            payload = json.dumps({"worktree_path": str(wt)})
            with mock.patch.dict(os.environ, {
                "HERDR_PLUGIN_EVENT": "worktree.removed",
                "HERDR_PLUGIN_EVENT_JSON": payload,
            }, clear=False), mock.patch.object(hwt, "release_port") as release:
                hwt.cmd_event(object(), cfg, root / "state", mock.Mock())
            release.assert_called_once_with(root / "state", str(wt))

    def test_bootstrap_failure_releases_new_port_lease(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            with mock.patch.object(hwt, "validate_worktree_identity", return_value=("demo", {}, Path("/canon"))), \
                 mock.patch.object(hwt, "apply_file_operations", side_effect=hwt.WorkflowError("boom")), \
                 mock.patch.object(hwt, "port_is_free", return_value=True):
                with self.assertRaises(hwt.WorkflowError):
                    hwt._bootstrap_locked({"ports": {"start": 4100, "end": 4100}}, state, Path("/wt"), "w1")
            leases = json.loads((state / "ports.json").read_text())
            self.assertEqual(leases, {})

    def test_workspace_discovery_compares_path_fields_exactly(self):
        fake = mock.Mock()
        fake.call.side_effect = [
            {"result": {"workspaces": [{"workspace_id": "w1"}, {"workspace_id": "w2"}]}},
            {"result": {"workspace": {"cwd": "/canon-other"}}},
            {"result": {"workspace": {"cwd": "/canon"}}},
        ]
        self.assertEqual(hwt.find_workspace_for_path(fake, Path("/canon")), "w2")

    def test_workspace_discovery_falls_back_to_exact_label(self):
        fake = mock.Mock()
        fake.call.side_effect = [
            {"result": {"workspaces": [{"workspace_id": "w1", "label": "demo"}]}},
            {"result": {"workspace": {"cwd": "/somewhere-else"}}},
        ]
        self.assertEqual(hwt.find_workspace_for_path(fake, Path("/canon"), "demo"), "w1")

    def test_workspace_discovery_without_label_does_not_guess(self):
        fake = mock.Mock()
        fake.call.side_effect = [
            {"result": {"workspaces": [{"workspace_id": "w1", "label": "demo"}]}},
            {"result": {"workspace": {"cwd": "/somewhere-else"}}},
        ]
        self.assertIsNone(hwt.find_workspace_for_path(fake, Path("/canon")))

    def test_workspace_discovery_accepts_herdr_checkout_path(self):
        fake = mock.Mock()
        fake.call.side_effect = [
            {"result": {"workspaces": [{"workspace_id": "w2"}]}},
            {"result": {"workspace": {"worktree": {
                "checkout_path": "/canon", "repo_root": "/canon"}}}},
        ]
        self.assertEqual(hwt.find_workspace_for_path(fake, Path("/canon")), "w2")

    def test_canonical_workspace_is_created_when_missing(self):
        fake = mock.Mock()
        fake.call.side_effect = [
            {"result": {"workspaces": []}},
            {"result": {"workspace": {"workspace_id": "w7"}}},
        ]
        with mock.patch.object(hwt, "ensure_canonical_layout") as layout:
            wid = hwt.ensure_canonical_workspace(fake, Path("/canon"), "demo", False)
        self.assertEqual(wid, "w7")
        fake.call.assert_any_call("workspace", "create", "--cwd", str(Path("/canon")), "--label", "demo", "--no-focus")
        layout.assert_called_once_with(fake, "w7", Path("/canon"), "demo", False, "hermes")

    def test_workspace_metadata_uses_live_cli_argument_order(self):
        api = hwt.Herdr("herdr")
        with mock.patch.object(hwt, "run") as command:
            api.metadata("w2", {"dev_port": "4180"})
        command.assert_called_once_with([
            "herdr", "workspace", "report-metadata", "w2", "--source",
            hwt.PLUGIN_ID, "--token", "dev_port=4180"])

    def test_server_reminder_writes_to_pane_tty_without_echoing_a_command(self):
        api = hwt.Herdr("herdr")
        process_info = mock.Mock(stdout=json.dumps(
            {"result": {"process_info": {"shell_pid": 4242}}}))
        with mock.patch.object(hwt, "run") as command, \
                mock.patch.object(hwt.os, "readlink", return_value="/dev/pts/7") as link, \
                mock.patch.object(hwt.os, "open", return_value=9) as opened, \
                mock.patch.object(hwt.os, "write") as wrote, \
                mock.patch.object(hwt.os, "close"):
            command.side_effect = [mock.Mock(), process_info, mock.Mock()]
            api.show_reminder("w2:p3", "To start the development server, run: hwt dev")
        link.assert_called_once_with("/proc/4242/fd/0")
        opened.assert_called_once_with("/dev/pts/7", os.O_WRONLY | os.O_NOCTTY)
        self.assertIn(b"To start the development server, run: hwt dev",
                      wrote.call_args.args[1])
        self.assertEqual(command.call_args_list, [
            mock.call([
                "herdr", "pane", "wait-output", "w2:p3", "--regex", "[$#%❯>] ?$",
                "--source", "visible", "--lines", "5", "--timeout", "10000", "--raw",
            ]),
            mock.call(["herdr", "pane", "process-info", "--pane", "w2:p3"]),
            mock.call(["herdr", "pane", "send-keys", "w2:p3", "enter"]),
        ])

    def test_server_reminder_falls_back_to_printf_when_tty_is_unavailable(self):
        api = hwt.Herdr("herdr")
        process_info = mock.Mock(stdout=json.dumps(
            {"result": {"process_info": {"shell_pid": 4242}}}))
        with mock.patch.object(hwt, "run") as command, \
                mock.patch.object(hwt.os, "readlink", side_effect=OSError):
            command.side_effect = [mock.Mock(), process_info, mock.Mock()]
            api.show_reminder("w2:p3", "To start the development server, run: hwt dev")
        self.assertEqual(command.call_args_list[-1], mock.call([
            "herdr", "pane", "run", "w2:p3",
            "printf '\\n%s\\n\\n' 'To start the development server, run: hwt dev'",
        ]))

    def test_layout_is_idempotent_and_has_exactly_three_tabs(self):
        fake = hwt.FakeHerdrForTests()
        fake.seed_workspace("w1", "/wt/demo", ["Tab 1"])
        hwt.ensure_layout(fake, "w1", Path("/wt/demo"), 4100, "0.0.0.0", "host.ts.net", True)
        hwt.ensure_layout(fake, "w1", Path("/wt/demo"), 4100, "0.0.0.0", "host.ts.net", True)
        self.assertEqual(fake.labels("w1"), ["agent", "shell", "server"])
        self.assertEqual(fake.agent_starts, 1)

    def test_new_worktree_server_tab_shows_dev_command_reminder_once(self):
        fake = hwt.FakeHerdrForTests()
        fake.seed_workspace("w1", "/wt/demo", ["Tab 1"])
        repo = {"commands": {"dev": ["npm", "run", "dev"]}}

        hwt.ensure_layout(fake, "w1", Path("/wt/demo"), 4100, "0.0.0.0", "host.ts.net", True, repo)
        hwt.ensure_layout(fake, "w1", Path("/wt/demo"), 4100, "0.0.0.0", "host.ts.net", True, repo)

        self.assertEqual(len(fake.reminders), 2)
        by_pane = dict(fake.reminders)
        self.assertIn("hwt -h", by_pane["w1:p2"])
        self.assertIn("hwt dev", by_pane["w1:p3"])
        self.assertIn("4100", by_pane["w1:p3"])
        self.assertIn("http://host.ts.net:4100", by_pane["w1:p3"])

    def test_repo_without_dev_command_points_at_init_instead_of_hwt_dev(self):
        fake = hwt.FakeHerdrForTests()
        fake.seed_workspace("w1", "/wt/demo", ["Tab 1"])
        hwt.ensure_layout(fake, "w1", Path("/wt/demo"), 4100, "127.0.0.1", "", True, {})
        by_pane = dict(fake.reminders)
        self.assertNotIn("hwt dev", by_pane["w1:p3"])
        self.assertIn("hwt init", by_pane["w1:p3"])

    def test_repository_environment_is_added_to_worktree_tabs(self):
        fake = mock.Mock()
        fake.tabs.return_value = [{"tab_id": "t1", "label": "agent", "pane_id": "p1"}]
        fake.has_agent.return_value = True
        fake.create_tab.side_effect = [("t2", "p2"), ("t3", "p3")]
        repo = {"environment": {"APP_ENV": "development"}}
        hwt.ensure_layout(fake, "w1", Path("/wt/demo"), 4100, "127.0.0.1", "", True, repo)
        environments = [call.args[3] for call in fake.create_tab.call_args_list]
        self.assertTrue(environments)
        self.assertTrue(all(env["APP_ENV"] == "development" for env in environments))

    def test_opening_existing_worktree_bootstraps_before_focus(self):
        cfg = {"worktree_root": "/approved", "repositories": {}}
        args = type("Args", (), {"target": "."})()
        fake = mock.Mock()
        with mock.patch.object(hwt, "resolve_existing", return_value=Path("/approved/demo")), \
             mock.patch.object(hwt, "resolve_configured_repo", side_effect=hwt.SafetyError("not canonical")), \
             mock.patch.object(hwt, "repo_for_worktree", return_value=("demo", {}, Path("/approved/canon"))), \
             mock.patch.object(hwt, "find_workspace_for_path", return_value="w9"), \
             mock.patch.object(hwt, "bootstrap", return_value={"workspace_id": "w9"}) as bootstrap:
            with contextlib.redirect_stdout(io.StringIO()):
                hwt.cmd_open(args, cfg, Path("/state"), fake)
        bootstrap.assert_called_once_with(cfg, Path("/state"), Path("/approved/demo"), "w9", fake)
        fake.call.assert_called_with("workspace", "focus", "w9")

    def test_existing_canonical_workspace_gets_idempotent_three_tab_layout(self):
        fake = hwt.FakeHerdrForTests()
        fake.seed_workspace("w1", "/canon", ["Tab 1"])
        hwt.ensure_canonical_layout(fake, "w1", Path("/canon"), "demo", True)
        hwt.ensure_canonical_layout(fake, "w1", Path("/canon"), "demo", True)
        self.assertEqual(fake.labels("w1"), ["agent", "shell", "server"])
        self.assertEqual(fake.agent_starts, 1)


class RemovalTests(unittest.TestCase):
    CFG = {"repositories": {"demo": {
        "path": "/canon/demo", "mode": "worktree", "remote": "origin",
        "base_branch": "origin/main",
    }}}
    ITEM = {
        "branch": "feature/x", "is_linked_worktree": True,
        "open_workspace_id": "w2", "path": "/approved/wt",
        "repository": "demo",
    }

    @staticmethod
    def cleanup_args(abandon=False, confirm=None):
        return type("Args", (), {"target": "feature/x", "abandon": abandon, "confirm": confirm})()

    def test_abandon_requires_exact_confirmation_before_git_mutation(self):
        args = self.cleanup_args(abandon=True, confirm="wrong")
        fake = mock.Mock()
        with mock.patch.object(hwt, "configured_worktree_items", return_value=[dict(self.ITEM)]), \
             mock.patch.object(hwt, "git") as git:
            with self.assertRaisesRegex(hwt.WorkflowError, "--abandon --confirm feature/x"):
                hwt.cmd_cleanup(args, self.CFG, Path("/state"), fake)
        git.assert_not_called()
        fake.call.assert_not_called()

    def test_cleanup_dirty_worktree_requires_abandon(self):
        args = self.cleanup_args()
        fake = mock.Mock()
        with mock.patch.object(hwt, "configured_worktree_items", return_value=[dict(self.ITEM)]), \
             mock.patch.object(hwt, "resolve_existing", side_effect=lambda path: Path(path)), \
             mock.patch.object(hwt, "validate_worktree_identity"), \
             mock.patch.object(hwt, "worktree_lock", return_value=contextlib.nullcontext()), \
             mock.patch.object(hwt, "git", side_effect=[
                 mock.Mock(stdout=" M app.py\n", returncode=0),
             ]) as git:
            with self.assertRaisesRegex(hwt.WorkflowError, "uncommitted changes.*--abandon --confirm feature/x"):
                hwt.cmd_cleanup(args, self.CFG, Path("/state"), fake)
        self.assertEqual(len(git.call_args_list), 1)
        fake.call.assert_not_called()

    def test_abandon_deletes_unmerged_dirty_worktree_and_branches(self):
        args = self.cleanup_args(abandon=True, confirm="feature/x")
        fake = mock.Mock()
        responses = [
            mock.Mock(stdout=" M app.py\n", returncode=0),   # status: dirty
            mock.Mock(stdout="", returncode=0),               # fetch
            mock.Mock(stdout="", returncode=1),               # merge-base: NOT merged
            mock.Mock(stdout="", returncode=0),               # show-ref: remote exists
            mock.Mock(stdout="", returncode=0),               # push --delete
            mock.Mock(stdout="", returncode=0),               # update-ref -d
        ]
        with mock.patch.object(hwt, "configured_worktree_items", return_value=[dict(self.ITEM)]), \
             mock.patch.object(hwt, "resolve_existing", side_effect=lambda path: Path(path)), \
             mock.patch.object(hwt, "validate_worktree_identity"), \
             mock.patch.object(hwt, "worktree_lock", return_value=contextlib.nullcontext()), \
             mock.patch.object(hwt, "git", side_effect=responses) as git, \
             mock.patch.object(hwt, "release_port") as release, \
             mock.patch.object(hwt, "suppress_teardown"), \
             contextlib.redirect_stdout(io.StringIO()):
            hwt.cmd_cleanup(args, self.CFG, Path("/state"), fake)
        self.assertIn(mock.call(Path("/canon/demo"), "push", "origin", "--delete", "feature/x"), git.call_args_list)
        self.assertIn(mock.call("worktree", "remove", "--workspace", "w2", "--force"), fake.call.call_args_list)
        self.assertIn(mock.call(Path("/canon/demo"), "update-ref", "-d", "refs/heads/feature/x"), git.call_args_list)
        release.assert_called_once_with(Path("/state"), str(Path("/approved/wt")))

    def test_cleanup_fetch_failure_leaves_checkout_and_branches_untouched(self):
        args = self.cleanup_args()
        fake = mock.Mock()
        with mock.patch.object(hwt, "configured_worktree_items", return_value=[dict(self.ITEM)]), \
             mock.patch.object(hwt, "resolve_existing", side_effect=lambda path: Path(path)), \
             mock.patch.object(hwt, "validate_worktree_identity"), \
             mock.patch.object(hwt, "worktree_lock", return_value=contextlib.nullcontext()), \
             mock.patch.object(hwt, "git", side_effect=[
                 mock.Mock(stdout="", returncode=0),
                 hwt.WorkflowError("fetch failed"),
             ]) as git:
            with self.assertRaisesRegex(hwt.WorkflowError, "fetch failed"):
                hwt.cmd_cleanup(args, self.CFG, Path("/state"), fake)
        self.assertEqual(git.call_args_list, [
            mock.call(Path("/approved/wt"), "status", "--porcelain"),
            mock.call(Path("/canon/demo"), "fetch", "origin", "--prune"),
        ])
        fake.call.assert_not_called()

    def test_cleanup_refuses_unmerged_branch_with_unique_commits(self):
        args = self.cleanup_args()
        fake = mock.Mock()
        responses = [
            mock.Mock(stdout="", returncode=0),           # status: clean
            mock.Mock(stdout="", returncode=0),           # fetch
            mock.Mock(stdout="", returncode=1),           # merge-base: not merged
            mock.Mock(stdout="abc123\n", returncode=0),   # rev-list: unique commit exists
        ]
        with mock.patch.object(hwt, "configured_worktree_items", return_value=[dict(self.ITEM)]), \
             mock.patch.object(hwt, "resolve_existing", side_effect=lambda path: Path(path)), \
             mock.patch.object(hwt, "validate_worktree_identity"), \
             mock.patch.object(hwt, "worktree_lock", return_value=contextlib.nullcontext()), \
             mock.patch.object(hwt, "git", side_effect=responses) as git:
            with self.assertRaisesRegex(hwt.WorkflowError, "not merged into origin/main.*--abandon --confirm feature/x"):
                hwt.cmd_cleanup(args, self.CFG, Path("/state"), fake)
        self.assertEqual(git.call_args_list[-1], mock.call(
            Path("/canon/demo"), "rev-list", "-n", "1", "refs/heads/feature/x", "--not",
            "--exclude=refs/heads/feature/x", "--exclude=refs/remotes/origin/feature/x",
            "--all", check=False))
        fake.call.assert_not_called()

    def test_cleanup_of_unmerged_branch_without_unique_commits_needs_no_confirmation(self):
        args = self.cleanup_args()
        fake = mock.Mock()
        responses = [
            mock.Mock(stdout="", returncode=0),   # status: clean
            mock.Mock(stdout="", returncode=0),   # fetch
            mock.Mock(stdout="", returncode=1),   # merge-base: not merged
            mock.Mock(stdout="", returncode=0),   # rev-list: nothing unique
            mock.Mock(stdout="", returncode=0),   # show-ref: remote exists
            mock.Mock(stdout="", returncode=0),   # push --delete
            mock.Mock(stdout="", returncode=0),   # update-ref -d
        ]
        with mock.patch.object(hwt, "configured_worktree_items", return_value=[dict(self.ITEM)]), \
             mock.patch.object(hwt, "resolve_existing", side_effect=lambda path: Path(path)), \
             mock.patch.object(hwt, "validate_worktree_identity"), \
             mock.patch.object(hwt, "worktree_lock", return_value=contextlib.nullcontext()), \
             mock.patch.object(hwt, "git", side_effect=responses) as git, \
             mock.patch.object(hwt, "release_port") as release, \
             mock.patch.object(hwt, "suppress_teardown"), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            hwt.cmd_cleanup(args, self.CFG, Path("/state"), fake)
        self.assertIn(mock.call("worktree", "remove", "--workspace", "w2"), fake.call.call_args_list)
        self.assertIn(mock.call(Path("/canon/demo"), "update-ref", "-d", "refs/heads/feature/x"), git.call_args_list)
        release.assert_called_once_with(Path("/state"), str(Path("/approved/wt")))
        self.assertEqual(json.loads(out.getvalue())["reason"], "no-unique-commits")

    def test_cleanup_of_merged_clean_branch_needs_no_confirmation(self):
        args = self.cleanup_args()
        fake = mock.Mock()
        responses = [
            mock.Mock(stdout="", returncode=0),
            mock.Mock(stdout="", returncode=0),
            mock.Mock(stdout="", returncode=0),
            mock.Mock(stdout="", returncode=0),
            mock.Mock(stdout="", returncode=0),
            mock.Mock(stdout="", returncode=0),
        ]
        with mock.patch.object(hwt, "configured_worktree_items", return_value=[dict(self.ITEM)]), \
             mock.patch.object(hwt, "resolve_existing", side_effect=lambda path: Path(path)), \
             mock.patch.object(hwt, "validate_worktree_identity"), \
             mock.patch.object(hwt, "worktree_lock", return_value=contextlib.nullcontext()), \
             mock.patch.object(hwt, "git", side_effect=responses) as git, \
             mock.patch.object(hwt, "release_port") as release, \
             mock.patch.object(hwt, "suppress_teardown"), \
             contextlib.redirect_stdout(io.StringIO()):
            hwt.cmd_cleanup(args, self.CFG, Path("/state"), fake)
        self.assertIn(mock.call(Path("/canon/demo"), "push", "origin", "--delete", "feature/x"), git.call_args_list)
        self.assertIn(mock.call("worktree", "remove", "--workspace", "w2"), fake.call.call_args_list)
        self.assertIn(mock.call(Path("/canon/demo"), "update-ref", "-d", "refs/heads/feature/x"), git.call_args_list)
        release.assert_called_once_with(Path("/state"), str(Path("/approved/wt")))

    def _refocus_run(self, focused_id):
        args = self.cleanup_args()
        fake = mock.Mock()

        def herdr_call(*call_args):
            if call_args[:2] == ("workspace", "list"):
                return {"result": {"workspaces": [
                    {"workspace_id": focused_id, "focused": True},
                    {"workspace_id": "w2"},
                ]}}
            return {"result": {}}

        fake.call.side_effect = herdr_call
        responses = [mock.Mock(stdout="", returncode=0)] * 6
        with mock.patch.object(hwt, "configured_worktree_items", return_value=[dict(self.ITEM)]), \
             mock.patch.object(hwt, "resolve_existing", side_effect=lambda path: Path(path)), \
             mock.patch.object(hwt, "validate_worktree_identity"), \
             mock.patch.object(hwt, "worktree_lock", return_value=contextlib.nullcontext()), \
             mock.patch.object(hwt, "git", side_effect=responses), \
             mock.patch.object(hwt, "release_port"), \
             mock.patch.object(hwt, "suppress_teardown"), \
             contextlib.redirect_stdout(io.StringIO()):
            hwt.cmd_cleanup(args, self.CFG, Path("/state"), fake)
        return fake.call.call_args_list

    def test_cleanup_restores_focus_to_where_it_was_run_from(self):
        calls = self._refocus_run("w-home")
        remove_index = calls.index(mock.call("worktree", "remove", "--workspace", "w2"))
        focus_index = calls.index(mock.call("workspace", "focus", "w-home"))
        self.assertLess(remove_index, focus_index)

    def test_no_refocus_when_the_removed_workspace_was_focused(self):
        calls = self._refocus_run("w2")
        self.assertFalse([c for c in calls if c.args[:2] == ("workspace", "focus")])

    def test_git_identity_rejects_unrelated_checkout(self):
        cfg = {"worktree_root": "/approved"}
        with mock.patch.object(hwt, "repo_for_worktree", return_value=("demo", {}, Path("/canon"))), \
             mock.patch.object(hwt, "git") as git:
            git.side_effect = [
                mock.Mock(stdout="/approved/demo/wt\n"),
                mock.Mock(stdout="/unrelated/.git\n"),
                mock.Mock(stdout="/canon/.git\n"),
            ]
            with self.assertRaises(hwt.SafetyError):
                hwt.validate_worktree_identity(cfg, Path("/approved/demo/wt"))

    def test_worktree_listing_queries_configured_canonical_path(self):
        cfg = {"repositories": {
            "demo": {"path": "/canon/demo", "mode": "worktree"},
            "docs": {"path": "/canon/docs", "mode": "open-only"},
        }}
        fake = mock.Mock()
        fake.call.return_value = {"result": {"worktrees": [{"branch": "feature/x"}]}}
        items = hwt.configured_worktree_items(cfg, fake)
        self.assertEqual(items, [{"branch": "feature/x", "repository": "demo"}])
        fake.call.assert_called_once_with("worktree", "list", "--cwd", "/canon/demo")

    def test_duplicate_worktree_entries_across_repo_aliases_are_deduped(self):
        cfg = {"repositories": {
            "demo": {"path": "/canon/demo", "mode": "worktree"},
            "demo--main": {"path": "/canon/demo--main", "mode": "worktree"},
        }}
        fake = mock.Mock()
        fake.call.return_value = {"result": {"worktrees": [{
            "branch": "feature/x", "path": "/wts/demo/feature-x",
            "is_linked_worktree": True}]}}
        items = hwt.configured_worktree_items(cfg, fake)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["repository"], "demo")

    def test_live_worktree_item_resolves_open_workspace_id(self):
        item = {"open_workspace_id": "w3", "path": "/approved/wt"}
        self.assertEqual(hwt.worktree_item_ids(item), ("w3", Path("/approved/wt")))

    def test_closed_worktree_item_has_no_workspace_but_keeps_path(self):
        self.assertEqual(hwt.worktree_item_ids({"path": "/approved/wt"}),
                         (None, Path("/approved/wt")))
        with self.assertRaisesRegex(hwt.WorkflowError, "checkout path"):
            hwt.worktree_item_ids({"branch": "feature/x"})

    def test_cleanup_of_closed_worktree_removes_checkout_with_git(self):
        item = {k: v for k, v in self.ITEM.items() if k != "open_workspace_id"}
        args = self.cleanup_args()
        fake = mock.Mock()
        responses = [
            mock.Mock(stdout="", returncode=0),   # status: clean
            mock.Mock(stdout="", returncode=0),   # fetch
            mock.Mock(stdout="", returncode=0),   # merge-base: merged
            mock.Mock(stdout="", returncode=0),   # show-ref: remote exists
            mock.Mock(stdout="", returncode=0),   # push --delete
            mock.Mock(stdout="", returncode=0),   # git worktree remove
            mock.Mock(stdout="", returncode=0),   # update-ref -d
        ]
        with mock.patch.object(hwt, "configured_worktree_items", return_value=[item]), \
             mock.patch.object(hwt, "resolve_existing", side_effect=lambda path: Path(path)), \
             mock.patch.object(hwt, "validate_worktree_identity"), \
             mock.patch.object(hwt, "worktree_lock", return_value=contextlib.nullcontext()), \
             mock.patch.object(hwt, "git", side_effect=responses) as git, \
             mock.patch.object(hwt, "release_port") as release, \
             mock.patch.object(hwt, "suppress_teardown"), \
             contextlib.redirect_stdout(io.StringIO()):
            hwt.cmd_cleanup(args, self.CFG, Path("/state"), fake)
        fake.call.assert_not_called()
        self.assertIn(mock.call(Path("/canon/demo"), "worktree", "remove", str(Path("/approved/wt"))),
                      git.call_args_list)
        self.assertIn(mock.call(Path("/canon/demo"), "update-ref", "-d", "refs/heads/feature/x"),
                      git.call_args_list)
        release.assert_called_once_with(Path("/state"), str(Path("/approved/wt")))

    def test_removal_revalidates_configured_worktree_before_git_or_herdr(self):
        cfg = {"repositories": {
            "demo": {"path": "/canon/demo", "mode": "worktree"}},
            "worktree_root": "/approved"}
        args = type("Args", (), {"target": "feature/x", "force": False, "confirm": None})()
        fake = mock.Mock()
        fake.call.return_value = {"result": {"worktrees": [{
            "branch": "feature/x", "is_linked_worktree": True,
            "open_workspace_id": "w2", "path": "/outside/wt"}]}}
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(hwt, "validate_worktree_identity", side_effect=hwt.SafetyError("outside")) as validate:
            with self.assertRaises(hwt.SafetyError):
                hwt.cmd_remove(args, cfg, Path(tmp), fake)
        validate.assert_called_once_with(cfg, Path("/outside/wt"))

    def test_removal_target_matching_is_exact_and_linked_only(self):
        items = [
            {"branch": "main", "is_linked_worktree": False, "open_workspace_id": "w1", "path": "/repo"},
            {"branch": "feature/alpha", "is_linked_worktree": True, "open_workspace_id": "w2", "path": "/wts/feature-alpha"},
            {"branch": "feature/alphabet", "is_linked_worktree": True, "open_workspace_id": "w3", "path": "/wts/feature-alphabet"},
        ]
        self.assertEqual(hwt.match_removal_items(items, "feature/alpha"), [items[1]])
        self.assertEqual(hwt.match_removal_items(items, "alpha"), [])
        self.assertEqual(hwt.match_removal_items(items, "main"), [])

    def test_dirty_removal_requires_force_and_exact_confirmation(self):
        with self.assertRaises(hwt.WorkflowError):
            hwt.check_remove_allowed(True, False, None, "feature/x")
        with self.assertRaises(hwt.WorkflowError):
            hwt.check_remove_allowed(True, True, "wrong", "feature/x")
        hwt.check_remove_allowed(True, True, "feature/x", "feature/x")


class WorktreeItemNormalizationTests(unittest.TestCase):
    def test_nested_worktree_fields_and_checkout_path_are_flattened(self):
        raw = {"workspace_id": "w3", "worktree": {
            "checkout_path": "/somewhere/wt", "is_linked_worktree": True}}
        item = hwt.normalize_worktree_item(raw, "demo")
        self.assertEqual(item["path"], "/somewhere/wt")
        self.assertTrue(item["is_linked_worktree"])
        self.assertEqual(item["repository"], "demo")

    def test_missing_branch_and_linkedness_are_derived_from_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            wt = Path(tmp)
            responses = {
                ("branch", "--show-current"): mock.Mock(stdout="worktree/quiet-harbor-af65\n", returncode=0),
                ("rev-parse", "--path-format=absolute", "--git-common-dir"): mock.Mock(stdout="/repo/.git\n", returncode=0),
                ("rev-parse", "--path-format=absolute", "--git-dir"): mock.Mock(stdout="/repo/.git/worktrees/x\n", returncode=0),
            }
            with mock.patch.object(hwt, "git", side_effect=lambda cwd, *a, **k: responses[a]):
                item = hwt.normalize_worktree_item({"checkout_path": str(wt)}, "demo")
        self.assertEqual(item["branch"], "worktree/quiet-harbor-af65")
        self.assertTrue(item["is_linked_worktree"])


class CleanupSelectionTests(unittest.TestCase):
    ITEMS = [
        {"branch": "worktree/quiet-harbor-af65", "is_linked_worktree": True,
         "path": "/wts/katapult-lidar/worktree-quiet-harbor", "repository": "katapult-lidar",
         "open_workspace_id": "w5"},
        {"branch": "feature/other", "is_linked_worktree": True,
         "path": "/wts/katapult-lidar/feature-other", "repository": "katapult-lidar",
         "open_workspace_id": "w6"},
        {"branch": "develop", "is_linked_worktree": False, "path": "/repo", "repository": "katapult-lidar"},
    ]

    def test_exact_match_needs_no_interaction(self):
        item = hwt.choose_worktree_item({}, [dict(x) for x in self.ITEMS], "worktree/quiet-harbor-af65")
        self.assertEqual(item["open_workspace_id"], "w5")

    def test_partial_target_finds_similar_worktrees(self):
        matches = hwt.fuzzy_worktree_matches([dict(x) for x in self.ITEMS[:2]], "quiet-harbor-af65")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["branch"], "worktree/quiet-harbor-af65")
        matches = hwt.fuzzy_worktree_matches([dict(x) for x in self.ITEMS[:2]], "worktree-quiet-harbor-af65")
        self.assertEqual(len(matches), 1)

    def test_no_target_with_tty_offers_numbered_choice(self):
        with mock.patch.object(hwt.sys, "stdin", mock.Mock(isatty=lambda: True)), \
             mock.patch.object(hwt, "log"), \
             mock.patch("builtins.input", return_value="2"):
            item = hwt.choose_worktree_item({}, [dict(x) for x in self.ITEMS], None)
        self.assertEqual(item["branch"], "feature/other")

    def test_without_tty_lists_candidates_and_refuses(self):
        logged = []
        with mock.patch.object(hwt.sys, "stdin", mock.Mock(isatty=lambda: False)), \
             mock.patch.object(hwt, "log", side_effect=logged.append):
            with self.assertRaisesRegex(hwt.WorkflowError, "re-run with one of the listed"):
                hwt.choose_worktree_item({}, [dict(x) for x in self.ITEMS], "quiet-harbor")
        self.assertTrue(any("worktree/quiet-harbor-af65" in line for line in logged))

    def test_unlinked_worktrees_are_never_offered(self):
        with mock.patch.object(hwt.sys, "stdin", mock.Mock(isatty=lambda: False)), \
             mock.patch.object(hwt, "log"):
            with self.assertRaises(hwt.WorkflowError):
                hwt.choose_worktree_item({}, [dict(self.ITEMS[2])], None)


class WorktreeSafetyTests(unittest.TestCase):
    CFG = {"repositories": {"demo": {
        "path": "/canon/demo", "remote": "origin", "base_branch": "origin/main"}}}
    ITEM = {"branch": "feature/x", "path": "/nonexistent-wt", "repository": "demo"}

    def test_detached_worktree(self):
        self.assertEqual(hwt.worktree_safety(self.CFG, {"path": "/wt"}), "detached")

    def test_dirty_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = {**self.ITEM, "path": tmp}
            with mock.patch.object(hwt, "git", return_value=mock.Mock(stdout=" M x\n", returncode=0)):
                self.assertEqual(hwt.worktree_safety(self.CFG, item), "dirty")

    def test_merged_branch(self):
        with mock.patch.object(hwt, "git", return_value=mock.Mock(stdout="", returncode=0)):
            self.assertEqual(hwt.worktree_safety(self.CFG, dict(self.ITEM)), "merged")

    def test_unpublished_work(self):
        responses = [mock.Mock(returncode=1, stdout=""), mock.Mock(returncode=0, stdout="abc\n")]
        with mock.patch.object(hwt, "git", side_effect=responses):
            self.assertEqual(hwt.worktree_safety(self.CFG, dict(self.ITEM)), "unpublished-work")

    def test_no_unique_commits(self):
        responses = [mock.Mock(returncode=1, stdout=""), mock.Mock(returncode=0, stdout="")]
        with mock.patch.object(hwt, "git", side_effect=responses):
            self.assertEqual(hwt.worktree_safety(self.CFG, dict(self.ITEM)), "no-unique-commits")


class TeardownSuppressionTests(unittest.TestCase):
    def test_marker_is_consumed_by_first_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            hwt.suppress_teardown(state, "/wt/x")
            self.assertTrue(hwt.teardown_suppressed(state, "/wt/x"))
            self.assertFalse(hwt.teardown_suppressed(state, "/wt/x"))

    def test_stale_marker_does_not_suppress(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            hwt.suppress_teardown(state, "/wt/x")
            later = time.time() + hwt.TEARDOWN_SUPPRESS_TTL + 1
            with mock.patch.object(hwt.time, "time", return_value=later):
                self.assertFalse(hwt.teardown_suppressed(state, "/wt/x"))

    def test_cleanup_suppresses_teardown_for_its_own_removal(self):
        args = type("Args", (), {"target": "feature/x", "abandon": False, "confirm": None})()
        fake = mock.Mock()
        responses = [
            mock.Mock(stdout="", returncode=0),   # status: clean
            mock.Mock(stdout="", returncode=0),   # fetch
            mock.Mock(stdout="", returncode=0),   # merge-base: merged
            mock.Mock(stdout="", returncode=1),   # show-ref: no remote branch
            mock.Mock(stdout="", returncode=0),   # update-ref -d
        ]
        with mock.patch.object(hwt, "configured_worktree_items", return_value=[dict(RemovalTests.ITEM)]), \
             mock.patch.object(hwt, "resolve_existing", side_effect=lambda path: Path(path)), \
             mock.patch.object(hwt, "validate_worktree_identity"), \
             mock.patch.object(hwt, "worktree_lock", return_value=contextlib.nullcontext()), \
             mock.patch.object(hwt, "git", side_effect=responses), \
             mock.patch.object(hwt, "release_port"), \
             mock.patch.object(hwt, "suppress_teardown") as marked, \
             contextlib.redirect_stdout(io.StringIO()):
            hwt.cmd_cleanup(args, RemovalTests.CFG, Path("/state"), fake)
        marked.assert_called_once_with(Path("/state"), str(Path("/approved/wt")))


class TeardownEventTests(unittest.TestCase):
    CFG = {"repositories": {"demo": {
        "path": "/canon/demo", "remote": "origin", "base_branch": "origin/main"}}}
    PAYLOAD = {"event": "worktree.removed",
               "data": {"type": "worktree_removed", "workspace_id": "w2", "forced": False,
                        "worktree": {"path": "/approved/demo/wt", "branch": "feature/x",
                                     "is_linked_worktree": True, "label": "feature/x",
                                     "is_bare": False, "is_detached": False, "is_prunable": False}}}

    def run_maybe(self, payload, *, suppressed=False, resolved=True, branch_exists=True):
        repo = self.CFG["repositories"]["demo"]
        found = ("demo", repo, Path("/canon/demo")) if resolved else None
        with mock.patch.object(hwt, "teardown_suppressed", return_value=suppressed), \
             mock.patch.object(hwt, "repo_for_removed_worktree", return_value=found), \
             mock.patch.object(Path, "is_dir", return_value=True), \
             mock.patch.object(hwt, "git", return_value=mock.Mock(returncode=0 if branch_exists else 1)), \
             mock.patch.object(hwt, "run") as runner:
            hwt.maybe_open_teardown(self.CFG, Path("/state"), payload)
        return runner

    def test_ui_removal_opens_teardown_popup_with_branch_context(self):
        runner = self.run_maybe(self.PAYLOAD)
        runner.assert_called_once()
        argv = runner.call_args.args[0]
        self.assertEqual(argv[1:5], ["plugin", "pane", "open", "--plugin"])
        self.assertIn("teardown", argv)
        self.assertIn("HWT_TEARDOWN_BRANCH=feature/x", argv)
        self.assertIn("HWT_TEARDOWN_REPO=demo", argv)
        self.assertIn("HWT_TEARDOWN_FORCED=0", argv)

    def test_forced_removal_flag_reaches_the_dialog(self):
        payload = json.loads(json.dumps(self.PAYLOAD))
        payload["data"]["forced"] = True
        self.assertIn("HWT_TEARDOWN_FORCED=1", self.run_maybe(payload).call_args.args[0])

    def test_corral_initiated_removal_is_suppressed(self):
        self.run_maybe(self.PAYLOAD, suppressed=True).assert_not_called()

    def test_detached_checkout_has_no_branch_to_tear_down(self):
        payload = json.loads(json.dumps(self.PAYLOAD))
        payload["data"]["worktree"].pop("branch")
        self.run_maybe(payload).assert_not_called()

    def test_canonical_checkout_removal_is_ignored(self):
        payload = json.loads(json.dumps(self.PAYLOAD))
        payload["data"]["worktree"]["is_linked_worktree"] = False
        self.run_maybe(payload).assert_not_called()

    def test_unrecognized_path_is_ignored(self):
        self.run_maybe(self.PAYLOAD, resolved=False).assert_not_called()

    def test_already_deleted_branch_is_ignored(self):
        self.run_maybe(self.PAYLOAD, branch_exists=False).assert_not_called()


class TeardownStatusTests(unittest.TestCase):
    REPO = {"path": "/canon/demo", "remote": "origin", "base_branch": "origin/main"}

    def status_with(self, responses):
        with mock.patch.object(hwt, "git", side_effect=responses):
            return hwt.teardown_status(self.REPO, Path("/canon/demo"), "feature/x")

    def test_merged_branch_whose_remote_was_deleted_after_pr_merge(self):
        status = self.status_with([
            mock.Mock(returncode=0),                 # show-ref tracking: existed
            mock.Mock(returncode=0),                 # fetch --prune
            mock.Mock(returncode=1),                 # show-ref tracking: pruned away
            mock.Mock(returncode=0),                 # merge-base: merged
            mock.Mock(returncode=0, stdout=""),      # rev-list: nothing unique
        ])
        self.assertEqual(status["remote_state"], "deleted")
        self.assertTrue(status["merged"])
        self.assertFalse(status["unique_commits"])

    def test_unpushed_unmerged_branch_with_unique_commits(self):
        status = self.status_with([
            mock.Mock(returncode=1),                 # show-ref tracking: never existed
            mock.Mock(returncode=0),                 # fetch --prune
            mock.Mock(returncode=1),                 # show-ref tracking: still absent
            mock.Mock(returncode=1),                 # merge-base: not merged
            mock.Mock(returncode=0, stdout="abc\n"), # rev-list: unique commit
        ])
        self.assertEqual(status["remote_state"], "never-pushed")
        self.assertFalse(status["merged"])
        self.assertTrue(status["unique_commits"])

    def test_offline_falls_back_to_local_tracking_ref_state(self):
        status = self.status_with([
            mock.Mock(returncode=0),                 # show-ref tracking: existed
            mock.Mock(returncode=1),                 # fetch --prune: offline
            mock.Mock(returncode=0),                 # merge-base: merged locally
            mock.Mock(returncode=0, stdout=""),      # rev-list
        ])
        self.assertEqual(status["remote_state"], "unknown")
        self.assertFalse(status["fetched"])
        self.assertTrue(status["merged"])

    def test_surviving_remote_branch_is_reported_as_existing(self):
        status = self.status_with([
            mock.Mock(returncode=0),                 # show-ref tracking: existed
            mock.Mock(returncode=0),                 # fetch --prune
            mock.Mock(returncode=0),                 # show-ref tracking: still there
            mock.Mock(returncode=1),                 # merge-base: not merged
            mock.Mock(returncode=0, stdout="abc\n"), # rev-list: unique commit
        ])
        self.assertEqual(status["remote_state"], "exists")
        self.assertTrue(status["unique_commits"])


class TeardownDialogTests(unittest.TestCase):
    CFG = {"repositories": {"demo": {
        "path": "/canon/demo", "remote": "origin", "base_branch": "origin/main"}}}
    ENV = {"HWT_TEARDOWN_REPO": "demo", "HWT_TEARDOWN_BRANCH": "feature/x",
           "HWT_TEARDOWN_PATH": "/approved/demo/wt", "HWT_TEARDOWN_FORCED": "0"}

    @staticmethod
    def status(**over):
        base = {"remote": "origin", "base_branch": "origin/main", "merged": True,
                "unique_commits": False, "remote_state": "deleted", "fetched": True}
        base.update(over)
        return base

    def run_dialog(self, status, answers, typed=None, env=None):
        args = type("Args", (), {})()
        with mock.patch.dict(os.environ, env or self.ENV), \
             mock.patch.object(hwt.sys.stdin, "isatty", return_value=True), \
             mock.patch.object(hwt, "resolve_existing", side_effect=lambda p: Path(p)), \
             mock.patch.object(hwt, "teardown_status", return_value=status), \
             mock.patch.object(hwt, "ask_yes_no", side_effect=answers) as asked, \
             mock.patch("builtins.input", side_effect=(typed or []) + [""]), \
             mock.patch.object(hwt, "git") as git, \
             contextlib.redirect_stdout(io.StringIO()):
            hwt.cmd_teardown(args, self.CFG, Path("/state"))
        return asked, git

    def test_merged_branch_with_deleted_remote_deletes_local_only(self):
        asked, git = self.run_dialog(self.status(), [True])
        git.assert_called_once_with(Path("/canon/demo"), "update-ref", "-d", "refs/heads/feature/x")
        self.assertTrue(asked.call_args_list[0].kwargs["default"])

    def test_surviving_remote_is_offered_and_deleted_after_local(self):
        asked, git = self.run_dialog(self.status(remote_state="exists"), [True, True])
        self.assertIn(mock.call(Path("/canon/demo"), "update-ref", "-d", "refs/heads/feature/x"),
                      git.call_args_list)
        self.assertIn(mock.call(Path("/canon/demo"), "push", "origin", "--delete", "feature/x"),
                      git.call_args_list)
        self.assertTrue(asked.call_args_list[1].kwargs["default"])

    def test_declining_the_remote_keeps_it(self):
        _, git = self.run_dialog(self.status(remote_state="exists"), [True, False])
        self.assertNotIn(mock.call(Path("/canon/demo"), "push", "origin", "--delete", "feature/x"),
                         git.call_args_list)

    def test_declining_local_deletion_never_asks_about_the_remote(self):
        asked, git = self.run_dialog(self.status(remote_state="exists"), [False])
        git.assert_not_called()
        self.assertEqual(asked.call_count, 1)

    def test_unique_commits_default_to_keep_and_require_typed_confirmation(self):
        risky = self.status(merged=False, unique_commits=True)
        asked, git = self.run_dialog(risky, [True], typed=["not-the-branch"])
        self.assertFalse(asked.call_args_list[0].kwargs["default"])
        git.assert_not_called()
        _, git = self.run_dialog(risky, [True], typed=["feature/x"])
        git.assert_called_once_with(Path("/canon/demo"), "update-ref", "-d", "refs/heads/feature/x")

    def test_protected_branch_is_refused(self):
        env = {**self.ENV, "HWT_TEARDOWN_BRANCH": "main"}
        with self.assertRaises(hwt.SafetyError):
            self.run_dialog(self.status(), [], env=env)


class PaletteLayoutTests(unittest.TestCase):
    def test_worktrees_group_under_repo_headers_with_indented_paths(self):
        items = [
            {"branch": "hermes/fix-a", "path": str(Path.home() / "GitRepos" / "wt-a"),
             "repository": "flashcards", "is_linked_worktree": True},
            {"branch": "hermes/fix-b", "path": "/elsewhere/wt-b",
             "repository": "flashcards", "is_linked_worktree": True},
            {"branch": "hermes/lidar-x", "path": "/elsewhere/wt-x",
             "repository": "lidar", "is_linked_worktree": True},
        ]
        with mock.patch.object(hwt, "worktree_safety", side_effect=["merged", "dirty", "no-unique-commits"]), \
             mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            lines = hwt.palette_lines({}, items)
        text = "\n".join(lines)
        self.assertIn("\nflashcards\n", text)
        self.assertIn("\nlidar\n", text)
        self.assertIn("   1. hermes/fix-a  [merged — cleans instantly]", lines)
        self.assertIn(f"      ~{os.sep}GitRepos{os.sep}wt-a", text)
        self.assertLess(text.index("flashcards"), text.index("hermes/fix-a"))
        self.assertLess(text.index("hermes/fix-b"), text.index("lidar"))

    def test_empty_list_is_stated(self):
        self.assertIn("  (none)", hwt.palette_lines({}, []))


class PaletteTests(unittest.TestCase):
    def test_palette_requires_a_terminal(self):
        with mock.patch.object(hwt.sys, "stdin", mock.Mock(isatty=lambda: False)):
            with self.assertRaisesRegex(hwt.WorkflowError, "interactive"):
                hwt.cmd_palette(object(), {}, Path("/state"), mock.Mock())

    def test_palette_cleans_selected_worktree_and_quits(self):
        items = [{"branch": "feature/x", "is_linked_worktree": True,
                  "path": "/wts/x", "repository": "demo"}]
        answers = iter(["1", "", "q"])
        with mock.patch.object(hwt.sys, "stdin", mock.Mock(isatty=lambda: True)), \
             mock.patch("builtins.input", side_effect=lambda *_: next(answers)), \
             mock.patch.object(hwt, "configured_worktree_items", return_value=items), \
             mock.patch.object(hwt, "worktree_safety", return_value="merged"), \
             mock.patch.object(hwt, "cleanup_worktree_item", return_value={"removed": "/wts/x"}) as core, \
             contextlib.redirect_stdout(io.StringIO()):
            hwt.cmd_palette(object(), {"repositories": {}}, Path("/state"), mock.Mock())
        core.assert_called_once()


class SweepTests(unittest.TestCase):
    def test_sweep_cleans_qualifying_and_reports_skipped_with_commands(self):
        items = [
            {"branch": "feature/a", "is_linked_worktree": True, "path": "/wts/a", "repository": "demo"},
            {"branch": "feature/b", "is_linked_worktree": True, "path": "/wts/b", "repository": "demo"},
            {"branch": "main", "is_linked_worktree": False, "path": "/repo", "repository": "demo"},
        ]

        def core(cfg, state, herdr, item, *, abandon, confirm):
            if item["branch"] == "feature/b":
                raise hwt.WorkflowError(
                    "worktree has uncommitted changes; rerun with --abandon --confirm feature/b to discard them")
            return {"removed": item["path"], "reason": "merged"}

        with mock.patch.object(hwt, "configured_worktree_items", return_value=items), \
             mock.patch.object(hwt, "cleanup_worktree_item", side_effect=core), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            hwt.cmd_sweep(object(), {}, Path("/state"), mock.Mock())
        report = json.loads(out.getvalue())
        self.assertEqual([c["removed"] for c in report["cleaned"]], ["/wts/a"])
        self.assertEqual(len(report["skipped"]), 1)
        self.assertIn("--abandon --confirm feature/b", report["skipped"][0]["reason"])

    def test_sweep_continues_after_a_failing_worktree(self):
        items = [
            {"branch": "feature/a", "is_linked_worktree": True, "path": "/wts/a", "repository": "demo"},
            {"branch": "feature/c", "is_linked_worktree": True, "path": "/wts/c", "repository": "demo"},
        ]

        def core(cfg, state, herdr, item, *, abandon, confirm):
            if item["branch"] == "feature/a":
                raise hwt.WorkflowError("fetch failed")
            return {"removed": item["path"], "reason": "merged"}

        with mock.patch.object(hwt, "configured_worktree_items", return_value=items), \
             mock.patch.object(hwt, "cleanup_worktree_item", side_effect=core), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            hwt.cmd_sweep(object(), {}, Path("/state"), mock.Mock())
        report = json.loads(out.getvalue())
        self.assertEqual([c["removed"] for c in report["cleaned"]], ["/wts/c"])
        self.assertEqual(report["skipped"][0]["reason"], "fetch failed")


class InteractiveNewTests(unittest.TestCase):
    CFG = {"repositories": {
        "demo": {"path": "/canon/demo", "mode": "worktree", "base_branch": "origin/develop"},
    }}

    @staticmethod
    def blank_args():
        return type("Args", (), {"branch": None, "base": None, "repo": None})()

    def test_outside_a_repo_picks_repo_base_and_branch(self):
        args = self.blank_args()
        answers = iter(["1", "1", "feature/x"])
        with mock.patch.object(hwt.sys, "stdin", mock.Mock(isatty=lambda: True)), \
             mock.patch.object(hwt, "log"), \
             mock.patch.object(hwt, "git", return_value=mock.Mock(returncode=1, stdout="")), \
             mock.patch("builtins.input", side_effect=lambda *_: next(answers)):
            hwt.interactive_new_setup(args, self.CFG)
        self.assertEqual(args.branch, "feature/x")
        self.assertEqual(args.base, "origin/develop")
        self.assertEqual(args.repo, "/canon/demo")

    def test_inside_canonical_defaults_to_its_current_branch(self):
        args = self.blank_args()
        answers = iter(["1", "feature/y"])

        def fake_git(cwd, *a, **k):
            if a[:2] == ("rev-parse", "--show-toplevel"):
                return mock.Mock(returncode=0, stdout="/canon/demo\n")
            if a[:2] == ("branch", "--show-current"):
                return mock.Mock(returncode=0, stdout="develop\n")
            return mock.Mock(returncode=1, stdout="")

        with mock.patch.object(hwt.sys, "stdin", mock.Mock(isatty=lambda: True)), \
             mock.patch.object(hwt, "log"), \
             mock.patch.object(hwt, "git", side_effect=fake_git), \
             mock.patch.object(hwt, "resolve_configured_repo",
                               return_value=("demo", self.CFG["repositories"]["demo"])), \
             mock.patch("builtins.input", side_effect=lambda *_: next(answers)):
            hwt.interactive_new_setup(args, self.CFG)
        self.assertEqual(args.base, "develop")
        self.assertEqual(args.branch, "feature/y")

    def test_inside_worktree_offers_stacking_on_current_branch(self):
        args = self.blank_args()
        answers = iter(["2", "feature/stacked"])

        def fake_git(cwd, *a, **k):
            if a[:2] == ("rev-parse", "--show-toplevel"):
                return mock.Mock(returncode=0, stdout="/wts/demo/task\n")
            if a[:2] == ("branch", "--show-current"):
                return mock.Mock(returncode=0, stdout="feature/base-work\n")
            return mock.Mock(returncode=1, stdout="")

        with mock.patch.object(hwt.sys, "stdin", mock.Mock(isatty=lambda: True)), \
             mock.patch.object(hwt, "log"), \
             mock.patch.object(hwt, "git", side_effect=fake_git), \
             mock.patch.object(hwt, "resolve_configured_repo", side_effect=hwt.SafetyError("not canonical")), \
             mock.patch.object(hwt, "repo_for_worktree",
                               return_value=("demo", self.CFG["repositories"]["demo"], Path("/canon/demo"))), \
             mock.patch("builtins.input", side_effect=lambda *_: next(answers)):
            hwt.interactive_new_setup(args, self.CFG)
        self.assertEqual(args.base, "feature/base-work")
        self.assertEqual(args.repo, "/canon/demo")

    def test_inside_worktree_defaults_to_configured_base(self):
        args = self.blank_args()
        answers = iter(["1", "feature/sibling"])

        def fake_git(cwd, *a, **k):
            if a[:2] == ("rev-parse", "--show-toplevel"):
                return mock.Mock(returncode=0, stdout="/wts/demo/task\n")
            if a[:2] == ("branch", "--show-current"):
                return mock.Mock(returncode=0, stdout="feature/base-work\n")
            return mock.Mock(returncode=1, stdout="")

        with mock.patch.object(hwt.sys, "stdin", mock.Mock(isatty=lambda: True)), \
             mock.patch.object(hwt, "log"), \
             mock.patch.object(hwt, "git", side_effect=fake_git), \
             mock.patch.object(hwt, "resolve_configured_repo", side_effect=hwt.SafetyError("not canonical")), \
             mock.patch.object(hwt, "repo_for_worktree",
                               return_value=("demo", self.CFG["repositories"]["demo"], Path("/canon/demo"))), \
             mock.patch("builtins.input", side_effect=lambda *_: next(answers)):
            hwt.interactive_new_setup(args, self.CFG)
        self.assertEqual(args.base, "origin/develop")

    def test_non_interactive_without_branch_is_an_error(self):
        args = self.blank_args()
        with mock.patch.object(hwt.sys, "stdin", mock.Mock(isatty=lambda: False)):
            with self.assertRaisesRegex(hwt.WorkflowError, "branch name required"):
                hwt.interactive_new_setup(args, self.CFG)


class AskBaseBranchTests(unittest.TestCase):
    def test_offers_current_setting_and_remote_candidates(self):
        with mock.patch.object(hwt, "remote_base_candidates",
                               return_value=["origin/develop", "origin/main"]), \
             mock.patch.object(hwt.sys, "stdin", mock.Mock(isatty=lambda: True)), \
             mock.patch.object(hwt, "log"), \
             mock.patch("builtins.input", return_value="2"):
            result = hwt.ask_base_branch(Path("/canon"), "origin", "origin/main")
        self.assertEqual(result, "origin/develop")

    def test_typed_ref_must_be_remote_tracking_and_exist(self):
        answers = iter(["3", "develop", "origin/develop"])
        with mock.patch.object(hwt, "remote_base_candidates", return_value=["origin/develop"]), \
             mock.patch.object(hwt, "ref_exists", return_value=True), \
             mock.patch.object(hwt.sys, "stdin", mock.Mock(isatty=lambda: True)), \
             mock.patch.object(hwt, "log"), \
             mock.patch("builtins.input", side_effect=lambda *_: next(answers)), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            result = hwt.ask_base_branch(Path("/canon"), "origin", "origin/main")
        self.assertEqual(result, "origin/develop")
        self.assertIn("remote-tracking", out.getvalue())

    def test_empty_typed_ref_keeps_current_setting(self):
        answers = iter(["2", ""])
        with mock.patch.object(hwt, "remote_base_candidates", return_value=[]), \
             mock.patch.object(hwt.sys, "stdin", mock.Mock(isatty=lambda: True)), \
             mock.patch.object(hwt, "log"), \
             mock.patch("builtins.input", side_effect=lambda *_: next(answers)):
            result = hwt.ask_base_branch(Path("/canon"), "origin", "origin/main")
        self.assertEqual(result, "origin/main")


class DevCommandSuggestionTests(unittest.TestCase):
    def suggest(self, scripts):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "package.json").write_text(json.dumps({"scripts": scripts}))
            (repo / "package-lock.json").write_text("{}")
            return hwt.suggest_dev_command(repo)

    def test_vite_scripts_get_explicit_port_flags(self):
        self.assertEqual(self.suggest({"dev": "vite"}),
                         ["npm", "run", "dev", "--", "--host", "{host}", "--port", "{port}"])

    def test_next_scripts_get_p_flag(self):
        self.assertEqual(self.suggest({"dev": "next dev --turbo"}),
                         ["npm", "run", "dev", "--", "-H", "{host}", "-p", "{port}"])

    def test_env_respecting_frameworks_stay_bare(self):
        self.assertEqual(self.suggest({"start": "react-scripts start"}),
                         ["npm", "run", "start"])

    def test_no_scripts_means_no_suggestion(self):
        self.assertIsNone(self.suggest({}))


class BaseRefTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "demo"
        self.repo.mkdir()
        hwt.git(self.repo.parent, "init", "-q", str(self.repo))
        (self.repo / "f").write_text("x")
        hwt.git(self.repo, "add", "f")
        hwt.git(self.repo, "-c", "user.email=t@example.invalid", "-c", "user.name=T",
                "-c", "commit.gpgsign=false", "commit", "-q", "-m", "c")

    def tearDown(self):
        self.tmp.cleanup()

    def test_ref_exists(self):
        self.assertTrue(hwt.ref_exists(self.repo, "HEAD"))
        self.assertFalse(hwt.ref_exists(self.repo, "dev"))

    def test_remote_candidates_exclude_head_and_worktree_branches(self):
        for name in ("develop", "feature/x", "used-branch"):
            hwt.git(self.repo, "update-ref", f"refs/remotes/origin/{name}", "HEAD")
        hwt.git(self.repo, "update-ref", "refs/remotes/origin/HEAD", "HEAD")
        hwt.git(self.repo, "worktree", "add", "-q", str(self.root / "wt"), "-b", "used-branch")
        names = hwt.remote_base_candidates(self.repo, "origin")
        self.assertIn("origin/develop", names)
        self.assertIn("origin/feature/x", names)
        self.assertNotIn("origin/HEAD", names)
        self.assertNotIn("origin/used-branch", names)

    def test_canonical_checkout_branch_is_not_excluded_and_plain_names_come_first(self):
        current = hwt.git(self.repo, "branch", "--show-current").stdout.strip()
        hwt.git(self.repo, "update-ref", f"refs/remotes/origin/{current}", "HEAD")
        hwt.git(self.repo, "update-ref", "refs/remotes/origin/hermes/topic-x", "HEAD")
        hwt.git(self.repo, "update-ref", "refs/remotes/origin/develop", "HEAD")
        names = hwt.remote_base_candidates(self.repo, "origin")
        # the branch checked out in the MAIN checkout stays offered
        self.assertIn(f"origin/{current}", names)
        # plain main-line names sort before slash-namespaced topic branches
        self.assertLess(names.index("origin/develop"), names.index("origin/hermes/topic-x"))

    def test_cmd_new_never_passes_cwd_alongside_workspace(self):
        # herdr worktree create rejects --workspace together with --cwd (exit 2)
        worktrees = self.root / ".wts"
        worktrees.mkdir()
        cfg = {"canonical_root": str(self.root), "worktree_root": str(worktrees),
               "worktree_placement": "shared-root",
               "repositories": {"demo": {"path": str(self.repo), "mode": "worktree",
                                         "fetch": False}}}
        branch_now = hwt.git(self.repo, "branch", "--show-current").stdout.strip()
        args = type("Args", (), {"branch": "deleteme", "base": branch_now,
                                 "repo": str(self.repo), "background": True,
                                 "config": Path("/nonexistent")})()
        fake = mock.Mock()
        fake.call.return_value = {"result": {"workspace": {"workspace_id": "w9"}}}
        with mock.patch.object(hwt.sys, "stdin", mock.Mock(isatty=lambda: False)), \
             mock.patch.object(hwt, "ensure_canonical_workspace", return_value="w1"), \
             mock.patch.object(hwt, "bootstrap", return_value={"port": 4100}), \
             contextlib.redirect_stdout(io.StringIO()):
            hwt.cmd_new(args, cfg, self.root / "state", fake)
        created = fake.call.call_args_list[0].args
        self.assertEqual(created[:2], ("worktree", "create"))
        self.assertNotIn("--cwd", created)
        self.assertIn("--workspace", created)
        # A label would become custom_name and disable Herdr's branch-derived
        # sidebar display; the branch itself must be what the sidebar shows.
        self.assertNotIn("--label", created)

    def test_run_failure_message_includes_stderr(self):
        with mock.patch.object(hwt.subprocess, "run",
                               return_value=mock.Mock(returncode=2, stdout="",
                                                      stderr="usage: herdr worktree create ...\n")):
            with self.assertRaisesRegex(hwt.WorkflowError, "usage: herdr worktree create"):
                hwt.run(["herdr", "worktree", "create"])

    def test_cmd_new_rejects_nonexistent_base_before_touching_herdr(self):
        worktrees = self.root / ".wts"
        worktrees.mkdir()
        cfg = {"canonical_root": str(self.root), "worktree_root": str(worktrees),
               "repositories": {"demo": {"path": str(self.repo), "mode": "worktree",
                                         "fetch": False}}}
        args = type("Args", (), {"branch": "feature/x", "base": "dev", "repo": str(self.repo),
                                 "background": True, "config": Path("/nonexistent")})()
        fake = mock.Mock()
        with mock.patch.object(hwt.sys, "stdin", mock.Mock(isatty=lambda: False)):
            with self.assertRaisesRegex(hwt.WorkflowError, "base ref does not exist"):
                hwt.cmd_new(args, cfg, self.root / "state", fake)
        fake.call.assert_not_called()


class OpenPickerTests(unittest.TestCase):
    CFG = {"repositories": {
        "demo": {"path": "/canon/demo", "mode": "worktree"},
        "docs": {"path": "/canon/docs", "mode": "open-only"},
    }, "worktree_root": "/wts"}
    ITEMS = [
        {"branch": "hermes/fix-a", "is_linked_worktree": True,
         "path": "/wts/demo/fix-a", "repository": "demo"},
    ]

    def test_candidates_include_repositories_and_worktrees(self):
        with mock.patch.object(hwt, "configured_worktree_items", return_value=[dict(x) for x in self.ITEMS]):
            entries = hwt.open_candidates(self.CFG, mock.Mock())
        self.assertEqual([e["kind"] for e in entries], ["repository", "repository", "worktree"])

    def test_no_target_with_tty_offers_numbered_choice(self):
        args = type("Args", (), {"target": None})()
        with mock.patch.object(hwt, "configured_worktree_items", return_value=[dict(x) for x in self.ITEMS]), \
             mock.patch.object(hwt.sys, "stdin", mock.Mock(isatty=lambda: True)), \
             mock.patch("builtins.input", return_value="3"), \
             mock.patch.object(hwt, "log"), \
             mock.patch.object(hwt, "resolve_existing", side_effect=lambda p: Path(p)):
            path = hwt.resolve_open_target(args, self.CFG, mock.Mock())
        self.assertEqual(path, Path("/wts/demo/fix-a"))

    def test_fuzzy_target_narrows_to_single_entry(self):
        args = type("Args", (), {"target": "fix-a"})()
        with mock.patch.object(hwt, "configured_worktree_items", return_value=[dict(x) for x in self.ITEMS]), \
             mock.patch.object(hwt, "resolve_existing", side_effect=lambda p: Path(p)):
            path = hwt.resolve_open_target(args, self.CFG, mock.Mock())
        self.assertEqual(path, Path("/wts/demo/fix-a"))

    def test_no_target_without_tty_lists_and_refuses(self):
        args = type("Args", (), {"target": None})()
        logged = []
        with mock.patch.object(hwt, "configured_worktree_items", return_value=[dict(x) for x in self.ITEMS]), \
             mock.patch.object(hwt.sys, "stdin", mock.Mock(isatty=lambda: False)), \
             mock.patch.object(hwt, "log", side_effect=logged.append):
            with self.assertRaises(hwt.WorkflowError):
                hwt.resolve_open_target(args, self.CFG, mock.Mock())
        text = "\n".join(logged)
        self.assertIn("demo", text)
        self.assertIn("hermes/fix-a", text)


class WorkspaceCleanupActionTests(unittest.TestCase):
    ITEM = {
        "branch": "feature/x", "is_linked_worktree": True,
        "open_workspace_id": "w2", "path": "/approved/wt", "repository": "demo",
    }

    def test_resolves_worktree_from_workspace_env_and_cleans(self):
        fake = mock.Mock()
        with mock.patch.dict(os.environ, {"HERDR_WORKSPACE_ID": "w2"}), \
             mock.patch.object(hwt, "configured_worktree_items", return_value=[dict(self.ITEM)]), \
             mock.patch.object(hwt, "cleanup_worktree_item", return_value={"removed": "/approved/wt"}) as core, \
             contextlib.redirect_stdout(io.StringIO()):
            hwt.cmd_cleanup_workspace(object(), {}, Path("/state"), fake)
        core.assert_called_once()
        self.assertEqual(core.call_args.kwargs, {"abandon": False, "confirm": None})

    def test_requires_workspace_environment(self):
        with mock.patch.dict(os.environ, {"HERDR_WORKSPACE_ID": ""}):
            with self.assertRaisesRegex(hwt.WorkflowError, "HERDR_WORKSPACE_ID"):
                hwt.cmd_cleanup_workspace(object(), {}, Path("/state"), mock.Mock())

    def test_unknown_workspace_is_rejected(self):
        with mock.patch.dict(os.environ, {"HERDR_WORKSPACE_ID": "w-unknown"}), \
             mock.patch.object(hwt, "configured_worktree_items", return_value=[dict(self.ITEM)]):
            with self.assertRaisesRegex(hwt.WorkflowError, "not a configured linked worktree"):
                hwt.cmd_cleanup_workspace(object(), {}, Path("/state"), mock.Mock())

    def test_refusal_is_surfaced_into_the_shell_pane_and_deletes_nothing(self):
        fake = mock.Mock()
        fake.tabs.return_value = [
            {"tab_id": "t1", "label": "agent", "pane_id": "p1"},
            {"tab_id": "t2", "label": "shell", "pane_id": "p2"},
        ]
        with mock.patch.dict(os.environ, {"HERDR_WORKSPACE_ID": "w2"}), \
             mock.patch.object(hwt, "configured_worktree_items", return_value=[dict(self.ITEM)]), \
             mock.patch.object(hwt, "cleanup_worktree_item",
                               side_effect=hwt.WorkflowError("branch feature/x is not merged")):
            with self.assertRaisesRegex(hwt.WorkflowError, "not merged"):
                hwt.cmd_cleanup_workspace(object(), {}, Path("/state"), fake)
        fake.show_reminder.assert_called_once()
        pane, message = fake.show_reminder.call_args.args
        self.assertEqual(pane, "p2")
        self.assertIn("not merged", message)


class DefaultEnvOperationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        hwt.git(self.repo.parent, "init", "-q", str(self.repo))
        (self.repo / ".env").write_text("A=1")
        (self.repo / ".env.local").write_text("B=2")
        (self.repo / ".env.example").write_text("A=")
        hwt.git(self.repo, "add", ".env.example")
        hwt.git(self.repo, "-c", "user.email=test@example.invalid", "-c", "user.name=Test",
                "-c", "commit.gpgsign=false", "commit", "-q", "-m", "add example env")

    def tearDown(self):
        self.tmp.cleanup()

    def test_untracked_env_files_are_copied_tracked_ones_are_not(self):
        ops = hwt.default_env_operations(self.repo)
        self.assertEqual([op["path"] for op in ops], [".env", ".env.local"])
        self.assertTrue(all(op["overwrite"] == "skip" for op in ops))


class OverwriteSkipTests(unittest.TestCase):
    def test_skip_overwrite_keeps_existing_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            canon = Path(tmp) / "canon"; wt = Path(tmp) / "wt"
            canon.mkdir(); wt.mkdir()
            (canon / "local.cfg").write_text("source")
            (wt / "local.cfg").write_text("changed")
            messages = []
            hwt.apply_file_operations(canon, wt, [{
                "path": "local.cfg", "action": "copy", "overwrite": "skip"}],
                logger=messages.append)
            self.assertEqual((wt / "local.cfg").read_text(), "changed")
            self.assertTrue(any("keeping existing" in m for m in messages))


class AutoDependencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.canon = Path(self.tmp.name) / "canon"
        self.wt = Path(self.tmp.name) / "wt"
        self.canon.mkdir(); self.wt.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_detect_install_command_prefers_lockfile(self):
        (self.wt / "package.json").write_text("{}")
        self.assertEqual(hwt.detect_install_command(self.wt), ["npm", "install"])
        (self.wt / "package-lock.json").write_text("{}")
        self.assertEqual(hwt.detect_install_command(self.wt), ["npm", "ci"])
        (self.wt / "pnpm-lock.yaml").write_text("")
        self.assertEqual(hwt.detect_install_command(self.wt), ["pnpm", "install"])

    def test_auto_without_package_json_is_a_no_op(self):
        self.assertEqual(hwt.prepare_dependencies(self.canon, self.wt, {"policy": "auto"}), "none")

    def test_auto_with_existing_node_modules_is_left_alone(self):
        (self.wt / "node_modules").mkdir()
        self.assertEqual(hwt.prepare_dependencies(self.canon, self.wt, {"policy": "auto"}), "present")

    def test_auto_with_missing_tool_skips_without_failing(self):
        (self.wt / "package.json").write_text("{}")
        (self.wt / "pnpm-lock.yaml").write_text("")
        with mock.patch.object(hwt.shutil, "which", return_value=None):
            self.assertEqual(hwt.prepare_dependencies(self.canon, self.wt, {"policy": "auto"}), "skipped")

    def test_auto_install_failure_is_non_fatal(self):
        (self.wt / "package.json").write_text("{}")
        with mock.patch.object(hwt.shutil, "which", return_value="/usr/bin/npm"), \
             mock.patch.object(hwt, "run", return_value=mock.Mock(returncode=1)):
            self.assertEqual(hwt.prepare_dependencies(self.canon, self.wt, {"policy": "auto"}), "install-failed")

    def test_auto_runs_detected_install(self):
        (self.wt / "package.json").write_text("{}")
        with mock.patch.object(hwt.shutil, "which", return_value="/usr/bin/npm"), \
             mock.patch.object(hwt, "run", return_value=mock.Mock(returncode=0)) as run:
            self.assertEqual(hwt.prepare_dependencies(self.canon, self.wt, {"policy": "auto"}), "installed")
        run.assert_called_once_with(["npm", "install"], cwd=self.wt, check=False)


class CloneDependencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.canon = Path(self.tmp.name) / "canon"
        self.wt = Path(self.tmp.name) / "wt"
        self.canon.mkdir(); self.wt.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_matching_lockfiles_clone_canonical_node_modules(self):
        (self.canon / "package-lock.json").write_text("lock")
        (self.wt / "package-lock.json").write_text("lock")
        module = self.canon / "node_modules" / "left-pad"
        module.mkdir(parents=True)
        (module / "index.js").write_text("module.exports = 1")
        result = hwt.prepare_dependencies(self.canon, self.wt, {"policy": "clone"})
        self.assertEqual(result, "cloned")
        self.assertTrue((self.wt / "node_modules" / "left-pad" / "index.js").is_file())
        self.assertFalse((self.wt / "node_modules").is_symlink())

    def test_lockfile_mismatch_falls_back_to_install_detection(self):
        (self.canon / "package-lock.json").write_text("one")
        (self.wt / "package-lock.json").write_text("two")
        (self.canon / "node_modules").mkdir()
        # no package.json in the worktree, so the fallback has nothing to install
        self.assertEqual(hwt.prepare_dependencies(self.canon, self.wt, {"policy": "clone"}), "none")
        self.assertFalse((self.wt / "node_modules").exists())

    def test_missing_canonical_modules_falls_back_to_install(self):
        (self.canon / "package-lock.json").write_text("lock")
        (self.wt / "package-lock.json").write_text("lock")
        (self.wt / "package.json").write_text("{}")
        with mock.patch.object(hwt.shutil, "which", return_value="/usr/bin/npm"), \
             mock.patch.object(hwt, "run", return_value=mock.Mock(returncode=0)) as run:
            self.assertEqual(hwt.prepare_dependencies(self.canon, self.wt, {"policy": "clone"}), "installed")
        run.assert_called_once_with(["npm", "ci"], cwd=self.wt, check=False)

    def test_existing_worktree_modules_left_alone(self):
        (self.wt / "node_modules").mkdir()
        self.assertEqual(hwt.prepare_dependencies(self.canon, self.wt, {"policy": "clone"}), "present")

    def test_clone_tree_plain_copy_fallback_works(self):
        source = self.canon / "node_modules"
        (source / "pkg").mkdir(parents=True)
        (source / "pkg" / "a.txt").write_text("data")
        destination = self.wt / "node_modules"
        self.assertTrue(hwt.clone_tree(source, destination))
        self.assertEqual((destination / "pkg" / "a.txt").read_text(), "data")


class ConfigOverlayTests(unittest.TestCase):
    def test_repos_d_overlay_replaces_repository_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "repos").mkdir(); (root / "wts").mkdir()
            demo = root / "repos" / "demo"; demo.mkdir()
            config = root / "config.toml"
            config.write_text(
                f'canonical_root = {json.dumps(str(root / "repos"))}\n'
                f'worktree_root = {json.dumps(str(root / "wts"))}\n'
                f'[repositories."demo"]\npath = {json.dumps(str(demo))}\nstart_agent = true\n')
            overlay_dir = root / "repos.d"
            overlay_dir.mkdir()
            (overlay_dir / "demo.toml").write_text(
                f'[repositories."demo"]\npath = {json.dumps(str(demo))}\nstart_agent = false\nfiles = []\n')
            cfg = hwt.load_config(config)
            self.assertEqual(cfg["repositories"]["demo"]["start_agent"], False)
            self.assertEqual(cfg["repositories"]["demo"]["files"], [])


class VersionConsistencyTests(unittest.TestCase):
    def test_manifest_and_module_versions_match(self):
        import tomllib
        manifest = tomllib.loads((Path(__file__).parents[1] / "herdr-plugin.toml").read_text())
        self.assertEqual(manifest["version"], hwt.__version__)

    def test_version_tuple_orders_semver(self):
        self.assertGreater(hwt.version_tuple("0.10.0"), hwt.version_tuple("0.9.1"))
        self.assertEqual(hwt.version_tuple("1.2.3"), (1, 2, 3))


class FirstRunTests(unittest.TestCase):
    def test_repo_is_unconfigured_detection(self):
        self.assertTrue(hwt.repo_is_unconfigured({}))
        self.assertFalse(hwt.repo_is_unconfigured({"files": []}))
        self.assertFalse(hwt.repo_is_unconfigured({"dependencies": {"policy": "auto"}}))
        self.assertFalse(hwt.repo_is_unconfigured({"commands": {"dev": ["npm", "run", "dev"]}}))

    def test_offer_is_skipped_without_a_terminal(self):
        cfg = {"repositories": {"demo": {}}}
        args = type("Args", (), {"config": Path("/nonexistent")})()
        with mock.patch.object(hwt.sys, "stdin", mock.Mock(isatty=lambda: False)):
            self.assertIs(hwt.maybe_offer_init(args, cfg, "demo"), cfg)

    def test_unconfigured_repo_gets_init_reminder_in_shell_tab(self):
        fake = hwt.FakeHerdrForTests()
        fake.seed_workspace("w1", "/wt/demo", ["Tab 1"])
        hwt.ensure_layout(fake, "w1", Path("/wt/demo"), 4100, "127.0.0.1", "", False,
                          {"_suggest_init": True})
        self.assertTrue(any("hwt init" in message for _, message in fake.reminders))


class RenderOverlayTests(unittest.TestCase):
    def test_rendered_overlay_is_valid_toml_and_self_contained(self):
        import tomllib
        text = hwt.render_repo_overlay(
            "demo",
            {"path": "/repos/demo", "base_branch": "origin/main",
             "environment": {"APP_ENV": "development"}},
            [{"path": ".env.local"}], "auto", ["npm", "run", "dev"])
        data = tomllib.loads(text)
        repo = data["repositories"]["demo"]
        self.assertEqual(repo["path"], "/repos/demo")
        self.assertEqual(repo["files"][0]["path"], ".env.local")
        self.assertEqual(repo["files"][0]["overwrite"], "skip")
        self.assertEqual(repo["dependencies"]["policy"], "auto")
        self.assertEqual(repo["commands"]["dev"], ["npm", "run", "dev"])
        self.assertEqual(repo["environment"]["APP_ENV"], "development")


class CliHelpTests(unittest.TestCase):
    def test_internal_subcommands_are_hidden_from_help(self):
        text = hwt.parser().format_help()
        self.assertNotIn("SUPPRESS", text)
        self.assertNotIn("cleanup-workspace", text)
        self.assertNotIn("palette-open", text)
        for public in ("sweep", "palette", "cleanup", "doctor"):
            self.assertIn(public, text)


class DiscoveryTests(unittest.TestCase):
    def test_linked_worktrees_are_not_discovered_as_repositories(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "corral_install", Path(__file__).parents[1] / "install.py")
        install = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(install)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            repo = root / "myrepo"
            repo.mkdir()
            hwt.git(root, "init", "-q", str(repo))
            (repo / "f.txt").write_text("x")
            hwt.git(repo, "add", "f.txt")
            hwt.git(repo, "-c", "user.email=t@example.invalid", "-c", "user.name=T",
                    "-c", "commit.gpgsign=false", "commit", "-q", "-m", "c")
            hwt.git(repo, "worktree", "add", "-q", str(root / "myrepo--main"), "-b", "wt-branch")
            found = install.discover_repositories(root)
        self.assertEqual([name for name, _, _ in found], ["myrepo"])


if __name__ == "__main__":
    unittest.main()

