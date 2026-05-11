import os
import sqlite3
import sys
import threading
import time
import json
from dataclasses import dataclass
from typing import Dict, List, Literal, Optional, Tuple

import requests
from flask import Flask, jsonify, request as flask_request
from prometheus_client import Gauge, Counter, start_http_server

# Add shared config to path — tries Docker layout (/app/shared) then local dev (../shared)
_this_dir = os.path.dirname(os.path.abspath(__file__))
for _p in [os.path.join(_this_dir, "shared"), os.path.join(_this_dir, "..", "shared")]:
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)
        break
from config import BackendConfig, load_backends  # noqa: E402

Phase = Literal["steady", "ramping", "post_failover_hold", "draining"]

# --- Core config ---
PROM_URL = os.getenv("PROM_URL", "http://localhost:9090")
ROUTER_URL = os.getenv("ROUTER_URL", "http://localhost:8080")
ADMIN_TOKEN = os.getenv("ROUTER_ADMIN_TOKEN", "changeme")
PRICEFEED_URL = os.getenv("PRICEFEED_URL", "http://localhost:7070")

DECISION_INTERVAL = int(os.getenv("DECISION_INTERVAL_SECONDS", "15"))
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "60"))
WINDOW = os.getenv("WINDOW", "2m")

MAX_P95_MS = float(os.getenv("MAX_P95_MS", "200"))
MAX_ERROR_RATE = float(os.getenv("MAX_ERROR_RATE", "0.02"))
MIN_TOTAL_RPS = float(os.getenv("MIN_TOTAL_RPS", "1"))

MIN_SAVINGS_PER_HOUR = float(os.getenv("MIN_SAVINGS_PER_HOUR", "0.001"))
# Phase 0 fix: SWITCHING_PENALTY_USD is a one-time gate at ramp start, not a per-cycle tax
SWITCHING_PENALTY_USD = float(os.getenv("SWITCHING_PENALTY_USD", "0.0"))

RAMP_STEPS = [float(x.strip()) for x in os.getenv("RAMP_STEPS", "0.10,0.25,0.50,1.00").split(",")]
RAMP_STEP_MIN_SECONDS = int(os.getenv("RAMP_STEP_MIN_SECONDS", "45"))
REQUIRED_GOOD_WINDOWS = int(os.getenv("REQUIRED_GOOD_WINDOWS", "3"))
REQUIRED_BAD_WINDOWS = int(os.getenv("REQUIRED_BAD_WINDOWS", "2"))

EG_COST_MODE = os.getenv("EG_COST_MODE", "project_full_traffic")
DB_PATH = os.getenv("DB_PATH", "bot.db")
BOT_METRICS_PORT = int(os.getenv("BOT_METRICS_PORT", "9101"))

# --- Phase 1A: hysteresis after failover ---
POST_FAILOVER_HOLD_SECONDS = int(os.getenv("POST_FAILOVER_HOLD_SECONDS", "300"))
POST_FAILOVER_CONFIRM_WINDOWS_NEEDED = int(os.getenv("POST_FAILOVER_CONFIRM_WINDOWS", "5"))

# --- Phase 1B: graceful drain ---
DRAIN_STEPS_COUNT = int(os.getenv("DRAIN_STEPS", "4"))
DRAIN_STEP_SECONDS = int(os.getenv("DRAIN_STEP_SECONDS", "10"))

# --- Phase 1C: alert integration ---
ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "")
ALERT_ON_FAILOVER = os.getenv("ALERT_ON_FAILOVER", "true").lower() == "true"
ALERT_ON_ROLLBACK = os.getenv("ALERT_ON_ROLLBACK", "true").lower() == "true"
ALERT_ON_BAD_WINDOWS = os.getenv("ALERT_ON_BAD_WINDOWS", "true").lower() == "true"
_ALERT_COST_STR = os.getenv("ALERT_COST_THRESHOLD_USD_PER_HOUR", "")
ALERT_COST_THRESHOLD: Optional[float] = float(_ALERT_COST_STR) if _ALERT_COST_STR else None
ALERT_RETRY_COUNT = int(os.getenv("ALERT_RETRY_COUNT", "3"))
ALERT_RETRY_BACKOFF = float(os.getenv("ALERT_RETRY_BACKOFF_SECONDS", "2"))
ALERT_TIMEOUT = float(os.getenv("ALERT_TIMEOUT_SECONDS", "5"))
BOT_INSTANCE_NAME = os.getenv("BOT_INSTANCE_NAME", "multicloud-bot")

# --- Phase 1D: REST API ---
BOT_API_PORT = int(os.getenv("BOT_API_PORT", "9102"))
BOT_API_TOKEN = os.getenv("BOT_API_TOKEN", "")

# --- Load N-backend registry ---
BACKENDS: Dict[str, BackendConfig] = load_backends()
BNAMES: List[str] = sorted(BACKENDS.keys())  # stable sorted order

# --- Prometheus metrics ---
M_PHASE = Gauge("bot_phase", "Bot phase (0=steady,1=ramping,2=post_failover_hold,3=draining)")
M_GOOD = Gauge("bot_good_windows", "Consecutive good windows")
M_BAD = Gauge("bot_bad_windows", "Consecutive bad windows")
M_ACTIONS = Counter("bot_actions_total", "Bot actions", ["action"])
M_EST_COST = Gauge("bot_estimated_total_cost_usd_per_hour", "Estimated total $/hr", ["backend"])
M_EST_EGRESS = Gauge("bot_estimated_egress_cost_usd_per_hour", "Estimated egress $/hr", ["backend"])
# Phase 1A metrics
M_FAILOVER_HOLD = Gauge("bot_post_failover_hold_remaining_seconds", "Seconds remaining in post-failover hold")
M_FAILOVER_CONFIRM = Gauge("bot_post_failover_confirm_windows", "Consecutive good shadow windows after failover")
# Phase 3B: cost anomaly
M_COST_ANOMALY = Gauge("bot_cost_anomaly", "1 if any backend cost exceeds threshold, else 0")

# --- Phase 1D: shared state ---
_state_lock = threading.Lock()
_live_state: dict = {
    "ts": "", "phase": "steady", "active": BNAMES[0] if BNAMES else "a",
    "candidate": BNAMES[-1] if len(BNAMES) > 1 else "b",
    "step_index": 0, "weights": {n: (1.0 if i == 0 else 0.0) for i, n in enumerate(BNAMES)},
    "good_windows": 0, "bad_windows": 0, "last_change": 0.0,
    "last_action": "none", "last_reason": "n/a",
    "slo": {}, "cost_usd_per_hour": {}, "egress_usd_per_hour": {},
    "ramp_info": {}, "last_failover_ts": 0.0, "post_failover_confirm_windows": 0,
}
_force_override: Optional[str] = None

# --- Phase 1C: alert rate-limiting ---
_last_cost_alert_ts: float = 0.0


@dataclass
class Slo:
    p95_ms: Optional[float]
    err: Optional[float]
    cb_open: Optional[float]
    rps_primary: Optional[float]

    def ok(self) -> bool:
        if self.cb_open is not None and self.cb_open >= 1.0:
            return False
        return (
            self.p95_ms is not None and self.err is not None
            and self.p95_ms <= MAX_P95_MS
            and self.err <= MAX_ERROR_RATE
        )


# ---------------------------------------------------------------------------
# SQLite helpers with N-backend schema migration
# ---------------------------------------------------------------------------

