"""Aggregate Prometheus counter series into per-user showback."""

from __future__ import annotations

from tokenomics.cost import TOKEN_TYPES, cost_from_totals, per_token_to_per_million

SPANS: dict[str, int] = {
    "1h": 3600,
    "24h": 86400,
    "7d": 7 * 86400,
    "30d": 30 * 86400,
}

STEPS: dict[str, int] = {
    "1h": 30,
    "24h": 120,
    "7d": 600,
    "30d": 1800,
}

# vLLM publishes prompt, cache, and generation counters per model pod.
# Cache hits are a subset of the prompt total, so input is the computed remainder.
MODEL_COUNTER_QUERIES: tuple[tuple[str, str], ...] = (
    ('kserve_vllm:prompt_tokens_by_source_total{source="local_compute"}', "input"),
    (
        'kserve_vllm:prompt_tokens_by_source_total{source=~"local_cache_hit|external_kv_transfer"}',
        "cache",
    ),
    ("kserve_vllm:generation_tokens_total", "decoded"),
)


def counter_increase(values: list[float]) -> float:
    """Reset-aware increase between scrape samples.

    The first sample is the baseline. A drop means the counter reset and the
    new value is usage since the reset. This does not extrapolate past the
    first or last scrape, so the result matches the samples Prometheus stored.
    """
    total = 0.0
    previous: float | None = None
    for value in values:
        if previous is not None:
            if value >= previous:
                total += value - previous
            else:
                total += value
        previous = value
    return total


def provider_for_model(model: str, providers: list[dict]) -> dict | None:
    """Pick the cost mapping for one usage model.

    An exact model name wins. Everything else uses the showback row, then
    any ``*`` mapping, then the first provider.
    """
    if not providers:
        return None
    folded = str(model or "").casefold()
    exact = [
        item
        for item in providers
        if str(item.get("model") or "*").strip() not in {"", "*"}
        and str(item.get("model")).casefold() == folded
    ]
    if exact:
        return exact[0]
    wildcard = [item for item in providers if str(item.get("model") or "*").strip() in {"", "*"}]
    if wildcard:
        return next((item for item in wildcard if item.get("local")), wildcard[0])
    return next((item for item in providers if item.get("local")), providers[0])


def _sum_costs(rows: list[dict]) -> dict[str, float]:
    costs = {f"{kind}_cost": 0.0 for kind in TOKEN_TYPES}
    costs["total_cost"] = 0.0
    for row in rows:
        for key in costs:
            costs[key] += float((row.get("costs") or {}).get(key, 0.0) or 0.0)
    return costs


def aggregate_series(
    series: list[dict],
    rates_per_token: dict[str, float],
    currency: str,
    span: str,
    providers: list[dict] | None = None,
) -> dict:
    """Price one row per user, OpenShift project, and model.

    ``providers`` maps each added provider to its own input, cache, and
    decoded rates. A row uses the provider whose model matches. The default
    rate covers models that do not match a specific provider.
    """
    grouped: dict[tuple[str, str, str], dict[str, float]] = {}
    for item in series:
        metric = item.get("metric") or {}
        user = str(metric.get("user") or "unknown")
        namespace = str(metric.get("project") or metric.get("namespace") or "unknown")
        model = str(metric.get("model") or "unknown")
        token_type = str(metric.get("token_type") or "")
        if token_type not in TOKEN_TYPES:
            continue
        samples = []
        for pair in item.get("values") or []:
            if len(pair) < 2:
                continue
            samples.append(float(pair[1]))
        count = counter_increase(samples)
        key = (user, namespace, model)
        bucket = grouped.setdefault(key, {kind: 0.0 for kind in TOKEN_TYPES})
        bucket[token_type] += count
    rows = []
    totals = {kind: 0 for kind in TOKEN_TYPES}
    for (user, namespace, model), counts in grouped.items():
        tokens = {kind: int(round(counts[kind])) for kind in TOKEN_TYPES}
        for kind in TOKEN_TYPES:
            totals[kind] += tokens[kind]
        chosen = provider_for_model(model, providers or [])
        mapped_rates = chosen["rates"] if chosen else rates_per_token
        costs = cost_from_totals(tokens, mapped_rates)
        rows.append(
            {
                "user": user,
                "namespace": namespace,
                "model": model,
                "provider": chosen["name"] if chosen else "Local MaaS",
                "tokens": tokens,
                "costs": costs,
            }
        )
    rows.sort(key=lambda row: (-row["costs"]["total_cost"], row["user"], row["namespace"], row["model"]))
    total_costs = _sum_costs(rows)
    return {
        "span": span,
        "currency": currency,
        "rates_per_token": {kind: float(rates_per_token.get(kind, 0.0)) for kind in TOKEN_TYPES},
        "rates_per_million": {
            kind: per_token_to_per_million(float(rates_per_token.get(kind, 0.0))) for kind in TOKEN_TYPES
        },
        "users": rows,
        "projects": sorted({row["namespace"] for row in rows}),
        "models": sorted({row["model"] for row in rows}),
        "totals": {"tokens": totals, "costs": total_costs},
    }


def series_from_model_counter(item: dict, token_type: str) -> dict:
    """Reshape one vLLM counter series into the showback metric labels."""
    metric = item.get("metric") or {}
    model = label_from_metric(metric, "model_name", "model")
    namespace = label_from_metric(metric, "namespace")
    user = label_from_metric(metric, "user", "username", default="all-callers")
    return {
        "metric": {
            "user": user,
            "namespace": namespace,
            "token_type": token_type,
            "model": model,
            "__name__": "maas_user_token_usage_total",
        },
        "values": item.get("values") or [],
    }


def label_from_metric(metric: dict, *keys: str, default: str = "unknown") -> str:
    for key in keys:
        value = metric.get(key)
        if value:
            return str(value)
    return default


def drop_empty_rows(report: dict) -> dict:
    """Remove models whose counters did not move in the span."""
    rows = [row for row in report["users"] if any(int(row["tokens"][kind]) for kind in TOKEN_TYPES)]
    totals = {kind: 0 for kind in TOKEN_TYPES}
    for row in rows:
        for kind in TOKEN_TYPES:
            totals[kind] += int(row["tokens"][kind])
    scoped = dict(report)
    scoped["users"] = rows
    scoped["projects"] = sorted({row["namespace"] for row in rows})
    scoped["models"] = sorted({row["model"] for row in rows})
    scoped["totals"] = {"tokens": totals, "costs": _sum_costs(rows)}
    return scoped


def scope_report(report: dict, viewer: str, admin: bool) -> dict:
    """Keep every project for an administrator. A user sees only their rows."""
    scoped = dict(report)
    scoped["viewer"] = viewer
    scoped["admin"] = admin
    if admin:
        scoped["scope"] = "cluster"
        return scoped
    rows = [row for row in report["users"] if row["user"] == viewer]
    totals = {kind: 0 for kind in TOKEN_TYPES}
    for row in rows:
        for kind in TOKEN_TYPES:
            totals[kind] += int(row["tokens"][kind])
    scoped["users"] = rows
    scoped["projects"] = sorted({row["namespace"] for row in rows})
    scoped["models"] = sorted({row["model"] for row in rows})
    scoped["totals"] = {"tokens": totals, "costs": _sum_costs(rows)}
    scoped["scope"] = "self"
    return scoped
