import os
from fastapi import FastAPI, HTTPException, Response
from prometheus_client import Gauge, generate_latest, CONTENT_TYPE_LATEST

app = FastAPI(title="pricefeed")

base_compute = {
    "a": float(os.getenv("COMPUTE_A_USD_PER_HOUR", "0.010")),
    "b": float(os.getenv("COMPUTE_B_USD_PER_HOUR", "0.008")),
}
base_egress = {
    "a": float(os.getenv("EGRESS_A_USD_PER_GB", "0.090")),
    "b": float(os.getenv("EGRESS_B_USD_PER_GB", "0.085")),
}

override_compute = {"a": None, "b": None}
override_egress = {"a": None, "b": None}

G_COMPUTE = Gauge("price_compute_usd_per_hour", "Compute price USD/hour", ["backend"])
G_EGRESS = Gauge("price_egress_usd_per_gb", "Egress price USD/GB", ["backend"])

def _current():
    compute = {
        k: float(override_compute[k]) if override_compute[k] is not None else float(base_compute[k])
        for k in ("a", "b")
    }
    egress = {
        k: float(override_egress[k]) if override_egress[k] is not None else float(base_egress[k])
        for k in ("a", "b")
    }
    for k in ("a", "b"):
        G_COMPUTE.labels(backend=k).set(compute[k])
        G_EGRESS.labels(backend=k).set(egress[k])
    return compute, egress

@app.get("/price")
def price():
    compute, egress = _current()
    return {"compute_usd_per_hour": compute, "egress_usd_per_gb": egress}

@app.post("/override")
def set_override(payload: dict):
    # payload examples:
    # {"compute": {"a": 0.009, "b": null}, "egress": {"a": 0.08}}
    if "compute" in payload:
        for k, v in payload["compute"].items():
            if k not in ("a", "b"):
                continue
            if v is None:
                override_compute[k] = None
            else:
                fv = float(v)
                if fv <= 0:
                    raise HTTPException(status_code=400, detail="compute must be > 0")
                override_compute[k] = fv

    if "egress" in payload:
        for k, v in payload["egress"].items():
            if k not in ("a", "b"):
                continue
            if v is None:
                override_egress[k] = None
            else:
                fv = float(v)
                if fv < 0:
                    raise HTTPException(status_code=400, detail="egress must be >= 0")
                override_egress[k] = fv

    compute, egress = _current()
    return {"compute_usd_per_hour": compute, "egress_usd_per_gb": egress}

@app.post("/clear")
def clear():
    override_compute["a"] = override_compute["b"] = None
    override_egress["a"] = override_egress["b"] = None
    compute, egress = _current()
    return {"compute_usd_per_hour": compute, "egress_usd_per_gb": egress}

@app.get("/metrics")
def metrics():
    _current()
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
