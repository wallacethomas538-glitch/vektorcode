'use strict';
/* VektorCode Nexus UI — every panel reads the real backend (/api/state,
   /api/health, /health, /api/mcp/status). Nothing here is mocked: when the
   backend does not provide a field yet, the UI says so instead of faking it.
   The parallel backend contract (session_id on /api/chat, GET
   /api/session/<id>/messages, tools_used[], SSE when stream:true) is
   consumed when present and degraded gracefully when absent. */

let state = null;
let currentSessionId = localStorage.getItem('vc_session_id') || null;
let chatHistory = [];          // [{role, content}] for the current session
let lastUserText = null;       // for the Retry button on retryable errors
let paletteItems = [];
let paletteSelected = 0;
let loginPromptShown = false;

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
}[c]));

/* ---------- low-level fetch ---------- */

// Returns {status, data}; data is null when the body is not JSON
// (e.g. 204 login success, or the 404 HTML page for an unimplemented route).
async function apiRaw(path, data = null, method = 'GET') {
    const options = { method, headers: { 'Content-Type': 'application/json' } };
    if (data) options.body = JSON.stringify(data);
    const res = await fetch(path, options);
    let parsed = null;
    try { parsed = await res.json(); } catch (_) { parsed = null; }
    return { status: res.status, data: parsed };
}

// Convenience wrapper for JSON endpoints. Returns the parsed body, or null
// on 401 (after opening the login prompt) / network failure.
async function api(path, data = null, method = 'GET') {
    let raw;
    try {
        raw = await apiRaw(path, data, method);
    } catch (err) {
        toast('Network error: could not reach the server.');
        return null;
    }
    if (raw.status === 401) {
        openLogin('Authentication required — unlock to continue.');
        return null;
    }
    if (raw.status >= 400) {
        return raw.data || { ok: false, error: 'HTTP ' + raw.status };
    }
    return raw.data;
}

function toast(msg) {
    const t = $('toast');
    t.textContent = msg;
    t.style.display = 'block';
    clearTimeout(t._timer);
    t._timer = setTimeout(() => { t.style.display = 'none'; }, 2600);
}

/* ---------- state loading & rendering ---------- */

async function refreshState() {
    const next = await api('/api/state');
    if (!next) return; // 401/network: api() already surfaced it
    state = next;
    renderModels();
    renderSessions();
    renderSkills();
    renderActivity();
    renderVitals();
    renderSettingsBits();
}

function renderModels() {
    const select = $('active-model');
    select.innerHTML = state.models.map((m) =>
        `<option value="${esc(m.id)}" ${m.id === state.active_model ? 'selected' : ''}>` +
        `${esc(m.name)}${m.configured ? '' : ' — key needed'}</option>`
    ).join('');
    renderModelNameSelect($('active-model-name'));
    renderModelNameSelect($('mobile-model'));
    const mp = $('mobile-provider');
    if (mp) {
        mp.innerHTML = select.innerHTML;
        mp.value = state.active_model;
    }
    const active = state.models.find((m) => m.id === state.active_model) || {};
    const modelLabel = active.name || state.active_model;
    const modelName = state.active_model_name || active.model || '';
    $('agent-status-model').textContent = modelLabel + (modelName ? ' · ' + modelName : '');
    $('settings-model').textContent = modelLabel + (modelName ? ' (' + modelName + ')' : '');
}

function renderModelNameSelect(select) {
    if (!select) return;
    const active = state.models.find((m) => m.id === state.active_model) || {};
    const models = active.models || [];
    const current = state.active_model_name || active.model || '';
    if (!models.length) {
        select.innerHTML = `<option value="">${esc(active.free_note || 'No free models for this provider')}</option>`;
        select.disabled = true;
        return;
    }
    select.disabled = false;
    select.innerHTML = models.map((name) =>
        `<option value="${esc(name)}" ${name === current ? 'selected' : ''}>${esc(name)}</option>`
    ).join('');
}

function renderSessions() {
    const list = $('session-list');
    if (!state.sessions.length) {
        list.innerHTML = '<div class="muted">No sessions yet.</div>';
        return;
    }
    list.innerHTML = state.sessions.map((s) =>
        `<div class="session-item ${s.id === currentSessionId ? 'active' : ''}" onclick="resumeSession('${esc(s.id)}')" title="${esc(s.title)} — ${esc(s.model)} · ${s.tokens || 0} tokens">` +
        `<span class="session-title">${esc(s.title)}</span>` +
        `<button class="session-del" aria-label="Delete session ${esc(s.title)}" onclick="event.stopPropagation(); deleteSession('${esc(s.id)}')">×</button>` +
        `</div>`
    ).join('');
}

