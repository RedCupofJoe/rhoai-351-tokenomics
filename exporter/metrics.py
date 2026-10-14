"""Prometheus collectors for per-user token showback."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge


class Metrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()
        self.tokens = Counter(
            "maas_user_token_usage",
            "Tokens attributed to a user. token_type is input, cache, or decoded.",
            ["user", "namespace", "token_type", "model"],
            registry=self.registry,
        )
        self.requests = Counter(
            "maas_user_requests",
            "Usage events accepted by the tokenomics exporter.",
            ["user", "namespace", "model", "source"],
            registry=self.registry,
        )
        self.events = Counter(
            "maas_exporter_events",
            "Usage events seen by the exporter, including duplicates and empties.",
            ["source", "result"],
            registry=self.registry,
        )
        self.poll_errors = Counter(
            "maas_exporter_poll_errors",
            "Failed polls of an upstream usage source.",
            ["source"],
            registry=self.registry,
        )
        self.last_success = Gauge(
            "maas_exporter_last_success_timestamp_seconds",
            "Unix time of the last successful poll or accepted push for a source.",
            ["source"],
            registry=self.registry,
        )
        self.build_info = Gauge(
            "maas_tokenomics_build_info",
            "Build information for the tokenomics exporter.",
            ["version"],
            registry=self.registry,
        )
