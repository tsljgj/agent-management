"""Run the console's real JavaScript in Node with a small DOM fixture."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest


PAGE = Path(__file__).parents[1] / "src" / "redline" / "assets" / "console.html"


@pytest.fixture
def run_console_js():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to execute console JavaScript")
    html = PAGE.read_text(encoding="utf-8")
    script = html.split("<script>", 1)[1].split("</script>", 1)[0]
    script = script.split("/* ---------- boot ---------- */", 1)[0]
    script = script.replace("/*__BOOT__*/{}", '{"actions":true,"token":"fixture"}')

    def run(scenario):
        harness = r"""
const vm = require("node:vm"), assert = require("node:assert/strict");
class Element {
  constructor() {
    this.dataset = {}; this.listeners = {}; this.children = [];
    this.innerHTML = ""; this.textContent = ""; this.className = ""; this.offsetHeight = 0;
    const classes = new Set();
    this.classList = {
      add: (...items) => items.forEach(x => classes.add(x)),
      remove: (...items) => items.forEach(x => classes.delete(x)),
      contains: x => classes.has(x),
      toggle: (x, on) => { const yes = on === undefined ? !classes.has(x) : on;
        if (yes) classes.add(x); else classes.delete(x); return yes; },
    };
  }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  appendChild(el) { this.children.push(el); }
  focus() {}
}
const elements = {}, tabs = ["claude", "codex", "plans"].map(tab => {
  const el = new Element(); el.dataset.tab = tab; return el;
});
const document = {
  body: new Element(), activeElement: {tagName: "BODY"},
  querySelector: selector => {
    const tab = /^\.tab\[data-tab="(.*)"\]$/.exec(selector);
    if (tab) return tabs.find(el => el.dataset.tab === tab[1]);
    if (selector === "#emptyAdd" || selector === ".acct.open") return null;
    return elements[selector] ||= new Element();
  },
  querySelectorAll: selector => selector === ".tab" ? tabs : [],
  createElement: () => new Element(), addEventListener: () => {},
};
const context = vm.createContext({document, elements, tabs, assert, console,
  window: {addEventListener() {}}, localStorage: {getItem: () => null, setItem() {}},
  fetch: async () => { throw new Error("unexpected network request"); },
  setTimeout: () => 1, clearTimeout() {}, setInterval() {}, requestAnimationFrame() {},
  getSelection: () => ({toString: () => ""}), innerWidth: 520, innerHeight: 700,
});
vm.runInContext(SCRIPT, context);
vm.runInContext(`
  function usage(account = "work", changes = {}) {
    return {account, provider: "codex", ok: true, stale: false, windows: [
      {name: "5h", used_percent: 0, resets_at: null},
      {name: "7d", used_percent: 0, resets_at: null},
    ], weekly_countdown: {state: "unstarted", message: "本周尚未开始计时。"}, ...changes};
  }
  const future = hours => new Date(Date.now() + hours * 3600000).toISOString();
`, context);
(async () => { await vm.runInContext(SCENARIO, context); })().catch(error => {
  console.error(error); process.exitCode = 1;
});
"""
        harness = "const SCRIPT = " + json.dumps(script) + ";\nconst SCENARIO = " + json.dumps(scenario) + ";\n" + harness
        result = subprocess.run([node, "-"], input=harness, capture_output=True, text=True, encoding="utf-8", timeout=15)
        assert result.returncode == 0, result.stderr or result.stdout

    return run


def test_countdown_classification_and_visible_reminders(run_console_js):
    run_console_js(r"""
