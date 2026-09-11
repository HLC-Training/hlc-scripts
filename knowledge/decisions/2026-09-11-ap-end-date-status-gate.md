# AP end-date event logger — status gate (bug ca9beaeb Phase 2, Task 1)

**Date:** 2026-09-11
**Bug:** SAM COS `ca9beaeb` (open — Phase 3 remains)
**Status:** Decided & implemented
**Related:** `2026-08-10-ap-end-date-detection.md` (the mechanism this
gates), orion-pll's `knowledge/decisions/2026-08-10-ap-end-date-gate-and-ack.md`
(the consuming ack flow), orion-pll's
`knowledge/decisions/2026-09-11-ap-date-ack-loop-diagnosis.md` (the
diagnosis that led here — "root cause: no notification channel," with a
Phase 2 grill-findings addendum on `ca9beaeb` identifying this phantom-event
bug separately from that diagnosis's own scope).

## What ships

The end-date detection (`fetch_ap_rows()` / `main()`'s AP title diff) fired
an `ap_end_date_changes` event on ANY parent-AP Current-Finish diff,
regardless of the parent's own status. On 2026-09-01, after Jen removed the
sheet's finish-date dependency, a bulk cleanup of stale historical dates on
COMPLETED APs fired 11 phantom events across 10 completed APs (AP-0291,
0295, 0369, 0413, 0414, 0415, 0469, 0476, 0570, 0791) — nobody should be
asked to acknowledge a date correction on a finished AP.

Fix: `parent_titles` now captures the parent row's own "Overall Status"
(`COL_STATUS`, same cell already read for every row — no extra fetch) as
`status_raw`. The event-append branch of the diff gates on
`p['status_raw'] in ACTIVE_STATUSES` (`{"Not Started", "In Progress"}`,
already defined in `sync_ap.py` and already used per-task as the sync's own
notion of "active" — reused rather than reimplemented, per the
2026-08-11 digest lesson against parallel `ACTIVE_STATUSES` definitions
drifting apart). A gated-out change still baselines: `write_end = new_end`
happens unconditionally before the event/skip branch, so the stored
`ap_titles.end_date` always tracks the sheet — a completed AP that later
reactivates gets a correct diff baseline, not a stale one. Skips are logged
(`End-date move skipped (status gate): <AP> status=<raw> <old> -> <new> —
date baselined, no event`) and counted (`date_events_skipped_inactive`),
threaded through `build_sync_accounting`, the dry-run/live summary lines,
and the SAM COS health heartbeat notes, alongside the existing
`date_baselines`/`date_events_detected`/`date_events_written` fields.

## Why gate on the sheet's own status, not a downstream lookup

Verified before writing anything (this was the build's flagged unknown):
`COL_STATUS` is read into `cells` at the top of the per-row loop in
`fetch_ap_rows()`, and the top-level-parent capture block that builds
`parent_titles` runs inside that same loop, over the same `cells` dict,
before `status_raw` is computed for the task-row shape further down. Adding
`"status_raw": str(cells.get(COL_STATUS) or "").strip()` to the
`parent_titles.append()` call is a same-row, same-fetch read — not a
downstream table lookup into `action_items`/`pc_projects`, which would have
been a bigger, STOP-worthy change per the build brief. No re-architecture
needed.

## Failure discipline preserved

The status gate sits entirely inside the existing event-vs-baseline branch
of the pre-write diff (`main()`, before the dry-run gate) — it does not
touch the events-before-titles write ordering documented in
`2026-08-10-ap-end-date-detection.md` (event insert still runs before the
`ap_titles` upsert; an event-insert failure still skips the title writes
for the run). A gated-out AP simply never enters the `date_events` list, so
it can never contribute to that insert or its failure mode.

## Verified live (2026-09-11)

Real (non-dry-run) `sync_ap.py` run against the live DB, using the same
fixture-and-cleanup pattern as the original 2026-08-10 detection build:

- **Completed AP, no event:** rewound `ap_titles.end_date` for AP-0291
  (confirmed Complete, one of the 10 phantom APs) from `2025-05-01` to
  `2025-04-30`. Run logged `End-date move skipped (status gate): AP-0291
  status='Complete' 2025-04-30 -> 2025-05-01 — date baselined, no event`;
  summary line read `end-date events: 1 detected, 1 written, 1 skipped
  (inactive)`. `ap_titles.end_date` self-healed to `2025-05-01`. No new row
  in `ap_end_date_changes` for AP-0291 — the only row for that AP is the
  pre-existing 2026-09-01 phantom, untouched.
- **Active AP, event fires:** rewound `ap_titles.end_date` for AP-0621
  (`overall_status` "In Progress" in `ap_tracker`) from `2027-07-05` to
  `2027-07-01` — the same fixture value the 2026-08-10 build used. Run
  logged `End-date change event: AP-0621 2027-07-01 -> 2027-07-05`;
  `ap_end_date_changes` gained one row (`837a20c9-…`). `ap_titles.end_date`
  self-healed to `2027-07-05`.
- Both in the same run (log timestamps 09:34:33 and 09:34:36 local),
  confirming the gate discriminates correctly within one execution, not
  just across separate runs.
- **Cleanup:** the synthetic AP-0621 event (`837a20c9-…`) deleted
  afterward so no real owner sees a false flag — same discipline as the
  2026-08-10 fixture cleanup. The AP-0291 phantom event was left in place;
  its removal is Task 2 of this same bug (dismissal, not deletion — see
  orion-pll's decision doc for the mechanism chosen).

## Scope note

This gate stops NEW phantom events. It does not retroactively clear the 11
that already exist — that is Task 2 (orion-pll, dismissal mechanism) of
this same `ca9beaeb` Phase 2 brief, tracked separately since it's a data
cleanup on the consuming side, not a sync-logic change here.
