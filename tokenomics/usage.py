"""Extract per-user input, cache, and decoded token counts.

Sources are LiteLLM ``standard_logging_object`` payloads and MLflow trace
spans that carry ``mlflow.chat.tokenUsage``. The shapes follow LiteLLM's
standard logging spec and the span attributes written by
``llm_cost.record_usage_and_cost``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

TOKEN_TYPES: tuple[str, ...] = ("input", "cache", "decoded")


@dataclass(frozen=True)
class UsageEvent:
    """One deduplicated usage observation."""

    event_id: str
    user: str
    model: str
    tokens: dict[str, int]
    source: str
    timestamp: float | None = None

    def total_tokens(self) -> int:
        return sum(int(self.tokens.get(kind, 0) or 0) for kind in TOKEN_TYPES)


def label_value(value: Any, default: str = "unknown") -> str:
    """Collapse empty identity fields into a stable Prometheus label value."""
    if value is None:
        return default
    text = str(value).replace("\n", " ").replace("\r", " ").strip()
    if not text:
        return default
    return text[:200]


def _as_int(value: Any) -> int:
    if value is None or value is False:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return max(0, int(value))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0
        try:
            return max(0, int(float(text)))
        except ValueError:
            return 0
    return 0


def _parse_maybe_json(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return value
    return value


def normalize_attributes(raw: Any) -> dict[str, Any]:
    """Accept a dict or an OpenTelemetry key/value attribute list."""
    raw = _parse_maybe_json(raw)
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        out: dict[str, Any] = {}
        for item in raw:
            if not isinstance(item, dict) or "key" not in item:
                continue
            value = item.get("value", item.get("v"))
            if isinstance(value, dict) and len(value) == 1:
                inner = next(iter(value.values()))
                value = _parse_maybe_json(inner)
            out[str(item["key"])] = _parse_maybe_json(value)
        return out
    return {}


def normalize_tags(raw: Any) -> dict[str, Any]:
    raw = _parse_maybe_json(raw)
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        out: dict[str, Any] = {}
        for item in raw:
            if isinstance(item, dict) and "key" in item:
                out[str(item["key"])] = item.get("value")
        return out
    return {}


def cache_token_count(usage: Mapping[str, Any]) -> int:
    """Read cache tokens from OpenAI-style or LiteLLM usage objects.

    ``cache_read_input_tokens`` and ``cache_creation_input_tokens`` are
    summed when both are present. ``prompt_tokens_details.cached_tokens``
    is the OpenAI subset and is not added a second time when it repeats
    the read count.
    """
    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    if not isinstance(details, dict):
        details = {}
    cached = _as_int(details.get("cached_tokens"))
    read = _as_int(
        usage.get("cache_read_input_tokens")
        if usage.get("cache_read_input_tokens") is not None
        else usage.get("cache_tokens")
        if usage.get("cache_tokens") is not None
        else usage.get("cached_tokens")
    )
    created = _as_int(usage.get("cache_creation_input_tokens"))
    if cached and not read and not created:
        return cached
    if read or created:
        if cached == read:
            return read + created
        if cached == read + created and cached:
            return cached
        return read + created
    return cached


def cache_is_subset_of_prompt(prompt: int, completion: int, cache: int, total: int | None) -> bool | None:
    """Return whether ``cache`` is already included in ``prompt``.

    OpenAI-compatible usage (vLLM, and the usual RHOAI MaaS response) reports
    ``total_tokens == prompt_tokens + completion_tokens`` while cached tokens
    are a subset of ``prompt_tokens``. Some providers add cache tokens beside
    the prompt, which shows up as ``total == prompt + completion + cache``.
    """
    if cache <= 0:
        return True
    if total is None:
        return None
    if total == prompt + completion:
        return True
    if total == prompt + completion + cache:
        return False
    return None


def split_token_counts(
    prompt: int,
    completion: int,
    cache: int,
    total: int | None = None,
    cache_included_in_prompt: bool = True,
) -> dict[str, int]:
    """Split a usage object into input, cache, and decoded counts.

    When cache tokens are a subset of the prompt, they are removed from
    ``input`` so showback does not bill those tokens twice. ``decoded`` is
    the completion / output count.
    """
    prompt = max(0, int(prompt))
    completion = max(0, int(completion))
    cache = max(0, int(cache))
    subset = cache_is_subset_of_prompt(prompt, completion, cache, total)
    if subset is None:
        subset = cache_included_in_prompt
    if cache > prompt:
        subset = False
    if subset:
        cache = min(cache, prompt)
        input_tokens = prompt - cache
    else:
        input_tokens = prompt
    return {"input": input_tokens, "cache": cache, "decoded": completion}


def _usage_dict(payload: Mapping[str, Any]) -> dict[str, Any]:
    metadata = payload.get("metadata") or {}
    if isinstance(metadata, str):
        metadata = _parse_maybe_json(metadata) or {}
    if not isinstance(metadata, dict):
        metadata = {}
    usage = _parse_maybe_json(metadata.get("usage_object")) or {}
    if not isinstance(usage, dict):
        usage = {}
    merged = dict(usage)
    for key in (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "cache_tokens",
        "cached_tokens",
    ):
        if payload.get(key) is not None and merged.get(key) is None:
            merged[key] = payload.get(key)
    if not merged and any(k in payload for k in ("prompt_tokens", "input_tokens", "completion_tokens", "output_tokens")):
        merged = dict(payload)
    return merged


def _first(mapping: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def user_from_payload(payload: Mapping[str, Any]) -> str:
    metadata = payload.get("metadata") or {}
    if isinstance(metadata, str):
        metadata = _parse_maybe_json(metadata) or {}
    if not isinstance(metadata, dict):
        metadata = {}
    value = _first(
        metadata,
        (
            "user_api_key_user_id",
            "user_api_key_end_user_id",
            "user_id",
            "user",
            "end_user",
        ),
    )
    if value is None:
        value = _first(payload, ("user", "end_user", "user_id"))
    return label_value(value)


def model_from_payload(payload: Mapping[str, Any]) -> str:
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    value = _first(payload, ("model", "model_group", "model_id"))
    if value is None and isinstance(metadata, dict):
        value = _first(metadata, ("model", "model_group"))
    return label_value(value)


def stable_event_id(payload: Mapping[str, Any], namespace: str = "") -> str:
    for key in ("id", "request_id", "trace_id", "call_id"):
        value = payload.get(key)
        if value not in (None, ""):
            ident = str(value)
            return f"{namespace}:{ident}" if namespace else ident[:512]
    raw = json.dumps(payload, sort_keys=True, default=str)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"{namespace}:{digest}" if namespace else digest


def event_from_usage_payload(
    payload: Mapping[str, Any],
    source: str,
    cache_included_in_prompt: bool = True,
    event_namespace: str = "",
) -> UsageEvent | None:
    """Build one event from a LiteLLM log, spend row, or OpenAI usage dict."""
    if not isinstance(payload, Mapping):
        return None
    usage = _usage_dict(payload)
    if not usage:
        return None
    prompt = _as_int(_first(usage, ("prompt_tokens", "input_tokens")))
    completion = _as_int(_first(usage, ("completion_tokens", "output_tokens")))
    cache = cache_token_count(usage)
    total_raw = usage.get("total_tokens")
    total = _as_int(total_raw) if total_raw is not None else None
    tokens = split_token_counts(
        prompt,
        completion,
        cache,
        total=total,
        cache_included_in_prompt=cache_included_in_prompt,
    )
    if sum(tokens.values()) == 0:
        return None
    timestamp = payload.get("endTime") or payload.get("startTime") or payload.get("timestamp")
    if isinstance(timestamp, str):
        timestamp = None
    return UsageEvent(
        event_id=stable_event_id(payload, event_namespace)[:512],
        user=user_from_payload(payload),
        model=model_from_payload(payload),
        tokens=tokens,
        source=source,
        timestamp=float(timestamp) if isinstance(timestamp, (int, float)) else None,
    )


def _span_id(span: Mapping[str, Any]) -> str | None:
    value = span.get("span_id") or span.get("spanId")
    return str(value) if value else None


def _parent_id(span: Mapping[str, Any]) -> str | None:
    value = span.get("parent_span_id") or span.get("parentSpanId") or span.get("parent_id")
    if value in (None, "", "0" * 16):
        return None
    return str(value)


def _span_usage(span: Mapping[str, Any]) -> dict[str, Any] | None:
    attributes = normalize_attributes(span.get("attributes") or span.get("span_attributes"))
    raw = attributes.get("mlflow.chat.tokenUsage")
    if raw is None:
        raw = attributes.get("mlflow.chat.token_usage")
    raw = _parse_maybe_json(raw)
    if isinstance(raw, dict) and raw:
        return raw
    return None


def _span_model(span: Mapping[str, Any]) -> str:
    attributes = normalize_attributes(span.get("attributes") or span.get("span_attributes"))
    return label_value(_first(attributes, ("mlflow.llm.model", "mlflow.trace.model", "model")))


def _span_user(span: Mapping[str, Any], tags: Mapping[str, Any]) -> str:
    attributes = normalize_attributes(span.get("attributes") or span.get("span_attributes"))
    value = _first(attributes, ("mlflow.trace.user", "mlflow.user", "user"))
    if value is None:
        value = _first(tags, ("mlflow.user", "mlflow.trace.user", "user"))
    return label_value(value)


def _selected_usage_spans(spans: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Keep outermost spans that already carry token usage.

    MLflow skips a descendant ``mlflow.chat.tokenUsage`` once an ancestor
    has the same attribute. Summing both would double-count a trace.
    """
    usage_spans = [span for span in spans if _span_usage(span)]
    if len(usage_spans) <= 1:
        return usage_spans
    by_id = {sid: span for span in spans if (sid := _span_id(span))}
    usage_ids = {sid for span in usage_spans if (sid := _span_id(span))}
    if not any(_parent_id(span) for span in usage_spans):
        totals = []
        for span in usage_spans:
            usage = _span_usage(span) or {}
            prompt = _as_int(_first(usage, ("prompt_tokens", "input_tokens")))
            completion = _as_int(_first(usage, ("completion_tokens", "output_tokens")))
            totals.append(prompt + completion)
        largest = max(totals)
        rest = sum(totals) - largest
        if rest > 0 and largest == rest:
            return [usage_spans[totals.index(largest)]]
        return usage_spans
    selected: list[Mapping[str, Any]] = []
    for span in usage_spans:
        parent = _parent_id(span)
        skip = False
        seen: set[str] = set()
        while parent and parent not in seen:
            seen.add(parent)
            if parent in usage_ids:
                skip = True
                break
            parent_span = by_id.get(parent)
            parent = _parent_id(parent_span) if parent_span else None
        if not skip:
            selected.append(span)
    return selected


