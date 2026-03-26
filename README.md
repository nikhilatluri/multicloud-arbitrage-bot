# Multi-Cloud Cost Arbitrage Bot (Docker Compose v3)

## Run
1) `cp .env.example .env` and set `ROUTER_ADMIN_TOKEN`
2) `docker compose up --build`

## URLs
- Router (traffic entry): http://localhost:8080/work
- Router config: http://localhost:8080/target
- Prometheus: http://localhost:9090
- Grafana: http://localhost:3000 (admin / $GF_ADMIN_PASSWORD)
- Bot metrics: http://localhost:9101/metrics
- Pricefeed: http://localhost:7070/price

## What’s new vs basic demo
- Router exports response bytes metrics for egress modeling.
- Pricefeed exposes compute + egress rates and also exports them as Prometheus metrics.
- Bot estimates total $/hr = compute + projected egress $/hr and ramps traffic 10%→25%→50%→100% with sustained SLO checks.
- Grafana dashboard is auto-provisioned from `grafana/dashboards/`.

## Tip: create cost changes live
POST overrides (examples):
- `curl -X POST http://localhost:7070/override -H 'Content-Type: application/json' -d '{"compute":{"b":0.006}}'`
- `curl -X POST http://localhost:7070/override -H 'Content-Type: application/json' -d '{"egress":{"a":0.12}}'`
