// The Corral panel's logic, without Herdr: every state it draws, what its
// buttons run, and how it names Corral's tabs. Run: node --test desktop/tests
// Element builders are stubbed (they keep text, ids and click handlers);
// `herdr-app check desktop/corral` checks the real native elements.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFile, mkdtemp, mkdir, writeFile } from "node:fs/promises";
import { execFileSync } from "node:child_process";
import { tmpdir } from "node:os";
import { join as pjoin, dirname as pdirname } from "node:path";

const source = (await readFile(new URL("../corral/mods/corral/main.js", import.meta.url), "utf8"))
  .replace(/^import [^;]+;\n/gm, "")
  .replace("export default class Corral", "class Corral");
const VERSION = source.match(/const VERSION = "([^"]+)"/)[1];
const esc = (text) => text.replace(/[.\\]/g, (c) => `\\${c}`); // for a RegExp

// A Windows machine, as its probe reports it.
const WIN_PY = "C:\\Py313\\python.exe";
const WIN_HWT = "C:\\Users\\b\\.local\\share\\herdr-corral\\hwt.py";
const WIN_HC = "C:\\Users\\b\\AppData\\Local\\Programs\\herdr-core\\hc.exe";

function element() {
  const node = { id: "", children: [], click: null };
  const el = new Proxy(node, {
    get: (_, method) => {
      if (method === "node") return node;
      return (...args) => {
        if (method === "id") node.id = args[0];
        if (method === "child") node.children.push(args[0]?.node ?? args[0]);
        if (method === "children") node.children.push(...args[0].map((c) => c?.node ?? c));
        if (method === "when" && args[0]) args[1](el);
        if (method === "on_click") node.click = args[0];
        return el;
      };
    },
  });
  return el;
}
const textOf = (n) => (typeof n === "string" ? n : (n?.children ?? []).map(textOf).join(" "));
const find = (n, id) => (n?.id === id ? n : (n?.children ?? []).map((c) => find(c, id)).find(Boolean));

