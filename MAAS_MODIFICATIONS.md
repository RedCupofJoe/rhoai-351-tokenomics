# MaaS modifications

`deploy.sh` does not change Models-as-a-Service workloads, Gateway, Authorino, Limitador, llm-d, or any Deployment in `redhat-ods-applications`. Phase 3 merge-patches `DataScienceClusterInitialization` monitoring and sets `OdhDashboardConfig` `observabilityDashboard`. Phase 5 is not part of the default run. It sets `OdhDashboardConfig` `agentsCatalog` and `mcpCatalog` to true, and sets `DataScienceCluster` component `mcplifecycleoperator` to `Managed`. That is the only component management state this repository sets.

The sections below are the only optional changes. Each one is off unless you run the command yourself.

## Applied by default

These are new objects. They are not patches of existing MaaS objects.

| Phase | Objects | Namespace |
| --- | --- | --- |
| 1 | Namespace, ServiceAccount `maas-tokenomics-exporter`, ServiceAccount `maas-pricing-ui` | `maas-finops-ui` |
| 1 | ClusterRoleBinding to the existing `cluster-monitoring-view` role | cluster |
| 1 | ClusterRole `maas-tokenomics-auth-review` and a binding for the pricing UI service account. This allows `create` on `subjectaccessreviews` so the UI can tell a cluster admin from a normal user. It does not grant admin rights. | cluster |
| 2 | ConfigMap, PVC, standalone Deployment, Service, ServiceMonitor, NetworkPolicy | `maas-finops-ui` |
| 3 | Subscriptions `cluster-observability-operator`, `opentelemetry-product`, and `tempo-product` (Red Hat operators, channel `stable`) | `openshift-operators` |
| 3 | Merge patch: `DSCInitialization` `spec.monitoring` metrics (5Gi, 90d, 1 replica), `alerting: {}`, and Tempo traces (`pv`, 2160h, sample ratio 0.1) | cluster |
| 3 | Merge patch: `OdhDashboardConfig` `spec.dashboardConfig.observabilityDashboard: true` | `redhat-ods-applications` |
| 3 | Merge patch: `Dashboard` `spec.observability.enabled: true` and Perses service `data-science-perses` | cluster |
| 3 | Raise `data-science-prometheus-cluster-proxy` to 100m CPU and 128Mi memory so it can finish starting | `redhat-ods-monitoring` |
| 3 | Subscription `grafana-operator` (Community Operators, channel `v5`) | `openshift-operators` |
| 3 | ServiceAccount `maas-tokenomics-grafana`, its token Secret, and a ClusterRoleBinding to `cluster-monitoring-view` | `maas-finops-ui` and cluster |
| 3 | Grafana `maas-tokenomics`, GrafanaDatasource `thanos`, GrafanaDashboard `maas-tokenomics`, dashboard ConfigMap | `maas-finops-ui` |
| 3 | ConfigMap `maas-tokenomics-dashboard` labeled `console.openshift.io/dashboard=true` and `console.openshift.io/odc-dashboard=true` | `openshift-config-managed` (OpenShift console dashboard menus) |
| 3 | ConfigMap `maas-tokenomics-dashboard` | RHOAI monitoring namespace, default `redhat-ods-monitoring` |
| 3 | NetworkPolicy `tokenomics-perses-operator-access` so the Perses operator in `openshift-operators` can publish dashboards | same |
| 3 | `PersesDashboard` `dashboard-maas-tokenomics` and `dashboard-cost-per-token-admin`, when that CRD already exists | same |
| 4 | Service CA ConfigMap, PVC, Secrets, Deployment with an `oauth-proxy` sidecar, Service, reencrypt Route, NetworkPolicy | `maas-finops-ui` |

Phase 3 does not modify `dashboard-3-maas-usage-admin` or any other dashboard. If `redhat-ods-monitoring` is absent, phase 3 stops and tells you to set `OBS_NAMESPACE`. It will not create that namespace.

Phase 5 (`./deploy.sh 5`) merge-patches `OdhDashboardConfig` `odh-dashboard-config` so `spec.dashboardConfig.agentsCatalog` and `spec.dashboardConfig.mcpCatalog` are true. It merge-patches `DataScienceCluster` `default-dsc` so `spec.components.mcplifecycleoperator.managementState` is `Managed`, and leaves every other component state alone. It then registers the Research source on `mcp-catalog-sources` and `agent-catalog-sources` in `rhoai-model-registries` by patching ConfigMap data only, so the operator labels stay in place.

The pricing UI is not registered with the RHOAI dashboard module federation. That registration would change `rhods-dashboard` in `redhat-ods-applications`. This project uses a Route in `maas-finops-ui` instead. The Route sits behind OpenShift login. A signed-in user sees only their own usage and cannot change prices. An OpenShift AI administrator, a member of `rhods-admins` or `system:cluster-admins`, sees cluster-wide usage and can set per-token rates.

## 1. Optional: read pod logs

Not applied unless you set `APPLY_LOG_RBAC=true`.

