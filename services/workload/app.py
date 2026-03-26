import os
import random
import time
from fastapi import FastAPI, Response
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

CLOUD_ID = os.getenv("CLOUD_ID", "unknown")
BASE_LATENCY_MS = int(os.getenv("BASE_LATENCY_MS", "80"))
JITTER_MS = int(os.getenv("JITTER_MS", "20"))
ERROR_RATE = float(os.getenv("ERROR_RATE", "0.0"))
PAYLOAD_KB = int(os.getenv("PAYLOAD_KB", "16"))

app = FastAPI(title=f"workload-{CLOUD_ID}")

REQS = Counter("workload_requests_total", "Total workload requests", ["cloud", "status"])
LAT = Histogram(
    "workload_request_duration_seconds",
    "Workload request duration (seconds)",
    ["cloud"],
    buckets=(0.01, 0.025, 0.05, 0.075, 0.1, 0.15, 0.2, 0.35, 0.5, 1.0, 2.0),
)

PAYLOAD = ("x" * (PAYLOAD_KB * 1024)).encode("utf-8")

@app.get("/health")
def health():
    return {"ok": True, "cloud": CLOUD_ID}

@app.get("/work")
def work():
    start = time.time()
    delay_ms = max(0, BASE_LATENCY_MS + random.randint(-JITTER_MS, JITTER_MS))
    time.sleep(delay_ms / 1000.0)

    if random.random() < ERROR_RATE:
        REQS.labels(cloud=CLOUD_ID, status="error").inc()
        LAT.labels(cloud=CLOUD_ID).observe(time.time() - start)
        return Response(content=f"error from {CLOUD_ID}".encode("utf-8"), status_code=500)

    REQS.labels(cloud=CLOUD_ID, status="ok").inc()
    LAT.labels(cloud=CLOUD_ID).observe(time.time() - start)

    # Return bytes so router can measure egress via response size
    return Response(
        content=PAYLOAD,
        media_type="application/octet-stream",
        headers={"X-Cloud": CLOUD_ID, "X-Delay-Ms": str(delay_ms)},
        status_code=200,
    )

@app.get("/metrics")
def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
