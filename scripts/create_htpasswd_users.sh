#!/usr/bin/env bash
# Create nora, owen, priya, and samir only when the cluster has no identity
# provider besides the local kubeadmin user. An existing provider is left as-is.
# The htpasswd file is created in a temp directory and is not committed.
set -euo pipefail

users=(nora owen priya samir)
password=dev
secret_name=tokenomics-htpasswd
idp_name=tokenomics-htpasswd

if ! command -v oc >/dev/null 2>&1; then
  echo "oc is required." >&2
  exit 1
fi

# kubeadmin is the local admin and is not an OAuth identity provider.
# Any configured provider is the cluster's authentication, so HTPasswd is not added.
provider_names=$(oc get oauth cluster -o jsonpath='{range .spec.identityProviders[*]}{.name}{"\n"}{end}')
if [[ -n "$provider_names" ]]; then
  echo "Using the existing authentication providers:"
  printf '%s\n' "$provider_names"
  echo "HTPasswd setup was not run."
  exit 0
fi

if ! command -v htpasswd >/dev/null 2>&1; then
  echo "htpasswd is required." >&2
  exit 1
fi

workdir=$(mktemp -d)
trap 'rm -rf "$workdir"' EXIT
htfile="${workdir}/users.htpasswd"

first=1
for name in "${users[@]}"; do
  if [[ "$first" -eq 1 ]]; then
    htpasswd -B -b -c "$htfile" "$name" "$password" >/dev/null
    first=0
  else
    htpasswd -B -b "$htfile" "$name" "$password" >/dev/null
  fi
done

echo "No identity provider is configured besides the local kubeadmin user."
echo "Adding HTPasswd identity provider ${idp_name}."

oc -n openshift-config create secret generic "$secret_name" \
  --from-file=htpasswd="$htfile" \
  --dry-run=client -o yaml | oc apply -f -

if ! oc patch oauth cluster --type json --patch "$(cat <<'EOF'
[{"op":"add","path":"/spec/identityProviders","value":[{"name":"tokenomics-htpasswd","mappingMethod":"claim","type":"HTPasswd","htpasswd":{"fileData":{"name":"tokenomics-htpasswd"}}}]}]
EOF
)"; then
  oc patch oauth cluster --type json --patch "$(cat <<'EOF'
[{"op":"add","path":"/spec/identityProviders/-","value":{"name":"tokenomics-htpasswd","mappingMethod":"claim","type":"HTPasswd","htpasswd":{"fileData":{"name":"tokenomics-htpasswd"}}}}]
EOF
)"
fi

after=$(oc get oauth cluster -o jsonpath='{.spec.identityProviders[0].name}')
if [[ "$after" != "$idp_name" ]]; then
  echo "Refusing to continue: first identity provider is ${after}, expected ${idp_name}." >&2
  exit 1
fi
echo "Identity providers: $(oc get oauth cluster -o jsonpath='{.spec.identityProviders[*].name}')"

echo "Waiting for the OpenShift OAuth server."
oc -n openshift-authentication rollout status deployment/oauth-openshift --timeout=300s

server=$(oc whoami --show-server)
insecure=$(oc config view --raw --minify -o jsonpath='{.clusters[0].cluster.insecure-skip-tls-verify}')
ca_data=$(oc config view --raw --minify -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')
ca_file=""
ca_path=""
if [[ -n "$ca_data" ]]; then
  ca_file="${workdir}/ca.crt"
  printf '%s' "$ca_data" | base64 -d > "$ca_file"
else
  ca_path=$(oc config view --raw --minify -o jsonpath='{.clusters[0].cluster.certificate-authority}')
fi

for name in "${users[@]}"; do
  cfg="${workdir}/${name}.kubeconfig"
  ok=0
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    login_cmd=(oc login --kubeconfig="$cfg" -u "$name" -p "$password" "$server")
    if [[ -n "$ca_file" ]]; then
      login_cmd+=(--certificate-authority="$ca_file")
    elif [[ -n "$ca_path" ]]; then
      login_cmd+=(--certificate-authority="$ca_path")
    elif [[ "$insecure" == "true" ]]; then
      login_cmd+=(--insecure-skip-tls-verify)
    fi
    if "${login_cmd[@]}" >"${workdir}/login.out" 2>&1; then
      ok=1
      break
    fi
    sleep 10
  done
  if [[ "$ok" -ne 1 ]]; then
    python3 - "$name" "${workdir}/login.out" <<'PY'
import re, sys
name, path = sys.argv[1], sys.argv[2]
text = open(path, encoding="utf-8", errors="replace").read()
text = re.sub(r"sha256~\S+", "[redacted]", text)
text = re.sub(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+", "[redacted]", text)
print("login failed for", name)
print(text[-500:])
PY
    exit 1
  fi
  who=$(oc --kubeconfig="$cfg" whoami)
  echo "Logged in ${who}"
  rm -f "$cfg"
done

for name in "${users[@]}"; do
  identity=$(oc get user "$name" -o jsonpath='{.identities[0]}')
  echo "user ${name} identity ${identity}"
  if [[ "$identity" != "${idp_name}:${name}" ]]; then
    echo "Expected identity ${idp_name}:${name}." >&2
    exit 1
  fi
done

admin=$(oc whoami)
if [[ "$admin" != "admin" ]]; then
  echo "Current kubeconfig is ${admin}, expected admin." >&2
  exit 1
fi
echo "Current user remains ${admin}."
