// Corral panel: Corral's worktree workflow for the focused space, run on
// that space's machine. It shows the space's repository and worktrees, and
// creates, opens and cleans up worktrees with `hwt`; it offers to install
// Corral there, and to set the repository up, when either is missing.
// It also names the tabs Corral starts (agent, shell, server): herdr-core
// can't name tabs, so Corral labels their terminals corral.tab=<name>.
// Every Corral install carries this panel's files; when a machine's are
// newer, the panel offers to update itself from them.
// A machine may run Windows, whichever system this window runs on. There the
// panel runs programs directly (no sh), and hwt as python <clone>\hwt.py.

import { View, div, svg } from "gpui-kit";
import { v_flex, h_flex, InputState, Input } from "gpui-base";
import { exec, spaces, panes, focused, terminals, run, theme, notify, window, layout, settings, shared, setShared,
  readTextFile, writeTextFile, appHome } from "herdr";

const VERSION = "1.4.4"; // this panel; ships with the same Corral version
const RELEASE = `v${VERSION}`; // the Corral release Install checks out
const REPO = "https://github.com/bfreed/herdr-corral";
const NEEDS = [1, 2, 0]; // the oldest hwt this panel works with
const TAB_ORDER = ["agent", "shell", "server"];
const REFRESH_MS = 15000; // ports and dev servers change on their own
const RAIL = 40; // collapsed width
const TOGGLE_ICON = "mods/workspace-chrome/sidebar.svg"; // Herdr's own sidebar toggle

// Runs a program on a machine; never throws. Windows line endings become \n.
const run1 = (program, args, on, timeout) =>
  exec(program, args, { on: on ?? null, timeout: timeout ?? 30000 })
    .then((r) => ({ ...r, stdout: (r.stdout ?? "").replace(/\r\n/g, "\n"), stderr: (r.stderr ?? "").replace(/\r\n/g, "\n") }))
    .catch((e) => ({ code: -1, stdout: "", stderr: String(e?.message ?? e) }));

// A login shell, so the person's PATH (~/.local/bin) applies on POSIX machines.
const sh = (script, args, on, timeout) => run1("sh", ["-lc", script, "corral", ...args], on, timeout);

// What each machine is: "posix", or { python } for Windows, learned from its
// first probe. A space's machine can run Windows whatever this window runs on.
const systems = new Map();
const NO_SH = /cannot (run|start) sh: program not found|'sh' is not recogni[sz]ed/i; // only sh missing
const NOT_FOUND = /program not found|not recogni[sz]ed|cannot find the file/i;
const PYTHONS = [["py", "-3"], ["python"], ["python3"]]; // Windows' usual names, in that order

// What the machine has, then (with a current hwt) what Corral knows about the folder.
const PROBE = `f="$1"
echo "os=$(uname -s 2>/dev/null)"
h=$(command -v hwt 2>/dev/null || true)
[ -z "$h" ] && [ -x "$HOME/.local/bin/hwt" ] && h="$HOME/.local/bin/hwt"
echo "hwt=$h"
command -v git >/dev/null 2>&1 && echo "git=1"
python3 -c 'import sys; print("py=%d" % (sys.version_info >= (3, 11)))' 2>/dev/null
echo "common=$(git -C "$f" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)"
[ -n "$h" ] && echo "version=$("$h" --version 2>/dev/null)"
[ -n "$h" ] && echo "@@info" && "$h" --frontend desktop info "$f" 2>&1
true`;

const INSTALL = `set -e
d="$HOME/.local/share/herdr-corral"
if [ -d "$d/.git" ]; then git -C "$d" fetch -q --tags origin; git -C "$d" checkout -q "$1"
else git clone -q --branch "$1" "$3" "$d"; fi
python3 "$d/install.py" --desktop --repos-root "$2" --agent-kind claude`;

