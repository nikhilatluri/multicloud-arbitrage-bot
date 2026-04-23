# Multi-Cloud Arbitrage Bot — Complete Skill Reference

> **Purpose of this document:** A deep, end-to-end knowledge base of every service, algorithm, configuration knob, metric, and operational procedure in this repository. Use it to onboard engineers, prompt AI agents, or plan enhancements.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Architecture & Data Flow](#2-architecture--data-flow)
3. [Service: workload](#3-service-workload)
4. [Service: router](#4-service-router)
5. [Service: pricefeed](#5-service-pricefeed)
6. [Service: bot](#6-service-bot)
7. [Observability Stack](#7-observability-stack)
8. [Load Testing](#8-load-testing)
9. [Docker Compose & Infrastructure](#9-docker-compose--infrastructure)
10. [Environment Variables Reference](#10-environment-variables-reference)
11. [Prometheus Metrics Catalogue](#11-prometheus-metrics-catalogue)
12. [Bot Decision Algorithm — Deep Dive](#12-bot-decision-algorithm--deep-dive)
13. [Cost Model — Deep Dive](#13-cost-model--deep-dive)
14. [Circuit Breaker — Deep Dive](#14-circuit-breaker--deep-dive)
15. [Canary Ramp & Rollback — Deep Dive](#15-canary-ramp--rollback--deep-dive)
16. [Admin API Reference](#16-admin-api-reference)
17. [State Persistence (SQLite)](#17-state-persistence-sqlite)
18. [Known Quirks & Edge Cases](#18-known-quirks--edge-cases)
19. [Enhancement Opportunities](#19-enhancement-opportunities)
20. [Quick-Start Commands](#20-quick-start-commands)

---

## 1. System Overview

This project is a **self-driving multi-cloud traffic arbitrage system**. It continuously measures the cost and SLO health of two cloud backends (`a` and `b`), then automatically shifts traffic to whichever backend is cheaper — as long as it stays within latency and error-rate SLOs.

**Key ideas:**
- **Arbitrage**: exploit real-time price differences between cloud providers.
- **Canary ramping**: traffic shifts gradually (10 % → 25 % → 50 % → 100 %) so problems are caught early.
- **Automatic rollback**: if the candidate backend degrades during ramping, traffic is instantly returned to the original.
- **Hard failover**: if the active backend violates SLOs and the other is healthy, a full instant switch happens regardless of cost.
- **Zero manual intervention**: the bot loop runs every 15 s, persists state across restarts, and emits structured JSON logs + Prometheus metrics.

---

## 2. Architecture & Data Flow

```
                         ┌─────────────┐
  Load / Users  ─────▶  │   router    │ :8080
                         │  (FastAPI)  │
                         └──────┬──────┘
                    primary     │    shadow (5 % by default)
               ┌────────────────┤────────────────────┐
               ▼                                      ▼
        ┌─────────────┐                       ┌─────────────┐
        │ workload-a  │ :8001                 │ workload-b  │ :8002
        │  (FastAPI)  │                       │  (FastAPI)  │
        └─────────────┘                       └─────────────┘

        ┌─────────────┐
        │  pricefeed  │ :7070   ◀── bot polls /price every 15 s
        │  (FastAPI)  │
        └─────────────┘

        ┌─────────────┐
        │     bot     │ :9101 (metrics only)
        │  (Python)   │ ◀── polls Prometheus + pricefeed
        │             │ ──▶ POSTs /admin/weights to router
        └─────────────┘

        ┌─────────────┐      scrapes all /metrics endpoints
        │ Prometheus  │ :9090
        └──────┬──────┘
               │
        ┌──────▼──────┐
        │   Grafana   │ :3000
        └─────────────┘
```

**Request path:**
1. Client hits `router:8080/work` (optionally with `X-Sticky-Key` header).
2. Router picks primary backend using weighted random (or sticky hash).
3. Router optionally fire-and-forgets a shadow copy to the secondary backend.
4. Router records latency, bytes, status per backend in Prometheus counters/histograms.
5. Bot reads those metrics from Prometheus every 15 s.
6. Bot fetches prices from pricefeed.
7. Bot runs decision logic → may POST new weights to `router/admin/weights`.
8. Grafana renders dashboards from Prometheus.

---

## 3. Service: workload

**File:** `services/workload/app.py`  
**Dockerfile:** `services/workload/Dockerfile`  
**Port (internal):** `8000`  
**External ports:** `8001` (workload-a), `8002` (workload-b)

### Purpose
Synthetic backend that simulates a real cloud workload. Two instances run side-by-side, each configured with different latency, error rate, and payload size to mimic real cloud cost/performance differences.

### Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/work` | Main work endpoint. Sleeps for `BASE_LATENCY_MS ± JITTER_MS`, optionally returns 500 at `ERROR_RATE`, then returns `PAYLOAD_KB` kilobytes of data. |
| GET | `/health` | Returns `{"ok": true, "cloud": "<CLOUD_ID>"}` |
| GET | `/metrics` | Prometheus metrics |

### Configuration (env vars)

| Variable | Default | Description |
|----------|---------|-------------|
| `CLOUD_ID` | `unknown` | Label for all metrics and responses |
| `BASE_LATENCY_MS` | `80` | Base response delay in ms |
| `JITTER_MS` | `20` | ± random jitter added to base latency |
| `ERROR_RATE` | `0.0` | Fraction of requests that return HTTP 500 |
| `PAYLOAD_KB` | `16` | Response payload size in kilobytes |

### Metrics emitted

| Metric | Type | Labels |
|--------|------|--------|
| `workload_requests_total` | Counter | `cloud`, `status` (`ok`/`error`) |
| `workload_request_duration_seconds` | Histogram | `cloud` |

### Docker Compose defaults

| Instance | CLOUD_ID | BASE_LATENCY_MS | JITTER_MS | ERROR_RATE | PAYLOAD_KB |
|----------|----------|-----------------|-----------|------------|------------|
| workload-a | `a` | 80 | 30 | 0.01 (1 %) | 32 |
| workload-b | `b` | 120 | 40 | 0.005 (0.5 %) | 64 |

> **Note:** workload-b is slower but has a lower error rate and larger payload. The larger payload means higher egress costs, which the bot factors into routing decisions.

---

## 4. Service: router

**File:** `services/router/app.py`  
**Dockerfile:** `services/router/Dockerfile`  
**Port (internal):** `8000`, **external:** `8080`

### Purpose
An HTTP reverse proxy with weighted routing, sticky sessions, circuit breakers, shadow traffic, and Prometheus instrumentation. It is the **only ingress point** for user traffic. The bot controls it via the admin API.

### Routing Modes

| Mode | Behaviour |
|------|-----------|
| `weighted` | Traffic split by `WEIGHTS["a"]` : `WEIGHTS["b"]` (default) |
| `force_a` | All traffic to `a`; falls back to `b` if `a`'s circuit is open |
| `force_b` | All traffic to `b`; falls back to `a` if `b`'s circuit is open |

### Sticky Sessions
If request carries header `X-Sticky-Key: <value>`, the backend is chosen deterministically via SHA-256 hash of the key. This means the same user always goes to the same backend during a canary — important for consistent UX testing.

### Circuit Breaker (per backend)
- Counts consecutive failures.
- After `CB_FAIL_THRESHOLD` failures (default 5), the circuit opens for `CB_OPEN_SECONDS` (default 20 s).
- While open, that backend is bypassed; all traffic goes to the other backend.
- On success, the failure counter resets to 0.

### Shadow Traffic
If `SHADOW_PERCENT > 0`, a random fraction of requests are also sent fire-and-forget to the **non-primary** backend. The shadow response is discarded (not returned to the client) but its metrics are recorded under `kind="shadow"`. This lets the bot measure the candidate backend's performance without affecting users.

### Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/target` | None | Current mode, weights, URLs, circuit breaker state |
| POST | `/admin/mode` | `X-Admin-Token` | Set routing mode |
| POST | `/admin/weights` | `X-Admin-Token` | Set `{"a": float, "b": float}` weights. Health-checks each backend before applying. |
| GET | `/health` | None | Router liveness |
| GET | `/metrics` | None | Prometheus metrics |
| ANY | `/{path}` | None | Proxy catch-all — forwards to chosen backend |

### Configuration (env vars)

| Variable | Default | Description |
|----------|---------|-------------|
| `TARGET_A_URL` | `http://localhost:8001` | Backend A base URL |
| `TARGET_B_URL` | `http://localhost:8002` | Backend B base URL |
| `ROUTER_ADMIN_TOKEN` | `changeme` | Token required for admin endpoints |
| `CB_FAIL_THRESHOLD` | `5` | Failures before circuit opens |
| `CB_OPEN_SECONDS` | `20` | Seconds circuit stays open |
| `SHADOW_PERCENT` | `0.0` | Fraction of requests mirrored to secondary (0.0–1.0) |
| `STICKY_HEADER` | `X-Sticky-Key` | Header name for sticky routing |

### Metrics emitted

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `router_backend_requests_total` | Counter | `backend`, `kind` (`primary`/`shadow`), `code_class` (`2xx`/`4xx`/`5xx`) | Request counts |
| `router_backend_response_bytes_total` | Counter | `backend`, `kind` | Bytes returned — used for egress cost estimation |
| `router_backend_latency_seconds` | Histogram | `backend`, `kind` | Latency histogram (12 buckets from 10 ms to 5 s) |
| `router_circuit_open` | Gauge | `backend` | 1 if circuit open, 0 if closed |
| `router_mode` | Gauge | — | 0=weighted, 1=force_a, 2=force_b |
| `router_weight` | Gauge | `backend` | Current weight value |

---

## 5. Service: pricefeed

**File:** `services/pricefeed/app.py`  
**Dockerfile:** `services/pricefeed/Dockerfile`  
**Port (internal):** `8000`, **external:** `7070`

### Purpose
Provides compute and egress prices for both backends. Base prices come from environment variables. Prices can be overridden at runtime via the `/override` API, enabling simulation of spot-price changes, price spikes, and arbitrage scenarios without restarting services.

### Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/price` | Returns current compute + egress prices for `a` and `b` |
| POST | `/override` | Override any price; `null` removes override and reverts to base |
| POST | `/clear` | Removes all overrides |
| GET | `/metrics` | Prometheus metrics |

### Override payload example
```json
{
  "compute": {"a": 0.009, "b": null},
  "egress":  {"a": 0.08}
}
```

### Configuration (env vars)

| Variable | Default | Description |
|----------|---------|-------------|
| `COMPUTE_A_USD_PER_HOUR` | `0.010` | Backend A compute cost per hour |
| `COMPUTE_B_USD_PER_HOUR` | `0.008` | Backend B compute cost per hour |
| `EGRESS_A_USD_PER_GB` | `0.090` | Backend A egress cost per GB |
| `EGRESS_B_USD_PER_GB` | `0.085` | Backend B egress cost per GB |

### Metrics emitted

| Metric | Type | Labels |
|--------|------|--------|
| `price_compute_usd_per_hour` | Gauge | `backend` |
| `price_egress_usd_per_gb` | Gauge | `backend` |

---

## 6. Service: bot

**File:** `services/bot/bot.py`  
**Dockerfile:** `services/bot/Dockerfile`  
**Port:** `9101` (Prometheus metrics only — no HTTP API)

### Purpose
The autonomous decision engine. Runs an infinite loop every `DECISION_INTERVAL_SECONDS`. Each iteration:
1. Queries Prometheus for SLO signals (P95 latency, error rate, circuit breaker, RPS, bytes/s).
2. Fetches current prices from pricefeed.
3. Estimates total cost per hour for routing 100 % of traffic to `a` vs `b`.
4. Decides whether to stay, ramp toward the cheaper backend, advance the ramp, roll back, or hard-failover.
5. Persists all state to SQLite (`bot.db`) for crash recovery.
6. Writes a structured JSON decision log to stdout.

### SLO Definition (`Slo` dataclass)

```python
@dataclass
class Slo:
    p95_ms: Optional[float]       # P95 latency in milliseconds
    err: Optional[float]          # Error rate (0.0–1.0)
    cb_open: Optional[float]      # Circuit breaker open (1.0 = open)
    rps_primary: Optional[float]  # Requests per second on primary
```

`Slo.ok()` returns `True` iff:
- Circuit breaker is NOT open (`cb_open < 1.0`)
- `p95_ms <= MAX_P95_MS` (default 200 ms)
- `err <= MAX_ERROR_RATE` (default 0.02 = 2 %)

### Prometheus queries used by bot

| Signal | PromQL pattern |
|--------|---------------|
| P95 latency (ms) | `histogram_quantile(0.95, sum(rate(router_backend_latency_seconds_bucket{backend="X",kind="primary"}[WINDOW])) by (le)) * 1000` |
| Error rate | `sum(rate(...5xx[W])) / sum(rate(...total[W]))` |
| RPS (primary) | `sum(rate(router_backend_requests_total{backend="X",kind="primary"}[W]))` |
| Bytes/s (primary) | `sum(rate(router_backend_response_bytes_total{backend="X",kind="primary"}[W]))` |
| Bytes/s (shadow) | `sum(rate(router_backend_response_bytes_total{backend="X",kind="shadow"}[W]))` |
| Circuit open | `max(router_circuit_open{backend="X"})` |

### Configuration (env vars)

| Variable | Default | Description |
|----------|---------|-------------|
| `PROM_URL` | `http://localhost:9090` | Prometheus base URL |
| `ROUTER_URL` | `http://localhost:8080` | Router base URL |
| `ROUTER_ADMIN_TOKEN` | `changeme` | Token for router admin API |
| `PRICEFEED_URL` | `http://localhost:7070` | Pricefeed base URL |
| `DECISION_INTERVAL_SECONDS` | `15` | Loop sleep interval |
| `COOLDOWN_SECONDS` | `60` | Min seconds between weight changes |
| `WINDOW` | `2m` | Prometheus rate window |
| `MAX_P95_MS` | `200` | SLO: max P95 latency (ms) |
| `MAX_ERROR_RATE` | `0.02` | SLO: max error rate |
| `MIN_TOTAL_RPS` | `1` | Minimum RPS before bot acts |
| `MIN_SAVINGS_PER_HOUR` | `0.001` | Minimum $/hr savings to trigger ramp |
| `SWITCHING_PENALTY_USD` | `0.0` | One-time penalty subtracted from savings estimate |
| `RAMP_STEPS` | `0.10,0.25,0.50,1.00` | Traffic fractions for canary steps |
| `RAMP_STEP_MIN_SECONDS` | `45` | Minimum dwell time at each ramp step |
| `REQUIRED_GOOD_WINDOWS` | `3` | Consecutive good windows to advance ramp |
| `REQUIRED_BAD_WINDOWS` | `2` | Consecutive bad windows to trigger rollback |
| `EG_COST_MODE` | `project_full_traffic` | Cost projection mode (`project_full_traffic` \| `use_observed_split`) |
| `DB_PATH` | `bot.db` | SQLite file path |
| `BOT_METRICS_PORT` | `9101` | Port for bot's Prometheus metrics |

### Bot Metrics emitted

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `bot_phase` | Gauge | — | 0=steady, 1=ramping |
| `bot_good_windows` | Gauge | — | Consecutive good observation windows |
| `bot_bad_windows` | Gauge | — | Consecutive bad observation windows |
| `bot_actions_total` | Counter | `action` | Count of each action taken |
| `bot_estimated_total_cost_usd_per_hour` | Gauge | `backend` | Estimated total $/hr if 100 % on this backend |
| `bot_estimated_egress_cost_usd_per_hour` | Gauge | `backend` | Egress-only portion of the above |

---

## 7. Observability Stack

### Prometheus

**Config:** `prometheus/prometheus.yml`  
**Port:** `9090`  
**Image:** `prom/prometheus:v2.54.1`

Scrapes `/metrics` from: `router:8000`, `workload-a:8000`, `workload-b:8000`, `pricefeed:8000`, `bot:9101`.

### Grafana

**Port:** `3000` (admin/admin by default)  
**Image:** `grafana/grafana:11.1.4`

- Datasource auto-provisioned: Prometheus at `http://prometheus:9090`
- Dashboard auto-provisioned: `multicloud_arbitrage_dashboard.json`

**Dashboard panels include:**
- Router weights over time (backend a vs b)
- P95 latency per backend
- Error rate per backend
- Bot phase and window counters
- Estimated cost per backend (compute + egress)
- Circuit breaker state
- Request throughput (RPS)
- Bytes throughput (egress proxy)

---

## 8. Load Testing

**File:** `services/loadtest/k6.js`  
**Tool:** [k6](https://k6.io/)

```javascript
export const options = {
  vus: 30,       // 30 virtual users
  duration: "3m" // 3 minutes
};
```

Each VU sends `GET http://localhost:8080/work` with a per-VU `X-Sticky-Key` header (e.g. `user-1`, `user-2`, ...) every 100 ms. This means each virtual user is sticky to one backend throughout the test, enabling realistic canary behavior observation.

**Run:**
```bash
k6 run services/loadtest/k6.js
```

---

## 9. Docker Compose & Infrastructure

**File:** `docker-compose.yml`

### Services summary

| Service | Image / Build | Internal Port | External Port | Depends On |
|---------|--------------|---------------|---------------|------------|
| `workload-a` | `./services/workload` | 8000 | 8001 | — |
| `workload-b` | `./services/workload` | 8000 | 8002 | — |
| `router` | `./services/router` | 8000 | 8080 | workload-a, workload-b |
| `pricefeed` | `./services/pricefeed` | 8000 | 7070 | — |
| `prometheus` | `prom/prometheus:v2.54.1` | 9090 | 9090 | router, workloads, pricefeed, bot |
| `grafana` | `grafana/grafana:11.1.4` | 3000 | 3000 | prometheus |
| `bot` | `./services/bot` | 9101 | 9101 | router, pricefeed |

### Named volumes
- `botdata` — mounted at `/data` in the `bot` container; stores `bot.db` SQLite state.

### Sensitive env vars (use `.env` file)
- `ROUTER_ADMIN_TOKEN` — defaults to `changeme`
- `GF_ADMIN_PASSWORD` — Grafana admin password, defaults to `admin`

---

## 10. Environment Variables Reference

### Complete variable matrix

| Variable | Service | Default | Type |
|----------|---------|---------|------|
| `CLOUD_ID` | workload | `unknown` | str |
| `BASE_LATENCY_MS` | workload | `80` | int (ms) |
| `JITTER_MS` | workload | `20` | int (ms) |
| `ERROR_RATE` | workload | `0.0` | float (0–1) |
| `PAYLOAD_KB` | workload | `16` | int |
| `TARGET_A_URL` | router | `http://localhost:8001` | URL |
| `TARGET_B_URL` | router | `http://localhost:8002` | URL |
| `ROUTER_ADMIN_TOKEN` | router, bot | `changeme` | str |
| `CB_FAIL_THRESHOLD` | router | `5` | int |
| `CB_OPEN_SECONDS` | router | `20` | int |
| `SHADOW_PERCENT` | router | `0.0` | float (0–1) |
| `STICKY_HEADER` | router | `X-Sticky-Key` | str |
| `COMPUTE_A_USD_PER_HOUR` | pricefeed | `0.010` | float |
| `COMPUTE_B_USD_PER_HOUR` | pricefeed | `0.008` | float |
| `EGRESS_A_USD_PER_GB` | pricefeed | `0.090` | float |
| `EGRESS_B_USD_PER_GB` | pricefeed | `0.085` | float |
| `PROM_URL` | bot | `http://localhost:9090` | URL |
| `ROUTER_URL` | bot | `http://localhost:8080` | URL |
| `PRICEFEED_URL` | bot | `http://localhost:7070` | URL |
| `DECISION_INTERVAL_SECONDS` | bot | `15` | int |
| `COOLDOWN_SECONDS` | bot | `60` | int |
| `WINDOW` | bot | `2m` | PromQL duration |
| `MAX_P95_MS` | bot | `200` | float |
| `MAX_ERROR_RATE` | bot | `0.02` | float |
| `MIN_TOTAL_RPS` | bot | `1` | float |
| `MIN_SAVINGS_PER_HOUR` | bot | `0.001` | float |
| `SWITCHING_PENALTY_USD` | bot | `0.0` | float |
| `RAMP_STEPS` | bot | `0.10,0.25,0.50,1.00` | CSV floats |
| `RAMP_STEP_MIN_SECONDS` | bot | `45` | int |
| `REQUIRED_GOOD_WINDOWS` | bot | `3` | int |
| `REQUIRED_BAD_WINDOWS` | bot | `2` | int |
| `EG_COST_MODE` | bot | `project_full_traffic` | enum |
| `DB_PATH` | bot | `bot.db` | path |
| `BOT_METRICS_PORT` | bot | `9101` | int |

---

## 11. Prometheus Metrics Catalogue

### From `router`

```
router_backend_requests_total{backend, kind, code_class}
router_backend_response_bytes_total{backend, kind}
router_backend_latency_seconds_bucket{backend, kind, le}
router_circuit_open{backend}
router_mode
router_weight{backend}
```

### From `workload`

```
workload_requests_total{cloud, status}
workload_request_duration_seconds_bucket{cloud, le}
```

### From `pricefeed`

```
price_compute_usd_per_hour{backend}
price_egress_usd_per_gb{backend}
```

### From `bot`

```
bot_phase
bot_good_windows
bot_bad_windows
bot_actions_total{action}
bot_estimated_total_cost_usd_per_hour{backend}
bot_estimated_egress_cost_usd_per_hour{backend}
```

---

## 12. Bot Decision Algorithm — Deep Dive

The bot's `main()` loop runs every `DECISION_INTERVAL_SECONDS`. Here is the full decision tree:

```
START LOOP
│
├─ Fetch: weights (GET /target), SLOs (Prometheus), prices (GET /price)
├─ Compute: egress + total cost estimates for a and b
├─ total_rps = rps_a + rps_b
│
├─ IF total_rps < MIN_TOTAL_RPS:
│     action = "hold_low_traffic"  ← bot does nothing during low traffic
│
└─ ELSE:
      cooldown_ok = (now - last_change) >= COOLDOWN_SECONDS
      savings_per_hour = total_cost[active] - total_cost[candidate] - SWITCHING_PENALTY_USD

      ┌─ HARD FAILOVER (highest priority):
      │  IF cooldown_ok AND active.SLO_violated AND candidate.SLO_ok:
      │     set_weights(0/1 or 1/0)  ← instant full switch
      │     phase = steady, reset counters
      │     action = "failover_to_<cand>"
      │
      └─ ELSE:
           ┌─ PHASE: steady
           │  IF cooldown_ok AND cand.SLO_ok AND savings >= MIN_SAVINGS_PER_HOUR:
           │     phase = ramping, step_index = 0
           │     set_weights(ramp_step[0])
           │     action = "start_ramp_<cand>"
           │  ELSE:
           │     action = "stay_steady"
           │
           └─ PHASE: ramping
              IF cand.SLO_ok AND savings >= MIN_SAVINGS_PER_HOUR:
                 good_windows += 1, bad_windows = 0
              ELSE:
                 bad_windows += 1, good_windows = 0

              ┌─ ROLLBACK:
              │  IF bad_windows >= REQUIRED_BAD_WINDOWS AND cooldown_ok:
              │     set_weights(full back to active)
              │     phase = steady
              │     action = "rollback_to_<active>"
              │
              ├─ ADVANCE STEP:
              │  IF step_elapsed >= RAMP_STEP_MIN_SECONDS
              │     AND good_windows >= REQUIRED_GOOD_WINDOWS
              │     AND cooldown_ok:
              │     IF more steps remain:
              │        step_index++, set_weights(next step)
              │        action = "ramp_step_<N>_<cand>"
              │     ELSE (last step done):
              │        phase = steady (cand is now active at 100 %)
              │        action = "promoted_<cand>"
              │
              └─ OTHERWISE:
                    action = "ramp_monitoring"
```

### Action values emitted (for `bot_actions_total` label)

| Action | Meaning |
|--------|---------|
| `none` | No-op this cycle |
| `hold_low_traffic` | Traffic too low to act |
| `stay_steady` | Steady, no opportunity |
| `failover_to_a` / `failover_to_b` | Emergency switch |
| `start_ramp_a` / `start_ramp_b` | Canary started |
| `ramp_step_1_a` ... `ramp_step_3_b` | Step advanced |
| `promoted_a` / `promoted_b` | Full switch completed |
| `rollback_to_a` / `rollback_to_b` | Canary aborted |
| `ramp_monitoring` | Watching during ramp step |

---

## 13. Cost Model — Deep Dive

### Total cost formula

```
total_cost[backend] = compute_usd_per_hour[backend]
                    + egress_cost_per_hour_if_100pct_on[backend]
```

### Egress cost estimation (`EG_COST_MODE`)

**Mode: `project_full_traffic`** (default, recommended)

Projects what egress would cost if **all** traffic moved to this backend.

```
total_rps = rps_primary_a + rps_primary_b

# bytes_per_req for backend X = shadow bytes/s / shadow rps
#   (falls back to primary bytes/s / primary rps if shadow insufficient)
projected_bytes_s = total_rps * bytes_per_req[backend]

egress_cost_per_hour = (projected_bytes_s * 3600 / 1_073_741_824)
                     * egress_usd_per_gb[backend]
```

This is fair because it normalizes for different payload sizes between backends.

**Mode: `use_observed_split`**

Just multiplies the current observed bytes/s by the egress rate. Simpler but misleading during canary (backend serving 10 % looks cheap).

### Savings calculation

```
savings_per_hour = total_cost[active] - total_cost[candidate] - SWITCHING_PENALTY_USD
```

The `SWITCHING_PENALTY_USD` amortizes any one-time switching cost (e.g., warm-up) over one hour. In the compose default it is `0.0002` $/hr.

Ramp only starts if `savings_per_hour >= MIN_SAVINGS_PER_HOUR` (default `0.001` $/hr = 0.1 cent/hr).

---

## 14. Circuit Breaker — Deep Dive

The router maintains per-backend state:

```python
_cb = {
    "a": {"fails": 0.0, "open_until": 0.0},
    "b": {"fails": 0.0, "open_until": 0.0},
}
```

**Opening:** After `CB_FAIL_THRESHOLD` consecutive failures (HTTP 5xx or connection error), `open_until = now + CB_OPEN_SECONDS`. Fail counter resets.

**Closing:** After `CB_OPEN_SECONDS` seconds, `_cb_is_open()` returns False. The backend is retried. Any success resets the counter.

**Bot interaction:** The bot reads `max(router_circuit_open{backend="X"})` from Prometheus. If it's `1.0`, `Slo.ok()` returns False, which may trigger a failover to the other backend.

**Important:** The circuit breaker state is **in-memory only** in the router. A router restart resets all circuit breakers. The bot's SQLite state is independent and survives restarts.

---

## 15. Canary Ramp & Rollback — Deep Dive

### Ramp steps (default)

| Step Index | Candidate traffic fraction | Active traffic fraction |
|------------|---------------------------|------------------------|
| 0 | 10 % | 90 % |
| 1 | 25 % | 75 % |
| 2 | 50 % | 50 % |
| 3 | 100 % | 0 % |

### Advancement criteria (all must be true)
- Time at current step ≥ `RAMP_STEP_MIN_SECONDS` (45 s)
- `good_windows >= REQUIRED_GOOD_WINDOWS` (3 consecutive)
- `cooldown_ok` (60 s since last change)
- candidate `Slo.ok()` is True
- `savings_per_hour >= MIN_SAVINGS_PER_HOUR`

### Rollback criteria (any bad window)
- `bad_windows >= REQUIRED_BAD_WINDOWS` (2 consecutive)
- `cooldown_ok`

### Step weights calculation

```python
# ramping toward candidate "b":
wa = 1.0 - step   # e.g. 0.90 at step 0
wb = step         # e.g. 0.10 at step 0

# ramping toward candidate "a":
wa = step
wb = 1.0 - step
```

---

## 16. Admin API Reference

### Router Admin Endpoints

#### `POST /admin/weights`
**Header:** `X-Admin-Token: <token>`  
**Body:**
```json
{"a": 0.75, "b": 0.25}
```
- Validates both values are `>= 0`
- Health-checks each backend before applying (returns 502 if unhealthy)
- Updates global `WEIGHTS` dict

#### `POST /admin/mode`
**Header:** `X-Admin-Token: <token>`  
**Body:**
```json
{"mode": "weighted"}
```
Valid values: `weighted`, `force_a`, `force_b`

### Pricefeed Admin Endpoints

#### `POST /override`
```json
{
  "compute": {"a": 0.009},
  "egress": {"b": 0.07}
}
```
Set `null` to revert to base price.

#### `POST /clear`
Resets all overrides.

---

## 17. State Persistence (SQLite)

**Database:** `bot.db` (inside `botdata` Docker volume)

### Table: `state`

Key-value store for bot operational state:

| Key | Type | Description |
|-----|------|-------------|
| `phase` | str | `steady` or `ramping` |
| `step_index` | int | Current ramp step (0–3) |
| `ramp_candidate` | str | `a` or `b` |
| `step_since` | float | Unix timestamp of last step change |
| `last_change` | float | Unix timestamp of last weight change |
| `good_windows` | int | Consecutive good decision windows |
| `bad_windows` | int | Consecutive bad decision windows |

### Table: `decisions`

Full audit log. One row per bot loop iteration:

```sql
ts, phase, active, candidate, step_index,
weights_a, weights_b,
compute_a, compute_b,
egress_a, egress_b,
p95_a, p95_b,
err_a, err_b,
cb_a, cb_b,
rps_a, rps_b,
bytesps_a, bytesps_b,
action, reason
```

**Query examples:**

```sql
-- Last 20 decisions
SELECT ts, phase, action, reason, weights_a, weights_b FROM decisions ORDER BY ts DESC LIMIT 20;

-- All failovers
SELECT * FROM decisions WHERE action LIKE 'failover%';

-- Cost trend
SELECT ts, compute_a + egress_a AS cost_a, compute_b + egress_b AS cost_b FROM decisions ORDER BY ts;
```

---

## 18. Known Quirks & Edge Cases

1. **Double LAT observation:** In `router/app.py` the `LAT.labels(backend=primary, kind="primary").observe(...)` call appears in both the `except` block and the `finally` block. On exception, latency is recorded twice. This inflates P95 slightly on error paths.

2. **Circuit breaker not persisted:** Router CB state lives in RAM. A router restart during an outage resets it, potentially sending traffic to a still-broken backend.

3. **`active_from_weights` heuristic:** The bot infers the "active" backend as whichever has `weight >= other`. At 50/50 split it picks `a`. This means during symmetric canary, `a` is always treated as active and `b` as candidate.

4. **Shadow fires even during ramp:** If `SHADOW_PERCENT > 0`, both backends receive traffic even during a full-on ramp. The shadow backend's metrics still accumulate, which can confuse the cost projection in `use_observed_split` mode.

5. **Prometheus warmup:** If the bot starts before enough Prometheus data accumulates (< `WINDOW` old), `prom_query` returns `None` for most signals. The bot catches exceptions and logs them but takes no action — safe but silent.

6. **`SWITCHING_PENALTY_USD` semantics:** It is subtracted from savings every decision cycle, not once per switch event. Set it to `0` unless you want a per-cycle cost tax on switches.

7. **`set_weights` health check:** The router's `POST /admin/weights` calls `/health` on each backend before accepting new weights. If a backend is temporarily slow (not 500), `/health` still returns 200, so the health check won't block the weight change.

---

## 19. Enhancement Opportunities

The following are natural next areas for development:

- **Multi-backend support:** Generalize from 2 backends (a/b) to N backends with a ranked selection.
- **Real cloud pricing APIs:** Replace static pricefeed env vars with live AWS/GCP/Azure spot price API calls.
- **Hysteresis on failover:** Add a "recovery confirmation" phase before switching back after a failover.
- **Egress-aware payload compression:** Route to the backend where smaller responses naturally occur.
- **Bot REST API:** Expose `/status`, `/history`, and `/force` endpoints on the bot for manual overrides.
- **Alert integration:** Emit PagerDuty/Slack webhooks on failover or sustained bad-window events.
- **Grafana alerting:** Add alert rules directly in the dashboard JSON for SLO breaches.
- **Graceful drain:** Before setting a backend weight to 0, gradually drain over N seconds for in-flight request safety.
- **Cost anomaly detection:** Alert when projected costs exceed a threshold regardless of routing decision.
- **AB test tracking:** Persist per-sticky-key backend assignment to measure business metrics per cloud.

---

## 20. Quick-Start Commands

### Start everything

```bash
docker compose up --build
```

### Run load test (requires k6)

```bash
k6 run services/loadtest/k6.js
```

### Watch bot decisions in real time

```bash
docker compose logs -f bot
```

### Force traffic to backend B

```bash
curl -s -X POST http://localhost:8080/admin/mode \
  -H "X-Admin-Token: changeme" \
  -H "Content-Type: application/json" \
  -d '{"mode":"force_b"}'
```

### Simulate a price spike on backend A

```bash
curl -s -X POST http://localhost:7070/override \
  -H "Content-Type: application/json" \
  -d '{"compute":{"a": 0.05}}'
```

### Simulate backend A going down (high error rate)

Edit `docker-compose.yml` or call the workload's env... or just:
```bash
docker compose stop workload-a
```

### Query bot audit log

```bash
docker compose exec bot sqlite3 /data/bot.db \
  "SELECT ts, action, reason, weights_a, weights_b FROM decisions ORDER BY ts DESC LIMIT 10;"
```

### Check current router state

```bash
curl -s http://localhost:8080/target | python -m json.tool
```

### Check current prices

```bash
curl -s http://localhost:7070/price | python -m json.tool
```

### Teardown

```bash
docker compose down -v
```

---

*Last updated: April 23, 2026 — auto-generated from full codebase review.*

