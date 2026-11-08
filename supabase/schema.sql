-- VektorCode durable dashboard storage for Supabase.
-- Run once in the Supabase SQL editor before setting SUPABASE_URL/SUPABASE_KEY.
-- VektorCode talks to these tables through the PostgREST API.

create table if not exists vektor_settings (
    key text primary key,
    value jsonb not null,
    updated_at double precision not null default 0
);

create table if not exists vektor_sessions (
    id text primary key,
    title text,
    model text,
    tokens integer not null default 0,
    cost double precision not null default 0,
    active boolean not null default false,
    created double precision not null,
    updated double precision not null
);

create table if not exists vektor_receipts (
    id text primary key,
    action text,
    detail jsonb,
    sensitive boolean not null default false,
    approved boolean not null default false,
    created double precision not null
);

-- Chat history (Track A, 2026-11-05): one row per user/assistant message.
create table if not exists vektor_messages (
    id text primary key,
    session_id text not null references vektor_sessions(id) on delete cascade,
    role text not null,
    content text not null,
    model text,
    provider text,
    created double precision not null
);

create index if not exists vektor_messages_session_idx
    on vektor_messages (session_id, created);

create index if not exists vektor_sessions_updated_idx
    on vektor_sessions (updated desc);
create index if not exists vektor_receipts_created_idx
    on vektor_receipts (created desc);

-- Long-term memory (Option B web access, 2026-11-07): durable
-- cross-session memories the model reads at the start of every chat.
-- Kinds: fact / preference / decision / lesson. One row per (kind, key);
-- saving the same kind+key replaces the value. Loaded into the system
-- prompt under a 4,000-char cap, most-recent-first per kind.
-- Writes come from Wallace in the UI, from "save to memory" in chat, or
-- from a model suggestion Wallace approves — never silent model writes.
-- Run this block in the Supabase SQL editor (no RLS — same as the
-- existing tables; enabling RLS with no policies would block the app).
create table if not exists vektor_memory (
    kind text not null check (kind in ('fact', 'preference', 'decision', 'lesson')),
    key text not null,
    value text not null,
    updated_at double precision not null default 0,
    primary key (kind, key)
);

create index if not exists vektor_memory_updated_idx
    on vektor_memory (updated_at desc);

-- Universal MCP registry (2026-11-07, Wallace's order): VektorCode can
-- talk to every HTTP MCP server, not only Composio. One row per server.
-- The registry stores env var NAMES only (auth_env) — never a secret
-- value; the key itself lives in the Render env. Transports: streamable-http
-- (default) or sse. Stdio is not supported on Render free.
-- enabled=false hides the server from the model and from calls;
-- expose_to_model=false keeps it callable by Wallace (API/UI test button)
-- but never declared to the model.
-- Run this block in the Supabase SQL editor (no RLS — same as the
-- existing tables; enabling RLS with no policies would block the app).
create table if not exists vektor_mcp_servers (
    id text primary key,
    name text not null,
    url text not null,
    transport text not null default 'streamable-http'
        check (transport in ('streamable-http', 'sse')),
    auth_kind text not null default 'none'
        check (auth_kind in ('none', 'bearer', 'api-key', 'header')),
    auth_header text not null default '',
    auth_env text not null default '',
    enabled boolean not null default true,
    expose_to_model boolean not null default true,
    updated_at double precision not null default 0
);

-- MCP egress hosts (mechanism B): Wallace-managed hosts that MCP calls
-- may reach, in addition to the hardcoded allowlist. MCP-only — the web
-- fetch / model cascade never sees these hosts. A server's host is added
-- automatically when the server is saved (app-side); rows can also be
-- managed directly here. The app reads this table on a 60s cache.
create table if not exists vektor_mcp_hosts (
    host text primary key,
    added_by text not null default 'wallace',
    created double precision not null default 0
);

-- Seed: Composio (the default server; its URL/key come from the Render
-- env — COMPOSIO_MCP_URL / COMPOSIO_API_KEY — the row only names them).
-- The composio row cannot be deleted from the registry UI.
insert into vektor_mcp_servers
    (id, name, url, transport, auth_kind, auth_header, auth_env,
     enabled, expose_to_model, updated_at)
values
    ('composio', 'Composio', 'https://connect.composio.dev/mcp',
     'streamable-http', 'header', 'x-consumer-api-key',
     'COMPOSIO_API_KEY', true, true, extract(epoch from now()))
on conflict (id) do nothing;

-- Seed: DeepWiki (live-probed 2026-11-07 at build time — tools/list
-- returned 3 tools: ask_wiki_question, read_wiki_contents,
-- read_wiki_structure). Public, no key. Docs for any public GitHub repo.
insert into vektor_mcp_servers
    (id, name, url, transport, auth_kind, auth_header, auth_env,
     enabled, expose_to_model, updated_at)
values
    ('deepwiki', 'DeepWiki', 'https://mcp.deepwiki.com/mcp',
     'streamable-http', 'none', '', '', true, true,
     extract(epoch from now()))
on conflict (id) do nothing;

-- Seed the MCP hosts for the two servers above (mechanism B).
insert into vektor_mcp_hosts (host, added_by, created)
values
    ('connect.composio.dev', 'wallace', extract(epoch from now())),
    ('mcp.deepwiki.com', 'wallace', extract(epoch from now()))
on conflict (host) do nothing;
