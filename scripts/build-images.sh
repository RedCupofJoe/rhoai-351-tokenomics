#!/usr/bin/env bash
# Build the exporter and pricing UI with OpenShift binary builds.
# Run ./deploy.sh 1 first so the namespaces exist.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FINOPS_NAMESPACE="${FINOPS_NAMESPACE:-maas-finops-ui}"
UI_NAMESPACE="${UI_NAMESPACE:-maas-finops-ui}"

if ! command -v oc >/dev/null 2>&1; then
  echo "oc is required." >&2
  exit 1
fi

oc get namespace "${FINOPS_NAMESPACE}" >/dev/null
oc get namespace "${UI_NAMESPACE}" >/dev/null

sed \
  -e "s|__FINOPS_NAMESPACE__|${FINOPS_NAMESPACE}|g" \
  -e "s|__UI_NAMESPACE__|${UI_NAMESPACE}|g" \
  "${ROOT}/deploy/build/exporter-build.yaml" | oc apply -f -
sed \
  -e "s|__FINOPS_NAMESPACE__|${FINOPS_NAMESPACE}|g" \
  -e "s|__UI_NAMESPACE__|${UI_NAMESPACE}|g" \
  "${ROOT}/deploy/build/ui-build.yaml" | oc apply -f -

oc -n "${FINOPS_NAMESPACE}" start-build maas-tokenomics-exporter --from-dir="${ROOT}" --follow
oc -n "${UI_NAMESPACE}" start-build maas-tokenomics-ui --from-dir="${ROOT}" --follow

echo "Images:"
echo "  image-registry.openshift-image-registry.svc:5000/${FINOPS_NAMESPACE}/maas-tokenomics-exporter:0.1.0"
echo "  image-registry.openshift-image-registry.svc:5000/${UI_NAMESPACE}/maas-tokenomics-ui:0.1.0"
