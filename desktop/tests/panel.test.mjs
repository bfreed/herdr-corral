// The Corral panel's logic, without Herdr: every state it draws, what its
// buttons run, and how it names Corral's tabs. Run: node --test desktop/tests
// Element builders are stubbed (they keep text, ids and click handlers);
// `herdr-app check desktop/corral` checks the real native elements.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const source = (await readFile(new URL("../corral/main.js", import.meta.url), "utf8"))
  .replace(/^import [^;]+;\n/gm, "")
  .replace("export default class Corral", "class Corral");

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

function harness({ probe = {}, info = null, hwt = {}, space = {}, terms = [], tabs = null, width = 260 } = {}) {
  const calls = { exec: [], run: [], notify: [] };
  const store = {};
  const block = { id: "b7", type: "block", content: { mod: "corral" }, size: { px: width } };
  const theSpace = { id: "hermes/sp1", machine: "hermes", folder: "/r/demo", named: false,
                     tabs: tabs ?? [{ id: "hermes/tb0", index: 1, named: false }], ...space };
  const state = { spaces: [theSpace], panes: [{ tab: theSpace.tabs[0]?.id, cwd: theSpace.folder }], terms };
  const lines = [`hwt=${probe.hwt ?? "/h/.local/bin/hwt"}`, probe.git === false ? "" : "git=1",
                 `py=${probe.py === false ? 0 : 1}`, `common=${probe.common ?? "/r/demo/.git"}`];
  if (probe.hwt !== "") lines.push(`version=hwt ${probe.version ?? "1.2.0"}`, "@@info", JSON.stringify(info));
  const globals = {
    View: class {}, div: element, v_flex: element, h_flex: element, svg: element,
    shared: (k) => store[k], setShared: (k, v) => { store[k] = v; return null; },
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
  const hwtCalls = () => calls.exec.filter((e) => e.args[1] === 'exec "$@"').map((e) => e.args.slice(6));
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
  assert.deepEqual(install.args.slice(3), ["v1.2.0", "/r", "https://github.com/bfreed/herdr-corral"]);
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
