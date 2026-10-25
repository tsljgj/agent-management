"""Monthly API credit of Claude Max / Team plans, read from the linked Claude Console org."""

import json
import threading
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from io import StringIO

import pytest

from redline import console_signin, credit
from redline.http import HTTPStatusError
from redline.models import ProviderError, Usage, Window
from redline.monitor import Monitor
from redline.render import credit_line, render_table
from test_countdown_ui import run_console_js  # noqa: F401  (the console's JS harness)

# The real balance document (2026-10-24, Max 20x), ids replaced.
BALANCE = json.loads("""{"amount":20000,"currency":"USD","balance":{"money":null,"credits":{"amount_minor":20000,"exponent":2}},"balance_credits":200,"amount_without_scoped_credits":0,"auto_reload_settings":null,"auto_reload_disabled_reason":null,"auto_reload_stripe_error_code":null,"pending_invoice_amount_cents":null,"last_paid_purchase_cents":2500,"expiry_policy_months":null,"credit_expiry_policy":null,"auto_reload_terms_banner":null,"tranches":[],"promo_tranches":[{"remaining_amount_minor_units":19999,"currency":"USD","expires_at":"2026-11-21T00:00:00Z","granted_amount_minor_units":20000,"granted_at":"2026-10-20T22:45:36.859000Z","remaining":{"money":null,"credits":{"amount_minor":19999,"exponent":2}},"granted":{"money":null,"credits":{"amount_minor":20000,"exponent":2}},"program_id":"subscription_api_credit","name":"API credit (Max 20x)","id":"00000000-0000-4000-8000-000000000001","scope":{"kind":"scoped","applies_to":[{"product":"agent_sdk","model_family":null,"model_version":null},{"product":"api","model_family":null,"model_version":null},{"product":"batch","model_family":null,"model_version":null},{"product":"console","model_family":null,"model_version":null},{"product":"managed-agents","model_family":null,"model_version":null},{"product":"managed-agents-onprem","model_family":null,"model_version":null}]}}],"next_expires_at":"2026-11-21T00:00:00Z"}""")
CLAUDE_ORG = "11111111-1111-4111-8111-111111111111"
CONSOLE_ORG = "22222222-2222-4222-8222-222222222222"
ORGS = [
    {"uuid": CLAUDE_ORG, "name": "someone@example.com's Organization", "capabilities": ["claude_max", "chat"],
     "billing_type": "stripe_subscription", "rate_limit_tier": "default_claude_max_20x"},
    {"uuid": CONSOLE_ORG, "name": "Someone's Individual Org", "capabilities": ["api", "api_individual", "customer_terms:standard"],
     "billing_type": "prepaid", "rate_limit_tier": "auto_trust_tier_c"},
]
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
IDENTITY = "a" * 64


@pytest.fixture(autouse=True)
def fresh_cache():
    credit._cache.clear()
    yield
    credit._cache.clear()


def no_plan_credit():
    doc = json.loads(json.dumps(BALANCE))
    doc["promo_tranches"][0]["program_id"] = "iguana_necktie"  # an old cloud credit: not the plan's
    doc["promo_tranches"][0]["remaining_amount_minor_units"] = 25000
    return doc


# ---------------------------------------------------------------- parsing


def test_parses_the_monthly_plan_credit():
    c = credit.parse_credit(BALANCE, now=NOW)
    assert c == {"state": "ok", "granted": 200.0, "remaining": 199.99, "currency": "USD",
                 "expires_at": "2026-11-21T00:00:00+00:00"}


@pytest.mark.parametrize("doc", [no_plan_credit(), {"promo_tranches": []}, {}, None, {"promo_tranches": "x"}])
def test_no_subscription_tranche_is_none_not_zero(doc):
    c = credit.parse_credit(doc, now=NOW)
    assert c["state"] == "none"
    assert c["remaining"] is None and c["granted"] is None
    assert not credit.has_plan_credit(doc)


