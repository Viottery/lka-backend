/* Synthetic browser exercise for explicit shared-memory edits; no live hub/model. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright-core');
const ROOT = path.resolve(__dirname, '../app/web/distributed');
const csrf = 'synthetic-memory-csrf';
const errors = [], writes = [], outbox = [];
let unavailable = false;
const original = {memory_id:'cloud_original',content:'偏好简短回复',version:1,memory_type:'preference',sensitivity:'normal',scope:'global',project_id:null,metadata:{origin:'cloud_replica',shared_memory_id:'hub-original'}};
let records = [original];
const json = (response, status, value) => {response.writeHead(status,{'content-type':'application/json; charset=utf-8'});response.end(JSON.stringify(value));};
const server = http.createServer(async (request,response) => {
  try {
    const url = new URL(request.url, 'http://127.0.0.1'), p = url.pathname;
    if (p === '/favicon.ico') { response.writeHead(204);return response.end(); }
    if (['/client','/client/client.js','/client/client.css'].includes(p)) {
      const name = p === '/client' ? 'client.html' : path.basename(p);
      response.writeHead(200,{'content-type':name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html; charset=utf-8'});
      return response.end(fs.readFileSync(path.join(ROOT,name)));
    }
    if (p === '/client/info') return json(response,200,{role:'replica',node_id:'desk',connections_enabled:true});
    if (p === '/connections/me') return json(response,200,{node_id:'desk',session_id:'human-session',user_id:'owner',authorization_epoch:1,workspace_ids:['work'],csrf_token:csrf});
    if (p === '/connections/sessions') return json(response,200,{items:[]});
    if (p === '/sessions') return json(response,200,{sessions:[]});
    if (p === '/local/workspaces') return json(response,200,{items:[{workspace_id:'work'}]});
    if (p === '/local/workspaces/work/entries') return json(response,200,{items:[]});
    if (p === '/local/cache') return json(response,200,{available:true,stale:true,age_seconds:500,reads_allow_stale:true,cursor:3});
    if (p === '/local/shared-memory/status') return json(response,unavailable?404:200,unavailable?{detail:'disabled'}:{write_enabled:true,cache:{stale:true},queued:outbox.filter(x=>x.status==='queued').length,conflict:outbox.filter(x=>x.status==='conflict').length,rejected:outbox.filter(x=>x.status==='rejected').length});
    if (p === '/local/shared-memory/records') return json(response,200,{memories:url.searchParams.get('scope')==='project'?[]:records,bound:true});
    if (p === '/local/shared-memory/mutations' && request.method === 'GET') return json(response,200,{items:outbox});
    if (p === '/local/shared-memory/mutations' && request.method === 'POST') {
      assert.equal(request.headers['x-csrf-token'],csrf);
      assert.equal(request.headers.authorization,undefined);
      let raw=''; for await (const chunk of request) raw+=chunk;
      const body=JSON.parse(raw); assert.equal(body.confirmed,true); assert.equal(body.consent,undefined);
      writes.push(body);
      const item={mutation_id:body.mutation_id,status:'queued',proposal:{...body,hub_id:'hub-test',memory_id:body.memory_id==='cloud_current'?'hub-current':'hub-original'},receipt:null};
      outbox.unshift(item);return json(response,201,item);
    }
    throw new Error(`Unexpected API ${request.method} ${p}`);
  } catch(error) { errors.push(error.stack);json(response,500,{detail:'mock failure'}); }
});
(async()=>{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
  try {
    const page=await browser.newPage({viewport:{width:390,height:844}});
    page.on('pageerror',error=>errors.push(error.stack));
    await page.goto(`http://127.0.0.1:${server.address().port}/client`);
    await page.locator('button[data-panel="resources"]').click();
    await page.locator('#shared-memory-panel').waitFor({state:'visible'});
    await page.locator('#shared-memory-records').getByText('偏好简短回复').waitFor();
    assert.equal((await page.locator('#shared-memory-records').textContent()).includes('版本 1'),true);
    await page.locator('#shared-memory-content').fill('我偏好 <img src=x onerror=alert(1)> 文本');
    await page.locator('#submit-shared-memory').click();assert.equal(writes.length,0,'human checkbox is required');
    await page.locator('#shared-memory-confirm').check();await page.locator('#submit-shared-memory').click();
    await page.waitForFunction(()=>document.querySelector('#shared-memory-outbox').textContent.includes('本机待提交'));
    assert.equal(writes[0].operation,'create');assert.equal(writes[0].expected_version,0);
    assert.equal(await page.locator('#shared-memory-outbox img').count(),0,'proposal renders as text');
    await page.locator('#shared-memory-records').getByRole('button',{name:'更正',exact:true}).click();
    await page.locator('#shared-memory-content').fill('我偏好附带完整推导');
    await page.locator('#shared-memory-confirm').check();await page.locator('#submit-shared-memory').click();
    await page.waitForFunction(()=>document.querySelectorAll('#shared-memory-outbox article').length===2);
    assert.equal(writes[1].memory_id,'cloud_original');assert.equal(writes[1].expected_version,1);
    outbox[0].status='conflict';outbox[0].receipt={current:{memory_id:'hub-original',status:'superseded',version:2,replacement:{memory_id:'hub-current',version:1,status:'active',content:'另一台电脑的更正'}}};
    records=[{...original,memory_id:'cloud_current',content:'另一台电脑的更正',metadata:{origin:'cloud_replica',shared_memory_id:'hub-current'}}];
    await page.locator('#refresh-shared-memory').click();
    await page.getByRole('button',{name:'核对当前版本并重新编辑'}).click();
    await page.waitForFunction(()=>document.querySelector('#shared-memory-content').value==='我偏好附带完整推导');
    assert.equal(writes.length,2,'rebase must not submit automatically');
    assert.equal(await page.locator('#shared-memory-confirm').isChecked(),false);
    assert.equal(await page.locator('#shared-memory-content').inputValue(),'我偏好附带完整推导');
    await page.locator('#shared-memory-confirm').check();await page.locator('#submit-shared-memory').click();
    await page.waitForFunction(()=>document.querySelectorAll('#shared-memory-outbox article').length===3);
    assert.equal(writes[2].memory_id,'cloud_current');assert.notEqual(writes[2].mutation_id,writes[1].mutation_id);
    await page.locator('#shared-memory-records').getByRole('button',{name:'删除',exact:true}).click();
    assert.equal(await page.locator('#shared-memory-confirm').isChecked(),false);
    await page.locator('#shared-memory-confirm').check();await page.locator('#submit-shared-memory').click();
    await page.waitForFunction(()=>document.querySelectorAll('#shared-memory-outbox article').length===4);
    assert.equal(writes[3].operation,'delete');assert.equal(writes[3].content,null);
    outbox[3].status='rejected';outbox[3].proposal.operation='create';
    await page.locator('#refresh-shared-memory').click();await page.getByRole('button',{name:'检查权限后按原提案重新编辑'}).click();
    await page.waitForFunction(()=>document.querySelector('#shared-memory-content').value.includes('<img'));
    assert.equal(writes.length,4);assert.equal(await page.locator('#shared-memory-confirm').isChecked(),false);
    await page.locator('#shared-memory-scope').selectOption('project');
    await page.locator('#shared-memory-records').getByText('此范围暂时没有缓存的共享记忆。').waitFor();
    await page.locator('#shared-memory-content').fill('项目采用显式版本核对');
    await page.locator('#shared-memory-confirm').check();await page.locator('#submit-shared-memory').click();
    await page.waitForFunction(()=>document.querySelectorAll('#shared-memory-outbox article').length===5);
    assert.equal(writes[4].scope,'project');assert.equal(writes[4].workspace_id,'work');
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,'mobile must not overflow');
    unavailable=true;await page.locator('#refresh-shared-memory').click();await page.locator('#shared-memory-panel').waitFor({state:'hidden'});
    assert.deepEqual(errors,[]);process.stdout.write('shared memory browser: create/update/delete, explicit conflict rebase, rejected proposal, project scope, mobile and disabled feature passed\n');
  } finally {await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error);server.close();process.exitCode=1;});
