"""Local stand-in for the Perpsplexity Phase 1 signup API.

Endpoint shapes mirror what the public frontend bundle calls:

    POST /api/auth/challenge  {address}                          -> {message}
    POST /api/auth/verify     {address, message, signature, network}
                                                                 -> {token, expiresAt, created, profile}
    GET  /api/signup          (Bearer)                           -> {signup | null}
    POST /api/signup          (Bearer) {referral}                -> {signup, total}
    GET  /api/signup/total                                        -> {total}

The real server's behaviour for duplicates, rate limits and signature format is
not published; the choices here (409 on duplicate POST, single-use challenges,
optional 429s) are assumptions made so the client's handling can be exercised.
"""

from __future__ import annotations

import json
import random
import secrets
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .wallets import is_valid_address, verify_signature

CHALLENGE_TTL = 300
TOKEN_TTL = 24 * 3600


@dataclass
class MockConfig:
    fail_rate: float = 0.0        # probability of an injected 500 on /api/* calls
    throttle_rate: float = 0.0    # probability of an injected 429 on /api/* calls
    retry_after: int = 60         # Retry-After seconds sent with 429s
    rate_limit_per_min: int = 0   # 0 = off; global fixed-window limit on /api/*
    latency: float = 0.0          # seconds of artificial latency per request
    seed: int | None = None


@dataclass
class MockState:
    config: MockConfig = field(default_factory=MockConfig)

    def __post_init__(self) -> None:
        self.lock = threading.Lock()
        self.rng = random.Random(self.config.seed)
        self.challenges: dict[str, tuple[str, float]] = {}   # message -> (address, expiry)
        self.tokens: dict[str, tuple[str, float]] = {}       # token -> (address, expiry)
        self.signups: dict[str, dict] = {}
        self.forced: list[tuple[int, dict, dict]] = []       # queued (status, body, headers)
        self.requests: list[tuple[str, str]] = []
        self._window_start = 0.0
        self._window_count = 0

    # Test hooks -------------------------------------------------------------
    def force_next(self, status: int, body: dict | None = None,
                   headers: dict | None = None, times: int = 1) -> None:
        with self.lock:
            for _ in range(times):
                self.forced.append((status, body or {"error": f"Injected {status}"}, headers or {}))

    def preregister(self, address: str) -> None:
        with self.lock:
            self._create_signup(address)

    def signup_posts(self) -> int:
        with self.lock:
            return sum(1 for m, p in self.requests if m == "POST" and p == "/api/signup")

    # Internals --------------------------------------------------------------
    def _create_signup(self, address: str) -> dict:
        signup = {"address": address, "createdMs": int(time.time() * 1000),
                  "referredBy": None, "boost": 1, "invited": 0, "codeBoost": 1}
        self.signups[address] = signup
        return signup

    def _injected(self) -> tuple[int, dict, dict] | None:
        if self.forced:
            return self.forced.pop(0)
        cfg = self.config
        if cfg.rate_limit_per_min > 0:
            now = time.monotonic()
            if now - self._window_start >= 60:
                self._window_start, self._window_count = now, 0
            self._window_count += 1
            if self._window_count > cfg.rate_limit_per_min:
                wait = max(1, int(60 - (now - self._window_start)))
                return 429, {"error": "Too many requests"}, {"Retry-After": str(wait)}
        roll = self.rng.random()
        if roll < cfg.throttle_rate:
            return 429, {"error": "Too many requests"}, {"Retry-After": str(cfg.retry_after)}
        if roll < cfg.throttle_rate + cfg.fail_rate:
            return 500, {"error": "Sign-up is unavailable right now."}, {}
        return None

    def _auth(self, header: str | None) -> str | None:
        if not header or not header.startswith("Bearer "):
            return None
        entry = self.tokens.get(header[len("Bearer "):])
        if not entry or entry[1] < time.time():
            return None
        return entry[0]

    def handle(self, method: str, path: str, headers, body: dict | None):
        with self.lock:
            self.requests.append((method, path))
            if path.startswith("/api/"):
                injected = self._injected()
                if injected:
                    return injected

            if method == "POST" and path == "/api/auth/challenge":
                address = (body or {}).get("address", "")
                if not is_valid_address(address):
                    return 400, {"error": "Invalid address"}, {}
                message = (f"perpsplexity-mock wants you to sign in.\n"
                           f"Address: {address}\nNonce: {secrets.token_hex(16)}\n"
                           f"Issued: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
                self.challenges[message] = (address, time.time() + CHALLENGE_TTL)
                return 200, {"message": message}, {}

            if method == "POST" and path == "/api/auth/verify":
                body = body or {}
                address, message = body.get("address", ""), body.get("message", "")
                entry = self.challenges.pop(message, None)  # single use
                if not entry or entry[0] != address or entry[1] < time.time():
                    return 401, {"error": "Challenge expired or unknown. Try again."}, {}
                if not verify_signature(address, message.encode(), body.get("signature", "")):
                    return 401, {"error": "Signature does not match this wallet."}, {}
                token = secrets.token_urlsafe(32)
                expires = time.time() + TOKEN_TTL
                self.tokens[token] = (address, expires)
                now_ms = int(time.time() * 1000)
                profile = {"address": address, "username": "u" + address[2:10], "bio": "",
                           "avatarUrl": None, "bannerUrl": None, "twitter": "", "website": "",
                           "loginKind": "wallet", "createdMs": now_ms, "updatedMs": now_ms}
                return 200, {"token": token, "expiresAt": int(expires * 1000),
                             "created": True, "profile": profile}, {}

            if path == "/api/signup/total" and method == "GET":
                return 200, {"total": len(self.signups)}, {}

            if path == "/api/signup":
                address = self._auth(headers.get("authorization"))
                if not address:
                    return 401, {"error": "Sign in first."}, {}
                if method == "GET":
                    return 200, {"signup": self.signups.get(address)}, {}
                if method == "POST":
                    if address in self.signups:
                        return 409, {"error": "This wallet is already signed up.",
                                     "signup": self.signups[address]}, {}
                    signup = self._create_signup(address)
                    return 200, {"signup": signup, "total": len(self.signups)}, {}

            return 404, {"error": "Not found"}, {}


def make_handler(state: MockState):
    class Handler(BaseHTTPRequestHandler):
        server_version = "signupmock/1.0"

        def log_message(self, fmt, *args):  # keep test output quiet
            pass

        def _dispatch(self, method: str) -> None:
            if state.config.latency:
                time.sleep(state.config.latency)
            body = None
            length = int(self.headers.get("content-length") or 0)
            if length:
                try:
                    body = json.loads(self.rfile.read(length))
                except ValueError:
                    return self._send(400, {"error": "Invalid JSON"}, {})
            status, payload, extra = state.handle(
                method, urlsplit(self.path).path,
                {k.lower(): v for k, v in self.headers.items()}, body)
            self._send(status, payload, extra)

        def _send(self, status: int, payload: dict, extra: dict) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            for k, v in extra.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

    return Handler


class MockServer:
    """Runs the mock API on a background thread (used by tests and `serve`)."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0,
                 config: MockConfig | None = None) -> None:
        self.state = MockState(config or MockConfig())
        self.httpd = ThreadingHTTPServer((host, port), make_handler(self.state))
        self.thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> "MockServer":
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
