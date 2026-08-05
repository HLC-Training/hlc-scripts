-- Applied to ORiON Supabase (czdkctjbejnwuopigxta) 2026-08-05 via MCP
-- migration name: tpm_digest_state
--
-- tpm_digest_state — watermark + audit log for the TPM P&C daily digest
-- to Michele (hlc-scripts/send_tpm_digest.py). One row per SUCCESSFUL
-- run, inserted only after the send in that run completed (or correctly
-- no-op'd via empty-suppression). The watermark is the latest row's
-- sent_at; a crashed run inserts nothing and therefore cannot advance it.
-- Separate table from pll_digest_state on purpose (brief, action item
-- c5cf4442) — the two digests must never share a watermark.
create table public.tpm_digest_state (
  id             uuid primary key default gen_random_uuid(),
  sent_at        timestamptz not null default now(),
  watermark_used timestamptz,          -- previous watermark this run computed "new since" from (null on first run)
  test_mode      boolean not null,     -- true = digest went to Jim only, not Michele
  summary        jsonb not null        -- per-TPM counts, suppressed flags, and section-1 project ids (boundary-date dedup)
);

comment on table public.tpm_digest_state is
  'TPM P&C daily digest (to Michele) watermark/audit. Written by hlc-scripts/send_tpm_digest.py (service role) only.';

-- RLS on, NO policies: readable/writable by service role only, matching
-- pll_digest_state's posture. Never add user-facing policies here.
alter table public.tpm_digest_state enable row level security;
