"""Read-only MLflow Tracing search.

Uses the MLflow tracking REST API. The exporter never writes traces.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from tokenomics.usage import UsageEvent, event_from_mlflow_trace

from ..checkpoint import Checkpoint
from ..config import Config
from ..http_json import request_json

LOG = logging.getLogger(__name__)

Fetch = Callable[..., Any]


class MLflowSource:
    def __init__(self, config: Config, checkpoint: Checkpoint, fetch: Fetch = request_json) -> None:
        self.config = config
        self.checkpoint = checkpoint
        self.fetch = fetch

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.config.mlflow_tracking_token:
            headers["Authorization"] = f"Bearer {self.config.mlflow_tracking_token}"
        return headers

    def experiment_ids(self) -> list[str]:
        if self.config.mlflow_experiment_ids:
            return list(self.config.mlflow_experiment_ids)
        payload = self.fetch(
            f"{self.config.mlflow_tracking_uri}/api/2.0/mlflow/experiments/search",
            method="POST",
            headers=self._headers(),
            body={"max_results": 100},
            timeout=self.config.http_timeout_seconds,
        )
        experiments = (payload or {}).get("experiments") or []
        return [str(item["experiment_id"]) for item in experiments if item.get("experiment_id") is not None]

    def poll(self) -> list[UsageEvent]:
        watermark = int(self.checkpoint.get_state("mlflow_watermark_ms", "0") or "0")
        experiment_ids = self.experiment_ids()
        if not experiment_ids:
            return []
        events: list[UsageEvent] = []
        committed = watermark
        page_token = ""
        fetched = 0
        stop = False
        while fetched < self.config.max_events_per_poll and not stop:
            body: dict[str, Any] = {
                "experiment_ids": experiment_ids,
                "max_results": min(100, self.config.max_events_per_poll - fetched),
                "order_by": ["timestamp_ms ASC"],
            }
            if watermark:
                body["filter"] = f"timestamp_ms >= {max(0, watermark - 1000)}"
            if page_token:
                body["page_token"] = page_token
            payload = self.fetch(
                f"{self.config.mlflow_tracking_uri}/api/2.0/mlflow/traces/search",
                method="POST",
                headers=self._headers(),
                body=body,
                timeout=self.config.http_timeout_seconds,
            ) or {}
            traces = payload.get("traces") or []
            for summary in traces:
                fetched += 1
                timestamp_ms = int(summary.get("timestamp_ms") or 0)
                trace = summary
                has_spans = bool(summary.get("spans") or (isinstance(summary.get("data"), dict) and summary["data"].get("spans")))
                if not has_spans:
                    if not (summary.get("request_id") or summary.get("trace_id")):
                        if timestamp_ms:
                            committed = max(committed, timestamp_ms)
                        continue
                    trace = self._hydrate(summary)
                    if trace is None:
                        stop = True
                        break
                event = event_from_mlflow_trace(trace, self.config.cache_included_in_prompt)
                if event:
                    events.append(event)
                if timestamp_ms:
                    committed = max(committed, timestamp_ms)
            page_token = payload.get("next_page_token") or ""
            if not page_token or not traces:
                break
        if committed:
            self.checkpoint.set_state("mlflow_watermark_ms", str(committed))
        return events

    def _hydrate(self, summary: dict[str, Any]) -> dict[str, Any] | None:
        request_id = summary.get("request_id") or summary.get("trace_id")
        if not request_id:
            return None
        try:
            payload = self.fetch(
                f"{self.config.mlflow_tracking_uri}/api/2.0/mlflow/traces/get?request_id={request_id}",
                headers=self._headers(),
                timeout=self.config.http_timeout_seconds,
            )
        except Exception:
            LOG.warning("MLflow trace hydrate failed for %s", request_id, exc_info=True)
            return None
        if isinstance(payload, dict) and isinstance(payload.get("trace"), dict):
            trace = payload["trace"]
            trace.setdefault("timestamp_ms", summary.get("timestamp_ms"))
            return trace
        return payload if isinstance(payload, dict) else None
