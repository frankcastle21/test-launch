"""HTTP client for the mock signup API.

Refuses any base URL that is not loopback. This tool exists to test
scheduling/persistence infrastructure without submitting registrations to a
real service, so there is deliberately no flag to point it elsewhere.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .wallets import Wallet

LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}

# Error kinds drive the runner's retry policy.
RETRYABLE = "retryable"   # 5xx, timeouts, connection errors
THROTTLED = "throttled"   # 429
PERMANENT = "permanent"   # other 4xx, malformed responses


class NonLocalTargetError(ValueError):
    pass


def ensure_loopback(base_url: str) -> str:
    parts = urllib.parse.urlsplit(base_url)
    if parts.scheme not in ("http", "https") or parts.hostname not in LOOPBACK_HOSTS:
        raise NonLocalTargetError(
            f"Refusing to target {base_url!r}: this tool only talks to a local mock "
            f"server ({', '.join(sorted(LOOPBACK_HOSTS))}).")
    return base_url.rstrip("/")


@dataclass(eq=False)
class ClientError(Exception):
    kind: str
    message: str
    status: int | None = None
    body: dict | None = None
    retry_after: float | None = None

    def __str__(self) -> str:
        return f"{self.kind}: {self.message}" + (f" (HTTP {self.status})" if self.status else "")


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None  # HTTP-date form not used by the mock


class MockSignupClient:
    def __init__(self, base_url: str, timeout: float = 15.0, network: str = "mainnet") -> None:
        self.base_url = ensure_loopback(base_url)
        self.timeout = timeout
        self.network = network
        # Never route loopback traffic through an environment proxy.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _request(self, method: str, path: str, body: dict | None = None,
                 token: str | None = None) -> tuple[int, dict, dict]:
        headers = {"accept": "application/json", "user-agent": "signupmock/1.0"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["content-type"] = "application/json"
        if token:
            headers["authorization"] = f"Bearer {token}"
        req = urllib.request.Request(self.base_url + path, data=data, headers=headers,
                                     method=method)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                status, raw, resp_headers = resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as e:
            status, raw, resp_headers = e.code, e.read(), dict(e.headers or {})
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            raise ClientError(RETRYABLE, f"{method} {path} failed: {e}")
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            payload = {"raw": raw[:500].decode(errors="replace")}
        return status, payload, {k.lower(): v for k, v in resp_headers.items()}

    def _checked(self, method: str, path: str, body: dict | None = None,
                 token: str | None = None, ok: tuple[int, ...] = ()) -> tuple[int, dict]:
        status, payload, headers = self._request(method, path, body, token)
        if 200 <= status < 300 or status in ok:
            return status, payload
        message = payload.get("error") or f"{method} {path} returned {status}"
        if status == 429:
            raise ClientError(THROTTLED, message, status, payload,
                              _parse_retry_after(headers.get("retry-after")))
        if status >= 500 or status == 408:
            raise ClientError(RETRYABLE, message, status, payload)
        raise ClientError(PERMANENT, message, status, payload)

    # API --------------------------------------------------------------------
    def sign_in(self, wallet: Wallet) -> str:
        _, ch = self._checked("POST", "/api/auth/challenge", {"address": wallet.address})
        message = ch.get("message")
        if not isinstance(message, str) or wallet.address not in message:
            raise ClientError(PERMANENT, "Challenge response is malformed", body=ch)
        signature = wallet.sign(message.encode())
        _, verified = self._checked("POST", "/api/auth/verify", {
            "address": wallet.address, "message": message,
            "signature": signature, "network": self.network})
        token = verified.get("token")
        if not isinstance(token, str) or not token:
            raise ClientError(PERMANENT, "Verify response has no token")
        return token

    def total(self) -> int | None:
        _, payload = self._checked("GET", "/api/signup/total")
        return payload.get("total")

    def register(self, wallet: Wallet, referral: str | None = None) -> dict:
        """Sign in, check for an existing signup, then POST only if needed.

        Returns {"outcome": "registered"|"already_registered", "status", "response"}.
        The pre-check GET makes a retry after a crash or lost response safe.
        """
        token = self.sign_in(wallet)
        _, existing = self._checked("GET", "/api/signup", token=token)
        if existing.get("signup"):
            return {"outcome": "already_registered", "status": 200, "response": existing}
        status, created = self._checked("POST", "/api/signup", {"referral": referral},
                                        token=token, ok=(409,))
        if status == 409:
            return {"outcome": "already_registered", "status": 409, "response": created}
        if not created.get("signup"):
            raise ClientError(PERMANENT, "Signup response has no signup record", status, created)
        return {"outcome": "registered", "status": status, "response": created}
