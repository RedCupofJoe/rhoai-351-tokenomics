# RHOAI 3.5.1 MaaS tokenomics

Showback for Models-as-a-Service on Red Hat OpenShift AI 3.5.1. The gateway metric `authorized_hits` is a single combined token count. This extension records **input**, **cache**, and **decoded** tokens per user, exposes them to Prometheus, adds a Tokenomics view to RHOAI Observability, and runs a pricing calculator in its own namespace.

Nothing in this repository patches a MaaS Deployment. Optional changes, if you choose them, are listed in [MAAS_MODIFICATIONS.md](MAAS_MODIFICATIONS.md).

## How usage is collected

The exporter starts idle. It serves `/metrics` and an authenticated `POST /v1/usage` endpoint. Turn on a source only by setting its configuration:

| Source | What it reads | Cluster change |
| --- | --- | --- |
| Push ingest | LiteLLM `standard_logging_object` or an MLflow trace JSON body | None, until you point a client at the exporter |
| MLflow Tracking API | Traces that carry `mlflow.chat.tokenUsage` | Read-only HTTP. Set `MLFLOW_TRACKING_URI` |
| LiteLLM spend logs | `GET /spend/logs` | Read-only HTTP. Set `LITELLM_BASE_URL` |
| Pod logs | JSON usage lines in container logs | Off by default. Needs `APPLY_LOG_RBAC=true` |

Cache tokens that are already included in `prompt_tokens` (the OpenAI / vLLM shape, where `total_tokens = prompt_tokens + completion_tokens`) are subtracted from input. They are billed on the cache rate only. If `total_tokens` shows cache beside the prompt, the prompt is left intact.

Decoded tokens are completion / output tokens.

## Cost

Rates are stored **per token**. The pricing page also shows the same rate per 1,000,000 tokens, which is the catalog style used by MaaS FinOps and by `llm_cost.py` in the distribution-center reference:

```text
input_cost   = input_tokens   * input_rate_per_token
cache_cost   = cache_tokens   * cache_rate_per_token
decoded_cost = decoded_tokens * decoded_rate_per_token
```

`tokenomics/cost.py` keeps that formula as a pure function. A flat $/million rate matches the reference helper `flat_million_rate_costs`.

## Layout

```text
tokenomics/                 shared cost and usage parsing
exporter/                   Prometheus exporter
ui/                         FastAPI pricing calculator and static UI
optional/litellm_callback.py
deploy/phase1               namespaces, service accounts, monitoring RBAC
deploy/phase2               exporter, Service, ServiceMonitor
deploy/phase3               Grafana dashboard JSON, GrafanaDashboard, PersesDashboard
deploy/phase4               pricing UI, Service, Route
deploy/optional             log-reader Role, not applied unless requested
deploy/build                OpenShift BuildConfigs
deploy.sh
```

## Metrics

```text
maas_user_token_usage_total{user, token_type, model}
maas_user_requests_total{user, model, source}
```

`token_type` is `input`, `cache`, or `decoded`. `model` is an extra label. Sum it away when you only need the user.

User-workload monitoring scrapes the ServiceMonitor. The pricing UI reads those series from Thanos Querier. Counters reset when the exporter restarts; Prometheus and the UI treat a reset as a new baseline. A persistent checkpoint stops the process from counting the same event twice after a restart.

Showback covers usage observed between the first and last scrape in the selected span. It does not invent tokens from before the exporter existed.

## Deploy

Prerequisites: `oc` logged in, user-workload monitoring enabled, and permission to create namespaces. Phase 3 also needs the RHOAI monitoring namespace (`redhat-ods-monitoring` on self-managed OpenShift AI, `opendatahub` on Open Data Hub).

```bash
./deploy.sh 1
./scripts/build-images.sh
./deploy.sh 2 3 4
```

`./deploy.sh` alone runs all four phases and expects the images to exist. Override namespaces and images with the variables in `deploy/env.example`.

```bash
export FINOPS_NAMESPACE=maas-finops
export UI_NAMESPACE=maas-finops-ui
export OBS_NAMESPACE=redhat-ods-monitoring
export EXPORTER_IMAGE=image-registry.openshift-image-registry.svc:5000/maas-finops/maas-tokenomics-exporter:0.1.0
export UI_IMAGE=image-registry.openshift-image-registry.svc:5000/maas-finops-ui/maas-tokenomics-ui:0.1.0
```

Phase 3 creates new dashboard objects. It does not edit the existing MaaS Usage dashboard. The Grafana ConfigMap is labeled `grafana_dashboard=1`. A `GrafanaDashboard` or `PersesDashboard` is applied only when that CRD is already installed.

The Perses datasource name defaults to `data-science-prometheus-datasource`. If that store does not contain user-workload series, set `PERSES_DATASOURCE` to the datasource that can query Thanos, or use the Grafana dashboard against user-workload monitoring.

### Ingest token and admin token

```bash
oc -n maas-finops get secret maas-tokenomics-credentials -o jsonpath='{.data.ingest-token}' | base64 -d; echo
oc -n maas-finops-ui get secret maas-pricing-admin -o jsonpath='{.data.token}' | base64 -d; echo
oc -n maas-finops-ui get route maas-pricing
```

Open the route, enter the admin token, set the three rates, and pick 1h, 24h, 7d, or 30d.

### Turn on MLflow or LiteLLM reads

Edit the exporter ConfigMap and restart. This does not change the MaaS pods.

```bash
oc -n maas-finops patch configmap maas-tokenomics-exporter --type merge -p '{"data":{"MLFLOW_TRACKING_URI":"http://mlflow.models-as-a-service.svc:5000"}}'
oc -n maas-finops rollout restart deploy/maas-tokenomics-exporter
```

To attach a LiteLLM master key:

```bash
oc -n maas-finops patch secret maas-tokenomics-credentials --type merge -p '{"stringData":{"litellm-api-key":"REPLACE_ME"}}'
```

To push logs without a callback installed in MaaS, post the LiteLLM standard logging object yourself:

```bash
curl -sS -X POST "http://maas-tokenomics-exporter.maas-finops.svc:8080/v1/usage" \
  -H "Authorization: Bearer ${INGEST_TOKEN}" \
  -H "Content-Type: application/json" \
  --data @examples/litellm-usage.json
```

## Local checks

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
pytest
TOKENOMICS_DEMO=1 ADMIN_TOKEN=dev PRICING_PATH=/tmp/pricing.json python -m ui.app
```

`TOKENOMICS_DEMO` is for local UI checks. The Deployment does not set it.

## Uninstall

```bash
./deploy.sh undeploy
```

That removes the two namespaces, the monitoring binding, the Tokenomics dashboards, and the optional log-reader Role if it was created. It does not delete MaaS, the RHOAI monitoring namespace, or the existing Usage dashboard.
