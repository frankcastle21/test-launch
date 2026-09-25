"""Headless Chromium with a Wallet Standard test wallet injected into the page.

The private key never enters the browser: the page's wallet calls back into
Python (via a Playwright binding) to sign, and Python signs only
`sui:signPersonalMessage` requests coming from the adapter's own origin. No
transaction-signing feature is exposed. Every request to a non-loopback host
is aborted.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

from .adapters.base import SignupAdapter, is_loopback_url
from .keys import Wallet

log = logging.getLogger("suibot")

# Minimal Wallet Standard wallet (https://github.com/wallet-standard/wallet-standard).
WALLET_JS = r"""
(() => {
  const cfg = __CFG__;
  const account = {
    address: cfg.address,
    publicKey: new Uint8Array(cfg.publicKey),
    chains: cfg.chains,
    features: ['sui:signPersonalMessage'],
    label: cfg.label,
  };
  const b64 = (bytes) => btoa(String.fromCharCode(...bytes));
  const wallet = {
    version: '1.0.0',
    name: cfg.name,
    icon: 'data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciLz4=',
    chains: cfg.chains,
    get accounts() { return [account]; },
    features: {
      'standard:connect': { version: '1.0.0', connect: async () => ({ accounts: [account] }) },
      'standard:disconnect': { version: '1.0.0', disconnect: async () => {} },
      'standard:events': { version: '1.0.0', on: () => () => {} },
      'sui:signPersonalMessage': {
        version: '1.1.0',
        signPersonalMessage: async ({ message, account: acct }) => {
          if (!acct || acct.address !== cfg.address) throw new Error('Unknown account');
          const bytes = b64(message);
          const signature = await window.__suibotSignPersonalMessage(bytes);
          return { bytes, signature };
        },
      },
    },
  };
  const register = (api) => api.register(wallet);
  window.addEventListener('wallet-standard:app-ready', (e) => register(e.detail));
  window.dispatchEvent(new CustomEvent('wallet-standard:register-wallet', { detail: register }));
})();
"""


def _origin(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


class WalletBrowser:
    def __init__(self, headless: bool = True, screenshots_dir: str | Path | None = None,
                 chains=("sui:mainnet",)) -> None:
        self.headless = headless
        self.screenshots_dir = Path(screenshots_dir) if screenshots_dir else None
        self.chains = list(chains)

    def run_signup(self, adapter: SignupAdapter, wallet: Wallet) -> str:
        """One isolated browser + context per attempt; nothing carries over between wallets."""
        blocked: list[str] = []
        signed: list[str] = []

        def guard(route):
            url = route.request.url
            if is_loopback_url(url):
                route.continue_()
            else:
                blocked.append(url)
                route.abort("blockedbyclient")

        def sign(source, message_b64: str) -> str:
            frame_url = source["frame"].url
            if _origin(frame_url) != adapter.origin:
                raise PermissionError(f"Refusing to sign for origin {_origin(frame_url)}")
            message = base64.b64decode(message_b64)
            signed.append(message[:80].decode(errors="replace"))
            log.info("signing personal message for %s: %r", wallet.label, signed[-1])
            return wallet.sign_personal_message(message)

        cfg = {"name": adapter.wallet_name, "address": wallet.address, "label": wallet.label,
               "publicKey": list(wallet.public_key), "chains": self.chains}

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            try:
                context = browser.new_context()
                context.route("**/*", guard)
                context.expose_binding("__suibotSignPersonalMessage", sign)
                context.add_init_script(WALLET_JS.replace("__CFG__", json.dumps(cfg)))
                page = context.new_page()
                try:
                    return adapter.signup(page, wallet.address)
                except Exception:
                    self._screenshot(page, wallet)
                    raise
            finally:
                if blocked:
                    log.warning("blocked %d non-local request(s), e.g. %s", len(blocked),
                                blocked[0])
                browser.close()

    def _screenshot(self, page, wallet: Wallet) -> None:
        if not self.screenshots_dir:
            return
        try:
            self.screenshots_dir.mkdir(parents=True, exist_ok=True)
            path = self.screenshots_dir / f"{int(time.time())}-{wallet.label}.png"
            page.screenshot(path=str(path), full_page=True)
            log.info("saved failure screenshot %s", path)
        except Exception as e:  # screenshots are best-effort
            log.debug("screenshot failed: %s", e)
