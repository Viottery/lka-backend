"""Portable checks for shipped client assets and their real Fetch/SSE helpers."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "app" / "web" / "distributed"


def test_assets_have_resolved_dom_hooks_and_local_links():
    html = (ASSETS / "client.html").read_text()
    script = (ASSETS / "client.js").read_text()
    ids = re.findall(r'\bid="([^"]+)"', html)
    assert len(ids) == len(set(ids))
    assert set(re.findall(r'\$\("([^"]+)"\)', script)) <= set(ids)
    assert 'href="/client/client.css"' in html
    assert 'src="/client/client.js"' in html
    assert '<html lang="zh-CN">' in html
    assert 'name="referrer" content="no-referrer"' in html
    assert "localStorage" not in script
    assert "innerHTML" not in script
    assert "Authorization" not in script
    assert "https://" not in html
    assert (ASSETS / "client.css").is_file()


def test_client_fetch_sse_and_connection_security_contracts():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    result = subprocess.run([node, "-e", r"""
const assert = require('node:assert/strict');
const {createApi, consumeSSE, safeNodeURL, MAX_FILE_BYTES} = require('./app/web/distributed/client.js');
(async () => {
  assert.equal(MAX_FILE_BYTES, 32*1024*1024);
  assert.equal(safeNodeURL('https://desk.example.ts.net', 'one-use'), 'https://desk.example.ts.net/client#grant=one-use');
  for (const origin of ['http://desk.example.ts.net','https://evil.example','https://a.ts.net.evil.example','https://user:secret@desk.example.ts.net','https://desk.example.ts.net/path']) assert.throws(()=>safeNodeURL(origin,'secret'));
  const calls=[];
  const api=createApi(async (path, options)=>{ calls.push({path,options}); return new Response(JSON.stringify({ok:true}),{headers:{'Content-Type':'application/json'}}); }, ()=>'csrf-only');
  await api('/sessions',{method:'POST',json:{title:'合成会话'}});
  assert.equal(calls[0].options.credentials,'same-origin');
  assert.equal(calls[0].options.headers.get('X-CSRF-Token'),'csrf-only');
  assert.equal(calls[0].options.headers.get('Authorization'),null);
  assert.equal(JSON.parse(calls[0].options.body).title,'合成会话');
  await assert.rejects(api('//evil.example/path'));
  await assert.rejects(api('https://evil.example/path'));
  const encoder = new TextEncoder();
  const raw=encoder.encode('event: final_answer\r\ndata: {"message":"中文结果"}\r\n\r\n');
  const events=[];
  const response=new Response(new ReadableStream({start(controller){for(let i=0;i<raw.length;i+=3) controller.enqueue(raw.slice(i,i+3));controller.close();}}),{headers:{'content-type':'text/event-stream'}});
  await consumeSSE(response,event=>events.push(event));
  assert.deepEqual(events,[{type:'final_answer',message:'中文结果'}]);
  await assert.rejects(consumeSSE(new Response('data: {"incomplete":true}',{headers:{'content-type':'text/event-stream'}}),()=>{}));
  await assert.rejects(consumeSSE(new Response('{}'),()=>{}));
  const controller=new AbortController();controller.abort();
  await assert.rejects(consumeSSE(new Response(new ReadableStream({start(c){c.close();}}),{headers:{'content-type':'text/event-stream'}}),()=>{},controller.signal),{name:'AbortError'});
})().catch(error=>{console.error(error);process.exitCode=1;});
"""], cwd=ROOT, capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
