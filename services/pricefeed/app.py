import os
import sys
import time
from typing import Any, Dict, Optional, Tuple

from fastapi import FastAPI, HTTPException, Response
from prometheus_client import Gauge, generate_latest, CONTENT_TYPE_LATEST

# Add shared config to path — tries Docker layout (/app/shared) then local dev (../shared)
_this_dir = os.path.dirname(os.path.abspath(__file__))
for _p in [os.path.join(_this_dir, "shared"), os.path.join(_this_dir, "..", "shared")]:
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)
        break
from config import BackendConfig, load_backends  # noqa: E402

# Optional AWS SDK — only imported when a backend uses provider=aws-spot
try:
    import boto3  # type: ignore
    _BOTO3_AVAILABLE = True
except ImportError:
    _BOTO3_AVAILABLE = False

# requests for GCP and Azure REST price APIs
try:
    import requests as _requests  # type: ignore
    _REQUESTS_AVAILABLE = True
except ImportError:
    _REQUESTS_AVAILABLE = False

app = FastAPI(title="pricefeed")

# --- Load backend registry ---
BACKENDS: Dict[str, BackendConfig] = load_backends()

# --- Base prices from registry ---
base_compute: Dict[str, float] = {k: v.compute_usd_per_hour for k, v in BACKENDS.items()}
base_egress: Dict[str, float] = {k: v.egress_usd_per_gb for k, v in BACKENDS.items()}

# --- Runtime overrides ---
override_compute: Dict[str, Optional[float]] = {k: None for k in BACKENDS}
override_egress: Dict[str, Optional[float]] = {k: None for k in BACKENDS}

# --- Phase 3A: live pricing config ---
LIVE_PRICE_TTL_SECONDS = int(os.getenv("LIVE_PRICE_TTL_SECONDS", "300"))


def _provider(backend: str) -> str:
    """Return pricing provider for a backend. Default: static."""
    return os.getenv(f"PRICING_PROVIDER_{backend.upper()}", "static").lower()


# --- Live price cache per backend ---
# { backend: { "compute": float, "egress": float, "ts": float, "ok": bool, "source": str } }
_live_cache: Dict[str, dict] = {}


def _fetch_aws_spot(backend: str) -> Tuple[Optional[float], Optional[float]]:
    """Fetch EC2 spot price for the backend's configured instance type and region."""
    if not _BOTO3_AVAILABLE:
        return None, None
    region = os.getenv(f"AWS_REGION_{backend.upper()}", "us-east-1")
    instance_type = os.getenv(f"AWS_INSTANCE_TYPE_{backend.upper()}", "m5.xlarge")
    az = os.getenv(f"AWS_AZ_{backend.upper()}", f"{region}a")
    try:
        client = boto3.client("ec2", region_name=region)
        resp = client.describe_spot_price_history(
            InstanceTypes=[instance_type],
            AvailabilityZone=az,
            ProductDescriptions=["Linux/UNIX"],
            MaxResults=1,
        )
        items = resp.get("SpotPriceHistory", [])
        if items:
            compute = float(items[0]["SpotPrice"])
            # Egress from env (AWS doesn't expose egress via spot API)
            egress = float(os.getenv(f"EGRESS_{backend.upper()}_USD_PER_GB",
                                     str(BACKENDS[backend].egress_usd_per_gb)))
            return compute, egress
    except Exception:
        pass
    return None, None


def _fetch_gcp_preempt(backend: str) -> Tuple[Optional[float], Optional[float]]:
    """Fetch GCE preemptible on-demand price via Cloud Billing Catalog REST API."""
    if not _REQUESTS_AVAILABLE:
        return None, None
    region = os.getenv(f"GCP_REGION_{backend.upper()}", "us-central1")
    machine_type = os.getenv(f"GCP_MACHINE_TYPE_{backend.upper()}", "n1-standard-4")
    # The GCP Cloud Billing Catalog API is public (no auth required for list prices)
    url = "https://cloudbilling.googleapis.com/v1/services/6F81-5844-456A/skus"
    params = {
        "currencyCode": "USD",
        "pageSize": 50,
    }
    try:
        r = _requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        skus = r.json().get("skus", [])
        for sku in skus:
            desc = sku.get("description", "").lower()
            regions = [r2.lower() for r2 in sku.get("serviceRegions", [])]
            if "preemptible" in desc and machine_type.lower() in desc and any(region in r2 for r2 in regions):
                pricing = sku.get("pricingInfo", [{}])[0]
                expr = pricing.get("pricingExpression", {})
                units = expr.get("tieredRates", [{}])
                if units:
                    nano = units[0].get("unitPrice", {}).get("nanos", 0)
                    compute = nano / 1e9
                    egress = float(os.getenv(f"EGRESS_{backend.upper()}_USD_PER_GB",
                                             str(BACKENDS[backend].egress_usd_per_gb)))
                    return compute, egress
    except Exception:
        pass
    return None, None