function renderSkills() {
    const list = $('skill-list');
    // Only Composio is actually wired to execute (via the MCP bridge in
    // app.py). The rest are registered labels with no executor behind them.
    list.innerHTML = state.tools.map((t) => {
        const wired = t.id === 'composio';
        const note = wired ? 'Wired via Composio MCP' : 'Registered, not executable';
        const locked = t.locked && !state.auth.authenticated;
        return `<label class="skill-row ${t.enabled ? 'enabled' : ''}" title="${esc(t.description)}">
            <input type="checkbox" ${t.enabled ? 'checked' : ''} ${locked ? 'disabled' : ''}
                onchange="toggleTool('${esc(t.id)}', this.checked, this)">
            <span class="skill-body"><b>${esc(t.name)}</b>
            <small>${esc(t.description)}</small>
            <small class="skill-note">${note}${locked ? ' · login required' : ''}${t.approval_required ? ' · approval-gated' : ''}</small></span>
        </label>`;
    }).join('');
}

function renderActivity() {
    const feed = $('activity-feed');
    if (!state.receipts.length) {
        feed.innerHTML = '<div class="muted">No recorded activity yet.</div>';
        return;
    }
    feed.innerHTML = state.receipts.map((r) => {
        let detail = '';
        try {
            const d = typeof r.detail === 'string' ? JSON.parse(r.detail) : r.detail;
            detail = typeof d === 'object' ? Object.entries(d).map(([k, v]) => `${k}=${v}`).join(' ') : String(d);
        } catch (_) { detail = String(r.detail || ''); }
        const when = r.created ? new Date(r.created * 1000).toLocaleString() : '';
        return `<div class="activity-item"><span class="activity-time">${esc(when)}</span>
            <span class="activity-action">${esc(r.action)}</span>
            ${r.sensitive ? '<span class="activity-badge">Aegis Gate</span>' : ''}
            ${detail ? `<span class="activity-detail">${esc(detail.slice(0, 140))}</span>` : ''}</div>`;
    }).join('');
}

function setVital(dotId, textId, text, ok) {
    $(textId).textContent = text;
    $(dotId).className = 'dot' + (ok ? ' online' : '');
}

function renderVitals() {
    // MCP status is real (from /api/state). API status comes from /health
    // via updateStatus(); until then it stays "Checking...".
    setVital('vital-mcp-dot', 'vital-mcp',
        state.mcp.connected ? `Online (${state.mcp.tool_count} tools)` : 'Offline',
        state.mcp.connected);
    setVital('vital-kill-dot', 'vital-kill',
        state.kill_switch ? 'ACTIVE — chat stopped' : 'Off', !state.kill_switch);
    setVital('vital-auth-dot', 'vital-auth',
        !state.auth.enabled ? 'Not configured (open)'
            : state.auth.authenticated ? 'Unlocked' : 'Locked — login required',
        !state.auth.enabled || state.auth.authenticated);
}

async function updateStatus() {
    // GET /health is the real service liveness check.
    try {
        const res = await fetch('/health');
        const data = await res.json().catch(() => null);
        const ok = res.ok && data && data.ok;
        setVital('vital-api-dot', 'vital-api', ok ? 'Online' : 'Unreachable', !!ok);
        $('health-service').textContent = ok ? 'Online (' + (data.service || 'vektorcode') + ')' : 'Unreachable';
    } catch (_) {
        setVital('vital-api-dot', 'vital-api', 'Unreachable', false);
        $('health-service').textContent = 'Unreachable';
    }
}

/* ---------- models & sessions ---------- */

async function changeModel(val) {
    const res = await api('/api/model', { model: val }, 'POST');
    if (!res || res.ok === false) {
        toast('Model change failed: ' + ((res && res.error) || 'server error'));
        await refreshState(); // restore the select to the real active model
        return;
    }
    await refreshState();
}

async function changeModelName(val) {
    if (!val) return;
    const res = await api('/api/model', { model: state.active_model, model_name: val }, 'POST');
    if (!res || res.ok === false) {
        toast('Model change failed: ' + ((res && res.error) || 'server error'));
        await refreshState();
        return;
    }
    await refreshState();
}

function currentModelName() {
    const sel = $('active-model-name');
    return sel && !sel.disabled && sel.value ? sel.value : null;
}

async function newSession() {
    const body = { title: 'New session', model: $('active-model').value };
    const mn = currentModelName();
    if (mn) body.model_name = mn;
    const res = await api('/api/session', body, 'POST');
    if (!res || !res.ok || !res.id) {
        toast('Could not create session: ' + ((res && res.error) || 'server error'));
        return;
    }
    setCurrentSession(res.id);
    clearChatView();
    await refreshState();
    toast('New session started');
}

function setCurrentSession(id) {
    currentSessionId = id;
    if (id) localStorage.setItem('vc_session_id', id);
    else localStorage.removeItem('vc_session_id');
}

