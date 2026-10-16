"""Durable pricing catalog. Rates are stored per token."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from tokenomics.cost import TOKEN_TYPES


def _rates(raw: dict | None) -> dict[str, float]:
    raw = raw or {}
    return {kind: float(raw.get(kind, 0.0) or 0.0) for kind in TOKEN_TYPES}


def _provider(raw: dict) -> dict:
    name = " ".join(str(raw.get("name") or "Local MaaS").split()) or "Local MaaS"
    model = " ".join(str(raw.get("model") or "*").split()) or "*"
    return {
        "name": name[:80],
        "model": model[:200],
        "local": bool(raw.get("local")),
        "rates": _rates(raw.get("rates") if isinstance(raw.get("rates"), dict) else None),
    }


def normalize_catalog(pricing: dict) -> dict:
    """Keep one showback rate and a matrix of provider mappings.

    A catalog written before the matrix existed has only ``rates``. That
    becomes the Local MaaS row. The showback rate is the row marked local.
    """
    raw_providers = pricing.get("providers")
    if isinstance(raw_providers, list) and raw_providers:
        providers = [_provider(item) for item in raw_providers if isinstance(item, dict)]
    else:
        providers = []
    if not providers:
        providers = [
            _provider({"name": "Local MaaS", "model": "*", "local": True, "rates": pricing.get("rates")})
        ]
    chosen = next((index for index, item in enumerate(providers) if item["local"]), 0)
    for index, item in enumerate(providers):
        item["local"] = index == chosen
    return {
        "currency": str(pricing.get("currency") or "USD"),
        "rates": dict(providers[chosen]["rates"]),
        "providers": providers,
    }


def default_pricing() -> dict:
    catalog = normalize_catalog({"currency": "USD", "rates": {kind: 0.0 for kind in TOKEN_TYPES}})
    catalog["updated_at"] = None
    return catalog


class PricingStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.save(default_pricing())

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = default_pricing()
        catalog = normalize_catalog(data)
        catalog["updated_at"] = data.get("updated_at")
        return catalog

    def save(self, pricing: dict) -> dict:
        catalog = normalize_catalog(pricing)
        body = {
            "currency": catalog["currency"],
            "rates": catalog["rates"],
            "providers": catalog["providers"],
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".pricing-", suffix=".json")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(body, stream)
                stream.write("\n")
            os.replace(temporary, self.path)
        except Exception:
            if os.path.exists(temporary):
                os.unlink(temporary)
            raise
        return body
