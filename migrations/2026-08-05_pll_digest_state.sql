-- Applied to ORiON Supabase (czdkctjbejnwuopigxta) 2026-08-05 via MCP
-- migration name: pll_digest_state
--
-- pll_digest_state — watermark + audit log for the PLL daily digest
-- (hlc-scripts/send_pll_digest.py). One row per SUCCESSFUL run, inserted
-- only after every send in that run completed (or all PLLs correctly
-- no-op'd). The watermark is the latest row's sent_at; a crashed run
-- inserts nothing and therefore cannot advance it.
create table public.pll_digest_state (
  id             uuid primary key default gen_random_uuid(),
  sent_at        timestamptz not null default now(),
  watermark_used timestamptz,          -- previous watermark this run computed "new since" from (null on first run)
  test_mode      boolean not null,     -- true = digests went to Jim only
  summary        jsonb not null        -- per-PLL counts, suppressed flags, and section-1 item ids (boundary-date dedup)
);

comment on table public.pll_digest_state is
  'PLL daily digest watermark/audit. Written by hlc-scripts/send_pll_digest.py (service role) only.';

-- RLS on, NO policies: readable/writable by service role only, like the
-- other job-owned tables. Never add user-facing policies here.
alter table public.pll_digest_state enable row level security;
