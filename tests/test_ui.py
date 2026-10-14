from pathlib import Path

from fastapi.testclient import TestClient

from ui.app import create_app, demo_series
from ui.pricing_store import PricingStore
from ui.showback import aggregate_series, counter_increase


def test_counter_increase_handles_reset() -> None:
    assert counter_increase([10, 20, 5, 8]) == 18


def test_showback_prices_demo_series(tmp_path: Path) -> None:
    store = PricingStore(tmp_path / "pricing.json")
    store.save({"currency": "USD", "rates": {"input": 0.000003, "cache": 0.0000003, "decoded": 0.000015}})
    series = demo_series("maas_user_token_usage_total", 1_000.0, 2_000.0, 30)
    report = aggregate_series(series, store.load()["rates"], "USD", "24h")
    alice = next(row for row in report["users"] if row["user"] == "alice")
    assert alice["tokens"] == {"input": 1000, "cache": 200, "decoded": 400}
    assert round(alice["costs"]["input_cost"], 6) == 0.003
    assert round(alice["costs"]["cache_cost"], 6) == 0.00006
    assert round(alice["costs"]["decoded_cost"], 6) == 0.006
    assert round(alice["costs"]["total_cost"], 6) == 0.00906


def _client(tmp_path: Path) -> TestClient:
    store = PricingStore(tmp_path / "pricing.json")
    app = create_app(store, demo_series, admin_token="admin-token", demo=True)
    return TestClient(app)


def test_ui_requires_token_and_calculates_showback(tmp_path: Path) -> None:
    client = _client(tmp_path)
    assert client.get("/healthz").status_code == 200
    assert client.get("/api/pricing").status_code == 401
    headers = {"Authorization": "Bearer admin-token"}
    saved = client.put(
        "/api/pricing",
        headers=headers,
        json={
            "currency": "usd",
            "rate_unit": "million",
            "rates": {"input": 3, "cache": 0.3, "decoded": 15},
        },
    )
    assert saved.status_code == 200
    body = saved.json()
    assert body["rates_per_token"]["input"] == 0.000003
    assert body["rates_per_million"]["decoded"] == 15
    report = client.get("/api/showback?span=7d", headers=headers)
    assert report.status_code == 200
    payload = report.json()
    assert payload["span"] == "7d"
    assert payload["users"][0]["user"] == "alice"
    assert client.get("/api/showback?span=2h", headers=headers).status_code == 400
    page = client.get("/")
    assert page.status_code == 200
    assert "Cost per token" in page.text
    assert "1h" in page.text and "30d" in page.text


def test_negative_price_is_rejected(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.put(
        "/api/pricing",
        headers={"Authorization": "Bearer admin-token"},
        json={"currency": "USD", "rates": {"input": -1, "cache": 0, "decoded": 0}},
    )
    assert response.status_code == 422