def _fetch_azure_spot(backend: str) -> Tuple[Optional[float], Optional[float]]:
    """Fetch Azure spot price via public Retail Prices REST API (no auth required)."""
    if not _REQUESTS_AVAILABLE:
        return None, None
    region = os.getenv(f"AZURE_REGION_{backend.upper()}", "eastus")
    instance_type = os.getenv(f"AZURE_INSTANCE_TYPE_{backend.upper()}", "Standard_D4s_v3")
    url = "https://prices.azure.com/api/retail/prices"
    params = {
        "$filter": (
            f"armRegionName eq '{region}' "
            f"and skuName eq '{instance_type} Spot' "
            f"and priceType eq 'Consumption'"
        )
    }
    try:
        r = _requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        items = r.json().get("Items", [])
        if items:
            compute = float(items[0]["retailPrice"])
            egress = float(os.getenv(f"EGRESS_{backend.upper()}_USD_PER_GB",
                                     str(BACKENDS[backend].egress_usd_per_gb)))
            return compute, egress
    except Exception:
        pass
    return None, None


def _refresh_live_price(backend: str) -> None:
    """Attempt to refresh live price for backend; update cache with result or mark failed."""
    provider = _provider(backend)
    compute: Optional[float] = None
    egress: Optional[float] = None

    if provider == "aws-spot":
        compute, egress = _fetch_aws_spot(backend)
    elif provider == "gcp-preempt":
        compute, egress = _fetch_gcp_preempt(backend)
    elif provider == "azure-spot":
        compute, egress = _fetch_azure_spot(backend)
    # "static" or unknown: leave as None (base prices will be used)

    ok = compute is not None and egress is not None
    _live_cache[backend] = {
        "compute": compute,
        "egress": egress,
        "ts": time.time(),
        "ok": ok,
        "source": provider,
    }


def _get_live_price(backend: str) -> Tuple[Optional[float], Optional[float]]:
    """Return (compute, egress) from cache, refreshing if stale. Returns (None, None) for static."""
    provider = _provider(backend)
    if provider == "static":
        return None, None

    cached = _live_cache.get(backend)
    if cached is None or (time.time() - cached["ts"]) > LIVE_PRICE_TTL_SECONDS:
        _refresh_live_price(backend)
        cached = _live_cache.get(backend, {})

    return cached.get("compute"), cached.get("egress")


G_COMPUTE = Gauge("price_compute_usd_per_hour", "Compute cost per hour", ["backend"])
G_EGRESS = Gauge("price_egress_usd_per_gb", "Egress cost per GB", ["backend"])


def _current() -> Tuple[Dict[str, float], Dict[str, float]]:
    """Compute effective prices. Priority: manual override > live price > base price."""
    compute: Dict[str, float] = {}
    egress: Dict[str, float] = {}
    for k in BACKENDS:
        live_c, live_e = _get_live_price(k)

        if override_compute[k] is not None:
            compute[k] = override_compute[k]  # type: ignore[assignment]
        elif live_c is not None:
            compute[k] = live_c
        else:
            compute[k] = base_compute[k]

        if override_egress[k] is not None:
            egress[k] = override_egress[k]  # type: ignore[assignment]
        elif live_e is not None:
            egress[k] = live_e
        else:
            egress[k] = base_egress[k]

        G_COMPUTE.labels(backend=k).set(compute[k])
        G_EGRESS.labels(backend=k).set(egress[k])
    return compute, egress


@app.get("/price")
def price():
    compute, egress = _current()
    return {"compute_usd_per_hour": compute, "egress_usd_per_gb": egress}


@app.get("/price/sources")
def price_sources():
    """Show whether each backend uses static or live pricing, plus last fetch status."""
    result = {}
    for k in BACKENDS:
        provider = _provider(k)
        cached = _live_cache.get(k)
        result[k] = {
            "provider": provider,
            "last_fetch_ts": cached["ts"] if cached else None,
            "last_fetch_ok": cached["ok"] if cached else None,
            "cache_age_seconds": round(time.time() - cached["ts"], 1) if cached else None,
            "ttl_seconds": LIVE_PRICE_TTL_SECONDS,
            "live_price_available": cached["ok"] if cached else False,
        }
    return result


@app.post("/override")
def set_override(payload: dict):
    """
    Override per-backend prices at runtime. Set a value to null to revert to live/base.
    Example: {"compute": {"a": 0.009, "b": null}, "egress": {"a": 0.08}}
    """
    if "compute" in payload:
        for k, v in payload["compute"].items():
            if k not in BACKENDS:
                continue
            if v is None:
                override_compute[k] = None
            else:
                fv = float(v)
                if fv <= 0:
                    raise HTTPException(status_code=400, detail=f"compute price for '{k}' must be > 0")
                override_compute[k] = fv

    if "egress" in payload:
        for k, v in payload["egress"].items():
            if k not in BACKENDS:
                continue
            if v is None:
                override_egress[k] = None
            else:
                fv = float(v)
                if fv < 0:
                    raise HTTPException(status_code=400, detail=f"egress price for '{k}' must be >= 0")
                override_egress[k] = fv

    compute, egress = _current()
    return {"compute_usd_per_hour": compute, "egress_usd_per_gb": egress}


@app.post("/clear")
def clear():
    """Remove all runtime overrides; prices revert to live or base values."""
    for k in BACKENDS:
        override_compute[k] = None
        override_egress[k] = None
    compute, egress = _current()
    return {"compute_usd_per_hour": compute, "egress_usd_per_gb": egress}


@app.get("/metrics")
def metrics():
    _current()
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