def _db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""CREATE TABLE IF NOT EXISTS state(
        k TEXT PRIMARY KEY,
        v TEXT
    )""")

    # Phase 2D: detect old fixed-column schema and migrate to JSON-blob columns
    cols = {row[1] for row in con.execute("PRAGMA table_info(decisions)").fetchall()}
    if not cols:
        # Fresh DB — create with JSON-blob schema
        con.execute("""CREATE TABLE decisions(
            ts TEXT,
            phase TEXT,
            active TEXT,
            candidate TEXT,
            step_index INTEGER,
            weights_json TEXT,
            compute_json TEXT,
            egress_json TEXT,
            p95_json TEXT,
            err_json TEXT,
            cb_json TEXT,
            rps_json TEXT,
            bytesps_json TEXT,
            action TEXT,
            reason TEXT
        )""")
    elif "weights_a" in cols and "weights_json" not in cols:
        # Old fixed-column schema — rename and add JSON columns
        con.execute("ALTER TABLE decisions RENAME TO decisions_legacy")
        con.execute("""CREATE TABLE decisions(
            ts TEXT,
            phase TEXT,
            active TEXT,
            candidate TEXT,
            step_index INTEGER,
            weights_json TEXT,
            compute_json TEXT,
            egress_json TEXT,
            p95_json TEXT,
            err_json TEXT,
            cb_json TEXT,
            rps_json TEXT,
            bytesps_json TEXT,
            action TEXT,
            reason TEXT
        )""")
        # Migrate existing rows (converts fixed a/b columns to JSON blobs)
        old_rows = con.execute(
            "SELECT ts,phase,active,candidate,step_index,"
            "weights_a,weights_b,compute_a,compute_b,egress_a,egress_b,"
            "p95_a,p95_b,err_a,err_b,cb_a,cb_b,rps_a,rps_b,bytesps_a,bytesps_b,"
            "action,reason FROM decisions_legacy"
        ).fetchall()
        for row in old_rows:
            (ts, ph, act, cnd, si,
             wa, wb, ca, cb, ea, eb,
             p95a, p95b, erra, errb, cba, cbb, rpsa, rpsb, bpa, bpb,
             ac, rs) = row
            con.execute(
                "INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ts, ph, act, cnd, si,
                    json.dumps({"a": wa, "b": wb}),
                    json.dumps({"a": ca, "b": cb}),
                    json.dumps({"a": ea, "b": eb}),
                    json.dumps({"a": p95a, "b": p95b}),
                    json.dumps({"a": erra, "b": errb}),
                    json.dumps({"a": cba, "b": cbb}),
                    json.dumps({"a": rpsa, "b": rpsb}),
                    json.dumps({"a": bpa, "b": bpb}),
                    ac, rs,
                ),
            )
    con.commit()
    return con


def _get_state(con: sqlite3.Connection, k: str, default: str) -> str:
    row = con.execute("SELECT v FROM state WHERE k=?", (k,)).fetchone()
    return row[0] if row else default


def _set_state(con: sqlite3.Connection, k: str, v: str) -> None:
    con.execute("INSERT INTO state(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
    con.commit()


# ---------------------------------------------------------------------------
# Prometheus query helpers (backend-agnostic strings)
# ---------------------------------------------------------------------------

def prom_query(q: str) -> Optional[float]:
    r = requests.get(f"{PROM_URL}/api/v1/query", params={"query": q}, timeout=5)
    r.raise_for_status()
    res = r.json().get("data", {}).get("result", [])
    if not res:
        return None
    try:
        return float(res[0]["value"][1])
    except Exception:
        return None


def p95_ms(backend: str) -> Optional[float]:
    q = (
        f'histogram_quantile(0.95, '
        f'sum(rate(router_backend_latency_seconds_bucket{{backend="{backend}",kind="primary"}}[{WINDOW}])) by (le)'
        f')'
    )
    v = prom_query(q)
    return None if v is None else v * 1000.0


def err_rate(backend: str) -> Optional[float]:
    total_q = f'sum(rate(router_backend_requests_total{{backend="{backend}",kind="primary"}}[{WINDOW}]))'
    total = prom_query(total_q)
    if total is None or total == 0:
        return None  # no traffic — can't compute a rate
    err_q = f'sum(rate(router_backend_requests_total{{backend="{backend}",kind="primary",code_class="5xx"}}[{WINDOW}]))'
    err = prom_query(err_q)
    # absent 5xx series means zero errors, not unknown
    return 0.0 if err is None else err / total


def rps_primary(backend: str) -> Optional[float]:
    q = f'sum(rate(router_backend_requests_total{{backend="{backend}",kind="primary"}}[{WINDOW}]))'
    return prom_query(q)


def bytes_per_s_primary(backend: str) -> Optional[float]:
    q = f'sum(rate(router_backend_response_bytes_total{{backend="{backend}",kind="primary"}}[{WINDOW}]))'
    return prom_query(q)


def bytes_per_s_shadow(backend: str) -> Optional[float]:
    q = f'sum(rate(router_backend_response_bytes_total{{backend="{backend}",kind="shadow"}}[{WINDOW}]))'
    return prom_query(q)


def rps_shadow(backend: str) -> Optional[float]:
    q = f'sum(rate(router_backend_requests_total{{backend="{backend}",kind="shadow"}}[{WINDOW}]))'
    return prom_query(q)


def cb_open(backend: str) -> Optional[float]:
    q = f'max(router_circuit_open{{backend="{backend}"}})'
    return prom_query(q)


def get_slos() -> Dict[str, Slo]:
    return {b: Slo(p95_ms(b), err_rate(b), cb_open(b), rps_primary(b)) for b in BNAMES}


def get_prices() -> Tuple[Dict[str, float], Dict[str, float]]:
    r = requests.get(f"{PRICEFEED_URL}/price", timeout=5)
    r.raise_for_status()
    j = r.json()
    compute = {k: float(v) for k, v in j["compute_usd_per_hour"].items()}
    egress = {k: float(v) for k, v in j["egress_usd_per_gb"].items()}
    return compute, egress


def get_weights() -> Dict[str, float]:
    r = requests.get(f"{ROUTER_URL}/target", timeout=5)
    r.raise_for_status()
    return {k: float(v) for k, v in r.json()["weights"].items()}


def set_weights_dict(weights: Dict[str, float]) -> None:
    """POST a full weights dict to the router."""
    r = requests.post(
        f"{ROUTER_URL}/admin/weights",
        json=weights,
        headers={"X-Admin-Token": ADMIN_TOKEN},
        timeout=5,
    )
    r.raise_for_status()


def build_weights(full: str, partial: str, step: float) -> Dict[str, float]:
    """Build weights dict: `full` backend gets (1-step), `partial` gets step, others get 0."""
    w = {b: 0.0 for b in BNAMES}
    w[full] = 1.0 - step
    w[partial] = step
    return w


def primary_from_weights(w: Dict[str, float]) -> str:
    """Backend with the highest weight. Tie-break: alphabetically first backend."""
    if not w:
        return BNAMES[0]
    best = max(BNAMES, key=lambda b: w.get(b, 0.0))
    # Phase 0 fix: warn when weights are exactly tied
    top_weight = w.get(best, 0.0)
    tied = [b for b in BNAMES if abs(w.get(b, 0.0) - top_weight) < 1e-9]
    if len(tied) > 1:
        print(json.dumps({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "warning": "weights_tied",
            "tied_backends": sorted(tied),
            "message": f"weights are tied; picking '{best}' (alphabetically first) as active",
        }), flush=True)
    return best


def candidates_ranked_by_cost(
    active: str,
    total_cost: Dict[str, Optional[float]],
    slos: Dict[str, Slo],
) -> List[str]:
    """Return non-active backends sorted by total cost (cheapest first), healthy ones first."""
    others = [b for b in BNAMES if b != active]
    healthy = [b for b in others if slos[b].ok() and total_cost.get(b) is not None]
    unhealthy = [b for b in others if b not in healthy]
    healthy.sort(key=lambda b: total_cost[b])  # type: ignore[index]
    return healthy + unhealthy


def full_weights_on(backend: str) -> Dict[str, float]:
    """100% traffic to one backend, 0 to all others."""
    w = {b: 0.0 for b in BNAMES}
    w[backend] = 1.0
    return w


def _gb(x_bytes: float) -> float:
    return x_bytes / 1024.0 / 1024.0 / 1024.0


def estimate_full_traffic_bytes_per_s() -> float:
    return sum(bytes_per_s_primary(b) or 0.0 for b in BNAMES)


def estimate_bytes_per_req_for_backend(backend: str) -> Optional[float]:
    srps = rps_shadow(backend)
    sbytes = bytes_per_s_shadow(backend)
    if srps is not None and sbytes is not None and srps > 0.2:
        return sbytes / srps
    prps = rps_primary(backend)
    pbytes = bytes_per_s_primary(backend)
    if prps is not None and pbytes is not None and prps > 0:
        return pbytes / prps
    return None


def estimate_egress_cost_per_hour_if_100pct_on(backend: str, egress_usd_per_gb: Dict[str, float]) -> Optional[float]:
    total_bytes_s = estimate_full_traffic_bytes_per_s()
    bpr = estimate_bytes_per_req_for_backend(backend)
    total_rps = sum(rps_primary(b) or 0.0 for b in BNAMES)

    if EG_COST_MODE == "use_observed_split":
        bps = bytes_per_s_primary(backend)
        if bps is None:
            return None
        return _gb(bps * 3600.0) * egress_usd_per_gb[backend]

    if bpr is None:
        return _gb(total_bytes_s * 3600.0) * egress_usd_per_gb[backend]
    projected_bytes_s = total_rps * bpr
    return _gb(projected_bytes_s * 3600.0) * egress_usd_per_gb[backend]


# ---------------------------------------------------------------------------
# Phase 1C: alert integration
# ---------------------------------------------------------------------------

def _do_send_alert(payload: dict) -> None:
    for attempt in range(ALERT_RETRY_COUNT):
        try:
            r = requests.post(ALERT_WEBHOOK_URL, json=payload, timeout=ALERT_TIMEOUT)
            r.raise_for_status()
            return
        except Exception:
            if attempt < ALERT_RETRY_COUNT - 1:
                time.sleep(ALERT_RETRY_BACKOFF)
    print(json.dumps({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "warning": "alert_send_failed",
        "event": payload.get("event"),
    }), flush=True)


def send_alert(event: str, severity: str, payload_data: dict) -> None:
    if not ALERT_WEBHOOK_URL:
        return
    payload = {
        "event": event,
        "severity": severity,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "bot_instance": BOT_INSTANCE_NAME,
        "text": f"[{severity.upper()}] {BOT_INSTANCE_NAME}: {event}",
        "payload": payload_data,
    }
    threading.Thread(target=_do_send_alert, args=(payload,), daemon=True).start()


# ---------------------------------------------------------------------------
# Phase 1D: Flask REST API + Dashboard UI
# ---------------------------------------------------------------------------

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>MultiCloud Arbitrage Bot</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#070a12;--surface:#0e1120;--surface2:#161a2c;--surface3:#1e2338;
  --border:#252a42;--border2:#2f3555;
  --accent:#7c3aed;--accent2:#2563eb;
  --green:#22c55e;--green-dim:rgba(34,197,94,.12);
  --amber:#f59e0b;--amber-dim:rgba(245,158,11,.12);
  --red:#f43f5e;--red-dim:rgba(244,63,94,.12);
  --blue:#38bdf8;--blue-dim:rgba(56,189,248,.12);
  --text:#f1f5f9;--text-dim:#94a3b8;--text-muted:#475569;
  --grad:linear-gradient(135deg,#7c3aed,#2563eb);
  --radius:14px;--radius-sm:9px;--radius-xs:6px;
  --font:'Inter',-apple-system,BlinkMacSystemFont,sans-serif;
  --mono:'JetBrains Mono',monospace;
}
html{font-family:var(--font);background:var(--bg);color:var(--text);min-height:100vh}
body{padding:28px 32px;max-width:1520px;margin:0 auto}
a{color:var(--accent);text-decoration:none}

/* ── Header ── */
.header{display:flex;align-items:center;justify-content:space-between;margin-bottom:32px;gap:16px;flex-wrap:wrap}
.header-left{display:flex;align-items:center;gap:16px}
.logo{width:48px;height:48px;background:var(--grad);border-radius:13px;display:flex;align-items:center;justify-content:center;font-size:24px;flex-shrink:0;box-shadow:0 0 28px rgba(124,58,237,.35)}
.header-title h1{font-size:1.35rem;font-weight:800;letter-spacing:-.4px;background:var(--grad);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.header-title p{font-size:.775rem;color:var(--text-muted);margin-top:3px;font-weight:500}
.header-right{display:flex;align-items:center;gap:12px}
.live-pill{display:flex;align-items:center;gap:8px;background:var(--surface);border:1px solid var(--border);border-radius:999px;padding:7px 16px;font-size:.775rem;color:var(--text-dim);font-weight:500}
.live-dot{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 8px var(--green);flex-shrink:0}
.live-dot.err{background:var(--red);box-shadow:0 0 8px var(--red)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.live-dot{animation:pulse 2s infinite}

/* ── Stat strip ── */
.stat-strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px;margin-bottom:22px}
.stat-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:20px 22px;position:relative;overflow:hidden;transition:border-color .25s}
.stat-card::after{content:'';position:absolute;inset:0;background:var(--grad);opacity:0;transition:opacity .25s;pointer-events:none;border-radius:var(--radius)}
.stat-card:hover{border-color:var(--border2)}
.stat-icon{font-size:1.2rem;margin-bottom:10px;opacity:.7}
.stat-label{font-size:.68rem;text-transform:uppercase;letter-spacing:.7px;color:var(--text-muted);font-weight:600;margin-bottom:7px}
.stat-value{font-size:1.65rem;font-weight:800;letter-spacing:-.5px;color:var(--text);line-height:1}
.stat-value.green{color:var(--green)}
.stat-value.amber{color:var(--amber)}
.stat-value.red{color:var(--red)}
.stat-value.accent{background:var(--grad);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.stat-sub{font-size:.72rem;color:var(--text-muted);margin-top:6px;font-weight:500}

/* ── Backend cards ── */
.backends-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:14px;margin-bottom:22px}
.backend-card{background:var(--surface);border:1.5px solid var(--border);border-radius:var(--radius);padding:22px;position:relative;overflow:hidden;transition:border-color .3s,box-shadow .3s}
.backend-card .top-bar{position:absolute;top:0;left:0;right:0;height:3px;border-radius:var(--radius) var(--radius) 0 0;opacity:0;transition:opacity .3s}
.backend-card.is-active{border-color:var(--green);box-shadow:0 0 20px rgba(34,197,94,.1)}
.backend-card.is-active .top-bar{background:linear-gradient(90deg,var(--green),#4ade80);opacity:1}
.backend-card.is-degraded{border-color:var(--red);box-shadow:0 0 20px rgba(244,63,94,.1)}
.backend-card.is-degraded .top-bar{background:var(--red);opacity:1}
.backend-card.is-candidate{border-color:var(--accent);box-shadow:0 0 20px rgba(124,58,237,.1)}
.backend-card.is-candidate .top-bar{background:var(--grad);opacity:1}
.bc-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:18px}
.bc-name{font-size:1.05rem;font-weight:800;text-transform:uppercase;letter-spacing:1.2px}
.badge{font-size:.65rem;font-weight:700;text-transform:uppercase;letter-spacing:.6px;padding:4px 11px;border-radius:999px}
.badge-active{background:var(--green-dim);color:var(--green);border:1px solid rgba(34,197,94,.3)}
.badge-candidate{background:rgba(124,58,237,.15);color:#a78bfa;border:1px solid rgba(124,58,237,.3)}
.badge-standby{background:var(--surface2);color:var(--text-muted);border:1px solid var(--border)}
.badge-degraded{background:var(--red-dim);color:var(--red);border:1px solid rgba(244,63,94,.3)}
.badge-cb{background:var(--red-dim);color:var(--red);border:1px solid rgba(244,63,94,.3)}
.metric-list{display:flex;flex-direction:column;gap:0}
.mrow{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid var(--border)}
.mrow:last-of-type{border-bottom:none}
.mkey{font-size:.775rem;color:var(--text-dim);font-weight:500}
.mval{font-size:.82rem;font-weight:700;font-family:var(--mono);color:var(--text)}
.mval.ok{color:var(--green)}
.mval.warn{color:var(--amber)}
.mval.bad{color:var(--red)}
.wbar-wrap{margin-top:14px}
.wbar-head{display:flex;justify-content:space-between;font-size:.72rem;color:var(--text-muted);margin-bottom:6px;font-weight:500}
.wbar-track{background:var(--surface2);border-radius:999px;height:7px;overflow:hidden;border:1px solid var(--border)}
.wbar-fill{height:100%;border-radius:999px;background:var(--grad);transition:width .7s cubic-bezier(.4,0,.2,1)}
.wbar-fill.is-active{background:linear-gradient(90deg,var(--green),#4ade80)}

/* ── Two-col charts ── */
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:22px}
@media(max-width:860px){.two-col{grid-template-columns:1fr}}
.chart-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:22px}
.section-title{font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.7px;color:var(--text-muted);margin-bottom:18px;display:flex;align-items:center;gap:8px}
.section-title::before{content:'';display:block;width:3px;height:14px;background:var(--grad);border-radius:99px}
.chart-wrap{position:relative;height:230px}

/* ── Controls ── */
.controls-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:22px;margin-bottom:22px}
.controls-body{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
.btn{display:inline-flex;align-items:center;gap:7px;padding:10px 20px;border-radius:var(--radius-sm);border:1.5px solid var(--border);font-size:.8rem;font-weight:600;cursor:pointer;transition:all .2s;letter-spacing:.1px;font-family:var(--font)}
.btn:hover{transform:translateY(-1px)}
.btn:active{transform:translateY(0) scale(.98)}
.btn svg{width:14px;height:14px;flex-shrink:0}
.btn-accent{background:var(--grad);border-color:transparent;color:#fff;box-shadow:0 4px 14px rgba(124,58,237,.3)}
.btn-accent:hover{box-shadow:0 6px 20px rgba(124,58,237,.45)}
.btn-danger{background:var(--red-dim);border-color:rgba(244,63,94,.4);color:var(--red)}
.btn-danger:hover{background:var(--red);border-color:var(--red);color:#fff}
.btn-warn{background:var(--amber-dim);border-color:rgba(245,158,11,.4);color:var(--amber)}
.btn-warn:hover{background:var(--amber);border-color:var(--amber);color:#fff}
.btn-ghost{background:transparent;border-color:var(--border);color:var(--text-dim)}
.btn-ghost:hover{border-color:var(--border2);color:var(--text)}
.divider{width:1px;height:32px;background:var(--border);flex-shrink:0}
.toast{display:none;align-items:center;gap:8px;font-size:.775rem;font-weight:500;padding:8px 14px;border-radius:var(--radius-xs)}
.toast.ok{background:var(--green-dim);color:var(--green);border:1px solid rgba(34,197,94,.3)}
.toast.err{background:var(--red-dim);color:var(--red);border:1px solid rgba(244,63,94,.3)}

/* ── History ── */
.history-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;margin-bottom:28px}
.history-head{display:flex;align-items:center;justify-content:space-between;padding:18px 22px;border-bottom:1px solid var(--border)}
.tbl-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:.78rem}
thead th{padding:10px 16px;text-align:left;font-size:.65rem;text-transform:uppercase;letter-spacing:.7px;color:var(--text-muted);background:var(--surface2);border-bottom:1px solid var(--border);font-weight:700;white-space:nowrap}
tbody tr{border-bottom:1px solid var(--border);transition:background .15s}
tbody tr:last-child{border-bottom:none}
tbody tr:hover{background:var(--surface2)}
tbody td{padding:10px 16px;color:var(--text-dim);white-space:nowrap;font-size:.78rem}
.a-none{color:var(--text-muted);font-style:italic}
.a-failover{color:var(--red);font-weight:700}
.a-ramp{color:#a78bfa;font-weight:700}
.a-promote{color:var(--green);font-weight:700}
.a-drain{color:var(--amber);font-weight:700}
.a-hold{color:var(--text-muted);font-style:italic}

/* ── Footer ── */
.footer{display:flex;justify-content:space-between;align-items:center;padding-top:18px;border-top:1px solid var(--border);font-size:.72rem;color:var(--text-muted);flex-wrap:wrap;gap:8px}
.footer a{color:var(--text-muted);transition:color .2s}
.footer a:hover{color:var(--text-dim)}

/* ── Phase banner ── */
.phase-banner{display:none;align-items:center;gap:12px;padding:12px 18px;border-radius:var(--radius-sm);margin-bottom:22px;font-size:.82rem;font-weight:600}
.phase-banner.ramping{background:rgba(124,58,237,.12);border:1px solid rgba(124,58,237,.3);color:#a78bfa;display:flex}
.phase-banner.post_failover_hold{background:var(--amber-dim);border:1px solid rgba(245,158,11,.3);color:var(--amber);display:flex}
.phase-banner.draining{background:var(--amber-dim);border:1px solid rgba(245,158,11,.3);color:var(--amber);display:flex}
.banner-dot{width:8px;height:8px;border-radius:50%;background:currentColor;flex-shrink:0}
@keyframes spin{to{transform:rotate(360deg)}}
.spinner{width:14px;height:14px;border:2px solid currentColor;border-right-color:transparent;border-radius:50%;animation:spin .8s linear infinite;flex-shrink:0}
</style>
</head>
<body>

<div class="header">
  <div class="header-left">
    <div class="logo">&#9889;</div>
    <div class="header-title">
      <h1>MultiCloud Arbitrage Bot</h1>
      <p>Real-time cost-optimized traffic routing &mdash; Master&rsquo;s Research Project</p>
    </div>
  </div>
  <div class="header-right">
    <div class="live-pill">
      <div class="live-dot" id="liveDot"></div>
      <span id="liveLabel">Connecting&hellip;</span>
    </div>
  </div>
</div>

<div class="phase-banner" id="phaseBanner">
  <div class="spinner"></div>
  <span id="phaseBannerText"></span>
</div>

<div class="stat-strip">
  <div class="stat-card">
    <div class="stat-label">System Phase</div>
    <div class="stat-value accent" id="sPhase">&mdash;</div>
    <div class="stat-sub" id="sLastAction">&mdash;</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Active Backend</div>
    <div class="stat-value green" id="sActive">&mdash;</div>
    <div class="stat-sub" id="sCandidate">Candidate: &mdash;</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Projected Saving</div>
    <div class="stat-value" id="sSaving">&mdash;</div>
    <div class="stat-sub">active vs cheapest alt</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">SLO Windows</div>
    <div class="stat-value green" id="sGood">&mdash;</div>
    <div class="stat-sub" id="sBad">Bad: &mdash;</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Last Decision</div>
    <div class="stat-value" style="font-size:1rem;letter-spacing:-.3px" id="sTs">&mdash;</div>
    <div class="stat-sub" id="sReason">&mdash;</div>
  </div>
</div>

<div class="backends-grid" id="backendsGrid"></div>

<div class="two-col">
  <div class="chart-card">
    <div class="section-title">Traffic Weight Distribution</div>
    <div class="chart-wrap"><canvas id="wChart"></canvas></div>
  </div>
  <div class="chart-card">
    <div class="section-title">Projected Cost per Hour</div>
    <div class="chart-wrap"><canvas id="cChart"></canvas></div>
  </div>
</div>

<div class="controls-card">
  <div class="section-title" style="margin-bottom:14px">Manual Controls</div>
  <div class="controls-body">
    <div id="failoverBtns"></div>
    <div class="divider"></div>
    <button class="btn btn-warn" onclick="forceAction('force_rollback')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 .49-4.5"/></svg>
      Rollback
    </button>
    <button class="btn btn-ghost" onclick="forceAction('force_hold')">
      <svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/></svg>
      Hold
    </button>
    <div class="toast" id="forceToast"></div>
  </div>
</div>

<div class="history-card">
  <div class="history-head">
    <div class="section-title" style="margin-bottom:0">Decision History</div>
    <span style="font-size:.75rem;color:var(--text-muted)" id="histCount">&mdash;</span>
  </div>
  <div class="tbl-wrap">
    <table>
      <thead><tr>
        <th>Timestamp</th><th>Phase</th><th>Active</th><th>Candidate</th>
        <th>Action</th><th>Reason</th><th>P95 (ms)</th><th>Err %</th><th>Weights</th>
      </tr></thead>
      <tbody id="histBody"></tbody>
    </table>
  </div>
</div>

<div class="footer">
  <span>Decision interval: <strong id="fInterval">&mdash;</strong>s &nbsp;&middot;&nbsp; Cooldown: <strong id="fCooldown">&mdash;</strong>s &nbsp;&middot;&nbsp; Window: <strong id="fWindow">&mdash;</strong></span>
  <span id="fTs">Last refresh: &mdash;</span>
</div>

<script>
Chart.defaults.color='#475569';
Chart.defaults.borderColor='#252a42';
Chart.defaults.font.family="'Inter',sans-serif";
Chart.defaults.font.size=12;

const PALETTE_BG=['rgba(124,58,237,.85)','rgba(37,99,235,.85)','rgba(34,197,94,.85)','rgba(245,158,11,.85)','rgba(244,63,94,.85)','rgba(56,189,248,.85)'];
const PALETTE=['#7c3aed','#2563eb','#22c55e','#f59e0b','#f43f5e','#38bdf8'];

let wChart,cChart,knownBackends=[];

function initCharts(backends){
  if(wChart)wChart.destroy();
  if(cChart)cChart.destroy();
  const bgs=PALETTE_BG.slice(0,backends.length);
  wChart=new Chart(document.getElementById('wChart'),{
    type:'doughnut',
    data:{labels:backends.map(b=>b.toUpperCase()),datasets:[{data:backends.map((_,i)=>i===0?1:0),backgroundColor:bgs,borderWidth:2,borderColor:'#0e1120',hoverOffset:6}]},
    options:{responsive:true,maintainAspectRatio:false,cutout:'65%',
      plugins:{legend:{position:'bottom',labels:{padding:16,boxWidth:13,font:{size:11,weight:'600'}}},
        tooltip:{callbacks:{label:ctx=>' '+ctx.label+': '+(ctx.parsed*100).toFixed(1)+'%'}}},
      animation:{duration:600,easing:'easeInOutQuart'}}
  });
  cChart=new Chart(document.getElementById('cChart'),{
    type:'bar',
    data:{labels:backends.map(b=>b.toUpperCase()),datasets:[{label:'Cost ($/hr)',data:backends.map(()=>0),backgroundColor:bgs,borderRadius:7,borderSkipped:false}]},
    options:{responsive:true,maintainAspectRatio:false,
      plugins:{legend:{display:false},tooltip:{callbacks:{label:ctx=>' $'+ctx.parsed.toFixed(6)+'/hr'}}},
      scales:{x:{grid:{display:false},ticks:{font:{weight:'600'}}},
        y:{grid:{color:'#252a42'},ticks:{callback:v=>'$'+parseFloat(v).toFixed(4)}}},
      animation:{duration:600}}
  });
}

function updateCharts(s){
  const bk=Object.keys(s.weights||{});
  if(!bk.length)return;
  if(knownBackends.join()!==bk.join()){knownBackends=bk;initCharts(bk);renderFailoverBtns(bk)}
  wChart.data.datasets[0].data=bk.map(b=>s.weights[b]||0);
  wChart.update('none');
  const costs=s.cost_usd_per_hour||{};
  cChart.data.datasets[0].data=bk.map(b=>costs[b]||0);
  cChart.update('none');
}

function renderFailoverBtns(bk){
  document.getElementById('failoverBtns').innerHTML=bk.map((b,i)=>
    `<button class="btn btn-accent" style="background:${PALETTE[i%PALETTE.length]};border-color:transparent;box-shadow:0 4px 14px ${PALETTE[i%PALETTE.length]}55" onclick="forceAction('force_failover_to_${b}')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg>
      Failover &rarr; ${b.toUpperCase()}
    </button>`
  ).join(' ');
}

function renderBackends(s){
  const bk=Object.keys(s.weights||{});
  if(!bk.length)return;
  document.getElementById('backendsGrid').innerHTML=bk.map((b,i)=>{
    const w=(s.weights[b]||0),pct=(w*100).toFixed(0);
    const slo=(s.slo||{})[b]||{};
    const cost=(s.cost_usd_per_hour||{})[b];
    const isActive=b===s.active,isCand=b===s.candidate&&b!==s.active;
    const cbOpen=slo.cb_open>=1;
    const p95ok=slo.p95_ms!=null&&slo.p95_ms<=200;
    const errok=slo.err!=null&&slo.err<=0.02;
    const degraded=slo.p95_ms!=null&&(!p95ok||!errok||cbOpen);
    const cardCls=isActive?'is-active':degraded?'is-degraded':isCand?'is-candidate':'';
    const badge=cbOpen?'<span class="badge badge-cb">&#9889; CB Open</span>':
      isActive?'<span class="badge badge-active">&#9679; Active</span>':
      isCand?'<span class="badge badge-candidate">&#9675; Candidate</span>':
      '<span class="badge badge-standby">&#9675; Standby</span>';
    const rps=slo.rps!=null?slo.rps.toFixed(1)+'&nbsp;rps':'&mdash;';
    return `<div class="backend-card ${cardCls}">
  <div class="top-bar"></div>
  <div class="bc-head">
    <span class="bc-name" style="color:${PALETTE[i%PALETTE.length]}">Backend ${b.toUpperCase()}</span>${badge}
  </div>
  <div class="metric-list">
    <div class="mrow"><span class="mkey">P95 Latency</span><span class="mval ${slo.p95_ms==null?'':p95ok?'ok':'bad'}">${slo.p95_ms!=null?slo.p95_ms.toFixed(0)+' ms':'&mdash;'}</span></div>
    <div class="mrow"><span class="mkey">Error Rate</span><span class="mval ${slo.err==null?'':errok?'ok':'bad'}">${slo.err!=null?(slo.err*100).toFixed(2)+'%':'&mdash;'}</span></div>
    <div class="mrow"><span class="mkey">Circuit Breaker</span><span class="mval ${cbOpen?'bad':'ok'}">${cbOpen?'OPEN':'Closed'}</span></div>
    <div class="mrow"><span class="mkey">Throughput</span><span class="mval">${rps}</span></div>
    <div class="mrow"><span class="mkey">Projected Cost</span><span class="mval">${cost!=null?'$'+cost.toFixed(6)+'/hr':'&mdash;'}</span></div>
  </div>
  <div class="wbar-wrap">
    <div class="wbar-head"><span>Traffic Weight</span><span>${pct}%</span></div>
    <div class="wbar-track"><div class="wbar-fill ${isActive?'is-active':''}" style="width:${pct}%"></div></div>
  </div>
</div>`;
  }).join('');
}

function updateStats(s){
  const phaseMap={'steady':'STEADY','ramping':'RAMPING','post_failover_hold':'HOLD','draining':'DRAINING'};
  const phaseColorMap={'steady':'accent','ramping':'accent','post_failover_hold':'amber','draining':'amber'};
  const p=s.phase||'';
  document.getElementById('sPhase').textContent=phaseMap[p]||p.toUpperCase().replace(/_/g,' ');
  document.getElementById('sPhase').className='stat-value '+(phaseColorMap[p]||'');
  document.getElementById('sLastAction').textContent=s.last_action||'&mdash;';
  document.getElementById('sActive').textContent=(s.active||'&mdash;').toUpperCase();
  document.getElementById('sCandidate').textContent='Candidate: '+((s.candidate||'&mdash;').toUpperCase());
  document.getElementById('sGood').textContent=s.good_windows??'&mdash;';
  document.getElementById('sBad').textContent='Bad windows: '+(s.bad_windows??'&mdash;');
  document.getElementById('sTs').textContent=s.ts?(s.ts.replace('T',' ').replace('Z','')):'&mdash;';
  document.getElementById('sReason').textContent=s.last_reason||'&mdash;';
  if(s.config){
    document.getElementById('fInterval').textContent=s.config.decision_interval_seconds;
    document.getElementById('fCooldown').textContent=s.config.cooldown_seconds;
    document.getElementById('fWindow').textContent=s.config.window;
  }
  const costs=s.cost_usd_per_hour||{};
  const ac=s.active,cn=s.candidate;
  if(ac&&cn&&costs[ac]!=null&&costs[cn]!=null){
    const diff=costs[ac]-costs[cn];
    const el=document.getElementById('sSaving');
    if(diff>0){el.className='stat-value green';el.textContent='+$'+diff.toFixed(6)+'/hr';}
    else if(diff<0){el.className='stat-value red';el.textContent='-$'+Math.abs(diff).toFixed(6)+'/hr';}
    else{el.className='stat-value';el.textContent='$0.000000/hr';}
  } else{document.getElementById('sSaving').className='stat-value';document.getElementById('sSaving').textContent='&mdash;';}
  const banner=document.getElementById('phaseBanner');
  const bannerMsg={'ramping':'Canary ramp in progress — gradually shifting traffic to candidate backend','post_failover_hold':'Post-failover hold active — awaiting confirmation windows before re-routing','draining':'Graceful drain in progress — stepping down weights before rollback'};
  if(bannerMsg[p]){banner.className='phase-banner '+p;document.getElementById('phaseBannerText').textContent=bannerMsg[p];}
  else{banner.className='phase-banner';}
}

function actionCls(a){
  if(!a||a==='none')return 'a-none';
  if(a.includes('failover'))return 'a-failover';
  if(a.includes('promoted'))return 'a-promote';
  if(a.includes('ramp'))return 'a-ramp';
  if(a.includes('drain')||a.includes('rollback'))return 'a-drain';
  if(a.includes('hold')||a.includes('released')||a.includes('steady'))return 'a-hold';
  return '';
}

async function fetchHistory(){
  try{
    const r=await fetch('/history?limit=30');const d=await r.json();
    const rows=d.rows||[];
    document.getElementById('histCount').textContent=rows.length+' entries';
    document.getElementById('histBody').innerHTML=rows.map(row=>{
      const w=row.weights||{};
      const wStr=Object.entries(w).map(([k,v])=>`${k.toUpperCase()}:${(v*100).toFixed(0)}%`).join(' ');
      const p95=row.p95_ms||{};const err=row.err_rate||{};
      const bk=Object.keys(w);
      const p95str=bk.map(b=>p95[b]!=null?p95[b].toFixed(0):'&mdash;').join(' / ');
      const errstr=bk.map(b=>err[b]!=null?(err[b]*100).toFixed(2)+'%':'&mdash;').join(' / ');
      const cls=actionCls(row.action);
      return `<tr>
<td style="font-family:var(--mono);font-size:.73rem;color:var(--text-muted)">${(row.ts||'').replace('T',' ').replace('Z','')}</td>
<td>${(row.phase||'').replace(/_/g,' ')}</td>
<td style="font-weight:700;color:var(--text)">${(row.active||'').toUpperCase()}</td>
<td>${(row.candidate||'').toUpperCase()}</td>
<td class="${cls}">${row.action||'&mdash;'}</td>
<td style="color:var(--text-dim)">${row.reason||'&mdash;'}</td>
<td style="font-family:var(--mono);font-size:.73rem">${p95str}</td>
<td style="font-family:var(--mono);font-size:.73rem">${errstr}</td>
<td style="font-family:var(--mono);font-size:.72rem;color:var(--text-muted)">${wStr}</td>
</tr>`;
    }).join('');
  }catch(e){}
}

async function fetchStatus(){
  try{
    const r=await fetch('/status');const s=await r.json();
    updateStats(s);renderBackends(s);updateCharts(s);
    document.getElementById('liveDot').className='live-dot';
    document.getElementById('liveLabel').textContent='Live';
    document.getElementById('fTs').textContent='Last refresh: '+new Date().toLocaleTimeString();
  }catch(e){
    document.getElementById('liveDot').className='live-dot err';
    document.getElementById('liveLabel').textContent='Disconnected';
  }
}

async function forceAction(action){
  const toast=document.getElementById('forceToast');
  try{
    const resp=await fetch('/force',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action})});
    const data=await resp.json();
    toast.className='toast '+(resp.ok?'ok':'err');
    toast.textContent=resp.ok?('\\u2713 Queued: '+action.replace(/_/g,' ')):(data.detail||data.status||'Error');
    toast.style.display='inline-flex';
    if(resp.ok)setTimeout(fetchStatus,700);
  }catch(e){toast.className='toast err';toast.textContent='Request failed';toast.style.display='inline-flex';}
  setTimeout(()=>{toast.style.display='none';},4500);
}

fetchStatus();fetchHistory();
setInterval(fetchStatus,3000);setInterval(fetchHistory,12000);
</script>
</body></html>"""

