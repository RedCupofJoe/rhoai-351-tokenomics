"""Pricing calculator API and static UI."""

from __future__ import annotations

import os
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from tokenomics.cost import TOKEN_TYPES, per_million_to_per_token, per_token_to_per_million

from .auth import DEFAULT_ADMIN_GROUPS, KubernetesAdminReview, principal_from_request
from .pricing_store import PricingStore
from .prometheus_query import PrometheusError
from .showback import (
    MODEL_COUNTER_QUERIES,
    SPANS,
    STEPS,
    aggregate_series,
    drop_empty_rows,
    scope_report,
    series_from_model_counter,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
QUERY = "maas_user_token_usage_total"


class RatesBody(BaseModel):
    input: float = Field(ge=0)
    cache: float = Field(ge=0)
    decoded: float = Field(ge=0)


class PricingBody(BaseModel):
    currency: str = Field(pattern=r"^[A-Za-z]{3}$")
    rates: RatesBody
    rate_unit: str = Field(default="token", pattern=r"^(token|million)$")


def create_app(
    store: PricingStore,
    query_series,
    admin_token: str,
    demo: bool = False,
    admin_groups: tuple[str, ...] = DEFAULT_ADMIN_GROUPS,
    admin_users: tuple[str, ...] = (),
    review_admin=None,
) -> FastAPI:
    app = FastAPI(title="MaaS Tokenomics Pricing", docs_url="/api/docs")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    def identify(request: Request):
        return principal_from_request(
            request,
            admin_token=admin_token,
            admin_groups=admin_groups,
            admin_users=admin_users,
            review_admin=review_admin,
            demo=demo,
        )

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok", "demo": demo}

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/me")
    def me(request: Request) -> dict:
        principal = identify(request)
        return {"user": principal.name, "admin": principal.admin, "method": principal.method}

    @app.get("/api/spans")
    def spans(request: Request) -> dict:
        identify(request)
        return {"spans": list(SPANS)}

    @app.get("/api/pricing")
    def get_pricing(request: Request) -> dict:
        principal = identify(request)
        return _public_pricing(store.load(), can_edit=principal.admin)

    @app.put("/api/pricing")
    def put_pricing(body: PricingBody, request: Request) -> dict:
        principal = identify(request)
        if not principal.admin:
            raise HTTPException(status_code=403, detail="Only an administrator can set prices")
        rates = body.rates.model_dump()
        if body.rate_unit == "million":
            rates = {kind: per_million_to_per_token(value) for kind, value in rates.items()}
        saved = store.save({"currency": body.currency.upper(), "rates": rates})
        return _public_pricing(saved, can_edit=True)

    @app.get("/api/showback")
    def showback(request: Request, span: str = "24h") -> dict:
        principal = identify(request)
        if span not in SPANS:
            raise HTTPException(status_code=400, detail="span must be one of 1h, 24h, 7d, 30d")
        pricing = store.load()
        end = time.time()
        start = end - SPANS[span]
        try:
            series = query_series(QUERY, start, end, STEPS[span])
            attribution = "exporter"
            if not series:
                attribution = "model"
                series = []
                for promql, token_type in MODEL_COUNTER_QUERIES:
                    for item in query_series(promql, start, end, STEPS[span]):
                        series.append(series_from_model_counter(item, token_type))
        except PrometheusError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        report = aggregate_series(series, pricing["rates"], pricing["currency"], span)
        report["attribution"] = attribution
        report = drop_empty_rows(report)
        report = scope_report(report, principal.name, principal.admin)
        report["demo"] = demo
        return report

    return app


def _public_pricing(pricing: dict, can_edit: bool) -> dict:
    rates = {kind: float(pricing["rates"].get(kind, 0.0)) for kind in TOKEN_TYPES}
    return {
        "currency": pricing["currency"],
        "rate_unit": "token",
        "can_edit": can_edit,
        "rates_per_token": rates,
        "rates_per_million": {kind: per_token_to_per_million(rates[kind]) for kind in TOKEN_TYPES},
        "updated_at": pricing.get("updated_at"),
    }


def demo_series(_query: str, start: float, end: float, _step: int) -> list[dict]:
    """Fixed samples so the UI can be exercised without a cluster."""
    return [
        _series("alice", "input", "gemma-4", "llm-serving", start, end, 1000),
        _series("alice", "cache", "gemma-4", "llm-serving", start, end, 200),
        _series("alice", "decoded", "gemma-4", "llm-serving", start, end, 400),
        _series("alice", "input", "granite-3", "llm-serving", start, end, 80),
        _series("alice", "cache", "granite-3", "llm-serving", start, end, 0),
        _series("alice", "decoded", "granite-3", "llm-serving", start, end, 20),
        _series("bob", "input", "gemma-4", "team-b", start, end, 50),
        _series("bob", "cache", "gemma-4", "team-b", start, end, 0),
        _series("bob", "decoded", "gemma-4", "team-b", start, end, 25),
    ]


def _series(
    user: str,
    token_type: str,
    model: str,
    namespace: str,
    start: float,
    end: float,
    total: float,
) -> dict:
    return {
        "metric": {
            "user": user,
            "namespace": namespace,
            "token_type": token_type,
            "model": model,
            "__name__": QUERY,
        },
        "values": [[start, "0"], [end, str(total)]],
    }


def build_query_series():
    if os.environ.get("TOKENOMICS_DEMO", "").lower() in {"1", "true", "yes"}:
        return demo_series, True
    from .prometheus_query import PrometheusClient

    client = PrometheusClient(
        base_url=os.environ.get(
            "PROMETHEUS_URL",
            "https://thanos-querier.openshift-monitoring.svc.cluster.local:9091",
        ),
        token_file=os.environ.get(
            "PROMETHEUS_TOKEN_FILE",
            "/var/run/secrets/kubernetes.io/serviceaccount/token",
        ),
        ca_file=os.environ.get("PROMETHEUS_CA_FILE", ""),
        timeout=float(os.environ.get("PROMETHEUS_TIMEOUT_SECONDS", "20")),
        insecure=os.environ.get("PROMETHEUS_TLS_INSECURE", "").lower() in {"1", "true", "yes"},
    )
    return client.query_range, False


def main() -> None:
    import uvicorn

    token = os.environ.get("ADMIN_TOKEN", "")
    token_file = os.environ.get("ADMIN_TOKEN_FILE", "")
    if not token and token_file:
        token = Path(token_file).read_text(encoding="utf-8").strip()
    store = PricingStore(os.environ.get("PRICING_PATH", "/data/pricing.json"))
    query_series, demo = build_query_series()
    if demo and store.load()["rates"]["input"] == 0:
        store.save(
            {
                "currency": "USD",
                "rates": {"input": 0.000003, "cache": 0.0000003, "decoded": 0.000015},
            }
        )
    groups = tuple(
        part.strip()
        for part in os.environ.get("ADMIN_GROUPS", ",".join(DEFAULT_ADMIN_GROUPS)).split(",")
        if part.strip()
    )
    users = tuple(part.strip() for part in os.environ.get("ADMIN_USERS", "").split(",") if part.strip())
    review = None
    review_enabled = os.environ.get("AUTH_REVIEW", "true").lower() in {"1", "true", "yes"}
    if review_enabled and os.environ.get("KUBERNETES_SERVICE_HOST"):
        review = KubernetesAdminReview()
    app = create_app(
        store,
        query_series,
        token,
        demo=demo,
        admin_groups=groups,
        admin_users=users,
        review_admin=review,
    )
    uvicorn.run(app, host=os.environ.get("LISTEN_HOST", "0.0.0.0"), port=int(os.environ.get("LISTEN_PORT", "8080")))


if __name__ == "__main__":
    main()