(() => {
  const notStarted = usage("unused");
  const zeroActive = usage("active-zero", {windows: [{name: "7d", used_percent: 0, resets_at: future(100)}]});
  const stale = usage("stale", {stale: true});
  const failedAccount = usage("failed", {ok: false, error: "network error"});
  const placeholder = usage("unknown", {weekly_countdown: {state: "unknown", message: "missing reset"}});
  const noWindow = usage("no-window", {windows: [], weekly_countdown: {state: "unavailable", message: "no weekly limit"}});
  assert.equal(weeklyOf(zeroActive).state, "active");
  assert.equal(weeklyTag(zeroActive), "");
  assert.equal(canStartWeekly(zeroActive), false);
  const expired = usage("expired", {windows: [{name: "7d", used_percent: 0,
    resets_at: new Date(Date.now() - 1000).toISOString()}],
    weekly_countdown: {state: "active", message: "old active status"}});
  const missingReset = usage("missing-reset", {weekly_countdown: {state: "active", message: "active without reset"}});
  for (const u of [expired, missingReset]) {
    assert.equal(weeklyOf(u).state, "unknown");
    assert.match(weeklyTag(u), /weekly 待确认/);
    assert.equal(canStartWeekly(u), true);
  }
  for (const u of [stale, failedAccount]) {
    assert.equal(weeklyOf(u).state, "unknown");
    assert.equal(canStartWeekly(u), false);
    assert.match(weeklyTag(u), /weekly 待确认/);
    assert.doesNotMatch(weeklyTag(u), /data-act="start-countdown"/);
  }
  assert.equal(canStartWeekly(placeholder), true);
  assert.match(weeklyTag(placeholder), /▶ 尝试开启/);
  assert.equal(canStartWeekly(noWindow), false);
  assert.equal(weeklyTag(noWindow), "");
  assert.equal(weeklyOf(usage("claude", {provider: "claude"})), null);
  assert.match(row(notStarted, null), /weekly 未开始/);
  assert.match(row(notStarted, null), /▶ 开启计时/);
  assert.match(row(placeholder, null), /待确认/);
  DATA = {usages: [notStarted, zeroActive, stale, placeholder, noWindow], meta: {accounts: {
    "codex:unused": {shared_quota_with: ["alias"]},
  }}};
  TAB = "codex"; render();
  assert.equal(elements["#weeklyNotice"].classList.contains("on"), true);
  assert.match(elements["#weeklyNotice"].textContent, /^1 个 Codex/);
  assert.equal(elements["#ncodex"].textContent, "5 · !1");
  assert.match(details(notStarted), /共享额度/);
  assert.match(details(notStarted), /alias/);
  assert.match(details(notStarted), /data-act="usage"/);
  TAB = "claude"; render();
  assert.equal(elements["#weeklyNotice"].classList.contains("on"), false);
})();
""")


def test_weekly_sort_prioritizes_unstarted_and_keeps_claude_behavior(run_console_js):
    run_console_js(r"""
(() => {
  const active = (name, hours, percent = 0) => usage(name, {windows: [
    {name: "7d", used_percent: percent, resets_at: future(hours)},
  ], weekly_countdown: {state: "active", message: "active"}});
  const unused = usage("unused");
  const unknown = usage("unknown", {weekly_countdown: {state: "unknown", message: "unknown"}});
  const login = usage("login", {ok: false, error: "not logged in"});
  SORT = "weekly";
  const list = [login, unknown, active("later", 20), active("full", 1, 100), active("soon", 4), unused];
  assert.deepEqual(sorted(list).map(u => u.account), ["unused", "soon", "later", "unknown", "login", "full"]);
  const claude = list.filter(u => ["later", "soon", "full"].includes(u.account)).map(u => ({...u, provider: "claude"}));
  assert.deepEqual(sorted(claude).map(u => u.account), ["soon", "later", "full"]);
})();
""")


def test_button_routes_actions_without_expanding_and_blocks_duplicate_requests(run_console_js):
    run_console_js(r"""
