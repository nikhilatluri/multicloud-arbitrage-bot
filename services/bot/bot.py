import os
import sqlite3
import time
import json
from dataclasses import dataclass
from typing import Dict, List, Literal, Optional, Tuple

import requests
from prometheus_client import Gauge, Counter, start_http_server

Backend = Literal["a", "b"]
Phase = Literal["steady", "ramping"]

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
SWITCHING_PENALTY_USD = float(os.getenv("SWITCHING_PENALTY_USD", "0.0"))

RAMP_STEPS = [float(x.strip()) for x in os.getenv("RAMP_STEPS", "0.10,0.25,0.50,1.00").split(",")]
RAMP_STEP_MIN_SECONDS = int(os.getenv("RAMP_STEP_MIN_SECONDS", "45"))
REQUIRED_GOOD_WINDOWS = int(os.getenv("REQUIRED_GOOD_WINDOWS", "3"))
REQUIRED_BAD_WINDOWS = int(os.getenv("REQUIRED_BAD_WINDOWS", "2"))

EG_COST_MODE = os.getenv("EG_COST_MODE", "project_full_traffic")  # project_full_traffic | use_observed_split
DB_PATH = os.getenv("DB_PATH", "bot.db")
BOT_METRICS_PORT = int(os.getenv("BOT_METRICS_PORT", "9101"))

# --- Bot metrics ---
M_PHASE = Gauge("bot_phase", "Bot phase (0 steady, 1 ramping)")
M_GOOD = Gauge("bot_good_windows", "Consecutive good windows")
M_BAD = Gauge("bot_bad_windows", "Consecutive bad windows")
M_ACTIONS = Counter("bot_actions_total", "Bot actions", ["action"])
M_EST_COST = Gauge("bot_estimated_total_cost_usd_per_hour", "Estimated total $/hr", ["backend"])
M_EST_EGRESS = Gauge("bot_estimated_egress_cost_usd_per_hour", "Estimated egress $/hr", ["backend"])

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

