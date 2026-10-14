import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

from prometheus_client import generate_latest

from exporter.checkpoint import Checkpoint
from exporter.config import Config
from exporter.metrics import Metrics
from exporter.record import Recorder
from exporter.server import make_server
from exporter.sources.litellm import LiteLLMSpendSource
from exporter.sources.mlflow import MLflowSource
from tokenomics.usage import UsageEvent


def test_checkpoint_dedup_and_restart(tmp_path: Path) -> None:
    path = tmp_path / "checkpoint.db"
    first = Checkpoint(path)
    metrics = Metrics()
    recorder = Recorder(first, metrics)
    event = UsageEvent("evt-1", "alice", "gemma-4", {"input": 5, "cache": 1, "decoded": 2}, "push")
    assert recorder.record(event) == "accepted"
    assert recorder.record(event) == "duplicate"
    first.close()

    second = Checkpoint(path)
    again = Metrics()
    restarted = Recorder(second, again)
    assert restarted.record(event) == "duplicate"
    second.close()


def test_metrics_exposition_uses_requested_labels(tmp_path: Path) -> None:
    metrics = Metrics()
    recorder = Recorder(Checkpoint(tmp_path / "metrics.db"), metrics)
    recorder.record(UsageEvent("evt-2", "alice", "gemma-4", {"input": 5, "cache": 0, "decoded": 2}, "push"))
    body = generate_latest(metrics.registry).decode()
    assert 'maas_user_token_usage_total{model="gemma-4",token_type="input",user="alice"} 5.0' in body
    assert 'maas_user_token_usage_total{model="gemma-4",token_type="decoded",user="alice"} 2.0' in body
    assert "token_type=\"cache\"" not in body


def test_mlflow_source_paginates_and_checkpoints(tmp_path: Path) -> None:
    checkpoint = Checkpoint(tmp_path / "mlflow.db")
    calls: list[str] = []

    def fetch(url, **kwargs):
        calls.append(url)
        if url.endswith("/experiments/search"):
            return {"experiments": [{"experiment_id": "1"}]}
        if url.endswith("/traces/search"):
            return {
                "traces": [
                    {
                        "request_id": "tr-1",
                        "trace_id": "tr-1",
                        "timestamp_ms": 2000,
                        "spans": [
                            {
                                "span_id": "s1",
                                "attributes": {
                                    "mlflow.chat.tokenUsage": {"input_tokens": 4, "output_tokens": 1, "total_tokens": 5},
                                    "mlflow.trace.user": "alice",
                                    "mlflow.llm.model": "gemma-4",
                                },
                            }
                        ],
                    }
                ]
            }
        raise AssertionError(url)

    config = Config.from_env()
    source = MLflowSource(
        config.__class__(**{**config.__dict__, "mlflow_tracking_uri": "http://mlflow.local", "mlflow_experiment_ids": ()}),
        checkpoint,
        fetch,
    )
    events = source.poll()
    assert len(events) == 1
    assert events[0].tokens["input"] == 4
    assert checkpoint.get_state("mlflow_watermark_ms") == "2000"
    assert any(url.endswith("/traces/search") for url in calls)
    checkpoint.close()


def test_litellm_spend_source_reads_rows(tmp_path: Path) -> None:
    checkpoint = Checkpoint(tmp_path / "lite.db")
    config = Config.from_env()
    source = LiteLLMSpendSource(
        config.__class__(**{**config.__dict__, "litellm_base_url": "http://litellm.local", "litellm_api_key": "secret"}),
        checkpoint,
        lambda url, **kwargs: [{"request_id": "r1", "user": "bob", "model": "gemma-4", "prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}],
    )
    events = source.poll()
    assert events[0].user == "bob"
    assert events[0].tokens == {"input": 3, "cache": 0, "decoded": 1}
    assert "Authorization" not in json.dumps(events[0].tokens)
    checkpoint.close()


def test_push_endpoint_rejects_missing_token_and_counts_once(tmp_path: Path) -> None:
    config = Config.from_env().__class__(
        **{
            **Config.from_env().__dict__,
            "listen_host": "127.0.0.1",
            "listen_port": 0,
            "ingest_token": "ingest-secret",
            "checkpoint_path": str(tmp_path / "push.db"),
        }
    )
    checkpoint = Checkpoint(config.checkpoint_path)
    metrics = Metrics()
    recorder = Recorder(checkpoint, metrics)
    server = make_server(config, recorder, metrics.registry)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    payload = json.dumps(
        {"id": "push-1", "user": "alice", "model": "gemma-4", "prompt_tokens": 6, "completion_tokens": 1, "total_tokens": 7}
    ).encode()
    denied = urllib.request.Request(f"http://127.0.0.1:{port}/v1/usage", data=payload, method="POST")
    try:
        urllib.request.urlopen(denied, timeout=2)
        raise AssertionError("expected 401")
    except urllib.error.HTTPError as exc:
        assert exc.code == 401
    accepted = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/usage",
        data=payload,
        method="POST",
        headers={"Authorization": "Bearer ingest-secret", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(accepted, timeout=2) as response:
        body = json.loads(response.read().decode())
    assert body == {"accepted": 1, "duplicates": 0}
    with urllib.request.urlopen(accepted, timeout=2) as response:
        body = json.loads(response.read().decode())
    assert body["duplicates"] == 1
    metrics_body = urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=2).read().decode()
    assert 'user="alice"' in metrics_body
    server.shutdown()
    checkpoint.close()
