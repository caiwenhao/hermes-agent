"""Playwright-over-CDP backend for Hermes browser tools.

This module provides a minimal execution backend that connects to an already
running Chrome/Chromium instance via the Chrome DevTools Protocol websocket.
It is intended as a fallback/alternative to the agent-browser CLI path when
Hermes is attached to a live local browser session (for example via
``/browser connect`` setting ``BROWSER_CDP_URL``).
"""

from __future__ import annotations

import tempfile
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse


def _lazy_sync_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - exercised via unit tests
        raise RuntimeError(
            "Playwright backend requested but the Python 'playwright' package is not installed. "
            "Install it with: pip install playwright && python -m playwright install chromium"
        ) from exc
    return sync_playwright


class PlaywrightCdpSession:
    """Small wrapper around a Playwright CDP browser connection."""

    def __init__(self, cdp_url: str):
        sync_playwright = _lazy_sync_playwright()
        self._pw_cm = sync_playwright()
        self._pw = self._pw_cm.start()
        self.browser = self._pw.chromium.connect_over_cdp(cdp_url)
        contexts = list(self.browser.contexts)
        self.context = contexts[0] if contexts else self.browser.new_context()
        pages = list(self.context.pages)
        self.page = pages[0] if pages else self.context.new_page()

    def close(self) -> None:
        try:
            self.browser.close()
        finally:
            self._pw_cm.stop()


def _ensure_page(session: PlaywrightCdpSession):
    if session.page.is_closed():
        pages = list(session.context.pages)
        session.page = pages[0] if pages else session.context.new_page()
    return session.page


def _normalize_url_for_match(url: str) -> str:
    parsed = urlparse((url or "").strip())
    if not parsed.scheme or not parsed.netloc:
        return (url or "").strip().rstrip("/")
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def _score_existing_page(page, target_url: str) -> int:
    score = 0
    current_url = getattr(page, "url", "") or ""
    normalized_current = _normalize_url_for_match(current_url)
    normalized_target = _normalize_url_for_match(target_url)
    if normalized_current and normalized_current == normalized_target:
        score += 100
    elif normalized_target and normalized_current.startswith(normalized_target):
        score += 75
    elif normalized_target and normalized_target in normalized_current:
        score += 40

    if current_url and current_url not in {"about:blank", "chrome://newtab/"}:
        score += 10

    try:
        title = page.title() or ""
    except Exception:
        title = ""
    if title:
        score += 5
    return score


def _pick_best_page(session: PlaywrightCdpSession, target_url: Optional[str] = None):
    pages = [p for p in list(session.context.pages) if not p.is_closed()]
    if not pages:
        session.page = session.context.new_page()
        return session.page

    if target_url:
        ranked = sorted(pages, key=lambda p: _score_existing_page(p, target_url), reverse=True)
        best = ranked[0]
        if _score_existing_page(best, target_url) > 0:
            session.page = best
            return best

    non_blank = [p for p in pages if (getattr(p, "url", "") or "") not in {"", "about:blank", "chrome://newtab/"}]
    if non_blank:
        session.page = non_blank[0]
        return session.page

    session.page = pages[0]
    return session.page


def _compact_snapshot(page) -> Dict[str, Any]:
    refs = page.evaluate(
        """() => {
            const selectors = [
              'a[href]', 'button', 'input', 'textarea', 'select',
              '[role="button"]', '[role="link"]', '[tabindex]'
            ];
            const elements = Array.from(document.querySelectorAll(selectors.join(',')));
            const refs = {};
            const lines = [];
            let idx = 1;
            for (const el of elements) {
              if (!(el instanceof HTMLElement)) continue;
              const style = window.getComputedStyle(el);
              if (style.display === 'none' || style.visibility === 'hidden') continue;
              const ref = '@e' + idx++;
              el.setAttribute('data-hermes-ref', ref);
              const role = (el.getAttribute('role') || el.tagName || '').toLowerCase();
              const text = (el.innerText || el.value || el.getAttribute('aria-label') || el.textContent || '').trim().replace(/\\s+/g, ' ');
              refs[ref] = {
                role,
                text,
                tag: el.tagName.toLowerCase(),
              };
              lines.push(`[${ref}] ${role || el.tagName.toLowerCase()} ${text}`.trim());
            }
            return { snapshot: lines.join('\\n'), refs };
        }"""
    )
    return refs


def run_playwright_cdp_command(
    command: str,
    args: List[str],
    *,
    cdp_url: str,
    session_store: Dict[str, Any],
    task_id: str,
) -> Dict[str, Any]:
    """Execute a browser command against a live CDP browser using Playwright."""
    session = session_store.get(task_id)
    if session is None:
        session = PlaywrightCdpSession(cdp_url)
        session_store[task_id] = session

    if command in {"open", "navigate"}:
        target = args[0]
        page = _pick_best_page(session, target)
        current_url = getattr(page, "url", "") or ""
        if _normalize_url_for_match(current_url) == _normalize_url_for_match(target):
            try:
                page.bring_to_front()
            except Exception:
                pass
            return {"success": True, "data": {"title": page.title(), "url": page.url}}
        page.goto(target, wait_until="domcontentloaded", timeout=60000)
        return {"success": True, "data": {"title": page.title(), "url": page.url}}

    page = _ensure_page(session)

    if command == "snapshot":
        data = _compact_snapshot(page)
        return {"success": True, "data": data}

    if command == "click":
        ref = args[0]
        locator = page.locator(f'[data-hermes-ref="{ref}"]')
        locator.first.click(timeout=10000)
        return {"success": True, "data": {}}

    if command == "fill":
        ref, text = args[0], args[1]
        locator = page.locator(f'[data-hermes-ref="{ref}"]')
        locator.first.fill(text, timeout=10000)
        return {"success": True, "data": {}}

    if command == "scroll":
        direction = args[0] if args else "down"
        delta = 700 if direction == "down" else -700
        page.evaluate("(delta) => window.scrollBy(0, delta)", delta)
        return {"success": True, "data": {}}

    if command == "back":
        page.go_back(wait_until="domcontentloaded", timeout=30000)
        return {"success": True, "data": {"url": page.url, "title": page.title()}}

    if command == "press":
        key = args[0]
        page.keyboard.press(key)
        return {"success": True, "data": {}}

    if command == "eval":
        expression = args[0]
        value = page.evaluate(expression)
        return {"success": True, "data": {"result": value}}

    if command == "evaluate":
        expression = args[0]
        value = page.evaluate(expression)
        return {"success": True, "data": {"result": value}}

    if command == "console":
        return {"success": True, "data": {"messages": [], "errors": []}}

    if command == "screenshot":
        tmp = tempfile.NamedTemporaryFile(prefix="hermes-playwright-", suffix=".png", delete=False)
        tmp.close()
        path = tmp.name
        page.screenshot(path=path, full_page=False)
        return {"success": True, "data": {"path": path}}

    if command == "close":
        session.close()
        session_store.pop(task_id, None)
        return {"success": True, "data": {}}

    return {"success": False, "error": f"Playwright CDP backend does not support command '{command}'"}
