"""Optional read-only tail of pod logs. Disabled unless ENABLE_POD_LOGS=true.

Requires the optional Role in the MaaS namespace. deploy.sh does not create
that Role unless APPLY_LOG_RBAC=true. See MAAS_MODIFICATIONS.md.
"""

from __future__ import annotations

import logging
import os
import ssl
import urllib.parse
import urllib.request
from typing import Any

from tokenomics.usage import UsageEvent, events_from_log_line

from ..checkpoint import Checkpoint
from ..config import Config

LOG = logging.getLogger(__name__)


class PodLogSource:
    def __init__(self, config: Config, checkpoint: Checkpoint, fetch=None) -> None:
        self.config = config
        self.checkpoint = checkpoint
        self.fetch = fetch or self._fetch

    def poll(self) -> list[UsageEvent]:
        events: list[UsageEvent] = []
        for namespace in self.config.pod_log_namespaces:
            pods = self.fetch("GET", f"/api/v1/namespaces/{namespace}/pods", None)
            for pod in (pods or {}).get("items") or []:
                meta = pod.get("metadata") or {}
                name = meta.get("name")
                if not name or not _matches(pod, self.config.pod_log_label_selector):
                    continue
                containers = [self.config.pod_log_container] if self.config.pod_log_container else _containers(pod)
                for container in containers:
                    events.extend(self._read_container(namespace, name, container))
                    if len(events) >= self.config.max_events_per_poll:
                        return events
        return events

    def _read_container(self, namespace: str, pod: str, container: str) -> list[UsageEvent]:
        key = f"podlog:{namespace}/{pod}/{container}"
        since = self.checkpoint.get_state(key)
        query = {"timestamps": "true", "tailLines": "200"}
        if container:
            query["container"] = container
        if since:
            query["sinceTime"] = since
        path = f"/api/v1/namespaces/{namespace}/pods/{pod}/log?{urllib.parse.urlencode(query)}"
        body = self.fetch("GET", path, None, raw=True)
        if not isinstance(body, str) or not body.strip():
            return []
        events: list[UsageEvent] = []
        last_stamp = since
        for line in body.splitlines():
            stamp, _payload = _split_stamp(line)
            if stamp:
                last_stamp = stamp
            events.extend(
                events_from_log_line(line, cache_included_in_prompt=self.config.cache_included_in_prompt)
            )
        if last_stamp:
            self.checkpoint.set_state(key, last_stamp)
        return events

    def _fetch(self, method: str, path: str, body: Any, raw: bool = False) -> Any:
        host = os.environ["KUBERNETES_SERVICE_HOST"]
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
        ca = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
        token = open(token_path, encoding="utf-8").read().strip()
        request = urllib.request.Request(
            f"https://{host}:{port}{path}",
            method=method,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        context = ssl.create_default_context(cafile=ca)
        with urllib.request.urlopen(request, timeout=self.config.http_timeout_seconds, context=context) as response:
            data = response.read()
        if raw:
            return data.decode("utf-8", errors="replace")
        import json

        return json.loads(data.decode("utf-8"))


def _containers(pod: dict[str, Any]) -> list[str]:
    spec = pod.get("spec") or {}
    return [item.get("name", "") for item in spec.get("containers") or [] if item.get("name")]


def _matches(pod: dict[str, Any], selector: str) -> bool:
    if not selector:
        return True
    labels = (pod.get("metadata") or {}).get("labels") or {}
    for part in selector.split(","):
        if not part.strip():
            continue
        if "=" not in part:
            return False
        key, value = part.split("=", 1)
        if labels.get(key.strip()) != value.strip():
            return False
    return True


def _split_stamp(line: str) -> tuple[str | None, str]:
    if line.startswith("20") and " " in line[:40]:
        stamp, _, rest = line.partition(" ")
        return stamp, rest
    return None, line