_flask_app = Flask("bot-api")


@_flask_app.get("/")
def dashboard():
    from flask import make_response
    resp = make_response(_DASHBOARD_HTML)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    return resp


@_flask_app.get("/status")
def api_status():
    with _state_lock:
        snap = dict(_live_state)
        snap["pending_force"] = _force_override
    now = time.time()
    if snap.get("last_change"):
        snap["last_change_ago_seconds"] = round(now - snap["last_change"], 1)
    if snap.get("phase") == "post_failover_hold" and snap.get("last_failover_ts"):
        remaining = max(0.0, snap["last_failover_ts"] + POST_FAILOVER_HOLD_SECONDS - now)
        snap["post_failover_hold_remaining_seconds"] = round(remaining, 1)
    snap["config"] = {
        "decision_interval_seconds": DECISION_INTERVAL,
        "cooldown_seconds": COOLDOWN_SECONDS,
        "window": WINDOW,
        "max_p95_ms": MAX_P95_MS,
        "max_error_rate": MAX_ERROR_RATE,
        "min_savings_per_hour": MIN_SAVINGS_PER_HOUR,
        "ramp_steps": RAMP_STEPS,
        "backends": BNAMES,
    }
    return jsonify(snap)


@_flask_app.get("/history")
def api_history():
    limit = min(int(flask_request.args.get("limit", 50)), 500)
    con2 = sqlite3.connect(DB_PATH, check_same_thread=False)
    rows = con2.execute(
        "SELECT ts,phase,active,candidate,step_index,"
        "weights_json,compute_json,egress_json,p95_json,err_json,"
        "cb_json,rps_json,bytesps_json,action,reason "
        "FROM decisions ORDER BY ts DESC LIMIT ?",
        (limit,),
    ).fetchall()
    con2.close()
    cols = [
        "ts", "phase", "active", "candidate", "step_index",
        "weights", "compute", "egress", "p95_ms", "err_rate",
        "cb_open", "rps", "bytes_per_s", "action", "reason",
    ]
    def _parse(row):
        d = {}
        for i, col in enumerate(cols):
            v = row[i]
            # JSON blob columns (indices 5-12)
            if 5 <= i <= 12:
                try:
                    v = json.loads(v) if v else {}
                except Exception:
                    pass
            d[col] = v
        return d
    return jsonify({"rows": [_parse(r) for r in rows], "count": len(rows), "limit": limit})


