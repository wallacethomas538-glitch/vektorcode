async function api(path, data = null, method = 'GET') {
    const options = {
        method: method,
        headers: { 'Content-Type': 'application/json' }
    };
    if (data) options.body = JSON.stringify(data);
    const res = await fetch(path, options);
    if (res.status === 401) {
        alert('Authentication required. Please login.');
        return null;
    }
    return res.json();
}

async function init() {
    await loadModels();
    await loadSessions();
    await updateStatus();
}

async function loadModels() {
    const state = await api('/api/state');
    const select = document.getElementById('active-model');
    select.innerHTML = state.models.map(m => 
        `<option value="${m.id}" ${m.id === state.active_model ? 'selected' : ''}>${m.name}</option>`
    ).join('');
}

async function loadSessions() {
    const state = await api('/api/state');
    const list = document.getElementById('session-list');
    list.innerHTML = state.sessions.map(s => 
        `<div class="session-item" onclick="resumeSession('${s.id}')">${s.title}</div>`
    ).join('');
}

async function updateStatus() {
    const state = await api('/api/state');
    document.getElementById('mcp-status').innerText = state.mcp.connected ? 'Online' : 'Offline';
}

async function changeModel(val) {
    await api('/api/model', { model: val }, 'POST');
    updateStatus();
}

async function resumeSession(id) {
    await api('/api/session/resume', { id }, 'POST');
    // In a real app, we'd load the messages here
    addMessage('system', 'Session resumed. How can the Master Architect help you today?');
}

function addMessage(role, text) {
    const flow = document.getElementById('chat-flow');
    const msg = document.createElement('div');
    msg.className = `message ${role}`;
    msg.innerText = text;
    flow.appendChild(msg);
    flow.scrollTop = flow.scrollHeight;
}

async function sendMessage() {
    const input = document.getElementById('user-input');
    const text = input.value.trim();
    if (!text) return;

    addMessage('user', text);
    input.value = '';

    const res = await api('/api/chat', { 
        messages: [{ role: 'user', content: text }],
        model: document.getElementById('active-model').value
    }, 'POST');

    if (res && res.ok) {
        addMessage('agent', res.message);
        checkForCode(res.message);
    } else {
        addMessage('agent', 'Error: ' + (res?.error || 'Unknown failure'));
    }
}

function checkForCode(text) {
    const codeRegex = /```(?:[a-z]+)?\n([\s\S]*?)\n```/g;
    const match = codeRegex.exec(text);
    if (match) {
        const code = match[1];
        document.getElementById('forge-code').innerText = code;
        document.getElementById('forge-meta').innerText = 'Code synthesized in The Forge';
    }
}

function handleInput(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
    }
    if (e.ctrlKey && e.key === 'k') {
        e.preventDefault();
        togglePalette();
    }
}

function togglePalette() {
    const p = document.getElementById('command-palette');
    p.style.display = p.style.display === 'none' ? 'flex' : 'none';
    if (p.style.display === 'flex') document.getElementById('palette-search').focus();
}

function clearChat() {
    document.getElementById('chat-flow').innerHTML = '';
}

function copyForge() {
    const code = document.getElementById('forge-code').innerText;
    navigator.clipboard.writeText(code);
    alert('Copied to clipboard!');
}

async function applyForge() {
    const code = document.getElementById('forge-code').innerText;
    // This would call a specific /api/apply endpoint
    alert('Applying code to system... (Requires backend endpoint)');
}

window.addEventListener('keydown', (e) => {
    if (e.ctrlKey && e.key === 'k') togglePalette();
});

init();

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
}
function closeForge() {
    document.getElementById('forge-panel').classList.remove('open');
}
// Close panels when tapping outside on mobile
document.addEventListener('click', (e) => {
    if (window.innerWidth > 900) return;
    const mind = document.getElementById('mind-panel');
    const forge = document.getElementById('forge-panel');
    if (!e.target.closest('.mind-panel') && !e.target.closest('.forge-panel') && !e.target.closest('.mobile-bar')) {
        mind.classList.remove('open');
        forge.classList.remove('open');
    }
});
