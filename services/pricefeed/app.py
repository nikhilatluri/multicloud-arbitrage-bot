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

app = FastAPI(
    title="MultiCloud Pricefeed",
    description=(
        "Real-time cloud pricing service supporting **static**, **AWS Spot**, "
        "**GCP Preemptible**, and **Azure Spot** pricing APIs. "
        "Prices are cached with a configurable TTL and expose Prometheus metrics. "
        "Runtime overrides allow in-flight price injection for testing."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

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


@app.get("/ui", include_in_schema=False)
def pricefeed_ui():
    from fastapi.responses import HTMLResponse
    html = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Pricefeed &#8212; MultiCloud Arbitrage</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#070a12;--surface:#0e1120;--surface2:#161a2c;--border:#252a42;--green:#22c55e;--amber:#f59e0b;--red:#f43f5e;--blue:#38bdf8;--text:#f1f5f9;--text-dim:#94a3b8;--text-muted:#475569;--font:'Inter',sans-serif;--mono:'JetBrains Mono',monospace;--radius:14px}
html{font-family:var(--font);background:var(--bg);color:var(--text)}body{padding:28px 32px;max-width:1100px;margin:0 auto}
.header{display:flex;align-items:center;gap:14px;margin-bottom:28px}
.logo{width:44px;height:44px;background:linear-gradient(135deg,#10b981,#0ea5e9);border-radius:12px;display:flex;align-items:center;justify-content:center;font-size:22px;box-shadow:0 0 24px rgba(16,185,129,.3)}
h1{font-size:1.25rem;font-weight:800;letter-spacing:-.4px;background:linear-gradient(135deg,#34d399,#38bdf8);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.sub{font-size:.75rem;color:var(--text-muted);margin-top:3px}
.pill{display:flex;align-items:center;gap:7px;background:var(--surface);border:1px solid var(--border);border-radius:999px;padding:6px 14px;font-size:.75rem;color:var(--text-dim);margin-left:auto}
.dot{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 7px var(--green)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}.dot{animation:pulse 2s infinite}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px;margin-bottom:22px}
.backend-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:22px;position:relative;overflow:hidden}
.bc-top{position:absolute;top:0;left:0;right:0;height:3px;background:linear-gradient(90deg,#10b981,#0ea5e9)}
.bc-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px}
.bc-name{font-size:1rem;font-weight:800;text-transform:uppercase;letter-spacing:1.2px;color:#34d399}
.source-tag{font-size:.65rem;font-weight:700;text-transform:uppercase;padding:3px 10px;border-radius:999px}
.src-static{background:rgba(56,189,248,.12);color:var(--blue);border:1px solid rgba(56,189,248,.3)}
.src-live{background:rgba(34,197,94,.12);color:var(--green);border:1px solid rgba(34,197,94,.3)}
.src-fail{background:rgba(244,63,94,.12);color:var(--red);border:1px solid rgba(244,63,94,.3)}
.mrow{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid var(--border)}
.mrow:last-child{border-bottom:none}
.mkey{font-size:.775rem;color:var(--text-dim);font-weight:500}
.mval{font-size:.85rem;font-weight:700;font-family:var(--mono);color:var(--text)}
.override-section{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:22px;margin-bottom:22px}
.section-title{font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.7px;color:var(--text-muted);margin-bottom:16px;display:flex;align-items:center;gap:8px}
.section-title::before{content:'';display:block;width:3px;height:14px;background:linear-gradient(135deg,#10b981,#0ea5e9);border-radius:99px}
.form-row{display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end}
.field{display:flex;flex-direction:column;gap:5px}
.field label{font-size:.7rem;text-transform:uppercase;letter-spacing:.6px;color:var(--text-muted);font-weight:600}
input[type=text],input[type=number]{background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:9px 13px;font-size:.82rem;color:var(--text);font-family:var(--mono);outline:none;width:160px;transition:border-color .2s}
input:focus{border-color:#10b981}
.btn{padding:10px 20px;border-radius:9px;border:1.5px solid;font-size:.8rem;font-weight:600;cursor:pointer;font-family:var(--font);transition:all .2s}
.btn-green{background:rgba(16,185,129,.12);border-color:rgba(16,185,129,.4);color:var(--green)}
.btn-green:hover{background:var(--green);border-color:var(--green);color:#fff}
.btn-ghost{background:transparent;border-color:var(--border);color:var(--text-muted)}
.btn-ghost:hover{border-color:#475569;color:var(--text-dim)}
.toast{display:none;font-size:.775rem;font-weight:500;padding:8px 14px;border-radius:6px;background:rgba(16,185,129,.12);color:var(--green);border:1px solid rgba(16,185,129,.3)}
.footer{padding-top:16px;border-top:1px solid var(--border);font-size:.72rem;color:var(--text-muted);display:flex;justify-content:space-between}
.footer a{color:#38bdf8;margin-right:14px;text-decoration:none}
</style></head>
<body>
<div class="header">
  <div class="logo">&#128178;</div>
  <div><h1>Pricefeed</h1><div class="sub">MultiCloud Arbitrage Bot &mdash; Pricing Service</div></div>
  <div class="pill"><div class="dot"></div><span id="liveLabel">Connecting&hellip;</span></div>
</div>
<div class="grid" id="backendGrid"></div>
<div class="override-section">
  <div class="section-title">Runtime Price Override</div>
  <div class="form-row" id="overrideForm"></div>
  <div style="margin-top:14px;display:flex;gap:10px;align-items:center">
    <button class="btn btn-green" onclick="applyOverride()">Apply Override</button>
    <button class="btn btn-ghost" onclick="clearOverride()">Clear All</button>
    <span class="toast" id="overrideToast"></span>
  </div>
</div>
<div class="footer">
  <div><a href="/docs">Swagger UI</a><a href="/redoc">ReDoc</a><a href="/price">Raw JSON</a><a href="/price/sources">Sources</a><a href="/metrics">Prometheus</a></div>
  <span id="footerTs">Last update: &mdash;</span>
</div>
<script>
let backends=[];
async function refresh(){
  try{
    const [pResp,sResp]=await Promise.all([fetch('/price'),fetch('/price/sources')]);
    const prices=await pResp.json();const sources=await sResp.json();
    const bk=Object.keys(prices.compute_usd_per_hour||{});
    backends=bk;
    if(document.getElementById('overrideForm').children.length===0)renderOverrideForm(bk);
    document.getElementById('backendGrid').innerHTML=bk.map((b,i)=>{
      const c=prices.compute_usd_per_hour[b];const e=prices.egress_usd_per_gb[b];
      const src=sources[b]||{};const provider=src.provider||'static';const ok=src.live_price_available;
      const age=src.cache_age_seconds!=null?src.cache_age_seconds.toFixed(0)+'s ago':'&mdash;';
      const srcCls=provider==='static'?'src-static':ok?'src-live':'src-fail';
      const srcLabel=provider==='static'?'Static':ok?'Live: '+provider:'Failed: '+provider;
      return `<div class="backend-card">
<div class="bc-top"></div>
<div class="bc-head"><span class="bc-name">Backend ${b.toUpperCase()}</span><span class="source-tag ${srcCls}">${srcLabel}</span></div>
<div class="mrow"><span class="mkey">Compute ($/hr)</span><span class="mval">$${c!=null?c.toFixed(6):'&mdash;'}</span></div>
<div class="mrow"><span class="mkey">Egress ($/GB)</span><span class="mval">$${e!=null?e.toFixed(6):'&mdash;'}</span></div>
<div class="mrow"><span class="mkey">TTL</span><span class="mval">${src.ttl_seconds!=null?src.ttl_seconds+'s':'&mdash;'}</span></div>
<div class="mrow"><span class="mkey">Last Fetch</span><span class="mval" style="font-size:.72rem">${age}</span></div>
</div>`;
    }).join('');
    document.getElementById('liveLabel').textContent='Live';
    document.getElementById('footerTs').textContent='Last update: '+new Date().toLocaleTimeString();
  }catch(e){document.getElementById('liveLabel').textContent='Error';}
}
function renderOverrideForm(bk){
  document.getElementById('overrideForm').innerHTML=bk.map(b=>`
<div class="field"><label>Backend ${b.toUpperCase()} Compute ($/hr)</label><input type="number" step="0.000001" id="oc_${b}" placeholder="e.g. 0.009"></div>
<div class="field"><label>Backend ${b.toUpperCase()} Egress ($/GB)</label><input type="number" step="0.000001" id="oe_${b}" placeholder="e.g. 0.08"></div>
`).join('');
}
async function applyOverride(){
  const compute={},egress={};
  backends.forEach(b=>{
    const cv=document.getElementById('oc_'+b)?.value;
    const ev=document.getElementById('oe_'+b)?.value;
    if(cv)compute[b]=parseFloat(cv);
    if(ev)egress[b]=parseFloat(ev);
  });
  try{
    const r=await fetch('/override',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({compute,egress})});
    const t=document.getElementById('overrideToast');
    t.textContent=r.ok?'\\u2713 Override applied':'Error: '+r.status;
    t.style.display='inline-block';setTimeout(()=>{t.style.display='none';},3500);
    refresh();
  }catch(e){}
}
async function clearOverride(){
  await fetch('/clear',{method:'POST'});
  backends.forEach(b=>{
    const ci=document.getElementById('oc_'+b);const ei=document.getElementById('oe_'+b);
    if(ci)ci.value='';if(ei)ei.value='';
  });
  refresh();
}
refresh();setInterval(refresh,5000);
</script>
</body></html>"""
    return HTMLResponse(content=html)
