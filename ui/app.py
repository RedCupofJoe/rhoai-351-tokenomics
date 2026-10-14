"""Pricing calculator API and static UI."""

from __future__ import annotations

import hmac
import os
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from tokenomics.cost import TOKEN_TYPES, per_million_to_per_token, per_token_to_per_million

from .pricing_store import PricingStore
from .prometheus_query import PrometheusError
from .showback import SPANS, STEPS, aggregate_series

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


def create_app(store: PricingStore, query_series, admin_token: str, demo: bool = False) -> FastAPI:
    app = FastAPI(title="MaaS Tokenomics Pricing", docs_url="/api/docs")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    def require_token(request: Request) -> None:
        if not admin_token:
            raise HTTPException(status_code=503, detail="Admin token is not configured")
        header = request.headers.get("authorization", "")
        supplied = ""
        if header.lower().startswith("bearer "):
            supplied = header.split(" ", 1)[1].strip()
        if not hmac.compare_digest(supplied.encode("utf-8"), admin_token.encode("utf-8")):
            raise HTTPException(status_code=401, detail="Unauthorized")

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok", "demo": demo}

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/spans")
    def spans(request: Request) -> dict:
        require_token(request)
        return {"spans": list(SPANS)}

    @app.get("/api/pricing")
    def get_pricing(request: Request) -> dict:
        require_token(request)
        return _public_pricing(store.load())

    @app.put("/api/pricing")
    def put_pricing(body: PricingBody, request: Request) -> dict:
        require_token(request)
        rates = body.rates.model_dump()
        if body.rate_unit == "million":
            rates = {kind: per_million_to_per_token(value) for kind, value in rates.items()}
        saved = store.save({"currency": body.currency.upper(), "rates": rates})
        return _public_pricing(saved)

    @app.get("/api/showback")
    def showback(request: Request, span: str = "24h") -> dict:
        require_token(request)
        if span not in SPANS:
            raise HTTPException(status_code=400, detail="span must be one of 1h, 24h, 7d, 30d")
        pricing = store.load()
        end = time.time()
        start = end - SPANS[span]
        try:
            series = query_series(QUERY, start, end, STEPS[span])
        except PrometheusError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        report = aggregate_series(series, pricing["rates"], pricing["currency"], span)
        report["demo"] = demo
        return report

    return app


def _public_pricing(pricing: dict) -> dict:
    rates = {kind: float(pricing["rates"].get(kind, 0.0)) for kind in TOKEN_TYPES}
    return {
        "currency": pricing["currency"],
        "rate_unit": "token",
        "rates_per_token": rates,
        "rates_per_million": {kind: per_token_to_per_million(rates[kind]) for kind in TOKEN_TYPES},
        "updated_at": pricing.get("updated_at"),
    }


def demo_series(_query: str, start: float, end: float, _step: int) -> list[dict]:
    """Fixed samples so the UI can be exercised without a cluster."""
    return [
        _series("alice", "input", "gemma-4", start, end, 1000),
        _series("alice", "cache", "gemma-4", start, end, 200),
        _series("alice", "decoded", "gemma-4", start, end, 400),
        _series("bob", "input", "gemma-4", start, end, 50),
        _series("bob", "cache", "gemma-4", start, end, 0),
        _series("bob", "decoded", "gemma-4", start, end, 25),
    ]


def _series(user: str, token_type: str, model: str, start: float, end: float, total: float) -> dict:
    return {
        "metric": {"user": user, "token_type": token_type, "model": model, "__name__": QUERY},
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
    app = create_app(store, query_series, token, demo=demo)
    uvicorn.run(app, host=os.environ.get("LISTEN_HOST", "0.0.0.0"), port=int(os.environ.get("LISTEN_PORT", "8080")))


if __name__ == "__main__":
    main()