def test_missing_amounts_are_unknown_and_flat_minor_units_are_a_fallback():
    doc = json.loads(json.dumps(BALANCE))
    t = doc["promo_tranches"][0]
    for k in ("remaining", "remaining_amount_minor_units", "expires_at"):
        t.pop(k)
    t.pop("granted")  # only the flat granted_amount_minor_units is left
    c = credit.parse_credit(doc, now=NOW)
    assert c["state"] == "ok"
    assert c["remaining"] is None          # unknown, never 0
    assert c["granted"] == 200.0
    assert c["expires_at"] is None


def test_a_lapsed_month_is_gone():
    assert credit.parse_credit(BALANCE, now=datetime(2026, 11, 21, 0, 1, tzinfo=timezone.utc))["state"] == "none"


# ---------------------------------------------------------------- picking the Console org


def test_picks_the_api_org_that_holds_the_plan_credit():
    other = {"uuid": "33333333-3333-4333-8333-333333333333", "capabilities": ["api"], "billing_type": "prepaid"}
    orgs = [ORGS[0], other, ORGS[1]]
    assert credit.pick_console_org(orgs, {other["uuid"]: no_plan_credit(), CONSOLE_ORG: BALANCE}) == CONSOLE_ORG
    # none has it yet (e.g. before the first grant): the first API org, never the claude.ai one
    assert credit.pick_console_org(orgs, {}) == other["uuid"]
    assert credit.pick_console_org([ORGS[0]], {}) is None
    assert credit.pick_console_org("not a list", {}) is None


# ---------------------------------------------------------------- identity check on sign-in


def test_identity_check_accepts_this_accounts_session_and_stores_it():
    calls = []
    org = console_signin.complete(
        "sk-secret", IDENTITY, CLAUDE_ORG,
        get_orgs=lambda key: calls.append(("orgs", key)) or ORGS,
        get_balance=lambda key, uuid: calls.append(("bal", uuid)) or BALANCE)
    assert org == CONSOLE_ORG
    assert ("bal", CLAUDE_ORG) not in calls  # only API orgs are read
    s = credit.load_session(IDENTITY)
    assert s["session_key"] == "sk-secret" and s["console_org_uuid"] == CONSOLE_ORG
    assert s["claude_org_uuid"] == CLAUDE_ORG and s["connected_at"]
    assert credit.session_path(IDENTITY).parent.parent.name != "webview"
    assert credit.load_session("b" * 64) is None  # another login sees nothing


@pytest.mark.parametrize("claude_org", ["99999999-9999-4999-8999-999999999999", None, ""])
def test_identity_check_rejects_another_accounts_session_and_stores_nothing(claude_org):
    with pytest.raises(console_signin.SignInError) as e:
        console_signin.complete("sk-other", IDENTITY, claude_org,
                                get_orgs=lambda key: ORGS, get_balance=lambda key, uuid: BALANCE)
    assert e.value.code == console_signin.MISMATCH
    assert "另一个账号" in str(e.value)
    assert credit.load_session(IDENTITY) is None
    assert not credit.session_path(IDENTITY).exists()


def test_a_session_file_for_another_login_is_ignored():
    credit.save_session(IDENTITY, "sk", CONSOLE_ORG, CLAUDE_ORG)
    doc = json.loads(credit.session_path(IDENTITY).read_text(encoding="utf-8"))
    doc["identity_key"] = "b" * 64
    credit.session_path(IDENTITY).write_text(json.dumps(doc), encoding="utf-8")
    assert credit.load_session(IDENTITY) is None


# ---------------------------------------------------------------- fetching


