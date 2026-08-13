import json
import os
import socket
import tempfile
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
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.canonical = self.root / "repos" / "demo"
        self.worktrees = self.root / "repos" / ".worktrees"
        self.canonical.mkdir(parents=True)
        self.worktrees.mkdir()

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

    def test_server_reminder_uses_live_pane_run_command(self):
        api = hwt.Herdr("herdr")
        with mock.patch.object(hwt, "run") as command:
            api.show_reminder("w2:p3", "To start the development server, run: hwt dev")
        self.assertEqual(command.call_args_list, [
            mock.call([
                "herdr", "pane", "wait-output", "w2:p3", "--regex", r"[$#] ?$",
                "--source", "visible", "--lines", "5", "--timeout", "10000", "--raw",
            ]),
            mock.call([
                "herdr", "pane", "run", "w2:p3",
                "printf '\\n%s\\n\\n' 'To start the development server, run: hwt dev'",
            ]),
        ])

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

        self.assertEqual(fake.reminders, [("w1:p3", "To start the development server, run: hwt dev")])

    def test_repo_without_dev_command_gets_no_misleading_reminder(self):
        fake = hwt.FakeHerdrForTests()
        fake.seed_workspace("w1", "/wt/demo", ["Tab 1"])
        hwt.ensure_layout(fake, "w1", Path("/wt/demo"), 4100, "127.0.0.1", "", True, {})
        self.assertEqual(fake.reminders, [])

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
             contextlib.redirect_stdout(io.StringIO()):
            hwt.cmd_cleanup(args, self.CFG, Path("/state"), fake)
        self.assertIn(mock.call(Path("/canon/demo"), "push", "origin", "--delete", "feature/x"), git.call_args_list)
        fake.call.assert_called_once_with("worktree", "remove", "--workspace", "w2", "--force")
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

    def test_cleanup_refuses_branch_not_merged_into_fetched_base(self):
        args = self.cleanup_args()
        fake = mock.Mock()
        responses = [
            mock.Mock(stdout="", returncode=0),
            mock.Mock(stdout="", returncode=0),
            mock.Mock(stdout="", returncode=1),
        ]
        with mock.patch.object(hwt, "configured_worktree_items", return_value=[dict(self.ITEM)]), \
             mock.patch.object(hwt, "resolve_existing", side_effect=lambda path: Path(path)), \
             mock.patch.object(hwt, "validate_worktree_identity"), \
             mock.patch.object(hwt, "worktree_lock", return_value=contextlib.nullcontext()), \
             mock.patch.object(hwt, "git", side_effect=responses) as git:
            with self.assertRaisesRegex(hwt.WorkflowError, "not merged into origin/main.*--abandon --confirm feature/x"):
                hwt.cmd_cleanup(args, self.CFG, Path("/state"), fake)
        self.assertEqual(git.call_args_list[-1], mock.call(
            Path("/canon/demo"), "merge-base", "--is-ancestor",
            "refs/heads/feature/x", "refs/remotes/origin/main", check=False))
        fake.call.assert_not_called()

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
             contextlib.redirect_stdout(io.StringIO()):
            hwt.cmd_cleanup(args, self.CFG, Path("/state"), fake)
        self.assertIn(mock.call(Path("/canon/demo"), "push", "origin", "--delete", "feature/x"), git.call_args_list)
        fake.call.assert_called_once_with("worktree", "remove", "--workspace", "w2")
        self.assertIn(mock.call(Path("/canon/demo"), "update-ref", "-d", "refs/heads/feature/x"), git.call_args_list)
        release.assert_called_once_with(Path("/state"), str(Path("/approved/wt")))

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

    def test_live_worktree_item_resolves_open_workspace_id(self):
        item = {"open_workspace_id": "w3", "path": "/approved/wt"}
        self.assertEqual(hwt.worktree_item_ids(item), ("w3", Path("/approved/wt")))

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


if __name__ == "__main__":
    unittest.main()

