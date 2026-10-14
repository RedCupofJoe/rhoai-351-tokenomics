"""Background pollers. Each source is skipped until it is configured."""

from __future__ import annotations

import logging
import threading
import time

from .config import Config
from .record import Recorder
from .sources.litellm import LiteLLMSpendSource
from .sources.mlflow import MLflowSource
from .sources.pod_logs import PodLogSource

LOG = logging.getLogger(__name__)


def start_pollers(config: Config, recorder: Recorder, stop: threading.Event) -> list[threading.Thread]:
    threads: list[threading.Thread] = []
    jobs: list[tuple[str, object]] = []
    if config.mlflow_enabled():
        jobs.append(("mlflow", MLflowSource(config, recorder.checkpoint)))
    else:
        LOG.info("MLflow polling is off. Set MLFLOW_TRACKING_URI to read traces.")
    if config.litellm_enabled():
        jobs.append(("litellm", LiteLLMSpendSource(config, recorder.checkpoint)))
    else:
        LOG.info("LiteLLM spend polling is off. Set LITELLM_BASE_URL to read spend logs.")
    if config.pod_logs_enabled():
        jobs.append(("podlog", PodLogSource(config, recorder.checkpoint)))
    else:
        LOG.info("Pod log tailing is off. Push ingest on /v1/usage stays available.")
    for name, source in jobs:
        thread = threading.Thread(
            target=_loop,
            name=f"poll-{name}",
            args=(name, source, recorder, config, stop),
            daemon=True,
        )
        thread.start()
        threads.append(thread)
    return threads


def _loop(name: str, source: object, recorder: Recorder, config: Config, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            events = source.poll()  # type: ignore[attr-defined]
            for event in events:
                recorder.record(event)
            if not events:
                recorder.metrics.last_success.labels(source=name).set(time.time())
        except Exception:
            LOG.exception("Poll failed for %s", name)
            recorder.metrics.poll_errors.labels(source=name).inc()
        stop.wait(config.poll_interval_seconds)