def test_fetch_reads_at_most_every_ten_minutes_and_401_means_reconnect(monkeypatch):
    credit.save_session(IDENTITY, "sk", CONSOLE_ORG, CLAUDE_ORG)
    answers = [BALANCE, HTTPStatusError(401, "{}", "u"), ProviderError("network error for u: down")]
    calls = []

    def get_balance(key, org):
        calls.append((key, org))
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    monkeypatch.setattr(credit, "get_balance", get_balance)
    t0 = 1_000_000.0
    c = credit.fetch_credit(IDENTITY, now=t0)
    assert c["state"] == "ok" and c["remaining"] == 199.99
    assert credit.fetch_credit(IDENTITY, now=t0 + 599) is c   # cached between reads
    assert len(calls) == 1 and calls[0] == ("sk", CONSOLE_ORG)
    c = credit.fetch_credit(IDENTITY, now=t0 + 601)
    assert c["state"] == "reconnect" and c["remaining"] == 199.99   # last amounts kept, marked
    c = credit.fetch_credit(IDENTITY, now=t0 + 1300)
    assert c["state"] == "stale" and c["granted"] == 200.0
    assert len(calls) == 3


def test_403_also_means_reconnect_and_a_new_session_reads_at_once(monkeypatch):
    credit.save_session(IDENTITY, "sk-old", CONSOLE_ORG, CLAUDE_ORG)

    def refused(key, org):
        if key == "sk-old":
            raise HTTPStatusError(403, "{}", "u")
        return BALANCE

    monkeypatch.setattr(credit, "get_balance", refused)
    c = credit.fetch_credit(IDENTITY, now=0)
    assert c["state"] == "reconnect" and c["remaining"] is None   # never seen: unknown, not 0
    credit.save_session(IDENTITY, "sk-new", CONSOLE_ORG, CLAUDE_ORG)
    assert credit.fetch_credit(IDENTITY, now=1)["state"] == "ok"


def test_no_session_no_credit_and_attach_only_touches_claude(monkeypatch):
    monkeypatch.setattr(credit, "get_balance", lambda *a: pytest.fail("no network without a session"))
    assert credit.fetch_credit(IDENTITY) is None
    assert credit.fetch_credit(None) is None
    u = credit.attach(Usage("w", "claude", True, identity_key=IDENTITY))
    assert u.credit is None
    credit.save_session(IDENTITY, "sk", CONSOLE_ORG, CLAUDE_ORG)
    x = credit.attach(Usage("x", "codex", True, identity_key=IDENTITY))
    assert x.credit is None


def test_the_session_key_never_reaches_the_log(monkeypatch, caplog):
    credit.save_session(IDENTITY, "sk-very-secret", CONSOLE_ORG, CLAUDE_ORG)
    monkeypatch.setattr(credit, "get_balance", lambda *a: (_ for _ in ()).throw(HTTPStatusError(401, "sk-very-secret", "u")))
    with caplog.at_level("INFO"):
        credit.fetch_credit(IDENTITY, now=0)
    assert "sk-very-secret" not in caplog.text


# ---------------------------------------------------------------- cache round trip


def test_usage_round_trip_keeps_the_credit_and_old_caches_have_none():
    c = {**credit.parse_credit(BALANCE, now=NOW), "fetched_at": NOW.isoformat()}
    u = Usage("w", "claude", True, plan="max 20x", credit=c)
    back = Usage.from_dict(json.loads(json.dumps(u.to_dict())))
    assert back.credit == c
    old = u.to_dict()
    old.pop("credit")
    assert Usage.from_dict(old).credit is None
    assert Usage.from_dict({**old, "credit": "junk"}).credit is None


