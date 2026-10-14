"""Read token counters from in-cluster Prometheus or Thanos."""

from __future__ import annotations

import json
import ssl
import urllib.parse
import urllib.request
from pathlib import Path


class PrometheusError(RuntimeError):
    pass


class PrometheusClient:
    def __init__(self, base_url: str, token_file: str, ca_file: str, timeout: float, insecure: bool = False) -> None:
        self.base_url = base_url.rstrip("/")
        self.token_file = token_file
        self.ca_file = ca_file
        self.timeout = timeout
        self.insecure = insecure

    def query_range(self, query: str, start: float, end: float, step: int) -> list[dict]:
        params = urllib.parse.urlencode(
            {"query": query, "start": f"{start:.3f}", "end": f"{end:.3f}", "step": str(step)}
        )
        url = f"{self.base_url}/api/v1/query_range?{params}"
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {self._token()}", "Accept": "application/json"})
        context = None
        if url.startswith("https"):
            if self.insecure:
                context = ssl._create_unverified_context()
            else:
                context = ssl.create_default_context(cafile=self.ca_file if self.ca_file else None)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=context) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise PrometheusError(f"Prometheus query failed: {exc}") from exc
        if payload.get("status") != "success":
            raise PrometheusError(f"Prometheus returned {payload.get('status')}: {payload.get('error', '')}")
        result = (payload.get("data") or {}).get("result") or []
        if not isinstance(result, list):
            raise PrometheusError("Prometheus result was not a matrix")
        return result

    def _token(self) -> str:
        path = Path(self.token_file)
        if not path.is_file():
            raise PrometheusError(f"Prometheus token file {self.token_file} is missing")
        return path.read_text(encoding="utf-8").strip()
