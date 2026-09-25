"""Adapter for the bundled mock signup page (mock_site/)."""

from __future__ import annotations

from playwright.sync_api import TimeoutError as PWTimeout

from .base import (ALREADY_REGISTERED, HUMAN_CHECK, PERMANENT, RATE_LIMITED, REGISTERED,
                   RETRYABLE, SignupAdapter, SignupError)


class MockSiteAdapter(SignupAdapter):
    def __init__(self, base_url: str, referral: str = "", timeout_ms: int = 30_000) -> None:
        super().__init__(base_url)
        self.referral = referral
        self.timeout_ms = timeout_ms

    def signup(self, page, address: str) -> str:
        page.set_default_timeout(self.timeout_ms)
        try:
            page.goto(f"{self.base_url}/signup", wait_until="networkidle")
            self.check_for_human_check(page)

            # Connect: pick our injected wallet from the page's wallet list.
            page.click("#connect")
            page.click(f"#wallet-list button[data-wallet='{self.wallet_name}']")
            page.wait_for_selector(f"#account[data-address='{address}']")

            # Fill and submit the form; the page asks the wallet to sign its challenge.
            if self.referral:
                page.fill("#referral", self.referral)
            page.click("#submit")
            result = page.wait_for_selector("#result[data-state]:not([data-state='busy'])")
            state = result.get_attribute("data-state")
            text = result.inner_text().strip()
        except PWTimeout as e:
            self.check_for_human_check(page)
            raise SignupError(RETRYABLE, f"Timed out waiting for the page: {e}")

        if state == "registered":
            return REGISTERED
        if state == "already_registered":
            return ALREADY_REGISTERED
        if state == "rate_limited":
            raise SignupError(RATE_LIMITED, text)
        if state == "human_check":
            raise SignupError(HUMAN_CHECK, text)
        if state == "rejected":
            raise SignupError(PERMANENT, text)
        raise SignupError(RETRYABLE, text or f"Unexpected page state {state!r}")

