"""Herdr Desktop frontend: Corral through herdr-core (hc), against a fake core
and real git repositories."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1]))

import hwt

GIT = ("-c", "user.email=t@example.invalid", "-c", "user.name=T", "-c", "commit.gpgsign=false")


class FakeCore:
    """Just enough of `hc` for HerdrCore: terminals with labels, cwd and a foreground."""
    BINARY = "hc-fake"

    def __init__(self):
        self.terms: dict[str, dict] = {}
        self.sent: list[tuple[str, str]] = []
        self.killed: list[str] = []
        self._real_run = hwt.run

    def add(self, cwd, labels, foreground=None, status=None):
        address = f"t{len(self.terms) + len(self.killed) + 1}"
        self.terms[address] = {"address": address, "cwd": str(cwd), "labels": dict(labels),
                               "created_ms": len(self.terms) + len(self.killed),
                               "foreground": {"name": foreground or "bash", "cwd": str(cwd)},
                               "status": status, "argv": ["bash"]}
        return address

    def run(self, argv, **kwargs):
        if argv[0] != self.BINARY:
            return self._real_run(argv, **kwargs)
        verb, rest = argv[1], [a for a in argv[2:] if a != "--json"]
        out = {}
        if verb == "ls":
            wanted = [rest[i + 1] for i, a in enumerate(rest) if a == "--label"]
            out = {"terminals": [{"address": a} for a, t in self.terms.items()
                                 if all(t["labels"].get(w.split("=", 1)[0]) == w.split("=", 1)[1]
                                        for w in wanted)]}
        elif verb == "get":
            out = self.terms[rest[0]]
        elif verb == "run":
            split = rest.index("--") if "--" in rest else len(rest)
            opts, program = rest[:split], rest[split + 1:]
            labels = dict(opts[i + 1].split("=", 1) for i, a in enumerate(opts) if a == "--label")
            address = self.add(opts[opts.index("--cwd") + 1], labels)
            self.terms[address]["argv"] = program or ["bash"]
            out = {"address": address}
        elif verb == "label":
            key, value = rest[1].split("=", 1)
            self.terms[rest[0]]["labels"][key] = value
        elif verb == "kill":
            self.terms.pop(rest[0])
            self.killed.append(rest[0])
        elif verb == "send":
            self.sent.append((rest[0], rest[1]))
        return subprocess.CompletedProcess(argv, 0, json.dumps(out), "")

    def space_of(self, space):
        return [t for t in self.terms.values() if t["labels"].get("herdr.space") == space]


class DesktopTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / "repos" / "demo"
        self.repo.mkdir(parents=True)
        hwt.git(self.repo.parent, "init", "-q", "-b", "main", str(self.repo))
        (self.repo / "f").write_text("x")
        hwt.git(self.repo, "add", "f")
        hwt.git(self.repo, *GIT, "commit", "-q", "-m", "c")
        # A remote-tracking base and default branch, without a real remote.
        hwt.git(self.repo, "update-ref", "refs/remotes/origin/develop", "HEAD")
        hwt.git(self.repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/develop")
        self.worktrees = self.root / "wts"
        self.worktrees.mkdir()
        self.state = self.root / "state"
        self.core = FakeCore()
        patcher = mock.patch.object(hwt, "run", side_effect=self.core.run)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.herdr = hwt.HerdrCore(FakeCore.BINARY)

    def tearDown(self):
        self.tmp.cleanup()

    def cfg(self, **repo):
        return {"canonical_root": str(self.root / "repos"), "worktree_root": str(self.worktrees),
                "remote_host": "box.example", "agent_kind": "claude",
                "repositories": {"demo": {"path": str(self.repo), "mode": "worktree",
                                          "base_branch": "origin/develop", "fetch": False,
                                          "start_agent": True, **repo}}}


class NewWorktreeTests(DesktopTestCase):
    def new(self, branch="feature/x"):
        args = type("Args", (), {"branch": branch, "base": None, "repo": str(self.repo),
                                 "background": True, "config": Path("/nonexistent")})()
        out = io.StringIO()
        with mock.patch.object(hwt.sys, "stdin", mock.Mock(isatty=lambda: False)), \
             contextlib.redirect_stdout(out):
            hwt.cmd_new(args, self.cfg(), self.state, self.herdr)
        return json.loads(out.getvalue())

    def test_new_worktree_is_one_space_with_named_agent_shell_and_server_tabs(self):
        result = self.new()
        checkout = Path(result["worktree"])
        self.assertEqual(hwt.git(checkout, "branch", "--show-current").stdout.strip(), "feature/x")
        self.assertTrue(hwt.is_within(checkout, self.worktrees / "demo"))
        terms = self.core.space_of(result["workspace_id"])
        self.assertEqual(len(terms), len(self.core.terms), "every terminal is in the new space")
        self.assertEqual(sorted(t["labels"]["corral.tab"] for t in terms), ["agent", "server", "shell"])
        self.assertEqual(len({t["labels"]["herdr.tab"] for t in terms}), 3)
        self.assertEqual(result["tabs"], {t["labels"]["corral.tab"]: t["labels"]["herdr.tab"] for t in terms})
        self.assertTrue(all(Path(t["cwd"]) == checkout for t in terms))

    def test_shell_and_server_tabs_get_the_leased_port_and_the_agent_starts(self):
        result = self.new()
        by_label = {t["labels"]["corral.tab"]: t for t in self.core.space_of(result["workspace_id"])}
        for label in ("shell", "server"):
            argv = by_label[label]["argv"]
            self.assertEqual(argv[0], "env")
            self.assertIn(f"PORT={result['port']}", argv)
            self.assertIn(f"HWT_REMOTE_URL=http://box.example:{result['port']}", argv)
        self.assertEqual(self.core.sent, [(by_label["agent"]["address"], "claude")])

    def test_no_space_is_made_for_the_main_checkout(self):
        self.new()
        self.assertFalse(any(Path(t["cwd"]) == self.repo for t in self.core.terms.values()))


class SpaceMappingTests(DesktopTestCase):
    def test_space_maps_to_the_deepest_checkout_its_terminals_are_in(self):
        nested = self.repo / ".wts" / "wt"
        hwt.git(self.repo, "worktree", "add", "-q", "-b", "wt", str(nested))
        (nested / "src").mkdir()
        self.core.add(nested / "src", {"herdr.space": "spWT", "herdr.tab": "tb1"})
        self.core.add(self.repo, {"herdr.space": "spMAIN", "herdr.tab": "tb2"})
        items = {Path(i["path"]).resolve(): i for i in self.herdr.worktrees(self.repo)}
        self.assertEqual(items[nested.resolve()]["workspace_id"], "spWT")
        self.assertTrue(items[nested.resolve()]["is_linked_worktree"])
        self.assertEqual(items[self.repo]["workspace_id"], "spMAIN")
        self.assertFalse(items[self.repo]["is_linked_worktree"])

    def test_a_space_whose_shell_started_at_home_is_found_by_its_live_folder(self):
        address = self.core.add(self.root, {"herdr.space": "spA", "herdr.tab": "tb1"})
        self.core.terms[address]["foreground"]["cwd"] = str(self.repo)
        self.assertEqual(hwt.find_workspace_for_path(self.herdr, self.repo), "spA")

    def test_unlabelled_terminals_are_not_spaces(self):
        self.core.add(self.repo, {})
        self.assertIsNone(hwt.find_workspace_for_path(self.herdr, self.repo))


class RemoveWorktreeTests(DesktopTestCase):
    def setUp(self):
        super().setUp()
        self.checkout = self.worktrees / "demo" / "wt"
        hwt.git(self.repo, "worktree", "add", "-q", "-b", "wt", str(self.checkout))
        self.mine = self.core.add(self.checkout, {"herdr.space": "spWT", "herdr.tab": "tb1"})
        self.other = self.core.add(self.repo, {"herdr.space": "spMAIN", "herdr.tab": "tb2"})

    def test_removal_deletes_the_checkout_then_ends_only_its_space(self):
        self.herdr.remove_worktree(self.repo, "spWT", self.checkout, force=False)
        self.assertFalse(self.checkout.exists())
        self.assertEqual(self.core.killed, [self.mine])
        self.assertIn(self.other, self.core.terms)

    def test_a_refused_removal_leaves_the_space_running(self):
        (self.checkout / "f").write_text("changed")
        with self.assertRaises(hwt.WorkflowError):
            self.herdr.remove_worktree(self.repo, "spWT", self.checkout, force=False)
        self.assertTrue(self.checkout.exists())
        self.assertEqual(self.core.killed, [])


class AgentDetectionTests(DesktopTestCase):
    def test_a_reported_status_or_a_non_shell_program_is_an_agent(self):
        self.assertTrue(self.herdr.has_agent(self.core.add(self.repo, {}, status={"state": "idle"})))
        self.assertTrue(self.herdr.has_agent(self.core.add(self.repo, {}, foreground="claude")))
        self.assertFalse(self.herdr.has_agent(self.core.add(self.repo, {}, foreground="zsh")))


class InfoTests(DesktopTestCase):
    def info(self, folder, cfg):
        args = type("Args", (), {"folder": str(folder), "frontend": "desktop"})()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            hwt.cmd_info(args, cfg, self.state)
        return json.loads(out.getvalue())

    def test_unconfigured_repository_suggests_the_remotes_default_branch(self):
        cfg = self.cfg()
        cfg["repositories"] = {}
        info = self.info(self.repo, cfg)
        self.assertIsNone(info["repository"])
        self.assertEqual(info["suggested_base"], "origin/develop")
        self.assertIn("origin/develop", info["base_candidates"])
        self.assertTrue(info["under_root"])

    def test_from_inside_a_worktree_it_reports_the_repository_and_each_lease(self):
        checkout = self.worktrees / "demo" / "wt"
        hwt.git(self.repo, "worktree", "add", "-q", "-b", "wt", str(checkout))
        hwt.allocate_port(self.state, str(checkout), 4150, 4150)
        (checkout / "sub").mkdir()
        info = self.info(checkout / "sub", self.cfg())
        self.assertEqual(info["repository"], "demo")
        self.assertTrue(info["linked"])
        self.assertEqual(Path(info["main"]), self.repo)
        self.assertEqual(info["branch"], "wt")
        rows = {Path(w["path"]).resolve(): w for w in info["worktrees"]}
        self.assertEqual(rows[checkout.resolve()]["port"], 4150)
        self.assertEqual(rows[checkout.resolve()]["dev_url"], "http://box.example:4150")
        self.assertTrue(rows[self.repo]["main"])
        self.assertIsNone(rows[self.repo]["port"])

    def test_a_folder_outside_git_says_so(self):
        info = self.info(self.root, self.cfg())
        self.assertFalse(info["git"])

    def test_it_points_the_panel_at_the_panel_files_this_install_ships(self):
        panel = self.info(self.root, self.cfg())["panel"]
        self.assertEqual(panel["version"], hwt.__version__)
        self.assertIn("main.js", panel["files"])
        self.assertIn("mod.json", panel["files"])
        self.assertTrue((Path(panel["dir"]) / "main.js").read_text().startswith("// Corral panel"))


class PanelVersionTests(unittest.TestCase):
    def test_the_panel_ships_as_the_same_version_as_hwt(self):
        # The panel updates itself when a machine's Corral is newer than it is.
        source = (Path(hwt.__file__).parent / hwt.PANEL_DIR / "main.js").read_text()
        self.assertIn(f'const VERSION = "{hwt.__version__}";', source)


class AddTests(DesktopTestCase):
    def add(self, path, base=None, repositories=None):
        config = self.root / "config" / "config.toml"
        config.parent.mkdir(exist_ok=True)
        config.write_text(f'canonical_root = "{self.root / "repos"}"\nworktree_root = "{self.worktrees}"\n')
        cfg = hwt.load_config(config)
        cfg["repositories"] = repositories or {}
        args = type("Args", (), {"path": str(path), "base": base, "config": config})()
        with contextlib.redirect_stdout(io.StringIO()):
            hwt.cmd_add(args, cfg, self.state)
        return hwt.load_config(config)["repositories"]

    def test_adds_the_repository_with_the_remotes_default_branch(self):
        repo = self.add(self.repo)["demo"]
        self.assertEqual(Path(repo["path"]), self.repo)
        self.assertEqual(repo["base_branch"], "origin/develop")
        self.assertEqual(repo["mode"], "worktree")
        self.assertTrue(hwt.repo_is_unconfigured(repo), "defaults only, so hwt init still offers itself")

    def test_refuses_what_cleanup_could_not_verify_against(self):
        with self.assertRaisesRegex(hwt.WorkflowError, "remote-tracking"):
            self.add(self.repo, base="develop")
        with self.assertRaisesRegex(hwt.WorkflowError, "no such ref"):
            self.add(self.repo, base="origin/nope")

    def test_refuses_a_linked_worktree_and_a_second_entry(self):
        checkout = self.root / "repos" / "wt"
        hwt.git(self.repo, "worktree", "add", "-q", "-b", "wt", str(checkout))
        with self.assertRaisesRegex(hwt.WorkflowError, "linked worktree"):
            self.add(checkout)
        with self.assertRaisesRegex(hwt.WorkflowError, "already configured"):
            self.add(self.repo, repositories={"other": {"path": str(self.repo)}})

    def test_refuses_a_repository_outside_the_approved_root(self):
        outside = self.root / "elsewhere"
        hwt.git(self.root, "init", "-q", str(outside))
        with self.assertRaises(hwt.SafetyError):
            self.add(outside)


class FrontendSelectionTests(unittest.TestCase):
    CLEAN = {k: v for k, v in os.environ.items()
             if k not in ("HERDR_ENV", "HERDR_PLUGIN_EVENT", "HERDR_WORKSPACE_ID", "HC_TERMINAL_ID")}

    def run_main(self, config_text, *argv, env=None):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            (Path(tmp) / "r").mkdir()
            config.write_text(f'canonical_root = "{tmp}/r"\nworktree_root = "{tmp}"\nrepositories = {{}}\n'
                              + config_text)
            with mock.patch.object(hwt, "cmd_list") as cmd_list, \
                 mock.patch.dict(os.environ, {**self.CLEAN, "HWT_STATE_DIR": tmp, **(env or {})}, clear=True):
                self.assertEqual(hwt.main(["--config", str(config), *argv, "list"]), 0)
            return cmd_list.call_args.args[3]

    def test_config_chooses_desktop_and_the_flag_overrides_it(self):
        self.assertIsInstance(self.run_main('frontend = "desktop"\n'), hwt.HerdrCore)
        self.assertIsInstance(self.run_main(""), hwt.Herdr)
        self.assertIsInstance(self.run_main('frontend = "desktop"\n', "--frontend", "tui"), hwt.Herdr)

    def test_the_herdr_it_runs_in_wins_over_config(self):
        # An agent in a Desktop terminal, with the config still on tui.
        self.assertIsInstance(self.run_main("", env={"HC_TERMINAL_ID": "t3"}), hwt.HerdrCore)
        # A TUI pane, or the TUI plugin's own hooks, with the config on desktop.
        self.assertIsInstance(self.run_main('frontend = "desktop"\n', env={"HERDR_ENV": "1"}), hwt.Herdr)
        self.assertIsInstance(self.run_main('frontend = "desktop"\n', env={"HERDR_PLUGIN_EVENT": "worktree.created"}), hwt.Herdr)
        # The TUI started inside a Desktop terminal: its panes are still the TUI's.
        self.assertIsInstance(self.run_main("", env={"HC_TERMINAL_ID": "t3", "HERDR_ENV": "1"}), hwt.Herdr)


@unittest.skipUnless(os.name == "posix", "installer run needs POSIX")
class DesktopInstallTests(unittest.TestCase):
    def test_desktop_install_needs_no_herdr_and_writes_the_desktop_frontend(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "repos" / "demo").mkdir(parents=True)
            subprocess.run(["git", "init", "-q", str(home / "repos" / "demo")], check=True)
            env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}  # no herdr on PATH
            result = subprocess.run(
                [sys.executable, str(Path(hwt.__file__).with_name("install.py")),
                 "--desktop", "--repos-root", str(home / "repos")],
                env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            config = tomllib.loads((home / ".config/herdr-corral/config.toml").read_text())
            self.assertEqual(config["frontend"], "desktop")
            self.assertIn("demo", config["repositories"])
            self.assertTrue((home / ".local/bin/hwt").exists())


if __name__ == "__main__":
    unittest.main()
