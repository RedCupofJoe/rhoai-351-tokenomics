from pathlib import Path

from fastapi.testclient import TestClient

from ui.app import create_app, demo_series
from ui.maas_models import MaasModelCatalog, names_from_refs
from ui.pricing_store import PricingStore
from ui.prometheus_query import PrometheusClient, PrometheusError
from ui.showback import aggregate_series, counter_increase, provider_for_model


def test_thanos_query_uses_service_account_token_and_service_ca(tmp_path: Path, monkeypatch) -> None:
    token = tmp_path / "token"
    token.write_text("sa-token\n", encoding="utf-8")
    ca = tmp_path / "service-ca.crt"
    ca.write_text("not-a-real-ca", encoding="utf-8")
    client = PrometheusClient(
        "https://thanos-querier.openshift-monitoring.svc.cluster.local:9091",
        str(token),
        str(ca),
        timeout=5,
    )
    captured: dict = {}

    class _Response:
        def read(self) -> bytes:
            return b'{"status":"success","data":{"result":[]}}'

        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *args: object) -> bool:
            return False

    def fake_urlopen(request, timeout, context):
        captured["authorization"] = request.get_header("Authorization")
        captured["url"] = request.full_url
        captured["cafile"] = context
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr("ui.prometheus_query.ssl.create_default_context", lambda cafile=None: cafile)
    monkeypatch.setattr("ui.prometheus_query.urllib.request.urlopen", fake_urlopen)
    assert client.query_range("maas_user_token_usage_total", 1.0, 2.0, 30) == []
    assert captured["authorization"] == "Bearer sa-token"
    assert captured["cafile"] == str(ca)
    assert "/api/v1/query_range?" in captured["url"]
    missing = PrometheusClient("https://thanos-querier.openshift-monitoring.svc:9091", str(token), "", 5)
    try:
        missing.query_range("up", 1.0, 2.0, 30)
        raise AssertionError("https without a CA file must fail")
    except PrometheusError as exc:
        assert "PROMETHEUS_CA_FILE" in str(exc)


def test_counter_increase_handles_reset() -> None:
    assert counter_increase([10, 20, 5, 8]) == 18


def test_showback_prices_demo_series(tmp_path: Path) -> None:
    store = PricingStore(tmp_path / "pricing.json")
    store.save({"currency": "USD", "rates": {"input": 0.000003, "cache": 0.0000003, "decoded": 0.000015}})
    series = demo_series("maas_user_token_usage_total", 1_000.0, 2_000.0, 30)
    report = aggregate_series(series, store.load()["rates"], "USD", "24h")
    alice = next(row for row in report["users"] if row["user"] == "alice" and row["model"] == "gemma-4")
    assert alice["namespace"] == "llm-serving"
    assert alice["tokens"] == {"input": 1000, "cache": 200, "decoded": 400}
    granite = next(row for row in report["users"] if row["user"] == "alice" and row["model"] == "granite-3")
    assert granite["tokens"] == {"input": 80, "cache": 0, "decoded": 20}
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
    assert "Project" in page.text
    assert "1h" in page.text and "30d" in page.text
    assert payload["scope"] == "cluster"
    assert {row["user"] for row in payload["users"]} == {"alice", "bob"}


def test_user_sees_only_their_usage_and_cannot_set_prices(tmp_path: Path) -> None:
    client = _client(tmp_path)
    headers = {"X-Forwarded-User": "alice"}
    me = client.get("/api/me", headers=headers)
    assert me.status_code == 200
    assert me.json() == {"user": "alice", "admin": False, "method": "openshift"}
    report = client.get("/api/showback?span=24h", headers=headers)
    assert report.status_code == 200
    body = report.json()
    assert body["scope"] == "self"
    assert body["totals"]["tokens"]["input"] == 1080
    assert {row["model"] for row in body["users"]} == {"gemma-4", "granite-3"}
    assert {row["user"] for row in body["users"]} == {"alice"}
    assert "bob" not in report.text
    denied = client.put(
        "/api/pricing",
        headers=headers,
        json={"currency": "USD", "rates": {"input": 1, "cache": 0, "decoded": 0}},
    )
    assert denied.status_code == 403
    pricing = client.get("/api/pricing", headers=headers)
    assert pricing.status_code == 200
    assert pricing.json()["can_edit"] is False


