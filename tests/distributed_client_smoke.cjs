/* Synthetic browser acceptance. No live backend, model or user data. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright-core');
const ROOT = path.resolve(__dirname, '../app/web/distributed');
const requests = [], failures = [], dialogs = [];
let redemptionAttempts = 0, grants = 0, uploaded = false, managerSent = false, nodeSent = false, approved = false, cacheUnavailable = false;
const csrf = 'synthetic-csrf-only', grant = 'synthetic-one-use-grant-'.repeat(3);
const stamp = '2026-10-24T00:00:00Z';
const manager = {session_id:'manager-1',title:'云端助理',kind:'manager',status:'active',updated_at:stamp};
const ordinary = {session_id:'ordinary-1',title:'电脑上的会话',kind:'ordinary',status:'active',updated_at:stamp};
const remote = {task_id:'task-1',owner_session_id:'manager-1',node_id:'desk',state:'waiting_confirmation'};
const answer = '这是合成的中文回答。';
function reply(status, value, headers={}) { return {status,headers:{'content-type':'application/json; charset=utf-8',...headers},body:JSON.stringify(value)}; }
function mock(method, url, raw, headers) {
  const node = url.hostname.endsWith('.ts.net'), p = url.pathname, body = raw && !p.endsWith('/file') ? JSON.parse(raw) : null;
  requests.push({method,path:p,node,headers,body,query:url.search});
  assert.equal(headers.authorization,undefined,'browser must never send bearer credentials');
  if (p === '/client/info') return reply(200,{role:node?'replica':'hub',node_id:node?'desk':null,connections_enabled:true,execution_enabled:true});
  if (p === '/auth/me') return reply(401,{detail:'login required'});
  if (p === '/auth/login') { assert.equal(body.password,'synthetic-password'); return reply(200,{account:{username:'owner',user_id:'u1'},csrf_token:csrf},{'set-cookie':'account=synthetic; Path=/; HttpOnly; SameSite=Strict'}); }
  if (p === '/connections/redeem') {
    assert.equal(body.grant,grant); redemptionAttempts++;
    if (redemptionAttempts === 1) return reply(403,{detail:'invalid or expired connection grant'});
    return reply(200,{csrf_token:csrf,node_id:'desk'},{'set-cookie':'node=synthetic; Path=/; Secure; HttpOnly; SameSite=Strict'});
  }
  if (p === '/connections/me') return reply(200,{node_id:'desk',session_id:'connection-1',workspace_ids:['work'],csrf_token:csrf});
  if (p === '/local/cache') return cacheUnavailable ? reply(503,{detail:'synthetic cache unavailable'}) : reply(200,{available:true,cursor:42,last_pull_at:stamp,stale:true,age_seconds:600,reads_allow_stale:true,observed_lag:0,hub_id:'synthetic-hub'});
  if (!['GET','HEAD'].includes(method)) assert.equal(headers['x-csrf-token'],csrf,`write requires csrf: ${p}`);
  if (p === '/devices/desk/connections') { grants++; return reply(201,{origin:'https://desk.example.ts.net',grant,node_id:'desk'}); }
  if (p === '/devices') return reply(200,{items:[{node_id:'desk',name:'书桌电脑',platform:'Linux',online:true,advertised_url:'https://desk.example.ts.net'}]});
  if (p === '/sessions' && method === 'GET') return reply(200,{sessions:[manager,ordinary]});
  if (p === '/sessions' && method === 'POST') return reply(201,{session:ordinary,messages:[]});
  if (p === '/managers/default') { assert.equal(node,false); return reply(200,{session:manager}); }
  if (p === '/sessions/manager-1' || p === '/sessions/ordinary-1') return reply(200,{session:node?ordinary:manager,messages:[{role:'agent',content:'可以接着说。'}]});
  if (p === '/sessions/manager-1/inputs') {
    assert.equal(node,false); if (method === 'POST') { managerSent=true; return reply(202,{input:{run_id:'manager-run'}}); }
    return reply(200,{inputs:[]});
  }
  if (p === '/agent/runs/manager-run/stream' || p === '/agent/turn/stream') {
    if (p === '/agent/turn/stream') { nodeSent=true; assert.equal(body.session_id,'ordinary-1'); }
    return {status:200,headers:{'content-type':'text/event-stream'},body:[
      {type:'run_started',run_id:node?'node-run':'manager-run',message:'正在处理'},
      {type:'llm_delta',message:'private reasoning must not show',payload:{display_target:'internal',delta:'private reasoning must not show'}},
      {type:'llm_delta',message:'这是合成',payload:{display_target:'assistant_answer',content_snapshot:'这是合成'}},
      {type:'final_answer',message:answer}, {type:'run_completed',message:'done'}
    ].map(item=>`event: ${item.type}\ndata: ${JSON.stringify(item)}\n\n`).join('')};
  }
  if (p === '/remote-execution/tasks') return reply(200,{items:[remote],next_offset:null});
  if (p === '/remote-execution/tasks/task-1') return reply(200,{...remote,state:approved?'completed':'waiting_confirmation',reports:[{result:{answer:'终端已返回合成结果'},reviews:approved?[]:[{review_id:'review-1',tool_name:'filesystem.edit_file',reason:'创建合成文件'}]}],next_offset:null});
  if (p === '/remote-execution/tasks/task-1/reviews/review-1') { approved=true; assert.equal(body.decision,'approve'); return reply(200,{decision:'approve'}); }
  if (p === '/local/workspaces') return reply(200,{items:[{workspace_id:'work'}]});
  if (p === '/local/shared-memory/status') return reply(404,{detail:'not enabled in this fixture'});
  if (p === '/connections/sessions') return reply(200,{items:[]});
  if (p === '/sessions/ordinary-1/workspace-id') { assert.equal(body.workspace_id,'work'); return reply(200,{workspace:{path:'/synthetic'}}); }
  if (p === '/local/workspaces/work/entries') return reply(200,{path:'',items:[{name:'readme.txt',is_directory:false,size:3},...(uploaded?[{name:'upload.txt',is_directory:false,size:4}]:[])]});
  if (p === '/local/workspaces/work/file' && method === 'PUT') { uploaded=true; return reply(201,{path:'upload.txt'}); }
  if (p === '/local/workspaces/work/file') return {status:200,headers:{'content-type':'application/octet-stream'},body:'synthetic download'};
  throw new Error(`unexpected mock API ${method} ${url.href}`);
}
const server=http.createServer(async(req,res)=>{
  try {
    const url=new URL(req.url,'http://127.0.0.1');
    if (['/client','/client/client.js','/client/client.css'].includes(url.pathname)) {
      const name=url.pathname==='/client'?'client.html':path.basename(url.pathname), content=fs.readFileSync(path.join(ROOT,name));
      res.writeHead(200,{'content-type':name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html; charset=utf-8'}); return res.end(content);
    }
    let raw=''; for await(const chunk of req) raw+=chunk;
    const result=mock(req.method,url,raw,req.headers); res.writeHead(result.status,result.headers);res.end(result.body);
  } catch(error) { failures.push(error.stack);res.writeHead(500);res.end('mock failure'); }
});
(async()=>{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const origin=`http://127.0.0.1:${server.address().port}`;
  let browser;
  try {
    browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
    const context=await browser.newContext({viewport:{width:1440,height:980},acceptDownloads:true});
    await context.route('https://desk.example.ts.net/**',async route=>{
      try {
        const request=route.request(),url=new URL(request.url());
        if (['/client','/client/client.js','/client/client.css'].includes(url.pathname)) {
          const name=url.pathname==='/client'?'client.html':path.basename(url.pathname);
          return route.fulfill({status:200,contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html; charset=utf-8',body:fs.readFileSync(path.join(ROOT,name))});
        }
        await route.fulfill(mock(request.method(),url,request.postData(),request.headers()));
      } catch(error) { failures.push(error.stack);await route.fulfill({status:500,body:'mock failure'}); }
    });
    const page=await context.newPage(); page.on('pageerror',error=>failures.push(error.stack));page.on('dialog',dialog=>{dialogs.push(dialog.message());dialog.dismiss();});
    await page.goto(origin+'/client'); await page.locator('#login-panel').waitFor({state:'visible'});
    await page.locator('#username').fill('owner');await page.locator('#password').fill('synthetic-password');await page.locator('#login-form button').click();
    await page.locator('#workbench').waitFor({state:'visible'});await page.waitForFunction(()=>document.querySelector('#conversation-title').textContent==='云端助理');
    assert.equal(await page.locator('#password').inputValue(),'');
    assert.equal(await page.locator('#new-session').isVisible(),false);
    await page.locator('#message').fill('请处理合成的助理任务');await page.locator('#send').click();
    await page.waitForFunction(()=>document.querySelector('#messages').textContent.includes('这是合成的中文回答。'));
    assert.equal(managerSent,true);assert.equal((await page.locator('#messages').textContent()).includes('private reasoning'),false);
    await page.locator('#task-list').getByRole('button',{name:'查看结果'}).click();
    await page.locator('#task-list').getByRole('button',{name:'批准',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#task-list').textContent.includes('已完成'));
    assert.equal(approved,true);
    if(process.env.SCREENSHOT_DIR) await page.screenshot({path:path.join(process.env.SCREENSHOT_DIR,'distributed-hub.png'),fullPage:true});
    await page.getByRole('button',{name:'进入终端'}).click();
    await page.waitForURL('https://desk.example.ts.net/client');
    await page.waitForFunction(()=>document.querySelector('#place').textContent==='终端 · desk');
    assert.equal(grants,1);assert.equal(redemptionAttempts,2);assert.equal(new URL(page.url()).hash,'');
    assert.equal(await page.locator('#devices-panel').isVisible(),false);
    const storage=await page.evaluate(()=>Object.fromEntries(Object.keys(sessionStorage).map(key=>[key,sessionStorage.getItem(key)])));
    assert.deepEqual(storage,{'lka.client.csrf':csrf});
    assert.equal((await page.content()).includes(grant),false);
    // A reopened page must recover write CSRF from the local connection cookie,
    // without visiting the cloud or requiring a fresh grant.
    await page.evaluate(()=>sessionStorage.clear()); await page.reload();
    await page.waitForFunction(()=>sessionStorage.getItem('lka.client.csrf')==='synthetic-csrf-only');
    await page.waitForFunction(()=>document.querySelector('#conversation-title').textContent==='电脑上的会话');
    await page.waitForFunction(()=>document.querySelector('#cache-status').textContent.includes('游标 42'));
    assert.equal((await page.locator('#cache-status').textContent()).includes('较旧'),true);
    assert.equal((await page.locator('#cache-status').textContent()).includes('10 分钟前同步'),true);
    cacheUnavailable=true; await page.locator('#refresh-cache').click();
    await page.waitForFunction(()=>document.querySelector('#cache-status').textContent.includes('普通会话仍可继续'));
    assert.equal(redemptionAttempts,2);
    await page.locator('#message').fill('云端不在线时的普通任务');await page.locator('#send').click();
    await page.waitForFunction(()=>document.querySelector('#messages').textContent.includes('这是合成的中文回答。'));
    assert.equal(nodeSent,true);
    cacheUnavailable=false; await page.locator('#refresh-cache').click();
    await page.waitForFunction(()=>document.querySelector('#cache-status').textContent.includes('游标 42'));
    assert.equal(requests.filter(request=>request.node&&['/devices','/managers/default','/remote-execution/tasks'].includes(request.path)).length,0);
    await page.locator('#bind-workspace').click();
    await page.waitForFunction(()=>document.querySelector('#run-status').textContent.includes('已绑定'));
    await page.locator('#upload-file').setInputFiles({name:'upload.txt',mimeType:'text/plain',buffer:Buffer.from('test')});
    await page.locator('#upload-form button').click(); await page.waitForFunction(()=>document.querySelector('#file-list').textContent.includes('upload.txt'));
    const downloadPromise=page.waitForEvent('download');await page.getByRole('button',{name:'readme.txt',exact:true}).click();await downloadPromise;
    assert.equal(uploaded,true);
    await page.setViewportSize({width:390,height:844});await page.getByRole('button',{name:'工作区文件',exact:true}).click();
    await page.locator('#files-panel').waitFor({state:'visible'});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollHeight>innerHeight),false);
    if(process.env.SCREENSHOT_DIR) await page.screenshot({path:path.join(process.env.SCREENSHOT_DIR,'distributed-node-mobile.png'),fullPage:true});
    await page.getByRole('button',{name:'对话',exact:true}).click();
    assert.equal(await page.locator('#composer').isVisible(),true);
    assert.deepEqual(dialogs,[]);assert.deepEqual(failures,[]);
    console.log('Distributed browser smoke passed: login, cloud queue, task approval, grant retry/scrub, offline node SSE, workspace bind/upload/download, mobile layout.');
  } finally { await browser?.close();server.close(); }
})().catch(error=>{console.error(error.stack);process.exitCode=1;server.close();});
