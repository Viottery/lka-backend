"""Optional, bounded JS rendering with public, pinned, GET-only acquisition.

The browser never makes direct upstream connections. Routes are fulfilled by
Python's validated TLS transport; a dead proxy also denies unhandled traffic.
No user profile, cookies, login, clicks, form submission or arbitrary JS input.
"""

from __future__ import annotations

import asyncio
import hashlib
import http.client
import json
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from app.integrations.web_search import (
    PublicPageFetcher,
    WebSearchError,
    _PinnedHTTPSConnection,
    _public_address,
    _safe_public_https_url,
)

_SCRIPT = Path(__file__).with_suffix('.js').read_text(encoding='utf-8')
_REDIRECTS = {301, 302, 303, 307, 308}


def _fetch_resource(url: str, headers: dict, deadline: float) -> tuple[int, dict, bytes]:
    """One hop, 2 MB, no inherited proxy/auth/cookies; pin DNS before TLS."""
    parts = urlsplit(_safe_public_https_url(url))
    host = parts.hostname or ''
    address = _public_address(host)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise WebSearchError('Rendered fetch timed out.')
    conn = _PinnedHTTPSConnection(host, address, remaining)
    try:
        path = (parts.path or '/') + (f'?{parts.query}' if parts.query else '')
        conn.request('GET', path, headers={
            'Host': parts.netloc, 'User-Agent': 'LocalKnowledgeAgent/0.1',
            'Accept': headers.get('accept', '*/*'), 'Accept-Encoding': 'identity',
            **({'Origin': headers['origin']} if headers.get('origin') else {}),
        })
        response = conn.getresponse()
        result_headers = {k.lower(): v for k, v in response.getheaders()}
        if response.status in _REDIRECTS:
            return response.status, result_headers, b''
        if result_headers.get('content-encoding', 'identity').lower() != 'identity':
            raise WebSearchError('Compressed browser resources are not supported.')
        content = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WebSearchError('Rendered fetch timed out.')
            if conn.sock is not None:
                conn.sock.settimeout(remaining)
            chunk = response.read1(min(65536, 2_000_001 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > 2_000_000:
                raise WebSearchError('Browser resource exceeded the 2 MB byte limit.')
        return response.status, result_headers, bytes(content)
    except (OSError, http.client.HTTPException) as exc:
        raise WebSearchError(f'Browser resource fetch failed: {type(exc).__name__}.') from exc
    finally:
        conn.close()


def _offers(jsonld: list[str], microdata: list[dict]) -> tuple[list[dict], bool]:
    """Project explicit schema.org declarations; never infer stock from prices/UI."""
    queue = [(item, 'microdata') for item in microdata]
    truncated = False
    for raw in jsonld:
        try:
            queue.append((json.loads(raw), 'jsonld'))
        except (ValueError, RecursionError):
            truncated = True
    result = []
    visited = 0
    while queue and visited < 1000:
        value, source = queue.pop()
        visited += 1
        if isinstance(value, list):
            queue.extend((item, source) for item in value[:1000])
            truncated |= len(value) > 1000
        elif isinstance(value, dict):
            kind = value.get('@type')
            if kind == 'Offer' or isinstance(kind, list) and 'Offer' in kind:
                availability = str(value.get('availability', ''))[:500]
                token = availability.removeprefix('https://schema.org/').removeprefix('http://schema.org/')
                available = True if token in {'InStock', 'LimitedAvailability'} else (
                    False if token in {'SoldOut', 'OutOfStock'} else None)
                result.append({
                    'name': str(value.get('name', ''))[:500],
                    'price': str(value.get('price', ''))[:200],
                    'currency': str(value.get('priceCurrency', ''))[:20],
                    'available': available, 'availability': availability,
                    'evidence': source, 'scope': 'page_declared_offer_not_verified_live_inventory',
                })
                if len(result) == 40:
                    truncated |= bool(queue)
                    break
            children = list(value.values())[:1000]
            truncated |= len(value) > 1000
            queue.extend((child, source) for child in children if isinstance(child, (dict, list)))
    return result, truncated or bool(queue)


@dataclass
class RenderedPageFetcher:
    enabled: bool = False
    wait_for_selector: str | None = None
    settle_ms: int = 1000
    transport: object = _fetch_resource
    # Per runtime, one browser at a time; waiting uses the same acquisition budget.
    _slots: threading.BoundedSemaphore = field(default_factory=lambda: threading.BoundedSemaphore(1))

    def options(self, *, wait_for_selector=None, settle_ms=1000):
        if wait_for_selector is not None and (
            not isinstance(wait_for_selector, str) or not wait_for_selector.strip()
            or len(wait_for_selector) > 300
        ):
            raise ValueError('wait_for_selector must contain 1 through 300 characters.')
        if type(settle_ms) is not int or not 0 <= settle_ms <= 5000:
            raise ValueError('render_wait_ms must be an integer from 0 through 5000.')
        return replace(self, wait_for_selector=wait_for_selector, settle_ms=settle_ms)

    @property
    def cache_namespace(self):
        options = json.dumps([self.wait_for_selector, self.settle_ms])
        return 'rendered-dom-v1:' + hashlib.sha256(options.encode()).hexdigest() + ':'

    find_in_page = staticmethod(PublicPageFetcher.find_in_page)

    def read(self, url, *, deadline=None, check=None):
        if not self.enabled:
            raise WebSearchError('Browser rendering is disabled; enable [web_search].browser_enabled and install the browser extra plus Chromium.')
        url = _safe_public_https_url(url)
        check = check or (lambda: None)
        deadline = deadline if deadline is not None else time.monotonic() + 12
        while not self._slots.acquire(timeout=0.05):
            check()
            if time.monotonic() >= deadline:
                raise WebSearchError('Browser acquisition deadline exceeded.')
        try:
            check()
            return asyncio.run(self._render(url, deadline, check))
        finally:
            self._slots.release()

    async def _render(self, url, deadline, check):
        try:
            from playwright.async_api import Error, async_playwright
        except ImportError as exc:
            raise WebSearchError('Browser rendering requires the browser extra and Chromium; see docs/configuration.md.') from exc
        try:
            async with async_playwright() as playwright:
                browser = None
                try:
                    async with asyncio.timeout(max(0.001, deadline - time.monotonic())):
                        browser = await playwright.chromium.launch(
                            headless=True, chromium_sandbox=True,
                            proxy={'server': 'http://127.0.0.1:9', 'bypass': '<-loopback>'},
                            args=['--disable-quic', '--disable-background-networking',
                                  '--force-webrtc-ip-handling-policy=disable_non_proxied_udp'],
                        )
                        return await self._capture(browser, url, deadline, check)
                finally:
                    if browser is not None:
                        await asyncio.wait_for(browser.close(), timeout=3)
        except TimeoutError as exc:
            raise WebSearchError('Browser rendering timed out; no snapshot was published.') from exc
        except Error as exc:
            raise WebSearchError(f'Browser rendering failed (Chromium and its sandbox must be installed): {str(exc)[:300]}') from exc

    async def _capture(self, browser, url, deadline, check):
        context = await browser.new_context(service_workers='block', accept_downloads=False)
        blocked = {}
        state = {'requests': 0, 'bytes': 0, 'fatal': None}
        network = {'requested_url': url, 'redirects': [], 'redirect_count': 0,
                   'final_http_status': 200, 'response_headers': {},
                   'cache_origin': 'not_determined',
                   'scope': 'this_fetch_only_not_search_provider_diagnostics'}
        page = None

        def deny(reason):
            blocked[reason] = blocked.get(reason, 0) + 1

        async def fetch_chain(target, headers, *, document=False):
            # Fulfilled HTTP redirects can bypass Playwright routing. Resolve
            # them here instead; every hop still uses validated, pinned TLS.
            for hop in range(4):
                check()
                target = _safe_public_https_url(target)
                state['requests'] += 1
                if state['requests'] > 64:
                    raise WebSearchError('Browser exceeded the 64 request limit.')
                status, response_headers, content = await asyncio.to_thread(self.transport, target, headers, deadline)
                check()
                state['bytes'] += len(content)
                if len(content) > 2_000_000 or state['bytes'] > 16_000_000:
                    raise WebSearchError('Browser exceeded the resource byte limit.')
                if status not in _REDIRECTS:
                    return target, status, response_headers, content
                if hop == 3 or not response_headers.get('location'):
                    raise WebSearchError('Browser redirect limit exceeded or target missing.')
                destination = _safe_public_https_url(urljoin(target, response_headers['location']))
                if document:
                    network['redirects'].append({'url': target, 'status': status, 'target': destination})
                    network['redirect_count'] = len(network['redirects'])
                target = destination
            raise WebSearchError('Browser fetch did not produce a response.')

        # Navigate to the resolved main URL so relative JS/API URLs retain the
        # correct origin and path without asking Chromium to follow a redirect.
        initial = await fetch_chain(url, {}, document=True)
        navigation_url = initial[0]
        prefetched = {navigation_url: initial}

        async def route_request(route):
            request = route.request
            try:
                check()
                if request.method != 'GET':
                    deny('non_get')
                    await route.abort()
                    return
                if request.resource_type in {'image', 'media', 'font'}:
                    deny('omitted_media')
                    await route.abort()
                    return
                # Capture only the main document; no popups or embedded frames.
                if request.is_navigation_request() and (page is None or request.frame != page.main_frame):
                    deny('other_document')
                    await route.abort()
                    return
                target = _safe_public_https_url(request.url)
                if request.is_navigation_request() and target != navigation_url:
                    raise WebSearchError('Page navigated during capture; reopen its destination URL explicitly.')
                _, status, headers, content = prefetched.pop(target) if target in prefetched else await fetch_chain(target, request.headers)
                if request.is_navigation_request():
                    if status != 200:
                        raise WebSearchError(f'Rendered page returned HTTP {status}.')
                    mime = headers.get('content-type', '').split(';')[0].lower()
                    if mime not in {'text/html', 'application/xhtml+xml', 'text/plain'}:
                        raise WebSearchError('Unsupported rendered page content type.')
                    network['response_headers'] = {key: headers[key][:512] for key in (
                        'date', 'age', 'cache-control', 'etag', 'last-modified') if key in headers}
                # Drop cookies and transport-level headers. Browser receives public
                # content/CORS/CSP headers only, never upstream authentication state.
                allowed = {key: value for key, value in headers.items() if key in {
                    'content-type', 'location', 'content-security-policy',
                    'access-control-allow-origin', 'access-control-allow-methods',
                    'access-control-allow-headers', 'x-content-type-options',
                }}
                await route.fulfill(status=status, headers=allowed, body=content)
            except Exception as exc:  # noqa: BLE001 - route callbacks must abort on cancellation/authority errors too
                deny('policy_or_fetch_failure')
                if not isinstance(exc, WebSearchError) or (
                    request.is_navigation_request() and request.frame == page.main_frame
                ):
                    state['fatal'] = exc
                await route.abort()

        await context.route('**/*', route_request)

        async def block_socket(socket):
            deny('websocket')
            await socket.close()

        await context.route_web_socket('**/*', block_socket)
        page = await context.new_page()
        page.on('dialog', lambda dialog: dialog.dismiss())
        try:
            try:
                await page.goto(navigation_url, wait_until='domcontentloaded', timeout=max(1, (deadline - time.monotonic()) * 1000))
            except Exception:
                if state['fatal']:
                    raise state['fatal']
                raise
            if self.wait_for_selector:
                # CSS only: no Playwright selector extensions capable of actions.
                locator = page.locator('css=' + self.wait_for_selector).first
                while not await locator.is_visible():
                    check()
                    await page.wait_for_timeout(50)
            end = min(deadline, time.monotonic() + self.settle_ms / 1000)
            while time.monotonic() < end:
                check()
                await page.wait_for_timeout(50)
            check()
            if state['fatal']:
                raise state['fatal']
            final_url = _safe_public_https_url(page.url)
            data = await page.evaluate(_SCRIPT)
            check()
            offers, incomplete = _offers(data['jsonld'], data['microdata'])
            projection = data['projection']
            projection['offers'] = offers
            projection['limits']['structured_data'] |= incomplete
            text = data['text'] + '\n\n[DOM projection; declared offers are not verified live inventory]\n' + json.dumps(projection, ensure_ascii=False)
            acquisition_complete = not any(reason != 'omitted_media' for reason in blocked)
            complete = not (data['text_truncated'] or any(projection['limits'].values())
                            or not acquisition_complete)
            return {
                'url': final_url, 'fetched_at': datetime.now(UTC).isoformat(),
                'text': text, 'text_sha256': hashlib.sha256(text.encode()).hexdigest(),
                'title': data['title'], 'outline': [], 'projection': projection,
                'extraction_method': 'rendered_dom', 'rendered_visibility': 'rendered_main_document',
                'extraction_version': 'rendered-dom-v1', 'extraction_complete': complete,
                'text_scope': 'readable_text_extraction',
                'source_scope': 'bounded_rendered_dom_and_declared_offers_not_full_webpage',
                'network_observations': network,
                'render_observations': {**{k: v for k, v in state.items() if k != 'fatal'},
                    'acquisition_complete': acquisition_complete,
                    'blocked_requests': blocked, 'wait_ms': self.settle_ms,
                    'wait_for_selector': self.wait_for_selector,
                    'limitations': ['GET-only; no login, cookies, clicks or forms',
                        'Main document only; no iframe text or canvas/image interpretation',
                        'A timed DOM sample cannot prove inventory freshness or completeness']},
            }
        finally:
            await context.close()