def test_monitor_keeps_the_last_credit_across_a_restart_when_reads_fail():
    c = {**credit.parse_credit(BALANCE, now=NOW), "fetched_at": NOW.isoformat()}
    good = [Usage("w", "claude", True, plan="max 20x", windows=[Window("5h", 10.0)], identity_key=IDENTITY, credit=c)]
    Monitor(collect=lambda a, refresh_tokens=False: good).poll()

    stale = {"state": "stale", "granted": None, "remaining": None, "currency": None, "expires_at": None}
    for ok in (True, False):  # the usage read itself fine, or failed as well
        u = Usage("w", "claude", ok, plan="max 20x", windows=[Window("5h", 12.0)] if ok else [],
                  error=None if ok else "network error", identity_key=IDENTITY, credit=dict(stale))
        m = Monitor(collect=lambda a, refresh_tokens=False, u=u: [u])
        m.poll()
        shown = m.payload()["usages"][0]["credit"]
        assert shown["state"] == "stale" and shown["remaining"] == 199.99 and shown["granted"] == 200.0

    # another login in that dir now: its (missing) credit is not filled from the old one
    other = Usage("w", "claude", True, windows=[Window("5h", 1.0)], identity_key="b" * 64, credit=dict(stale))
    m = Monitor(collect=lambda a, refresh_tokens=False: [other])
    m.poll()
    assert m.usages[0].credit["remaining"] is None


# ---------------------------------------------------------------- terminal


