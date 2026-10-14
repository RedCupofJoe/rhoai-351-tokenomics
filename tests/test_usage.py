import json
from pathlib import Path

from tokenomics.cost import cost_from_totals, flat_million_rate_costs, per_million_to_per_token
from tokenomics.usage import (
    event_from_mlflow_trace,
    event_from_usage_payload,
    events_from_log_line,
    split_token_counts,
)

ROOT = Path(__file__).resolve().parents[1]


def test_flat_rate_matches_llm_cost_reference() -> None:
    costs = flat_million_rate_costs(1_500_000, 500_000, 2.5)
    assert costs["input_cost"] == 3.75
    assert costs["decoded_cost"] == 1.25
    assert costs["cache_cost"] == 0
    assert costs["total_cost"] == 5.0


def test_three_rates_do_not_bill_cache_as_input() -> None:
    rates = {
        "input": per_million_to_per_token(3),
        "cache": per_million_to_per_token(0.3),
        "decoded": per_million_to_per_token(15),
    }
    costs = cost_from_totals({"input": 1_000_000, "cache": 1_000_000, "decoded": 1_000_000}, rates)
    assert costs["input_cost"] == 3
    assert costs["cache_cost"] == 0.3
    assert costs["decoded_cost"] == 15
    assert costs["total_cost"] == 18.3


def test_openai_cache_is_removed_from_input() -> None:
    split = split_token_counts(prompt=100, completion=20, cache=40, total=120)
    assert split == {"input": 60, "cache": 40, "decoded": 20}


def test_separate_cache_is_kept_when_total_includes_it() -> None:
    split = split_token_counts(prompt=60, completion=20, cache=40, total=120)
    assert split == {"input": 60, "cache": 40, "decoded": 20}


def test_cache_larger_than_prompt_is_not_discarded() -> None:
    split = split_token_counts(prompt=0, completion=5, cache=40, total=None, cache_included_in_prompt=True)
    assert split == {"input": 0, "cache": 40, "decoded": 5}


def test_litellm_standard_logging_object() -> None:
    payload = json.loads((ROOT / "examples/litellm-usage.json").read_text(encoding="utf-8"))
    event = event_from_usage_payload(payload, source="push")
    assert event is not None
    assert event.user == "alice"
    assert event.model == "gemma-4"
    assert event.tokens == {"input": 60, "cache": 40, "decoded": 20}
    assert event.event_id == "chatcmpl-example"


def test_mlflow_trace_does_not_double_count_child_span() -> None:
    payload = json.loads((ROOT / "examples/mlflow-trace.json").read_text(encoding="utf-8"))
    event = event_from_mlflow_trace(payload)
    assert event is not None
    assert event.user == "alice"
    assert event.model == "gemma-4"
    assert event.tokens == {"input": 60, "cache": 40, "decoded": 20}
    assert event.event_id == "tr-example"
    assert event.source == "mlflow"


def test_otel_attribute_list() -> None:
    payload = {
        "trace_id": "tr-otel",
        "spans": [
            {
                "span_id": "1",
                "attributes": [
                    {
                        "key": "mlflow.chat.tokenUsage",
                        "value": {"stringValue": json.dumps({"input_tokens": 10, "output_tokens": 4, "total_tokens": 14})},
                    },
                    {"key": "mlflow.llm.model", "value": {"stringValue": "gemma-4"}},
                    {"key": "mlflow.trace.user", "value": {"stringValue": "carol"}},
                ],
            }
        ],
    }
    event = event_from_mlflow_trace(payload)
    assert event is not None
    assert event.user == "carol"
    assert event.tokens == {"input": 10, "cache": 0, "decoded": 4}


def test_pod_log_line_with_timestamp() -> None:
    line = '2026-10-13T12:00:00.000000000Z {"id":"req-1","user":"erin","model":"gemma-4","prompt_tokens":8,"completion_tokens":2,"total_tokens":10}'
    events = events_from_log_line(line)
    assert len(events) == 1
    assert events[0].user == "erin"
    assert events[0].tokens["decoded"] == 2
    assert events[0].source == "podlog"


def test_spend_log_metadata_string_carries_cache_tokens() -> None:
    payload = {
        "request_id": "spend-1",
        "user": "alice",
        "model": "gemma-4",
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "total_tokens": 120,
        "metadata": json.dumps(
            {"usage_object": {"cache_read_input_tokens": 40, "prompt_tokens_details": {"cached_tokens": 40}}}
        ),
    }
    event = event_from_usage_payload(payload, source="litellm")
    assert event is not None
    assert event.tokens == {"input": 60, "cache": 40, "decoded": 20}