async function resumeSession(id) {
    setCurrentSession(id);
    clearChatView();
    // Restore this session's stored provider:model selection (sessions
    // store "provider:model", older ones store just the provider id).
    const sess = (state.sessions || []).find((x) => x.id === id);
    if (sess && sess.model) {
        const parts = String(sess.model).split(':');
        const pid = parts[0];
        const mname = parts.slice(1).join(':');
        const entry = (state.models || []).find((m) => m.id === pid);
        if (entry) {
            state.active_model = pid;
            state.active_model_name = mname || entry.model || '';
            renderModels();
        }
    }
    // Preferred: real history from the backend (GET /api/session/<id>/messages).
    // Fallback: POST /api/session/resume, which also returns the messages.
    // Last resort: resume without history and say so honestly.
    let loaded = false;
    try {
        const raw = await apiRaw('/api/session/' + encodeURIComponent(id) + '/messages');
        const msgs = raw.data && (Array.isArray(raw.data) ? raw.data : raw.data.messages);
        if (raw.status === 200 && Array.isArray(msgs)) {
            renderHistory(msgs);
            loaded = true;
        } else if (raw.status === 401) {
            openLogin('Login required to load session history.');
        }
    } catch (_) { loaded = false; }
    if (!loaded) {
        const res = await api('/api/session/resume', { id }, 'POST');
        if (res && Array.isArray(res.messages) && res.messages.length) {
            renderHistory(res.messages);
            loaded = true;
        }
    }
    if (!loaded) {
        addMessage('system', 'Session resumed. Message history is unavailable — the backend returned no history for this session.');
    }
    renderSessions();
}

function renderHistory(msgs) {
    chatHistory = msgs.filter((m) => m && typeof m.content === 'string')
        .map((m) => ({ role: m.role || 'user', content: m.content }));
    chatHistory.forEach((m) => addMessage(m.role === 'assistant' ? 'agent' : m.role, m.content));
    if (!chatHistory.length) {
        addMessage('system', 'Session resumed — no messages in this session yet.');
    }
}

async function deleteSession(id) {
    const res = await api('/api/session/delete', { id }, 'POST');
    if (!res || res.ok === false) {
        toast('Delete failed: ' + ((res && res.error) || 'server error'));
        return;
    }
    if (id === currentSessionId) {
        setCurrentSession(null);
        clearChatView();
    }
    await refreshState();
    toast('Session deleted');
}

/* ---------- skills / tools ---------- */

async function toggleTool(id, enabled, checkbox) {
    let raw;
    try {
        raw = await apiRaw('/api/tool', { tool: id, enabled }, 'POST');
    } catch (_) {
        checkbox.checked = !enabled;
        toast('Network error — tool toggle did not reach the server.');
        return;
    }
    if (raw.status === 401) {
        checkbox.checked = !enabled;
        openLogin('Login required to toggle auth-gated tools.');
        return;
    }
    if (raw.status !== 200 || (raw.data && raw.data.ok === false)) {
        checkbox.checked = !enabled;
        toast('Tool toggle failed: ' + ((raw.data && raw.data.error) || ('HTTP ' + raw.status)));
        return;
    }
    toast(enabled ? 'Tool enabled' : 'Tool disabled');
    await refreshState();
}

function toggleSkills() {
    // The tool-toggle button jumps to the skills panel (left drawer on mobile).
    const panel = $('mind-panel');
    if (window.innerWidth <= 900) panel.classList.add('open');
    const list = $('skill-list');
    if (list.scrollIntoView) list.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    list.classList.add('flash');
    setTimeout(() => list.classList.remove('flash'), 1200);
}

/* ---------- chat ---------- */

function clearChatView() {
    $('chat-flow').innerHTML = '';
    chatHistory = [];
    lastUserText = null;
}

function clearChat() {
    clearChatView();
    toast('Chat view cleared (session history on the server is untouched).');
}

function addMessage(role, text) {
    const flow = $('chat-flow');
    const msg = document.createElement('div');
    msg.className = `message ${role}`;
    msg.textContent = text;
    flow.appendChild(msg);
    flow.scrollTop = flow.scrollHeight;
    return msg;
}

function addToolResults(toolsUsed) {
    // The backend returns tools_used as tool NAMES (strings); older/other
    // shapes may carry objects with name + content. Render both honestly:
    // a bare name means the backend returned no result detail.
    const flow = $('chat-flow');
    const box = document.createElement('div');
    box.className = 'tool-results';
    box.innerHTML = '<div class="tool-results-title">Tool results</div>' + toolsUsed.map((t) => {
        if (typeof t === 'string') {
            return `<div class="tool-result"><b>${esc(t)}</b><pre>Tool ran — no result detail returned by the backend.</pre></div>`;
        }
        const name = esc(t.name || t.tool || 'tool');
        let content = t.content;
        if (typeof content !== 'string') {
            try { content = JSON.stringify(content); } catch (_) { content = String(content ?? ''); }
        }
        return `<div class="tool-result"><b>${name}</b><pre>${esc((content || '').slice(0, 2000))}</pre></div>`;
    }).join('');
    flow.appendChild(box);
    flow.scrollTop = flow.scrollHeight;
}

