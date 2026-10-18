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
  assert.match(row(placeholder, null), /class="acct[^\"]*weekly-attention/);
  DATA = {usages: [notStarted, zeroActive, stale, placeholder, noWindow], meta: {accounts: {
    "codex:unused": {shared_quota_with: ["alias"]},
  }}};
  TAB = "codex"; render();
  assert.equal(elements["#weeklyNotice"].classList.contains("on"), true);
  assert.match(elements["#weeklyNotice"].textContent, /^1 个 Codex/);
  assert.match(elements["#weeklyNotice"].textContent, /2 个 Codex 账户的 weekly 状态待确认/);
  assert.equal(elements["#ncodex"].textContent, "5 · !1 · ?2");
  assert.match(details(notStarted), /共享额度/);
  assert.match(details(notStarted), /alias/);
  assert.match(details(notStarted), /data-act="usage"/);
  TAB = "claude"; render();
  assert.equal(elements["#weeklyNotice"].classList.contains("on"), false);
})();
""")


def test_unknown_weekly_states_get_prominent_reminders_without_claiming_unstarted(run_console_js):
    run_console_js(r"""
(async () => {
  const uncertain = usage("uncertain", {weekly_countdown: {state: "unknown", message: "missing reset"}});
  const stale = usage("stale", {stale: true});
  const failedAccount = usage("failed", {ok: false, error: "network error"});
  DATA = {usages: [uncertain, stale, failedAccount], meta: {}};
  TAB = "codex"; render();
  const tab = tabs.find(el => el.dataset.tab === "codex");
  assert.equal(elements["#weeklyNotice"].classList.contains("on"), true);
  assert.match(elements["#weeklyNotice"].textContent, /3 个 Codex 账户的 weekly 状态待确认/);
  assert.match(elements["#weeklyNotice"].textContent, /「尝试开启」/);
  assert.doesNotMatch(elements["#weeklyNotice"].textContent, /尚未开始|「开启计时」/);
  assert.equal(tab.classList.contains("waiting"), true);
  assert.match(tab.title, /3 个 Codex 账户的 weekly 状态待确认/);
  assert.equal(elements["#ncodex"].textContent, "3 · ?3");
  assert.match(weeklyTag(uncertain), /class="weekly unknown"/);
  assert.match(weeklyTag(uncertain), /▶ 尝试开启/);
  let calls = 0;
  action = async () => { calls++; return true; };
  for (const u of [stale, failedAccount]) {
    assert.match(row(u, null), /weekly-attention/);
    assert.doesNotMatch(weeklyTag(u), /data-act="start-countdown"/);
    await startWeekly(u);
  }
  assert.equal(calls, 0);
  const noDuration = usage("no-duration", {windows: [{name: "primary", used_percent: 0, resets_at: null}],
    weekly_countdown: {state: "unknown", message: "window duration missing"}});
  DATA.usages = [stale, failedAccount, noDuration]; render();
  assert.equal(elements["#weeklyNotice"].classList.contains("on"), true);
  assert.match(elements["#weeklyNotice"].textContent, /3 个 Codex 账户的 weekly 状态待确认/);
  assert.doesNotMatch(elements["#weeklyNotice"].textContent, /「尝试开启」|尚未开始/);
  assert.equal(canStartWeekly(noDuration), false);
  assert.doesNotMatch(weeklyTag(noDuration), /data-act="start-countdown"/);
  await startWeekly(noDuration); assert.equal(calls, 0);
  TAB = "claude"; render();
  assert.equal(elements["#weeklyNotice"].classList.contains("on"), false);
  assert.equal(tab.classList.contains("waiting"), true);
})();
""")


def test_api_key_accounts_remain_unavailable_and_outside_weekly_reminders(run_console_js):
    run_console_js(r"""
(async () => {
  const apiKey = usage("api-key", {ok: false, error: "API key accounts do not have subscription usage",
    weekly_countdown: {state: "unavailable", message: "API key account"}});
  const cachedApiKey = {...apiKey, account: "cached-api-key", stale: true};
  DATA = {usages: [apiKey, cachedApiKey], meta: {}}; TAB = "codex"; render();
  for (const u of DATA.usages) {
    assert.equal(weeklyOf(u).state, "unavailable");
    assert.equal(canStartWeekly(u), false);
    assert.equal(weeklyTag(u), "");
    assert.doesNotMatch(row(u, null), /weekly-attention/);
  }
  assert.equal(elements["#weeklyNotice"].classList.contains("on"), false);
  assert.equal(elements["#weeklyNotice"].textContent, "");
  assert.equal(tabs.find(el => el.dataset.tab === "codex").classList.contains("waiting"), false);
  assert.equal(elements["#ncodex"].textContent, 2);
})();
""")


def test_weekly_sort_prioritizes_unstarted_then_actionable_unknown_and_keeps_claude_behavior(run_console_js):
    run_console_js(r"""