def unwrap_trace(payload: Mapping[str, Any]) -> tuple[str, dict[str, Any], list[Any], dict[str, Any]]:
    body: Mapping[str, Any] = payload
    if isinstance(payload.get("trace"), dict):
        body = payload["trace"]
    info = body.get("info") if isinstance(body.get("info"), dict) else {}
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    spans = data.get("spans") or body.get("spans") or []
    tags = normalize_tags(info.get("tags") or body.get("tags") or {})
    trace_id = (
        info.get("trace_id")
        or info.get("request_id")
        or body.get("trace_id")
        or body.get("request_id")
        or ""
    )
    return str(trace_id), tags, list(spans), dict(info)


def event_from_mlflow_trace(
    payload: Mapping[str, Any],
    cache_included_in_prompt: bool = True,
) -> UsageEvent | None:
    """Collapse one MLflow trace into a single usage event."""
    trace_id, tags, spans, info = unwrap_trace(payload)
    selected = _selected_usage_spans([span for span in spans if isinstance(span, Mapping)])
    if not selected:
        return None
    totals = {"input": 0, "cache": 0, "decoded": 0}
    models: list[str] = []
    user = "unknown"
    for span in selected:
        usage = _span_usage(span) or {}
        prompt = _as_int(_first(usage, ("prompt_tokens", "input_tokens")))
        completion = _as_int(_first(usage, ("completion_tokens", "output_tokens")))
        cache = cache_token_count(usage)
        total_raw = usage.get("total_tokens")
        total = _as_int(total_raw) if total_raw is not None else None
        split = split_token_counts(
            prompt,
            completion,
            cache,
            total=total,
            cache_included_in_prompt=cache_included_in_prompt,
        )
        for kind in TOKEN_TYPES:
            totals[kind] += split[kind]
        models.append(_span_model(span))
        candidate = _span_user(span, tags)
        if user == "unknown" and candidate != "unknown":
            user = candidate
    if user == "unknown":
        user = label_value(_first(tags, ("mlflow.user", "mlflow.trace.user", "user")))
    distinct = [model for model in dict.fromkeys(models) if model != "unknown"]
    if not distinct:
        model = "unknown"
    elif len(distinct) == 1:
        model = distinct[0]
    else:
        model = "mixed"
    if sum(totals.values()) == 0:
        return None
    event_id = trace_id or stable_event_id({"spans": spans, "tags": tags})
    timestamp_ms = info.get("timestamp_ms") or payload.get("timestamp_ms")
    timestamp = float(timestamp_ms) / 1000 if isinstance(timestamp_ms, (int, float)) else None
    return UsageEvent(
        event_id=str(event_id)[:512],
        user=user,
        model=label_value(model),
        tokens=totals,
        source="mlflow",
        timestamp=timestamp,
    )


