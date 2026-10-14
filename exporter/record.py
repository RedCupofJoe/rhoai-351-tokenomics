"""Apply a usage event to the checkpoint and the Prometheus counters."""

from __future__ import annotations

import threading
import time

from tokenomics.usage import TOKEN_TYPES, UsageEvent

from .checkpoint import Checkpoint
from .metrics import Metrics


class Recorder:
    def __init__(self, checkpoint: Checkpoint, metrics: Metrics) -> None:
        self.checkpoint = checkpoint
        self.metrics = metrics
        self._lock = threading.Lock()

    def record(self, event: UsageEvent) -> str:
        """Record an event once. Returns accepted, duplicate, or empty."""
        if event.total_tokens() <= 0:
            self.metrics.events.labels(source=event.source, result="empty").inc()
            return "empty"
        with self._lock:
            if self.checkpoint.seen(event.event_id):
                self.metrics.events.labels(source=event.source, result="duplicate").inc()
                return "duplicate"
            for kind in TOKEN_TYPES:
                count = int(event.tokens.get(kind, 0) or 0)
                if count:
                    self.metrics.tokens.labels(
                        user=event.user,
                        namespace=event.namespace,
                        token_type=kind,
                        model=event.model,
                    ).inc(count)
            self.metrics.requests.labels(
                user=event.user,
                namespace=event.namespace,
                model=event.model,
                source=event.source,
            ).inc()
            self.metrics.events.labels(source=event.source, result="accepted").inc()
            ts = int(event.timestamp or time.time())
            self.checkpoint.mark(event.event_id, ts)
            self.metrics.last_success.labels(source=event.source).set(time.time())
            return "accepted"
