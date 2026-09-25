"""Site adapter interface. Everything site-specific (URLs, selectors, result
parsing) lives in an adapter; the runner and browser layers stay generic."""

from __future__ import annotations

from abc import ABC, abstractmethod
from urllib.parse import urlsplit

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

# SignupError kinds; the runner decides what to do with each.
RETRYABLE = "retryable"        # timeouts, 5xx, flaky UI: retry later with backoff
RATE_LIMITED = "rate_limited"  # the site said slow down: pause the whole queue
HUMAN_CHECK = "human_check"    # CAPTCHA / human verification: stop, never solve it
PERMANENT = "permanent"        # rejected for this wallet: don't retry

# Outcomes an adapter returns on success.
REGISTERED = "registered"
ALREADY_REGISTERED = "already_registered"

# Selectors that indicate a human-verification challenge on common providers.
HUMAN_CHECK_SELECTORS = [
    "[data-captcha]",
    ".g-recaptcha", "iframe[src*='recaptcha']",
    ".h-captcha", "iframe[src*='hcaptcha']",
    ".cf-turnstile", "iframe[src*='challenges.cloudflare.com']",
]


class SignupError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def is_loopback_url(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme in ("http", "https") and parts.hostname in LOOPBACK_HOSTS


class SignupAdapter(ABC):
    #: Name the injected test wallet announces via Wallet Standard.
    wallet_name = "Suibot Test Wallet"

    def __init__(self, base_url: str) -> None:
        if not is_loopback_url(base_url):
            raise ValueError(
                f"Refusing {base_url!r}: adapters may only target a local mock "
                f"({', '.join(sorted(LOOPBACK_HOSTS))}).")
        self.base_url = base_url.rstrip("/")

    @property
    def origin(self) -> str:
        p = urlsplit(self.base_url)
        return f"{p.scheme}://{p.netloc}"

    @abstractmethod
    def signup(self, page, address: str) -> str:
        """Drive the page through connect -> sign -> submit for `address`.

        Return REGISTERED or ALREADY_REGISTERED, or raise SignupError.
        """

    def check_for_human_check(self, page) -> None:
        for sel in HUMAN_CHECK_SELECTORS:
            if page.locator(sel).count():
                raise SignupError(HUMAN_CHECK, f"Human verification present ({sel}); "
                                               "the bot does not solve CAPTCHAs")
