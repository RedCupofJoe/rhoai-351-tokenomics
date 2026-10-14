"""Cost math for input, cache, and decoded tokens.

Adapted from ``local-dc-agent/src/llm_cost.py`` in
https://github.com/glroland/distribution-center.

That module writes ``mlflow.llm.cost`` onto the active MLflow span from a
single dollars-per-million-token rate:

    input_cost  = input_tokens  / 1_000_000 * rate
    output_cost = output_tokens / 1_000_000 * rate

RHOAI showback needs three rates and must not require an active span, so
the same division lives here as a pure function. Rates are stored per
token. ``per_million_to_per_token`` converts a catalog price so callers
can keep thinking in dollars per million tokens.
"""

from __future__ import annotations

TOKEN_TYPES: tuple[str, ...] = ("input", "cache", "decoded")


def per_million_to_per_token(rate_per_million: float) -> float:
    """Convert a $/1,000,000-token price into a $/token price."""
    return float(rate_per_million) / 1_000_000


def per_token_to_per_million(rate_per_token: float) -> float:
    """Convert a $/token price into a $/1,000,000-token price."""
    return float(rate_per_token) * 1_000_000


def cost_from_totals(totals: dict[str, int], rates_per_token: dict[str, float]) -> dict[str, float]:
    """Return input, cache, decoded, and total cost for one set of counts.

    ``totals`` uses the keys ``input``, ``cache``, and ``decoded``.
    ``decoded`` is the completion / output count from the reference module.
    Missing keys are treated as zero. Cache is priced on its own rate so it
    is not billed again as input.
    """
    costs: dict[str, float] = {}
    for kind in TOKEN_TYPES:
        count = int(totals.get(kind, 0) or 0)
        rate = float(rates_per_token.get(kind, 0.0) or 0.0)
        costs[f"{kind}_cost"] = count * rate
    costs["total_cost"] = costs["input_cost"] + costs["cache_cost"] + costs["decoded_cost"]
    return costs


def flat_million_rate_costs(input_tokens: int, output_tokens: int, cost_per_million_tokens: float) -> dict[str, float]:
    """Match ``llm_cost.cost_from_totals`` for a single $/million rate.

    Cache is zero. Division happens before the rate multiply, in the same
    order as the reference, so a whole number of millions stays exact.
    """
    input_cost = input_tokens / 1_000_000 * cost_per_million_tokens
    decoded_cost = output_tokens / 1_000_000 * cost_per_million_tokens
    return {
        "input_cost": input_cost,
        "cache_cost": 0.0,
        "decoded_cost": decoded_cost,
        "total_cost": input_cost + decoded_cost,
    }
