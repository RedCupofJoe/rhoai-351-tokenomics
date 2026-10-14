"""OpenShift user identity for the pricing UI.

The Route terminates on an oauth-proxy sidecar. That proxy sets
``X-Forwarded-User`` and ``X-Forwarded-Groups`` and is the only process that
can reach the application, which listens on localhost. A break-glass bearer
token from ``maas-pricing-admin`` is an administrator.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from fastapi import HTTPException, Request

log = logging.getLogger(__name__)

DEFAULT_ADMIN_GROUPS = (
    "system:cluster-admins",
    "cluster-admins",
    "rhods-admins",
    "dedicated-admins",
)


@dataclass(frozen=True)
class Principal:
    name: str
    admin: bool
    method: str


def parse_groups(header_values: list[str]) -> list[str]:
    groups: list[str] = []
    for value in header_values:
        groups.extend(part.strip() for part in value.split(",") if part.strip())
    return groups


def review_allows(payload: dict) -> bool:
    status = payload.get("status") or {}
    return bool(status.get("allowed"))


class KubernetesAdminReview:
    """True when the user holds cluster-admin style wildcard access."""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, bool]] = {}

    def __call__(self, user: str, groups: list[str]) -> bool:
        key = user + "\n" + ",".join(groups)
        cached = self._cache.get(key)
        now = time.time()
        if cached and now - cached[0] < 60:
            return cached[1]
        allowed = self._review(user, groups)
        self._cache[key] = (now, allowed)
        return allowed

    def _review(self, user: str, groups: list[str]) -> bool:
        host = os.environ.get("KUBERNETES_SERVICE_HOST", "")
        if not host:
            return False
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
        ca_path = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
        if not os.path.exists(token_path) or not os.path.exists(ca_path):
            return False
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        body = json.dumps(
            {
                "apiVersion": "authorization.k8s.io/v1",
                "kind": "SubjectAccessReview",
                "spec": {
                    "user": user,
                    "groups": groups,
                    "resourceAttributes": {"verb": "*", "group": "*", "resource": "*"},
                },
            }
        ).encode("utf-8")
        try:
            token = open(token_path, encoding="utf-8").read().strip()
            request = urllib.request.Request(
                f"https://{host}:{port}/apis/authorization.k8s.io/v1/subjectaccessreviews",
                data=body,
                method="POST",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(request, timeout=5, cafile=ca_path) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
            log.warning("SubjectAccessReview failed for %s: %s", user, exc)
            return False
        return review_allows(payload if isinstance(payload, dict) else {})


def principal_from_request(
    request: Request,
    admin_token: str,
    admin_groups: tuple[str, ...],
    admin_users: tuple[str, ...],
    review_admin,
    demo: bool,
) -> Principal:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        supplied = header.split(" ", 1)[1].strip()
        if admin_token and hmac.compare_digest(supplied.encode("utf-8"), admin_token.encode("utf-8")):
            return Principal(name="admin-token", admin=True, method="token")
        raise HTTPException(status_code=401, detail="Unauthorized")
    user = (
        request.headers.get("x-forwarded-user")
        or request.headers.get("x-forwarded-preferred-username")
        or ""
    ).strip()
    if not user and demo:
        user = request.cookies.get("tokenomics-demo-user", "").strip()
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if len(user) > 256:
        raise HTTPException(status_code=401, detail="Unauthorized")
    groups = parse_groups(request.headers.getlist("x-forwarded-groups"))
    admin = user in admin_users or any(group in admin_groups for group in groups)
    if not admin and review_admin is not None:
        admin = bool(review_admin(user, groups))
    return Principal(name=user, admin=admin, method="openshift")
