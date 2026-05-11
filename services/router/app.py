import hashlib
import os
import random
import sys
import time
from typing import Dict, List, Literal, Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response
from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST

# Add shared config to path — tries Docker layout (/app/shared) then local dev (../shared)
_this_dir = os.path.dirname(os.path.abspath(__file__))
for _p in [os.path.join(_this_dir, "shared"), os.path.join(_this_dir, "..", "shared")]:
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)
        break
from config import BackendConfig, load_backends, backend_names  # noqa: E402

Backend = str
Mode = Literal["weighted", "force"]

ADMIN_TOKEN = os.getenv("ROUTER_ADMIN_TOKEN", "changeme")
CB_FAIL_THRESHOLD = int(os.getenv("CB_FAIL_THRESHOLD", "5"))
CB_OPEN_SECONDS = int(os.getenv("CB_OPEN_SECONDS", "20"))
SHADOW_PERCENT = float(os.getenv("SHADOW_PERCENT", "0.0"))
STICKY_HEADER = os.getenv("STICKY_HEADER", "X-Sticky-Key")

# Load backends from shared config (backends.json or legacy env vars)
BACKENDS: Dict[str, BackendConfig] = load_backends()
_NAMES: List[str] = backend_names()

# Global routing state — built dynamically from BACKENDS
MODE: Mode = "weighted"
FORCE_BACKEND: Optional[str] = None
WEIGHTS: Dict[str, float] = {n: (1.0 if i == 0 else 0.0) for i, n in enumerate(_NAMES)}
_cb: Dict[str, dict] = {n: {"fails": 0.0, "open_until": 0.0} for n in _NAMES}

