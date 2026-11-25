"""The console card: five speed levels, a compact "used in", no terminal button, a Gemini tab."""

from __future__ import annotations

from test_countdown_ui import run_console_js  # noqa: F401  (the console's JS harness)


def test_gauge_gives_every_level_an_equal_share_and_pins_past_ten(run_console_js):  # noqa: F811
    run_console_js(r"""
(() => {
  assert.deepEqual(GAUGE_TICKS, [0.5, 1.25, 2.5, 5]);
  const f = p => Math.round(gaugeFrac(p) * 1000) / 1000;
  assert.deepEqual([0, 0.5, 1.25, 2.5, 5, 10, 40].map(f), [0, 0.2, 0.4, 0.6, 0.8, 1, 1]);
  assert.equal(f(-1), 0); assert.equal(f(undefined), 0);
  assert.ok(f(3.75) > f(2.5) && f(3.75) < f(5));   // inside a level: linear
  // the level is the class the colour comes from; the tip says when the window runs out
  const u = {...usage("a"), provider: "claude",
    rate: {level: "burn", per_hour: 120, pace: 6, window: "5h", full_in: 1800}};
  const tag = rateTag(u);
  assert.match(tag, /^<div class="rate burn"/);
  assert.match(tag, /burn: 5h window filling at 120%\/h over the last 30 min \(6x pace\) · at this rate it runs out in 30m/);
  assert.equal((gauge(6).match(/class="sg on"/g) || []).length, 13);   // 0.84 of 15 segments
})();
""")


def test_used_in_is_one_line_with_the_rest_behind_a_count(run_console_js):  # noqa: F811
    run_console_js(r"""
(() => {
  const projects = ["/w/api", "/w/web", "/w/infra", "/w/docs", "/w/tools"].map((path, i) =>
    ({path, at: 1000 - i * 600, active: i === 0}));
  const u = {...usage("a"), provider: "claude", plan: "max 20x"};
  DATA = {now: 1000, usages: [u], meta: {accounts: {"claude:a": {projects, profile: {name: "Work"}}}}};
  const html = details(u);
  const used = html.match(/<div class="wide"><span>used in<\/span>[\s\S]*?<\/div><\/div>/)[0];
  assert.equal((used.match(/data-act="code"/g) || []).length, 3);
  assert.match(used, /<b>api<\/b><em class="on">●<\/em>/);
  assert.match(used, /<b>web<\/b><em class="">10m<\/em>/);
  assert.match(used, /<em class="more" title="\/w\/docs\n\/w\/tools">\+2<\/em>/);
  assert.equal((html.match(/used in/g) || []).length, 1);
  // the terminal button is gone; rename and remove stay
  assert.doesNotMatch(html, /data-act="jack"|terminal/);
  assert.match(html, /data-act="rename"/); assert.match(html, /data-act="del"/);
  // 5h and 7d share a line, renews shares one with the Chrome profile
  assert.match(html, /<div><span>5h ↻<\/span><b class="" title="not started">not started<\/b><\/div><div><span>7d ↻<\/span>/);
  assert.match(html, /<div><span>renews<\/span>.*?<\/div><\/div><div><span>chrome<\/span><b class="" title="Work">Work<\/b><\/div>/);
  // no projects: no row
  DATA.meta.accounts["claude:a"].projects = [];
  assert.doesNotMatch(details(u), /used in/);
})();
""")


def test_gemini_tab_rows_and_details(run_console_js):  # noqa: F811
    run_console_js(r"""
(() => {
  const gem = (name, changes = {}) => ({account: name, provider: "gemini", ok: true, stale: false,
    email: name + "@gmail.com", plan: "ai pro", windows: [
      {name: "pro", used_percent: 40, resets_at: future(6)},
      {name: "flash", used_percent: 5, resets_at: future(6)},
      {name: "flash-lite", used_percent: 0, resets_at: future(6)}],
    extra: {project: "proj-123", models: {"gemini-2.5-pro": "60% left", "gemini-2.5-flash": "95% left"}}, ...changes});
  const claude = {...usage("c"), provider: "claude"};
  // the tab shows only once there is a Gemini login
  DATA = {fetched_at: 1, now: 1, usages: [claude], meta: {accounts: {}}};
  TAB = "claude"; render();
  const tab = tabs.find(t => t.dataset.tab === "gemini");
  assert.equal(tab.classList.contains("hide"), true);
  DATA.usages = [claude, gem("me")]; render();
  assert.equal(tab.classList.contains("hide"), false);
  assert.equal(elements["#ngemini"].textContent, 1);
  // its rows: a meter per model family (pro, flash), the tier, no gauge, no login / wake / credit
  setTab("gemini");
  assert.equal(TAB, "gemini");
  assert.equal(elements["#plus"].classList.contains("hide"), true);
  assert.equal(elements["#wake"].classList.contains("hide"), true);
  const html = elements["#list"].innerHTML;
  assert.match(html, /class="acct gemini/);
  assert.match(html, /<span class="k">pro<\/span>/); assert.match(html, /<span class="k">flash<\/span>/);
  assert.doesNotMatch(html, /flash-lite<\/span>/);
  assert.match(html, /<div class="plan">AI PRO<\/div>/);
  assert.doesNotMatch(html, /class="rate|data-act="(login|wake1|console|rename|del|jack)"/);
  const det = details(gem("me"));
  assert.match(det, /me@gmail\.com · ai pro/); assert.match(det, /proj-123/);
  assert.match(det, /gemini-2\.5-pro 60% left · gemini-2\.5-flash 95% left/);
  // a failed read: what to do, in the Gemini CLI; retry, never a login from here
  const bad = row(gem("me", {ok: false, windows: [], error: "access token expired; run `gemini` once to renew it"}), null);
  assert.match(bad, /登录已过期 · 在终端运行一次 gemini/);
  assert.match(bad, /data-act="retry"/); assert.doesNotMatch(bad, /data-act="(login|wake1)"|⟳ renew/);
  // claude rows are unchanged by it
  setTab("claude");
  assert.equal(elements["#plus"].classList.contains("hide"), false);
  // plans stays claude + codex
  setTab("plans");
  assert.doesNotMatch(elements["#list"].innerHTML, /GEMINI/);
})();
""")
