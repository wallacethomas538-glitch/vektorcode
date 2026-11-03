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

create index if not exists vektor_sessions_updated_idx
    on vektor_sessions (updated desc);
create index if not exists vektor_receipts_created_idx
    on vektor_receipts (created desc);