app = FastAPI(
    title="MultiCloud Traffic Router",
    description=(
        "Intelligent HTTP traffic router with **weighted canary distribution**, "
        "per-backend **circuit breakers**, **sticky sessions**, and **shadow traffic** mirroring. "
        "Supports N backends loaded from a shared registry (`backends.json`)."
    ),
    version="3.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

REQS = Counter(
    "router_backend_requests_total",
    "Router->backend requests",
    ["backend", "kind", "code_class"],
)
RESP_BYTES = Counter(
    "router_backend_response_bytes_total",
    "Bytes returned from backend (response size)",
    ["backend", "kind"],
)
LAT = Histogram(
    "router_backend_latency_seconds",
    "Router->backend latency seconds",
    ["backend", "kind"],
    buckets=(0.01, 0.025, 0.05, 0.075, 0.1, 0.15, 0.2, 0.35, 0.5, 1.0, 2.0, 5.0),
)
CB_OPEN = Gauge("router_circuit_open", "Circuit breaker open (1/0)", ["backend"])
ROUTER_MODE = Gauge("router_mode", "Router mode (0=weighted, 1=force)")
W_GAUGE = Gauge("router_weight", "Configured weight", ["backend"])


def _base_url(b: str) -> str:
    return BACKENDS[b].url


def _code_class(code: int) -> str:
    if 200 <= code < 300:
        return "2xx"
    if 300 <= code < 400:
        return "3xx"
    if 400 <= code < 500:
        return "4xx"
    return "5xx"


def _cb_is_open(b: str) -> bool:
    return time.time() < _cb[b]["open_until"]


def _cb_record(b: str, ok: bool) -> None:
    if ok:
        _cb[b]["fails"] = 0.0
        return
    _cb[b]["fails"] += 1.0
    if _cb[b]["fails"] >= CB_FAIL_THRESHOLD:
        _cb[b]["open_until"] = time.time() + CB_OPEN_SECONDS
        _cb[b]["fails"] = 0.0


def _sticky_pick(key: str, eligible: Dict[str, float]) -> str:
    """Deterministic backend selection via SHA-256 hash over N eligible backends."""
    h = hashlib.sha256(key.encode("utf-8")).hexdigest()
    x = int(h[:12], 16) / float(16 ** 12)
    names = sorted(eligible.keys())
    total = sum(max(0.0, eligible[n]) for n in names)
    if total <= 0:
        return names[0]
    cumulative = 0.0
    for n in names:
        cumulative += max(0.0, eligible[n]) / total
        if x < cumulative:
            return n
    return names[-1]


def _choose_backend(sticky_key: Optional[str]) -> str:
    """Pick primary backend, respecting circuit breakers, mode, and weights."""
    open_map = {n: _cb_is_open(n) for n in _NAMES}
    for n in _NAMES:
        CB_OPEN.labels(backend=n).set(1 if open_map[n] else 0)

    if MODE == "force" and FORCE_BACKEND and FORCE_BACKEND in _NAMES:
        if not open_map[FORCE_BACKEND]:
            return FORCE_BACKEND
        # Force target is open — fall through to weighted among remaining
        eligible = {n: WEIGHTS[n] for n in _NAMES if n != FORCE_BACKEND and not open_map[n]}
    else:
        eligible = {n: WEIGHTS[n] for n in _NAMES if not open_map[n] and WEIGHTS[n] > 0}

    if not eligible:
        # All eligible by circuit state are open — last-resort: pick any non-open or just first
        non_open = [n for n in _NAMES if not open_map[n]]
        return non_open[0] if non_open else _NAMES[0]

    if sticky_key:
        return _sticky_pick(sticky_key, eligible)

    total = sum(max(0.0, v) for v in eligible.values())
    if total <= 0:
        return sorted(eligible.keys())[0]
    r = random.random() * total
    cumulative = 0.0
    for n in sorted(eligible.keys()):
        cumulative += max(0.0, eligible[n])
        if r < cumulative:
            return n
    return sorted(eligible.keys())[-1]


async def _health(b: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(_base_url(b) + "/health")
            return r.status_code == 200
    except Exception:
        return False


async def _send_shadow(method: str, url: str, params: dict, content: bytes, headers: dict, backend: str):
    start = time.time()
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.request(method=method, url=url, params=params, content=content, headers=headers)
        REQS.labels(backend=backend, kind="shadow", code_class=_code_class(r.status_code)).inc()
        RESP_BYTES.labels(backend=backend, kind="shadow").inc(len(r.content) if r.content else 0)
    except Exception:
        REQS.labels(backend=backend, kind="shadow", code_class="5xx").inc()
    finally:
        LAT.labels(backend=backend, kind="shadow").observe(time.time() - start)


@app.get("/target")
def get_target():
    return {
        "mode": MODE,
        "force_backend": FORCE_BACKEND,
        "weights": WEIGHTS,
        "backends": {k: v.url for k, v in BACKENDS.items()},
        "cb": _cb,
        "shadow_percent": SHADOW_PERCENT,
    }


@app.post("/admin/mode")
def set_mode(payload: dict, x_admin_token: Optional[str] = Header(default=None)):
    global MODE, FORCE_BACKEND
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="invalid admin token")
    m = payload.get("mode")

    # Backward-compat shims for old force_a / force_b values
    if m == "force_a":
        MODE, FORCE_BACKEND = "force", "a"
        return {"mode": MODE, "force_backend": FORCE_BACKEND}
    if m == "force_b":
        MODE, FORCE_BACKEND = "force", "b"
        return {"mode": MODE, "force_backend": FORCE_BACKEND}

    if m == "weighted":
        MODE, FORCE_BACKEND = "weighted", None
        return {"mode": MODE, "force_backend": FORCE_BACKEND}

    if m == "force":
        backend = payload.get("backend")
        if not backend or backend not in BACKENDS:
            raise HTTPException(
                status_code=400,
                detail=f"'backend' must be one of {sorted(BACKENDS.keys())} when mode=force",
            )
        MODE, FORCE_BACKEND = "force", backend
        return {"mode": MODE, "force_backend": FORCE_BACKEND}

    raise HTTPException(status_code=400, detail="mode must be 'weighted' or 'force' (with 'backend')")


@app.post("/admin/weights")
async def set_weights(payload: dict, x_admin_token: Optional[str] = Header(default=None)):
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="invalid admin token")

    new_weights = {}
    for name in _NAMES:
        v = payload.get(name, WEIGHTS[name])
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"weight for '{name}' must be a number")
        if v < 0:
            raise HTTPException(status_code=400, detail=f"weight for '{name}' must be >= 0")
        new_weights[name] = v

    # Reject unknown backends in payload
    unknown = set(payload.keys()) - set(_NAMES)
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown backends: {sorted(unknown)}")

    # Health-check backends receiving positive weight
    for name, w in new_weights.items():
        if w > 0 and not await _health(name):
            raise HTTPException(status_code=502, detail=f"backend '{name}' unhealthy")

    for name in _NAMES:
        WEIGHTS[name] = new_weights[name]
    return {"weights": WEIGHTS}


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/ui", include_in_schema=False)
def router_ui():
    from fastapi.responses import HTMLResponse
    html = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Router — MultiCloud Arbitrage</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#070a12;--surface:#0e1120;--surface2:#161a2c;--border:#252a42;--accent:#2563eb;--green:#22c55e;--amber:#f59e0b;--red:#f43f5e;--text:#f1f5f9;--text-dim:#94a3b8;--text-muted:#475569;--font:'Inter',sans-serif;--mono:'JetBrains Mono',monospace;--radius:14px;--radius-sm:9px}
