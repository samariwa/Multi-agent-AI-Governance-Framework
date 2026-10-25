"""HMAC-signed agent tokens.

Format:  base64url(payload_json) "." base64url(HMAC-SHA256(secret, payload_b64))

Not a full OAuth server — just the correct shape: signed by a secret only the
broker holds, short-lived, and verified outside the agent on every call.

Mint an (optionally expired) token from inside the broker container:
    python -m app.agent_token --agent assistant-agent --ttl -60
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import time


class TokenError(Exception):
    pass


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64d(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _secret() -> bytes:
    secret = os.environ.get("BROKER_SIGNING_SECRET", "")
    if len(secret) < 16:
        raise RuntimeError("BROKER_SIGNING_SECRET must be set (>= 16 chars)")
    return secret.encode()


def default_ttl() -> int:
    return int(os.environ.get("TOKEN_TTL_SECONDS", "900"))


def mint(agent_id: str, ttl_seconds: int | None = None, now: int | None = None,
         extra: dict | None = None, exp: int | None = None) -> tuple[str, dict]:
    """`extra` carries delegation claims: chain (delegators → this agent),
    on_behalf_of (the user the chain is bound to) and delegation_id."""
    now = int(time.time()) if now is None else now
    ttl = default_ttl() if ttl_seconds is None else ttl_seconds
    payload = {"agent_id": agent_id, "scope_ref": agent_id, "iat": now, "exp": exp if exp is not None else now + ttl, **(extra or {})}
    body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    sig = _b64e(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}", payload


def verify(token: str, now: int | None = None) -> dict:
    """Return the payload if the signature is valid and the token is unexpired."""
    if not isinstance(token, str) or token.count(".") != 1:
        raise TokenError("malformed token")
    body, sig = token.split(".")
    expected = _b64e(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        raise TokenError("invalid token signature")
    try:
        payload = json.loads(_b64d(body))
    except ValueError:
        raise TokenError("malformed token payload")
    now = int(time.time()) if now is None else now
    if not isinstance(payload.get("exp"), int) or payload["exp"] <= now:
        raise TokenError("token expired")
    if not payload.get("agent_id") or not payload.get("scope_ref"):
        raise TokenError("token missing agent identity")
    return payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Mint an agent token (for tests/demos).")
    parser.add_argument("--agent", default="assistant-agent")
    parser.add_argument("--ttl", type=int, default=None, help="seconds; negative mints an expired token")
    args = parser.parse_args()
    token, _ = mint(args.agent, args.ttl)
    print(token)
