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
  // 5h and 7d share a line; plan, login and Chrome profile are one quiet line at the top
  assert.match(html, /<div><span>5h ↻<\/span><b class="" title="not started">not started<\/b><\/div><div><span>7d ↻<\/span>/);
  assert.match(html, /^<div class="det"><div class="wide dhead"><b title="max 20x · Chrome Work">max 20x · Chrome Work<\/b><\/div>/);
  assert.match(html, /<div class="gap"><span>renews<\/span>/);
  assert.doesNotMatch(html, /<span>(chrome|account)<\/span>/);
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


def test_used_up_accounts_say_when_they_are_back_and_usable_ones_are_marked(run_console_js):  # noqa: F811
    run_console_js(r"""
(() => {
  const cl = (name, windows, changes = {}) => ({...usage(name), provider: "claude", plan: "max 20x", windows, ...changes});
  const full = cl("full", [{name: "5h", used_percent: 20, resets_at: future(2)},
                           {name: "7d", used_percent: 100, resets_at: future(5 * 24 + 11)}]);
  const both = cl("both", [{name: "5h", used_percent: 100, resets_at: future(2)},
                           {name: "7d", used_percent: 100, resets_at: future(30)}]);
  const fine = cl("fine", [{name: "5h", used_percent: 20, resets_at: future(2)},
                           {name: "7d", used_percent: 40, resets_at: future(50)}]);
  DATA = {now: 1, usages: [full, both, fine], meta: {accounts: {}}};
  // used up: the row keeps both bars, a full red one with MAX, and its name is dimmed;
  // the window it waits on has its countdown brighter, and it ticks like the others
  const r = row(full, null);
  assert.match(r, /class="acct[^"]* blocked/);
  assert.match(r, /class="m max gate"[^>]*><span class="k">7d<\/span><div class="bar"><\/div><span class="v">MAX<\/span><span class="t" data-reset="[^"]+">↻ 5d 1\dh</);
  assert.match(r, /<div class="m"[^>]*><span class="k">5h<\/span>/);
  assert.doesNotMatch(r, /used up · back in|class="back"/);
  // both full: two red bars, the later reset is the one it waits on
  const rb = row(both, null);
  assert.equal((rb.match(/class="m max/g) || []).length, 2);
  assert.match(rb, /class="m max"[^>]*><span class="k">5h</);
  assert.match(rb, /class="m max gate"[^>]*><span class="k">7d</);
  // usable now: marked, and not blocked
  const ok = row(fine, null);
  assert.match(ok, /class="acct[^"]* ready/); assert.doesNotMatch(ok, /blocked|class="back"/);
  // the 7d reset in the details is no longer red; the other limits by their readable names
  const named = cl("n", [{name: "5h", used_percent: 10, resets_at: future(2)},
    {name: "7d", used_percent: 50, resets_at: future(50)},
    {name: "weekly weekly_all", used_percent: 50.4, resets_at: future(50)},
    {name: "weekly Fable", used_percent: 100, resets_at: future(50)}]);
  const det = details(named);
  assert.match(det, /<span>limits<\/span><b class="" title="7d Fable used up">7d Fable used up<\/b>/);   // the all-models copy of 7d is left out
  assert.doesNotMatch(det, /weekly_all|class="red"/);
  assert.equal(niceName("weekly weekly_all"), "7d all models");
  assert.equal(niceName("weekly Fable"), "7d Fable");
  assert.equal(niceName("seven_day_opus"), "seven day opus");
  // a weekly-all limit that differs from 7d stays, by its name
  named.windows[2].used_percent = 80;
  assert.match(details(named), /7d all models 80%/);
  // a codex account with a spent primary window is blocked too; gemini never is
  const cx = {...usage("cx"), provider: "codex", windows: [{name: "primary", used_percent: 100, resets_at: future(3)}]};
  assert.ok(blockedOf(cx));
  assert.equal(blockedOf({...cx, provider: "gemini"}), null);
  // an estimated renewal is never in alarm colours
  const est = renewTag({...cl("e", []), extra: {}, plan: "pro"});
  assert.doesNotMatch(est || "", /class="sub (soon|urgent|high|mid)/);
})();
""")


def test_only_the_logo_and_the_empty_gap_move_the_window():
    from pathlib import Path

    html = (Path(__file__).parents[1] / "src" / "redline" / "assets" / "console.html").read_text(encoding="utf-8")
    head = html.split("<header", 1)[1].split("</header>", 1)[0]
    assert head.startswith(' id="top">')            # the header itself is not a drag region
    assert '<div class="logo pywebview-drag-region">' in head
    assert '<div class="sp pywebview-drag-region">' in head
    assert head.count("pywebview-drag-region") == 2  # so no tab or header button can drag the window
    assert "move_to" not in html                     # no hand-rolled window drag


