"""Signed tokens with a tenant claim, and tenant resolution from the subdomain or a header.

The tenant a request targets comes from the Host subdomain (acme.tenancylab.test) or the X-Tenant-ID
header. The token's tid claim must match it. A forged header or subdomain with a valid token for another
tenant is a 403, never a silent switch to the token's tenant.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time


class AuthError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def issue_token(secret: str, tenant_id: str, sub: str, role: str = "member", ttl: int = 3600) -> str:
    payload = {"tid": tenant_id, "sub": sub, "role": role, "exp": int(time.time()) + ttl}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_token(secret: str, token: str) -> dict:
    try:
        body, sig = token.split(".")
    except ValueError as exc:
        raise AuthError(401, "malformed token") from exc
    expected = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        raise AuthError(401, "bad signature")
    try:
        claims = json.loads(_unb64(body))
    except ValueError as exc:
        raise AuthError(401, "malformed token") from exc
    if claims.get("exp", 0) < time.time():
        raise AuthError(401, "token expired")
    return claims


RESERVED = {"api", "www", "admin", "app"}


def tenant_from_host(host: str | None, base_domain: str) -> str | None:
    if not host:
        return None
    host = host.split(":")[0].lower()
    suffix = "." + base_domain
    if host.endswith(suffix):
        sub = host[: -len(suffix)]
        if sub and "." not in sub and sub not in RESERVED:
            return sub
    return None


def resolve(authorization: str | None, header_tenant: str | None, host: str | None, secret: str, base_domain: str):
    """Return (tenant_id, claims) or raise AuthError."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthError(401, "missing bearer token")
    claims = verify_token(secret, authorization.split(" ", 1)[1].strip())
    from_host = tenant_from_host(host, base_domain)
    if from_host and header_tenant and from_host != header_tenant:
        raise AuthError(400, "subdomain and X-Tenant-ID disagree")
    tenant = from_host or header_tenant
    if not tenant:
        raise AuthError(400, "tenant not specified: use a subdomain or X-Tenant-ID")
    if claims.get("tid") != tenant:
        raise AuthError(403, "token tenant does not match requested tenant")
    return tenant, claims
