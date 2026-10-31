"""Rendered acquisition stays separate from cheap find and scoped local continuation."""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from app.core.agent_storage import SqliteAgentRunStore
from app.core.tools import ToolExecutor
from app.integrations.web_search import WebSearchError
from app.integrations.web_snapshot import RenderedPageFetcher, _fetch_resource, _offers
from app.tool_packages.web import WebSnapshotTool
from tests.test_answer_generation_recovery_quality import make_loop, response, scope
from tests.test_web_snapshots import URL

pytest_plugins = ['tests.test_web_snapshots']


@dataclass
class StubRenderer(RenderedPageFetcher):
    calls: list = field(default_factory=list)

    def read(self, url, *, deadline=None, check=None):
        check()
        self.calls.append(url)
        if not self.enabled:
            raise WebSearchError('Browser rendering is disabled.')
        projection = {'offers': [
            {'name': 'Circle', 'price': '90', 'currency': 'SGD', 'available': False,
             'availability': 'https://schema.org/SoldOut', 'evidence': 'jsonld',
             'scope': 'page_declared_offer_not_verified_live_inventory'},
            {'name': 'Stalls', 'price': '120', 'currency': 'SGD', 'available': None,
             'availability': '', 'evidence': 'jsonld',
             'scope': 'page_declared_offer_not_verified_live_inventory'}],
            'controls': [{'label': 'Book', 'disabled': True}], 'rows': [], 'links': [],
            'scope': 'main_document_dom_not_pixels_or_full_inventory'}
        text = 'Circle: SoldOut. Stalls: price only, availability unknown.\n\n' + json.dumps(projection)
        return {'url': url, 'text': text, 'fetched_at': datetime.now(UTC).isoformat(),
                'text_sha256': hashlib.sha256(text.encode()).hexdigest(), 'outline': [],
                'projection': projection, 'render_observations': {'limitations': ['Not verified live inventory']},
                'extraction_method': 'rendered_dom', 'rendered_visibility': 'rendered_main_document',
                'text_scope': 'readable_text_extraction', 'extraction_complete': False,
                'network_observations': {'requested_url': url, 'redirects': [], 'redirect_count': 0,
                    'final_http_status': 200, 'response_headers': {}, 'cache_origin': 'not_determined',
                    'scope': 'this_fetch_only_not_search_provider_diagnostics'}}


@pytest.fixture
def rich(web):
    renderer = StubRenderer(enabled=True)
    web.registry.register_tool(WebSnapshotTool(renderer, web.resources))
    return renderer


def test_snapshot_and_http_cache_are_distinct_and_find_never_renders(web, rich):
    plain = web.call('web.find', {'url': URL, 'query': 'Original'})
    assert plain.status == 'completed' and rich.calls == []
    result = web.call('web.snapshot', {'url': URL})
    assert result.status == 'completed', result.error
    assert result.output['snapshot_id'] != plain.output['snapshot_id']
    assert result.output['extraction_complete'] is False
    assert result.output['projection']['offers'][1]['available'] is None
    assert len(rich.calls) == 1
    assert web.call('web.snapshot', {'url': URL}).output['cache_hit'] is True
    snapshot = result.output['snapshot_id']
    found = web.call('web.find', {'snapshot_id': snapshot, 'query': 'SoldOut'})
    assert found.status == 'completed' and found.output['matches']
    opened = web.call('web.open', {'snapshot_id': snapshot, 'offset': 0, 'max_chars': 20000})
    assert opened.status == 'completed' and 'availability unknown' in opened.output['text']
    assert opened.output['text_sha256'] == result.output['text_sha256']
    assert len(rich.calls) == 1 and len(web.calls) == 1
    refreshed = web.call('web.snapshot', {'url': URL, 'refresh': True})
    assert refreshed.status == 'completed' and refreshed.output['snapshot_id'] != snapshot
    assert len(rich.calls) == 2


def test_snapshot_authority_refresh_validation_and_disabled_browser(web, rich):
    result = web.call('web.snapshot', {'url': URL}).output
    snapshot = result['snapshot_id']
    assert web.call('web.snapshot', {'snapshot_id': snapshot, 'refresh': True}).status == 'failed'
    assert web.call('web.snapshot', {'snapshot_id': snapshot, 'render_wait_ms': 1}).status == 'failed'
    assert web.call('web.snapshot', {'url': URL, 'offset': -1}).status == 'rejected'
    assert web.call('web.snapshot', {'snapshot_id': snapshot}, web.context('other', 'other')).status == 'failed'
    assert web.call('web.snapshot', {'snapshot_id': snapshot}, web.context('s', 'child', child=True)).status == 'failed'
    assert len(rich.calls) == 1
    web.registry.register_effect_constraint('offline', blocked_domains=('external_read',))
    web.executor.constraint_store.add([('session', 's')], {'offline'})
    assert web.call('web.snapshot', {'url': URL, 'refresh': True}).status == 'rejected'
    assert web.call('web.snapshot', {'snapshot_id': snapshot}).status == 'completed'
    with pytest.raises(WebSearchError, match='disabled'):
        RenderedPageFetcher().read(URL)


def test_declared_offers_preserve_unknown_and_do_not_guess_from_price():
    values = [{'@type': 'Offer', 'name': 'Tier', 'price': '100', 'availability': value}
              for value in ['https://schema.org/InStock', 'http://schema.org/SoldOut',
                            'https://schema.org/PreOrder', '', 'https://evil.example/InStock']]
    offers, truncated = _offers([json.dumps({'@graph': values})], [])
    statuses = {row['availability']: row['available'] for row in offers}
    assert statuses == {'https://schema.org/InStock': True, 'http://schema.org/SoldOut': False,
                        'https://schema.org/PreOrder': None, '': None,
                        'https://evil.example/InStock': None}
    assert truncated is False
    assert _offers(['invalid JSON'], [])[1] is True


