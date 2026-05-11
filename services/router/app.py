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