// PROBE for Windows, in Python (run as python -c). It finds hwt through the
// hwt.cmd install.py writes ("<python>" "<clone>\hwt.py" %*) and prints how
// to run it, since a program started without a shell can't run a .cmd file.
const WIN_PROBE = `import json, os, re, shutil, subprocess, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # the panel reads UTF-8
f = sys.argv[1]
home = os.path.expanduser("~")
def out(argv):
    try:
        r = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
        return r.returncode, r.stdout.decode("utf-8", "replace")
    except Exception as e:
        return -1, str(e)
print("os=Windows")
print("python=" + sys.executable)
print("py=%d" % (sys.version_info >= (3, 11)))
git = shutil.which("git")
print("git=%d" % bool(git))
code, common = out([git, "-C", f, "rev-parse", "--path-format=absolute", "--git-common-dir"]) if git else (1, "")
print("common=" + (common.strip() if code == 0 else ""))
local = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
hc = shutil.which("hc") or next((p for p in (os.path.join(local, "Programs", "herdr-core", n) for n in ("hc.exe", "herdr-core.exe")) if os.path.isfile(p)), "")
print("hc=" + hc)
h = shutil.which("hwt") or os.path.join(home, ".local", "bin", "hwt.cmd")
run = []
if os.path.isfile(h):
    if h.lower().endswith((".cmd", ".bat")):
        with open(h, "rb") as fh:
            raw = fh.read()
        # install.py writes it in the OEM code page, or as UTF-8 after a chcp 65001 line.
        text = raw.decode("utf-8" if raw.startswith(b"@chcp 65001") or os.name != "nt" else "oem", "replace")
        quoted = [q.replace("%%", "%") for q in re.findall(r'"([^"]+)"', text)]
        script = next((q for q in quoted if q.lower().endswith("hwt.py")), "")
        if script and os.path.isfile(script):
            interpreter = quoted[0] if quoted[0] != script and os.path.isfile(quoted[0]) else sys.executable
            run = [interpreter, "-X", "utf8", script]
    else:
        run = [h]
print("hwt=" + (h if run else ""))
print("run=" + json.dumps(run))
if run:
    print("version=" + out(run + ["--version"])[1].strip())
    print("@@info")
    sys.stdout.write(out(run + ["--frontend", "desktop", "info", f])[1])
`;

const WIN_INSTALL = `import os, subprocess, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")
ref, root, repo = sys.argv[1:4]
d = os.path.join(os.path.expanduser("~"), ".local", "share", "herdr-corral")
def go(*argv):
    if subprocess.run(list(argv)).returncode:
        sys.exit("%s %s failed" % argv[:2])
if os.path.isdir(os.path.join(d, ".git")):
    go("git", "-C", d, "fetch", "-q", "--tags", "origin")
    go("git", "-C", d, "checkout", "-q", ref)
else:
    go("git", "clone", "-q", "--branch", ref, repo, d)
sys.exit(subprocess.run([sys.executable, os.path.join(d, "install.py"), "--desktop", "--repos-root", root, "--agent-kind", "claude"]).returncode)
`;

