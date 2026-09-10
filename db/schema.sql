-- Undercurrent schema (Section 4, refined during implementation).
--
-- Refinements vs. the spec's sketch, and why:
--   * raw_items gains content_hash / normalized_title / url_norm so exact and near
--     duplicate detection (Section 5) stays deterministic code, not an LLM call.
--   * signals gains theme_id + score so the digest can rank without a join storm.
--   * run_logs / source_runs / llm_usage added for Section 12 ("fail loud": log
--     source counts, failures, processing time, delivery status, LLM budget).
--   * x_query_state added for Section 3 rotating X queries without repeats.
--   * No graph extension (Section 14): plain tables + a relationships edge table.
--     Volume here is tiny; a graph DB would be a second dependency for no gain.
--
-- Apply by pasting into the Supabase SQL editor, or:
--   psql "$DATABASE_URL" -f db/schema.sql

create extension if not exists "pgcrypto";

-- ---------------------------------------------------------------- raw layer --

create table if not exists raw_items (
  id               uuid primary key default gen_random_uuid(),
  source           text not null,
  external_id      text not null,
  title            text,
  url              text,
  url_norm         text,
  normalized_title text,
  content_snippet  text,
  content_hash     text,
  author           text,
  score            numeric,
  collected_at     timestamptz not null default now(),
  published_at     timestamptz,
  raw_metadata     jsonb not null default '{}'::jsonb,
  unique (source, external_id)
);

create index if not exists raw_items_collected_at_idx on raw_items (collected_at desc);
create index if not exists raw_items_published_at_idx on raw_items (published_at desc);
create index if not exists raw_items_content_hash_idx on raw_items (content_hash);
create index if not exists raw_items_url_norm_idx on raw_items (url_norm);
create index if not exists raw_items_source_idx on raw_items (source);

-- ------------------------------------------------------------------ memory --

create table if not exists entities (
  id             uuid primary key default gen_random_uuid(),
  entity_type    text,                                  -- company | person | product | lab | technology
  canonical_name text not null,
  description    text,
  status         text not null default 'unconfirmed',   -- unconfirmed | confirmed
  metadata       jsonb not null default '{}'::jsonb,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),
  unique (entity_type, canonical_name)
);

create table if not exists entity_aliases (
  id        uuid primary key default gen_random_uuid(),
  entity_id uuid not null references entities(id) on delete cascade,
  alias     text not null,
  unique (alias)
);

create index if not exists entity_aliases_entity_idx on entity_aliases (entity_id);

create table if not exists themes (
  id             uuid primary key default gen_random_uuid(),
  name           text not null unique,
  slug           text unique,
  description    text,
  status         text not null default 'active',   -- active | decayed | dormant
  theme_type     text default 'general',           -- drives decay half-life
  momentum_score numeric not null default 0,
  baseline_score numeric not null default 0,
  last_signal_at timestamptz,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);

create index if not exists themes_momentum_idx on themes (momentum_score desc);