function harness({ probe = {}, info = null, hwt = {}, space = {}, terms = [], tabs = null, width = 260, files = {},
                   windows = false, pythons = ["py"], oldPythons = [], gitSh = false, execError = null,
                   home = "/mac/.config/herdr-app" } = {}) {
  const calls = { exec: [], run: [], notify: [], writes: [] };
  const store = {};
  const block = { id: "b7", type: "block", content: { mod: "corral" }, size: { px: width } };
  const theSpace = { id: "hermes/sp1", machine: "hermes", folder: "/r/demo", named: false,
                     tabs: tabs ?? [{ id: "hermes/tb0", index: 1, named: false }], ...space };
  const state = { spaces: [theSpace], panes: [{ tab: theSpace.tabs[0]?.id, cwd: theSpace.folder }], terms };
  const lines = [`os=${gitSh ? "MINGW64_NT-10.0" : "Linux"}`, `hwt=${probe.hwt ?? "/h/.local/bin/hwt"}`, probe.git === false ? "" : "git=1",
                 `py=${probe.py === false ? 0 : 1}`, `common=${probe.common ?? "/r/demo/.git"}`];
  if (probe.hwt !== "") lines.push(`version=hwt ${probe.version ?? VERSION}`, "@@info", JSON.stringify(info));
  const installed = probe.hwt !== "";
  const winLines = ["os=Windows", `python=${WIN_PY}`, `py=${probe.py === false ? 0 : 1}`, probe.git === false ? "git=0" : "git=1",
                    `common=${probe.common ?? "C:/r/demo/.git"}`, `hc=${WIN_HC}`,
                    `hwt=${installed ? "C:\\Users\\b\\.local\\bin\\hwt.cmd" : ""}`, `run=${JSON.stringify(installed ? [WIN_PY, WIN_HWT] : [])}`];
  if (installed) winLines.push(`version=hwt ${probe.version ?? VERSION}`, "@@info", JSON.stringify(info));
  const notFound = (cmd) => { throw new Error(`herdr.exec: cannot run ${cmd}: program not found`); };
  const globals = {
    View: class {}, div: element, v_flex: element, h_flex: element, svg: element,
    shared: (k) => store[k], setShared: (k, v) => { store[k] = v; return null; },
    appHome: () => home,
    readTextFile: async (path, opts) => files[`${opts?.on ?? "local"}:${path}`] ?? null,
    writeTextFile: async (path, text, expected) => {
      assert.equal(files[`local:${path}`] ?? null, expected, "writes only over what it read");
      files[`local:${path}`] = text;
      calls.writes.push(path);
      return null;
    },
    Input: { new: element },
    InputState: { new: () => { let v = ""; return { on() {}, value: () => v, set_value: (x) => { v = x; }, type: (x) => { v = x; } }; } },
    theme: () => new Proxy({}, { get: (_, k) => String(k) }),
    window: () => ({ platform: "macos", titlebar: "none", fullscreen: false }),
    layout: () => ({ traffic_lights: [16, 18], type: "row", children: [block, { id: "sidebar", children: [] }] }),
    settings: () => ({ zoom: 1 }),
    spaces: () => structuredClone(state.spaces),
    panes: () => structuredClone(state.panes),
    focused: () => ({ space: theSpace.id }),
    terminals: () => structuredClone(state.terms),
    run: (...a) => {
      calls.run.push(a);
      if (a[0] === "layout.resize" && a[1].id === block.id) block.size = { px: a[1].px };
      return null;
    },
    notify: (n) => { calls.notify.push(n); return "n"; },
    exec: async (cmd, args, opts) => {
      calls.exec.push({ cmd, args, on: opts?.on });
      const reply = (sub) => {
        let out = hwt[sub] ?? {};
        if (Array.isArray(out)) out = out.shift() ?? {}; // one reply per call
        return out.error ? { code: 2, stdout: "", stderr: `hwt: ${out.error}\r\n` } : { code: 0, stdout: JSON.stringify(out), stderr: "" };
      };
      if (windows && cmd !== "sh") {
        const c = args.indexOf("-c");
        if (c >= 0 && args[c + 1].includes("@@info")) {
          if (oldPythons.includes(cmd)) return { code: 0, stdout: "os=Windows\r\npython=C:\\Py39\\python.exe\r\npy=0\r\ngit=1\r\ncommon=C:/r/demo/.git\r\nhwt=\r\nrun=[]\r\n", stderr: "" };
          if (!pythons.includes(cmd) && cmd !== WIN_PY) notFound(cmd);
          return { code: 0, stdout: winLines.join("\r\n") + "\r\n", stderr: "" }; // Windows line endings
        }
        if (cmd === WIN_PY && args[0] === WIN_HWT) return reply(args[2] === "desktop" ? args[3] : args[1]);
        if (cmd === "git" && !pythons.length) return { code: 0, stdout: "C:/r/demo/.git\r\n", stderr: "" };
        if (cmd === WIN_HC && args[0] === "ls") return { code: 0, stdout: JSON.stringify({ terminals: [{ address: "t9" }] }), stderr: "" };
        return { code: 0, stdout: "{}", stderr: "" };
      }
      if (windows && !gitSh) notFound("sh");
      if (execError) throw new Error(execError);
      const script = args[1];
      if (script.includes("@@info")) return { code: 0, stdout: lines.join("\n") + "\n", stderr: "" };
      if (script === 'exec "$@"') {
        const sub = args[6];
        let reply = hwt[sub] ?? {};
        if (Array.isArray(reply)) reply = reply.shift() ?? {}; // one reply per call
        return reply.error ? { code: 2, stdout: "", stderr: `hwt: ${reply.error}\n` } : { code: 0, stdout: JSON.stringify(reply), stderr: "" };
      }
      return { code: 0, stdout: "{}", stderr: "" };
    },
  };
  const Corral = new Function(...Object.keys(globals), `${source}; return Corral;`)(...Object.values(globals));
  const tasks = [];
  const cx = { notify() {}, stop_propagation() {}, timer: { every() {} }, spawn: (f) => tasks.push(f(cx)) };
  const panel = new Corral();
  panel.init({}, cx);
  const settle = async () => { while (tasks.length) await tasks.shift(); };
  const draw = () => panel.render(cx).node;
  const click = async (id) => {
    const n = find(draw(), id);
    assert.ok(n?.click, `no button ${id}`);
    n.click({}, cx);
    await settle();
  };
  const hwtCalls = () => windows
    ? calls.exec.filter((e) => e.cmd === WIN_PY && e.args[0] === WIN_HWT).map((e) => e.args.slice(3))
    : calls.exec.filter((e) => e.args[1] === 'exec "$@"').map((e) => e.args.slice(6));
  return { panel, cx, calls, state, store, settle, draw, click, hwtCalls };
}