def test_height_follows_the_content_downward_and_stops_at_the_screen_bottom(run_console_js):  # noqa: F811
    run_console_js(r"""
(async () => {
  const fits = [];
  let release;
  window.pywebview = {api: {fit: (w, h) => { fits.push([w, h]); return new Promise(r => { release = r; }); }}};
  window.screen = {availTop: 0, availHeight: 1040}; window.screenY = 100;
  const settle = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
  elements["#list"].children = [{offsetHeight: 300}, {offsetHeight: 300}];
  fitHeight();
  // one call, straight to the height the content wants, at the width as it is
  assert.deepEqual(fits, [[520, 602]]);
  // while it runs, more changes wait for it and then send only the latest
  elements["#list"].children.push({offsetHeight: 200});
  fitHeight(); fitHeight();
  assert.equal(fits.length, 1);
  innerHeight = 602; release(); await settle();
  assert.deepEqual(fits.at(-1), [520, 802]);
  innerHeight = 802; release(); await settle();
  assert.equal(fits.length, 2);
  // nothing changed: no call at all
  fitHeight(); await settle();
  assert.equal(fits.length, 2);
  // an opened account would run past the bottom of the screen: it stops there and the list scrolls
  elements["#list"].children.push({offsetHeight: 600});
  fitHeight(); await settle();
  assert.deepEqual(fits.at(-1), [520, 1040 - 100 - 8]);
})();
""")


def test_a_height_set_by_hand_stays_and_a_width_set_by_hand_keeps_the_fit(run_console_js):  # noqa: F811
    run_console_js(r"""
(async () => {
  const calls = [];
  window.pywebview = {api: {
    fit: async (w, h) => { calls.push(["fit", w, h]); },
    resize: async (w, h, e) => { calls.push(["resize", w, h, e]); },
    save_size: (w, h, byHeight) => { calls.push(["save", w, h, byHeight]); },
  }};
  window.addEventListener = () => {}; window.removeEventListener = () => {};
  const settle = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
  const edge = e => edges.find(x => x.dataset.e === e);
  const drag = async (e, dx, dy) => {
    const h = edge(e);
    h.listeners.pointerdown[0]({button: 0, pointerId: 1, screenX: 500, screenY: 500, preventDefault() {}});
    // no fit while an edge is being dragged
    const before = calls.length;
    elements["#list"].children = [{offsetHeight: 900}];
    fitHeight(); await settle();
    assert.equal(calls.length, before);
    h.listeners.pointermove[0]({buttons: 1, screenX: 500 + dx, screenY: 500 + dy});
    await settle();
    h.listeners.pointerup[0]();
    // let go: nothing follows the mouse any more
    assert.equal((h.listeners.pointermove || []).length, 0);
  };
  // the right edge: the width is yours, the height still follows the content
  await drag("e", 80, 0);
  assert.deepEqual(calls.at(-1), ["save", 600, 700, false]);
  innerWidth = 600;
  fitHeight(); await settle();
  assert.deepEqual(calls.at(-1), ["fit", 600, 902]);
  // the bottom edge: the height is yours, and the content no longer changes it
  innerHeight = 902;
  await drag("s", 0, -200);
  assert.deepEqual(calls.at(-1), ["save", 600, 702, true]);
  calls.length = 0; innerHeight = 702;
  elements["#list"].children.push({offsetHeight: 300});
  fitHeight(); await settle();
  assert.deepEqual(calls, []);
})();
""")


def test_an_edge_drag_ends_when_the_mouse_is_already_up(run_console_js):  # noqa: F811
    run_console_js(r"""
(async () => {
  const calls = [];
  window.pywebview = {api: {resize: async (...a) => { calls.push(a); }, save_size: () => calls.push("save")}};
  window.addEventListener = () => {}; window.removeEventListener = () => {};
  const h = edges.find(x => x.dataset.e === "se");
  h.listeners.pointerdown[0]({button: 0, pointerId: 1, screenX: 500, screenY: 500, preventDefault() {}});
  // the up was lost (the window moved under the mouse): the next move sees no button and stops
  h.listeners.pointermove[0]({buttons: 0, screenX: 700, screenY: 700});
  await Promise.resolve();
  assert.deepEqual(calls, ["save"]);
  assert.equal((h.listeners.pointermove || []).length, 0);
  // losing the capture ends it the same way
  h.listeners.pointerdown[0]({button: 0, pointerId: 2, screenX: 500, screenY: 500, preventDefault() {}});
  h.listeners.lostpointercapture[0]();
  assert.equal((h.listeners.pointermove || []).length, 0);
  assert.equal(calls.filter(c => c === "save").length, 2);
})();
""")