function errorTextFor(status, serverError) {
    switch (status) {
        case 400: return 'Request rejected (400): ' + (serverError || 'bad request');
        case 401: return 'Authentication required (401). Unlock in Settings and retry.';
        case 423: return 'Kill switch is active (423): chat is stopped until the kill switch is cleared server-side.';
        case 429: return 'Rate limited (429): ' + (serverError || 'all provider keys exhausted') + '. You can retry.';
        case 502: return 'LLM unavailable (502): ' + (serverError || 'the model endpoint could not be reached') + '. You can retry.';
        default: return 'Error' + (status ? ' (' + status + ')' : '') + ': ' + (serverError || 'unknown failure');
    }
}

function addErrorBubble(status, serverError, retryable) {
    const flow = $('chat-flow');
    const msg = document.createElement('div');
    msg.className = 'message error';
    const text = document.createElement('span');
    text.textContent = errorTextFor(status, serverError);
    msg.appendChild(text);
    if (retryable) {
        const btn = document.createElement('button');
        btn.className = 'btn-retry';
        btn.textContent = 'Retry';
        btn.onclick = () => { msg.remove(); sendChat(lastUserText, true); };
        msg.appendChild(btn);
    }
    flow.appendChild(msg);
    flow.scrollTop = flow.scrollHeight;
}