const configured = (over = {}) => ({
  version: "1.2.0", git: true, top: "/r/demo", main: "/r/demo", linked: false, branch: "develop",
  canonical_root: "/r", under_root: true, repository: "demo", mode: "worktree", base_branch: "origin/develop",
  dev_command: true, defaults_only: false,
  worktrees: [{ path: "/r/demo", branch: "develop", main: true, port: null, listening: false, dev_url: null },
              { path: "/w/demo/fix-1", branch: "fix", main: false, port: 4111, listening: true, dev_url: "http://box:4111" }],
  ...over,
});

test("no Corral on the machine: Install clones the pinned release for the repo's parent folder", async () => {
  const h = harness({ probe: { hwt: "" } });
  await h.settle();
  assert.match(textOf(h.draw()), /Corral isn't installed on hermes/);
  assert.match(textOf(h.draw()), /repositories in \/r\b/);
  await h.click("install");
  const install = h.calls.exec.find((e) => e.args[1].includes("install.py"));
  assert.deepEqual(install.args.slice(3), [`v${VERSION}`, "/r", "https://github.com/bfreed/herdr-corral"]);
  assert.equal(install.on, "hermes");
});

test("missing Python or git: says what to install and offers no button", async () => {
  const h = harness({ probe: { hwt: "", py: false } });
  await h.settle();
  assert.match(textOf(h.draw()), /needs Python 3\.11\+ on hermes/);
  assert.equal(find(h.draw(), "install"), undefined);
});

test("an older hwt asks for an update and runs hwt update", async () => {
  const h = harness({ probe: { version: "1.1.0" }, info: configured() });
  await h.settle();
  assert.match(textOf(h.draw()), /needs Corral 1\.2\.0 or newer; hermes has 1\.1\.0/);
  await h.click("update");
  const update = h.calls.exec.find((e) => e.args[1].includes("update"));
  assert.deepEqual(update.args.slice(1), ['exec "$1" update', "corral", "/h/.local/bin/hwt"]);
  assert.equal(update.on, "hermes");
});

test("a repository Corral doesn't know: Set up with the chosen base branch", async () => {
  const info = { ...configured({ repository: null }), suggested_base: "origin/develop", base_candidates: ["origin/main", "origin/develop"] };
  const h = harness({ info });
  await h.settle();
  assert.match(textOf(h.draw()), /Set up demo/i);
  await h.click("base-1"); // origin/main (the suggestion is listed first)
  await h.click("setup");
  assert.deepEqual(h.hwtCalls(), [["add", "/r/demo", "--base", "origin/main"]]);
});

test("main checkout: New worktree runs hwt new from the repo, then selects and names the new space", async () => {
  const h = harness({ info: configured(), hwt: { new: { repository: "demo", branch: "feat", port: 4112,
    workspace_id: "sp9", tabs: { agent: "tbA", shell: "tbS", server: "tbV" } } } });
  await h.settle();
  assert.match(textOf(h.draw()), /New worktree of demo/i);
  assert.match(textOf(h.draw()), /from origin\/develop/);
  h.panel.input.type("feat");
  await h.click("new");
  assert.deepEqual(h.hwtCalls(), [["new", "feat", "--repo", "/r/demo", "--background"]]);
  // Desktop shows the space a moment later; the panel names its tabs and shows it.
  h.state.spaces.push({ id: "hermes/sp9", machine: "hermes", folder: "/w/demo/feat", tabs: [
    { id: "hermes/tbA", index: 1, named: false }, { id: "hermes/tbV", index: 2, named: false },
    { id: "hermes/tbS", index: 3, named: false }] });
  h.panel.tidyTabs();
  const runs = h.calls.run.map(([name, args]) => `${name} ${args.id ?? ""} ${args.name ?? args.index ?? ""}`.trim());
  assert.deepEqual(runs, ["tab.rename hermes/tbA agent", "tab.rename hermes/tbV server", "tab.rename hermes/tbS shell",
                          "tab.move hermes/tbS 2", "tab.move hermes/tbV 3", "space.select hermes/sp9"]);
  h.panel.tidyTabs();
  assert.equal(h.calls.run.length, runs.length, "each only once");
});

test("tabs from Corral's labels are named, but never one the person named", () => {
  const h = harness({
    terms: [{ labels: { "herdr.tab": "tb0", "corral.tab": "agent" } }, { labels: { "herdr.tab": "tb1", "corral.tab": "shell" } }],
    tabs: [{ id: "hermes/tb0", index: 1, named: false }, { id: "hermes/tb1", index: 2, named: true }],
  });
  h.panel.tidyTabs();
  assert.deepEqual(h.calls.run, [["tab.rename", { id: "hermes/tb0", name: "agent" }]]);
});

test("a worktree: its dev URL, and Clean up asks once more before running hwt cleanup", async () => {
  const info = configured({ top: "/w/demo/fix-1", linked: true, branch: "fix" });
  const h = harness({ info, space: { folder: "/w/demo/fix-1" },
                      hwt: { cleanup: { local_branch_deleted: "fix", reason: "merged" } } });
  await h.settle();
  const text = textOf(h.draw());
  assert.match(text, /This worktree/i);
  assert.match(text, /http:\/\/box:4111/);
  assert.match(text, /Dev server running/);
  await h.click("cleanup");
  assert.deepEqual(h.hwtCalls(), [], "first click only asks");
  assert.match(textOf(h.draw()), /Only if it's merged into origin\/develop/);
  await h.click("delete");
  assert.deepEqual(h.hwtCalls(), [["cleanup", "/w/demo/fix-1"]]);
  assert.match(h.calls.notify[0].body, /Deleted fix \(merged\)/);
});

test("any worktree in the list: × asks, then Delete worktree and branch cleans it up", async () => {
  const h = harness({ info: configured(), hwt: { cleanup: { local_branch_deleted: "fix", reason: "merged" } } });
  await h.settle();
  await h.click("wt-x-0");
  assert.deepEqual(h.hwtCalls(), [], "the × only asks");
  assert.match(textOf(h.draw()), /Deletes the worktree and the branch fix/);
  await h.click("delete");
  assert.deepEqual(h.hwtCalls(), [["cleanup", "/w/demo/fix-1"]]);
  assert.doesNotMatch(textOf(h.draw()), /Deletes the worktree/);
});

test("unmerged work: Corral's reason, then Delete anyway runs the abandoning cleanup", async () => {
  const refusal = "branch fix is not merged into origin/develop and has commits not available on any other branch; rerun with --abandon --confirm fix to delete it anyway";
  const h = harness({ info: configured(), hwt: { cleanup: [{ error: refusal }, { local_branch_deleted: "fix", reason: "abandoned" }] } });
  await h.settle();
  await h.click("wt-x-0");
  await h.click("delete");
  const text = textOf(h.draw());
  assert.match(text, /Corral kept fix: branch fix is not merged into origin\/develop and has commits not available on any other branch\./);
  assert.doesNotMatch(text, /--abandon/, "no command-line advice in the panel");
  await h.click("delete-anyway");
  assert.deepEqual(h.hwtCalls(), [["cleanup", "/w/demo/fix-1"], ["cleanup", "/w/demo/fix-1", "--abandon", "--confirm", "fix"]]);
  assert.match(h.calls.notify[0].body, /Deleted fix \(abandoned\)/);
});

test("a refusal --abandon can't override is just shown, with no Delete anyway", async () => {
  const h = harness({ info: configured(), hwt: { cleanup: { error: "refusing to clean up protected branch: fix" } } });
  await h.settle();
  await h.click("wt-x-0");
  await h.click("delete");
  assert.match(textOf(h.draw()), /refusing to clean up protected branch: fix/);
  assert.equal(find(h.draw(), "delete-anyway"), undefined);
});

test("collapse narrows the panel's block to a rail and expand restores its width", async () => {
  const h = harness({ info: configured(), width: 300 });
  await h.settle();
  await h.click("collapse");
  assert.deepEqual(h.calls.run.at(-1), ["layout.resize", { id: "b7", px: 40 }]);
  assert.equal(find(h.draw(), "new"), undefined, "the rail shows only the expand button");
  assert.ok(find(h.draw(), "expand"));
  await h.click("expand");
  assert.deepEqual(h.calls.run.at(-1), ["layout.resize", { id: "b7", px: 300 }]);
  assert.ok(find(h.draw(), "new"));
});

test("clicking a worktree that has no space opens it with hwt open", async () => {
  const h = harness({ info: configured(), hwt: { open: { workspace_id: "sp7" } } });
  await h.settle();
  await h.click("wt-0");
  assert.deepEqual(h.hwtCalls(), [["open", "/w/demo/fix-1"]]);
});

test("on macOS the header clears the window buttons", () => {
  const h = harness();
  assert.equal(h.panel.lightsPadding(), 70); // (16 + 64) - the panel's own 10 px padding
});

test("a machine whose Corral ships a newer panel: Update panel copies its files over this one, main.js last", async () => {
  const dir = "/h/.local/share/herdr-corral/desktop/corral/mods/corral";
  const files = {
    "local:/mac/.config/herdr-app/mods/corral/main.js": "// Corral panel old",
    "local:/mac/.config/herdr-app/mods/corral/mod.json": "{}",
    [`hermes:${dir}/main.js`]: "// Corral panel new",
    [`hermes:${dir}/mod.json`]: "{}",
    [`hermes:${dir}/icon.svg`]: "<svg/>",
  };
  const h = harness({ files, info: configured({ panel: { version: "9.0.0", dir, files: ["icon.svg", "main.js", "mod.json"] } }) });
  await h.settle();
  assert.match(textOf(h.draw()), new RegExp(`Corral on hermes has panel 9\\.0\\.0; this one is ${esc(VERSION)}`));
  await h.click("update-panel");
  assert.deepEqual(h.calls.writes, ["/mac/.config/herdr-app/mods/corral/icon.svg", "/mac/.config/herdr-app/mods/corral/main.js"],
                   "unchanged mod.json isn't rewritten; main.js (which reloads the panel) goes last");
  assert.equal(files["local:/mac/.config/herdr-app/mods/corral/main.js"], "// Corral panel new");
});

test("no update offered for the same or an older panel, and none over a folder that isn't this panel", async () => {
  const same = harness({ info: configured({ panel: { version: VERSION, dir: "/d", files: ["main.js"] } }) });
  await same.settle();
  assert.equal(find(same.draw(), "update-panel"), undefined);
  const older = harness({ info: configured({ panel: { version: "1.1.9", dir: "/d", files: ["main.js"] } }) });
  await older.settle();
  assert.equal(find(older.draw(), "update-panel"), undefined);
  const stranger = harness({ files: { "local:/mac/.config/herdr-app/mods/corral/main.js": "// someone else's corral" },
                             info: configured({ panel: { version: "9.0.0", dir: "/d", files: ["main.js"] } }) });
  await stranger.settle();
  await stranger.click("update-panel");
  assert.deepEqual(stranger.calls.writes, []);
  assert.match(textOf(stranger.draw()), /isn't in \/mac\/\.config\/herdr-app\/mods\/corral/);
});

// ---- a space on a Windows machine (whatever system this window runs on) ----

const winConfigured = (over = {}) => configured({
  top: "C:\\r\\demo", main: "C:\\r\\demo", canonical_root: "C:\\r",
  worktrees: [{ path: "C:\\r\\demo", branch: "develop", main: true, port: null, listening: false, dev_url: null },
              { path: "C:\\w\\demo\\fix-1", branch: "fix", main: false, port: 4111, listening: true, dev_url: "http://box:4111" }],
  ...over,
});
const winSpace = { folder: "C:\\r\\demo" };

test("Windows: no sh there, so the probe runs in Python, and hwt runs as python <clone>\\hwt.py", async () => {
  const h = harness({ windows: true, space: winSpace, info: winConfigured(),
                      hwt: { new: { repository: "demo", branch: "feat", workspace_id: "sp9", tabs: {} } } });
  await h.settle();
  assert.deepEqual(h.calls.exec.map((e) => e.cmd), ["sh", "py"], "tries sh, then the py launcher");
  assert.deepEqual(h.calls.exec[1].args.slice(0, 2), ["-3", "-c"]);
  assert.equal(h.calls.exec[1].args.at(-1), "C:\\r\\demo");
  assert.match(textOf(h.draw()), /New worktree of demo/i);
  h.panel.input.type("feat");
  await h.click("new");
  assert.deepEqual(h.hwtCalls(), [["new", "feat", "--repo", "C:\\r\\demo", "--background"]]);
  assert.ok(h.calls.exec.every((e) => e.on === "hermes"));
});

test("Windows: the machine is remembered, and later probes use the Python that answered", async () => {
  const h = harness({ windows: true, pythons: ["python"], space: winSpace, info: winConfigured() });
  await h.settle();
  assert.deepEqual(h.calls.exec.map((e) => e.cmd), ["sh", "py", "python"]);
  h.calls.exec.length = 0;
  h.panel.at = 0;
  h.panel.tick(h.cx);
  await h.settle();
  assert.deepEqual(h.calls.exec.map((e) => e.cmd), [WIN_PY], "no sh, straight to the Python it found");
});

test("Windows with Git's sh on PATH is still Windows", async () => {
  const h = harness({ windows: true, gitSh: true, space: winSpace, info: winConfigured() });
  await h.settle();
  assert.deepEqual(h.calls.exec.map((e) => e.cmd), ["sh", "py"]);
  assert.match(textOf(h.draw()), /New worktree of demo/i);
});

test("Windows paths: a space whose folder differs in case and separators still shows its worktree as open", async () => {
  const h = harness({ windows: true, space: winSpace, info: winConfigured() });
  h.state.spaces.push({ id: "hermes/sp2", machine: "hermes", folder: "c:/W/demo/fix-1/src", tabs: [{ id: "hermes/tb5", index: 1 }] });
  h.state.panes.push({ tab: "hermes/tb5", cwd: "c:/W/demo/fix-1/src" });
  await h.settle();
  assert.match(textOf(find(h.draw(), "wt-0")), /fix 4111 open/);
  await h.click("wt-0");
  assert.deepEqual(h.calls.run.at(-1), ["space.select", { id: "hermes/sp2" }]);
  assert.deepEqual(h.hwtCalls(), [], "selected, not reopened");
});

test("Windows: this worktree is found though git and hwt write its path differently", async () => {
  const info = winConfigured({ top: "C:\\w\\demo\\fix-1", linked: true, branch: "fix" });
  info.worktrees[1].path = "C:/w/demo/fix-1"; // as an older hwt passes git's on
  const h = harness({ windows: true, space: { folder: "C:\\w\\demo\\fix-1" }, info });
  await h.settle();
  assert.match(textOf(h.draw()), /Dev server running/);
});

test("Windows: Install runs the installer in Python, for the repo's parent folder", async () => {
  const h = harness({ windows: true, probe: { hwt: "" }, space: winSpace });
  await h.settle();
  assert.match(textOf(h.draw()), /repositories in C:\\r\b/);
  await h.click("install");
  const install = h.calls.exec.find((e) => e.args.some((a) => String(a).includes('"--desktop"')));
  assert.equal(install.cmd, WIN_PY);
  assert.deepEqual(install.args.slice(2), [`v${VERSION}`, "C:\\r", "https://github.com/bfreed/herdr-corral"]);
});

test("Windows without Python: says so, and still knows the folder's repository", async () => {
  const h = harness({ windows: true, pythons: [], probe: { hwt: "" }, space: winSpace });
  await h.settle();
  assert.deepEqual(h.calls.exec.map((e) => e.cmd), ["sh", "py", "python", "python3", "git"]);
  assert.match(textOf(h.draw()), /needs Python 3\.11\+ on hermes/);
  assert.equal(find(h.draw(), "install"), undefined);
});

test("Windows: Start dev server types hwt dev through hc, or opens a tab running python hwt.py dev", async () => {
  const info = winConfigured({ top: "C:\\w\\demo\\fix-1", linked: true, branch: "fix" });
  info.worktrees[1].listening = false;
  const h = harness({ windows: true, space: { folder: "C:\\w\\demo\\fix-1" }, info });
  await h.settle();
  await h.click("dev");
  const hc = h.calls.exec.filter((e) => e.cmd === WIN_HC).map((e) => e.args);
  assert.deepEqual(hc, [["ls", "--json", "--label", "herdr.space=sp1", "--label", "corral.tab=server"],
                       ["send", "t9", `& '${WIN_PY}' '${WIN_HWT}' dev`]]);
  assert.equal(h.panel.hwtArgv("dev").join(" "), `${WIN_PY} ${WIN_HWT} dev`);
});

test("Windows: Update panel reads and writes with Windows separators", async () => {
  const here = "C:\\Users\\b\\.config\\herdr-app\\mods\\corral";
  const dir = "C:\\Users\\b\\.local\\share\\herdr-corral\\desktop\\corral\\mods\\corral";
  const files = {
    [`local:${here}\\main.js`]: "// Corral panel old",
    [`hermes:${dir}\\main.js`]: "// Corral panel new",
  };
  const h = harness({ windows: true, space: winSpace, home: "C:\\Users\\b\\.config\\herdr-app", files,
                      info: winConfigured({ panel: { version: "9.0.0", dir, files: ["main.js"] } }) });
  await h.settle();
  await h.click("update-panel");
  assert.deepEqual(h.calls.writes, [`${here}\\main.js`]);
});

test("Windows: an hwt error with Windows line endings reads cleanly", async () => {
  const h = harness({ windows: true, space: winSpace, info: winConfigured(), hwt: { cleanup: { error: "refusing to clean up protected branch: fix" } } });
  await h.settle();
  await h.click("wt-x-0");
  await h.click("delete");
  assert.match(textOf(h.draw()), /refusing to clean up protected branch: fix(?!\r)/);
  assert.doesNotMatch(textOf(h.draw()), /\r/);
});

test("a POSIX machine whose exec fails for another reason isn't taken for Windows", async () => {
  const h = harness({ execError: "hc exec: cannot start hc: program not found", info: configured() });
  await h.settle();
  assert.deepEqual(h.calls.exec.map((e) => e.cmd), ["sh"], "no Python probes");
  assert.match(textOf(h.draw()), /cannot start hc: program not found/);
  h.panel.at = 0;
  h.panel.tick(h.cx);
  await h.settle();
  assert.deepEqual(h.calls.exec.map((e) => e.cmd), ["sh", "sh"], "still tried as POSIX");
});

test("Windows: a Python too old is passed over for one that's new enough", async () => {
  const h = harness({ windows: true, oldPythons: ["py"], pythons: ["python"], space: winSpace, info: winConfigured() });
  await h.settle();
  assert.deepEqual(h.calls.exec.map((e) => e.cmd), ["sh", "py", "python"]);
  assert.equal(h.panel.st.python, WIN_PY);
  assert.match(textOf(h.draw()), /New worktree of demo/i);
});

test("Windows: with only a Python too old, says Python 3.11+ is needed", async () => {
  const h = harness({ windows: true, oldPythons: ["py"], pythons: [], probe: { hwt: "" }, space: winSpace });
  await h.settle();
  assert.match(textOf(h.draw()), /needs Python 3\.11\+ on hermes/);
});

// The Windows probe is Python: run it for real (any OS) against a repository
// and the hwt.cmd install.py writes, with this checkout's hwt.py.
test("the Windows probe script finds the repository and runs hwt through its hwt.cmd", async (t) => {
  const python = process.platform === "win32" ? "python" : "python3";
  let interpreter;
  try { interpreter = execFileSync(python, ["-c", "import sys; print(sys.executable)"], { encoding: "utf8" }).trim(); }
  catch { return t.skip(`no ${python}`); }
  const probe = source.match(/const WIN_PROBE = `([\s\S]*?)`;/)[1];
  const home = await mkdtemp(pjoin(tmpdir(), "corral-probe-"));
  const repo = pjoin(home, "repos", "demo");
  await mkdir(repo, { recursive: true });
  execFileSync("git", ["init", "-q", repo]);
  const hwtPy = new URL("../../hwt.py", import.meta.url);
  const script = process.platform === "win32" ? hwtPy.pathname.replace(/^\//, "").replace(/\//g, "\\") : hwtPy.pathname;
  await mkdir(pjoin(home, ".local", "bin"), { recursive: true });
  await writeFile(pjoin(home, ".local", "bin", "hwt.cmd"), `@"${interpreter}" -X utf8 "${script}" %*\r\n`);
  const git = execFileSync(python, ["-c", "import shutil; print(shutil.which('git'))"], { encoding: "utf8" }).trim();
  const env = { ...process.env, HOME: home, USERPROFILE: home, PATH: pdirname(git) };
  const out = execFileSync(interpreter, ["-c", probe, repo], { env, encoding: "utf8" }).replace(/\r\n/g, "\n");
  const lines = Object.fromEntries(out.split("\n@@info\n")[0].split("\n").filter(Boolean).map((l) => [l.slice(0, l.indexOf("=")), l.slice(l.indexOf("=") + 1)]));
  assert.equal(lines.os, "Windows");
  assert.equal(lines.py, "1");
  assert.equal(lines.git, "1");
  assert.match(lines.common, /[\\/]repos[\\/]demo[\\/]\.git$/);
  assert.deepEqual(JSON.parse(lines.run), [interpreter, "-X", "utf8", script]);
  assert.equal(lines.version, `hwt ${VERSION}`);
  assert.ok(out.includes("\n@@info\n"), "then hwt info's output");
});