def test_admin_group_can_see_the_cluster_and_set_prices(tmp_path: Path) -> None:
    client = _client(tmp_path)
    headers = {"X-Forwarded-User": "carol", "X-Forwarded-Groups": "rhods-admins"}
    report = client.get("/api/showback?span=24h", headers=headers)
    assert report.status_code == 200
    assert report.json()["admin"] is True
    assert {row["user"] for row in report.json()["users"]} == {"alice", "bob"}
    saved = client.put(
        "/api/pricing",
        headers=headers,
        json={"currency": "USD", "rates": {"input": 0.000001, "cache": 0, "decoded": 0}},
    )
    assert saved.status_code == 200
    assert saved.json()["can_edit"] is True


def test_subject_access_review_grants_admin(tmp_path: Path) -> None:
    store = PricingStore(tmp_path / "pricing.json")
    app = create_app(
        store,
        demo_series,
        admin_token="admin-token",
        demo=True,
        review_admin=lambda user, groups: user == "dave",
    )
    client = TestClient(app)
    allowed = client.get("/api/me", headers={"X-Forwarded-User": "dave"})
    assert allowed.json()["admin"] is True
    denied = client.get("/api/me", headers={"X-Forwarded-User": "erin"})
    assert denied.json()["admin"] is False


def test_bad_bearer_does_not_fall_through_to_the_user_header(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.get(
        "/api/showback",
        headers={"Authorization": "Bearer nope", "X-Forwarded-User": "alice"},
    )
    assert response.status_code == 401


def test_empty_exporter_series_fall_back_to_model_counters(tmp_path: Path) -> None:
    def query(promql: str, start: float, end: float, _step: int) -> list[dict]:
        if promql == "maas_user_token_usage_total":
            return []
        if 'source="local_compute"' in promql:
            return [
                {
                    "metric": {"model_name": "gemma-2-9b-it", "namespace": "llm", "source": "local_compute"},
                    "values": [[start, "0"], [end, "273"]],
                },
                {
                    "metric": {"model_name": "granite-4-tiny-gpu", "namespace": "llm", "source": "local_compute"},
                    "values": [[start, "0"], [end, "0"]],
                },
            ]
        if "local_cache_hit" in promql:
            return [
                {
                    "metric": {"model_name": "gemma-2-9b-it", "namespace": "llm", "source": "local_cache_hit"},
                    "values": [[start, "0"], [end, "40"]],
                }
            ]
        if promql.startswith("kserve_vllm:generation_tokens_total"):
            return [
                {
                    "metric": {"model_name": "gemma-2-9b-it", "namespace": "llm"},
                    "values": [[start, "0"], [end, "12"]],
                }
            ]
        raise AssertionError(promql)

    store = PricingStore(tmp_path / "pricing.json")
    store.save({"currency": "USD", "rates": {"input": 0.000003, "cache": 0.0000003, "decoded": 0.000015}})
    client = TestClient(create_app(store, query, admin_token="admin-token", demo=False))
    report = client.get("/api/showback?span=24h", headers={"Authorization": "Bearer admin-token"})
    assert report.status_code == 200
    body = report.json()
    assert body["attribution"] == "model"
    assert [row["model"] for row in body["users"]] == ["gemma-2-9b-it"]
    assert body["users"][0]["user"] == "all-callers"
    assert body["users"][0]["namespace"] == "llm"
    assert body["users"][0]["tokens"] == {"input": 273, "cache": 40, "decoded": 12}
    personal = client.get("/api/showback?span=24h", headers={"X-Forwarded-User": "alice"})
    assert personal.status_code == 200
    assert personal.json()["users"] == []


def test_pricing_page_has_token_pie_and_red_hat_themes() -> None:
    root = Path(__file__).resolve().parents[1]
    html = (root / "ui/static/index.html").read_text(encoding="utf-8")
    css = (root / "ui/static/styles.css").read_text(encoding="utf-8")
    script = (root / "ui/static/app.js").read_text(encoding="utf-8")
    assert 'id="token-pie"' in html
    assert "demonstration of capabilities, not a supported user interface" in html
    assert "demo-banner" in css
    assert 'id="login" class="card hidden"' in html
    assert "app.js?v=__ASSET_VERSION__" in html
    assert 'id="theme-toggle"' in html
    assert 'data-theme="dark"' in css
    assert "#ee0000" in css
    assert "#151515" in css
    assert "renderPie" in script
    assert "prefers-color-scheme: dark" in script
    assert 'const openshift = await fetch("/api/me")' in script
    assert "useBreakGlass" in script
    assert 'data-field="model-select"' in script
    assert "/api/models" in script
    assert "Other" in script


def test_legacy_pricing_file_becomes_a_local_provider(tmp_path: Path) -> None:
    path = tmp_path / "pricing.json"
    path.write_text(
        '{"currency":"USD","rates":{"input":1,"cache":2,"decoded":3},"updated_at":"2026-01-01T00:00:00Z"}\n',
        encoding="utf-8",
    )
    loaded = PricingStore(path).load()
    assert loaded["providers"][0]["name"] == "Local MaaS"
    assert loaded["providers"][0]["local"] is True
    assert loaded["rates"]["decoded"] == 3.0


def test_provider_matrix_uses_the_showback_row(tmp_path: Path) -> None:
    client = _client(tmp_path)
    headers = {"Authorization": "Bearer admin-token"}
    saved = client.put(
        "/api/pricing",
        headers=headers,
        json={
            "currency": "USD",
            "rate_unit": "million",
            "providers": [
                {
                    "name": "Local MaaS",
                    "model": "*",
                    "local": True,
                    "rates": {"input": 1, "cache": 0, "decoded": 0},
                },
                {
                    "name": "OpenAI",
                    "model": "gpt-4o",
                    "local": False,
                    "rates": {"input": 5, "cache": 1, "decoded": 15},
                },
            ],
        },
    )
    assert saved.status_code == 200
    body = saved.json()
    assert body["rates_per_token"]["input"] == 0.000001
    assert body["providers"][1]["name"] == "OpenAI"
    assert body["providers"][1]["model"] == "gpt-4o"
    assert body["providers"][1]["rates_per_million"]["decoded"] == 15
    report = client.get("/api/showback?span=24h", headers=headers)
    alice = next(row for row in report.json()["users"] if row["user"] == "alice" and row["model"] == "gemma-4")
    assert round(alice["costs"]["input_cost"], 6) == 0.001
    page = client.get("/")
    assert page.headers["cache-control"] == "no-store"
    assert "__ASSET_VERSION__" not in page.text
    assert "/static/app.js?v=" in page.text
    assert 'id="login" class="card hidden"' in page.text
    assert 'id="provider-matrix"' in page.text
    assert "Add provider" in page.text
    duplicate = client.put(
        "/api/pricing",
        headers=headers,
        json={
            "currency": "USD",
            "providers": [
                {"name": "OpenAI", "model": "gpt-4o", "local": True, "rates": {"input": 1, "cache": 0, "decoded": 0}},
                {"name": "openai", "model": "GPT-4o", "local": False, "rates": {"input": 2, "cache": 0, "decoded": 0}},
            ],
        },
    )
    assert duplicate.status_code == 400


def test_each_provider_maps_cost_for_its_model() -> None:
    providers = [
        {
            "name": "Local MaaS",
            "model": "*",
            "local": True,
            "rates": {"input": 0.000001, "cache": 0.0, "decoded": 0.0},
        },
        {
            "name": "OpenAI",
            "model": "gpt-4o",
            "local": False,
            "rates": {"input": 0.000005, "cache": 0.0, "decoded": 0.0},
        },
    ]
    assert provider_for_model("GPT-4o", providers)["name"] == "OpenAI"
    assert provider_for_model("gemma-4", providers)["name"] == "Local MaaS"
    series = [
        {
            "metric": {"user": "alice", "namespace": "llm", "token_type": "input", "model": "gpt-4o"},
            "values": [[1.0, "0"], [2.0, "1000"]],
        },
        {
            "metric": {"user": "alice", "namespace": "llm", "token_type": "input", "model": "gemma-4"},
            "values": [[1.0, "0"], [2.0, "100"]],
        },
    ]
    report = aggregate_series(series, providers[0]["rates"], "USD", "24h", providers)
    gpt = next(row for row in report["users"] if row["model"] == "gpt-4o")
    gemma = next(row for row in report["users"] if row["model"] == "gemma-4")
    assert gpt["provider"] == "OpenAI"
    assert gpt["costs"]["input_cost"] == 0.005
    assert gemma["provider"] == "Local MaaS"
    assert round(gemma["costs"]["input_cost"], 6) == 0.0001
    assert round(report["totals"]["costs"]["input_cost"], 6) == 0.0051


def test_maas_model_names_include_alias_and_served_name() -> None:
    names = names_from_refs(
        [
            {
                "spec": {"modelRef": {"kind": "LLMInferenceService", "name": "gemma-2-9b-it"}},
                "status": {"resolvedModelAlias": "publishers/llm/models/gemma-2-9b-it"},
            },
            {
                "spec": {"modelRef": {"kind": "LLMInferenceService", "name": "gpt-oss-20b"}},
                "status": {"resolvedModelAlias": "publishers/llm/models/gpt-oss-20b"},
            },
            {
                "spec": {"modelRef": {"kind": "ExternalModel", "name": "openai-gpt"}},
                "status": {"resolvedModelAlias": "publishers/llm/models/gpt-oss-20b"},
            },
        ]
    )
    assert names == [
        "gemma-2-9b-it",
        "gpt-oss-20b",
        "openai-gpt",
        "publishers/llm/models/gemma-2-9b-it",
        "publishers/llm/models/gpt-oss-20b",
    ]
    assert names_from_refs([{"spec": {"modelRef": {"name": "same"}}, "status": {"resolvedModelAlias": "same"}}]) == [
        "same"
    ]
    assert names_from_refs([]) == []


def test_model_list_is_cached_and_a_failure_stays_empty() -> None:
    calls = {"count": 0}

    def listing():
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError("api down")
        return [{"spec": {"modelRef": {"name": "gemma-2-9b-it"}}, "status": {"resolvedModelAlias": "alias"}}]

    catalog = MaasModelCatalog(listing)
    assert catalog() == []
    assert catalog() == ["alias", "gemma-2-9b-it"]
    assert catalog() == ["alias", "gemma-2-9b-it"]
    assert calls["count"] == 2


def test_signed_in_user_can_read_the_model_list(tmp_path: Path) -> None:
    store = PricingStore(tmp_path / "pricing.json")
    app = create_app(
        store,
        demo_series,
        admin_token="admin-token",
        demo=False,
        list_models=lambda: ["publishers/llm/models/gemma-2-9b-it", "gemma-2-9b-it"],
    )
    client = TestClient(app)
    assert client.get("/api/models").status_code == 401
    listed = client.get("/api/models", headers={"X-Forwarded-User": "nora"})
    assert listed.status_code == 200
    assert listed.json()["models"] == ["publishers/llm/models/gemma-2-9b-it", "gemma-2-9b-it"]
    empty = TestClient(create_app(store, demo_series, admin_token="admin-token", list_models=lambda: []))
    assert empty.get("/api/models", headers={"Authorization": "Bearer admin-token"}).json() == {"models": []}


def test_negative_price_is_rejected(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.put(
        "/api/pricing",
        headers={"Authorization": "Bearer admin-token"},
        json={"currency": "USD", "rates": {"input": -1, "cache": 0, "decoded": 0}},
    )
    assert response.status_code == 422