(async () => {
  const u = usage(); DATA = {usages: [u], meta: {}}; TAB = "codex";
  let resolveRequest, calls = [];
  action = body => { calls.push(body); return new Promise(resolve => { resolveRequest = resolve; }); };
  pull = async () => { DATA.meta.countdown = {"codex:work": {state: "verifying", message: "确认中"}}; };
  const card = {dataset: {name: "work"}, classList: {toggle: () => assert.fail("button expanded its row")}};
  const button = {dataset: {act: "start-countdown"}, disabled: false, closest: selector => selector === ".acct" ? card : null};
  const target = {closest: selector => selector === "[data-act]" ? button : null};
  const click = elements["#list"].listeners.click[0];
  const first = click({target});
  await click({target});
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0], {action: "start-countdown", provider: "codex", name: "work"});
  assert.equal(countdownBusy(u), true);
  assert.match(weeklyTag(u), /disabled/);
  assert.match(weeklyTag(u), /开启中/);
  resolveRequest(true); await first;
  assert.equal(countdownPending.has("codex:work"), false);
  assert.equal(countdownBusy(u), true);
  assert.match(weeklyTag(u), /确认中/);
  await click({target}); assert.equal(calls.length, 1);
  DATA.meta.countdown["codex:work"] = {state: "error", message: "SDK unavailable"};
  assert.equal(countdownBusy(u), false);
  assert.match(weeklyTag(u), /class="weekly error"/);
  assert.match(weeklyTag(u), /weekly 开启未确认/);
  assert.match(weeklyTag(u), /data-act="start-countdown"/);
  assert.doesNotMatch(weeklyTag(u), /disabled/);
  action = async body => { calls.push(body); return true; };
  button.dataset.act = "usage";
  await click({target});
  assert.deepEqual(calls[1], {action: "web", provider: "codex", name: "work", usage: true});
})();
""")


def test_pull_renders_meta_changes_and_completion_alerts_between_usage_polls(run_console_js):
    run_console_js(r"""
(async () => {
  const u = usage(); TAB = "codex";
  const snapshot = job => ({fetched_at: 123, usages: [u], events: [], meta: {countdown: {"codex:work": job}}});
  const deliveries = [snapshot({}), snapshot({state: "starting", message: "启动 SDK"}),
    snapshot({state: "verifying", message: "刷新确认"}), snapshot({state: "error", message: "SDK missing"})];
  fetch = async () => ({ok: true, json: async () => deliveries.shift()});
  await pull(false);
  assert.match(elements["#list"].innerHTML, /▶ 开启计时/);
  await pull(false);
  assert.match(elements["#list"].innerHTML, /weekly 开启中/);
  assert.match(elements["#list"].innerHTML, /disabled/);
  editing = true;
  await pull(false);
  assert.match(elements["#list"].innerHTML, /weekly 开启中/);
  editing = false;
  // Same usage timestamp, but a new meta job state must redraw after editing ends.
  await pull(false);
  assert.match(elements["#list"].innerHTML, /SDK missing/);
  assert.doesNotMatch(elements["#list"].innerHTML, /disabled/);
  const success = snapshot({state: "success", message: "weekly 已确认"});
  success.usages = [usage("work", {windows: [{name: "7d", used_percent: 0, resets_at: future(168)}]})];
  success.events = [{id: 1, text: "weekly 已确认", level: "ok", alert: true}];
  deliveries.push(success);
  await pull(false);
  assert.doesNotMatch(elements["#list"].innerHTML, /weekly 未开始|data-act="start-countdown"/);
  assert.equal(elements["#weeklyNotice"].classList.contains("on"), false);
  assert.equal(elements["#toast"].textContent, "weekly 已确认");
  assert.equal(elements["#toast"].className, "show good");
  const failure = snapshot({state: "error", message: "开启失败"});
  failure.events = [{id: 2, text: "开启失败", level: "error", alert: true}];
  deliveries.push(failure); await pull(false);
  assert.equal(elements["#toast"].textContent, "开启失败");
  assert.equal(elements["#toast"].className, "show bad");
})();
""")