def test_render_line():
    c = credit.parse_credit(BALANCE, now=NOW)
    day = datetime.fromisoformat(c["expires_at"]).astimezone().strftime("%m-%d")
    days = int((datetime.fromisoformat(c["expires_at"]) - NOW).total_seconds() // 86400)
    assert credit_line(c, now=NOW) == f"API credit  $0.01 / $200.00 · $199.99 left · ↻ {day} {days}d"
    assert days == 27
    assert credit_line({**c, "state": "reconnect"}, now=NOW, name="work").endswith(" · reconnect: redline console work")
    assert credit_line({**c, "state": "stale"}, now=NOW).endswith(" · stale")
    assert credit_line({"state": "reconnect", "granted": None, "remaining": None, "expires_at": None}, now=NOW) \
        == "API credit  $? / $? · $? left · reconnect"
    assert credit_line({"state": "none"}, now=NOW) == "API credit  –"
    assert credit_line(None) is None
    out = render_table([Usage("work", "claude", True, credit=c)], stream=StringIO())
    assert "    API credit  $0.01 / $200.00 · $199.99 left" in out
    assert "API credit" not in render_table([Usage("work", "claude", True)], stream=StringIO())
    assert "subscription_api_credit" not in out and CONSOLE_ORG not in out

    class GbkPipe(StringIO):  # a redirected Chinese-Windows console can't encode the arrow
        encoding = "gbk"

    gbk = render_table([Usage("work", "claude", True, credit=c)], stream=GbkPipe())
    assert " · -> " in gbk and "↻" not in gbk.split("API credit")[1]
    gbk.split("API credit")[1].encode("gbk")


# ---------------------------------------------------------------- the sign-in window (faked)


class FakeWindow:
    """Stands in for a pywebview Window: the page answers the probe as `answers` say."""

    def __init__(self, urls, answers, cookie="sk-live", domain=".platform.claude.com"):
        self.urls, self.answers, self.ran = list(urls), list(answers), []
        jar = SimpleCookie()
        if cookie:
            jar["sessionKey"] = cookie
            jar["sessionKey"]["domain"] = domain
        self.jar = jar

    def get_current_url(self):
        return self.urls.pop(0) if len(self.urls) > 1 else self.urls[0]

    def run_js(self, script):
        assert "sk-" not in script  # the session never goes to the page
        self.ran.append(script)

    def evaluate_js(self, script):
        assert script == console_signin.READ_JS
        return json.dumps(self.answers.pop(0)) if self.answers else "null"

    def get_cookies(self):
        return [self.jar]


def fake_clock():
    t = [0.0]
    return (lambda s: t.__setitem__(0, t[0] + s)), (lambda: t[0])


def test_watch_waits_for_the_sign_in_then_hands_over_the_session():
    win = FakeWindow(["https://platform.claude.com/login", "https://accounts.google.com/x",
                      "https://platform.claude.com/settings/billing"],
                     [{"s": 401, "b": ""}, {"s": 200, "b": "[]"}])
    got = []
    sleep, clock = fake_clock()
    assert console_signin.watch(win, got.append, sleep=sleep, clock=clock) == console_signin.OK
    assert got == ["sk-live"]
    assert all("window.__x" in s and "/api/organizations" in s for s in win.ran)


def test_watch_stops_when_the_window_closes_or_times_out():
    sleep, clock = fake_clock()
    win = FakeWindow(["https://platform.claude.com/"], [{"s": 401}] * 1000)
    assert console_signin.watch(win, lambda k: pytest.fail("not signed in"), closed=lambda: True,
                                sleep=sleep, clock=clock) == console_signin.CANCELLED
    assert console_signin.watch(win, lambda k: pytest.fail("not signed in"), sleep=sleep, clock=clock,
                                timeout=30) == console_signin.CANCELLED


def test_watch_passes_a_mismatch_up():
    sleep, clock = fake_clock()
    win = FakeWindow(["https://platform.claude.com/"], [{"s": 200}])

    def finish(key):
        raise console_signin.SignInError(console_signin.MISMATCH)

    with pytest.raises(console_signin.SignInError):
        console_signin.watch(win, finish, sleep=sleep, clock=clock)


def test_session_cookie_only_from_the_console():
    assert console_signin.session_cookie(FakeWindow(["x"], []).get_cookies()) == "sk-live"
    assert console_signin.session_cookie(FakeWindow(["x"], [], domain=".evil.example").get_cookies()) is None
    assert console_signin.session_cookie(FakeWindow(["x"], [], cookie=None).get_cookies()) is None
    assert console_signin.session_cookie(None) is None


def test_console_action_opens_one_window_per_account_and_reports_back(monkeypatch):
    from redline.actions import ActionError
    from redline.config import Account

    monkeypatch.setattr(console_signin, "webview_available", lambda: True)
    acct = Account("work", "claude", "/nowhere")
    release, started = threading.Event(), []

    class Proc:
        def wait(self):
            release.wait(5)
            return console_signin.MISMATCH

    def popen(argv, **kw):
        started.append(argv)
        return Proc()

    logs, done = [], threading.Event()
    msg = console_signin.start(acct, lambda lvl, text: logs.append((lvl, text)), on_done=done.set, popen=popen)
    assert "work" in msg
    assert started[0][-2:] == ["console", "claude:work"]
    with pytest.raises(ActionError):  # already open
        console_signin.start(acct, lambda *a: None, popen=popen)
    release.set()
    assert done.wait(5)
    assert logs == [("error", "work: " + console_signin.MESSAGES[console_signin.MISMATCH])]
    with pytest.raises(ActionError):
        console_signin.start(Account("x", "codex", "/nowhere"), lambda *a: None, popen=popen)
    monkeypatch.setattr(console_signin, "webview_available", lambda: False)
    with pytest.raises(ActionError, match="pywebview"):
        console_signin.start(acct, lambda *a: None, popen=popen)


def test_cli_has_the_console_command():
    from redline.cli import build_parser

    args = build_parser().parse_args(["console", "work"])
    assert args.name == "work" and args.func.__name__ == "cmd_console"


# ---------------------------------------------------------------- console page


def test_console_tag_details_and_connect_button(run_console_js):  # noqa: F811
    run_console_js(r"""
(() => {
  const at = days => new Date(Date.now() + days * 86400000).toISOString();
  const cr = (remaining, days, state = "ok", granted = 200) => ({state, granted, remaining, currency: "USD", expires_at: at(days)});
  const claude = (name, changes = {}) => ({...usage(name, {plan: "max 20x", weekly_countdown: null, ...changes}), provider: "claude"});
  const a = claude("a", {credit: cr(199.99, 27.5)}), b = claude("b", {plan: "pro"});
  DATA = {usages: [a, b], meta: {accounts: {}}};
  // whole dollars, rounded down, in its own slot after SUB; every Claude row gets the slot
  const slots = html => [...html.matchAll(/<div class="(rate[^"]*|slot[^"]*|cred[^"]*)"/g)]
    .map(m => m[1].startsWith("rate") ? "rate" : m[1]);
  assert.deepEqual(slots(row(a, null)), ["rate", "slot", "slot cr", "cred usd "]);
  assert.deepEqual(slots(row(b, null)), ["rate", "slot", "slot cr"]);
  assert.match(creditTag(a), />\$199<\/div>$/);
  assert.match(creditTag(a), /title="API credit · [^"]+ 到期"/);
  // colours: amber at 30% left, red at 10%, cyan = 20%+ left and it lapses within 3 days
  assert.equal(creditLevel(creditOf(claude("x", {credit: cr(150, 20)}))), "");
  assert.equal(creditLevel(creditOf(claude("x", {credit: cr(60, 20)}))), "mid");
  assert.equal(creditLevel(creditOf(claude("x", {credit: cr(20, 20)}))), "high");
  assert.equal(creditLevel(creditOf(claude("x", {credit: cr(150, 2.5)}))), "urgent");
  assert.equal(creditLevel(creditOf(claude("x", {credit: cr(30, 2.5)}))), "mid");
  assert.equal(creditLevel(creditOf(claude("x", {credit: cr(10, 2.5)}))), "high");
  assert.match(creditTag(claude("x", {credit: cr(null, 9)})), />\$\?</);   // unknown, not $0
  assert.match(creditTag(claude("x", {credit: cr(150, 9, "stale")})), /class="cred usd  old"/);
  assert.equal(creditTag(claude("x", {credit: cr(150, -1)})), "");        // lapsed: no rollover
  assert.equal(creditTag(claude("x", {credit: {state: "none"}})), "");
  // details: one line in the detail style
  const line = creditDetail(a);
  const date = (() => { const d = new Date(a.credit.expires_at); return `${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; })();
  assert.match(line, new RegExp(`<span>API credit</span><div class="prj"><b class="" title="" data-credit="[^"]+" data-text="[^"]+">\\$0\\.01 / \\$200\\.00 · \\$199\\.99 left · ↻ ${date} 27d</b></div>`));
  assert.doesNotMatch(line, /button/);
  // one entry point: connect on a paid Max / Team account without a session, reconnect when refused
  assert.match(creditDetail(claude("c")), /<b>–<\/b><button class="act" data-act="console">连接 Console<\/button>/);
  assert.match(creditDetail(claude("t", {plan: "team"})), />连接 Console</);
  assert.match(creditDetail(claude("r", {credit: cr(150, 9, "reconnect")})), />重新连接<\/button>/);
  assert.equal(creditDetail(b), "");
  assert.equal(creditDetail(claude("f", {plan: "free"})), "");
  assert.equal(creditDetail(usage("codex-one")), "");
  assert.match(details(claude("c")), /data-act="console"/);
  const html = row(a, null) + row(claude("r", {credit: cr(150, 9, "reconnect")}), null);
  assert.doesNotMatch(html, /subscription_api_credit|sessionKey|organizations|uuid|Max 20x\)/);
})();
""")


def test_console_button_asks_the_server_to_connect(run_console_js):  # noqa: F811
    run_console_js(r"""
(async () => {
  const calls = [];
  api = async (path, opts) => { calls.push(JSON.parse(opts.body)); return {ok: true, message: "ok"}; };
  const card = {dataset: {name: "work"}};
  const button = {dataset: {act: "console"}, closest: s => s === ".acct" ? card : null};
  await elements["#list"].listeners.click[0]({target: {closest: s => s === "[data-act]" ? button : null}});
  assert.deepEqual(calls, [{action: "console-connect", provider: "claude", name: "work"}]);
})();
""")
