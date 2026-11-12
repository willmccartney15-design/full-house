"""Polite page fetching: robots.txt, per-host rate limits, a 24h disk cache.

``Fetcher`` uses httpx for plain pages and Playwright (Chromium) for pages that
need JavaScript (OTA listings and search results). ``FixtureFetcher`` serves
local HTML files instead, for dry runs and tests.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from fullhouse.core.config import Settings

log = logging.getLogger(__name__)


class FetchError(RuntimeError):
    pass


class RobotsDisallowed(FetchError):
    pass


@dataclass
class Page:
    url: str
    status: int
    html: str
    elapsed_ms: float
    from_cache: bool = False


class Fetcher:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._robots: dict[str, RobotFileParser] = {}
        self._last_hit: dict[str, float] = {}
        self._browser = None
        self._pw = None

    # ------------------------------------------------------------------ public

    def get(self, url: str, *, render: bool = False, use_cache: bool = True) -> Page:
        if use_cache and (cached := self._cache_read(url, render)):
            return cached
        if not self.allowed(url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")
        self._throttle(url)
        page = self._render(url) if render else self._http(url)
        if page.status < 400:
            self._cache_write(url, render, page)
        return page

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            rp = RobotFileParser()
            try:
                resp = httpx.get(f"{origin}/robots.txt", headers={"User-Agent": self.settings.user_agent}, timeout=15)
                if resp.status_code >= 400:
                    rp.parse([])  # no robots.txt: everything allowed
                else:
                    rp.parse(resp.text.splitlines())
            except httpx.HTTPError as exc:
                log.warning("robots.txt unreachable for %s (%s); treating as disallowed", origin, exc)
                rp.parse(["User-agent: *", "Disallow: /"])
            self._robots[origin] = rp
        return self._robots[origin].can_fetch(self.settings.user_agent, url)

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
        if self._pw is not None:
            self._pw.stop()
        self._browser = self._pw = None

    # ---------------------------------------------------------------- internals

    def _throttle(self, url: str) -> None:
        host = urlsplit(url).netloc
        wait = self._last_hit.get(host, 0) + self.settings.min_seconds_between_requests - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_hit[host] = time.monotonic()

    def _http(self, url: str) -> Page:
        start = time.perf_counter()
        try:
            resp = httpx.get(
                url,
                headers={"User-Agent": self.settings.user_agent, "Accept-Language": "en-AU,en;q=0.9"},
                follow_redirects=True,
                timeout=30,
            )
        except httpx.HTTPError as exc:
            raise FetchError(f"{url}: {exc}") from exc
        return Page(url=str(resp.url), status=resp.status_code, html=resp.text, elapsed_ms=_ms(start))

    def _render(self, url: str) -> Page:
        if self._browser is None:
            from playwright.sync_api import sync_playwright

            self._pw = sync_playwright().start()
            launch = {"headless": True}
            if self.settings.chromium_path:
                launch["executable_path"] = self.settings.chromium_path
            try:
                self._browser = self._pw.chromium.launch(**launch)
            except Exception as exc:
                self.close()
                raise FetchError(
                    f"Chromium did not start ({str(exc).splitlines()[0]}). Run `uv run playwright install chromium` "
                    "or set FULLHOUSE_CHROMIUM_PATH to an installed Chrome/Chromium."
                ) from exc
        context = self._browser.new_context(user_agent=self.settings.user_agent, locale="en-AU")
        try:
            page = context.new_page()
            start = time.perf_counter()
            resp = page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(2_000)  # let client-side rendering settle
            return Page(url=page.url, status=resp.status if resp else 0, html=page.content(), elapsed_ms=_ms(start))
        finally:
            context.close()

    def _cache_path(self, url: str, render: bool) -> Path:
        key = hashlib.sha256(f"{render}:{url}".encode()).hexdigest()
        return self.settings.cache_dir / f"{key}.json"

    def _cache_read(self, url: str, render: bool) -> Page | None:
        path = self._cache_path(url, render)
        if not path.exists():
            return None
        if time.time() - path.stat().st_mtime > self.settings.cache_ttl_hours * 3600:
            return None
        data = json.loads(path.read_text())
        return Page(**data, from_cache=True)

    def _cache_write(self, url: str, render: bool, page: Page) -> None:
        data = {"url": page.url, "status": page.status, "html": page.html, "elapsed_ms": page.elapsed_ms}
        self._cache_path(url, render).write_text(json.dumps(data))


class FixtureFetcher:
    """Serves local files for known URLs. ``routes`` maps a URL prefix to a file path."""

    def __init__(self, routes: dict[str, Path]):
        self.routes = routes

    def get(self, url: str, *, render: bool = False, use_cache: bool = True) -> Page:
        for prefix in sorted(self.routes, key=len, reverse=True):
            if url.startswith(prefix):
                return Page(url=url, status=200, html=Path(self.routes[prefix]).read_text(), elapsed_ms=420.0)
        return Page(url=url, status=404, html="", elapsed_ms=0.0)

    def allowed(self, url: str) -> bool:
        return True

    def close(self) -> None:
        pass


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)
