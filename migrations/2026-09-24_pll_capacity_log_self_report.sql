-- Applied to SAM COS Supabase (hucrkbomqsxpmokgypxg) 2026-09-24 via MCP
-- migration name: pll_capacity_log_self_report
--
-- pll_capacity_log gains a third source, 'self_report' — the PLL's own
-- monthly ORiON workload check-in, synced up by
-- hlc-scripts/sync_workload_checkins.py (service role). Jim's reads
-- (meeting / eod) are untouched: existing rows, existing writers (Meetings
-- project, EOD skill), existing CHECK on capacity_read all stay as they were.
-- Decision: orion-pll knowledge/decisions/2026-09-24-workload-checkin-v1.md
-- (SAM COS action item ed6e818e).
--
-- orion_checkin_id = the ORiON workload_checkins.id the row was derived
-- from. NULL on every meeting/eod row. UNIQUE so the sync is idempotent
-- (upsert on_conflict=orion_checkin_id, ignore_duplicates) — a second run
-- adds nothing and never rewrites a row.

ALTER TABLE public.pll_capacity_log
  DROP CONSTRAINT pll_capacity_log_source_check;

ALTER TABLE public.pll_capacity_log
  ADD CONSTRAINT pll_capacity_log_source_check
  CHECK (source = ANY (ARRAY['meeting'::text, 'eod'::text, 'self_report'::text]));

ALTER TABLE public.pll_capacity_log
  ADD COLUMN orion_checkin_id uuid UNIQUE;

COMMENT ON COLUMN public.pll_capacity_log.orion_checkin_id IS
  'ORiON workload_checkins.id this self_report row was synced from (hlc-scripts/sync_workload_checkins.py). NULL for meeting/eod rows. UNIQUE = sync idempotency key.';