function speak(text) {
    if (!$('speak-toggle').checked) return;
    if (!('speechSynthesis' in window)) return;
    // Don't read code blocks aloud.
    const clean = text.replace(/```[\s\S]*?```/g, ' [code block] ').replace(/`[^`]*`/g, '').trim();
    if (!clean) return;
    speechSynthesis.cancel();
    speechSynthesis.speak(new SpeechSynthesisUtterance(clean));
}

async function ensureSession() {
    if (currentSessionId) return currentSessionId;
    const body = { title: 'New session', model: $('active-model').value };
    const mn = currentModelName();
    if (mn) body.model_name = mn;
    const res = await api('/api/session', body, 'POST');
    if (res && res.ok && res.id) {
        setCurrentSession(res.id);
        await refreshState(); // pick up the new session so it shows in the list at once
        return res.id;
    }
    return null; // chat proceeds without a session id; backend degrades
}

async function sendMessage() {
    const input = $('user-input');
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    addMessage('user', text);
    chatHistory.push({ role: 'user', content: text });
    lastUserText = text;
    await sendChat(text, false);
}

async function sendChat(text, isRetry) {
    const sendBtn = $('send-btn');
    sendBtn.disabled = true;
    const sessionId = await ensureSession();
    // The user turn must be the last history entry (a failed turn pops it,
    // so Retry re-adds it here instead of sending a history without it).
    const last = chatHistory[chatHistory.length - 1];
    if (!last || last.role !== 'user' || last.content !== text) {
        chatHistory.push({ role: 'user', content: text });
    }
    const payload = {
        messages: chatHistory,
        model: $('active-model').value,
        stream: true
    };
    const modelName = $('active-model-name') && $('active-model-name').value;
    if (modelName) payload.model_name = modelName;
    if (sessionId) payload.session_id = sessionId;

    let res;
    try {
        res = await fetch('/api/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream' },
            body: JSON.stringify(payload)
        });
    } catch (_) {
        addErrorBubble(0, 'could not reach the VektorCode server', true);
        sendBtn.disabled = false;
        return;
    }

    const contentType = res.headers.get('content-type') || '';
    if (contentType.includes('text/event-stream')) {
        await readSseReply(res);
        sendBtn.disabled = false;
        return;
    }

    let data = null;
    try { data = await res.json(); } catch (_) { data = null; }
    if (res.ok && data && data.ok) {
        finishReply(data.message || '', data);
    } else {
        const status = res.status;
        if (status === 401) openLogin('Authentication required — unlock to continue.');
        const retryable = data && typeof data.retryable === 'boolean'
            ? data.retryable : (status === 429 || status === 502);
        addErrorBubble(status, data && data.error, retryable);
        chatHistory.pop(); // keep history consistent: failed turn is not stored
    }
    sendBtn.disabled = false;
}

async function readSseReply(res) {
    // Streaming path, per the backend contract (Track A):
    //   {"type":"delta","content":piece}  — accumulate, show live
    //   {"type":"done","message":FULL,"tools_used":[...],"session_id":...}
    //       — the done message REPEATS the streamed text; never append it
    //   {"type":"error","error":...,"retryable":bool}
    // Only used when the backend actually returns an event stream.
    const flow = $('chat-flow');
    const bubble = document.createElement('div');
    bubble.className = 'message agent';
    bubble.textContent = '';
    flow.appendChild(bubble);
    let full = '';
    let doneData = null;
    let streamError = null;
    try {
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buf = '';
        for (;;) {
            const { done, value } = await reader.read();
            if (done) break;
            buf += decoder.decode(value, { stream: true });
            let idx;
            while ((idx = buf.indexOf('\n\n')) !== -1) {
                const chunk = buf.slice(0, idx);
                buf = buf.slice(idx + 2);
                for (const line of chunk.split('\n')) {
                    if (!line.startsWith('data:')) continue;
                    const raw = line.slice(5).trim();
                    if (!raw || raw === '[DONE]') continue;
                    let evt;
                    try { evt = JSON.parse(raw); } catch (_) { continue; }
                    if (evt.type === 'delta') {
                        const piece = typeof evt.content === 'string' ? evt.content
                            : (typeof evt.delta === 'string' ? evt.delta : '');
                        if (piece) {
                            full += piece;
                            bubble.textContent = full;
                            flow.scrollTop = flow.scrollHeight;
                        }
                    } else if (evt.type === 'done') {
                        doneData = evt;
                    } else if (evt.type === 'error') {
                        streamError = evt;
                    }
                }
            }
        }
    } catch (_) {
        if (!full && !doneData) {
            bubble.remove();
            addErrorBubble(502, 'stream interrupted', true);
            chatHistory.pop();
            return;
        }
    }
    if (streamError) {
        bubble.remove();
        addErrorBubble(502, streamError.error || 'stream failed',
            streamError.retryable !== false);
        chatHistory.pop();
        return;
    }
    // The done event carries the full message; prefer the streamed text
    // (identical), falling back to done.message if no deltas arrived.
    const text = full.trim() ? full : (doneData && doneData.message) || '';
    if (!text.trim()) {
        bubble.remove();
        addErrorBubble(502, 'empty streamed reply', true);
        chatHistory.pop();
        return;
    }
    bubble.remove(); // finishReply renders the final bubble once
    finishReply(text, doneData);
}

function finishReply(text, data) {
    chatHistory.push({ role: 'assistant', content: text });
    addMessage('agent', text);
    if (data && Array.isArray(data.tools_used) && data.tools_used.length) {
        addToolResults(data.tools_used);
    }
    if (data && data.session_id && data.session_id !== currentSessionId) {
        setCurrentSession(data.session_id);
        renderSessions();
    }
    checkForCode(text);
    speak(text);
    refreshState().catch(() => {}); // receipts/sessions may have changed
}

function checkForCode(text) {
    const codeRegex = /```(?:[a-z]+)?\n([\s\S]*?)\n```/g;
    const match = codeRegex.exec(text);
    if (match) {
        document.getElementById('forge-code').textContent = match[1];
        document.getElementById('forge-meta').textContent = 'Code synthesized in The Forge';
    }
}

function handleInput(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
    }
}

/* ---------- settings / login / kill / health ---------- */

function openSettings() {
    renderSettingsBits();
    $('settings-modal').style.display = 'flex';
}

function closeSettings() {
    $('settings-modal').style.display = 'none';
}

function renderSettingsBits() {
    if (!state) return;
    const active = state.models.find((m) => m.id === state.active_model) || {};
    $('settings-model').textContent =
        (active.name || state.active_model) + ((state.active_model_name || active.model) ? ' (' + (state.active_model_name || active.model) + ')' : '');
    // Ollama + Qwen endpoint fields (effective values: saved setting wins over env).
    const ollamaInput = $('ollama-base-url');
    if (ollamaInput && document.activeElement !== ollamaInput) ollamaInput.value = state.ollama_base_url || '';
    const qwenInput = $('qwen-api-url');
    if (qwenInput && document.activeElement !== qwenInput) qwenInput.value = state.qwen_api_url || '';
    // Providers table: every provider with its real API URL, key env, status.
    const pt = $('providers-table');
    if (pt) {
        pt.innerHTML = '<table class="health-grid"><thead><tr><th>Provider</th><th>API URL</th><th>Key env</th><th>Status</th></tr></thead><tbody>' +
            state.models.map((m) =>
                '<tr><td>' + esc(m.name) + (m.id === 'qwen' && m.source ? '<br><small><a href="' + esc(m.source) + '" target="_blank" rel="noopener">Qwen3 fork (model source)</a></small>' : '') + '</td>' +
                '<td style="word-break:break-all;">' + esc(m.api_url || '—') + '</td>' +
                '<td>' + esc(m.key_env || '—') + '</td>' +
                '<td>' + (m.configured ? 'Configured' : 'Not configured') + '</td></tr>'
            ).join('') + '</tbody></table>' +
            '<p class="muted">Qwen routes: Groq-hosted Qwen3 first, local Ollama Qwen when Groq keys are absent, DashScope compat third. The fork itself does not run on Render.</p>';
    }
    $('login-status').textContent = !state.auth.enabled
        ? 'Auth is not configured on this server — tools are open, no login needed.'
        : state.auth.authenticated
            ? 'Logged in (session cookie set).'
            : 'Not logged in — auth-gated tools are locked until you unlock.';
    $('login-form').style.display = state.auth.enabled && !state.auth.authenticated ? 'block' : 'none';
    $('kill-status').textContent = state.kill_switch
        ? 'ACTIVE — chat is stopped (server returns 423)'
        : 'Off';
    $('kill-btn').disabled = !!state.kill_switch;
    $('meta-status').textContent = !state.meta.configured
        ? 'Meta OAuth is not configured on this server (add META_APP_ID / META_APP_SECRET).'
        : state.meta.profile
            ? 'Connected as ' + state.meta.profile.name
            : 'Meta OAuth ready — click Connect Meta below.';
    const metaBtn = $('meta-connect-btn');
    if (metaBtn) metaBtn.style.display = state.meta.configured && !state.meta.profile ? 'inline-block' : 'none';
    if (state.health && state.health.providers) renderHealthTable(state.health);
}

function openLogin(note) {
    if (note) toast(note);
    openSettings();
    const pw = $('login-password');
    if (pw) pw.focus();
    loginPromptShown = true;
}

async function submitLogin(e) {
    e.preventDefault();
    const pw = $('login-password').value;
    const errEl = $('login-error');
    errEl.textContent = '';
    let res;
    try {
        res = await fetch('/api/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ password: pw })
        });
    } catch (_) {
        errEl.textContent = 'Network error — could not reach the server.';
        return;
    }
    if (res.status === 204 || res.status === 200) {
        $('login-password').value = '';
        toast('Unlocked.');
        await refreshState();
    } else if (res.status === 401) {
        errEl.textContent = 'Wrong password.';
    } else {
        errEl.textContent = 'Login failed (HTTP ' + res.status + ').';
    }
}

async function activateKill() {
    if (!confirm('Activate the kill switch? Chat will stop (server returns 423). This cannot be undone from this UI.')) return;
    const res = await api('/api/kill', {}, 'POST');
    if (!res || res.ok === false) {
        toast('Kill switch failed: ' + ((res && res.error) || 'server error'));
        return;
    }
    toast('Kill switch ACTIVE — chat is stopped.');
    await refreshState();
}

function renderHealthTable(health) {
    const box = $('health-table');
    if (!health.providers || !health.providers.length) {
        box.innerHTML = '<p class="muted">Health check returned no providers.</p>';
        return;
    }
    box.innerHTML = '<table class="health-grid"><thead><tr><th>Provider</th><th>Configured</th><th>Status</th><th>Latency</th><th>Note</th></tr></thead><tbody>' +
        health.providers.map((p) =>
            `<tr><td>${esc(p.provider)}</td><td>${p.configured ? 'Yes' : 'No'}</td>` +
            `<td>${esc(p.status)}</td><td>${p.latency_ms == null ? '—' : p.latency_ms + ' ms'}</td>` +
            `<td>${esc(p.uptime || p.error_rate || '—')}</td></tr>`
        ).join('') + '</tbody></table>';
}

async function saveOllamaSettings() {
    const status = $('ollama-save-status');
    const body = {};
    const base = $('ollama-base-url').value.trim();
    const qwenUrl = $('qwen-api-url').value.trim();
    body.ollama_base_url = base;
    body.qwen_api_url = qwenUrl;
    const res = await api('/api/settings', body, 'POST');
    if (!res || res.ok === false) {
        if (status) status.textContent = 'Save failed: ' + ((res && res.error) || 'server error');
        toast('Ollama settings save failed');
        return;
    }
    if (status) status.textContent = 'Saved. Ollama base URL and Qwen API URL are now in effect.';
    await refreshState();
}

async function saveQwenModelSetting() {
    const status = $('qwen-model-status');
    const model = $('qwen-model-setting').value;
    const res = await api('/api/model', { model: 'qwen', model_name: model }, 'POST');
    if (!res || res.ok === false) {
        if (status) status.textContent = 'Failed: ' + ((res && res.error) || 'server error');
        return;
    }
    if (status) status.textContent = 'Qwen model set to ' + model + ' (local Ollama route).';
    await refreshState();
}

/* ---------- voice chat: mic input + spoken replies ---------- */

let recog = null;
let listening = false;

function speechRecognitionCtor() {
    return window.SpeechRecognition || window.webkitSpeechRecognition || null;
}

function initMic() {
    const btn = $('mic-btn');
    if (!btn) return;
    const Ctor = speechRecognitionCtor();
    if (!Ctor) {
        btn.disabled = true;
        btn.title = 'Speech recognition is not supported in this browser — type your message instead.';
        btn.setAttribute('aria-disabled', 'true');
        return;
    }
    if (!window.isSecureContext) {
        btn.disabled = true;
        btn.title = 'Speech recognition needs a secure page (HTTPS) — type your message instead.';
        btn.setAttribute('aria-disabled', 'true');
        return;
    }
    btn.disabled = false;
    btn.title = 'Speak your message';
}

function toggleMic() {
    const Ctor = speechRecognitionCtor();
    if (!Ctor || !window.isSecureContext) {
        toast('Speech recognition is not available here — type your message instead.');
        return;
    }
    if (listening) {
        try { recog.stop(); } catch (_) {}
        return;
    }
    recog = new Ctor();
    recog.lang = 'en-US';
    recog.interimResults = true;
    recog.continuous = false;
    let finalText = '';
    recog.onresult = (e) => {
        let interim = '';
        for (let i = e.resultIndex; i < e.results.length; i++) {
            const r = e.results[i];
            if (r.isFinal) finalText += r[0].transcript;
            else interim += r[0].transcript;
        }
        $('user-input').value = (finalText + interim).trim();
    };
    recog.onerror = (e) => {
        listening = false;
        $('mic-btn').classList.remove('listening');
        toast('Microphone failed: ' + (e.error || 'unknown error') + ' — type your message instead.');
    };
    recog.onend = () => {
        listening = false;
        $('mic-btn').classList.remove('listening');
        const input = $('user-input');
        if (finalText.trim()) {
            input.value = finalText.trim();
            sendMessage(); // transcribed speech is sent as a chat message
        }
    };
    try {
        recog.start();
        listening = true;
        $('mic-btn').classList.add('listening');
        toast('Listening… speak now.');
    } catch (err) {
        listening = false;
        toast('Could not start the microphone: ' + err.message);
    }
}

async function refreshHealth() {
    const box = $('health-table');
    box.innerHTML = '<p class="muted">Checking…</p>';
    const res = await api('/api/health', {}, 'POST');
    if (!res || res.ok === false || !res.health) {
        box.innerHTML = '<p class="muted">Health check unavailable: ' + esc((res && res.error) || 'server error') + '</p>';
        return;
    }
    renderHealthTable(res.health);
    await updateStatus();
}

/* ---------- command palette ---------- */

function paletteActions() {
    const actions = [
        { label: 'New session', hint: 'Create a chat session', run: () => newSession() },
        { label: 'Clear chat view', hint: 'Empty the message pane', run: () => clearChat() },
        { label: 'Open settings', hint: 'Login, kill switch, health', run: () => openSettings() },
        { label: 'Refresh health', hint: 'Re-check every provider', run: () => { openSettings(); refreshHealth(); } },
        { label: 'Show skills / tools', hint: 'Jump to the skills panel', run: () => toggleSkills() },
        { label: 'Copy forge code', hint: 'Copy the code in The Forge', run: () => copyForge() },
        { label: 'Toggle voice replies', hint: 'Read replies aloud on/off', run: () => { setSpeak(!$('speak-toggle').checked); } },
    ];
    if (state) {
        state.sessions.forEach((s) => actions.push({
            label: 'Resume: ' + s.title, hint: 'Session · ' + s.model,
            run: () => resumeSession(s.id)
        }));
        state.models.forEach((m) => actions.push({
            label: 'Switch model: ' + m.name, hint: m.configured ? 'Configured' : 'Key needed',
            run: () => changeModel(m.id)
        }));
        state.tools.forEach((t) => actions.push({
            label: (t.enabled ? 'Disable tool: ' : 'Enable tool: ') + t.name,
            hint: t.id === 'composio' ? 'Wired via Composio MCP' : 'Registered, not executable',
            run: () => toggleTool(t.id, !t.enabled, { checked: t.enabled })
        }));
    }
    return actions;
}

function togglePalette() {
    const p = $('command-palette');
    const opening = p.style.display === 'none';
    p.style.display = opening ? 'flex' : 'none';
    if (opening) {
        paletteItems = paletteActions();
        paletteSelected = 0;
        $('palette-search').value = '';
        filterPalette();
        $('palette-search').focus();
    }
}

function filterPalette() {
    const q = $('palette-search').value.trim().toLowerCase();
    const items = paletteItems.filter((it) =>
        !q || it.label.toLowerCase().includes(q) || (it.hint || '').toLowerCase().includes(q));
    paletteSelected = Math.max(0, Math.min(paletteSelected, items.length - 1));
    const box = $('palette-results');
    if (!items.length) {
        box.innerHTML = '<div class="palette-item">No matching actions.</div>';
        return;
    }
    box.innerHTML = items.map((it, i) =>
        `<div class="palette-item ${i === paletteSelected ? 'selected' : ''}" data-idx="${i}" onclick="runPaletteItem(${i})">` +
        `<b>${esc(it.label)}</b>${it.hint ? `<small>${esc(it.hint)}</small>` : ''}</div>`
    ).join('');
    box._items = items;
}

function runPaletteItem(i) {
    const items = $('palette-results')._items || [];
    const item = items[i];
    if (!item) return;
    $('command-palette').style.display = 'none';
    item.run();
}

function paletteKey(e) {
    const items = $('palette-results')._items || [];
    if (e.key === 'ArrowDown') {
        e.preventDefault();
        paletteSelected = Math.min(paletteSelected + 1, items.length - 1);
        filterPalette();
    } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        paletteSelected = Math.max(paletteSelected - 1, 0);
        filterPalette();
    } else if (e.key === 'Enter') {
        e.preventDefault();
        runPaletteItem(paletteSelected);
    } else if (e.key === 'Escape') {
        $('command-palette').style.display = 'none';
    }
}

/* ---------- forge ---------- */

function copyForge() {
    const code = document.getElementById('forge-code').textContent;
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(code).then(
            () => toast('Copied to clipboard.'),
            () => toast('Copy failed — select the code manually.'));
    } else {
        const ta = document.createElement('textarea');
        ta.value = code;
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand('copy'); toast('Copied to clipboard.'); }
        catch (_) { toast('Copy failed — select the code manually.'); }
        ta.remove();
    }
}
// NOTE: applyForge() was removed on purpose. It used to be an alert() mock and
// the backend has no /api/apply endpoint, so the Apply button is disabled
// with an honest note instead of a fake action.

/* ---------- voice ---------- */

function setSpeak(on) {
    $('speak-toggle').checked = on;
    $('speak-toggle-settings').checked = on;
    localStorage.setItem('vc_speak', on ? '1' : '0');
    if (!on && 'speechSynthesis' in window) speechSynthesis.cancel();
}

function initVoice() {
    const saved = localStorage.getItem('vc_speak');
    const on = saved === null ? true : saved === '1'; // default on, like the Nov 1 backup
    $('speak-toggle').checked = on;
    $('speak-toggle-settings').checked = on;
    $('speak-toggle').addEventListener('change', (e) => setSpeak(e.target.checked));
    $('speak-toggle-settings').addEventListener('change', (e) => setSpeak(e.target.checked));
    const voiceStatus = $('voice-status');
    if (!('speechSynthesis' in window)) {
        document.querySelectorAll('.voice-toggle').forEach((el) => {
            el.title = 'Voice output is not supported in this browser.';
        });
        if (voiceStatus) voiceStatus.textContent = 'Voice output is not supported in this browser — replies will show as text only.';
    } else if (voiceStatus) {
        voiceStatus.textContent = 'Voice output ready — replies are read aloud when the Voice toggle is on.';
    }
    initMic();
}

/* ---------- mobile drawers (PR #11 pattern, kept) ---------- */

function syncBackdrop() {
    const anyOpen = document.getElementById('mind-panel').classList.contains('open')
        || document.getElementById('forge-panel').classList.contains('open');
    document.getElementById('panel-backdrop').style.display = (anyOpen && window.innerWidth <= 900) ? 'block' : 'none';
}

function togglePanel(which) {
    const mind = document.getElementById('mind-panel');
    const forge = document.getElementById('forge-panel');
    if (which === 'mind') {
        mind.classList.toggle('open');
        forge.classList.remove('open');
    } else {
        forge.classList.toggle('open');
        mind.classList.remove('open');
    }
    syncBackdrop();
}

function closeMind() {
    document.getElementById('mind-panel').classList.remove('open');
    syncBackdrop();
}

function closeForge() {
    document.getElementById('forge-panel').classList.remove('open');
    syncBackdrop();
}

function closePanels() {
    document.getElementById('mind-panel').classList.remove('open');
    document.getElementById('forge-panel').classList.remove('open');
    syncBackdrop();
}

// Close panels when tapping outside on mobile
document.addEventListener('click', (e) => {
    if (window.innerWidth > 900) return;
    const mind = document.getElementById('mind-panel');
    const forge = document.getElementById('forge-panel');
    if (!e.target.closest('.mind-panel') && !e.target.closest('.forge-panel') && !e.target.closest('.mobile-bar')) {
        mind.classList.remove('open');
        forge.classList.remove('open');
        syncBackdrop();
    }
});

/* ---------- global keys & init ---------- */

window.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        togglePalette();
    } else if (e.key === 'Escape') {
        if ($('command-palette').style.display !== 'none') $('command-palette').style.display = 'none';
        else if ($('settings-modal').style.display !== 'none') closeSettings();
    }
});

document.addEventListener('DOMContentLoaded', () => {
    $('palette-search').addEventListener('keydown', paletteKey);
    $('settings-modal').addEventListener('click', (e) => {
        if (e.target === $('settings-modal')) closeSettings();
    });
});

async function init() {
    initVoice();
    await refreshState();
    await updateStatus();
    if (currentSessionId && state && !state.sessions.some((s) => s.id === currentSessionId)) {
        setCurrentSession(null); // stored session no longer exists server-side
    }
    if (state) renderSessions();
}

init();
