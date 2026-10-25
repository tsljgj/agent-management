"""Console: the ▶ on Claude's idle 5h label, and percentages that never round up (real JS in Node)."""

from test_countdown_ui import run_console_js  # noqa: F401  (fixture)

CLAUDE = r"""
  const claude = (account = "work", changes = {}) => ({account, provider: "claude", ok: true, stale: false,
    windows: [{name: "5h", used_percent: 0, resets_at: null}, {name: "7d", used_percent: 40, resets_at: future(90)}],
    ...changes});
  const START = /data-act="start-5h"/;
"""


def test_start_control_only_on_an_idle_5h_with_weekly_left(run_console_js):
    run_console_js(CLAUDE + r"""
(() => {
  DATA = {usages: [], meta: {}}; TAB = "claude";
  const idle = claude();
  assert.equal(canStart5h(idle), true);
  assert.match(row(idle, null), /idle<button class="act go" data-act="start-5h" title="开始计时">▶<\/button>/);
  assert.equal(row(idle, null).match(/data-act="start-5h"/g).length, 1);   // one entry point
  const week = (p) => claude("w", {windows: [{name: "5h", used_percent: 0, resets_at: null}, {name: "7d", used_percent: p, resets_at: future(9)}]});
  assert.match(row(week(99.6), null), START);          // 99.6% is not spent
  assert.doesNotMatch(row(week(100), null), START);    // weekly limit: the request would be rejected
  const counting = claude("c", {windows: [{name: "5h", used_percent: 3, resets_at: future(4)}, {name: "7d", used_percent: 1, resets_at: future(90)}]});
  assert.doesNotMatch(row(counting, null), START);
  for (const u of [claude("s", {stale: true}), claude("f", {ok: false, error: "network error"}),
                   claude("l", {ok: false, error: "not logged in"})])
    assert.doesNotMatch(row(u, null), START);
  assert.doesNotMatch(row({...idle, provider: "codex", weekly_countdown: {state: "active", message: ""}}, null), START);
  assert.doesNotMatch(row(usage("cx"), null), START);   // Codex gets its own (weekly) control
  assert.match(row(usage("cx"), null), /data-act="start-countdown"/);
  BOOT.actions = true;
})();
""")


def test_busy_state_click_routing_and_duplicate_clicks(run_console_js):
    run_console_js(CLAUDE + r"""
(async () => {
  const u = claude(); DATA = {usages: [u], meta: {}}; TAB = "claude";
  let resolveRequest, calls = [];
  action = body => { calls.push(body); return new Promise(resolve => { resolveRequest = resolve; }); };
  pull = async () => { DATA.meta.start5h = {"claude:work": {state: "verifying", message: "正在确认 5h 计时"}}; };
  const card = {dataset: {name: "work"}, classList: {toggle: () => assert.fail("button expanded its row")}};
  const button = {dataset: {act: "start-5h"}, disabled: false, closest: s => s === ".acct" ? card : null};
  const target = {closest: s => s === "[data-act]" ? button : null};
  const click = elements["#list"].listeners.click[0];
  const first = click({target});
  await click({target});
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0], {action: "start-5h", provider: "claude", name: "work"});
  assert.match(row(u, null), /data-act="start-5h" disabled title="正在开始计时">…/);
  resolveRequest(true); await first;
  assert.equal(start5hBusy(u), true);                  // the server job is still verifying
  await click({target}); assert.equal(calls.length, 1);
  DATA.meta.start5h["claude:work"] = {state: "error", message: "5h 未开始 · 请求超时"};
  assert.equal(start5hBusy(u), false);
  assert.match(row(u, null), /data-act="start-5h" title="开始计时"/);
  assert.match(details(u), /开启计时/);
  assert.match(details(u), /5h 未开始 · 请求超时/);
  // success: the meter shows the countdown and the control is gone
  const started = claude("work", {windows: [{name: "5h", used_percent: 0, resets_at: future(5)}, {name: "7d", used_percent: 40, resets_at: future(90)}]});
  assert.doesNotMatch(row(started, null), START);
  assert.match(row(started, null), /↻ [45]h \d\dm/);
})();
""")


def test_percentages_floor_and_max_only_at_100(run_console_js):
    run_console_js(r"""
(() => {
  const m = p => meter({name: "5h", used_percent: p, resets_at: null}, "5h");
  assert.match(m(99.6), /<span class="v">99%<\/span>/);
  assert.match(m(99.6), /title="5h 99%/);
  assert.doesNotMatch(m(99.6), /MAX|100%/);
  assert.match(m(100), /class="m max"/);
  assert.match(m(100), />MAX</);
  assert.match(m(42.9), />42%</);
  assert.equal(spent(99.9), false);
  assert.equal(spent(100), true);
  const u = usage("x", {windows: [{name: "5h", used_percent: 0, resets_at: null}, {name: "7d", used_percent: 0, resets_at: null},
    {name: "7d opus", used_percent: 99.6, resets_at: null}]});
  DATA = {usages: [u], meta: {}};
  assert.match(details(u), /7d opus 99%/);
  assert.match(row(u, null), /7d opus 99%/);
})();
""")
