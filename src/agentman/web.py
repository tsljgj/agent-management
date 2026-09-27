"""Minimal local dashboard: `agentman serve` then open http://127.0.0.1:8765."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import load_accounts

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agent Usage</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1b1f24;--muted:#6b7280;--track:#e5e7eb;--ok:#16a34a;--warn:#d97706;--bad:#dc2626;--border:#e5e7eb}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#171a21;--fg:#e6e8eb;--muted:#9aa3ae;--track:#2a2f39;--border:#262b35}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
header{display:flex;justify-content:space-between;align-items:baseline;padding:20px 16px 8px;max-width:1100px;margin:auto}
h1{font-size:18px;margin:0}#meta{color:var(--muted);font-size:12px}
main{display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));padding:8px 16px 24px;max-width:1100px;margin:auto}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px}
.top{display:flex;justify-content:space-between;gap:8px}.name{font-weight:600}.prov{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
.who{color:var(--muted);font-size:12px;margin:2px 0 10px;overflow-wrap:anywhere}
.w{margin:8px 0}.wl{display:flex;justify-content:space-between;font-size:12px}.wl span:last-child{color:var(--muted)}
.track{height:8px;background:var(--track);border-radius:4px;overflow:hidden;margin-top:3px}.fill{height:100%;border-radius:4px}
.err{color:var(--bad);font-size:12px;overflow-wrap:anywhere}.extra{font-size:12px;color:var(--muted)}
button{background:none;border:1px solid var(--border);color:var(--fg);border-radius:6px;padding:4px 10px;cursor:pointer}
</style></head><body>
<header><div><h1>Agent usage</h1><div id="meta">loading…</div></div><button id="r">Refresh</button></header>
<main id="grid"></main>
<script>
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function until(iso){if(!iso)return"";let s=(new Date(iso)-Date.now())/1000;if(s<=0)return"resets now";
 const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);
 return "resets in "+(d?`${d}d ${h}h`:h?`${h}h ${m}m`:`${m}m`)}
const col=p=>p==null?"var(--muted)":p>=90?"var(--bad)":p>=70?"var(--warn)":"var(--ok)";
function card(u){let h=`<div class="card"><div class="top"><span class="name">${esc(u.account)}</span><span class="prov">${esc(u.provider)}</span></div>
 <div class="who">${esc(u.email||"")} ${u.plan?"· "+esc(u.plan):""}</div>`;
 if(!u.ok)h+=`<div class="err">${esc(u.error)}</div>`;
 for(const w of u.windows){const p=w.used_percent;h+=`<div class="w"><div class="wl"><span>${esc(w.name)} · <b>${p==null?"?":Math.round(p)}%</b></span><span>${until(w.resets_at)}</span></div>
  <div class="track"><div class="fill" style="width:${Math.min(100,p||0)}%;background:${col(p)}"></div></div></div>`}
 for(const [k,v] of Object.entries(u.extra||{}))h+=`<div class="extra">${esc(k)}: ${esc(v)}</div>`;
 return h+"</div>"}
async function load(force){const r=await fetch("api/usage"+(force?"?force=1":""));const d=await r.json();
 document.getElementById("grid").innerHTML=d.usages.length?d.usages.map(card).join(""):'<div class="card">No accounts. Run <code>agentman add claude &lt;name&gt;</code>.</div>';
 document.getElementById("meta").textContent="updated "+new Date(d.fetched_at*1000).toLocaleTimeString()}
document.getElementById("r").onclick=()=>load(true);load(false);setInterval(()=>load(false),60000);
</script></body></html>"""


class _Cache:
    def __init__(self, min_interval: int, refresh_tokens: bool):
        self.min_interval = min_interval
        self.refresh_tokens = refresh_tokens
        self.lock = threading.Lock()
        self.at = 0.0
        self.data: list[dict] = []

    def get(self, force: bool) -> dict:
        from .cli import collect

        with self.lock:
            # "force" still respects a short floor so a stuck button can't hammer the APIs.
            floor = 15 if force else self.min_interval
            if time.time() - self.at >= floor:
                usages = collect(load_accounts(), refresh_tokens=self.refresh_tokens)
                self.data = [u.to_dict() for u in usages]
                self.at = time.time()
            return {"fetched_at": self.at, "usages": self.data}


def serve(host: str, port: int, min_interval: int = 60, refresh_tokens: bool = False) -> None:
    cache = _Cache(min_interval, refresh_tokens)

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path, _, query = self.path.partition("?")
            if path == "/":
                self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            elif path == "/api/usage":
                payload = cache.get(force="force=1" in query)
                self._send(200, json.dumps(payload).encode(), "application/json")
            else:
                self._send(404, b"not found", "text/plain")

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"agentman dashboard on http://{host}:{port}  (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
