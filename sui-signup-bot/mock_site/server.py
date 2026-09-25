"""Local mock of a Sui wallet signup site: python -m mock_site [--port 8080]

Implements challenge -> signPersonalMessage -> verify -> signup, with checks a
real site might have so the bot's handling of them can be tested:
  * Origin check on every POST (cross-origin requests get 403)
  * Single-use, expiring challenges; real Sui personal-message signature verification
  * Optional global rate limit on signups (429)
  * Optional CAPTCHA mode (428 until a human solves it; the bot must stop)
  * --fail-rate injects 500s
Signups are kept in memory (or in --data JSON file if given).
"""

from __future__ import annotations

import argparse
import json
import random
import secrets
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from suibot.keys import verify_personal_message  # noqa: E402

INDEX = (Path(__file__).parent / "index.html").read_bytes()


class MockSite:
    def __init__(self, rate_limit_per_min=0, captcha=False, fail_rate=0.0, data_file=None):
        self.lock = threading.Lock()
        self.rate_limit_per_min = rate_limit_per_min
        self.captcha = captcha
        self.fail_rate = fail_rate
        self.data_file = Path(data_file) if data_file else None
        self.challenges: dict[str, tuple[str, float]] = {}
        self.tokens: dict[str, str] = {}
        self.signups: dict[str, dict] = {}
        self.recent: deque[float] = deque()
        self.posts = 0  # successful + duplicate POST /api/signup, for tests
        if self.data_file and self.data_file.exists():
            self.signups = json.loads(self.data_file.read_text())

    def _save(self):
        if self.data_file:
            self.data_file.write_text(json.dumps(self.signups, indent=1))

    def handle(self, method, path, headers, body, own_origins):
        with self.lock:
            if method == "POST" and headers.get("origin") not in own_origins:
                return 403, {"error": "Cross-origin request refused"}
            if path.startswith("/api/") and self.fail_rate and random.random() < self.fail_rate:
                return 500, {"error": "Injected server error"}

            if (method, path) == ("GET", "/api/config"):
                return 200, {"captcha": self.captcha}
            if (method, path) == ("GET", "/api/signup/total"):
                return 200, {"total": len(self.signups)}

            if (method, path) == ("POST", "/api/auth/challenge"):
                address = str(body.get("address", "")).lower()
                if not (address.startswith("0x") and len(address) == 66):
                    return 400, {"error": "Invalid address"}
                message = (f"Sign in to Mock Signup\nAddress: {address}\n"
                           f"Nonce: {secrets.token_hex(16)}\nIssued: {int(time.time())}")
                self.challenges[message] = (address, time.time() + 300)
                return 200, {"message": message}

            if (method, path) == ("POST", "/api/auth/verify"):
                address, message = str(body.get("address", "")).lower(), body.get("message", "")
                entry = self.challenges.pop(message, None)  # single use
                if not entry or entry[0] != address or entry[1] < time.time():
                    return 401, {"error": "Unknown or expired challenge"}
                if not verify_personal_message(address, message.encode(),
                                               body.get("signature", "")):
                    return 401, {"error": "Bad signature"}
                token = secrets.token_urlsafe(24)
                self.tokens[token] = address
                return 200, {"token": token}

            if path == "/api/signup":
                auth = headers.get("authorization", "")
                address = self.tokens.get(auth.removeprefix("Bearer "))
                if not address:
                    return 401, {"error": "Not signed in"}
                if method == "GET":
                    return 200, {"signup": self.signups.get(address)}
                self.posts += 1
                if address in self.signups:
                    return 409, {"error": "Already signed up"}
                if self.captcha and body.get("captcha") != "solved-by-human":
                    return 428, {"error": "Human verification required"}
                now = time.time()
                while self.recent and now - self.recent[0] > 60:
                    self.recent.popleft()
                if self.rate_limit_per_min and len(self.recent) >= self.rate_limit_per_min:
                    return 429, {"error": "Too many signups right now. Try again later."}
                self.recent.append(now)
                self.signups[address] = {"address": address, "createdAt": int(now),
                                         "referral": body.get("referral")}
                self._save()
                return 200, {"signup": self.signups[address], "total": len(self.signups)}

            return 404, {"error": "Not found"}


def make_server(site: MockSite, host="127.0.0.1", port=8080) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            if not getattr(self.server, "quiet", False):
                sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

        def _send(self, status, payload, ctype="application/json"):
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _handle(self, method):
            path = urlsplit(self.path).path
            if method == "GET" and path in ("/", "/signup"):
                return self._send(200, INDEX, "text/html; charset=utf-8")
            body = {}
            if int(self.headers.get("content-length") or 0):
                try:
                    body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                except ValueError:
                    return self._send(400, {"error": "Bad JSON"})
            port_ = self.server.server_address[1]
            own = {f"http://127.0.0.1:{port_}", f"http://localhost:{port_}"}
            headers = {k.lower(): v for k, v in self.headers.items()}
            self._send(*site.handle(method, path, headers, body or {}, own))

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

    return ThreadingHTTPServer((host, port), Handler)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1", "localhost"])
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--rate-limit-per-min", type=int, default=0)
    p.add_argument("--captcha", action="store_true")
    p.add_argument("--fail-rate", type=float, default=0.0)
    p.add_argument("--data", help="persist signups to this JSON file")
    args = p.parse_args(argv)
    site = MockSite(args.rate_limit_per_min, args.captcha, args.fail_rate, args.data)
    httpd = make_server(site, args.host, args.port)
    print(f"Mock signup site: http://{args.host}:{args.port}/signup", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
