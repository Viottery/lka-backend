/* Same-origin browser client. Cookies carry credentials; storage holds CSRF only. */
(function (root) {
  "use strict";
  const MAX_FILE_BYTES = 32 * 1024 * 1024;
  const CSRF_KEY = "lka.client.csrf";
  const terminal = new Set(["completed", "failed", "timed_out", "cancelled", "unknown"]);
  const states = {queued:"等待执行",running:"正在执行",waiting_confirmation:"等待确认",waiting_user:"等待补充",completed:"已完成",failed:"执行失败",timed_out:"已超时",cancelled:"已取消",leased:"终端已领取",paused:"已暂停"};
  class ApiError extends Error {
    constructor(status, detail) { super(typeof detail === "string" ? detail : `请求未完成（${status}）`); this.status = status; }
  }
  function safeNodeURL(origin, grant) {
    const url = new URL(origin);
    if (url.protocol !== "https:" || !/^[a-z0-9.-]+\.ts\.net$/.test(url.hostname) || url.username || url.password || url.port || !["", "/"].includes(url.pathname) || url.search || url.hash) throw new Error("终端地址不是已批准的 Tailscale HTTPS 地址");
    url.pathname = "/client";
    url.hash = new URLSearchParams({grant}).toString();
    return url.href;
  }
  function createApi(fetcher, getCSRF) {
    return async function api(path, options = {}) {
      if (!path.startsWith("/") || path.startsWith("//")) throw new Error("只允许连接当前服务");
      const headers = new Headers(options.headers || {});
      const method = (options.method || "GET").toUpperCase();
      if (!["GET", "HEAD"].includes(method) && getCSRF()) headers.set("X-CSRF-Token", getCSRF());
      let body = options.body;
      if (options.json !== undefined) { headers.set("Content-Type", "application/json"); body = JSON.stringify(options.json); }
      const response = await fetcher(path, {...options, method, headers, body, credentials:"same-origin", cache:"no-store", redirect:"error"});
      if (!response.ok) {
        let detail;
        try { detail = (await response.json()).detail; } catch (_) { /* No server body is required. */ }
        throw new ApiError(response.status, detail);
      }
      if (options.raw) return response;
      if (response.status === 204) return null;
      return response.json();
    };
  }
  async function consumeSSE(response, onEvent, signal) {
    if (!response.body || !(response.headers.get("content-type") || "").includes("text/event-stream")) throw new Error("服务没有返回事件流，请刷新会话记录查看结果");
    const reader = response.body.getReader(), decoder = new TextDecoder();
    let buffer = "";
    function drain(final) {
      let boundary;
      // Normalize CRLF once complete lines are available, including a split CR/LF.
      buffer = buffer.replace(/\r\n/g, "\n");
      while ((boundary = buffer.indexOf("\n\n")) >= 0) {
        const frame = buffer.slice(0, boundary); buffer = buffer.slice(boundary + 2);
        const lines = frame.split("\n"), data = lines.filter(line => line.startsWith("data:")).map(line => line.slice(5).replace(/^ /, "")).join("\n");
        if (!data) continue;
        const event = JSON.parse(data);
        event.type = event.type || lines.find(line => line.startsWith("event:"))?.slice(6).trim() || "message";
        onEvent(event);
      }
      if (buffer.length > 2 * 1024 * 1024) throw new Error("单个事件超过显示上限，请刷新会话记录");
      if (final && buffer.trim() && !buffer.trim().startsWith(":")) throw new Error("事件流中断，请刷新会话记录核对结果");
    }
    const abort = () => { reader.cancel().catch(() => {}); };
    signal?.addEventListener("abort", abort, {once:true});
    try {
      while (true) {
        if (signal?.aborted) throw new DOMException("显示已断开", "AbortError");
        const part = await reader.read();
        buffer += decoder.decode(part.value || new Uint8Array(), {stream:!part.done});
        drain(part.done);
        if (part.done) break;
      }
    } finally { signal?.removeEventListener("abort", abort); await reader.cancel().catch(() => {}); reader.releaseLock(); }
  }
  function delay(milliseconds, signal) {
    return new Promise((resolve, reject) => {
      if (signal?.aborted) return reject(new DOMException("已断开", "AbortError"));
      const abort = () => { clearTimeout(timer); reject(new DOMException("已断开", "AbortError")); };
      const timer = setTimeout(() => { signal?.removeEventListener("abort", abort); resolve(); }, milliseconds);
      signal?.addEventListener("abort", abort, {once:true});
    });
  }
  function sharedMemoryReplacement(current) {
    for (let count = 0; current?.replacement && count < 8; count++) current = current.replacement;
    return current;
  }
  function boot() {
    const $ = id => document.getElementById(id);
    const state = {info:null, principal:null, csrf:sessionStorage.getItem(CSRF_KEY) || "", session:null, sessions:[], runId:null, stream:null, path:"", tasks:new Map(), loggedIn:false};
    const api = createApi(root.fetch.bind(root), () => state.csrf);
    const setCSRF = value => { state.csrf = value || ""; if (value) sessionStorage.setItem(CSRF_KEY, value); else sessionStorage.removeItem(CSRF_KEY); };
    function notice(message = "", error = false) { $("notice").textContent = message; $("notice").hidden = !message; $("signal").classList.toggle("error", error); }
    function status(message) { $("run-status").textContent = message; }
    function fail(error) { if (error.name !== "AbortError") notice(error.message, true); }
    function el(tag, text, className) { const item = document.createElement(tag); if (text !== undefined) item.textContent = text; if (className) item.className = className; return item; }
    function action(text, fn, className = "subtle") { const button = el("button", text, className); button.type = "button"; button.addEventListener("click", () => Promise.resolve(fn()).catch(fail)); return button; }
    function panel(name) { $("workbench").dataset.panel = name; document.querySelectorAll("[data-panel]").forEach(button => { if (button.tagName === "BUTTON") button.setAttribute("aria-pressed", String(button.dataset.panel === name)); }); }
    function message(role, text) {
      const article = el("article", undefined, `message ${role}`), body = el("div", text, "message-body");
      article.append(el("div", role === "user" ? "你" : "真理", "message-author"), body);
      $("messages").append(article); $("messages").scrollTop = $("messages").scrollHeight;
      return body;
    }
    function writesAvailable() {
      if (!state.csrf && state.loggedIn) throw new Error("此标签页缺少操作凭据，请重新登录；终端连接请回云端重新进入");
    }
    async function loadSession(identity) {
      if (state.stream) state.stream.abort();
      state.stream = null; state.runId = null;
      const detail = await api(`/sessions/${encodeURIComponent(identity)}`);
      state.session = detail.session;
      $("conversation-title").textContent = detail.session.title;
      $("messages").replaceChildren(); $("reviews").replaceChildren();
      for (const item of detail.messages || []) if (["user", "agent", "assistant"].includes(item.role)) message(item.role === "user" ? "user" : "agent", item.content);
      if (!$("messages").childElementCount) $("messages").append(el("p", "在这里接着说。", "empty"));
      $("send").disabled = false; $("disconnect").hidden = true; status("");
      renderSessions(); panel("conversation");
    }
    function renderSessions() {
      $("session-list").replaceChildren();
      for (const item of state.sessions) {
        const row = action(item.title || "未命名会话", () => loadSession(item.session_id), "session-row" + (item.session_id === state.session?.session_id ? " selected" : ""));
        const date = new Date(item.updated_at); row.append(el("small", Number.isNaN(date.valueOf()) ? "" : date.toLocaleDateString("zh-CN")));
        $("session-list").append(row);
      }
      if (!state.sessions.length) $("session-list").append(el("p", "还没有会话，创建后开始对话。", "help"));
    }
    async function loadSessions() {
      const data = await api("/sessions?limit=100");
      state.sessions = (data.sessions || []).filter(item => state.info.role === "hub" ? item.kind === "manager" : item.kind !== "manager");
      renderSessions();
    }
    async function createSession() {
      writesAvailable();
      const data = await api("/sessions", {method:"POST", json:{title:"新的普通会话"}});
      await loadSessions(); await loadSession(data.session.session_id);
      if ($("workspace").value) await bindWorkspace();
    }
    async function bindWorkspace() {
      writesAvailable();
      if (!state.session || !$("workspace").value) throw new Error("先选择会话和工作区");
      await api(`/sessions/${encodeURIComponent(state.session.session_id)}/workspace-id`, {method:"POST", json:{workspace_id:$("workspace").value}});
      status(`会话已绑定工作区 ${$("workspace").value}`);
    }
    async function loadWorkspaces() {
      const data = await api("/local/workspaces");
      $("workspace").replaceChildren(el("option", "选择工作区")); $("workspace").firstChild.value = "";
      for (const item of data.items || []) { const option = el("option", item.workspace_id); option.value = item.workspace_id; $("workspace").append(option); }
      if (data.items?.length === 1) { $("workspace").value = data.items[0].workspace_id; await loadFiles(); }
    }
    async function loadCacheStatus() {
      try {
        const cache = await api("/local/cache");
        if (!cache.available) { $("cache-status").textContent = "共享记忆缓存暂不可读；本机记忆仍可使用。"; return; }
        const age = typeof cache.age_seconds === "number" ? (cache.age_seconds < 60 ? `${Math.floor(cache.age_seconds)} 秒` : cache.age_seconds < 3600 ? `${Math.floor(cache.age_seconds / 60)} 分钟` : `${Math.floor(cache.age_seconds / 3600)} 小时`) : null;
        const freshness = age ? `${age}前同步` : "尚无同步时间";
        $("cache-status").textContent = `共享记忆缓存：${cache.stale ? "较旧" : "最近已同步"}，${freshness}；游标 ${cache.cursor ?? "未知"}。${cache.reads_allow_stale ? "可离线读取。" : ""}`;
      } catch (error) {
        $("cache-status").textContent = error.status === 404 ? "共享记忆缓存未启用；使用本机记忆。" : "暂时无法读取共享记忆状态；普通会话仍可继续。";
      }
    }
    let sharedMemoryRecords = [], sharedMemoryWriteEnabled = false, memoryDraft = null, memoryDraftID = null, sharedMemoryProjectID = null;
    let memoryScopeKey = "", memoryLoadGeneration = 0;
    function memoryScope() {
      return {scope:$("shared-memory-scope").value, workspace_id:$("shared-memory-scope").value === "project" ? $("workspace").value : null};
    }
    function resetMemoryDraft() {
      memoryDraft = null; memoryDraftID = root.crypto.randomUUID();
      $("shared-memory-form").reset(); $("shared-memory-edit-title").textContent = "新增共享记忆";
      $("shared-memory-target").textContent = ""; $("shared-memory-content").disabled = false;
      $("shared-memory-content").required = true;
      $("shared-memory-type").disabled = false; $("shared-memory-sensitivity").disabled = false;
      $("shared-memory-confirm-label").textContent = "我确认这是我本人要保存的记忆，并授权正式提交。";
      $("shared-memory-form").hidden = !sharedMemoryWriteEnabled;
    }
    function editSharedMemory(record, operation = "update", content = record.content) {
      resetMemoryDraft(); memoryDraft = {record, operation, scope:memoryScope()};
      $("shared-memory-edit-title").textContent = operation === "delete" ? "删除共享记忆" : "更正共享记忆";
      $("shared-memory-target").textContent = `基于版本 ${record.version}；云端状态变更后需重新核对。`;
      $("shared-memory-content").value = content || record.content || "";
      $("shared-memory-content").disabled = operation === "delete";
      $("shared-memory-content").required = operation !== "delete";
      $("shared-memory-type").value = ["preference", "project_decision", "user_fact"].includes(record.memory_type) ? record.memory_type : "user_fact";
      $("shared-memory-sensitivity").value = record.sensitivity === "personal" ? "personal" : "normal";
      $("shared-memory-type").disabled = true; $("shared-memory-sensitivity").disabled = true;
      $("shared-memory-confirm-label").textContent = operation === "delete" ? "我确认删除这条共享记忆，并授权正式提交。" : "我确认这是我本人要更正的记忆，并授权正式提交。";
      $("shared-memory-content").focus();
    }
    async function rebaseSharedMemory(entry) {
      await loadSharedMemory();
      const current = sharedMemoryReplacement(entry.receipt?.current);
      const record = sharedMemoryRecords.find(item => item.metadata?.shared_memory_id === current?.memory_id && item.version === current?.version);
      if (!record || current?.status !== "active") throw new Error("当前版本尚未进入镜像，或记忆已撤回。请刷新后核对；已删除的记忆不能通过重试恢复。");
      editSharedMemory(record, entry.proposal.operation, entry.proposal.content);
      notice("已保留你的提案并载入当前版本。请核对内容，再次勾选确认；提交会创建新的修改编号。");
    }
    async function retryRejectedMemory(entry) {
      await loadSharedMemory();
      if (entry.proposal.scope !== memoryScope().scope || entry.proposal.scope === "project" && entry.proposal.project_id !== sharedMemoryProjectID) throw new Error("请先切换到原提案的记忆范围与工作区，再核对提交。");
      if (entry.proposal.operation === "create") {
        resetMemoryDraft(); $("shared-memory-content").value = entry.proposal.content || "";
        $("shared-memory-type").value = entry.proposal.memory_type;
        $("shared-memory-sensitivity").value = entry.proposal.sensitivity;
      } else {
        const record = sharedMemoryRecords.find(item => item.metadata?.shared_memory_id === entry.proposal.memory_id);
        if (!record) throw new Error("原目标当前不在可用镜像中。请刷新并选择要更正的当前记录；不会恢复已删除记忆。");
        editSharedMemory(record, entry.proposal.operation, entry.proposal.content);
      }
      notice("已保留被拒绝的记录，并创建新的编辑编号。请检查连接权限和内容后再次明确确认。");
    }
    function renderSharedMemoryOutbox(items) {
      $("shared-memory-outbox").replaceChildren();
      const labels = {queued:"本机待提交",accepted:"云端已受理",conflict:"版本冲突，待你核对",rejected:"云端拒绝，提案已保留"};
      for (const entry of items) {
        const card = el("article", undefined, "memory-entry" + (entry.status === "conflict" ? " memory-conflict" : ""));
        card.append(el("strong", labels[entry.status] || entry.status), el("p", entry.proposal.content || "删除记忆", "memory-content"));
        const detail = el("details"), summary = el("summary", "查看提交与版本");
        detail.append(summary, el("p", `修改编号：${entry.mutation_id}`, "help"));
        if (entry.status === "conflict") {
          const current = sharedMemoryReplacement(entry.receipt?.current);
          detail.append(el("p", "你的提案已保留；此修改不会自动覆盖云端版本。", "help"));
          detail.append(el("p", current?.content ? `云端回执中的版本 ${current.version}：${current.content}` : "目标已撤回、来源不可用，或当前版本不在可同步范围。", "memory-content"));
          if (sharedMemoryWriteEnabled && current?.status === "active" && !current.content_unavailable) card.append(action("核对当前版本并重新编辑", () => rebaseSharedMemory(entry), "quiet"));
        }
        if (entry.status === "rejected") {
          detail.append(el("p", "此提案未获云端受理。请核对权限或输入；原编号不会自动重试，也未产生成功回执。", "help"));
          if (sharedMemoryWriteEnabled) card.append(action("检查权限后按原提案重新编辑", () => retryRejectedMemory(entry), "quiet"));
        }
        card.append(detail); $("shared-memory-outbox").append(card);
      }
      if (!items.length) $("shared-memory-outbox").append(el("p", "还没有共享记忆提交记录。", "help"));
    }
    async function loadSharedMemory() {
      if (state.info?.role !== "replica" || !state.principal) return;
      const generation = ++memoryLoadGeneration, selection = memoryScope();
      try {
        const control = await api("/local/shared-memory/status");
        if (generation !== memoryLoadGeneration) return;
        sharedMemoryWriteEnabled = control.write_enabled === true;
        $("shared-memory-panel").hidden = false;
        $("new-shared-memory").hidden = !sharedMemoryWriteEnabled;
        $("shared-memory-status").textContent = `${control.cache?.stale ? "镜像可能落后" : "使用本地镜像"}；待提交 ${control.queued || 0}，冲突 ${control.conflict || 0}，被拒绝 ${control.rejected || 0}。${sharedMemoryWriteEnabled ? "断云时仍可排队。" : "此终端未启用共享写入。"}`;
        const key = JSON.stringify(selection);
        if (key !== memoryScopeKey) { memoryScopeKey = key; resetMemoryDraft(); }
        $("shared-memory-records").replaceChildren();
        const data = selection.scope === "project" && !selection.workspace_id ? {memories:[],bound:false} : await api(`/local/shared-memory/records?${new URLSearchParams(Object.entries(selection).filter(([,value]) => value !== null))}`);
        if (generation !== memoryLoadGeneration) return;
        sharedMemoryRecords = data.memories || [];
        sharedMemoryProjectID = data.shared_project_id || null;
        for (const record of sharedMemoryRecords) {
          const card = el("article", undefined, "memory-entry");
          card.append(el("p", record.content, "memory-content"), el("small", `版本 ${record.version} · ${record.scope === "project" ? "项目" : "全局"}`));
          if (sharedMemoryWriteEnabled) card.append(action("更正", () => editSharedMemory(record), "quiet"), action("删除", () => editSharedMemory(record, "delete"), "quiet"));
          $("shared-memory-records").append(card);
        }
        if (!sharedMemoryRecords.length) $("shared-memory-records").append(el("p", selection.scope === "project" && !data.bound ? "请选择已绑定云端项目的工作区。" : "此范围暂时没有缓存的共享记忆。", "help"));
        $("new-shared-memory").disabled = selection.scope === "project" && !data.bound;
        if (!sharedMemoryWriteEnabled || selection.scope === "project" && !data.bound) $("shared-memory-form").hidden = true;
        const outbox = await api("/local/shared-memory/mutations");
        if (generation === memoryLoadGeneration) renderSharedMemoryOutbox(outbox.items || []);
      } catch (error) {
        if (generation !== memoryLoadGeneration) return;
        if (error.status === 404) { $("shared-memory-panel").hidden = true; return; }
        $("shared-memory-status").textContent = error.message;
      }
    }
    async function submitSharedMemory(event) {
      event.preventDefault();
      if (!$("shared-memory-confirm").checked || !sharedMemoryWriteEnabled) return;
      const button = $("submit-shared-memory"); button.disabled = true;
      try {
        writesAvailable();
        const selection = memoryScope(), draft = memoryDraft;
        if (draft && JSON.stringify(draft.scope) !== JSON.stringify(selection)) throw new Error("范围已改变，请重新选择要编辑的记忆。");
        memoryDraftID ||= root.crypto.randomUUID();
        const payload = {...selection, mutation_id:memoryDraftID, operation:draft?.operation || "create", memory_id:draft?.record.memory_id || null,
          expected_version:draft?.record.version || 0, confirmed:true, memory_type:$("shared-memory-type").value,
          sensitivity:$("shared-memory-sensitivity").value, content:draft?.operation === "delete" ? null : $("shared-memory-content").value.trim()};
        await api("/local/shared-memory/mutations", {method:"POST",json:payload});
        resetMemoryDraft(); await loadSharedMemory(); notice("修改已保存在本机待提交队列。云端受理并更新镜像后才用于记忆召回。");
      } catch (error) { fail(error); }
      finally { button.disabled = false; }
    }
    function fileURL(path) { return `/local/workspaces/${encodeURIComponent($("workspace").value)}/file?${new URLSearchParams({path})}`; }
    async function download(path, name) {
      const response = await api(fileURL(path), {raw:true});
      const blob = await response.blob();
      if (blob.size > MAX_FILE_BYTES) throw new Error("文件超过 32 MiB");
      const url = URL.createObjectURL(blob), link = el("a"); link.href = url; link.download = name;
      document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
    async function loadFiles() {
      $("file-list").replaceChildren(); $("file-path").textContent = "/" + state.path;
      if (!$("workspace").value) return;
      const data = await api(`/local/workspaces/${encodeURIComponent($("workspace").value)}/entries?${new URLSearchParams({path:state.path})}`);
      for (const item of data.items || []) {
        const path = [state.path, item.name].filter(Boolean).join("/");
        const row = el("div", undefined, "file-row");
        row.append(action((item.is_directory ? "▸ " : "") + item.name, async () => { if (item.is_directory) { state.path = path; await loadFiles(); } else await download(path, item.name); }, "quiet"));
        if (!item.is_directory) row.append(el("small", `${Math.ceil(item.size / 1024)} KiB`));
        $("file-list").append(row);
      }
      if (!data.items?.length) $("file-list").append(el("p", "此目录还没有文件。", "help"));
    }
    async function loadDevices() {
      const data = await api("/devices"); $("device-list").replaceChildren();
      for (const device of data.items || []) {
        const row = el("div", undefined, "device");
        row.append(el("strong", device.name || device.node_id), el("p", `${device.platform || ""}　${device.revoked ? "已撤销" : device.online ? "在线" : "最近未连接"}`));
        if (!device.revoked && device.advertised_url && state.info.connections_enabled) row.append(action("进入终端", async () => {
          writesAvailable();
          const grant = await api(`/devices/${encodeURIComponent(device.node_id || device.device_id)}/connections`, {method:"POST", json:{}});
          root.location.assign(safeNodeURL(grant.origin, grant.grant));
        }));
        else row.append(el("p", "尚未开放直接连接"));
        if (!device.revoked && state.info.connections_enabled) row.append(action("撤销我的既有连接", async () => {
          writesAvailable();
          await api(`/devices/${encodeURIComponent(device.node_id || device.device_id)}/connections/revoke`, {method:"POST", json:{}});
          notice("撤销已记录，终端下次成功连接云端时生效。离线终端暂时保留原连接。");
        }, "quiet"));
        $("device-list").append(row);
      }
      if (!data.items?.length) $("device-list").append(el("p", "尚未登记终端。请先完成终端批准与连接配置。", "help"));
    }
    async function loadConnections() {
      $("connections-panel").hidden = true;
      if (state.info.role !== "replica" || !state.principal?.node_id || state.principal.username) return;
      const data = await api("/connections/sessions");
      $("connections-panel").hidden = false; $("connection-list").replaceChildren();
      for (const connection of data.items || []) {
        const row = el("div", undefined, "device");
        row.append(el("strong", connection.current ? "当前连接" : `连接 ${connection.session_id.slice(0, 8)}`), el("p", `到期：${new Date(connection.expires_at * 1000).toLocaleString("zh-CN")}`));
        row.append(action("撤销连接", async () => {
          writesAvailable();
          await api(`/connections/sessions/${encodeURIComponent(connection.session_id)}`, {method:"DELETE"});
          if (connection.current) { state.stream?.abort(); setCSRF(""); state.loggedIn = false; state.principal = null; showLogin(); }
          else await loadConnections();
        }, "quiet"));
        $("connection-list").append(row);
      }
    }
    function renderTasks() {
      $("task-list").replaceChildren();
      for (const task of state.tasks.values()) {
        const item = el("article", undefined, "task");
        item.append(el("h3", `${task.node_id} · ${states[task.state] || task.state}`), el("p", task.task_id, "help"));
        item.append(action("查看结果", () => inspectTask(task.task_id), "quiet"));
        const commandIds = new Map();
        const control = async (kind, values = {}) => {
          writesAvailable();
          const key = kind + JSON.stringify(values);
          if (!commandIds.has(key)) commandIds.set(key, root.crypto.randomUUID());
          await api(`/remote-execution/tasks/${encodeURIComponent(task.task_id)}/${kind}`, {method:"POST", json:{command_id:commandIds.get(key), ...values}});
          notice("指令已接收，等待原终端处理。请刷新任务核对状态。");
          await inspectTask(task.task_id);
        };
        if (["running", "waiting_user", "waiting_confirmation"].includes(task.state)) item.append(action("暂停任务", () => control("pause"), "quiet"));
        if (task.state === "paused") item.append(action("继续任务", () => control("resume"), "quiet"));
        if (!terminal.has(task.state)) item.append(action("取消任务", async () => { writesAvailable(); await api(`/remote-execution/tasks/${encodeURIComponent(task.task_id)}/cancel`, {method:"POST", json:{}}); await inspectTask(task.task_id); }, "quiet"));
        const latest = task.reports?.[task.reports.length - 1];
        for (const report of task.reports || []) {
          if (report.error) item.append(el("p", typeof report.error === "string" ? report.error : JSON.stringify(report.error), "error"));
          else if (report.error_type) item.append(el("p", `终端报告：${report.error_type}`, "error"));
          if (report.result) {
            const result = report.result;
            item.append(el("pre", typeof result.answer === "string" ? result.answer : JSON.stringify(result, null, 2).slice(0,24000)));
          }
          for (const review of report.reviews || []) if (report === latest && task.next_offset == null && task.state === "waiting_confirmation" && review.review_id) {
            const pending = el("div", undefined, "review");
            pending.append(el("strong", review.tool_name || "待确认操作"), el("p", review.reason || "终端请求执行此操作。"));
            for (const [label, decision] of [["批准", "approve"],["拒绝", "reject"]]) pending.append(action(label, async () => {
              writesAvailable(); await api(`/remote-execution/tasks/${encodeURIComponent(task.task_id)}/reviews/${encodeURIComponent(review.review_id)}`, {method:"POST", json:{decision}}); await inspectTask(task.task_id);
            }));
            item.append(pending);
          }
        }
        if (task.next_offset == null && task.state === "waiting_user" && latest?.pending_user_question?.question_id) {
          const question = latest.pending_user_question;
          const form = el("form", undefined, "inline-form"), label = el("label", question.question || question.user_question || question.prompt || "补充回答"), input = el("textarea");
          input.required = true; input.maxLength = 16000; label.append(input); form.append(label);
          const submit = el("button", "回答并继续", "subtle"); submit.type = "submit"; form.append(submit);
          form.addEventListener("submit", async event => {
            event.preventDefault(); submit.disabled = true;
            try { await control("answer", {question_id:question.question_id, answer:input.value}); }
            catch (error) { fail(error); submit.disabled = false; }
          });
          item.append(form);
        }
        if (task.next_offset == null && ["completed", "failed"].includes(task.state) && latest && ["completed", "failed"].includes(latest.status)) {
          const form = el("form", undefined, "inline-form"), label = el("label", "在原终端和会话中继续"), input = el("textarea");
          input.required = true; input.maxLength = 16000; label.append(input); form.append(label);
          const submit = el("button", "提交补充指令", "subtle"); submit.type = "submit"; form.append(submit);
          form.addEventListener("submit", async event => {
            event.preventDefault(); submit.disabled = true;
            try { await control("instruction", {expected_run_id:task.run_id, purpose:input.value}); await loadTasks(); }
            catch (error) { fail(error); submit.disabled = false; }
          });
          item.append(form);
        }
        if (task.next_offset !== null && task.next_offset !== undefined) item.append(action("继续读取结果", async () => {
          const more = await api(`/remote-execution/tasks/${encodeURIComponent(task.task_id)}?limit=20&offset=${task.next_offset}`);
          state.tasks.set(task.task_id, {...more, reports:[...(task.reports || []), ...(more.reports || [])]}); renderTasks();
        }, "quiet"));
        $("task-list").append(item);
      }
      if (!state.tasks.size) $("task-list").append(el("p", "当前会话还没有终端任务。", "help"));
    }
    async function inspectTask(identity) {
      const task = await api(`/remote-execution/tasks/${encodeURIComponent(identity)}?limit=20&offset=0`);
      state.tasks.set(task.task_id, task); renderTasks();
    }
    async function loadTasks() {
      if (!state.session) return;
      const data = await api(`/remote-execution/tasks?${new URLSearchParams({owner_session_id:state.session.session_id,limit:"100",offset:"0"})}`);
      state.tasks.clear(); for (const task of data.items || []) state.tasks.set(task.task_id, task); renderTasks();
    }
    async function localReviews() {
      if (!state.runId) return;
      const data = await api(`/agent/runs/${encodeURIComponent(state.runId)}/safety-reviews`);
      $("reviews").replaceChildren();
      for (const review of data.reviews || []) if (review.status === "pending") {
        const item = el("div", undefined, "review"); item.append(el("strong", review.tool_name), el("p", review.reason));
        for (const [label, decision] of [["批准", "approve"],["拒绝", "reject"]]) item.append(action(label, async () => {
          writesAvailable(); await api(`/agent/safety-reviews/${encodeURIComponent(review.review_id)}/decision`, {method:"POST",json:{decision}}); await localReviews();
        }));
        $("reviews").append(item);
      }
    }
    async function streamTurn(text, signal, bubble) {
      let response, sawTerminal = false, sawFinal = false;
      if (state.info.role === "hub") {
        const clientMessage = root.crypto.randomUUID();
        const receipt = await api(`/sessions/${encodeURIComponent(state.session.session_id)}/inputs`, {method:"POST",json:{client_message_id:clientMessage,user_input:text,attachment_ids:[],llm:{response_mode:"stream"}},signal});
        state.runId = receipt.input?.run_id;
        for (let count=0; !state.runId && count<120; count++) {
          status("消息已接收，等待助理处理…"); await delay(1000, signal);
          const queue = await api(`/sessions/${encodeURIComponent(state.session.session_id)}/inputs`, {signal});
          const input = (queue.inputs || []).find(item => item.client_message_id === clientMessage);
          if (input && terminal.has(input.status) && !input.run_id) throw new Error("消息未启动，请刷新会话检查队列");
          state.runId = input?.run_id;
          if (queue.paused) throw new Error("消息已接收，队列暂停；请检查上一条任务后继续");
        }
        if (!state.runId) throw new Error("消息仍在队列中，请刷新记录查看；不要重复发送");
        response = await api(`/agent/runs/${encodeURIComponent(state.runId)}/stream`, {raw:true,signal});
      } else response = await api("/agent/turn/stream", {method:"POST",json:{session_id:state.session.session_id,user_input:text,llm:{response_mode:"stream"}},raw:true,signal});
      await consumeSSE(response, event => {
        if (event.run_id) state.runId = event.run_id;
        const payload = event.payload || {};
        if (event.type === "llm_delta" && payload.display_target === "assistant_answer") bubble.textContent = payload.content_snapshot || bubble.textContent + (payload.delta || event.message || "");
        if (event.type === "final_answer") { bubble.textContent = event.message || payload.answer || payload.metadata?.answer || ""; sawFinal = true; }
        if (event.type === "safety_review_required") { status("有操作等待确认"); localReviews().catch(fail); }
        if (event.type === "run_failed") throw new Error(event.message || "任务执行失败，请刷新记录");
        if (["run_completed", "run_cancelled"].includes(event.type)) { sawTerminal = true; status(event.type === "run_completed" ? "已完成" : "运行已取消"); }
        else if (!["llm_delta","heartbeat","final_answer","safety_review_required"].includes(event.type)) status(event.message || "正在处理…");
        $("messages").scrollTop = $("messages").scrollHeight;
      }, signal);
      if (!sawTerminal && !sawFinal) throw new Error("显示连接已结束，请刷新记录核对运行状态");
    }
    async function send(event) {
      event.preventDefault();
      const text = $("message").value.trim(); if (!text || state.stream) return;
      try {
        writesAvailable(); if (!state.session) throw new Error("先选择或创建会话");
        $("messages").querySelector(".empty")?.remove(); message("user", text); const bubble = message("agent", "正在处理…");
        $("message").value = ""; $("send").disabled = true; $("disconnect").hidden = false;
        const controller = new AbortController(); state.stream = controller;
        try { await streamTurn(text, controller.signal, bubble); }
        catch (error) { if (error.name === "AbortError") status("显示已断开；运行可能仍在继续，可刷新记录查看"); else { status(error.message); fail(error); } }
        finally { if (state.stream === controller) { state.stream = null; $("send").disabled = false; $("disconnect").hidden = true; } }
        await loadSessions(); if (state.info.role === "hub" && state.info.execution_enabled) await loadTasks();
      } catch (error) { fail(error); }
    }
    async function connected(principal) {
      if (principal?.csrf_token) setCSRF(principal.csrf_token);
      state.principal = principal; state.loggedIn = Boolean(principal);
      $("login-panel").hidden = true; $("workbench").hidden = false; $("logout").hidden = !principal;
      $("identity").textContent = principal?.username || "";
      $("signal").className = "signal ready";
      const hub = state.info.role === "hub";
      $("place").textContent = hub ? "云端助理" : `终端 · ${state.info.node_id || "本机"}`;
      $("sessions-heading").textContent = hub ? "助理会话" : "本机普通会话";
      $("conversation-kind").textContent = hub ? "云端持续管理" : "当前终端独立对话";
      $("scope-note").textContent = hub ? "助理与委派由云端接续。进入终端可单独处理本机工作。" : "普通会话保存在此终端。已有连接在云端暂时不可用时仍可使用。";
      $("connection-help").textContent = hub ? "云端负责助理与共享状态。终端文件通过批准的直接连接访问。" : "此页直接访问当前终端。共享记忆使用本地缓存；新同步前可能存在延迟。";
      $("cache-status").hidden = hub; $("refresh-cache").hidden = hub;
      $("new-session").hidden = hub; $("devices-panel").hidden = !hub; $("files-panel").hidden = hub;
      $("remote-panel").hidden = !hub || !state.info.execution_enabled;
      document.querySelector('[data-panel="resources"]').textContent = hub ? "终端与任务" : "工作区文件";
      await loadSessions().catch(fail);
      if (hub) {
        try {
          if (state.csrf) { const manager = await api("/managers/default", {method:"POST",json:{}}); await loadSessions(); await loadSession(manager.session.session_id); }
          else if (state.sessions[0]) await loadSession(state.sessions[0].session_id);
        } catch (error) { fail(error); }
        await loadDevices().catch(fail);
        if (state.info.execution_enabled) await loadTasks().catch(fail);
      } else {
        if (state.sessions[0]) await loadSession(state.sessions[0].session_id).catch(fail);
        await loadWorkspaces().catch(fail);
        await loadCacheStatus();
        await loadConnections().catch(fail);
        await loadSharedMemory();
      }
      if (principal && !state.csrf) notice("已恢复只读会话。发送或修改前，请重新登录；通过云端进入的终端请重新连接。");
    }
    function showLogin() {
      $("workbench").hidden = true; $("login-panel").hidden = false;
      $("login-title").textContent = state.info?.role === "hub" ? "登录云端助理" : "登录当前终端";
      $("login-help").textContent = state.info?.role === "hub" ? "登录后可使用助理，或进入已批准的电脑。" : "使用本机独立账号登录，或从云端终端列表重新进入。";
      $("place").textContent = state.info?.role === "hub" ? "云端助理" : `终端 · ${state.info?.node_id || "本机"}`;
    }
    $("login-form").addEventListener("submit", async event => {
      event.preventDefault(); const password = $("password").value; $("password").value = "";
      const button = event.currentTarget.querySelector("button"); button.disabled = true; $("login-error").textContent = "";
      try { const data = await api("/auth/login", {method:"POST",json:{username:$("username").value.trim(),password}}); setCSRF(data.csrf_token); notice(); await connected(data.account); }
      catch (error) { $("login-error").textContent = error.message; }
      finally { button.disabled = false; }
    });
    $("logout").addEventListener("click", async () => {
      try { writesAvailable(); await api(state.principal?.node_id && !state.principal?.username ? "/connections/logout" : "/auth/logout", {method:"POST",json:{}}); state.stream?.abort(); setCSRF(""); state.loggedIn = false; state.principal = null; showLogin(); }
      catch (error) { fail(error); }
    });
    $("composer").addEventListener("submit", send);
    $("message").addEventListener("keydown", event => { if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); $("composer").requestSubmit(); } });
    $("new-session").addEventListener("click", () => createSession().catch(fail));
    $("refresh-sessions").addEventListener("click", () => loadSessions().catch(fail));
    $("refresh-chat").addEventListener("click", () => state.session && loadSession(state.session.session_id).catch(fail));
    $("refresh-devices").addEventListener("click", () => loadDevices().catch(fail));
    $("refresh-tasks").addEventListener("click", () => loadTasks().catch(fail));
    $("task-form").addEventListener("submit", event => { event.preventDefault(); inspectTask($("task-id").value.trim()).catch(fail); });
    $("disconnect").addEventListener("click", () => state.stream?.abort());
    $("bind-workspace").addEventListener("click", () => bindWorkspace().catch(fail));
    $("workspace").addEventListener("change", () => { state.path = ""; loadFiles().catch(fail); });
    $("refresh-files").addEventListener("click", () => loadFiles().catch(fail));
    $("refresh-cache").addEventListener("click", loadCacheStatus);
    $("refresh-connections").addEventListener("click", () => loadConnections().catch(fail));
    $("refresh-shared-memory").addEventListener("click", () => loadSharedMemory().catch(fail));
    $("shared-memory-scope").addEventListener("change", () => loadSharedMemory().catch(fail));
    $("workspace").addEventListener("change", () => { if ($("shared-memory-scope").value === "project") loadSharedMemory().catch(fail); });
    $("new-shared-memory").addEventListener("click", resetMemoryDraft);
    $("cancel-shared-memory").addEventListener("click", () => { resetMemoryDraft(); $("shared-memory-form").hidden = true; });
    $("shared-memory-form").addEventListener("submit", submitSharedMemory);
    $("file-up").addEventListener("click", () => { state.path = state.path.split("/").slice(0,-1).join("/"); loadFiles().catch(fail); });
    $("upload-form").addEventListener("submit", async event => {
      event.preventDefault();
      try {
        writesAvailable(); const file = $("upload-file").files[0];
        if (!file || !$("workspace").value) throw new Error("先选择工作区和文件");
        if (file.size > MAX_FILE_BYTES) throw new Error("文件超过 32 MiB");
        const name = $("upload-name").value.trim() || file.name;
        if (/[\\/]/.test(name)) throw new Error("文件名不能包含目录分隔符");
        const path = [state.path,name].filter(Boolean).join("/");
        await api(fileURL(path), {method:"PUT",body:file,headers:{"Content-Type":"application/octet-stream"}});
        $("upload-form").reset(); await loadFiles(); notice("文件已上传到当前终端。");
      } catch (error) { fail(error); }
    });
    document.querySelectorAll("button[data-panel]").forEach(button => button.addEventListener("click", () => panel(button.dataset.panel)));
    root.addEventListener("pagehide", () => state.stream?.abort());
    async function start() {
      // Remove the one-use secret before any network request or error display.
      const fragment = new URLSearchParams(root.location.hash.slice(1));
      const grant = fragment.get("grant");
      if (root.location.hash) root.history.replaceState(null,"",root.location.pathname + root.location.search);
      state.info = await api("/client/info");
      if (grant) {
        if (state.info.role !== "replica" || !state.info.connections_enabled) throw new Error("此服务不接受终端连接授权");
        notice("等待终端接收连接授权…");
        let admitted = false;
        for (let count=0; count<45; count++) {
          try { const data = await api("/connections/redeem", {method:"POST",json:{grant}}); setCSRF(data.csrf_token); admitted = true; break; }
          catch (error) { if (error.status !== 403 || count === 44) throw error; await delay(2000); }
        }
        if (!admitted) throw new Error("终端未收到连接授权，请回云端重新连接");
        notice(); await connected(await api("/connections/me")); return;
      }
      if (state.info.role === "replica" && state.info.connections_enabled) {
        try { await connected(await api("/connections/me")); return; }
        catch (error) { if (![401,403].includes(error.status)) throw error; }
      }
      try { await connected(await api("/auth/me")); }
      catch (error) { if ([401,403].includes(error.status)) showLogin(); else if (error.status === 404 && state.info.role === "local") await connected(null); else throw error; }
    }
    start().catch(error => { fail(error); if (!state.loggedIn) showLogin(); });
  }
  const exported = {createApi, consumeSSE, safeNodeURL, sharedMemoryReplacement, MAX_FILE_BYTES};
  if (typeof module === "object" && module.exports) module.exports = exported;
  if (typeof document !== "undefined") boot();
})(typeof window !== "undefined" ? window : globalThis);
