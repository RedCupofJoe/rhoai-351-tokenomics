"""Metrics, health, and LiteLLM push ingest."""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from tokenomics.usage import UsageEvent, event_from_mlflow_trace, event_from_usage_payload

from .config import Config
from .record import Recorder

LOG = logging.getLogger(__name__)
MAX_BODY = 1_048_576


class ExporterHandler(BaseHTTPRequestHandler):
    config: Config
    recorder: Recorder
    registry: Any

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.info("%s - %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/metrics":
            payload = generate_latest(self.registry)
            self._send(200, payload, CONTENT_TYPE_LATEST)
            return
        if path in {"/healthz", "/readyz"}:
            ready = self.recorder.checkpoint.healthy()
            if path == "/healthz" or ready:
                self._send(200 if ready or path == "/healthz" else 503, b"ok\n", "text/plain")
                return
            self._send(503, b"checkpoint unavailable\n", "text/plain")
            return
        self._send(404, b"not found\n", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path not in {"/v1/usage", "/v1/usage/batch"}:
            self._send(404, b"not found\n", "text/plain")
            return
        if not self._authorized():
            self._send(401, b"unauthorized\n", "text/plain")
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0 or length > MAX_BODY:
            self._send(413, b"body required and must be under 1MiB\n", "text/plain")
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(400, b"invalid json\n", "text/plain")
            return
        items = payload if isinstance(payload, list) else [payload]
        if path == "/v1/usage" and isinstance(payload, dict) and isinstance(payload.get("events"), list):
            items = payload["events"]
        accepted = 0
        duplicates = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            event = _event_from_push(item, self.config.cache_included_in_prompt)
            if event is None:
                continue
            result = self.recorder.record(event)
            if result == "accepted":
                accepted += 1
            elif result == "duplicate":
                duplicates += 1
        body = json.dumps({"accepted": accepted, "duplicates": duplicates}).encode("utf-8")
        self._send(202, body, "application/json")

    def _authorized(self) -> bool:
        expected = self.config.ingest_token
        if not expected:
            return False
        header = self.headers.get("Authorization", "")
        supplied = self.headers.get("X-Ingest-Token", "")
        if header.lower().startswith("bearer "):
            supplied = header.split(" ", 1)[1].strip()
        return _constant_compare(supplied, expected)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _constant_compare(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _unwrap_log_forwarder(item: dict[str, Any]) -> dict[str, Any]:
    """Turn a Cluster Logging or Vector record into a usage payload.

    The collector posts its own envelope. The token counts stay in the
    original JSON log line, under ``message`` or ``log``. The pod namespace
    becomes the OpenShift project when the line does not already name one.
    """
    message = item.get("message")
    if not isinstance(message, str):
        message = item.get("log")
    if not isinstance(message, str):
        return item
    text = message.strip()
    if not text.startswith("{") and not text.startswith("["):
        return item
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return item
    if not isinstance(parsed, dict):
        return item
    kube = item.get("kubernetes") if isinstance(item.get("kubernetes"), dict) else {}
    namespace = str(kube.get("namespace_name") or kube.get("namespace") or "").strip()
    if namespace and not any(parsed.get(key) for key in ("namespace", "openshift_project", "project")):
        parsed = dict(parsed)
        parsed["namespace"] = namespace
    return parsed


def _event_from_push(item: dict[str, Any], cache_included_in_prompt: bool) -> UsageEvent | None:
    item = _unwrap_log_forwarder(item)
    standard = item.get("standard_logging_object")
    if isinstance(standard, dict):
        item = standard
    if "spans" in item or isinstance(item.get("trace"), dict) or (
        isinstance(item.get("data"), dict) and "spans" in item["data"]
    ):
        return event_from_mlflow_trace(item, cache_included_in_prompt=cache_included_in_prompt)
    return event_from_usage_payload(item, source="push", cache_included_in_prompt=cache_included_in_prompt)


def make_server(config: Config, recorder: Recorder, registry: Any) -> ThreadingHTTPServer:
    handler = type(
        "BoundExporterHandler",
        (ExporterHandler,),
        {"config": config, "recorder": recorder, "registry": registry},
    )
    server = ThreadingHTTPServer((config.listen_host, config.listen_port), handler)
    server.daemon_threads = True
    return server
