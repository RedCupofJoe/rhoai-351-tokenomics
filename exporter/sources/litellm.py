"""Read-only LiteLLM spend-log poller.

LiteLLM's ``GET /spend/logs`` returns rows that already contain
``prompt_tokens``, ``completion_tokens``, and a user id. Cache tokens are
read from the row's ``metadata.usage_object`` when the proxy stored them.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from tokenomics.usage import UsageEvent, event_from_usage_payload

from ..checkpoint import Checkpoint
from ..config import Config
from ..http_json import request_json

LOG = logging.getLogger(__name__)


class LiteLLMSpendSource:
    def __init__(self, config: Config, checkpoint: Checkpoint, fetch=request_json) -> None:
        self.config = config
        self.checkpoint = checkpoint
        self.fetch = fetch

    def poll(self) -> list[UsageEvent]:
        start = self.checkpoint.get_state("litellm_start_date")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if not start:
            start = today
            self.checkpoint.set_state("litellm_start_date", start)
        url = f"{self.config.litellm_base_url}/spend/logs?start_date={start}&end_date={today}"
        headers = {"Accept": "application/json"}
        if self.config.litellm_api_key:
            headers["Authorization"] = f"Bearer {self.config.litellm_api_key}"
        payload = self.fetch(url, headers=headers, timeout=self.config.http_timeout_seconds)
        rows = _rows(payload)
        events: list[UsageEvent] = []
        for row in rows[: self.config.max_events_per_poll]:
            if not isinstance(row, dict):
                continue
            event = event_from_usage_payload(
                row,
                source="litellm",
                cache_included_in_prompt=self.config.cache_included_in_prompt,
            )
            if event:
                events.append(event)
        self.checkpoint.set_state("litellm_start_date", today)
        LOG.debug("LiteLLM spend poll returned %s rows", len(rows))
        return events


def _rows(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "logs", "spend_logs", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []
