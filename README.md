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
exporter/                   Prometheus exporter and its UBI Containerfile
ui/                         FastAPI pricing calculator, static UI, and its UBI Containerfile
grafana/                    UBI Containerfile for the Grafana server image
optional/litellm_callback.py
deploy/phase1               namespaces, service accounts, monitoring RBAC
deploy/phase2               exporter, Service, ServiceMonitor
deploy/phase3               Grafana operator subscription, Grafana instance, datasource, dashboard, PersesDashboard
deploy/phase4               pricing UI, Service, Route
deploy/optional             log-reader Role, not applied unless requested
deploy/build                OpenShift BuildConfigs for the Containerfiles
deploy.sh
```

## Red Hat support

The pricing page and the exporter are customer code. They run on Red Hat OpenShift and publish a dashboard into OpenShift AI Observability. The table below is the tooling around that code.

| Tooling | Product | Role |
| --- | --- | --- |
| Namespaces, service accounts, RBAC, ConfigMaps, PVCs, Deployments, Services, Routes, NetworkPolicies | OpenShift | Runs the standalone exporter and the pricing page in `maas-finops-ui`. The exporter is not added to a MaaS pod |
| `oauth-proxy` (`registry.redhat.io/openshift4/ose-oauth-proxy-rhel9`) | OpenShift | Signs the pricing route in with the cluster identity |
| Service CA | OpenShift | TLS for the proxy and for Thanos queries |
| User-workload monitoring, `ServiceMonitor`, Prometheus Operator, Thanos Querier | OpenShift | Scrapes `maas_user_token_usage_total` and answers the pricing page |
| `BuildConfig`, `ImageStream`, internal registry | OpenShift | Builds the exporter, UI, and Grafana images from Containerfiles |
| UBI 9 (`registry.access.redhat.com/ubi9/python-312` and `registry.access.redhat.com/ubi9/ubi`) | Red Hat UBI | Base image for every image this repository builds |
| Optional pod-log Role | OpenShift API | Off unless `APPLY_LOG_RBAC=true` |
| `PersesDashboard` in the OpenShift AI monitoring namespace | OpenShift AI Observability | Tokenomics view in the OpenShift AI UI. The CRD comes from the Cluster Observability Operator |
| `cluster-prometheus-datasource` | OpenShift AI | Datasource that can read the Thanos series this exporter publishes |

These are not Red Hat OpenShift or OpenShift AI components:

| Tooling | What it is |
| --- | --- |
| Exporter and `tokenomics/` | This repository. OpenShift AI MaaS metrics (`authorized_hits`, `kserve_vllm:*`, `rhai_api_pricing_mtok`) do not carry per-user input, cache, and decoded tokens |
| Pricing UI | FastAPI and the static page in `ui/` |
| `prometheus-client` | PyPI library that writes the text format OpenShift Prometheus scrapes |
| SQLite checkpoint | `sqlite3` from the UBI Python image, stored on the exporter PVC. It is not a Red Hat database service |
| Grafana Operator (`grafana.integreatly.org`) from the Community Operators catalog, plus the Grafana server tarball in `grafana/Containerfile` | Community operator and upstream Grafana OSS. The image base is UBI. OpenShift AI Observability uses the Perses dashboard |
| MLflow Tracking API and LiteLLM spend logs | Optional readers. Both stay off until their URLs are set. OpenShift AI does not ship either as the MaaS usage API |

## Metrics

```text
maas_user_token_usage_total{user, token_type, model}
maas_user_requests_total{user, model, source}
```

`token_type` is `input`, `cache`, or `decoded`. `model` is an extra label. Sum it away when you only need the user.

User-workload monitoring scrapes the ServiceMonitor. The pricing UI reads those series from Thanos Querier. Counters reset when the exporter restarts; Prometheus and the UI treat a reset as a new baseline. A persistent checkpoint stops the process from counting the same event twice after a restart.

Showback covers usage observed between the first and last scrape in the selected span. It does not invent tokens from before the exporter existed.

## Deploy

Prerequisites: `oc` logged in as a user who can create namespaces, and user-workload monitoring enabled. Phase 3 also needs the RHOAI monitoring namespace (`redhat-ods-monitoring` on self-managed OpenShift AI, `opendatahub` on Open Data Hub).

User-workload monitoring is off on a new cluster. The exporter ServiceMonitor is ignored until this is set. `deploy.sh` does not edit `openshift-monitoring`. Enable it once, as a cluster admin:

```bash
oc -n openshift-monitoring create configmap cluster-monitoring-config \
  --from-literal=config.yaml='enableUserWorkload: true'
