"""Process configuration. Sources stay off until an operator sets them."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return int(raw)


def _list(name: str) -> tuple[str, ...]:
    raw = os.environ.get(name, "")
    return tuple(part.strip() for part in raw.split(",") if part.strip())


@dataclass(frozen=True)
class Config:
    listen_host: str
    listen_port: int
    checkpoint_path: str
    ingest_token: str
    poll_interval_seconds: int
    cache_included_in_prompt: bool
    mlflow_tracking_uri: str
    mlflow_tracking_token: str
    mlflow_experiment_ids: tuple[str, ...]
    litellm_base_url: str
    litellm_api_key: str
    enable_pod_logs: bool
    pod_log_namespaces: tuple[str, ...]
    pod_log_label_selector: str
    pod_log_container: str
    http_timeout_seconds: float
    max_events_per_poll: int

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            listen_host=os.environ.get("LISTEN_HOST", "0.0.0.0"),
            listen_port=_int("LISTEN_PORT", 8080),
            checkpoint_path=os.environ.get("CHECKPOINT_PATH", "/data/checkpoint.db"),
            ingest_token=os.environ.get("INGEST_TOKEN", "").strip(),
            poll_interval_seconds=_int("POLL_INTERVAL_SECONDS", 30),
            cache_included_in_prompt=_bool("CACHE_INCLUDED_IN_PROMPT", True),
            mlflow_tracking_uri=os.environ.get("MLFLOW_TRACKING_URI", "").strip().rstrip("/"),
            mlflow_tracking_token=os.environ.get("MLFLOW_TRACKING_TOKEN", "").strip(),
            mlflow_experiment_ids=_list("MLFLOW_EXPERIMENT_IDS"),
            litellm_base_url=os.environ.get("LITELLM_BASE_URL", "").strip().rstrip("/"),
            litellm_api_key=os.environ.get("LITELLM_API_KEY", "").strip(),
            enable_pod_logs=_bool("ENABLE_POD_LOGS", False),
            pod_log_namespaces=_list("POD_LOG_NAMESPACES"),
            pod_log_label_selector=os.environ.get("POD_LOG_LABEL_SELECTOR", "").strip(),
            pod_log_container=os.environ.get("POD_LOG_CONTAINER", "").strip(),
            http_timeout_seconds=float(os.environ.get("HTTP_TIMEOUT_SECONDS", "15")),
            max_events_per_poll=_int("MAX_EVENTS_PER_POLL", 200),
        )

    def mlflow_enabled(self) -> bool:
        return bool(self.mlflow_tracking_uri)

    def litellm_enabled(self) -> bool:
        return bool(self.litellm_base_url)

    def pod_logs_enabled(self) -> bool:
        return self.enable_pod_logs and bool(self.pod_log_namespaces)
