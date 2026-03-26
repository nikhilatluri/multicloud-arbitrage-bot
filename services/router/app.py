import hashlib
import os
import random
import time
from typing import Dict, Literal, Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response
from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST

Backend = Literal["a", "b"]
Mode = Literal["weighted", "force_a", "force_b"]

TARGET_A_URL = os.getenv("TARGET_A_URL", "http://localhost:8001")
TARGET_B_URL = os.getenv("TARGET_B_URL", "http://localhost:8002")
ADMIN_TOKEN = os.getenv("ROUTER_ADMIN_TOKEN", "changeme")

CB_FAIL_THRESHOLD = int(os.getenv("CB_FAIL_THRESHOLD", "5"))
CB_OPEN_SECONDS = int(os.getenv("CB_OPEN_SECONDS", "20"))
SHADOW_PERCENT = float(os.getenv("SHADOW_PERCENT", "0.0"))
STICKY_HEADER = os.getenv("STICKY_HEADER", "X-Sticky-Key")

MODE: Mode = "weighted"
WEIGHTS: Dict[Backend, float] = {"a": 1.0, "b": 0.0}

_cb = {
    "a": {"fails": 0.0, "open_until": 0.0},
    "b": {"fails": 0.0, "open_until": 0.0},
}

app = FastAPI(title="router-v3")

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
ROUTER_MODE = Gauge("router_mode", "Router mode (0 weighted,1 force_a,2 force_b)")
W_GAUGE = Gauge("router_weight", "Configured weight", ["backend"])

def _base_url(b: Backend) -> str:
    return TARGET_A_URL if b == "a" else TARGET_B_URL

def _code_class(code: int) -> str:
    if 200 <= code < 300:
        return "2xx"
    if 300 <= code < 400:
        return "3xx"
    if 400 <= code < 500:
        return "4xx"
    return "5xx"

def _cb_is_open(b: Backend) -> bool:
    return time.time() < _cb[b]["open_until"]

def _cb_record(b: Backend, ok: bool) -> None:
    if ok:
        _cb[b]["fails"] = 0.0
        return
    _cb[b]["fails"] += 1.0
    if _cb[b]["fails"] >= CB_FAIL_THRESHOLD:
        _cb[b]["open_until"] = time.time() + CB_OPEN_SECONDS
        _cb[b]["fails"] = 0.0

def _sticky_pick(key: str, wa: float, wb: float) -> Backend:
    h = hashlib.sha256(key.encode("utf-8")).hexdigest()
    x = int(h[:12], 16) / float(16**12)
    total = max(0.0, wa) + max(0.0, wb)
    if total <= 0:
        return "a"
    p_a = max(0.0, wa) / total
    return "a" if x < p_a else "b"

def _choose_backend(sticky_key: Optional[str]) -> Backend:
    a_open, b_open = _cb_is_open("a"), _cb_is_open("b")
    CB_OPEN.labels(backend="a").set(1 if a_open else 0)
    CB_OPEN.labels(backend="b").set(1 if b_open else 0)

    if MODE == "force_a":
        return "b" if a_open and not b_open else "a"
    if MODE == "force_b":
        return "a" if b_open and not a_open else "b"

    if a_open and not b_open:
        return "b"
    if b_open and not a_open:
        return "a"

    wa, wb = float(WEIGHTS["a"]), float(WEIGHTS["b"])
    if sticky_key:
        return _sticky_pick(sticky_key, wa, wb)

    total = max(0.0, wa) + max(0.0, wb)
    if total <= 0:
        return "a"
    r = random.random() * total
    return "a" if r < max(0.0, wa) else "b"

async def _health(b: Backend) -> bool:
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(_base_url(b) + "/health")
            return r.status_code == 200
    except Exception:
        return False

async def _send_shadow(method: str, url: str, params: dict, content: bytes, headers: dict, backend: Backend):
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
    return {"mode": MODE, "weights": WEIGHTS, "a": TARGET_A_URL, "b": TARGET_B_URL, "cb": _cb}

@app.post("/admin/mode")
def set_mode(payload: dict, x_admin_token: Optional[str] = Header(default=None)):
    global MODE
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="invalid admin token")
    m = payload.get("mode")
    if m not in ("weighted", "force_a", "force_b"):
        raise HTTPException(status_code=400, detail="mode must be weighted|force_a|force_b")
    MODE = m
    return {"mode": MODE}

@app.post("/admin/weights")
async def set_weights(payload: dict, x_admin_token: Optional[str] = Header(default=None)):
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="invalid admin token")

    wa = float(payload.get("a", WEIGHTS["a"]))
    wb = float(payload.get("b", WEIGHTS["b"]))
    if wa < 0 or wb < 0:
        raise HTTPException(status_code=400, detail="weights must be >= 0")

    if wa > 0 and not await _health("a"):
        raise HTTPException(status_code=502, detail="backend a unhealthy")
    if wb > 0 and not await _health("b"):
        raise HTTPException(status_code=502, detail="backend b unhealthy")

    WEIGHTS["a"], WEIGHTS["b"] = wa, wb
    return {"weights": WEIGHTS}

@app.get("/health")
def health():
    return {"ok": True}

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(path: str, request: Request):
    ROUTER_MODE.set(0 if MODE == "weighted" else (1 if MODE == "force_a" else 2))
    W_GAUGE.labels(backend="a").set(float(WEIGHTS["a"]))
    W_GAUGE.labels(backend="b").set(float(WEIGHTS["b"]))

    sticky_key = request.headers.get(STICKY_HEADER)
    primary = _choose_backend(sticky_key)
    secondary: Backend = "b" if primary == "a" else "a"

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
        LAT.labels(backend=primary, kind="primary").observe(time.time() - start)
        return Response(content=b"upstream error", status_code=502)
    finally:
        LAT.labels(backend=primary, kind="primary").observe(time.time() - start)

    if SHADOW_PERCENT > 0 and random.random() < SHADOW_PERCENT and not _cb_is_open(secondary):
        shadow_url = f"{_base_url(secondary)}/{path}"
        await _send_shadow(request.method, shadow_url, params, body, headers, backend=secondary)

    out_headers = {}
    ct = resp.headers.get("content-type")
    if ct:
        out_headers["content-type"] = ct
    return Response(content=resp.content, status_code=resp.status_code, headers=out_headers)

@app.get("/metrics")
def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
