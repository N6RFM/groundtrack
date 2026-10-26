#!/usr/bin/env python3
"""
Local web dashboard for this computer's ground stations.

    python3 dashboard.py            # then open http://127.0.0.1:8750
    python3 dashboard.py --port 9000

Shows, for every station on THIS computer: whether run_passes.py is running, the next
pass with a live countdown, the upcoming queue, what was recorded in the last 24 h,
TLE age, IQ disk space and any problems (the same checks as status.py).

Read-only. Listens on 127.0.0.1 only, so nothing leaves this computer and nothing goes
through GitHub. The page refreshes itself every 30 seconds.
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import status

DEFAULT_PORT = 8750


def snapshot(hours=24):
    now = datetime.now(timezone.utc)
    stations = []
    for n, l, d in status.stations_here():
        try:
            stations.append(status.collect(n, l, d, hours, now))
        except Exception as e:  # one broken station must not blank the page
            stations.append({"name": n, "label": l, "configured": True, "pid": None, "next": None,
                             "queued": None, "upcoming": [], "recent": [], "recent_hours": hours,
                             "tle": None, "disks": [], "grid": "", "problems": [f"could not read this station: {e}"]})
    return {"site": status.site_name(), "zone": now.astimezone().strftime("%Z (UTC%z)"), "now": now.isoformat(), "now_ts": now.timestamp(),
            "now_local": now.astimezone().strftime("%a %Y-%m-%d %H:%M:%S %Z"),
            "stations": stations}


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>groundtrack dashboard</title>
<style>
:root{--bg:#f5f6f8;--card:#fff;--fg:#1c2330;--mut:#667085;--line:#e3e6eb;--ok:#117a3d;--bad:#b42318;--warn:#a15c00;--info:#175cd3}
@media (prefers-color-scheme:dark){:root{--bg:#10141b;--card:#1a212c;--fg:#e6e9ef;--mut:#98a2b3;--line:#2b3441;--ok:#4ade80;--bad:#f87171;--warn:#fbbf24;--info:#7aa7ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif;padding:16px}
header{display:flex;justify-content:space-between;flex-wrap:wrap;gap:4px;margin-bottom:12px}
h1{font-size:18px;margin:0}.mut{color:var(--mut)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:14px}
.card h2{font-size:16px;margin:0 0 8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.pill{font-size:12px;padding:2px 8px;border-radius:99px;border:1px solid currentColor}
.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}.info{color:var(--info)}
.next{font-size:20px;font-weight:600;margin:4px 0}
.probs{border-left:4px solid var(--bad);padding:6px 10px;margin:8px 0;background:rgba(180,35,24,.08)}
.probs div{margin:2px 0}.clear{border-left-color:var(--ok);background:rgba(17,122,61,.08)}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:4px 8px 4px 0;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--mut);font-weight:500}.scroll{overflow-x:auto}
h3{font-size:13px;color:var(--mut);margin:12px 0 4px;text-transform:uppercase;letter-spacing:.04em}
</style></head><body>
<header><h1 id="site">groundtrack</h1><div class="mut" id="stamp">loading...</div></header>
<div id="root"></div>
<script>
let snap=null, offset=0;
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function human(sec){sec=Math.max(0,Math.round(sec));const d=Math.floor(sec/86400),h=Math.floor(sec%86400/3600),m=Math.floor(sec%3600/60),s=sec%60;
 if(d)return d+"d "+h+"h";if(h)return h+"h "+String(m).padStart(2,"0")+"m";if(m)return m+"m "+String(s).padStart(2,"0")+"s";return s+"s"}
function nowTs(){return Date.now()/1000+offset}
function render(){
 if(!snap)return;
 document.getElementById("site").textContent="groundtrack - "+snap.site;
 document.title="groundtrack - "+snap.site;
 document.getElementById("stamp").textContent="updated "+snap.now_local+" - times are "+snap.zone+" - refreshes every 30 s";
 const t=nowTs();
 document.getElementById("root").innerHTML=snap.stations.map(s=>{
  const title=(s.name?s.name.toUpperCase()+" - ":"")+esc(s.label||"Station")+(s.grid?` <span class="mut" style="font-weight:400">grid ${esc(s.grid)}</span>`:"");
  if(!s.configured)return `<div class="card"><h2>${title}</h2><div class="mut">No satellites.yaml here.</div></div>`;
  const run=s.pid?`<span class="pill ok">run_passes running (PID ${s.pid})</span>`:`<span class="pill bad">run_passes NOT running</span>`;
  let next='<div class="mut">No upcoming approved passes.</div>';
  if(s.next){const n=s.next,live=t>=n.aos_ts&&t<n.los_ts;
   next=`<div class="next">${esc(n.name)} - ${live?'<span class="ok">ACTIVE, sets in '+human(n.los_ts-t)+'</span>':'in '+human(n.aos_ts-t)}</div>
   <div class="mut">${esc(n.local)} local, ${n.max_el??"?"}&deg; max ${esc(n.track)}${s.queued?" - "+s.queued.count+" queued through "+esc(s.queued.through):""}</div>`}
  const probs=s.problems.length?`<div class="probs">${s.problems.map(p=>"<div>! "+esc(p)+"</div>").join("")}</div>`:`<div class="probs clear">All clear</div>`;
  const up=s.upcoming.filter(p=>p.los_ts>t).slice(0,12).map(p=>`<tr><td>${esc(p.local)}</td><td>${esc(p.name)}</td><td>${p.max_el??"?"}&deg;</td><td>${esc(p.direction)}</td><td>${esc(p.track)}</td><td>${t>=p.aos_ts?"now":"in "+human(p.aos_ts-t)}</td></tr>`).join("");
  const rec=s.recent.slice().reverse().map(e=>`<tr><td>${esc(e.local)}</td><td>${esc(e.satellite)}</td><td class="${e.level}">${esc(e.mark)}</td><td class="mut">${esc(e.extra)}</td></tr>`).join("");
  const tle=s.tle?`TLE age: newest ${s.tle.newest.toFixed(1)} d, oldest ${s.tle.oldest.toFixed(1)} d`:"";
  const disk=s.disks.map(d=>`IQ disk: ${d.free_gb.toFixed(0)} GB free`).join(" &middot; ");
  return `<div class="card"><h2>${title} ${run}</h2>${next}${probs}
   <h3>Upcoming</h3><div class="scroll">${up?`<table><tr><th>Local</th><th>Satellite</th><th>Max el</th><th>Dir</th><th>Track</th><th>Starts</th></tr>${up}</table>`:'<div class="mut">nothing queued</div>'}</div>
   <h3>Last ${s.recent_hours} h</h3><div class="scroll">${rec?`<table><tr><th>Local</th><th>Satellite</th><th>Result</th><th>Detail</th></tr>${rec}</table>`:'<div class="mut">no passes logged</div>'}</div>
   <div class="mut" style="margin-top:10px">${[tle,disk].filter(Boolean).join(" &middot; ")}</div></div>`}).join("");
}
async function load(){
 try{const r=await fetch("api/status",{cache:"no-store"});snap=await r.json();offset=snap.now_ts-Date.now()/1000;render()}
 catch(e){document.getElementById("stamp").textContent="dashboard server not reachable - retrying"}
}
load();setInterval(load,30000);setInterval(render,1000);
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, ctype, body):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", PAGE)
        elif path == "/api/status":
            self._send(200, "application/json", json.dumps(snapshot()))
        else:
            self._send(404, "text/plain", "not found")

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args()
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as e:
        sys.exit(f"cannot listen on port {args.port}: {e}\n(already running? try --port {args.port + 1})")
    print(f"groundtrack dashboard: http://127.0.0.1:{args.port}   (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