def test_browser_resource_transport_pins_dns_and_omits_credentials(monkeypatch):
    calls = []
    class Response:
        status = 200
        def getheaders(self):
            return [('content-type', 'application/json')]
        def read1(self, _):
            return b''
    class Connection:
        sock = None
        def __init__(self, host, address, timeout):
            calls.append((host, address))
        def request(self, method, path, *, headers):
            calls.append((method, path, headers))
        def getresponse(self):
            return Response()
        def close(self):
            calls.append('closed')
    monkeypatch.setattr('app.integrations.web_snapshot._public_address', lambda host: '8.8.8.8')
    monkeypatch.setattr('app.integrations.web_snapshot._PinnedHTTPSConnection', Connection)
    _fetch_resource('https://example.org/tiers', {'cookie': 'private', 'authorization': 'private',
                                                'accept': 'application/json'}, time.monotonic() + 10)
    assert calls[0] == ('example.org', '8.8.8.8')
    assert calls[1][0:2] == ('GET', '/tiers')
    assert 'cookie' not in {key.lower() for key in calls[1][2]}
    assert 'authorization' not in {key.lower() for key in calls[1][2]}
    assert calls[-1] == 'closed'


def test_rich_snapshot_survives_real_context_assembly(web, rich, tmp_path):
    result = web.call('web.snapshot', {'url': URL})
    assert result.status == 'completed', result.error
    loop, provider, manager = make_loop(tmp_path, [response('Circle is declared sold out; Stalls availability is unknown.')])
    loop.tool_executor = ToolExecutor(web.registry)
    store = SqliteAgentRunStore(tmp_path / 'answer.sqlite3')
    loop.tool_invocation_store = store
    with scope(manager) as run:
        store.put_artifact(artifact_id=f'tool_result_{result.invocation_id}', run_id=run.run_id,
            kind='tool_result', payload=result.model_dump(mode='json'), summary='rich webpage snapshot',
            created_at=datetime.now(UTC).isoformat())
        observation = loop._observation_for_decision_prompt(tool_name=result.tool_name,
            tool_input={'url': URL}, tool_result=result,
            feedback={'status': 'accepted', 'protocol_status': 'valid'}, run_id=run.run_id,
            force_gate=True)
        assert observation['_resource']['read_tool'] == 'web.open'
        assert observation['_resource']['find_tool'] == 'web.find'
        loop._answer_with_llm(user_input='Which ticket tiers remain?', route={}, context_window={},
                              observations=[observation], final_decision=None, llm_events=[])
    delivered = provider.requests[0].messages[1].content
    assert 'Circle' in delivered and 'SoldOut' in delivered and 'availability unknown' in delivered
    assert result.output['snapshot_id'] in delivered
    assert 'untrusted_data' in delivered
    assert 'Circle' not in provider.requests[0].messages[0].content


@pytest.mark.skipif(os.environ.get('WEB_SNAPSHOT_BROWSER_TEST') != '1', reason='opt-in installed Chromium smoke')
def test_real_browser_dynamic_dom_network_boundaries_and_deadline():
    calls = []
    html = b'''<html><head><script type="application/ld+json">{"@type":"Offer","name":"Circle","price":"90","availability":"https://schema.org/SoldOut"}</script></head>
    <body><p>Loading</p><script>
    fetch('https://127.0.0.1/private').catch(()=>{});
    fetch('https://8.8.8.8/write', {method:'POST'}).catch(()=>{});
    fetch('/tiers').then(r=>r.json()).then(data=> {
      document.body.insertAdjacentHTML('beforeend', '<table><tr><td id="tier">'+data.name+'</td><td>Sold out</td></tr></table><button disabled>Book</button><select><option>Stalls SGD120</option></select>');
    });
    </script></body></html>'''

    def transport(url, headers, deadline):
        calls.append(url)
        if url.endswith('/start'):
            return 302, {'location': '/event'}, b''
        if url.endswith('/tiers'):
            return 200, {'content-type': 'application/json'}, b'{"name":"Circle"}'
        return 200, {'content-type': 'text/html'}, html

    renderer = RenderedPageFetcher(enabled=True, settle_ms=100, wait_for_selector='#tier', transport=transport)
    start = time.monotonic()
    page = renderer.read('https://8.8.8.8/start')
    elapsed = time.monotonic() - start
    assert page['url'] == 'https://8.8.8.8/event'
    assert page['projection']['rows'][0]['cells'] == ['Circle', 'Sold out']
    assert page['projection']['offers'][0]['available'] is False
    assert page['projection']['controls'][0]['disabled'] is True
    assert not any('127.0.0.1' in url or '/write' in url for url in calls)
    assert page['network_observations']['redirect_count'] == 1
    assert page['render_observations']['blocked_requests']['non_get'] == 1
    assert page['extraction_complete'] is False
    print(f'Chromium synthetic cold capture: {elapsed:.3f}s; upstream requests={len(calls)}')
    with pytest.raises(WebSearchError, match='timed out'):
        renderer.options(wait_for_selector='#never', settle_ms=0).read(URL, deadline=time.monotonic() + 1.5)
    def failed_api(url, headers, deadline):
        if url.endswith('/tiers'):
            raise WebSearchError('Synthetic public API response exceeded its byte budget.')
        return 200, {'content-type': 'text/html'}, html
    partial = RenderedPageFetcher(enabled=True, settle_ms=100, transport=failed_api).read(URL)
    assert partial['extraction_complete'] is False
    assert partial['render_observations']['acquisition_complete'] is False
    assert partial['render_observations']['blocked_requests']['policy_or_fetch_failure'] >= 1
