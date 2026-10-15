#!/bin/sh
# Entrypoint compatible with the Grafana operator, which sets GF_* and mounts grafana.ini.
set -eu
export GF_PATHS_DATA="${GF_PATHS_DATA:-/var/lib/grafana}"
export GF_PATHS_LOGS="${GF_PATHS_LOGS:-/var/log/grafana}"
export GF_PATHS_PLUGINS="${GF_PATHS_PLUGINS:-/var/lib/grafana/plugins}"
export GF_PATHS_PROVISIONING="${GF_PATHS_PROVISIONING:-/etc/grafana/provisioning}"
if [ -f /etc/grafana/grafana.ini ]; then
  exec /usr/share/grafana/bin/grafana server --homepath=/usr/share/grafana --config=/etc/grafana/grafana.ini
fi
exec /usr/share/grafana/bin/grafana server --homepath=/usr/share/grafana
