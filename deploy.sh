#!/usr/bin/env bash
# Apply the tokenomics extension in phase order.
# This script creates new objects only. It does not patch MaaS workloads.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

FINOPS_NAMESPACE="${FINOPS_NAMESPACE:-maas-finops-ui}"
UI_NAMESPACE="${UI_NAMESPACE:-maas-finops-ui}"
OBS_NAMESPACE="${OBS_NAMESPACE:-redhat-ods-monitoring}"
APPLICATIONS_NAMESPACE="${APPLICATIONS_NAMESPACE:-redhat-ods-applications}"
DSCI_NAME="${DSCI_NAME:-default-dsci}"
DASHBOARD_NAME="${DASHBOARD_NAME:-default-dashboard}"
CONSOLE_NAMESPACE="${CONSOLE_NAMESPACE:-openshift-config-managed}"
CARAVAN_NAMESPACE="${CARAVAN_NAMESPACE:-test-project}"
CARAVAN_AGENT_IMAGE="${CARAVAN_AGENT_IMAGE:-registry.custom.mirror/caravan-agent:dev1}"
CARAVAN_BASE_IMAGE="${CARAVAN_BASE_IMAGE:-image-registry.openshift-image-registry.svc:5000/openshift/python:3.12-ubi9}"
OPENWEBUI_IMAGE="${OPENWEBUI_IMAGE:-image-registry.openshift-image-registry.svc:5000/${CARAVAN_NAMESPACE}/open-webui:0.1.0}"
RESEARCHER_IMAGE="${RESEARCHER_IMAGE:-image-registry.openshift-image-registry.svc:5000/${CARAVAN_NAMESPACE}/researcher-agent:0.1.0}"
DSC_NAME="${DSC_NAME:-default-dsc}"
MODEL_REGISTRY_NAMESPACE="${MODEL_REGISTRY_NAMESPACE:-rhoai-model-registries}"
OPENAI_BASE_URL="${OPENAI_BASE_URL:-https://maas.apps.ocp.tlphn.sandbox983.opentlc.com/v1}"
OPENAI_MODEL="${OPENAI_MODEL:-publishers/llm/models/gemma-2-9b-it}"
# The existing test-project key is subscribed to gpt-oss-20b. Gemma returns 403 for that key.
RESEARCHER_MODEL="${RESEARCHER_MODEL:-publishers/llm/models/gpt-oss-20b}"
MAAS_NAMESPACE="${MAAS_NAMESPACE:-models-as-a-service}"
GRAFANA_LABEL="${GRAFANA_LABEL:-grafana}"
PERSES_DATASOURCE="${PERSES_DATASOURCE:-data-science-prometheus-datasource}"
EXPORTER_IMAGE="${EXPORTER_IMAGE:-image-registry.openshift-image-registry.svc:5000/${FINOPS_NAMESPACE}/maas-tokenomics-exporter:0.1.0}"
UI_IMAGE="${UI_IMAGE:-image-registry.openshift-image-registry.svc:5000/${UI_NAMESPACE}/maas-tokenomics-ui:0.1.0}"
GRAFANA_IMAGE="${GRAFANA_IMAGE:-image-registry.openshift-image-registry.svc:5000/${FINOPS_NAMESPACE}/maas-tokenomics-grafana:0.1.0}"
OAUTH_PROXY_IMAGE="${OAUTH_PROXY_IMAGE:-registry.redhat.io/openshift4/ose-oauth-proxy-rhel9:v4.18}"
APPLY_LOG_RBAC="${APPLY_LOG_RBAC:-false}"
APPLY_NETWORK_POLICY="${APPLY_NETWORK_POLICY:-true}"