def _db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS state(
        k TEXT PRIMARY KEY,
        v TEXT
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS decisions(
        ts TEXT,
        phase TEXT,
        active TEXT,
        candidate TEXT,
        step_index INTEGER,
        weights_a REAL, weights_b REAL,
        compute_a REAL, compute_b REAL,
        egress_a REAL, egress_b REAL,
        p95_a REAL, p95_b REAL,
        err_a REAL, err_b REAL,
        cb_a REAL, cb_b REAL,
        rps_a REAL, rps_b REAL,
        bytesps_a REAL, bytesps_b REAL,
        action TEXT,
        reason TEXT
    )""")
    return con

def _get_state(con: sqlite3.Connection, k: str, default: str) -> str:
    row = con.execute("SELECT v FROM state WHERE k=?", (k,)).fetchone()
    return row[0] if row else default

def _set_state(con: sqlite3.Connection, k: str, v: str) -> None:
    con.execute("INSERT INTO state(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
    con.commit()

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

def p95_ms(backend: Backend) -> Optional[float]:
    q = (
        f'histogram_quantile(0.95, '
        f'sum(rate(router_backend_latency_seconds_bucket{{backend="{backend}",kind="primary"}}[{WINDOW}])) by (le)'
        f')'
    )
    v = prom_query(q)
    return None if v is None else v * 1000.0

def err_rate(backend: Backend) -> Optional[float]:
    q = (
        f'(sum(rate(router_backend_requests_total{{backend="{backend}",kind="primary",code_class="5xx"}}[{WINDOW}])) '
        f'/ sum(rate(router_backend_requests_total{{backend="{backend}",kind="primary"}}[{WINDOW}])))'
    )
    return prom_query(q)

def rps_primary(backend: Backend) -> Optional[float]:
    q = f'sum(rate(router_backend_requests_total{{backend="{backend}",kind="primary"}}[{WINDOW}]))'
    return prom_query(q)

def bytes_per_s_primary(backend: Backend) -> Optional[float]:
    q = f'sum(rate(router_backend_response_bytes_total{{backend="{backend}",kind="primary"}}[{WINDOW}]))'
    return prom_query(q)

def bytes_per_s_shadow(backend: Backend) -> Optional[float]:
    q = f'sum(rate(router_backend_response_bytes_total{{backend="{backend}",kind="shadow"}}[{WINDOW}]))'
    return prom_query(q)

def rps_shadow(backend: Backend) -> Optional[float]:
    q = f'sum(rate(router_backend_requests_total{{backend="{backend}",kind="shadow"}}[{WINDOW}]))'
    return prom_query(q)

def cb_open(backend: Backend) -> Optional[float]:
    q = f'max(router_circuit_open{{backend="{backend}"}})'
    return prom_query(q)

def get_slos() -> Dict[Backend, Slo]:
    return {
        "a": Slo(p95_ms("a"), err_rate("a"), cb_open("a"), rps_primary("a")),
        "b": Slo(p95_ms("b"), err_rate("b"), cb_open("b"), rps_primary("b")),
    }

def get_prices() -> Tuple[Dict[Backend, float], Dict[Backend, float]]:
    r = requests.get(f"{PRICEFEED_URL}/price", timeout=5)
    r.raise_for_status()
    j = r.json()
    compute = {"a": float(j["compute_usd_per_hour"]["a"]), "b": float(j["compute_usd_per_hour"]["b"])}
    egress = {"a": float(j["egress_usd_per_gb"]["a"]), "b": float(j["egress_usd_per_gb"]["b"])}
    return compute, egress

def get_weights() -> Dict[Backend, float]:
    r = requests.get(f"{ROUTER_URL}/target", timeout=5)
    r.raise_for_status()
    w = r.json()["weights"]
    return {"a": float(w["a"]), "b": float(w["b"])}

def set_weights(wa: float, wb: float) -> None:
    r = requests.post(
        f"{ROUTER_URL}/admin/weights",
        json={"a": wa, "b": wb},
        headers={"X-Admin-Token": ADMIN_TOKEN},
        timeout=5,
    )
    r.raise_for_status()

def active_from_weights(w: Dict[Backend, float]) -> Backend:
    return "a" if w["a"] >= w["b"] else "b"

def other(b: Backend) -> Backend:
    return "b" if b == "a" else "a"

def _gb(x_bytes: float) -> float:
    return x_bytes / 1024.0 / 1024.0 / 1024.0

def estimate_full_traffic_bytes_per_s() -> float:
    # total observed primary bytes/s across both backends ~= total traffic bytes/s
    a = bytes_per_s_primary("a") or 0.0
    b = bytes_per_s_primary("b") or 0.0
    return a + b

def estimate_bytes_per_req_for_backend(backend: Backend) -> Optional[float]:
    # Prefer shadow sample for candidate quality; else fall back to primary
    srps = rps_shadow(backend)
    sbytes = bytes_per_s_shadow(backend)
    if srps is not None and sbytes is not None and srps > 0.2:
        return sbytes / srps

    prps = rps_primary(backend)
    pbytes = bytes_per_s_primary(backend)
    if prps is not None and pbytes is not None and prps > 0:
        return pbytes / prps

    return None

def estimate_egress_cost_per_hour_if_100pct_on(backend: Backend, egress_usd_per_gb: Dict[Backend, float]) -> Optional[float]:
    total_bytes_s = estimate_full_traffic_bytes_per_s()
    bpr = estimate_bytes_per_req_for_backend(backend)
    total_rps = (rps_primary("a") or 0.0) + (rps_primary("b") or 0.0)

    if EG_COST_MODE == "use_observed_split":
        # just use what backend currently serves (under current weights)
        bps = bytes_per_s_primary(backend)
        if bps is None:
            return None
        return _gb(bps * 3600.0) * egress_usd_per_gb[backend]

    # project_full_traffic: assume all traffic moves to target, but payload shape follows that backend's bytes/req
    if bpr is None:
        # fallback: use current aggregate traffic bytes/s without shaping
        return _gb(total_bytes_s * 3600.0) * egress_usd_per_gb[backend]

    projected_bytes_s = total_rps * bpr
    return _gb(projected_bytes_s * 3600.0) * egress_usd_per_gb[backend]

def main():
    start_http_server(BOT_METRICS_PORT)
    con = _db()

    phase: Phase = _get_state(con, "phase", "steady")  # type: ignore[assignment]
    step_index = int(_get_state(con, "step_index", "0"))
    ramp_candidate: Backend = _get_state(con, "ramp_candidate", "b")  # type: ignore[assignment]
    step_since = float(_get_state(con, "step_since", str(time.time())))
    last_change = float(_get_state(con, "last_change", "0"))

    good_windows = int(_get_state(con, "good_windows", "0"))
    bad_windows = int(_get_state(con, "bad_windows", "0"))

    while True:
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        action, reason = "none", "n/a"

        try:
            w = get_weights()
            active = active_from_weights(w)
            cand = other(active)

            slos = get_slos()
            compute, egress = get_prices()

            # egress + total cost model (per hour) for each target
            eg_a = estimate_egress_cost_per_hour_if_100pct_on("a", egress)
            eg_b = estimate_egress_cost_per_hour_if_100pct_on("b", egress)
            if eg_a is not None:
                M_EST_EGRESS.labels(backend="a").set(eg_a)
            if eg_b is not None:
                M_EST_EGRESS.labels(backend="b").set(eg_b)

            total_cost = {}
            for b in ("a", "b"):
                eg = eg_a if b == "a" else eg_b
                if eg is None:
                    total_cost[b] = None
                else:
                    total_cost[b] = compute[b] + eg
                    M_EST_COST.labels(backend=b).set(total_cost[b])

            total_rps = (slos["a"].rps_primary or 0.0) + (slos["b"].rps_primary or 0.0)
            if total_rps < MIN_TOTAL_RPS:
                reason = "hold_low_traffic"
            else:
                cooldown_ok = (time.time() - last_change) >= COOLDOWN_SECONDS

                # Savings if fully moved, including switching penalty (per hour)
                if total_cost[active] is None or total_cost[cand] is None:
                    savings_per_hour = None
                else:
                    savings_per_hour = float(total_cost[active] - total_cost[cand] - SWITCHING_PENALTY_USD)

                # Hard failover: active violates SLO and candidate ok
                if cooldown_ok and (not slos[active].ok()) and slos[cand].ok():
                    set_weights(0.0 if active == "a" else 1.0, 1.0 if active == "a" else 0.0)
                    phase, step_index = "steady", 0
                    good_windows, bad_windows = 0, 0
                    last_change = time.time()
                    action, reason = f"failover_to_{cand}", "active_slo_violation"
                else:
                    if phase == "steady":
                        if cooldown_ok and slos[cand].ok() and savings_per_hour is not None and savings_per_hour >= MIN_SAVINGS_PER_HOUR:
                            ramp_candidate = cand
                            step_index = 0
                            step_since = time.time()
                            good_windows, bad_windows = 0, 0
                            phase = "ramping"
                            last_change = time.time()

                            step = RAMP_STEPS[step_index]
                            wa, wb = (1.0 - step, step) if ramp_candidate == "b" else (step, 1.0 - step)
                            set_weights(wa, wb)
                            action, reason = f"start_ramp_{ramp_candidate}", "cost_opportunity"
                        else:
                            reason = "stay_steady"
                    else:
                        # ramping toward ramp_candidate
                        cand = ramp_candidate
                        act = other(cand)

                        if slos[cand].ok() and savings_per_hour is not None and savings_per_hour >= MIN_SAVINGS_PER_HOUR:
                            good_windows += 1
                            bad_windows = 0
                        else:
                            bad_windows += 1
                            good_windows = 0

                        step_elapsed_ok = (time.time() - step_since) >= RAMP_STEP_MIN_SECONDS

                        if bad_windows >= REQUIRED_BAD_WINDOWS and cooldown_ok:
                            set_weights(1.0 if act == "a" else 0.0, 0.0 if act == "a" else 1.0)
                            phase, step_index = "steady", 0
                            good_windows, bad_windows = 0, 0
                            last_change = time.time()
                            action, reason = f"rollback_to_{act}", "candidate_unhealthy_or_no_savings"
                        elif step_elapsed_ok and good_windows >= REQUIRED_GOOD_WINDOWS and cooldown_ok:
                            if step_index < len(RAMP_STEPS) - 1:
                                step_index += 1
                                step_since = time.time()
                                good_windows, bad_windows = 0, 0
                                last_change = time.time()

                                step = RAMP_STEPS[step_index]
                                wa, wb = (1.0 - step, step) if cand == "b" else (step, 1.0 - step)
                                set_weights(wa, wb)
                                action, reason = f"ramp_step_{step_index}_{cand}", "sustained_good_canary"
                            else:
                                phase, step_index = "steady", 0
                                good_windows, bad_windows = 0, 0
                                action, reason = f"promoted_{cand}", "ramp_complete"
                        else:
                            reason = "ramp_monitoring"

            # update bot metrics
            M_PHASE.set(0 if phase == "steady" else 1)
            M_GOOD.set(good_windows)
            M_BAD.set(bad_windows)
            if action != "none":
                M_ACTIONS.labels(action=action).inc()

            # persist state
            _set_state(con, "phase", phase)
            _set_state(con, "step_index", str(step_index))
            _set_state(con, "ramp_candidate", ramp_candidate)
            _set_state(con, "step_since", str(step_since))
            _set_state(con, "last_change", str(last_change))
            _set_state(con, "good_windows", str(good_windows))
            _set_state(con, "bad_windows", str(bad_windows))

            # audit log row
            bytesps_a = bytes_per_s_primary("a")
            bytesps_b = bytes_per_s_primary("b")
            con.execute(
                "INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ts, phase, active, cand, step_index,
                    w["a"], w["b"],
                    compute["a"], compute["b"],
                    egress["a"], egress["b"],
                    slos["a"].p95_ms, slos["b"].p95_ms,
                    slos["a"].err, slos["b"].err,
                    slos["a"].cb_open, slos["b"].cb_open,
                    slos["a"].rps_primary, slos["b"].rps_primary,
                    bytesps_a, bytesps_b,
                    action, reason
                ),
            )
            con.commit()

            print(json.dumps({
                "ts": ts,
                "phase": phase,
                "weights": w,
                "slo_ok": {"a": slos["a"].ok(), "b": slos["b"].ok()},
                "compute_usd_per_hour": compute,
                "egress_usd_per_gb": egress,
                "estimated_total_cost_usd_per_hour": total_cost,
                "action": action,
                "reason": reason
            }), flush=True)

        except Exception as e:
            print(json.dumps({"ts": ts, "error": str(e)}), flush=True)

        time.sleep(DECISION_INTERVAL)

if __name__ == "__main__":
    main()
