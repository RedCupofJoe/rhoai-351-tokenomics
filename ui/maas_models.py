"""Models MaaS is serving, read from MaaSModelRef.

The pricing page lists these names in the cost matrix. A failure or a
cluster with no API access returns an empty list. This client does not
create or change a MaaS object.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

CACHE_SECONDS = 60


def names_from_refs(items: list) -> list[str]:
    """Gateway alias plus the served name when the two differ.

    Matching on the pricing page is an exact compare, and the vLLM fallback
    labels a row with the served name while clients send the alias.
    """
    found: dict[str, str] = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        spec = item.get("spec") if isinstance(item.get("spec"), dict) else {}
        status = item.get("status") if isinstance(item.get("status"), dict) else {}
        model_ref = spec.get("modelRef") if isinstance(spec.get("modelRef"), dict) else {}
        alias = str(status.get("resolvedModelAlias") or "").strip()
        served = str(model_ref.get("name") or "").strip()
        for name in (alias, served):
            if not name or name == "*":
                continue
            found.setdefault(name.casefold(), name)
    return [found[key] for key in sorted(found)]


def list_refs_from_api() -> list[dict]:
    host = os.environ.get("KUBERNETES_SERVICE_HOST", "")
    if not host:
        return []
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    ca_path = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
    if not os.path.exists(token_path) or not os.path.exists(ca_path):
        return []
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    token = open(token_path, encoding="utf-8").read().strip()
    request = urllib.request.Request(
        f"https://{host}:{port}/apis/maas.opendatahub.io/v1alpha1/maasmodelrefs",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=5, cafile=ca_path) as response:
        payload = json.loads(response.read().decode("utf-8"))
    items = payload.get("items") if isinstance(payload, dict) else None
    return items if isinstance(items, list) else []


class MaasModelCatalog:
    """Cached names from MaaSModelRef. The cache lasts one minute."""

    def __init__(self, list_refs=None) -> None:
        self._list_refs = list_refs or list_refs_from_api
        self._cached: tuple[float, list[str]] | None = None

    def __call__(self) -> list[str]:
        now = time.time()
        if self._cached and now - self._cached[0] < CACHE_SECONDS:
            return list(self._cached[1])
        try:
            names = names_from_refs(self._list_refs())
        except (OSError, urllib.error.URLError, json.JSONDecodeError, TimeoutError, ValueError) as exc:
            log.warning("MaaS model list failed: %s", exc)
            return list(self._cached[1]) if self._cached else []
        self._cached = (now, names)
        return list(names)
