"""Optional LiteLLM proxy callback.

This file is not installed by deploy.sh. Copying it into a LiteLLM
deployment and registering the callback changes that deployment.
Read MAAS_MODIFICATIONS.md before using it.

The handler posts LiteLLM's standard_logging_object to the exporter.
The exporter extracts user id, prompt tokens, completion tokens, and
cache tokens. Prompt text is not required and should not be added here.
"""

from __future__ import annotations

import json
import os
import urllib.request

try:
    from litellm.integrations.custom_logger import CustomLogger
except ImportError:  # pragma: no cover - exercised only inside a LiteLLM image
    class CustomLogger:  # type: ignore[no-redef]
        """Stand-in so this module imports in unit tests without LiteLLM."""


class MaasTokenomicsHandler(CustomLogger):
    """POST terminal LiteLLM success payloads to the tokenomics exporter."""

    def log_success_event(self, kwargs, response_obj, start_time, end_time):  # noqa: ANN001
        payload = (kwargs or {}).get("standard_logging_object")
        if isinstance(payload, dict):
            post_usage(payload)

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):  # noqa: ANN001
        self.log_success_event(kwargs, response_obj, start_time, end_time)


def post_usage(payload: dict, opener=urllib.request.urlopen) -> None:
    url = os.environ["MAAS_TOKENOMICS_INGEST_URL"]
    token = os.environ.get("MAAS_TOKENOMICS_INGEST_TOKEN", "")
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    opener(request, timeout=5)
