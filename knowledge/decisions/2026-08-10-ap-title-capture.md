# Decision: AP title capture in sync_ap.py (parent rows → ap_titles)

Decided 2026-08-10 (Jim's brief; action item SAM COS
`23d5e80a-45dc-48d8-946a-f0177a61d578`). Sync-side half of the
AP-name-in-header build; the schema-shape decision and UI wiring are
documented in orion-pll's
`knowledge/decisions/2026-08-10-ap-title-header-display.md`.

## What was added

`fetch_child_ap_tasks()` now also returns `parent_titles`: for every
TOP-LEVEL parent/summary row in the AP tracker (`Is Parent` set, AP#
matching `^AP-\d+$` exactly — e.g. `AP-0621`, never `AP-0621-1`), the AP
number, the primary "Improvement" text (the AP's real title), and the
Smartsheet row id. The capture runs before the existing child filters and
never short-circuits them — `child_tasks` comes out byte-identical to the
pre-capture behavior, verified by the harness in this build (child insert
behavior unchanged with capture active).

`main()` diffs those titles against the existing `ap_titles` table
(shared ORiON Supabase project `czdkctjbejnwuopigxta`) and upserts only
new/changed rows, in batches of 25, with an explicit `updated_at` — the
table has no BEFORE UPDATE trigger (checked at migration time, per this
file's own repeated trigger-before-timestamp guidance), so `updated_at`
means "the title last actually changed", not "the sync last ran".

## Failure-path decision (the one this doc exists to record)

Three distinct failure classes, three deliberate behaviors:

1. **Malformed parent rows** (a genuine top-level parent with a blank
   Improvement cell, or a duplicate top-level parent row): log a warning,
   skip the row, run stays green. Same class as unmapped leads —
   Smartsheet data-quality noise, expected, not actionable from CI.
   Mid-level summary rows (`Is Parent` on `AP-0621-1`) are expected sheet
   structure and are skipped silently, no log.
2. **`ap_titles` read or write failure**: the run **exits non-zero, but
   only after everything else has completed**. The child sync (the
   production purpose of the run) finishes all its writes first, both
   heartbeats fire, the summary prints with `[TITLE CAPTURE FAILED]`,
   and then `sys.exit(1)`. Rationale: a green run that silently dropped
   titles is the same lie as the 2026-07-30 fetch-failure incident
   ("exit 0 on a failed fetch"), just smaller — but a display-title
   write must never abort or precede the primary child sync either.
   Title writes are therefore sequenced AFTER the child write phase.
3. **Smartsheet fetch failure**: unchanged — whole run exits non-zero
   as before (bug 306cea89 fix untouched).

The completion heartbeat (`health:github:orion_ap_sync`) still fires on a
title-capture failure, deliberately: it is the *child-sync* health signal
the dashboard keys on, and the child sync did succeed. The red Actions
run is the signal for the title failure. Notes now carry
`titles_captured` / `titles_written` / `title_capture_failed` so the
heartbeat record shows the distinction.

## Verified

- Forced-failure harness (real `main()`, stubbed DB where only the
  `ap_titles` upsert raises): child `action_items` insert completed
  before the failure surfaced, exit code 1, zero title writes recorded.
  Success scenario: exit 0, both fixture titles upserted with explicit
  `updated_at`.
- Live: first real run after this change captured the tracker's
  top-level titles into `ap_titles` (side-by-side values in the build
  session's gate output), with both heartbeats updated.

## Boundaries kept

Child-row sync behavior, cron schedule, exit-code discipline for the
existing paths, and the heartbeat pattern are all unchanged. No writes to
any table other than `ap_titles` were added. `ORION_SUPABASE_SERVICE_KEY`
naming untouched.

Status: active.