render() {
  sed \
    -e "s|__FINOPS_NAMESPACE__|${FINOPS_NAMESPACE}|g" \
    -e "s|__UI_NAMESPACE__|${UI_NAMESPACE}|g" \
    -e "s|__OBS_NAMESPACE__|${OBS_NAMESPACE}|g" \
    -e "s|__MAAS_NAMESPACE__|${MAAS_NAMESPACE}|g" \
    -e "s|__EXPORTER_IMAGE__|${EXPORTER_IMAGE}|g" \
    -e "s|__UI_IMAGE__|${UI_IMAGE}|g" \
    -e "s|__OAUTH_PROXY_IMAGE__|${OAUTH_PROXY_IMAGE}|g" \
    -e "s|__GRAFANA_IMAGE__|${GRAFANA_IMAGE}|g" \
    -e "s|__GRAFANA_LABEL__|${GRAFANA_LABEL}|g" \
    -e "s|__PERSES_DATASOURCE__|${PERSES_DATASOURCE}|g" \
    -e "s|__CARAVAN_NAMESPACE__|${CARAVAN_NAMESPACE}|g" \
    -e "s|__CARAVAN_AGENT_IMAGE__|${CARAVAN_AGENT_IMAGE}|g" \
    -e "s|__CARAVAN_BASE_IMAGE__|${CARAVAN_BASE_IMAGE}|g" \
    -e "s|__OPENWEBUI_IMAGE__|${OPENWEBUI_IMAGE}|g" \
    -e "s|__RESEARCHER_IMAGE__|${RESEARCHER_IMAGE}|g" \
    -e "s|__OPENAI_BASE_URL__|${OPENAI_BASE_URL}|g" \
    -e "s|__OPENAI_MODEL__|${OPENAI_MODEL}|g" \
    -e "s|__RESEARCHER_MODEL__|${RESEARCHER_MODEL}|g" \
    "$1"
}

merge_patch() {
  local target="$1"
  local file="$2"
  shift 2
  echo "Patching ${target} from ${file}"
  oc patch "${target}" --type merge -p "$(render "${file}")" "$@"
}

apply_file() {
  local file="$1"
  case "$(basename "$file")" in
    *networkpolicy.yaml)
      if [[ "${APPLY_NETWORK_POLICY}" != "true" ]]; then
        echo "Skipping network policy ${file}"
        return 0
      fi
      ;;
  esac
  echo "Applying ${file}"
  render "$file" | oc apply -f -
}

