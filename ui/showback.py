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


def aggregate_series(series: list[dict], rates_per_token: dict[str, float], currency: str, span: str) -> dict:
    """Sum model series into one row per user and price that row."""
    per_user: dict[str, dict[str, float]] = {}
    models: dict[str, set[str]] = {}
    for item in series:
        metric = item.get("metric") or {}
        user = str(metric.get("user") or "unknown")
        token_type = str(metric.get("token_type") or "")
        if token_type not in TOKEN_TYPES:
            continue
        samples = []
        for pair in item.get("values") or []:
            if len(pair) < 2:
                continue
            samples.append(float(pair[1]))
        count = counter_increase(samples)
        bucket = per_user.setdefault(user, {kind: 0.0 for kind in TOKEN_TYPES})
        bucket[token_type] += count
        model = str(metric.get("model") or "")
        if model:
            models.setdefault(user, set()).add(model)
    rows = []
    totals = {kind: 0 for kind in TOKEN_TYPES}
    for user, counts in per_user.items():
        tokens = {kind: int(round(counts[kind])) for kind in TOKEN_TYPES}
        for kind in TOKEN_TYPES:
            totals[kind] += tokens[kind]
        costs = cost_from_totals(tokens, rates_per_token)
        rows.append(
            {
                "user": user,
                "tokens": tokens,
                "costs": costs,
                "models": sorted(models.get(user, [])),
            }
        )
    rows.sort(key=lambda row: (-row["costs"]["total_cost"], row["user"]))
    total_costs = cost_from_totals(totals, rates_per_token)
    return {
        "span": span,
        "currency": currency,
        "rates_per_token": {kind: float(rates_per_token.get(kind, 0.0)) for kind in TOKEN_TYPES},
        "rates_per_million": {
            kind: per_token_to_per_million(float(rates_per_token.get(kind, 0.0))) for kind in TOKEN_TYPES
        },
        "users": rows,
        "totals": {"tokens": totals, "costs": total_costs},
    }
