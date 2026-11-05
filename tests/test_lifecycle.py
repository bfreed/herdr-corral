"""Worktree lifecycle against real git: cleanup that fails partway and is
finished by running it again, Corral's own links in a worktree, the order a
new worktree is prepared in, updating a release checkout, and config errors."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1]))

import hwt
import install


def _can_symlink() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        try:
            (Path(tmp) / "link").symlink_to(tmp, target_is_directory=True)
            return True
        except OSError:
            return False


needs_symlinks = unittest.skipUnless(_can_symlink(), "symlinks unavailable on this system")

GIT = ["-c", "user.email=t@example.invalid", "-c", "user.name=T", "-c", "commit.gpgsign=false"]


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *GIT, *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def pending(state, target):
    """The half-done cleanup target names, as cmd_cleanup would pick it."""
    item = hwt.choose_worktree_item({}, [], target, hwt.pending_cleanups(state))
    return Path(item["path"]), item["_pending"]


def nothing_pending(state):
    return hwt.pending_cleanups(state) == []


class Project(unittest.TestCase):
    """A main checkout with a bare origin, and a merged branch in a worktree."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.tmp.name).resolve()
        self.origin, self.canon = root / "origin.git", root / "repos" / "demo"
        self.state, wts = root / "state", root / "wts"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        self.canon.mkdir(parents=True)
        git(self.canon, "init", "-q", "-b", "main")
        (self.canon / ".gitignore").write_text("node_modules/\n")
        git(self.canon, "add", ".gitignore")
        git(self.canon, "commit", "-q", "-m", "base")
        git(self.canon, "remote", "add", "origin", str(self.origin))
        git(self.canon, "push", "-q", "-u", "origin", "main")
        self.branch = "feature/x"
        self.wt = wts / "demo" / hwt.branch_slug(self.branch)
        self.wt.parent.mkdir(parents=True)
        git(self.canon, "worktree", "add", "-q", "-b", self.branch, str(self.wt), "main")
        (self.wt / "app.txt").write_text("feature")
        git(self.wt, "add", "app.txt")
        git(self.wt, "commit", "-q", "-m", "feature")
        git(self.wt, "push", "-q", "origin", self.branch)
        git(self.canon, "merge", "-q", "--ff-only", self.branch)
        git(self.canon, "push", "-q", "origin", "main")
        git(self.canon, "fetch", "-q", "origin")
        self.cfg = {"worktree_root": str(wts), "additional_worktree_roots": [],
                    "repositories": {"demo": {"path": str(self.canon), "mode": "worktree",
                                              "remote": "origin", "base_branch": "origin/main"}}}
        with mock.patch.object(hwt, "port_is_free", return_value=True):
            self.port = hwt.allocate_port(self.state, str(self.wt), 4100, 4199)
        for name in ("stop_worktree_servers", "orphan_servers"):
            patcher = mock.patch.object(hwt, name, return_value=[])
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def item(self):
        return {"branch": self.branch, "is_linked_worktree": True, "path": str(self.wt), "repository": "demo"}

    def has_branch(self):
        return subprocess.run(["git", "-C", str(self.canon), "show-ref", "--verify", "--quiet",
                               f"refs/heads/{self.branch}"]).returncode == 0

    def has_remote_branch(self):
        return bool(git(self.origin, "branch", "--list", self.branch))