def events_from_log_line(line: str, cache_included_in_prompt: bool = True) -> list[UsageEvent]:
    """Parse one container log line. Non-JSON lines are ignored."""
    text = line.strip()
    # ``kubectl logs --timestamps`` prefixes an RFC3339 timestamp before the payload.
    if text.startswith("20") and " " in text[:40]:
        _, _, rest = text.partition(" ")
        if rest.startswith("{") or rest.startswith("["):
            text = rest
    if not text.startswith("{") and not text.startswith("["):
        brace = text.find("{")
        if brace > 0:
            text = text[brace:]
    if not text.startswith("{") and not text.startswith("["):
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(payload, list):
        events: list[UsageEvent] = []
        for item in payload:
            if isinstance(item, dict):
                event = event_from_usage_payload(item, source="podlog", cache_included_in_prompt=cache_included_in_prompt)
                if event:
                    events.append(event)
        return events
    if not isinstance(payload, dict):
        return []
    if "spans" in payload or (isinstance(payload.get("trace"), dict)) or (isinstance(payload.get("data"), dict) and "spans" in payload.get("data", {})):
        event = event_from_mlflow_trace(payload, cache_included_in_prompt=cache_included_in_prompt)
        return [event] if event else []
    standard = payload.get("standard_logging_object")
    if isinstance(standard, dict):
        payload = standard
    event = event_from_usage_payload(payload, source="podlog", cache_included_in_prompt=cache_included_in_prompt)
    return [event] if event else []
