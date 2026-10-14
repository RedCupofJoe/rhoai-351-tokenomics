"""Durable pricing catalog. Rates are stored per token."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from tokenomics.cost import TOKEN_TYPES


def default_pricing() -> dict:
    return {
        "currency": "USD",
        "rates": {kind: 0.0 for kind in TOKEN_TYPES},
        "updated_at": None,
    }


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
        rates = data.get("rates") or {}
        data["rates"] = {kind: float(rates.get(kind, 0.0) or 0.0) for kind in TOKEN_TYPES}
        data["currency"] = str(data.get("currency") or "USD")
        return data

    def save(self, pricing: dict) -> dict:
        body = {
            "currency": pricing["currency"],
            "rates": {kind: float(pricing["rates"][kind]) for kind in TOKEN_TYPES},
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