apply_dir() {
  local dir="$1"
  local file
  for file in "${dir}"/*.yaml; do
    [[ -e "$file" ]] || continue
    apply_file "$file"
  done
}

require_oc() {
  if ! command -v oc >/dev/null 2>&1; then
    echo "oc is required. Install the OpenShift CLI and log in before deploying." >&2
    exit 1
  fi
}

ensure_secret() {
  local namespace="$1"
  local name="$2"
  local key="$3"
  if oc -n "$namespace" get secret "$name" >/dev/null 2>&1; then
    echo "Secret ${namespace}/${name} already exists."
    return 0
  fi
  local value
  value="$(openssl rand -hex 32)"
  oc -n "$namespace" create secret generic "$name" --from-literal="${key}=${value}"
  echo "Created ${namespace}/${name}. Read it with:"
  echo "  oc -n ${namespace} get secret ${name} -o jsonpath='{.data.${key}}' | base64 -d; echo"
}

phase1() {
  apply_dir "${ROOT}/deploy/phase1"
  if [[ "${APPLY_LOG_RBAC}" == "true" ]]; then
    echo "Applying optional log-reader RBAC in ${MAAS_NAMESPACE}. This adds a Role only."
    apply_file "${ROOT}/deploy/optional/log-reader-rbac.yaml"
  else
    echo "Skipping log-reader RBAC. Set APPLY_LOG_RBAC=true to read MaaS pod logs."
  fi
}

phase2() {
  ensure_secret "${FINOPS_NAMESPACE}" maas-tokenomics-credentials ingest-token
  apply_dir "${ROOT}/deploy/phase2"
}

publish_dashboard_configmap() {
  local namespace="$1"
  echo "Creating Tokenomics dashboard ConfigMap in ${namespace}"
  oc -n "${namespace}" create configmap maas-tokenomics-dashboard \
    --from-file="tokenomics.json=${ROOT}/deploy/phase3/tokenomics.json" \
    --dry-run=client -o yaml | oc apply -f -
}

ensure_cluster_observability() {
  apply_file "${ROOT}/deploy/phase3/cluster-observability-subscription.yaml"
  apply_file "${ROOT}/deploy/phase3/opentelemetry-subscription.yaml"
  apply_file "${ROOT}/deploy/phase3/tempo-subscription.yaml"
  echo "Waiting for the observability operators"
  local i
  for i in $(seq 1 60); do
    if oc get crd perses.perses.dev >/dev/null 2>&1 \
      && oc get crd monitoringstacks.monitoring.rhobs >/dev/null 2>&1 \
      && oc get crd opentelemetrycollectors.opentelemetry.io >/dev/null 2>&1 \
      && oc get crd tempomonolithics.tempo.grafana.com >/dev/null 2>&1; then
      return 0
    fi
    sleep 5
  done
  echo "Observability operator CRDs were not installed. Confirm the redhat-operators catalog is available." >&2
  exit 1
}

fix_cluster_prometheus_proxy() {
  # The operator starts this proxy at 20m CPU and 40Mi memory. It never binds
  # before the liveness probe restarts it. Match the namespace proxy budget.
  local i
  for i in $(seq 1 30); do
    if oc -n "${OBS_NAMESPACE}" get deploy data-science-prometheus-cluster-proxy >/dev/null 2>&1; then
      oc -n "${OBS_NAMESPACE}" patch deploy data-science-prometheus-cluster-proxy --type json -p '[
        {"op":"replace","path":"/spec/template/spec/containers/0/resources","value":{"requests":{"cpu":"50m","memory":"64Mi"},"limits":{"cpu":"100m","memory":"128Mi"}}}
      ]'
      return 0
    fi
    sleep 10
  done
  echo "Deployment ${OBS_NAMESPACE}/data-science-prometheus-cluster-proxy was not created." >&2
  exit 1
}

enable_maas_observability() {
  ensure_cluster_observability
  merge_patch "dscinitialization/${DSCI_NAME}" "${ROOT}/deploy/phase3/dsci-monitoring-patch.yaml"
  merge_patch "odhdashboardconfig/odh-dashboard-config" "${ROOT}/deploy/phase3/odh-observability-dashboard-patch.yaml" -n "${APPLICATIONS_NAMESPACE}"
  echo "Waiting for Perses service data-science-perses in ${OBS_NAMESPACE}"
  local i
  for i in $(seq 1 90); do
    if oc -n "${OBS_NAMESPACE}" get service data-science-perses >/dev/null 2>&1; then
      merge_patch "dashboard/${DASHBOARD_NAME}" "${ROOT}/deploy/phase3/dashboard-observability-patch.yaml"
      fix_cluster_prometheus_proxy
      echo "MaaS observability dashboard proxy is enabled."
      return 0
    fi
    sleep 10
  done
  echo "Perses service ${OBS_NAMESPACE}/data-science-perses was not created." >&2
  exit 1
}

ensure_grafana_operator() {
  apply_file "${ROOT}/deploy/phase3/operator-subscription.yaml"
  echo "Waiting for Grafana operator CRDs from the Community Operators catalog"
  local i
  for i in $(seq 1 60); do
    if oc get crd grafanas.grafana.integreatly.org >/dev/null 2>&1 \
      && oc get crd grafanadashboards.grafana.integreatly.org >/dev/null 2>&1 \
      && oc get crd grafanadatasources.grafana.integreatly.org >/dev/null 2>&1; then
      return 0
    fi
    sleep 5
  done
  echo "Grafana operator CRDs were not installed. Confirm the community-operators catalog is available." >&2
  exit 1
}

ensure_grafana_bearer() {
  local namespace="$1"
  local token_b64=""
  local i
  for i in $(seq 1 30); do
    token_b64="$(oc -n "${namespace}" get secret maas-tokenomics-grafana-token -o jsonpath='{.data.token}' 2>/dev/null || true)"
    if [[ -n "${token_b64}" ]]; then
      break
    fi
    sleep 2
  done
  if [[ -z "${token_b64}" ]]; then
    echo "Service account token for maas-tokenomics-grafana was not issued." >&2
    exit 1
  fi
  local raw
  raw="$(printf '%s' "${token_b64}" | base64 -d | tr -d '\n')"
  oc -n "${namespace}" create secret generic maas-tokenomics-grafana-bearer \
    --from-literal=authorization="Bearer ${raw}" \
    --dry-run=client -o yaml | oc apply -f -
  echo "Grafana Thanos credential secret ${namespace}/maas-tokenomics-grafana-bearer is set."
}

wait_for_service_ca() {
  local namespace="$1"
  local cert=""
  local i
  for i in $(seq 1 30); do
    cert="$(oc -n "${namespace}" get configmap openshift-service-ca -o jsonpath='{.data.service-ca\.crt}' 2>/dev/null || true)"
    if [[ -n "${cert}" ]]; then
      return 0
    fi
    sleep 2
  done
  echo "OpenShift did not inject the service CA bundle into ${namespace}/openshift-service-ca." >&2
  exit 1
}

phase3() {
  if ! oc get namespace "${OBS_NAMESPACE}" >/dev/null 2>&1; then
    echo "Namespace ${OBS_NAMESPACE} does not exist. Set OBS_NAMESPACE to the RHOAI monitoring namespace." >&2
    exit 1
  fi
  enable_maas_observability
  ensure_grafana_operator
  apply_file "${ROOT}/deploy/phase3/grafana-rbac.yaml"
  apply_file "${ROOT}/deploy/phase3/10-service-ca.yaml"
  wait_for_service_ca "${FINOPS_NAMESPACE}"
  ensure_grafana_bearer "${FINOPS_NAMESPACE}"
  apply_file "${ROOT}/deploy/phase3/grafana.yaml"
  apply_file "${ROOT}/deploy/phase3/grafanadatasource.yaml"
  publish_dashboard_configmap "${FINOPS_NAMESPACE}"
  apply_file "${ROOT}/deploy/phase3/grafanadashboard.yaml"
  if [[ "${OBS_NAMESPACE}" != "${FINOPS_NAMESPACE}" ]]; then
    oc -n "${OBS_NAMESPACE}" delete grafanadashboard maas-tokenomics --ignore-not-found
  fi
  publish_dashboard_configmap "${OBS_NAMESPACE}"
  publish_dashboard_configmap "${CONSOLE_NAMESPACE}"
  oc -n "${CONSOLE_NAMESPACE}" label configmap maas-tokenomics-dashboard \
    console.openshift.io/dashboard=true \
    console.openshift.io/odc-dashboard=true \
    app.kubernetes.io/part-of=rhoai-tokenomics \
    app.kubernetes.io/name=maas-tokenomics-dashboard \
    app.kubernetes.io/component=dashboard \
    --overwrite
  echo "OpenShift console dashboard: Observe and Developer menus in ${CONSOLE_NAMESPACE}/maas-tokenomics-dashboard"
  oc -n "${OBS_NAMESPACE}" label configmap maas-tokenomics-dashboard \
    grafana_dashboard=1 \
    console.openshift.io/odc-dashboard=true \
    dashboard=true \
    app.kubernetes.io/part-of=rhods-dashboard \
    app.kubernetes.io/name=maas-tokenomics-dashboard \
    app.kubernetes.io/component=dashboard \
    --overwrite
  oc -n "${OBS_NAMESPACE}" annotate configmap maas-tokenomics-dashboard \
    grafana_folder="RHOAI Observability" \
    --overwrite
  echo "Grafana image: ${GRAFANA_IMAGE}"
  echo "Grafana routes in ${FINOPS_NAMESPACE}:"
  oc -n "${FINOPS_NAMESPACE}" get route -o jsonpath='{range .items[*]}{.metadata.name}{" https://"}{.spec.host}{"\n"}{end}' || true
  if oc get crd persesdashboards.perses.dev >/dev/null 2>&1; then
    apply_file "${ROOT}/deploy/phase3/perses-operator-networkpolicy.yaml"
    apply_file "${ROOT}/deploy/phase3/persesdashboard.yaml"
    apply_file "${ROOT}/deploy/phase3/cost-per-token-dashboard.yaml"
  else
    echo "PersesDashboard CRD is not installed. The RHOAI Observability tab will not show Tokenomics until Cluster Observability Operator is installed."
  fi
}

ensure_cookie_secret() {
  local namespace="$1"
  if oc -n "$namespace" get secret maas-pricing-oauth-cookie >/dev/null 2>&1; then
    echo "Secret ${namespace}/maas-pricing-oauth-cookie already exists."
    return 0
  fi
  oc -n "$namespace" create secret generic maas-pricing-oauth-cookie \
    --from-literal=session_secret="$(openssl rand -base64 32 | tr -d '\n')"
  echo "Created ${namespace}/maas-pricing-oauth-cookie."
}

ensure_named_secret() {
  local namespace="$1"
  local name="$2"
  local key="$3"
  local value="$4"
  if oc -n "$namespace" get secret "$name" >/dev/null 2>&1; then
    echo "Secret ${namespace}/${name} already exists."
    return 0
  fi
  oc -n "$namespace" create secret generic "$name" --from-literal="${key}=${value}"
  echo "Created ${namespace}/${name}."
}

wait_for_mcp_crd() {
  echo "Waiting for MCPServer CRD"
  local _i
  for _i in $(seq 1 60); do
    if oc get crd mcpservers.mcp.x-k8s.io >/dev/null 2>&1; then
      return 0
    fi
    sleep 3
  done
  echo "MCPServer CRD did not appear. The MCP Lifecycle Operator is still starting." >&2
  exit 1
}

patch_catalog_configmap() {
  local name="$1"
  shift
  local patch
  patch="$(mktemp)"
  python3 - "$patch" "${CARAVAN_NAMESPACE}" "$@" <<'PY'
import json
import pathlib
import sys

out = pathlib.Path(sys.argv[1])
namespace = sys.argv[2]
data = {}
args = sys.argv[3:]
for index in range(0, len(args), 2):
    key, path = args[index], args[index + 1]
    text = pathlib.Path(path).read_text(encoding="utf-8")
    data[key] = text.replace("__CARAVAN_NAMESPACE__", namespace)
out.write_text(json.dumps({"data": data}), encoding="utf-8")
PY
  echo "Patching configmap ${MODEL_REGISTRY_NAMESPACE}/${name}"
  oc -n "${MODEL_REGISTRY_NAMESPACE}" patch configmap "$name" --type merge --patch-file "$patch"
  rm -f "$patch"
}

catalog_lists_name() {
  local path="$1"
  oc -n "${MODEL_REGISTRY_NAMESPACE}" exec deploy/model-catalog -c catalog -- \
    curl -sf -m 15 "http://127.0.0.1:8080${path}" | grep -q '"name"'
}

catalog_is_serving_research() {
  catalog_lists_name "/api/mcp_catalog/v1alpha1/mcp_servers?name=wikipedia" \
    && catalog_lists_name "/api/agent_catalog/v1alpha1/agents?name=researcher-agent"
}

wait_for_research_catalog() {
  local _i
  for _i in $(seq 1 18); do
    if catalog_is_serving_research; then
      echo "Model catalog is serving the Research sources."
      return 0
    fi
    sleep 5
  done
  return 1
}

reload_catalog_if_needed() {
  # Leadership can take about a minute after a rollout. Restart only if the
  # sources are still absent, then clear a stale catalog-leader row if the
  # new pod cannot take the lock left by the pod that just exited.
  if wait_for_research_catalog; then
    return 0
  fi
  echo "Model catalog did not reload the Research sources. Restarting model-catalog."
  oc -n "${MODEL_REGISTRY_NAMESPACE}" rollout restart deploy/model-catalog
  oc -n "${MODEL_REGISTRY_NAMESPACE}" rollout status deploy/model-catalog --timeout=300s
  if wait_for_research_catalog; then
    return 0
  fi
  echo "Clearing a stale catalog-leader lock so the catalog pod can load the sources."
  oc -n "${MODEL_REGISTRY_NAMESPACE}" exec deploy/model-catalog-postgres -- \
    psql -U postgres -d model_catalog -c "delete from locks where name = 'catalog-leader';"
  if wait_for_research_catalog; then
    return 0
  fi
  echo "Model catalog is not serving the Research sources." >&2
  exit 1
}

ensure_researcher_image() {
  apply_file "${ROOT}/deploy/build/researcher-build.yaml"
  if [[ "${REBUILD_RESEARCHER:-false}" != "true" ]] && oc -n "${CARAVAN_NAMESPACE}" get imagestreamtag researcher-agent:0.1.0 >/dev/null 2>&1; then
    echo "Researcher image ${RESEARCHER_IMAGE} already exists."
    return 0
  fi
  echo "Building researcher agent from researcher/Containerfile (UBI 9 Python)."
  oc -n "${CARAVAN_NAMESPACE}" start-build researcher-agent --from-dir="${ROOT}" --follow
}

ensure_openwebui_image() {
  apply_file "${ROOT}/deploy/build/openwebui-build.yaml"
  if [[ "${REBUILD_OPENWEBUI:-false}" != "true" ]] && oc -n "${CARAVAN_NAMESPACE}" get imagestreamtag open-webui:0.1.0 >/dev/null 2>&1; then
    echo "Open WebUI image ${OPENWEBUI_IMAGE} already exists."
    return 0
  fi
  echo "Building Open WebUI from openwebui/Containerfile (UBI 9 Python)."
  oc -n "${CARAVAN_NAMESPACE}" start-build open-webui --from-dir="${ROOT}" --follow
}

phase4() {
  apply_file "${ROOT}/deploy/phase4/10-service-ca.yaml"
  apply_file "${ROOT}/deploy/phase4/20-pvc.yaml"
  ensure_secret "${UI_NAMESPACE}" maas-pricing-admin token
  ensure_cookie_secret "${UI_NAMESPACE}"
  apply_file "${ROOT}/deploy/phase4/40-service.yaml"
  apply_file "${ROOT}/deploy/phase4/50-route.yaml"
  apply_file "${ROOT}/deploy/phase4/30-deployment.yaml"
  apply_file "${ROOT}/deploy/phase4/60-networkpolicy.yaml"
  echo "Pricing route:"
  oc -n "${UI_NAMESPACE}" get route maas-pricing -o jsonpath='https://{.spec.host}{"\n"}' || true
}

phase5() {
  merge_patch "odhdashboardconfig/odh-dashboard-config" "${ROOT}/deploy/caravan/80-dashboard-catalog-patch.yaml" -n "${APPLICATIONS_NAMESPACE}"
  merge_patch "datasciencecluster/${DSC_NAME}" "${ROOT}/deploy/caravan/81-dsc-mcp-operator-patch.yaml"
  wait_for_mcp_crd
  apply_file "${ROOT}/deploy/caravan/00-namespace.yaml"
  apply_file "${ROOT}/deploy/caravan/10-serviceaccount.yaml"
  apply_file "${ROOT}/deploy/caravan/20-pvc.yaml"
  if oc -n "${CARAVAN_NAMESPACE}" get secret ojoseph-test-agent-secret >/dev/null 2>&1; then
    echo "Secret ${CARAVAN_NAMESPACE}/ojoseph-test-agent-secret already exists."
  else
    oc -n "${CARAVAN_NAMESPACE}" create secret generic ojoseph-test-agent-secret \
      --from-literal=OPENAI_API_KEY="${OPENAI_API_KEY:-}"
    echo "Created ${CARAVAN_NAMESPACE}/ojoseph-test-agent-secret."
    if [[ -z "${OPENAI_API_KEY:-}" ]]; then
      echo "OPENAI_API_KEY was empty. Set it with: oc -n ${CARAVAN_NAMESPACE} create secret generic ojoseph-test-agent-secret --from-literal=OPENAI_API_KEY=... --dry-run=client -o yaml | oc apply -f -"
    fi
  fi
  ensure_named_secret "${CARAVAN_NAMESPACE}" open-webui-secret WEBUI_SECRET_KEY "$(openssl rand -hex 32)"
  ensure_named_secret "${CARAVAN_NAMESPACE}" open-webui-oauth-cookie session_secret "$(openssl rand -base64 32 | tr -d '\n')"
  apply_file "${ROOT}/deploy/caravan/50-service.yaml"
  apply_file "${ROOT}/deploy/caravan/60-route.yaml"
  ensure_openwebui_image
  ensure_researcher_image
  apply_file "${ROOT}/deploy/caravan/35-ojoseph-test-agent.yaml"
  apply_file "${ROOT}/deploy/caravan/36-researcher-agent.yaml"
  apply_file "${ROOT}/deploy/caravan/37-research-mcp.yaml"
  patch_catalog_configmap mcp-catalog-sources \
    sources.yaml "${ROOT}/catalog/mcp-sources.yaml" \
    research-mcp.yaml "${ROOT}/catalog/research-mcp.yaml"
  patch_catalog_configmap agent-catalog-sources \
    sources.yaml "${ROOT}/catalog/agent-sources.yaml" \
    researcher-agent.yaml "${ROOT}/catalog/researcher-agent.yaml"
  reload_catalog_if_needed
  oc -n "${CARAVAN_NAMESPACE}" delete deployment caravan-agent caravan-agent-ubi --ignore-not-found
  oc -n "${CARAVAN_NAMESPACE}" delete networkpolicy caravan-agent --ignore-not-found
  apply_file "${ROOT}/deploy/caravan/40-open-webui.yaml"
  apply_file "${ROOT}/deploy/caravan/70-networkpolicy.yaml"
  echo "Open WebUI route:"
  oc -n "${CARAVAN_NAMESPACE}" get route open-webui -o jsonpath='https://{.spec.host}{"\n"}' || true
}

undeploy_caravan() {
  echo "Removing Caravan and Open WebUI from ${CARAVAN_NAMESPACE}."
  oc delete namespace "${CARAVAN_NAMESPACE}" --ignore-not-found --wait=false
  echo "Namespace deletion requested for ${CARAVAN_NAMESPACE}."
}

undeploy() {
  echo "Removing tokenomics objects. MaaS workloads are left in place."
  oc -n "${FINOPS_NAMESPACE}" delete grafanadatasource thanos --ignore-not-found
  oc -n "${FINOPS_NAMESPACE}" delete grafanadashboard maas-tokenomics --ignore-not-found
  oc -n "${OBS_NAMESPACE}" delete grafanadashboard maas-tokenomics --ignore-not-found
  oc -n "${FINOPS_NAMESPACE}" delete grafana maas-tokenomics --ignore-not-found
  oc -n openshift-operators delete subscription grafana-operator --ignore-not-found
  oc -n openshift-operators delete csv -l operators.coreos.com/grafana-operator.openshift-operators= --ignore-not-found
  oc delete clusterrolebinding "${FINOPS_NAMESPACE}-grafana-monitoring-view" --ignore-not-found
  oc -n "${OBS_NAMESPACE}" delete persesdashboard dashboard-maas-tokenomics dashboard-cost-per-token-admin --ignore-not-found
  oc -n "${OBS_NAMESPACE}" delete networkpolicy tokenomics-perses-operator-access --ignore-not-found
  oc -n "${OBS_NAMESPACE}" delete configmap maas-tokenomics-dashboard --ignore-not-found
  oc -n "${CONSOLE_NAMESPACE}" delete configmap maas-tokenomics-dashboard --ignore-not-found
  oc delete clusterrolebinding "${UI_NAMESPACE}-monitoring-view" --ignore-not-found
  oc delete clusterrolebinding "${UI_NAMESPACE}-auth-review" --ignore-not-found
  oc delete clusterrole maas-tokenomics-auth-review --ignore-not-found
  oc -n "${MAAS_NAMESPACE}" delete role,rolebinding maas-tokenomics-log-reader --ignore-not-found
  oc delete namespace "${FINOPS_NAMESPACE}" "${UI_NAMESPACE}" --ignore-not-found --wait=false
  echo "Namespace deletion requested for ${FINOPS_NAMESPACE} and ${UI_NAMESPACE}."
}

render_all() {
  local file
  for file in "${ROOT}/deploy/phase1"/*.yaml "${ROOT}/deploy/phase2"/*.yaml \
    "${ROOT}/deploy/phase3"/*.yaml "${ROOT}/deploy/phase4"/*.yaml \
    "${ROOT}/deploy/caravan"/*.yaml \
    "${ROOT}/deploy/optional"/*.yaml "${ROOT}/deploy/build"/*.yaml; do
    [[ -e "$file" ]] || continue
    echo "---"
    echo "# source: ${file#"${ROOT}/"}"
    render "$file"
  done
}

usage() {
  cat <<EOF
Usage: ./deploy.sh [phase ...]

Phases:
  1  Namespaces, service accounts, monitoring RBAC
  2  Exporter Deployment, Service, ServiceMonitor
  3  Grafana operator, UBI Grafana instance, Thanos datasource, dashboard, and Perses when that CRD exists
  4  Pricing UI, OpenShift login proxy, Service, and Route in ${UI_NAMESPACE}
  5  Caravan agent, researcher agent, research MCP servers, and the Agent and MCP catalogs in ${CARAVAN_NAMESPACE}

With no arguments, phases run in order 1 2 3 4. Phase 5 is separate.
  ./deploy.sh undeploy          Remove the tokenomics extension. Does not delete MaaS workloads.
  ./deploy.sh undeploy-caravan  Remove the Caravan namespace.
  ./deploy.sh render            Print rendered manifests. Does not require oc.

Build images after phase 1 and before phase 2:
  ./scripts/build-images.sh
EOF
}

main() {
  if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
  fi
  if [[ "${1:-}" == "render" ]]; then
    render_all
    exit 0
  fi
  require_oc
  if [[ "${1:-}" == "undeploy" ]]; then
    undeploy
    exit 0
  fi
  if [[ "${1:-}" == "undeploy-caravan" ]]; then
    undeploy_caravan
    exit 0
  fi
  local phases=("$@")
  if [[ ${#phases[@]} -eq 0 ]]; then
    phases=(1 2 3 4)
  fi
  local phase
  for phase in "${phases[@]}"; do
    case "$phase" in
      1) phase1 ;;
      2) phase2 ;;
      3) phase3 ;;
      4) phase4 ;;
      5) phase5 ;;
      *)
        echo "Unknown phase: $phase" >&2
        usage
        exit 1
        ;;
    esac
  done
}

main "$@"