(() => {
  const active = (name, hours, percent = 0) => usage(name, {windows: [
    {name: "7d", used_percent: percent, resets_at: future(hours)},
  ], weekly_countdown: {state: "active", message: "active"}});
  const unused = usage("unused");
  const unknown = usage("unknown", {weekly_countdown: {state: "unknown", message: "unknown"}});
  const stale = usage("stale", {stale: true});
  const failedAccount = usage("failed", {ok: false, error: "network error"});
  const usedUnknown = usage("used-unknown", {windows: [{name: "7d", used_percent: 5, resets_at: null}],
    weekly_countdown: {state: "unknown", message: "missing reset"}});
  const login = usage("login", {ok: false, error: "not logged in"});
  SORT = "weekly";
  const list = [login, stale, failedAccount, usedUnknown, unknown, active("later", 20), active("full", 1, 100), active("soon", 4), unused];
  assert.deepEqual(sorted(list).map(u => u.account), ["unused", "unknown", "soon", "later", "stale", "failed", "used-unknown", "login", "full"]);
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


def test_free_accounts_show_reported_main_windows_and_honest_missing_window_state(run_console_js):
    run_console_js(r"""
(() => {
  const free = (name, windows) => usage(name, {plan: "free", windows,
    weekly_countdown: {state: "unavailable", message: "no weekly limit"}});
  const weekly = free("weekly", [{name: "7d", used_percent: 17, resets_at: future(100)}]);
  assert.match(row(weekly, null), /class="plan">Free/);
  assert.match(quotaMeters(weekly), /^<div class="m none"><\/div><div class="m"/);
  assert.match(quotaMeters(weekly), /class="k">7d<.*class="v">17%/s);
  for (const name of ["1d", "primary", "secondary"]) {
    const u = free("single", [{name, used_percent: 32, resets_at: future(20)}]);
    const html = quotaMeters(u);
    assert.ok(html.includes(`class="k">${name}<`));
    assert.match(html, /class="v">32%/);
    assert.doesNotMatch(html, /未返回额度窗口/);
  }
  const mixed = free("mixed", [{name: "1d", used_percent: 32, resets_at: future(20)},
    {name: "7d", used_percent: 17, resets_at: future(100)}]);
  assert.ok(quotaMeters(mixed).indexOf('class="k">1d<') < quotaMeters(mixed).indexOf('class="k">7d<'));
  for (const windows of [[], [{name: "review 7d", used_percent: 0, resets_at: future(100)}]]) {
    const u = free("missing", windows);
    assert.match(quotaMeters(u), /未返回额度窗口/);
    assert.doesNotMatch(quotaMeters(u), /class="v"|class="bar"/);
  }
  const unknownPercent = free("unknown", [{name: "1d", used_percent: null, resets_at: null}]);
  assert.match(quotaMeters(unknownPercent), /class="v">\?</);
  assert.match(details(unknownPercent), /1d \?%/);
})();
""")


def test_banked_reset_expirations_are_individual_and_update_without_a_poll(run_console_js):
    run_console_js(r"""
(() => {
  const expiry = future(49), soon = future(1 / 120);
  const u = usage("resets", {extra: {banked_resets: {available: 4, credits: [
    {title: 'Monthly <img src=x onerror="bad()">', expires_at: expiry},
    {title: "Referral", expires_at: null},
    {expires_at: future(-1)},
  ]}}});
  const html = details(u);
  assert.match(html, /Reset #1 · Monthly &lt;img/);
  assert.match(html, /Reset #2 · Referral/);
  assert.match(html, /Reset #3/);
  assert.ok(html.includes(esc(bankedExpiryText(expiry))));
  assert.match(html, /剩余 2d/);
  assert.match(html, /到期时间未知/);
  assert.match(html, /已到期/);
  assert.match(html, /另有 1 次未返回到期明细/);
  assert.doesNotMatch(html, /<img/);
  assert.match(row(u, null), /到期详情 ↓/);
  assert.match(bankedExpiryText(soon), /不足 1 分钟/);
  assert.equal(bankedExpiryText("not a date"), "到期时间未知");
  const el = {dataset: {expiry}, textContent: ""};
  const originalQuery = document.querySelectorAll;
  document.querySelectorAll = selector => selector === "[data-expiry]" ? [el] : [];
  tick();
  assert.equal(el.textContent, bankedExpiryText(expiry));
  const originalNow = Date.now;
  Date.now = () => new Date(expiry).getTime() + 1;
  tick();
  assert.match(el.textContent, /已到期/);
  Date.now = originalNow;
  document.querySelectorAll = originalQuery;
  DATA = {usages: [u], meta: {accounts: {"codex:resets": {login_mismatch: true}}}};
  assert.doesNotMatch(details(u), /Reset #|data-expiry/);
})();
""")


def test_missing_banked_reset_details_are_explicit_including_old_caches(run_console_js):
    run_console_js(r"""
(() => {
  const detail = b => details(usage("work", {extra: {banked_resets: b}}));
  assert.match(detail({available: 2}), /另有 2 次未返回到期明细/);
  assert.match(detail({available: 2, credits: null}), /另有 2 次未返回到期明细/);
  assert.match(detail({available: 2, credits: []}), /另有 2 次未返回到期明细/);
  assert.match(detail({applicable: 1}), /未取得到期明细/);
  assert.doesNotMatch(detail({available: 0}), /未返回到期明细|未取得到期明细|Reset #/);
  assert.doesNotMatch(detail({available: 0, credits: []}), /未返回到期明细|未取得到期明细|Reset #/);
  const onlyDetails = {credits: [{expires_at: future(12)}]};
  assert.match(detail(onlyDetails), /Reset #1/);
  assert.match(bankedResetText(usage("work", {extra: {banked_resets: onlyDetails}})), /总数未知/);
})();
""")


def test_banked_resets_are_separate_from_credits_and_preserve_zero_and_partial_counts(run_console_js):
    run_console_js(r"""
(() => {
  const counted = usage("counted", {extra: {credits: "5", banked_resets: {available: 3, applicable: 0}}});
  const html = row(counted, null), detail = details(counted);
  assert.match(html, /class="banked">Banked resets: 3 次 · 当前适用 0 次/);
  assert.match(html, /\+ 5 credits/);
  assert.match(detail, /<span>Banked resets<\/span>/);
  assert.match(detail, />3 次 · 当前适用 0 次<\/b>/);
  assert.doesNotMatch(html, /\[object Object\]|banked_resets/);
  const zero = usage("zero", {extra: {banked_resets: {available: 0}}});
  assert.match(row(zero, null), /class="banked">Banked resets: 0 次/);
  assert.doesNotMatch(row(zero, null), /当前适用/);
  const partial = usage("partial", {extra: {banked_resets: {applicable: 2}}});
  assert.match(row(partial, null), /Banked resets: 总数未知 · 当前适用 2 次/);
  assert.doesNotMatch(row(usage("missing"), null), /Banked resets/);
  assert.doesNotMatch(row(usage("empty", {extra: {banked_resets: {}}}), null), /Banked resets/);
})();
""")


def test_login_mismatch_shows_both_identities_and_blocks_wrong_account_quota_actions(run_console_js):
    run_console_js(r"""
(async () => {
  const u = usage("expected", {plan: "free", email: "actual@example.test", extra: {banked_resets: {available: 3}}});
  const correct = usage("correct", {plan: "plus"});
  DATA = {usages: [u, correct], meta: {accounts: {"codex:expected": {
    login_mismatch: true, email: "actual@example.test", expected_email: "expected@example.test",
  }}}}; TAB = "codex";
  const html = row(u, null);
  assert.match(html, /登录账号不符/);
  assert.match(html, /<span>实际：actual@example.test<\/span>/);
  assert.match(html, /<span>预期：expected@example.test<\/span>/);
  assert.match(html, /data-act="login">重新登录预期账号/);
  assert.doesNotMatch(html, /class="plan"|\bfree\b|Banked resets|class="bar"|data-act="start-countdown"/i);
  assert.equal(canStartWeekly(u), false);
  assert.equal(reauthTag({...u, ok: false, stale: true, error: "expired"}), "");
  const paidMismatch = {...u, plan: "plus", renews_at: future(100), renews_kind: "end"};
  assert.equal(renewOf(paidMismatch), null);
  renderPlans([paidMismatch]);
  assert.match(elements["#list"].innerHTML, /登录账号不符/);
  assert.doesNotMatch(elements["#list"].innerHTML, /<em>plus<\/em>/);
  assert.match(elements["#list"].innerHTML, /<span class="dt[^"]*"[^>]*>unknown<\/span>/);
  assert.equal(best("codex").account, "correct");
  SORT = "weekly";
  assert.deepEqual(sorted(DATA.usages).map(x => x.account), ["correct", "expected"]);
  DATA.usages = [u]; render();
  assert.doesNotMatch(elements["#weeklyNotice"].textContent, /尚未开始|「开启计时」|「尝试开启」/);
  let calls = [];
  action = async body => { calls.push(body); return true; };
  await startWeekly(u); assert.equal(calls.length, 0);
  const card = {dataset: {name: "expected"}};
  const button = {dataset: {act: "login"}, closest: selector => selector === ".acct" ? card : null};
  const target = {closest: selector => selector === "[data-act]" ? button : null};
  await elements["#list"].listeners.click[0]({target});
  assert.deepEqual(calls, [{action: "login", provider: "codex", name: "expected"}]);
})();
""")


def test_last_days_of_a_subscription_show_on_the_main_page(run_console_js):
    run_console_js(r"""
(() => {
  const day = n => { const d = new Date(); d.setHours(12, 0, 0, 0); d.setDate(d.getDate() + n); return d.toISOString(); };
  const ends = (name, n, changes = {}) => usage(name, {plan: "plus", renews_at: day(n), renews_kind: "end", ...changes});
  DATA = {usages: [], meta: {accounts: {"codex:set": {renews: day(2).slice(0, 10)}}}};
  assert.match(row(ends("soon", 3), null), /class="renew-soon soon"[^>]*>订阅还剩 3 天</);
  assert.match(row(ends("week", 6), null), /class="renew-soon "[^>]*>订阅还剩 6 天</);
  assert.doesNotMatch(row(ends("later", 8), null), /renew-soon/);
  // a Free plan has nothing to renew, whatever paid-until an old subscription left behind
  const free = ends("free", 3, {plan: "free"});
  assert.equal(renewOf(free), null);
  assert.doesNotMatch(row(free, null), /renew-soon/);
  // Claude's monthly estimate says so
  const claude = {...usage("claude", {plan: "max 20x", renews_at: addMonths(new Date(day(5)), -1).toISOString(),
    renews_kind: "start"}), provider: "claude"};
  const est = renewParts(renewOf(claude)).days;
  assert.ok(est <= 7);
  assert.match(row(claude, null), new RegExp(`title="[^"]*每月续费估算[^"]*">订阅还剩 约 ${est} 天<`));
  assert.match(row(ends("set", 30), null), /title="[^"]*你设置的日期[^"]*">订阅还剩 2 天</);
})();
""")


def test_plans_list_claude_and_codex_apart_soonest_first(run_console_js):
    run_console_js(r"""
(() => {
  const day = n => { const d = new Date(); d.setHours(12, 0, 0, 0); d.setDate(d.getDate() + n); return d.toISOString(); };
  const claude = (name, n, plan) => ({...usage(name, {plan, renews_at: day(n), renews_kind: "end"}), provider: "claude"});
  DATA = {usages: [], meta: {accounts: {}}};
  renderPlans([usage("x-free", {plan: "free", renews_at: day(-40), renews_kind: "end"}), claude("c-late", 20, "max 20x"),
               usage("x-soon", {plan: "plus", renews_at: day(1), renews_kind: "end"}), claude("c-soon", 2, "pro")]);
  const html = elements["#list"].innerHTML;
  assert.deepEqual([...html.matchAll(/class="plh">(\w+)|data-name="([^"]+)"/g)].map(m => m[1] || m[2]),
                   ["CLAUDE", "c-soon", "c-late", "CODEX", "x-soon", "x-free"]);
  assert.match(html, /class="plh">CLAUDE<span>2<\/span>/);
  assert.match(html, /data-name="x-free".*?title="free plan: no subscription">–<\/span>/s);
  renderPlans([usage("only-codex", {plan: "plus"})]);
  assert.doesNotMatch(elements["#list"].innerHTML, /CLAUDE/);
})();
""")