@_flask_app.post("/force")
def api_force():
    global _force_override
    valid_actions = {"force_failover_to_" + b for b in BNAMES} | {"force_rollback", "force_hold"}
    if BOT_API_TOKEN:
        auth = flask_request.headers.get("Authorization", "")
        if auth != f"Bearer {BOT_API_TOKEN}":
            return jsonify({"status": "error", "detail": "unauthorized"}), 401
    data = flask_request.get_json(force=True, silent=True) or {}
    action = data.get("action", "")
    if action not in valid_actions:
        return jsonify({"status": "error", "detail": f"unknown action; valid: {sorted(valid_actions)}"}), 400
    with _state_lock:
        if _force_override is not None:
            return jsonify({"status": "conflict", "pending": _force_override}), 409
        _force_override = action
    return jsonify({"status": "queued", "action": action, "note": "consumed on next decision tick"}), 202


def _run_flask() -> None:
    _flask_app.run(host="0.0.0.0", port=BOT_API_PORT, threaded=True, use_reloader=False)


# ---------------------------------------------------------------------------
# Main decision loop
# ---------------------------------------------------------------------------

def main():
    global _force_override, _last_cost_alert_ts

    start_http_server(BOT_METRICS_PORT)
    threading.Thread(target=_run_flask, daemon=True).start()

    con = _db()

    # Load persisted state
    phase: str = _get_state(con, "phase", "steady")
    step_index = int(_get_state(con, "step_index", "0"))
    ramp_candidate: str = _get_state(con, "ramp_candidate", BNAMES[-1] if len(BNAMES) > 1 else BNAMES[0])
    step_since = float(_get_state(con, "step_since", str(time.time())))
    last_change = float(_get_state(con, "last_change", "0"))
    good_windows = int(_get_state(con, "good_windows", "0"))
    bad_windows = int(_get_state(con, "bad_windows", "0"))

    # Phase 1A state
    last_failover_ts = float(_get_state(con, "last_failover_ts", "0"))
    post_failover_confirm_windows = int(_get_state(con, "post_failover_confirm_windows", "0"))
    failover_from: str = _get_state(con, "failover_from", BNAMES[0])

    # Phase 1B state
    drain_target: str = _get_state(con, "drain_target", BNAMES[0])
    drain_loser: str = _get_state(con, "drain_loser", BNAMES[-1] if len(BNAMES) > 1 else BNAMES[0])
    drain_current_step = int(_get_state(con, "drain_current_step", "0"))
    drain_step_since = float(_get_state(con, "drain_step_since", "0"))
    drain_initial_weight_loser = float(_get_state(con, "drain_initial_weight_loser", "0"))

    # Bug fix: validate restored backend names against current BNAMES; stale values after
    # backends.json changes would cause KeyError crashes on the next tick.
    if ramp_candidate not in BNAMES:
        ramp_candidate = BNAMES[-1] if len(BNAMES) > 1 else BNAMES[0]
        if phase == "ramping":
            phase = "steady"
    if drain_target not in BNAMES:
        drain_target = BNAMES[0]
        if phase == "draining":
            phase = "steady"
    if drain_loser not in BNAMES:
        drain_loser = BNAMES[-1] if len(BNAMES) > 1 else BNAMES[0]
    if failover_from not in BNAMES:
        failover_from = BNAMES[0]

    startup_cb_checked = False

    while True:
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        action, reason = "none", "n/a"

        # Defaults so audit log always has values even on early exception
        active: str = BNAMES[0]
        cand: str = BNAMES[-1] if len(BNAMES) > 1 else BNAMES[0]
        w: Dict[str, float] = {b: (1.0 if i == 0 else 0.0) for i, b in enumerate(BNAMES)}
        slos: Dict[str, Slo] = {}
        compute: Dict[str, float] = {}
        egress: Dict[str, float] = {}
        total_cost: Dict[str, Optional[float]] = {}
        eg_costs: Dict[str, Optional[float]] = {}

        try:
            # Phase 1D: consume pending force override
            with _state_lock:
                force_cmd = _force_override
                _force_override = None

            w = get_weights()
            active = primary_from_weights(w)

            slos = get_slos()
            compute, egress = get_prices()

            # Phase 0: warn at startup if circuit breaker is already open
            if not startup_cb_checked:
                startup_cb_checked = True
                for b in BNAMES:
                    if slos[b].cb_open is not None and slos[b].cb_open >= 1.0:
                        print(json.dumps({
                            "ts": ts, "warning": "stale_cb_state", "backend": b,
                            "message": "circuit breaker open at startup; state may be stale after router restart",
                        }), flush=True)

            # Egress + total cost per backend (N backends)
            for b in BNAMES:
                eg = estimate_egress_cost_per_hour_if_100pct_on(b, egress)
                eg_costs[b] = eg
                if eg is not None:
                    M_EST_EGRESS.labels(backend=b).set(eg)
                total_cost[b] = (compute[b] + eg) if eg is not None else None
                if total_cost[b] is not None:
                    M_EST_COST.labels(backend=b).set(total_cost[b])

            # Phase 1C: cost anomaly alert (rate-limited)
            anomaly_detected = False
            if ALERT_COST_THRESHOLD and total_cost.get(active) is not None:
                if total_cost[active] > ALERT_COST_THRESHOLD:  # type: ignore[operator]
                    anomaly_detected = True
                    if time.time() - _last_cost_alert_ts > 3 * DECISION_INTERVAL:
                        _last_cost_alert_ts = time.time()
                        send_alert("cost_anomaly", "warning", {
                            "backend": active,
                            "projected_cost_usd_per_hour": total_cost[active],
                            "threshold_usd_per_hour": ALERT_COST_THRESHOLD,
                        })
            M_COST_ANOMALY.set(1 if anomaly_detected else 0)

            total_rps = sum(slos[b].rps_primary or 0.0 for b in BNAMES)

            # Phase 2D: ranked candidates list (N backends)
            ranked_cands = candidates_ranked_by_cost(active, total_cost, slos)
            # best_cand is the cheapest healthy non-active backend (requires known cost)
            best_cand = ranked_cands[0] if ranked_cands else (BNAMES[-1] if len(BNAMES) > 1 else BNAMES[0])
            cand = best_cand
            # For hard failover: pick any healthy non-active backend even if cost is unknown
            best_failover_cand = next(
                (b for b in BNAMES if b != active and slos.get(b, Slo(None, None, None, None)).ok()),
                None,
            )

            # Phase 0 fix: base savings without penalty (for monitoring); penalty applied only at ramp-start
            if total_cost.get(active) is not None and total_cost.get(cand) is not None:
                base_savings: Optional[float] = float(total_cost[active]) - float(total_cost[cand])  # type: ignore[arg-type]
            else:
                base_savings = None

            # --- Phase 1D: force overrides ---
            if force_cmd:
                cooldown_ok = (time.time() - last_change) >= COOLDOWN_SECONDS
                if force_cmd.startswith("force_failover_to_"):
                    target = force_cmd[len("force_failover_to_"):]
                    if target in BNAMES:
                        set_weights_dict(full_weights_on(target))
                        phase, step_index = "steady", 0
                        good_windows, bad_windows = 0, 0
                        last_change = time.time()
                        action, reason = force_cmd, "manual_override"
                elif force_cmd == "force_rollback":
                    if phase == "ramping":
                        drain_loser = ramp_candidate
                        drain_target = active
                        drain_initial_weight_loser = float(w.get(drain_loser, 0.0))
                        drain_current_step = 0
                        drain_step_since = time.time()
                        phase = "draining"
                        last_change = time.time()
                        good_windows, bad_windows = 0, 0
                        action, reason = f"force_rollback_drain_started_to_{active}", "manual_override"
                        # drain_loser persisted below alongside drain_target
                    else:
                        reason = "force_rollback_no_active_ramp"
                elif force_cmd == "force_hold":
                    last_change = time.time()
                    action, reason = "force_hold", "manual_hold"

            elif total_rps < MIN_TOTAL_RPS:
                reason = "hold_low_traffic"

            else:
                cooldown_ok = (time.time() - last_change) >= COOLDOWN_SECONDS

                # --- Phase 1A: post-failover hold ---
                if phase == "post_failover_hold":
                    recovering = failover_from
                    slo_rec = slos.get(recovering)
                    slo_rec_ok = (
                        slo_rec is not None
                        and slo_rec.p95_ms is not None and slo_rec.p95_ms <= MAX_P95_MS
                        and slo_rec.err is not None and slo_rec.err <= MAX_ERROR_RATE
                        and (slo_rec.cb_open is None or slo_rec.cb_open < 1.0)
                    )

                    if slo_rec_ok:
                        post_failover_confirm_windows += 1
                    else:
                        post_failover_confirm_windows = 0

                    hold_remaining = max(0.0, last_failover_ts + POST_FAILOVER_HOLD_SECONDS - time.time())
                    M_FAILOVER_HOLD.set(hold_remaining)
                    M_FAILOVER_CONFIRM.set(post_failover_confirm_windows)

                    hold_elapsed = (time.time() - last_failover_ts) >= POST_FAILOVER_HOLD_SECONDS
                    confirm_met = post_failover_confirm_windows >= POST_FAILOVER_CONFIRM_WINDOWS_NEEDED

                    # Escape hatch: current active also degrades — allow re-failover
                    if cooldown_ok and not slos[active].ok() and slo_rec_ok:
                        set_weights_dict(full_weights_on(recovering))
                        phase = "post_failover_hold"
                        step_index = 0
                        last_failover_ts = time.time()
                        failover_from = active
                        post_failover_confirm_windows = 0
                        last_change = time.time()
                        action, reason = f"failover_to_{recovering}", "double_failover_escape"
                        if ALERT_ON_FAILOVER:
                            send_alert("failover_triggered", "critical", {
                                "from_backend": active, "to_backend": recovering,
                                "reason": "double_failover_escape",
                            })
                    elif hold_elapsed and confirm_met:
                        phase = "steady"
                        post_failover_confirm_windows = 0
                        M_FAILOVER_HOLD.set(0)
                        action, reason = "post_failover_hold_released", "hold_and_confirm_complete"
                    else:
                        reason = "post_failover_hold"

                # --- Phase 1B: graceful drain ---
                elif phase == "draining":
                    # drain_loser is persisted state; do not recompute (wrong for N>2 backends)

                    if not slos[drain_target].ok():
                        # Abort: drain target degraded, restore drain_loser
                        set_weights_dict(full_weights_on(drain_loser))
                        phase, step_index = "steady", 0
                        good_windows, bad_windows = 0, 0
                        last_change = time.time()
                        action, reason = f"drain_aborted_to_{drain_loser}", "drain_target_degraded"
                    elif (time.time() - drain_step_since) >= DRAIN_STEP_SECONDS:
                        drain_current_step += 1
                        drain_step_since = time.time()

                        if drain_current_step >= DRAIN_STEPS_COUNT or drain_initial_weight_loser <= 0:
                            set_weights_dict(full_weights_on(drain_target))
                            phase, step_index = "steady", 0
                            good_windows, bad_windows = 0, 0
                            action, reason = f"drain_complete_to_{drain_target}", "drain_complete"
                        else:
                            loser_w = drain_initial_weight_loser * (1.0 - drain_current_step / DRAIN_STEPS_COUNT)
                            winner_w = 1.0 - loser_w
                            new_w = {b: 0.0 for b in BNAMES}
                            new_w[drain_target] = winner_w
                            new_w[drain_loser] = loser_w
                            set_weights_dict(new_w)
                            action, reason = f"drain_step_{drain_current_step}_{drain_target}", "draining"
                    else:
                        reason = "drain_waiting"

                # --- Hard failover: active violates SLO, a healthy backend is available ---
                elif cooldown_ok and not slos[active].ok() and best_failover_cand is not None:
                    failover_dest = best_failover_cand
                    set_weights_dict(full_weights_on(failover_dest))
                    phase = "post_failover_hold"
                    step_index = 0
                    good_windows, bad_windows = 0, 0
                    last_failover_ts = time.time()
                    failover_from = active
                    post_failover_confirm_windows = 0
                    last_change = time.time()
                    cand = failover_dest
                    action, reason = f"failover_to_{failover_dest}", "active_slo_violation"
                    if ALERT_ON_FAILOVER:
                        send_alert("failover_triggered", "critical", {
                            "from_backend": active, "to_backend": failover_dest,
                            "reason": "active_slo_violation",
                            "slo_active": {"p95_ms": slos[active].p95_ms, "err": slos[active].err,
                                           "cb_open": slos[active].cb_open, "ok": False},
                            "slo_candidate": {"p95_ms": slos[failover_dest].p95_ms, "err": slos[failover_dest].err,
                                              "cb_open": slos[failover_dest].cb_open, "ok": True},
                        })

                # --- Steady phase ---
                elif phase == "steady":
                    # Phase 0 fix: apply SWITCHING_PENALTY_USD only at ramp-start gate
                    ramp_savings = (base_savings - SWITCHING_PENALTY_USD) if base_savings is not None else None
                    if (cooldown_ok and slos.get(cand, Slo(None, None, None, None)).ok()
                            and ramp_savings is not None and ramp_savings >= MIN_SAVINGS_PER_HOUR):
                        ramp_candidate = cand
                        step_index = 0
                        step_since = time.time()
                        good_windows, bad_windows = 0, 0
                        phase = "ramping"
                        last_change = time.time()

                        step = RAMP_STEPS[step_index]
                        set_weights_dict(build_weights(full=active, partial=cand, step=step))
                        action, reason = f"start_ramp_{ramp_candidate}", "cost_opportunity"
                    else:
                        reason = "stay_steady"

                # --- Ramping phase ---
                else:
                    cand = ramp_candidate  # locked-in ramp target
                    act = primary_from_weights(w)

                    # Phase 0 fix: use base_savings (no penalty) for ramp monitoring
                    # Recompute savings against the locked ramp_candidate
                    if total_cost.get(act) is not None and total_cost.get(cand) is not None:
                        ramp_base_savings: Optional[float] = float(total_cost[act]) - float(total_cost[cand])  # type: ignore[arg-type]
                    else:
                        ramp_base_savings = None

                    if slos.get(cand, Slo(None, None, None, None)).ok() and ramp_base_savings is not None and ramp_base_savings >= MIN_SAVINGS_PER_HOUR:
                        good_windows += 1
                        bad_windows = 0
                    else:
                        bad_windows += 1
                        good_windows = 0

                    step_elapsed_ok = (time.time() - step_since) >= RAMP_STEP_MIN_SECONDS

                    if bad_windows >= REQUIRED_BAD_WINDOWS and cooldown_ok:
                        # Phase 1B: start graceful drain instead of instant rollback
                        bad_windows_snapshot = bad_windows
                        drain_loser = cand
                        drain_target = act
                        drain_initial_weight_loser = float(w.get(cand, 0.0))
                        drain_current_step = 0
                        drain_step_since = time.time()
                        phase = "draining"
                        last_change = time.time()
                        good_windows, bad_windows = 0, 0
                        action, reason = f"rollback_drain_started_to_{act}", "candidate_unhealthy_or_no_savings"

                        if ALERT_ON_BAD_WINDOWS:
                            send_alert("bad_windows_sustained", "warning", {
                                "bad_windows": bad_windows_snapshot,
                                "threshold": REQUIRED_BAD_WINDOWS,
                                "candidate": cand, "phase": "ramping",
                            })
                        if ALERT_ON_ROLLBACK:
                            send_alert("rollback_triggered", "warning", {
                                "rolled_back_to": act, "candidate_was": cand,
                                "bad_windows": bad_windows_snapshot,
                                "slo_candidate": {
                                    "p95_ms": slos[cand].p95_ms if cand in slos else None,
                                    "err": slos[cand].err if cand in slos else None,
                                    "ok": slos[cand].ok() if cand in slos else False,
                                },
                            })

                    elif step_elapsed_ok and good_windows >= REQUIRED_GOOD_WINDOWS and cooldown_ok:
                        if step_index < len(RAMP_STEPS) - 1:
                            step_index += 1
                            step_since = time.time()
                            good_windows, bad_windows = 0, 0
                            last_change = time.time()

                            step = RAMP_STEPS[step_index]
                            set_weights_dict(build_weights(full=act, partial=cand, step=step))
                            action, reason = f"ramp_step_{step_index}_{cand}", "sustained_good_canary"
                        else:
                            phase, step_index = "steady", 0
                            good_windows, bad_windows = 0, 0
                            action, reason = f"promoted_{cand}", "ramp_complete"
                    else:
                        reason = "ramp_monitoring"

            # Update Prometheus metrics
            phase_val = {"steady": 0, "ramping": 1, "post_failover_hold": 2, "draining": 3}.get(phase, 0)
            M_PHASE.set(phase_val)
            M_GOOD.set(good_windows)
            M_BAD.set(bad_windows)
            if action != "none":
                M_ACTIONS.labels(action=action).inc()

            # Persist all state
            _set_state(con, "phase", phase)
            _set_state(con, "step_index", str(step_index))
            _set_state(con, "ramp_candidate", ramp_candidate)
            _set_state(con, "step_since", str(step_since))
            _set_state(con, "last_change", str(last_change))
            _set_state(con, "good_windows", str(good_windows))
            _set_state(con, "bad_windows", str(bad_windows))
            _set_state(con, "last_failover_ts", str(last_failover_ts))
            _set_state(con, "post_failover_confirm_windows", str(post_failover_confirm_windows))
            _set_state(con, "failover_from", failover_from)
            _set_state(con, "drain_target", drain_target)
            _set_state(con, "drain_loser", drain_loser)
            _set_state(con, "drain_current_step", str(drain_current_step))
            _set_state(con, "drain_step_since", str(drain_step_since))
            _set_state(con, "drain_initial_weight_loser", str(drain_initial_weight_loser))

            # Audit log (JSON-blob schema)
            bytesps = {b: bytes_per_s_primary(b) for b in BNAMES}
            con.execute(
                "INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ts, phase, active, cand, step_index,
                    json.dumps({b: w.get(b, 0.0) for b in BNAMES}),
                    json.dumps({b: compute.get(b) for b in BNAMES}),
                    json.dumps({b: egress.get(b) for b in BNAMES}),
                    json.dumps({b: slos[b].p95_ms if b in slos else None for b in BNAMES}),
                    json.dumps({b: slos[b].err if b in slos else None for b in BNAMES}),
                    json.dumps({b: slos[b].cb_open if b in slos else None for b in BNAMES}),
                    json.dumps({b: slos[b].rps_primary if b in slos else None for b in BNAMES}),
                    json.dumps({b: bytesps.get(b) for b in BNAMES}),
                    action, reason,
                ),
            )
            con.commit()

            # Phase 1D: update live state snapshot for Flask API
            with _state_lock:
                _live_state.update({
                    "ts": ts,
                    "phase": phase,
                    "active": active,
                    "candidate": cand,
                    "step_index": step_index,
                    "weights": dict(w),
                    "good_windows": good_windows,
                    "bad_windows": bad_windows,
                    "last_change": last_change,
                    "last_action": action,
                    "last_reason": reason,
                    "slo": {
                        b: {
                            "p95_ms": slos[b].p95_ms if b in slos else None,
                            "err": slos[b].err if b in slos else None,
                            "cb_open": slos[b].cb_open if b in slos else None,
                            "rps": slos[b].rps_primary if b in slos else None,
                            "ok": slos[b].ok() if b in slos else False,
                        }
                        for b in BNAMES
                    },
                    "cost_usd_per_hour": {b: total_cost.get(b) for b in BNAMES},
                    "egress_usd_per_hour": {b: eg_costs.get(b) for b in BNAMES},
                    "ramp_info": {
                        "candidate": ramp_candidate,
                        "step_index": step_index,
                        "step_pct": RAMP_STEPS[step_index] if step_index < len(RAMP_STEPS) else 1.0,
                        "steps": RAMP_STEPS,
                    },
                    "last_failover_ts": last_failover_ts,
                    "post_failover_confirm_windows": post_failover_confirm_windows,
                    "drain_info": {
                        "target": drain_target,
                        "current_step": drain_current_step,
                        "total_steps": DRAIN_STEPS_COUNT,
                        "initial_weight_loser": drain_initial_weight_loser,
                    },
                    "backends": BNAMES,
                })

            print(json.dumps({
                "ts": ts,
                "phase": phase,
                "weights": w,
                "slo_ok": {b: slos[b].ok() for b in slos},
                "compute_usd_per_hour": compute,
                "egress_usd_per_gb": egress,
                "estimated_total_cost_usd_per_hour": total_cost,
                "action": action,
                "reason": reason,
            }), flush=True)

        except Exception as e:
            print(json.dumps({"ts": ts, "error": str(e)}), flush=True)

        time.sleep(DECISION_INTERVAL)


if __name__ == "__main__":
    main()