html{font-family:var(--font);background:var(--bg);color:var(--text)}body{padding:28px 32px;max-width:1100px;margin:0 auto}
.header{display:flex;align-items:center;gap:14px;margin-bottom:28px}
.logo{width:44px;height:44px;background:linear-gradient(135deg,#2563eb,#7c3aed);border-radius:12px;display:flex;align-items:center;justify-content:center;font-size:22px;box-shadow:0 0 24px rgba(37,99,235,.35)}
h1{font-size:1.25rem;font-weight:800;letter-spacing:-.4px;background:linear-gradient(135deg,#60a5fa,#a78bfa);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.sub{font-size:.75rem;color:var(--text-muted);margin-top:3px}
.pill{display:flex;align-items:center;gap:7px;background:var(--surface);border:1px solid var(--border);border-radius:999px;padding:6px 14px;font-size:.75rem;color:var(--text-dim);margin-left:auto}
.dot{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 7px var(--green)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}.dot{animation:pulse 2s infinite}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px;margin-bottom:22px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:20px}
.card-label{font-size:.68rem;text-transform:uppercase;letter-spacing:.7px;color:var(--text-muted);font-weight:600;margin-bottom:8px}
.card-value{font-size:1.5rem;font-weight:800;letter-spacing:-.5px}
.card-value.blue{color:#60a5fa}.card-value.green{color:var(--green)}.card-value.amber{color:var(--amber)}
.section{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:22px;margin-bottom:22px}
.section-title{font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.7px;color:var(--text-muted);margin-bottom:16px;display:flex;align-items:center;gap:8px}
.section-title::before{content:'';display:block;width:3px;height:14px;background:linear-gradient(135deg,#2563eb,#7c3aed);border-radius:99px}
table{width:100%;border-collapse:collapse;font-size:.8rem}
th{padding:9px 14px;text-align:left;font-size:.65rem;text-transform:uppercase;letter-spacing:.6px;color:var(--text-muted);background:var(--surface2);border-bottom:1px solid var(--border);font-weight:700}
td{padding:10px 14px;border-bottom:1px solid var(--border);color:var(--text-dim)}
tr:last-child td{border-bottom:none}tr:hover td{background:var(--surface2)}
.tag{display:inline-block;padding:3px 10px;border-radius:999px;font-size:.65rem;font-weight:700;text-transform:uppercase;letter-spacing:.5px}
.tag-active{background:rgba(34,197,94,.12);color:var(--green);border:1px solid rgba(34,197,94,.3)}
.tag-standby{background:var(--surface2);color:var(--text-muted);border:1px solid var(--border)}
.tag-open{background:rgba(244,63,94,.12);color:var(--red);border:1px solid rgba(244,63,94,.3)}
.wbar-track{background:var(--surface2);border-radius:999px;height:6px;overflow:hidden;width:120px;display:inline-block;vertical-align:middle;margin-left:8px}
.wbar-fill{height:100%;border-radius:999px;background:linear-gradient(90deg,#2563eb,#7c3aed)}
.footer{padding-top:16px;border-top:1px solid var(--border);font-size:.72rem;color:var(--text-muted);display:flex;justify-content:space-between}
.links a{color:#60a5fa;margin-right:14px;text-decoration:none}
</style></head>
<body>
<div class="header">
  <div class="logo">&#128257;</div>
  <div><h1>Traffic Router</h1><div class="sub">MultiCloud Arbitrage Bot &mdash; Router Service</div></div>
  <div class="pill"><div class="dot"></div><span id="liveLabel">Connecting&hellip;</span></div>
</div>
<div class="cards">
  <div class="card"><div class="card-label">Mode</div><div class="card-value blue" id="cMode">&mdash;</div></div>
  <div class="card"><div class="card-label">Active Backend</div><div class="card-value green" id="cActive">&mdash;</div></div>
  <div class="card"><div class="card-label">Shadow Traffic</div><div class="card-value amber" id="cShadow">&mdash;</div></div>
</div>
<div class="section">
  <div class="section-title">Backend Status &amp; Weights</div>
  <table><thead><tr><th>Backend</th><th>URL</th><th>Weight</th><th>Traffic Bar</th><th>Circuit Breaker</th><th>Status</th></tr></thead>
  <tbody id="backendTable"></tbody></table>
</div>
<div class="footer">
  <div class="links"><a href="/docs">Swagger UI</a><a href="/redoc">ReDoc</a><a href="/target">Raw JSON</a><a href="/metrics">Prometheus</a></div>
  <span id="footerTs">Last update: &mdash;</span>
</div>
<script>
async function refresh(){
  try{
    const r=await fetch('/target');const d=await r.json();
    document.getElementById('cMode').textContent=(d.mode||'').toUpperCase();
    const bk=Object.keys(d.weights||{});
    const active=Object.entries(d.weights||{}).sort((a,b)=>b[1]-a[1])[0]?.[0]||'&mdash;';
    document.getElementById('cActive').textContent=active.toUpperCase();
    document.getElementById('cShadow').textContent=(d.shadow_percent*100).toFixed(1)+'% mirror';
    document.getElementById('backendTable').innerHTML=bk.map(b=>{
      const w=(d.weights[b]*100).toFixed(0);
      const cb=d.cb[b]||{};
      const cbOpen=Date.now()/1000<cb.open_until;
      const isActive=b===active;
      return `<tr>
<td style="font-weight:700;color:#f1f5f9">${b.toUpperCase()}</td>
<td style="font-family:'JetBrains Mono',monospace;font-size:.75rem">${d.backends[b]||'&mdash;'}</td>
<td style="font-weight:700;font-family:'JetBrains Mono',monospace">${w}%</td>
<td><div class="wbar-track"><div class="wbar-fill" style="width:${w}%"></div></div></td>
<td><span class="tag ${cbOpen?'tag-open':'tag-standby'}">${cbOpen?'&#9889; Open':'Closed'}</span></td>
<td><span class="tag ${isActive?'tag-active':'tag-standby'}">${isActive?'&#9679; Active':'&#9675; Standby'}</span></td>
</tr>`;
    }).join('');
    document.getElementById('liveLabel').textContent='Live';
    document.getElementById('footerTs').textContent='Last update: '+new Date().toLocaleTimeString();
  }catch(e){document.getElementById('liveLabel').textContent='Error';}
}
refresh();setInterval(refresh,3000);
</script>
</body></html>"""
    return HTMLResponse(content=html)


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(path: str, request: Request):
    ROUTER_MODE.set(0 if MODE == "weighted" else 1)
    for n in _NAMES:
        W_GAUGE.labels(backend=n).set(float(WEIGHTS[n]))

    sticky_key = request.headers.get(STICKY_HEADER)
    primary = _choose_backend(sticky_key)

    # Shadow candidate: random non-primary backend with open circuit excluded
    shadow_candidates = [n for n in _NAMES if n != primary and not _cb_is_open(n)]

    body = await request.body()
    headers = dict(request.headers)
    headers.pop("host", None)
    params = dict(request.query_params)

    primary_url = f"{_base_url(primary)}/{path}"
    start = time.time()

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.request(
                method=request.method,
                url=primary_url,
                params=params,
                content=body,
                headers=headers,
            )
        ok = resp.status_code < 500
        _cb_record(primary, ok=ok)
        REQS.labels(backend=primary, kind="primary", code_class=_code_class(resp.status_code)).inc()
        RESP_BYTES.labels(backend=primary, kind="primary").inc(len(resp.content) if resp.content else 0)
    except Exception:
        _cb_record(primary, ok=False)
        REQS.labels(backend=primary, kind="primary", code_class="5xx").inc()
        return Response(content=b"upstream error", status_code=502)
    finally:
        LAT.labels(backend=primary, kind="primary").observe(time.time() - start)

    if SHADOW_PERCENT > 0 and shadow_candidates and random.random() < SHADOW_PERCENT:
        shadow = random.choice(shadow_candidates)
        shadow_url = f"{_base_url(shadow)}/{path}"
        await _send_shadow(request.method, shadow_url, params, body, headers, backend=shadow)

    out_headers = {}
    ct = resp.headers.get("content-type")
    if ct:
        out_headers["content-type"] = ct
    return Response(content=resp.content, status_code=resp.status_code, headers=out_headers)


@app.get("/metrics")
def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