```bash
APPLY_LOG_RBAC=true MAAS_NAMESPACE=models-as-a-service ./deploy.sh 1
```

This creates Role and RoleBinding `maas-tokenomics-log-reader` in `MAAS_NAMESPACE`. The Role can `get`, `list`, and `watch` pods and `get` pod logs. It does not add a sidecar, volume, or environment variable to any MaaS pod.

After the Role exists, turn the reader on in the exporter namespace:

```bash
oc -n maas-finops-ui patch configmap maas-tokenomics-exporter --type merge -p '{
  "data": {
    "ENABLE_POD_LOGS": "true",
    "POD_LOG_NAMESPACES": "models-as-a-service",
    "POD_LOG_LABEL_SELECTOR": "app=litellm",
    "POD_LOG_CONTAINER": ""
  }
}'
oc -n maas-finops-ui rollout restart deploy/maas-tokenomics-exporter
```

Change the label selector to the pods that already emit JSON usage lines. Leave `POD_LOG_CONTAINER` empty to read every container in those pods.

## 2. Optional: LiteLLM callback

Not applied by `deploy.sh`. Use this only if a LiteLLM proxy is already part of the MaaS path and you want it to push `standard_logging_object` to the exporter.

File to copy into the LiteLLM image or a mounted config volume:

`optional/litellm_callback.py`

Environment to add on the LiteLLM Deployment:

```text
MAAS_TOKENOMICS_INGEST_URL=http://maas-tokenomics-exporter.maas-finops-ui.svc:8080/v1/usage
MAAS_TOKENOMICS_INGEST_TOKEN=<ingest-token from maas-tokenomics-credentials>
```

LiteLLM proxy setting to add:

```yaml
litellm_settings:
  callbacks: optional.litellm_callback.MaasTokenomicsHandler
```

The module path must match where you placed the file. The callback posts the standard logging object only. It does not add prompt text.

This is a change to the LiteLLM Deployment (environment and callback setting). Review it before applying. The exporter can instead poll `GET /spend/logs` with `LITELLM_BASE_URL` and `litellm-api-key`, which does not change the LiteLLM Deployment.

## 3. Optional: point the exporter at MLflow

Not a change to MaaS if tracing is already being stored. Set `MLFLOW_TRACKING_URI` on the exporter ConfigMap, as described in the README. Add `mlflow-token` to `maas-tokenomics-credentials` when the tracking server requires a bearer token.

Enabling MLflow tracing on a gateway or model pod that does not already emit traces would be a change to that pod. This repository does not ship that patch. Traces need `mlflow.chat.tokenUsage` plus a user attribute (`mlflow.trace.user` or the `mlflow.user` tag) or the series is labeled `user="unknown"`.

## 4. vLLM usage flags

Not applied by `deploy.sh`. Run this when the model servers should return token usage, including cache details, on every completion:

```bash
LLM_NAMESPACE=llm ./scripts/enable-vllm-usage-flags.sh
```

The script adds `--enable-force-include-usage` and `--enable-prompt-tokens-details` to each `LLMInferenceService` container named `main`. It scales that model to zero, updates the template, and scales it back, so the rollout does not request a second GPU. The model is unavailable while its pod starts.

## 5. What this project will not change for you

- No sidecar is added to MaaS, gateway, or model pods. The vLLM usage-flag script above is the only change to a model template, and only when you run it.
- The only `DataScienceCluster` component management state this repository sets is `mcplifecycleoperator: Managed`, and only when phase 5 runs. Phase 3 sets `DSCInitialization` `spec.monitoring` for metrics, alerting, and traces. It does not set external metrics exporters.
- No OpenTelemetry Collector config is rewritten.
- No Limitador `TokenRateLimitPolicy` or TelemetryPolicy is edited. `authorized_hits` stays the combined gateway total.
- User-workload monitoring is required for the ServiceMonitor and is not enabled by `deploy.sh`. A cluster administrator turns it on with `./scripts/enable-user-workload-monitoring.sh`. That edits `cluster-monitoring-config` in `openshift-monitoring`.
- `deploy/optional/cluster-log-forwarder.yaml` is not applied by `deploy.sh`. It is an optional ClusterLogForwarder that posts MaaS application logs to the exporter. It does not change a MaaS pod.

## 6. If the Observability tab has no Tokenomics series

The ServiceMonitor sends metrics to user-workload Prometheus. The RHOAI Perses datasource `data-science-prometheus-datasource` sometimes queries only the OpenShift AI metric store. Confirm which store has the series before changing either Prometheus:

```bash
oc -n openshift-monitoring exec -c thanos-querier \
  "$(oc -n openshift-monitoring get pod -l app.kubernetes.io/name=thanos-query -o name | head -1)" \
  -- wget -qO- 'http://localhost:9090/api/v1/query?query=maas_user_token_usage_total'
```

If Thanos has the series and the Perses panel is empty, set `PERSES_DATASOURCE` to a datasource that reads Thanos and re-apply phase 3. Do not remote-write into the OpenShift AI Prometheus unless you have a separate change record for that collector.
