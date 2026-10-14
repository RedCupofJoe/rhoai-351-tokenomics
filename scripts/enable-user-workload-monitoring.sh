#!/usr/bin/env bash
# Enable user workload monitoring so ServiceMonitors outside openshift-monitoring
# are scraped. This edits cluster-monitoring-config in openshift-monitoring.
# deploy.sh does not call this script. It does not change MaaS workloads.
set -euo pipefail

if ! command -v oc >/dev/null 2>&1; then
  echo "oc is required. Log in as a cluster admin before running this script." >&2
  exit 1
fi

namespace="openshift-monitoring"
name="cluster-monitoring-config"
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

merge_config() {
  python3 -c '
import sys
text = sys.stdin.read()
lines = text.splitlines()
found = False
out = []
for line in lines:
    if line.startswith("enableUserWorkload:"):
        out.append("enableUserWorkload: true")
        found = True
    else:
        out.append(line)
if not found:
    out.insert(0, "enableUserWorkload: true")
sys.stdout.write("\n".join(out).rstrip() + "\n")
'
}

if ! oc -n "$namespace" get configmap "$name" >/dev/null 2>&1; then
  printf '%s\n' "enableUserWorkload: true" >"$tmp"
  oc -n "$namespace" create configmap "$name" --from-file=config.yaml="$tmp"
  echo "Created ${namespace}/${name} with enableUserWorkload: true"
  echo "Wait for the user-workload monitoring stack in openshift-user-workload-monitoring to roll out."
  exit 0
fi

current="$(oc -n "$namespace" get configmap "$name" -o jsonpath='{.data.config\.yaml}')"
printf '%s' "$current" | merge_config >"$tmp"
oc -n "$namespace" create configmap "$name" \
  --from-file=config.yaml="$tmp" \
  --dry-run=client -o yaml | oc apply -f -
echo "Set enableUserWorkload: true on ${namespace}/${name}."
echo "Other keys in config.yaml were left in place."
echo "Wait for the user-workload monitoring stack in openshift-user-workload-monitoring to roll out."