class ResumedCleanupTests(Project):
    def fail_like_windows(self, canonical, path, force):
        # git unregisters the worktree, then can't delete the folder a shell is in.
        hwt.git(canonical, "worktree", "remove", "--force", str(path))
        path.mkdir()
        raise hwt.WorkflowError(f"command failed (git) with exit status 255: "
                                f"error: failed to delete '{path}': Permission denied")

    def test_a_failed_removal_deletes_no_branch_and_a_second_cleanup_finishes(self):
        with mock.patch.object(hwt, "remove_checkout", side_effect=self.fail_like_windows):
            with self.assertRaisesRegex(hwt.WorkflowError, "Permission denied"):
                hwt.cleanup_worktree_item(self.cfg, self.state, mock.Mock(), self.item(),
                                          abandon=False, confirm=None)
        self.assertTrue(self.has_branch(), "the branch stays until the checkout is gone")
        self.assertTrue(self.has_remote_branch(), "so does its remote copy")
        self.assertEqual(hwt.leased_port(self.state, str(self.wt)), self.port)
        self.assertTrue(self.wt.is_dir())

        args = type("Args", (), {"target": self.branch, "abandon": False, "confirm": None})()
        with mock.patch.object(hwt, "configured_worktree_items", return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            hwt.cmd_cleanup(args, self.cfg, self.state, mock.Mock())
        result = json.loads(out.getvalue())
        self.assertTrue(result["resumed"])
        self.assertEqual((result["local_branch_deleted"], result["remote_branch_deleted"]),
                         (self.branch, self.branch))
        self.assertFalse(self.wt.exists(), "the empty folder git left is deleted")
        self.assertFalse(self.has_branch())
        self.assertFalse(self.has_remote_branch())
        self.assertIsNone(hwt.leased_port(self.state, str(self.wt)))
        self.assertTrue(nothing_pending(self.state))

    def test_a_branch_that_gained_commits_since_is_kept_unless_abandoned(self):
        with mock.patch.object(hwt, "remove_checkout", side_effect=self.fail_like_windows):
            with self.assertRaises(hwt.WorkflowError):
                hwt.cleanup_worktree_item(self.cfg, self.state, mock.Mock(), self.item(),
                                          abandon=False, confirm=None)
        # Someone commits to the branch from the main checkout meanwhile.
        git(self.canon, "switch", "-q", self.branch)
        (self.canon / "later.txt").write_text("new work")
        git(self.canon, "add", "later.txt")
        git(self.canon, "commit", "-q", "-m", "later")
        git(self.canon, "switch", "-q", "main")
        found = pending(self.state, self.branch)
        with self.assertRaisesRegex(hwt.WorkflowError, "now has commits not merged"):
            hwt.resume_cleanup(self.cfg, self.state, *found)
        self.assertTrue(self.has_branch())
        with self.assertRaisesRegex(hwt.WorkflowError, "exact confirmation"):
            hwt.resume_cleanup(self.cfg, self.state, *found, abandon=True, confirm="wrong")
        result = hwt.resume_cleanup(self.cfg, self.state, *found, abandon=True, confirm=self.branch)
        self.assertEqual(result["reason"], "abandoned")
        self.assertFalse(self.has_branch())

    def other_repo_with_the_same_branch(self):
        other = self.canon.parent / "other"
        other.mkdir()
        git(other, "init", "-q", "-b", "main")
        git(other, "commit", "-q", "--allow-empty", "-m", "base")
        wt = self.wt.parent.parent / "other" / hwt.branch_slug(self.branch)
        wt.parent.mkdir(parents=True)
        git(other, "worktree", "add", "-q", "-b", self.branch, str(wt), "main")
        self.cfg["repositories"]["other"] = {"path": str(other), "mode": "worktree", "remote": "origin",
                                             "base_branch": "origin/main"}
        return {"branch": self.branch, "is_linked_worktree": True, "path": str(wt), "repository": "other"}

    def test_a_retry_by_branch_name_asks_when_another_repository_has_that_branch(self):
        live = self.other_repo_with_the_same_branch()
        with mock.patch.object(hwt, "remove_checkout", side_effect=self.fail_like_windows):
            with self.assertRaises(hwt.WorkflowError):
                hwt.cleanup_worktree_item(self.cfg, self.state, mock.Mock(), self.item(),
                                          abandon=False, confirm=None)
        args = type("Args", (), {"target": self.branch, "abandon": False, "confirm": None})()
        with mock.patch.object(hwt, "configured_worktree_items", return_value=[live]), \
                mock.patch.object(hwt.sys.stdin, "isatty", return_value=False), \
                contextlib.redirect_stderr(io.StringIO()) as listed:
            with self.assertRaisesRegex(hwt.WorkflowError, "re-run with one of the listed names or paths"):
                hwt.cmd_cleanup(args, self.cfg, self.state, mock.Mock())
        self.assertIn("matches several worktrees", listed.getvalue())
        self.assertIn(hwt.shorten_home(str(self.wt)), listed.getvalue())
        self.assertIn(hwt.shorten_home(live["path"]), listed.getvalue())
        self.assertIn("cleanup cut short", listed.getvalue())
        self.assertTrue(Path(live["path"]).is_dir(), "the other repository's worktree is untouched")
        # By path, as the failure's message says: the half-done one.
        args.target = str(self.wt)
        with mock.patch.object(hwt, "configured_worktree_items", return_value=[live]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            hwt.cmd_cleanup(args, self.cfg, self.state, mock.Mock())
        self.assertTrue(json.loads(out.getvalue())["resumed"])
        self.assertFalse(self.has_branch())
        self.assertTrue(Path(live["path"]).is_dir())

    def test_an_ambiguous_name_with_input_from_nul_says_so_without_a_traceback(self):
        # Windows counts NUL as a terminal: `hwt cleanup x < NUL` asks, and gets EOF.
        live = self.other_repo_with_the_same_branch()
        with mock.patch.object(hwt, "remove_checkout", side_effect=self.fail_like_windows):
            with self.assertRaises(hwt.WorkflowError):
                hwt.cleanup_worktree_item(self.cfg, self.state, mock.Mock(), self.item(),
                                          abandon=False, confirm=None)
        args = type("Args", (), {"target": self.branch, "abandon": False, "confirm": None})()
        with mock.patch.object(hwt, "configured_worktree_items", return_value=[live]), \
                mock.patch.object(hwt.sys.stdin, "isatty", return_value=True), \
                mock.patch("builtins.input", side_effect=EOFError), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(hwt.WorkflowError, "re-run with one of the listed names or paths"):
                hwt.cmd_cleanup(args, self.cfg, self.state, mock.Mock())
        self.assertTrue(self.has_branch())
        self.assertTrue(Path(live["path"]).is_dir())

    def test_a_retry_closes_the_worktrees_own_workspace_first(self):
        with mock.patch.object(hwt, "remove_checkout", side_effect=self.fail_like_windows):
            with self.assertRaises(hwt.WorkflowError):
                hwt.cleanup_worktree_item(self.cfg, self.state, mock.Mock(), self.item(),
                                          abandon=False, confirm=None)
        # Herdr's removal failed, so the worktree's workspace is still open and
        # its terminals hold the folder until it closes.
        herdr, closed = mock.Mock(), []
        herdr.call.side_effect = lambda *a: closed.append(a) or {"result": {}}
        real_rmtree = hwt.shutil.rmtree
        def rmtree(path, *a, **k):
            if not closed:
                raise PermissionError(13, "in use", str(path))
            real_rmtree(path, *a, **k)
        with mock.patch.object(hwt, "find_workspace_for_path", return_value="w7"), \
                mock.patch.object(hwt.shutil, "rmtree", side_effect=rmtree), \
                mock.patch.dict(os.environ, {"HERDR_WORKSPACE_ID": "w1"}):
            result = hwt.resume_cleanup(self.cfg, self.state, *pending(self.state, str(self.wt)), herdr=herdr)
        self.assertEqual(closed, [("workspace", "close", "w7")])
        self.assertFalse(self.wt.exists())
        self.assertIsNone(result["leftover_folder"])

    def test_a_retry_from_inside_the_worktrees_workspace_says_so(self):
        with mock.patch.object(hwt, "remove_checkout", side_effect=self.fail_like_windows):
            with self.assertRaises(hwt.WorkflowError):
                hwt.cleanup_worktree_item(self.cfg, self.state, mock.Mock(), self.item(),
                                          abandon=False, confirm=None)
        herdr = mock.Mock()
        with mock.patch.object(hwt, "find_workspace_for_path", return_value="w7"), \
                mock.patch.dict(os.environ, {"HERDR_WORKSPACE_ID": "w7"}):
            with self.assertRaisesRegex(hwt.WorkflowError, "own workspace, which has to close first"):
                hwt.resume_cleanup(self.cfg, self.state, *pending(self.state, str(self.wt)), herdr=herdr)
        herdr.call.assert_not_called()
        self.assertTrue(self.has_branch(), "nothing deleted")

    def test_a_folder_still_holding_files_is_reported_not_deleted(self):
        def fail(canonical, path, force):
            hwt.git(canonical, "worktree", "remove", "--force", str(path))
            (path / "locked").mkdir(parents=True)
            (path / "locked" / "open.log").write_text("in use")
            raise hwt.WorkflowError("command failed (git) with exit status 255: Permission denied")
        with mock.patch.object(hwt, "remove_checkout", side_effect=fail):
            with self.assertRaises(hwt.WorkflowError):
                hwt.cleanup_worktree_item(self.cfg, self.state, mock.Mock(), self.item(),
                                          abandon=False, confirm=None)
        with contextlib.redirect_stderr(io.StringIO()):
            result = hwt.resume_cleanup(self.cfg, self.state, *pending(self.state, str(self.wt)))
        self.assertEqual(result["leftover_folder"], str(self.wt))
        self.assertEqual((self.wt / "locked" / "open.log").read_text(), "in use")
        self.assertFalse(self.has_branch())

    def test_a_cleanup_that_succeeds_leaves_nothing_pending(self):
        result = hwt.cleanup_worktree_item(self.cfg, self.state, mock.Mock(), self.item(),
                                           abandon=False, confirm=None)
        self.assertEqual(result["reason"], "merged")
        self.assertFalse(self.wt.exists())
        self.assertFalse(self.has_remote_branch())
        self.assertTrue(nothing_pending(self.state))


class CorralLinkTests(Project):
    @needs_symlinks
    def test_a_shared_node_modules_link_is_not_uncommitted_work(self):
        (self.canon / "node_modules" / "pkg").mkdir(parents=True)
        (self.canon / "node_modules" / "pkg" / "index.js").write_text("keep")
        repo = {**self.cfg["repositories"]["demo"], "dependencies": {"policy": "shared"}}
        self.assertEqual(hwt.prepare_dependencies(self.canon, self.wt, repo["dependencies"]), "shared")
        self.assertIn("?? node_modules", git(self.wt, "status", "--porcelain"),
                      "git counts the link: node_modules/ matches only a real folder")
        self.assertFalse(hwt.worktree_dirty(self.wt, repo))
        self.cfg["repositories"]["demo"] = repo
        hwt.cleanup_worktree_item(self.cfg, self.state, mock.Mock(), self.item(), abandon=False, confirm=None)
        self.assertFalse(self.wt.exists())
        self.assertEqual((self.canon / "node_modules" / "pkg" / "index.js").read_text(), "keep")

    def test_other_untracked_files_are_still_uncommitted_work(self):
        (self.wt / "notes.txt").write_text("mine")
        self.assertTrue(hwt.worktree_dirty(self.wt, self.cfg["repositories"]["demo"]))

    @needs_symlinks
    def test_a_symlinked_file_can_be_applied_again(self):
        (self.canon / "local.cfg").write_text("secret")
        ops = [{"path": "local.cfg", "action": "symlink"}]
        said = []
        hwt.apply_file_operations(self.canon, self.wt, ops, logger=said.append)
        hwt.apply_file_operations(self.canon, self.wt, ops, logger=said.append)
        self.assertEqual(said, ["linked local path: local.cfg", "symlink already current: local.cfg"])
        (self.wt / "local.cfg").unlink()
        (self.wt / "local.cfg").symlink_to(self.canon / ".gitignore")
        with self.assertRaisesRegex(hwt.SafetyError, "symlink escape"):
            hwt.apply_file_operations(self.canon, self.wt, ops, logger=said.append)


class LockfileTests(unittest.TestCase):
    def test_lockfiles_differing_only_in_line_endings_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            canon, wt = Path(tmp) / "canon", Path(tmp) / "wt"
            (canon / "node_modules" / "pkg").mkdir(parents=True)
            wt.mkdir()
            (canon / "package-lock.json").write_bytes(b'{\n  "a": 1\n}\n')
            (wt / "package-lock.json").write_bytes(b'{\r\n  "a": 1\r\n}\r\n')  # core.autocrlf=true
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(hwt.prepare_dependencies(canon, wt, {"policy": "clone"}), "cloned")
            (wt / "other.lock").write_bytes(b"a\n")
            (canon / "other.lock").write_bytes(b"b\n")
            self.assertNotEqual(hwt.lockfile_digest(wt / "other.lock"), hwt.lockfile_digest(canon / "other.lock"))


class NewWorktreeOrderTests(unittest.TestCase):
    def test_dependencies_and_metadata_come_before_the_agent(self):
        events = []

        class Recording(hwt.FakeHerdrForTests):
            def create_tab(self, workspace, cwd, label, env):
                events.append(f"tab {label}")
                return super().create_tab(workspace, cwd, label, env)

            def metadata(self, workspace, tokens):
                events.append("metadata")

            def start_agent(self, pane_id, name, kind="hermes"):
                events.append("agent")

        fake = Recording()
        fake.seed_workspace("w1", "/wt", [])
        with tempfile.TemporaryDirectory() as tmp:
            repo = {"start_agent": True, "files": []}
            with mock.patch.object(hwt, "validate_worktree_identity", return_value=("demo", repo, Path(tmp))), \
                    mock.patch.object(hwt, "port_is_free", return_value=True), \
                    mock.patch.object(hwt, "prepare_dependencies",
                                      side_effect=lambda *a: events.append("dependencies") or "installed"):
                summary = hwt._bootstrap_locked({}, Path(tmp) / "state", Path(tmp), "w1", fake)
        self.assertEqual(events, ["tab agent", "tab shell", "tab server", "dependencies", "metadata", "agent"])
        self.assertEqual(summary["dependencies"], "installed")

    def test_an_agent_waiting_for_an_answer_is_not_a_failure(self):
        waiting = subprocess.CompletedProcess([], 1, "", json.dumps({"id": "cli:agent:start", "error": {
            "code": "agent_not_ready", "message": "agent hwt-1 is blocked during startup"}}))
        with mock.patch.object(hwt, "run", return_value=waiting), contextlib.redirect_stderr(io.StringIO()):
            hwt.Herdr("herdr").start_agent("w1:p1", "hwt-1", "claude")
        failed = subprocess.CompletedProcess([], 1, "", '{"error":{"code":"agent_start_failed"}}')
        with mock.patch.object(hwt, "run", return_value=failed):
            with self.assertRaisesRegex(hwt.WorkflowError, "agent_start_failed"):
                hwt.Herdr("herdr").start_agent("w1:p1", "hwt-1", "claude")

    def test_an_agent_waiting_for_an_answer_keeps_the_worktrees_port(self):
        waiting = subprocess.CompletedProcess([], 1, "", '{"error":{"code":"agent_not_ready"}}')
        herdr = hwt.Herdr("herdr")
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            repo = {"start_agent": True, "files": []}
            with mock.patch.object(hwt, "validate_worktree_identity", return_value=("demo", repo, Path(tmp))), \
                    mock.patch.object(hwt, "port_is_free", return_value=True), \
                    mock.patch.object(hwt, "prepare_dependencies", return_value="none"), \
                    mock.patch.object(hwt, "ensure_layout", side_effect=lambda h, *a, **k: (
                        k["before_agent"](), h.start_agent("w1:p1", "hwt-1", "claude"))[0]), \
                    mock.patch.object(hwt, "run", return_value=waiting), \
                    contextlib.redirect_stderr(io.StringIO()):
                hwt._bootstrap_locked({}, state, Path(tmp), "w1", herdr)
            self.assertIsNotNone(hwt.leased_port(state, tmp))


class UpdateTests(unittest.TestCase):
    """hwt update in a checkout of Corral: a branch is pulled; a release tag
    (what the Corral panel's Install checks out) moves to the newest release."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.tmp.name)
        self.origin, self.clone = root / "corral", root / "clone"
        self.origin.mkdir()
        git(self.origin, "init", "-q", "-b", "main")
        for version in ("1.3.0", "1.3.1"):
            (self.origin / "herdr-plugin.toml").write_text(f'version = "{version}"\n')
            git(self.origin, "add", ".")
            git(self.origin, "commit", "-q", "-m", version)
            git(self.origin, "tag", f"v{version}")
        git(self.origin, "tag", "v9.0.0-rc1")  # not a release

    def tearDown(self):
        self.tmp.cleanup()

    def update(self):
        with mock.patch.object(hwt, "install_root", return_value=self.clone), \
                mock.patch.object(hwt, "__version__", "1.3.0"), \
                mock.patch.object(hwt.shutil, "which", return_value=None), \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            hwt.cmd_update(object())
        return json.loads(out.getvalue())

    def test_a_release_checkout_moves_to_the_newest_release(self):
        subprocess.run(["git", "clone", "-q", "--branch", "v1.3.0", str(self.origin), str(self.clone)],
                       check=True, capture_output=True)
        result = self.update()
        self.assertEqual(result["version"], "1.3.1")
        self.assertEqual(git(self.clone, "rev-parse", "HEAD"), git(self.origin, "rev-parse", "v1.3.1^{commit}"))

    def test_a_branch_checkout_is_pulled(self):
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.clone)], check=True, capture_output=True)
        (self.origin / "herdr-plugin.toml").write_text('version = "1.4.0"\n')
        git(self.origin, "commit", "-q", "-am", "1.4.0")
        self.assertEqual(self.update()["version"], "1.4.0")
        self.assertEqual(git(self.clone, "branch", "--show-current"), "main")


class NoAnswerTests(unittest.TestCase):
    def test_a_question_that_gets_no_answer_ends_hwt_with_a_message(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(hwt, "load_config", return_value={"repositories": {}}), \
                mock.patch.object(hwt, "runtime_state_dir", return_value=Path(tmp)), \
                mock.patch.object(hwt, "cmd_init", side_effect=EOFError), \
                contextlib.redirect_stderr(io.StringIO()) as said:
            self.assertEqual(hwt.main(["--frontend", "tui", "init", "demo"]), 2)
        self.assertIn("stopped at a question that got no answer", said.getvalue())
        self.assertNotIn("Traceback", said.getvalue())


class ConfigErrorTests(unittest.TestCase):
    def test_a_malformed_overlay_is_named(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "repos").mkdir(); (root / "wts").mkdir(); (root / "repos.d").mkdir()
            config = root / "config.toml"
            config.write_text(f'canonical_root = {json.dumps(str(root / "repos"))}\n'
                              f'worktree_root = {json.dumps(str(root / "wts"))}\n')
            (root / "repos.d" / "demo.toml").write_text("[repositories.demo\npath = 1\n")
            with self.assertRaisesRegex(hwt.WorkflowError, r"demo\.toml isn't valid TOML"):
                hwt.load_config(config)
            with contextlib.redirect_stderr(io.StringIO()) as said:
                self.assertEqual(hwt.main(["--config", str(config), "list"]), 2)
            self.assertIn("demo.toml", said.getvalue())
            self.assertNotIn("Traceback", said.getvalue())

    def test_a_config_without_repositories_works(self):
        """install.py writes no [repositories] table when --repos-root holds no
        clones; doctor (the installer's next step) and info crashed with KeyError."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "repos").mkdir(); (root / "wts").mkdir()
            repo = root / "elsewhere" / "demo"; repo.mkdir(parents=True)
            git(repo, "init", "-q")
            config = root / "config.toml"
            config.write_text(install.render_config(root / "repos", root / "wts", [],
                                                    "127.0.0.1", "", "claude"))
            self.assertEqual(hwt.load_config(config)["repositories"], {})
            with mock.patch.object(hwt, "latest_remote_version", return_value=None), \
                 mock.patch.object(hwt, "orphan_servers", return_value=[]), \
                 contextlib.redirect_stdout(io.StringIO()) as out, \
                 contextlib.redirect_stderr(io.StringIO()) as said:
                # Other checks (HERDR_ENV, tools on PATH, the Windows launcher)
                # depend on the machine and may fail, exiting 1; only a crash matters.
                with contextlib.suppress(SystemExit):
                    hwt.main(["--config", str(config), "doctor"])
                report = json.loads(out.getvalue())
                self.assertFalse([c for c in report["checks"] if c["check"].startswith("repo:")])
                self.assertIn("No repositories are configured yet", said.getvalue())
                out.truncate(0); out.seek(0)
                self.assertEqual(hwt.main(["--config", str(config), "info", str(repo)]), 0)
                self.assertIsNone(json.loads(out.getvalue())["repository"])


if __name__ == "__main__":
    unittest.main()
