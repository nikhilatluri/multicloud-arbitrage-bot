"""
Shared backend registry loader.

All services (router, pricefeed, bot) import this module to discover N backends.
Primary source: BACKENDS_CONFIG_PATH JSON file.
Fallback: legacy TARGET_A_URL / COMPUTE_A_USD_PER_HOUR style env vars (backward compat).
"""

import json
import os
from dataclasses import dataclass
from typing import Dict, List


@dataclass
class BackendConfig:
    name: str
    url: str
    compute_usd_per_hour: float
    egress_usd_per_gb: float


def load_backends() -> Dict[str, BackendConfig]:
    """Load backend registry. Returns dict keyed by backend name (e.g. "a", "b", "c")."""
    config_path = os.getenv("BACKENDS_CONFIG_PATH", "/etc/arbitrage/backends.json")

    if os.path.exists(config_path):
        with open(config_path) as f:
            data = json.load(f)
        backends_raw = data.get("backends", {})
        if not backends_raw:
            raise ValueError(f"backends.json at {config_path} has no 'backends' entries")
        return {
            name: BackendConfig(
                name=name,
                url=cfg["url"],
                compute_usd_per_hour=float(cfg["compute_usd_per_hour"]),
                egress_usd_per_gb=float(cfg["egress_usd_per_gb"]),
            )
            for name, cfg in backends_raw.items()
        }

    # --- Backward-compat fallback: read legacy env vars ---
    # Supports TARGET_A_URL/TARGET_B_URL and COMPUTE_A_USD_PER_HOUR etc.
    # Dynamically discovers backends by scanning TARGET_<NAME>_URL env vars.
    discovered: Dict[str, BackendConfig] = {}

    # Explicit legacy 2-backend support
    target_a = os.getenv("TARGET_A_URL")
    target_b = os.getenv("TARGET_B_URL")

    if target_a:
        discovered["a"] = BackendConfig(
            name="a",
            url=target_a,
            compute_usd_per_hour=float(os.getenv("COMPUTE_A_USD_PER_HOUR", "0.010")),
            egress_usd_per_gb=float(os.getenv("EGRESS_A_USD_PER_GB", "0.090")),
        )
    if target_b:
        discovered["b"] = BackendConfig(
            name="b",
            url=target_b,
            compute_usd_per_hour=float(os.getenv("COMPUTE_B_USD_PER_HOUR", "0.008")),
            egress_usd_per_gb=float(os.getenv("EGRESS_B_USD_PER_GB", "0.085")),
        )

    # Generic scan for TARGET_<NAME>_URL patterns beyond a/b
    for key, val in os.environ.items():
        if key.startswith("TARGET_") and key.endswith("_URL"):
            name = key[len("TARGET_"):-len("_URL")].lower()
            if name not in discovered:
                discovered[name] = BackendConfig(
                    name=name,
                    url=val,
                    compute_usd_per_hour=float(
                        os.getenv(f"COMPUTE_{name.upper()}_USD_PER_HOUR", "0.010")
                    ),
                    egress_usd_per_gb=float(
                        os.getenv(f"EGRESS_{name.upper()}_USD_PER_GB", "0.090")
                    ),
                )

    if not discovered:
        # Final fallback: assume defaults for "a" and "b"
        discovered = {
            "a": BackendConfig("a", "http://localhost:8001", 0.010, 0.090),
            "b": BackendConfig("b", "http://localhost:8002", 0.008, 0.085),
        }

    return discovered


def backend_names() -> List[str]:
    """Sorted list of backend names from the registry."""
    return sorted(load_backends().keys())
