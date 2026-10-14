#!/usr/bin/env bash
# Apply the tokenomics extension in phase order.
# This script creates new objects only. It does not patch MaaS workloads.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

FINOPS_NAMESPACE="${FINOPS_NAMESPACE:-maas-finops-ui}"
UI_NAMESPACE="${UI_NAMESPACE:-maas-finops-ui}"
OBS_NAMESPACE="${OBS_NAMESPACE:-redhat-ods-monitoring}"
MAAS_NAMESPACE="${MAAS_NAMESPACE:-models-as-a-service}"
GRAFANA_LABEL="${GRAFANA_LABEL:-grafana}"
PERSES_DATASOURCE="${PERSES_DATASOURCE:-data-science-prometheus-datasource}"
EXPORTER_IMAGE="${EXPORTER_IMAGE:-image-registry.openshift-image-registry.svc:5000/${FINOPS_NAMESPACE}/maas-tokenomics-exporter:0.1.0}"
UI_IMAGE="${UI_IMAGE:-image-registry.openshift-image-registry.svc:5000/${UI_NAMESPACE}/maas-tokenomics-ui:0.1.0}"
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
    -e "s|__GRAFANA_LABEL__|${GRAFANA_LABEL}|g" \
    -e "s|__PERSES_DATASOURCE__|${PERSES_DATASOURCE}|g" \
    "$1"
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

phase3() {
  if ! oc get namespace "${OBS_NAMESPACE}" >/dev/null 2>&1; then
    echo "Namespace ${OBS_NAMESPACE} does not exist. Set OBS_NAMESPACE to the RHOAI monitoring namespace." >&2
    exit 1
  fi
  echo "Creating Tokenomics Grafana ConfigMap in ${OBS_NAMESPACE}"
  oc -n "${OBS_NAMESPACE}" create configmap maas-tokenomics-dashboard \
    --from-file="tokenomics.json=${ROOT}/deploy/phase3/tokenomics.json" \
    --dry-run=client -o yaml | oc apply -f -
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
  if oc get crd grafanadashboards.grafana.integreatly.org >/dev/null 2>&1; then
    apply_file "${ROOT}/deploy/phase3/grafanadashboard.yaml"
  else
    echo "GrafanaDashboard CRD is not installed. Import deploy/phase3/tokenomics.json into Grafana, or install the Grafana operator."
  fi
  if oc get crd persesdashboards.perses.dev >/dev/null 2>&1; then
    apply_file "${ROOT}/deploy/phase3/persesdashboard.yaml"
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

undeploy() {
  echo "Removing tokenomics objects. MaaS workloads are left in place."
  oc -n "${OBS_NAMESPACE}" delete grafanadashboard maas-tokenomics --ignore-not-found
  oc -n "${OBS_NAMESPACE}" delete persesdashboard dashboard-maas-tokenomics --ignore-not-found
  oc -n "${OBS_NAMESPACE}" delete configmap maas-tokenomics-dashboard --ignore-not-found
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
  3  Grafana ConfigMap plus Perses/Grafana dashboard objects when those CRDs exist
  4  Pricing UI, OpenShift login proxy, Service, and Route in ${UI_NAMESPACE}

With no arguments, phases run in order 1 2 3 4.
  ./deploy.sh undeploy   Remove this extension. Does not delete MaaS workloads.
  ./deploy.sh render     Print rendered manifests. Does not require oc.

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
      *)
        echo "Unknown phase: $phase" >&2
        usage
        exit 1
        ;;
    esac
  done
}

main "$@"
