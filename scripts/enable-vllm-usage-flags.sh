#!/usr/bin/env bash
# Turn on vLLM response usage for every LLMInferenceService in a namespace.
# deploy.sh does not call this script. It edits the model template and restarts
# each model. The model is scaled to zero first so the rollout does not ask
# for a second GPU.
set -euo pipefail

namespace="${LLM_NAMESPACE:-llm}"

if ! command -v oc >/dev/null 2>&1; then
  echo "oc is required. Log in as a cluster admin before running this script." >&2
  exit 1
fi

names="$(oc -n "${namespace}" get llminferenceservices.serving.kserve.io -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')"
if [[ -z "${names}" ]]; then
  echo "No LLMInferenceService in ${namespace}." >&2
  exit 1
fi

while IFS= read -r name; do
  [[ -n "${name}" ]] || continue
  echo "Checking ${namespace}/${name}"
  plan="$(oc -n "${namespace}" get llminferenceservice "${name}" -o json | python3 -c '
import json, sys
doc = json.load(sys.stdin)
containers = doc["spec"]["template"]["containers"]
index = next(i for i, container in enumerate(containers) if container.get("name") == "main")
args = list(containers[index].get("args") or [])
flags = ["--enable-force-include-usage", "--enable-prompt-tokens-details"]
missing = [flag for flag in flags if flag not in args]
print(json.dumps({
    "index": index,
    "replicas": int(doc["spec"].get("replicas") or 1),
    "args": args + missing,
    "missing": missing,
}))
')"
  missing="$(python3 -c 'import json,sys; print(" ".join(json.loads(sys.argv[1])["missing"]))' "${plan}")"
  if [[ -z "${missing}" ]]; then
    echo "${name} already has both usage flags."
    continue
  fi
  replicas="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["replicas"])' "${plan}")"
  echo "Scaling ${name} to 0 before adding:${missing}"
  oc -n "${namespace}" patch llminferenceservice "${name}" --type merge -p '{"spec":{"replicas":0}}'
  oc -n "${namespace}" delete pod -l "app.kubernetes.io/name=${name}" --field-selector=status.phase=Failed --ignore-not-found --wait=true
  if oc -n "${namespace}" get pod -l "app.kubernetes.io/name=${name}" --no-headers 2>/dev/null | grep -q .; then
    oc -n "${namespace}" wait --for=delete pod -l "app.kubernetes.io/name=${name}" --timeout=300s
  fi
  patch_file="$(mktemp)"
  python3 -c '
import json, sys
plan = json.loads(sys.argv[1])
patch = [{
    "op": "replace",
    "path": "/spec/template/containers/%d/args" % plan["index"],
    "value": plan["args"],
}]
with open(sys.argv[2], "w", encoding="utf-8") as handle:
    json.dump(patch, handle)
' "${plan}" "${patch_file}"
  oc -n "${namespace}" patch llminferenceservice "${name}" --type json --patch-file "${patch_file}"
  rm -f "${patch_file}"
  echo "Scaling ${name} back to ${replicas}"
  oc -n "${namespace}" patch llminferenceservice "${name}" --type merge -p "{\"spec\":{\"replicas\":${replicas}}}"
  oc -n "${namespace}" rollout status "deploy/${name}-kserve" --timeout=900s
  echo "${name} is running with the usage flags."
done <<EOF
${names}
EOF