```

If that ConfigMap already exists, add `enableUserWorkload: true` at the top of `data.config.yaml` and leave every other key in place:

```bash
oc -n openshift-monitoring edit configmap cluster-monitoring-config
```

Or run `./scripts/enable-user-workload-monitoring.sh`, which creates the ConfigMap or sets that one key. Then wait until pods appear in `openshift-user-workload-monitoring`.

```bash
./deploy.sh 1
./scripts/build-images.sh
./deploy.sh 2 3 4
```

Phase 5 is separate. It creates the `test-project` namespace, builds Open WebUI from `openwebui/Containerfile` (UBI 9 Python), and deploys the Caravan agent plus Open WebUI. Open WebUI listens on `127.0.0.1` and the OpenShift `oauth-proxy` sidecar is the login. Set `OPENAI_API_KEY` in the environment before the first run; the key is stored in `caravan-agent-secret` and is not committed.

```bash
export OPENAI_API_KEY=...
export OAUTH_PROXY_IMAGE=registry.redhat.io/openshift4/ose-oauth-proxy-rhel9:v4.21
./deploy.sh 5
```

`./deploy.sh` alone runs all four phases and expects the images to exist. Override namespaces and images with the variables in `deploy/env.example`.

```bash
export FINOPS_NAMESPACE=maas-finops-ui
export UI_NAMESPACE=maas-finops-ui
export OBS_NAMESPACE=redhat-ods-monitoring
export EXPORTER_IMAGE=image-registry.openshift-image-registry.svc:5000/maas-finops-ui/maas-tokenomics-exporter:0.1.0
export UI_IMAGE=image-registry.openshift-image-registry.svc:5000/maas-finops-ui/maas-tokenomics-ui:0.1.0
```

The exporter Deployment stays in `FINOPS_NAMESPACE` (default `maas-finops-ui`). It is a separate Deployment. It pulls MLflow when `MLFLOW_TRACKING_URI` is set, and it accepts usage JSON on `POST /v1/usage`. OpenShift Cluster Logging can forward MaaS pod logs to that endpoint with `deploy/optional/cluster-log-forwarder.yaml`. That file is not applied by `deploy.sh`.

Phase 3 installs the Grafana operator from the Community Operators catalog (`grafana-operator`, channel `v5`) and creates a Grafana instance, a Thanos datasource, and the tokenomics dashboard in `maas-finops-ui`. It leaves the existing MaaS Usage dashboard unchanged. The Grafana instance is labeled `dashboards=grafana` and `folders=grafana`. The dashboard and datasource select `dashboards=grafana`. The Grafana process image is `grafana/Containerfile`, whose base is UBI 9; `./scripts/build-images.sh` builds it before phase 3. A copy of the dashboard ConfigMap in `openshift-config-managed` is labeled `console.openshift.io/dashboard=true` and `console.openshift.io/odc-dashboard=true`. Those labels put **Tokenomics** in the Administrator **Observe → Dashboards** menu and the Developer **Observe → Dashboards** menu. Another copy in the OpenShift AI monitoring namespace is labeled `grafana_dashboard=1`, `console.openshift.io/odc-dashboard=true`, `dashboard=true`, and `app.kubernetes.io/part-of=rhods-dashboard`. The Perses dashboard is labeled `app.opendatahub.io/dashboard=true` and `platform.opendatahub.io/part-of=dashboard`, which is what the OpenShift AI 3.5 Observability UI selects. `PersesDashboard` is applied when that CRD is already installed.

The Perses datasource name defaults to `data-science-prometheus-datasource`. If that store does not contain user-workload series, set `PERSES_DATASOURCE` to the datasource that can query Thanos, or use the Grafana dashboard against user-workload monitoring.

### Who can see usage

Open the pricing route and sign in with OpenShift. A normal user sees only rows whose `user` label matches their OpenShift username, in every project where that user has usage. The price fields are visible and cannot be saved.

An administrator sees every user and OpenShift project and can set the per-token rates. A user is an administrator when their token groups include `system:cluster-admins`, `cluster-admins`, `rhods-admins`, or `dedicated-admins`, when `ADMIN_USERS` lists them, or when a subject access review shows cluster-admin wildcard access. Override the groups with `ADMIN_GROUPS` on the pricing Deployment.

The gateway or trace must record that same OpenShift username. A different LiteLLM user id will not appear on that person's page. An administrator still sees the recorded user label and its project.

The break-glass token in `maas-pricing-admin` is also an administrator. It is for operators. Cluster users do not need it.

```bash
oc -n maas-finops-ui get secret maas-tokenomics-credentials -o jsonpath='{.data.ingest-token}' | base64 -d; echo
oc -n maas-finops-ui get secret maas-pricing-admin -o jsonpath='{.data.token}' | base64 -d; echo
oc -n maas-finops-ui get route maas-pricing
```

The route uses an `oauth-proxy` sidecar. Set `OAUTH_PROXY_IMAGE` to the tag that matches the cluster, for example `registry.redhat.io/openshift4/ose-oauth-proxy-rhel9:v4.18`.

### Turn on MLflow or LiteLLM reads

Edit the exporter ConfigMap and restart. This does not change the MaaS pods.

```bash
oc -n maas-finops-ui patch configmap maas-tokenomics-exporter --type merge -p '{"data":{"MLFLOW_TRACKING_URI":"http://mlflow.models-as-a-service.svc:5000"}}'
oc -n maas-finops-ui rollout restart deploy/maas-tokenomics-exporter
```

To attach a LiteLLM master key:

```bash
oc -n maas-finops-ui patch secret maas-tokenomics-credentials --type merge -p '{"stringData":{"litellm-api-key":"REPLACE_ME"}}'
```

To push logs without a callback installed in MaaS, post the LiteLLM standard logging object yourself:

```bash
curl -sS -X POST "http://maas-tokenomics-exporter.maas-finops-ui.svc:8080/v1/usage" \
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

`TOKENOMICS_DEMO` is for local UI checks. The Deployment does not set it. While it is set, a browser can preview one user by storing the cookie `tokenomics-demo-user` with that OpenShift username. The cookie is ignored when demo mode is off.

## Uninstall

```bash
./deploy.sh undeploy
```

That removes the two namespaces, the monitoring binding, the auth-review binding and ClusterRole, the Tokenomics dashboards, and the optional log-reader Role if it was created. It does not delete MaaS, the RHOAI monitoring namespace, or the existing Usage dashboard.
