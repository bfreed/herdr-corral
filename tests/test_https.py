import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1]))

import hwt

DNS = "box.tail1234.ts.net"
ON = {"https": {"provider": "tailscale"}, "ports": {"start": 4100, "end": 4199}}


def done(stdout: str = "", code: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], code, stdout, stderr)


class FakeTailscale:
    """The tailscale CLI over an in-memory Serve config."""
    def __init__(self, web: dict | None = None, fail_serve: str | None = None, certs: bool = True):
        self.web = dict(web or {})
        self.certs = certs
        self.fail_serve = fail_serve
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str, timeout: float = 20.0):
        self.calls.append(args)
        if args[0] == "status":
            return done(json.dumps({"Self": {"DNSName": DNS + "."},
                                    "CertDomains": [DNS] if self.certs else None}))
        if args[:2] == ("serve", "status"):
            tcp = {key.rpartition(":")[2]: {"HTTPS": True} for key in self.web}
            return done(json.dumps({"TCP": tcp, "Web": self.web} if self.web else {}))
        if args[0] == "serve" and args[-1] == "off":
            self.web.pop(f"{DNS}:{args[1].removeprefix('--https=')}", None)
            return done()
        if args[:2] == ("serve", "--bg"):
            if self.fail_serve:
                return done(code=1, stderr=self.fail_serve)
            self.web[f"{DNS}:{args[2].removeprefix('--https=')}"] = {"Handlers": {"/": {"Proxy": args[3]}}}
            return done()
        raise AssertionError(args)

    def changes(self):
        return [c for c in self.calls if c[0] == "serve" and c[1] != "status"]


def mapping(port: int) -> dict:
    return {"Handlers": {"/": {"Proxy": f"http://127.0.0.1:{port}"}}}


class HttpsSettingsTests(unittest.TestCase):
    def test_off_unless_configured(self):
        self.assertIsNone(hwt.https_settings({}))
        self.assertIsNone(hwt.https_port({"https": {"provider": "off"}}, 4120))

    def test_tailscale_offsets_the_leased_port(self):
        self.assertEqual(hwt.https_port(ON, 4120), 5120)
        self.assertEqual(hwt.https_port({"https": {"provider": "tailscale", "port_offset": 300}}, 4120), 4420)

    def test_unknown_provider_is_an_error(self):
        with self.assertRaises(hwt.WorkflowError):
            hwt.https_settings({"https": {"provider": "caddy"}})


class EnsureHttpsTests(unittest.TestCase):
    def test_off_never_runs_tailscale(self):
        with mock.patch.object(hwt, "tailscale") as ts:
            self.assertIsNone(hwt.ensure_https({}, 4120))
        ts.assert_not_called()

    def test_serves_the_port_and_returns_its_url(self):
        ts = FakeTailscale()
        with mock.patch.object(hwt, "tailscale", ts):
            url = hwt.ensure_https(ON, 4120)
        self.assertEqual(url, f"https://{DNS}:5120")
        self.assertEqual(ts.changes(), [("serve", "--bg", "--https=5120", "http://127.0.0.1:4120")])

    def test_existing_mapping_is_reused(self):
        ts = FakeTailscale({f"{DNS}:5120": mapping(4120)})
        with mock.patch.object(hwt, "tailscale", ts):
            self.assertEqual(hwt.ensure_https(ON, 4120), f"https://{DNS}:5120")
        self.assertEqual(ts.changes(), [])

    def test_someone_elses_mapping_is_left_alone(self):
        ts = FakeTailscale({f"{DNS}:5120": mapping(3000)})
        with mock.patch.object(hwt, "tailscale", ts), mock.patch.object(hwt, "log") as log:
            self.assertIsNone(hwt.ensure_https(ON, 4120))
        self.assertEqual(ts.changes(), [])
        self.assertIn("something else", log.call_args[0][0])

    def test_without_tailscale_it_warns_and_serves_http_only(self):
        with mock.patch.object(hwt, "tailscale", return_value=None), mock.patch.object(hwt, "log") as log:
            self.assertIsNone(hwt.ensure_https(ON, 4120))
        self.assertIn("HTTP only", log.call_args[0][0])

    def test_without_tailnet_certificates_it_warns_without_waiting_on_serve(self):
        ts = FakeTailscale(certs=False)
        with mock.patch.object(hwt, "tailscale", ts), mock.patch.object(hwt, "log") as log:
            self.assertIsNone(hwt.ensure_https(ON, 4120))
        self.assertEqual(ts.changes(), [])
        self.assertIn("HTTPS certificates", log.call_args[0][0])

    def test_denied_serve_suggests_the_operator_fix(self):
        ts = FakeTailscale(fail_serve="Access denied: serve config denied")
        with mock.patch.object(hwt, "tailscale", ts), mock.patch.object(hwt, "log") as log:
            self.assertIsNone(hwt.ensure_https(ON, 4120))
        self.assertIn("--operator", log.call_args[0][0])