const versionOf = (text) => (text.match(/(\d+)\.(\d+)\.(\d+)/) ?? []).slice(1).map(Number);
const atLeast = (have, need) => {
  for (let i = 0; i < need.length; i++) if ((have[i] ?? 0) !== need[i]) return (have[i] ?? 0) > need[i];
  return true;
};
// Paths. POSIX ones are compared exactly. Windows ones (C:\…, \\server\share)
// arrive from Herdr, git and hwt with either separator and any case.
const isWin = (p) => /^[A-Za-z]:([\\/]|$)|^[\\/]{2}[^\\/]/.test(p ?? "");
const winKey = (p) => p.replace(/^[\\/]{2}\?[\\/]/, "").replace(/\//g, "\\").replace(/\\+$/, "").toLowerCase();
const dirname = (p) => {
  if (!isWin(p)) return p.replace(/\/+$/, "").replace(/\/[^/]*$/, "") || "/";
  const q = p.replace(/\//g, "\\").replace(/\\+$/, "");
  const head = q.includes("\\") ? q.slice(0, q.lastIndexOf("\\")) : q;
  return /^[A-Za-z]:$/.test(head) ? `${head}\\` : head;
};
const basename = (p) => (isWin(p) ? p.replace(/[\\/]+$/, "").split(/[\\/]/) : p.replace(/\/+$/, "").split("/")).pop();
const samePath = (a, b) => (isWin(a) || isWin(b) ? winKey(a ?? "") === winKey(b ?? "") : a === b);
const inside = (p, dir) => {
  if (!isWin(p) && !isWin(dir)) return p === dir || p.startsWith(dir + "/");
  const [a, d] = [winKey(p), winKey(dir)];
  return a === d || a.startsWith(d + "\\");
};
const join = (dir, ...names) => [dir.replace(/[\\/]+$/, ""), ...names].join(isWin(dir) ? "\\" : "/");
const local = (id) => String(id).split("/").pop(); // remote ids carry a machine/ prefix
const errorText = (r) => (r.stderr || r.stdout || "").trim().split("\n").filter(Boolean).slice(-3).join(" ").replace(/^hwt: /, "").slice(0, 400);

// The layout block showing this panel (Herdr docks it with an id of its own).
function blockOf(node) {
  if (node?.content?.mod === "corral") return node;
  for (const child of node?.children ?? []) {
    const found = blockOf(child);
    if (found) return found;
  }
  return null;
}

// Where a space is now: its first terminal's live folder, else where it was made.
function place(s, paneList) {
  const tabs = new Set((s.tabs ?? []).map((c) => c.id));
  return paneList.find((p) => tabs.has(p.tab) && p.cwd)?.cwd || s.folder || null;
}

export default class Corral extends View {
  init(_props, cx) {
    this.key = null; // machine + folder the shown state is for
    this.st = null; // { key, error?, hwt, git, py, version, root, info }
    this.at = 0;
    this.busy = ""; // what's running, for the person
    this.error = "";
    this.deleting = null; // { path, branch, refused? }: a Clean up being confirmed
    this.base = null; // Set up's chosen base branch
    this.named = new Set(); // tab ids already named
    this.ordered = new Set(); // space ids already put in order
    this.pending = []; // { machine, space, tabs, select, until } from our own hwt runs
    this.input = InputState.new({ placeholder: "new branch name" });
    this.input.on("submit", (_e, cx) => this.create(cx));
    cx.timer.every(1000, (cx) => this.tick(cx));
    this.tick(cx);
  }

  view() {
    const id = focused()?.space;
    const s = id && spaces().find((x) => x.id === id);
    if (!s) return null;
    const folder = place(s, panes());
    return folder ? { space: s, machine: s.machine ?? null, folder } : null;
  }

  tick(cx) {
    this.tidyTabs();
    const v = this.view();
    const key = v ? `${v.machine ?? ""}|${v.folder}` : "";
    if (this.busy || this.collapsed()) return;
    if (key !== this.key || Date.now() - this.at > REFRESH_MS) {
      if (key !== this.key) { this.error = ""; this.deleting = null; this.base = null; }
      this.key = key;
      this.at = Date.now();
      if (v) this.refresh(cx, v, key);
      else { this.st = null; cx.notify(); }
    }
  }

  // Runs the probe on a machine: through sh, or on Windows (no sh there, or
  // Git's sh, which says MINGW) through the first Python that answers.
  async probe(machine, folder) {
    const id = machine ?? "";
    if (systems.get(id) !== undefined && systems.get(id) !== "posix") return this.probeWindows(machine, folder);
    const r = await sh(PROBE, [folder], machine);
    const windows = (r.code !== 0 && NO_SH.test(r.stderr)) || /^os=(MINGW|MSYS|CYGWIN)/m.test(r.stdout);
    if (!windows) { systems.set(id, "posix"); return r; }
    systems.set(id, {});
    return this.probeWindows(machine, folder);
  }

  async probeWindows(machine, folder) {
    const id = machine ?? "";
    const known = systems.get(id)?.python;
    let fallback = null; // an answer from a Python too old (or one that failed)
    let failure = null; // a failure other than "no such program"
    for (const [program, ...flags] of known ? [[known], ...PYTHONS] : PYTHONS) {
      const r = await run1(program, [...flags, "-c", WIN_PROBE, folder], machine, 120000);
      const python = r.stdout.match(/^python=(.+)$/m)?.[1]?.trim();
      if (python && r.code === 0 && /^py=1$/m.test(r.stdout)) { systems.set(id, { python }); return r; }
      if (python) fallback ??= r;
      else if (!NOT_FOUND.test(r.stderr)) failure ??= r;
    }
    if (fallback) return fallback;
    if (failure) return failure;
    // No Python: say what's missing (git too, if it isn't there).
    const git = await run1("git", ["-C", folder, "rev-parse", "--path-format=absolute", "--git-common-dir"], machine);
    const common = git.code === 0 ? git.stdout.trim() : "";
    const found = git.code !== -1 || !NOT_FOUND.test(git.stderr);
    return { code: 0, stdout: `os=Windows\npy=0\ngit=${found ? 1 : 0}\ncommon=${common}\n`, stderr: "" };
  }

  refresh(cx, v, key) {
    cx.spawn(async (cx) => {
      const r = await this.probe(v.machine, v.folder);
      if (key !== this.key) return; // the person moved on
      const st = { key, hwt: "", git: false, py: false, version: [], root: null, info: null, windows: false, run: [], hc: "" };
      if (r.code !== 0) st.error = errorText(r) || `Can't reach ${v.machine ?? "this computer"}.`;
      const [head, tail] = (r.stdout ?? "").split("\n@@info\n");
      for (const line of head.split("\n")) {
        const at = line.indexOf("=");
        const [k, val] = [line.slice(0, at), line.slice(at + 1).trim()];
        if (k === "os") st.windows = val === "Windows";
        if (k === "hwt") st.hwt = val;
        if (k === "git") st.git = val === "1";
        if (k === "py") st.py = val === "1";
        if (k === "python") st.python = val;
        if (k === "hc") st.hc = val;
        if (k === "run") { try { st.run = JSON.parse(val); } catch { st.run = []; } }
        if (k === "version") st.version = versionOf(val);
        if (k === "common" && val) st.root = dirname(/[\\/]\.git$/.test(val) ? val.slice(0, -5) : val);
      }
      if (tail && atLeast(st.version, NEEDS)) {
        try { st.info = JSON.parse(tail); } catch { st.error = tail.trim().slice(0, 400); }
      }
      this.st = st;
      cx.notify();
    });
  }

  // Runs hwt on the shown space's machine: `hwt …` through sh, or on Windows
  // python <clone>\hwt.py ….
  runHwt(args, timeout) {
    const on = this.view()?.machine;
    if (this.st.windows) return run1(this.st.run[0], [...this.st.run.slice(1), ...args], on, timeout);
    return sh('exec "$@"', [this.st.hwt, ...args], on, timeout);
  }

  // hwt's argv for a new tab on the space's machine (Windows can't start hwt.cmd there).
  hwtArgv(...args) {
    return this.st?.windows && this.st.run.length ? [...this.st.run, ...args] : ["hwt", ...args];
  }

  // Runs hwt on the shown space's machine; resolves with its JSON, or throws its message.
  async hwt(args, timeout) {
    const r = await this.runHwt(["--frontend", "desktop", ...args], timeout);
    if (r.code !== 0) throw new Error(errorText(r) || `hwt ${args[0]} failed`);
    try { return JSON.parse(r.stdout); } catch { return {}; }
  }

  act(cx, label, work) {
    if (this.busy) return;
    this.busy = label;
    this.error = "";
    cx.notify();
    cx.spawn(async (cx) => {
      try { await work(); }
      catch (e) { this.error = String(e?.message ?? e); }
      this.busy = "";
      this.at = 0; // refresh now
      cx.notify();
    });
  }

  // Remember a space hwt just made: select it, name its tabs, when Desktop shows it.
  expect(out, select) {
    if (out?.workspace_id) {
      this.pending.push({ machine: this.view()?.machine ?? null, space: local(out.workspace_id),
                          tabs: out.tabs ?? {}, select, until: Date.now() + 30000 });
    }
  }

  // Names Corral's tabs and puts them in agent, shell, server order (once each),
  // and selects a space Corral just made once it shows up.
  tidyTabs() {
    const now = Date.now();
    this.pending = this.pending.filter((p) => p.until > now);
    const want = new Map();
    for (const t of terminals()) {
      const l = t.labels ?? {};
      if (l["corral.tab"] && l["herdr.tab"]) want.set(l["herdr.tab"], l["corral.tab"]);
    }
    for (const p of this.pending) for (const [name, tab] of Object.entries(p.tabs)) want.set(local(tab), name);
    if (want.size === 0) return;
    for (const s of spaces()) {
      const tabs = s.tabs ?? [];
      const ours = tabs.filter((tab) => want.has(local(tab.id)));
      for (const tab of ours) {
        if (!tab.named && !this.named.has(tab.id)) {
          this.named.add(tab.id);
          run("tab.rename", { id: tab.id, name: want.get(local(tab.id)) });
        }
      }
      const labels = new Set(ours.map((tab) => want.get(local(tab.id))));
      if (!this.ordered.has(s.id) && ours.length === tabs.length && TAB_ORDER.every((n) => labels.has(n))) {
        this.ordered.add(s.id);
        TAB_ORDER.forEach((name, i) => {
          const tab = ours.find((x) => want.get(local(x.id)) === name);
          if (tab && tab.index !== i + 1) run("tab.move", { id: tab.id, index: i + 1 });
        });
      }
      const p = this.pending.find((x) => x.select && x.space === local(s.id) && (x.machine ?? null) === (s.machine ?? null));
      if (p) { p.select = false; run("space.select", { id: s.id }); }
    }
  }

  // The space showing a checkout (the deepest checkout its folder is in wins).
  spaceFor(path, all, machine) {
    const paneList = panes();
    return spaces().find((s) => (s.machine ?? null) === machine && (() => {
      const at = place(s, paneList);
      if (!at || !inside(at, path)) return false;
      return !all.some((c) => !samePath(c.path, path) && c.path.length > path.length && inside(at, c.path));
    })());
  }

  create(cx) {
    const branch = this.input.value().trim();
    const info = this.st?.info;
    if (!branch || !info?.main) return;
    this.act(cx, `Creating ${branch}…`, async () => {
      const out = await this.hwt(["new", branch, "--repo", info.main, "--background"], 600000);
      this.input.set_value("");
      this.expect(out, true);
      notify({ title: "Corral", body: `${out.repository}/${out.branch} ready${out.port ? ` on port ${out.port}` : ""}`, level: "success" });
    });
  }

  open(cx, checkout, all) {
    const v = this.view();
    const there = this.spaceFor(checkout.path, all, v?.machine ?? null);
    if (there) return run("space.select", { id: there.id });
    this.act(cx, `Opening ${checkout.branch ?? basename(checkout.path)}…`, async () => {
      this.expect(await this.hwt(["open", checkout.path], 600000), true);
    });
  }

  // Corral's merge-checked cleanup. When Corral keeps a branch for a reason
  // its --abandon would override (unmerged commits, uncommitted changes), the
  // panel says why and offers "Delete <branch> anyway" as a separate click.
  cleanup(cx, c, abandon = false) {
    this.act(cx, `${abandon ? "Deleting" : "Cleaning up"} ${c.branch}…`, async () => {
      try {
        const out = await this.hwt(["cleanup", c.path, ...(abandon ? ["--abandon", "--confirm", c.branch] : [])], 300000);
        this.deleting = null;
        notify({ title: "Corral", body: `Deleted ${out.local_branch_deleted} (${out.reason})`, level: "success" });
      } catch (e) {
        const message = String(e?.message ?? e);
        if (abandon || !message.includes("--abandon")) { this.deleting = null; throw e; }
        this.deleting = { path: c.path, branch: c.branch, refused: message.replace(/;?\s*rerun with --abandon.*$/, "").trim() };
      }
    });
  }

  // Copies the panel files a machine's Corral ships over this panel's own;
  // Herdr reloads the panel as main.js lands, so it goes last.
  updatePanel(cx, panel) {
    const on = this.view()?.machine;
    const here = join(appHome(), "mods", "corral");
    this.act(cx, "Updating the panel…", async () => {
      const mine = await readTextFile(join(here, "main.js"));
      if (!mine?.startsWith("// Corral panel")) throw new Error(`This panel isn't in ${here}; update it by hand.`);
      const files = [...panel.files.filter((f) => f !== "main.js"), "main.js"];
      for (const name of files) {
        const text = await readTextFile(join(panel.dir, name), { on });
        if (text === null) throw new Error(`Couldn't read ${name} from Corral on ${on ?? "this computer"}.`);
        const old = await readTextFile(join(here, name));
        if (name === "main.js") notify({ title: "Corral", body: `Panel updated to ${panel.version}`, level: "success" });
        if (old !== text) await writeTextFile(join(here, name), text, old);
      }
    });
  }

  askDelete(cx, c) {
    if (this.busy || !c.branch) return;
    this.deleting = { path: c.path, branch: c.branch };
    cx.notify();
  }

  // Collapsing narrows the panel's own layout block to a rail, as Herdr's sidebar does.
  collapsed() {
    return (blockOf(layout())?.size?.px ?? Infinity) < 120;
  }

  toggle() {
    const block = blockOf(layout());
    if (!block) return;
    if (this.collapsed()) {
      run("layout.resize", { id: block.id, px: shared("corral.width") ?? 260 });
      this.at = 0; // catch up on what changed while folded
    } else {
      setShared("corral.width", block.size?.px ?? 260);
      run("layout.resize", { id: block.id, px: RAIL });
    }
  }

  startServer(cx, info) {
    const v = this.view();
    const win = this.st.windows;
    const hc = (...args) => (win ? run1(this.st.hc || "hc", args, v.machine) : null);
    this.act(cx, "Starting the dev server…", async () => {
      const found = win ? await hc("ls", "--json", "--label", `herdr.space=${local(v.space.id)}`, "--label", "corral.tab=server")
        : await sh('hc ls --json --label "herdr.space=$1" --label corral.tab=server', [local(v.space.id)], v.machine);
      const address = (() => { try { return JSON.parse(found.stdout).terminals[0]?.address; } catch { return null; } })();
      if (address) {
        // Typed into the tab's shell, which finds hwt on its PATH. A Windows tab's
        // PowerShell may predate the PATH Install set, so it gets hwt in full.
        const typed = win && this.st.run.length ? `& ${this.st.run.map((a) => `'${a.replace(/'/g, "''")}'`).join(" ")} dev` : "hwt dev";
        const sent = win ? await hc("send", address, typed) : await sh('hc send "$1" "hwt dev"', [address], v.machine);
        if (sent.code !== 0) throw new Error(errorText(sent) || "Couldn't reach the server tab.");
      } else {
        run("tab.new", { space: v.space.id, name: "server", folder: info.top, argv: this.hwtArgv("dev") });
      }
    });
  }

  install(cx) {
    const st = this.st;
    this.act(cx, "Installing Corral…", async () => {
      const on = this.view()?.machine;
      const r = st.windows ? await run1(st.python, ["-c", WIN_INSTALL, RELEASE, st.root, REPO], on, 600000)
        : await sh(INSTALL, [RELEASE, st.root, REPO], on, 600000);
      if (r.code !== 0) throw new Error(errorText(r) || "The install failed.");
      notify({ title: "Corral", body: `Installed on ${this.view()?.machine ?? "this computer"}`, level: "success" });
    });
  }

  setUp(cx, info, base) {
    this.act(cx, `Setting up ${basename(info.main)}…`, async () => {
      await this.hwt(["add", info.main, "--base", base]);
    });
  }

  settings(info) {
    run("tab.new", { space: this.view()?.space.id, name: "corral setup", folder: info.main, argv: this.hwtArgv("init") });
  }

  update(cx) {
    // Not this.hwt: an older hwt doesn't know --frontend.
    this.act(cx, "Updating Corral…", async () => {
      const r = this.st.windows ? await this.runHwt(["update"], 120000)
        : await sh('exec "$1" update', [this.st.hwt], this.view()?.machine, 120000);
      if (r.code !== 0) throw new Error(errorText(r) || "The update failed.");
    });
  }

  openUrl(url) {
    const p = window().platform;
    if (p === "macos") exec("open", [url]).catch(() => null);
    else if (p === "windows") exec("cmd", ["/c", "start", "", url]).catch(() => null);
    else exec("xdg-open", [url]).catch(() => null);
  }

  // ---- drawing ----

  button(id, label, onClick, { primary = false, danger = false, disabled = false } = {}) {
    const t = theme();
    const off = disabled || Boolean(this.busy);
    const bg = danger ? t.blocked : primary ? t.solid : t.raised;
    const fg = danger || primary ? t.solid_foreground : t.foreground;
    return h_flex().id(id).h(24).px(9).flex_none().items_center().justify_center().rounded(5)
      .text_size(12).bg(bg).text_color(fg).border(1).border_color(danger ? t.blocked : primary ? t.solid : t.border)
      .when(off, (el) => el.opacity(0.5))
      .when(!off, (el) => el.cursor_pointer().hover((s) => s.opacity(0.85)).on_click((_e, cx) => onClick(cx)))
      .child(label);
  }

  note(text, color) {
    return div().text_size(12).text_color(color ?? theme().muted).child(text);
  }

  section(title, ...children) {
    const t = theme();
    return v_flex().gap(6)
      .child(div().text_size(11).font_semibold().text_color(t.dim).child(title.toUpperCase()))
      .children(children);
  }

  toggleButton(id, tip) {
    const t = theme();
    return div().id(id).size(24).flex_none().flex().items_center().justify_center().rounded(5).cursor_pointer()
      .text_color(t.dim).hover((s) => s.bg(t.hover).text_color(t.foreground)).tooltip(tip)
      .on_click(() => this.toggle()).child(svg(TOGGLE_ICON).size(15));
  }

  // The confirm (or the refusal and "Delete anyway") under a worktree being cleaned up.
  deleteBox(c, base) {
    const t = theme();
    const d = this.deleting;
    if (!d || d.path !== c.path) return null;
    const cancel = this.button("delete-cancel", "Cancel", (cx) => { this.deleting = null; cx.notify(); });
    const box = v_flex().gap(6).p(8).rounded(5).border(1).border_color(t.blocked);
    if (d.refused) {
      return box
        .child(this.note(`Corral kept ${d.branch}: ${d.refused}.`, t.foreground))
        .child(this.note("Delete anyway also deletes its uncommitted changes and the commits only it has. This can't be undone."))
        .child(h_flex().gap(6).flex_wrap()
          .child(this.button("delete-anyway", `Delete ${d.branch} anyway`, (cx) => this.cleanup(cx, c, true), { danger: true }))
          .child(cancel));
    }
    return box
      .child(this.note(`Deletes the worktree and the branch ${d.branch}, here and on the remote. Only if it's merged into ${base} (or has nothing of its own) and has no uncommitted changes; otherwise Corral keeps it and says why.`))
      .child(h_flex().gap(6).flex_wrap()
        .child(this.button("delete", "Delete worktree and branch", (cx) => this.cleanup(cx, c), { danger: true }))
        .child(cancel));
  }

  dot(on) {
    const t = theme();
    return div().w(7).h(7).flex_none().rounded(4).bg(on ? t.working : t.border);
  }

  worktreeRows(info, machine) {
    const t = theme();
    const all = info.worktrees ?? [];
    const linked = all.filter((c) => !c.main);
    if (linked.length === 0) return [this.note("No worktrees yet.")];
    return linked.map((c, i) => {
      const there = this.spaceFor(c.path, all, machine);
      const current = samePath(info.top, c.path); // its Clean up is in "This worktree"
      const box = current ? null : this.deleteBox(c, info.base_branch);
      const row = h_flex().id(`wt-${i}`).h(26).px(6).gap(6).items_center().rounded(5).cursor_pointer()
        .when(current, (el) => el.bg(t.selected))
        .when(!current, (el) => el.hover((s) => s.bg(t.hover)))
        .on_click((_e, cx) => this.open(cx, c, all))
        .child(this.dot(c.listening))
        .child(div().flex_1().min_w(0).truncate().text_size(12.5).text_color(t.foreground).child(c.branch ?? basename(c.path)))
        .when(Boolean(c.port), (el) => el.child(div().flex_none().text_size(11).text_color(t.dim).child(String(c.port))))
        .when(Boolean(there) && !current, (el) => el.child(div().flex_none().text_size(11).text_color(t.accent).child("open")))
        .when(Boolean(c.branch) && !current, (el) => el.child(div().id(`wt-x-${i}`).px(3).flex_none().text_color(t.dim)
          .cursor_pointer().hover((s) => s.text_color(t.blocked)).tooltip("Clean up…")
          .on_click((_e, cx) => { cx.stop_propagation(); this.askDelete(cx, c); }).child("×")));
      return v_flex().gap(4).child(row).when(Boolean(box), (el) => el.child(box));
    });
  }

  body(v) {
    const t = theme();
    const st = this.st;
    const where = v.machine ?? "this computer";
    if (!st || st.key !== this.key) return [this.note("…")];
    if (st.error && !st.info) return [this.note(st.error, t.blocked)];
    if (!st.root) return [this.note(`${v.folder} isn't in a git repository.`)];
    if (!st.hwt) {
      if (!st.git || !st.py) {
        return [this.section("Install Corral",
          this.note(`Corral needs ${[!st.git && "git", !st.py && "Python 3.11+"].filter(Boolean).join(" and ")} on ${where}. Install ${!st.git && !st.py ? "them" : "it"}, then come back.`))];
      }
      return [this.section("Install Corral",
        this.note(`Corral isn't installed on ${where}.`, t.foreground),
        this.note(`Install clones Corral ${RELEASE} into ~/.local/share/herdr-corral, puts hwt in ~/.local/bin, and sets up the repositories in ${st.root}.`),
        h_flex().child(this.button("install", "Install Corral", (cx) => this.install(cx), { primary: true })))];
    }
    if (!atLeast(st.version, NEEDS)) {
      return [this.section("Update Corral",
        this.note(`This panel needs Corral ${NEEDS.join(".")} or newer; ${where} has ${st.version.join(".") || "an older one"}.`),
        h_flex().child(this.button("update", "Update Corral", (cx) => this.update(cx), { primary: true })))];
    }
    const info = st.info;
    const newer = info?.panel && !atLeast(versionOf(VERSION), versionOf(info.panel.version)) ? info.panel : null;
    const update = newer && this.section("Panel update",
      this.note(`Corral on ${where} has panel ${newer.version}; this one is ${VERSION}.`),
      h_flex().child(this.button("update-panel", "Update panel", (cx) => this.updatePanel(cx, newer), { primary: true })));
    return update ? [update, ...this.repoParts(v, info)] : this.repoParts(v, info);
  }

  repoParts(v, info) {
    const t = theme();
    if (!info?.git) return [this.note(`${v.folder} isn't in a git repository.`)];
    const name = basename(info.main);
    if (!info.repository) {
      if (!info.under_root) {
        return [this.section(`Set up ${name}`,
          this.note(`${name} is outside Corral's repositories folder (${info.canonical_root}). Change canonical_root in ~/.config/herdr-corral/config.toml to use it.`))];
      }
      const options = [...new Set([info.suggested_base, ...(info.base_candidates ?? [])].filter(Boolean))].slice(0, 5);
      const base = this.base ?? options[0] ?? "origin/main";
      return [this.section(`Set up ${name}`,
        this.note("New worktrees start from, and Clean up checks merges into:"),
        v_flex().gap(2).children(options.map((ref, i) =>
          h_flex().id(`base-${i}`).h(24).px(6).gap(6).items_center().rounded(5).cursor_pointer()
            .when(ref === base, (el) => el.bg(t.selected)).when(ref !== base, (el) => el.hover((s) => s.bg(t.hover)))
            .on_click((_e, cx) => { this.base = ref; cx.notify(); })
            .child(div().w(12).text_color(t.accent).child(ref === base ? "●" : "○"))
            .child(div().text_size(12.5).text_color(t.foreground).child(ref)))),
        h_flex().gap(6)
          .child(this.button("setup", "Set up", (cx) => this.setUp(cx, info, base), { primary: true }))
          .child(this.button("more", "More settings…", () => this.settings(info))))];
    }
    if (info.mode !== "worktree") return [this.note(`${name} is set up for opening only, not worktrees.`)];
    const parts = [];
    if (info.linked) {
      const me = (info.worktrees ?? []).find((c) => samePath(c.path, info.top)) ?? {};
      parts.push(this.section("This worktree",
        h_flex().gap(6).items_center()
          .child(this.dot(me.listening))
          .child(div().flex_1().min_w(0).truncate().text_size(13).font_semibold().text_color(t.foreground).child(info.branch ?? basename(info.top))),
        me.dev_url
          ? h_flex().id("url").gap(4).cursor_pointer().on_click(() => this.openUrl(me.dev_url))
              .child(div().truncate().text_size(12).text_color(t.accent).child(me.dev_url))
          : this.note("No port leased."),
        this.note(me.listening ? "Dev server running." : info.dev_command ? "Dev server stopped." : "No dev command set up."),
        h_flex().gap(6).flex_wrap()
          .when(info.dev_command && !me.listening, (el) => el.child(this.button("dev", "Start dev server", (cx) => this.startServer(cx, info))))
          .when(!info.dev_command, (el) => el.child(this.button("more", "More settings…", () => this.settings(info))))
          .when(this.deleting?.path !== info.top && Boolean(info.branch), (el) =>
            el.child(this.button("cleanup", "Clean up…", (cx) => this.askDelete(cx, { path: info.top, branch: info.branch })))),
        this.deleteBox({ path: info.top, branch: info.branch }, info.base_branch) ?? div()));
    } else {
      parts.push(this.section(`New worktree of ${name}`,
        Input.new(this.input).h(28).px(8).items_center().rounded(5).border(1).border_color(t.input).bg(t.background).text_color(t.foreground).text_size(12.5),
        h_flex().gap(6).items_center()
          .child(this.button("new", "New worktree", (cx) => this.create(cx), { primary: true }))
          .child(div().truncate().text_size(11).text_color(t.dim).child(`from ${info.base_branch}`))));
    }
    parts.push(this.section(`Worktrees of ${name}`, ...this.worktreeRows(info, v.machine)));
    return parts;
  }

  // Room for macOS's window buttons: docked on the left, the panel is the
  // window's first column (Herdr's own sidebar header does the same).
  lightsPadding() {
    const w = window();
    if (w?.platform !== "macos" || w.titlebar !== "none" || w.fullscreen) return 0;
    const x = layout()?.traffic_lights?.[0] ?? 12;
    return Math.max(0, (x + 64) / (settings().zoom ?? 1) - 10);
  }

  render() {
    const t = theme();
    const v = this.view();
    if (this.collapsed()) {
      // Below macOS's window buttons, which sit over this corner.
      return v_flex().id("corral").size_full().bg(t.chrome).items_center().pt(this.lightsPadding() ? 40 : 8)
        .child(this.toggleButton("expand", "Show Corral"));
    }
    return v_flex().id("corral").size_full().bg(t.chrome).p(10).gap(12).overflow_y_scroll()
      .child(h_flex().gap(6).items_center().pl(this.lightsPadding())
        .child(div().flex_1().font_semibold().text_size(13).text_color(t.foreground).child("Corral"))
        .child(div().text_size(11).text_color(t.dim).child(v ? (v.machine ?? "this computer") : ""))
        .child(div().id("refresh").px(4).cursor_pointer().text_color(t.dim).hover((s) => s.text_color(t.foreground))
          .on_click((_e, cx) => { this.at = 0; this.tick(cx); }).child("↻"))
        .child(this.toggleButton("collapse", "Hide Corral")))
      .when(!v, (el) => el.child(this.note("Open a space to use Corral.")))
      .when(Boolean(v), (el) => el.children(this.body(v)))
      .when(Boolean(this.busy), (el) => el.child(h_flex().gap(6).items_center().text_size(12).text_color(t.muted)
        .child(div().frames(["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"], 90)).child(this.busy)))
      .when(Boolean(this.error), (el) => el.child(h_flex().id("error").gap(6).items_start()
        .child(div().flex_1().text_size(12).text_color(t.blocked).child(this.error))
        .child(div().id("error-x").cursor_pointer().text_color(t.dim).on_click((_e, cx) => { this.error = ""; cx.notify(); }).child("×"))));
  }
}