create table if not exists problems (
  id          uuid primary key default gen_random_uuid(),
  description text not null,
  slug        text unique,
  domain      text,
  intensity   numeric not null default 0,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

-- ----------------------------------------------------------------- signals --

create table if not exists signals (
  id          uuid primary key default gen_random_uuid(),
  raw_item_id uuid not null references raw_items(id) on delete cascade,
  theme_id    uuid references themes(id) on delete set null,
  signal_type text,          -- pain | launch | research | capability | traction | discussion
  novelty     numeric,
  relevance   numeric,
  confidence  numeric,
  score       numeric,
  metadata    jsonb not null default '{}'::jsonb,
  created_at  timestamptz not null default now(),
  unique (raw_item_id)
);

create index if not exists signals_created_at_idx on signals (created_at desc);
create index if not exists signals_theme_idx on signals (theme_id);
create index if not exists signals_score_idx on signals (score desc);

create table if not exists relationships (
  id         uuid primary key default gen_random_uuid(),
  from_type  text not null,
  from_id    uuid not null,
  relation   text not null,   -- near_duplicate_of | mentions | relates_to | contradicts | evidence_for
  to_type    text not null,
  to_id      uuid not null,
  confidence numeric,
  evidence   jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  unique (from_type, from_id, relation, to_type, to_id)
);

create index if not exists relationships_from_idx on relationships (from_type, from_id);
create index if not exists relationships_to_idx on relationships (to_type, to_id);

create table if not exists observations (
  id               uuid primary key default gen_random_uuid(),
  theme_id         uuid references themes(id) on delete cascade,
  observation_type text,   -- strengthened | weakened | convergence | contradiction | new
  statement        text not null,
  score            numeric,
  observed_at      timestamptz not null default now(),
  evidence         jsonb not null default '{}'::jsonb
);

create index if not exists observations_observed_at_idx on observations (observed_at desc);

create table if not exists hypotheses (
  id              uuid primary key default gen_random_uuid(),
  hypothesis_type text,     -- opportunity | research | startup | project | trend
  statement       text not null,
  rationale       text,
  confidence      numeric,
  status          text not null default 'open',   -- open | supported | weakened | retired
  theme_id        uuid references themes(id) on delete set null,
  evidence        jsonb not null default '{}'::jsonb,
  dedup_key       text unique,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);

create index if not exists hypotheses_status_idx on hypotheses (status, confidence desc);

-- ------------------------------------------------------------------ output --

create table if not exists digests (
  id          uuid primary key default gen_random_uuid(),
  digest_date date not null unique,
  content     text not null,
  item_count  int not null default 0,
  delivered   boolean not null default false,
  created_at  timestamptz not null default now()
);

create table if not exists digest_items (
  digest_id   uuid not null references digests(id) on delete cascade,
  raw_item_id uuid not null references raw_items(id) on delete cascade,
  primary key (digest_id, raw_item_id)
);

-- ------------------------------------------------------------- operational --

create table if not exists run_logs (
  id            uuid primary key default gen_random_uuid(),
  run_date      date not null default current_date,
  started_at    timestamptz not null default now(),
  finished_at   timestamptz,
  status        text not null default 'running',   -- running | ok | partial | failed
  items_fetched int not null default 0,
  items_new     int not null default 0,
  duration_ms   int,
  delivery      text,
  errors        jsonb not null default '[]'::jsonb,
  stats         jsonb not null default '{}'::jsonb
);

create index if not exists run_logs_started_at_idx on run_logs (started_at desc);

create table if not exists source_runs (
  id                 uuid primary key default gen_random_uuid(),
  run_id             uuid references run_logs(id) on delete cascade,
  source             text not null,
  items_fetched      int not null default 0,
  items_after_filter int not null default 0,
  items_new          int not null default 0,
  duration_ms        int,
  error              text,
  created_at         timestamptz not null default now()
);

create index if not exists source_runs_run_idx on source_runs (run_id);

create table if not exists llm_usage (
  id         uuid primary key default gen_random_uuid(),
  run_id     uuid references run_logs(id) on delete set null,
  usage_date date not null default current_date,
  provider   text,
  model      text,
  purpose    text,          -- extraction | disambiguation | synthesis
  calls      int not null default 1,
  batch_size int,
  ok         boolean not null default true,
  error      text,
  created_at timestamptz not null default now()
);

create index if not exists llm_usage_date_idx on llm_usage (usage_date);

-- Section 3: rotate X queries, never repeat the exact query day to day.
create table if not exists x_query_state (
  id        uuid primary key default gen_random_uuid(),
  query     text not null,
  layer     text,
  last_used timestamptz,
  use_count int not null default 0,
  unique (query)
);

create index if not exists x_query_state_last_used_idx on x_query_state (last_used nulls first);

-- ---------------------------------------------------------------- security --
--
-- Undercurrent is headless: its only client is the pipeline, authenticating with
-- the service_role key, which bypasses RLS. Enabling RLS with *no policies* on
-- every table therefore denies the anon and authenticated roles everything while
-- leaving the pipeline untouched. This matters because Supabase's anon key is
-- public by design -- without this, any holder of it could read and rewrite the
-- entire research memory through PostgREST.
--
-- The Supabase linter reports these as INFO "RLS enabled, no policy". That is
-- the intended end state here, not an oversight; do not add policies unless a
-- non-service_role client is ever introduced.

alter table raw_items      enable row level security;
alter table entities       enable row level security;
alter table entity_aliases enable row level security;
alter table themes         enable row level security;
alter table problems       enable row level security;
alter table signals        enable row level security;
alter table relationships  enable row level security;
alter table observations   enable row level security;
alter table hypotheses     enable row level security;
alter table digests        enable row level security;
alter table digest_items   enable row level security;
alter table run_logs       enable row level security;
alter table source_runs    enable row level security;
alter table llm_usage      enable row level security;
alter table x_query_state  enable row level security;
