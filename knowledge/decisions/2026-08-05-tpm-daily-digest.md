# TPM P&C daily digest to Michele — design decisions (2026-08-05)

Action item of record: SAM COS `c5cf4442`. Job: `send_tpm_digest.py` +
`.github/workflows/tpm-digest.yml`. State: `tpm_digest_state` (ORiON
Supabase `czdkctjbejnwuopigxta`). Near-clone of the PLL digest
(action item `37425f71`, `2026-08-05-pll-daily-digest.md`) — this doc
only covers what differs.

## Scope-of-record drift, noted and resolved

The action item's own notes (written before the brief was finalized)
describe the data source as `action_items` via the AP Smartsheet sync,
and ask to verify `created_date` on that table. The brief that actually
shipped supersedes this: P&C's "project" is `pc_projects`, not
`action_items` — same concept as a Delivery action item, different
table and different word. All verification below was done against
`pc_projects`, confirmed against the live schema before writing any
code. Flagging this so the action item's notes aren't read as
authoritative over the brief if either is revisited later.

## Table + column mapping (confirmed live 2026-08-05)

- Insert date: `pc_projects.created_at` (timestamptz, `default now()`).
  Verified via `pg_trigger` that `pc_projects` has **no** triggers at
  all — unlike `action_items.last_updated`'s `BEFORE UPDATE` trigger
  (PLL digest's whole reason for avoiding it), there is no DB-level
  bump on any pc_projects column. Also verified in `sync_ap.py`'s P&C
  update path (`fields` dict, lines ~705-724): `created_at` is never
  included in the fields an update ever writes. So `created_at` is a
  clean, immutable insert timestamp for AP-synced rows.
- Because `created_at` is a full timestamptz (not a bare date like
  `action_items.created_date`), "new since last digest" is a direct
  `created_at > watermark` timestamp comparison — no date-truncation
  boundary exists, so unlike the PLL digest, **no reported-ids
  exclusion list is needed** to prevent double-counting a same-day
  boundary case.
- Target date: `target_end_date` (date). Used for both the "coming up"
  and "overdue" sections, matching the brief.
- Owner: `owner_id` → `portal_users`. The digest fetches
  `portal_users` where `role = 'tpm'` first (9 live rows), then
  `pc_projects` filtered to those owner ids — mirrors the PLL digest's
  `fetch_plls` → `fetch_items` shape exactly. One live pc_projects row
  is owned by a `director`, not a TPM; it's correctly excluded since
  it isn't in the TPM owner-id set.

## Active/done status set

`pc_projects.status` is free text. Live distinct values as of
2026-08-05: `active` (11), `approved` (30), `on_hold` (1) — none of
the app's CHECK constraint's terminal values are present yet. Rather
than hardcode an assumed "active" whitelist (which would have missed
the 30 `approved` rows — most of the live data), the filter is built
as `status NOT IN DONE_STATUSES`, where `DONE_STATUSES = {complete,
cancelled}` — the exact TERMINAL set from ORiON's own lifecycle
doctrine ("complete / cancelled: TERMINAL — no reopen, no edits,
ever"). This means `approved` and `on_hold` both count as "active" for
digest purposes, which matches how AP-synced projects actually behave
today (`sync_ap.py` writes `approved` for Smartsheet "Not Started" and
`active` for "In Progress" — both are live, trackable projects with
real target dates, not administrative pre-states like `draft` or
`submitted`).

## Roll-up shape

One combined email to Michele, not per-TPM emails and not a cc to Jim.
Structurally: three sections (New projects / Coming up in the next two
weeks / Overdue) each containing per-TPM sub-groups, populated only for
TPMs with items in that particular section. A TPM absent from every
section (6 of 9 live TPMs, as of this build) simply never appears in
the email. The overdue cap (5 longest-overdue + "+N more") is applied
**per TPM**, not globally, matching the PLL digest's per-person cap.

There is no second "roll-up to Jim" email the way the PLL digest has
one (per-PLL emails + a separate summary email to Jim) — this digest
already *is* the one combined summary, so the TEST-mode copy sent to
Jim during rollout is the exact render Michele will eventually
receive, not a separate audit view.

## Watermark

Separate table (`tpm_digest_state`), same shape as `pll_digest_state`
(`id`, `sent_at`, `watermark_used`, `test_mode`, `summary`), same
posture (RLS on, no policies, service-role only). Advances only after
a successful send *or* a correctly-suppressed empty run — an empty run
still needs to move the window forward so a future non-empty run
doesn't have to re-scan every P&C project ever created. Same 72-hour
first-run fallback as the PLL digest.

## Holiday list — factored to a shared module

The PLL digest's 2026 GE Vernova holiday set was inline in
`send_pll_digest.py`. Factored out to `ge_holidays.py` (new file,
`HOLIDAYS` set + the `send_decision` weekday/holiday gate function) so
both jobs read one calendar — refresh it in one place for 2027.
`send_pll_digest.py` now imports from it; its local copies were
deleted, behavior unchanged (verified via `--check-date` before/after
on a weekday, a holiday, and a weekend date).

## Cron slot

`21 11 * * 1-5` — 11:21 UTC = 6:21 AM Chicago during CDT. Minute 21 is
distinct from every other cron slot already running in this repo (PLL
digest `:07`, AP sync `:13,:43`, xyleme sync `:17,:47`; `:00`/`:30` are
the known-contended slots per `tasks/lessons.md`) so this job and the
PLL digest can't collide on the same GitHub Actions slot.

## Test-first rollout — stays in test past verification

Default mode is TEST: the full render goes to Jim with a
`[TEST — would go to Michele]` subject prefix. Live requires the repo
variable `TPM_DIGEST_LIVE=true`, a **separate** variable from the PLL
digest's `PLL_DIGEST_LIVE` — enabling one must never enable the other,
verified by inspection (each script reads its own env var name only).

Unlike the PLL digest (which goes live once the 7 PLLs are confirmed
receiving it), this one is scoped by Jim to stay in TEST **indefinitely
after verification** — the go-live trigger is tied to P&C's non-AP
project import landing and Michele's team launching on ORiON, not to
this build being correct. Verified-and-working is not the same
condition as go-live; action item `c5cf4442` stays open until Michele
is actually receiving it.

## Verification evidence (2026-08-05)

- Migration confirmed live via `information_schema.columns`.
- `--check-date` confirmed the weekday/holiday gate unchanged after the
  `ge_holidays.py` refactor.
- `workflow_dispatch --dry-run --force-date-gate` (run 31034948916):
  correct grouping, correct fallback-window watermark, 3/9 TPMs with
  items, correct per-TPM overdue detail, nothing sent, no state
  written.
- Real `workflow_dispatch` (dry_run=false) on today's ordinary weekday
  (run 31035095693): TEST-mode email actually sent to Jim, subject
  `[TEST — would go to Michele] Your team's P&C projects — Wednesday,
  August 5`; `tpm_digest_state` row inserted only after the send
  succeeded (`test_mode: true`, `watermark_used` = the fallback
  timestamp computed that run).
- Overdue cap + empty-suppression: `build_digest()` is a pure function
  (no I/O), so both were proven with synthesized in-memory rows rather
  than a rolled-back Supabase transaction — `tasks/lessons.md` already
  flags multi-statement Supabase-MCP transactions as unreliable, so a
  direct unit-style proof was the more trustworthy evidence here. 7
  synthesized overdue rows → exactly 5 shown + overflow 2, longest-
  overdue first; an all-terminal-status TPM → suppressed (excluded
  from the roll-up).
- Fetch-failure: ran locally with a deliberately invalid
  `ORION_SUPABASE_SERVICE_KEY` → 401 from Supabase, caught, logged,
  `sys.exit(1)`, zero sends attempted.

## Env naming

`ORION_SUPABASE_SERVICE_KEY`, never the generic name (lessons.md
2026-08-03).