class StopHttpsTests(unittest.TestCase):
    def test_removes_only_its_own_mapping(self):
        ts = FakeTailscale({f"{DNS}:5120": mapping(4120), f"{DNS}:5121": mapping(3000)})
        with mock.patch.object(hwt, "tailscale", ts):
            hwt.stop_https(ON, 4120)
            hwt.stop_https(ON, 4121)
        self.assertEqual(ts.changes(), [("serve", "--https=5120", "off")])

    def test_releasing_a_lease_stops_https(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            with mock.patch.object(hwt, "port_is_free", return_value=True):
                port = hwt.allocate_port(state, "/wt/a", 4120, 4120)
            with mock.patch.object(hwt, "stop_https") as stop:
                hwt.release_port(state, "/wt/a", ON)
                hwt.release_port(state, "/wt/a", ON)  # nothing leased the second time
        stop.assert_called_once_with(ON, port)

    def test_orphans_are_unleased_ports_in_range(self):
        ts = FakeTailscale({f"{DNS}:5120": mapping(4120), f"{DNS}:5121": mapping(4121),
                            f"{DNS}:4443": mapping(4102), f"{DNS}:8000": mapping(7000)})
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            with mock.patch.object(hwt, "port_is_free", return_value=True):
                hwt.allocate_port(state, "/wt/a", 4120, 4120)
            with mock.patch.object(hwt, "tailscale", ts):
                orphans = hwt.orphan_https(ON, state)
        # 4443 -> 4102 isn't at the offset, so it isn't Corral's.
        self.assertEqual(orphans, [{"port": 4121, "url": f"https://{DNS}:5121"}])


class StartDevTests(unittest.TestCase):
    def bootstrap(self, *, repo: dict, cfg: dict | None = None, deps: str = "cloned",
                  free: bool = True, labels: tuple[str, ...] = ("agent",)):
        fake = hwt.FakeHerdrForTests()
        fake.seed_workspace("w1", "/wt/demo", list(labels))
        cfg = {"repositories": {"demo": repo}, **(cfg or {})}
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(hwt, "validate_worktree_identity", return_value=("demo", repo, Path("/canon"))), \
             mock.patch.object(hwt, "allocate_port", return_value=4120), \
             mock.patch.object(hwt, "apply_file_operations"), \
             mock.patch.object(hwt, "prepare_local_cache_paths"), \
             mock.patch.object(hwt, "prepare_dependencies", return_value=deps), \
             mock.patch.object(hwt, "ensure_https", return_value=f"https://{DNS}:5120"), \
             mock.patch.object(hwt, "port_is_free", return_value=free):
            summary = hwt._bootstrap_locked(cfg, Path(tmp), Path("/wt/demo"), "w1", fake)
        return fake, summary

    DEV = {"commands": {"dev": ["npm", "run", "dev"]}}

    def test_starts_hwt_dev_in_the_new_server_tab(self):
        fake, summary = self.bootstrap(repo=self.DEV, cfg={"start_dev": True})
        server = next(t["pane_id"] for t in fake.workspaces["w1"] if t["label"] == "server")
        self.assertEqual(fake.commands, [(server, "hwt dev")])
        self.assertTrue(summary["dev_started"])
        self.assertEqual(summary["https_url"], f"https://{DNS}:5120")
        self.assertTrue(any("starting the dev server" in m and "https://" in m for _, m in fake.reminders))

    def test_repository_setting_overrides_the_global_one(self):
        fake, _ = self.bootstrap(repo={**self.DEV, "start_dev": False}, cfg={"start_dev": True})
        self.assertEqual(fake.commands, [])

    def test_off_by_default(self):
        fake, summary = self.bootstrap(repo=self.DEV)
        self.assertEqual(fake.commands, [])
        self.assertFalse(summary["dev_started"])

    def test_needs_a_dev_command(self):
        fake, _ = self.bootstrap(repo={}, cfg={"start_dev": True})
        self.assertEqual(fake.commands, [])

    def test_not_after_a_failed_install(self):
        fake, _ = self.bootstrap(repo=self.DEV, cfg={"start_dev": True}, deps="install-failed")
        self.assertEqual(fake.commands, [])

    def test_not_when_the_port_is_already_serving(self):
        fake, _ = self.bootstrap(repo=self.DEV, cfg={"start_dev": True}, free=False)
        self.assertEqual(fake.commands, [])

    def test_reopening_with_a_server_tab_starts_nothing(self):
        fake, _ = self.bootstrap(repo=self.DEV, cfg={"start_dev": True}, labels=("agent", "shell", "server"))
        self.assertEqual(fake.commands, [])


if __name__ == "__main__":
    unittest.main()
