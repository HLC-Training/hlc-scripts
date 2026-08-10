# Parent-AP end-date capture + change events in sync_ap.py

**Date:** 2026-08-10
**Action item:** 1d039530-1f7d-4210-b289-dfbcff49781b
**Status:** Decided & implemented
**Related:** `2026-08-10-ap-title-capture.md` (the capture pass this
extends), orion-pll's `knowledge/decisions/2026-08-10-ap-end-date-gate-and-ack.md`
(the consuming gate/ack/lead-visibility build), orion-pll's
`knowledge/proposals/2026-08-10-parent-ap-end-date-ack.md` (approved
investigation).

## What ships

The existing top-level parent-row capture (`ap_titles`) now also reads
"Current Finish" (`parse_due_date`, same as child rows) and diffs it
against `ap_titles.end_date` each run:

- stored **NULL** → silent baseline write, no event. The first run after
  ship populated all 71 top-level APs without flagging anyone — this is
  the entire "going-forward scope" mechanism; no separate backfill exists.
- stored **differs** (either direction) → update `ap_titles`, insert one
  `ap_end_date_changes` row (AP number, old, new). orion-pll turns that
  into an owner-ack flow.
- blank "Current Finish" on a top-level parent (none exist today) →
  log-and-keep-stored; never null out, never fire an event.

## Failure discipline — the one new ordering decision

Same three-way shape as the title capture (2026-08-10 lesson), plus a
write-order rule the title build didn't need: **events insert BEFORE the
ap_titles upsert, and an event-insert failure skips the title writes for
the run.** If the stored end_date advanced past an unrecorded event, the
event is lost forever — the next run's diff sees no change. The reverse
failure only produces a duplicate event on retry, which the app's
ack-all-outstanding-per-AP design absorbs. So the recoverable failure mode
is the one left reachable. Either failure still exits non-zero at the very
end, after the child sync completed; the completion heartbeat carries
`date_events_detected/written` and `date_baselines` alongside the title
fields.

Proven with a stubbed-DB run of the real `main()` (per the title build's
lesson: prints, not vibes): child writes land first, event insert raises,
titles skipped, exit 1; happy path orders child → events → titles, exit 0.

## Verified live (2026-08-10)

- Baseline run: "AP titles: captured 71, written 71, end-date events: 0
  detected, 0 written."
- Detection run after rewinding AP-0621's stored date to 2027-07-01:
  exactly one event row, old 2027-07-01 → new 2027-07-05, stored value
  self-healed to the sheet's date. Fixture event deleted afterward so no
  real owner sees a synthetic flag.
