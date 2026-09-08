# ap_tracker uniqueness: prune vanished mirror rows, then guard on (ap_number, is_parent)

**Date:** 2026-09-08
**Repo:** `hlc-scripts` (`sync_ap.py`, `tests/test_ap_tracker_prune.py`). ORiON `czdkctjbejnwuopigxta` (`ap_tracker`) only.
**Origin:** action item `3ca6c010`, handed forward from the AP-pending fix (decision `2026-09-08-ap-pending-clear-and-direction.md`, bug `424c1637`).
**Related:** `2026-08-26-ap-tracker-mirror-architecture` (the mirror is the destination architecture), `2026-08-26-ap-writeback-phasing` (untouched by this build).

---

## The two defects

`sync_ap.py` upserts `ap_tracker` on `smartsheet_row_id` but never prunes. When
Jen deletes a sheet row — including the common case of a renumber done by
deleting the old row and creating a new one under a new number — the old
`ap_tracker` row is orphaned forever; nothing ever removes a mirror row whose
`smartsheet_row_id` 404s on the sheet. Six such stale siblings (AP-0293-3-2,
AP-0785-1, AP-214-8-1, AP-214-8-2, AP-214-9-1, AP-214-9-2) were deleted by hand
earlier today.

Separately, there was no uniqueness guard on `ap_number`, so stale mirrors
accumulate as duplicate AP numbers with no constraint to catch it. The
verbatim `unique (ap_number)` from the original mirror brief was correctly
never applied: `AP-036` and `AP-174` are legitimate live parent+child twins —
one `is_parent=true` row and one `is_child=true` row sharing one flat AP
number, both real sheet rows with distinct `smartsheet_row_id`s. A bare
`unique(ap_number)` cannot be created against live data and would throw on
every future renumber.

## The fix

**Prune** (`prune_stale_mirror_rows()` in `sync_ap.py`, called from `main()`
right after the mirror upsert phase): deletes `ap_tracker` rows whose
`smartsheet_row_id` is absent from the current Smartsheet fetch. Gated on
BOTH conditions holding:

- `fetch_complete` — `fetch_ap_rows()` now returns this alongside
  `all_row_ids` (every row id in the fetch, every shape). It is True only
  when Smartsheet's `totalRowCount` cross-check (the sync's existing
  pagination-completeness check, widened from a log-only warning into a
  returned flag) matches the fetched row count exactly. No `totalRowCount`
  in the response = not confirmed complete = False, never assumed True.
- `mirror_load_failed` is False — the pre-upsert `ap_tracker` read that
  seeds `existing_mirror` must itself have succeeded, or the "what's
  currently in the mirror" side of the diff is untrustworthy.

Either condition failing skips the prune and logs why, every run — never
silent. The prune diffs `smartsheet_row_id` sets only, never `ap_number`, so
the AP-036/AP-174 twins (two distinct row ids under one ap_number) are never
prune candidates regardless of what the rest of the table looks like.

**Composite guard**: `create unique index ap_tracker_ap_number_is_parent_key
on ap_tracker (ap_number, is_parent)`, applied only after confirming the live
table held zero `(ap_number, is_parent)` collisions and zero NULL `is_parent`
(the column is `NOT NULL`, so the NULL case is structurally impossible here).
Permits the twins (opposite `is_parent`, same `ap_number`); blocks a stale
mirror duplicate (same `is_parent`, same `ap_number` — the shape every stale
sibling actually took).

The sync's `ap_tracker` upsert keys on `smartsheet_row_id`
(`on_conflict='smartsheet_row_id'`) — its only conflict target — so the new
composite index changes nothing about how the upsert resolves conflicts;
confirmed live by re-upserting one real row (AP-036's child,
`smartsheet_row_id` 1796179528224644) through the new index with no
violation.

## Verification

Table was already clean at build time (the six stale siblings were removed
by hand before this build started) — zero `(ap_number, is_parent)`
collisions, zero NULL `is_parent`, twins confirmed 1 parent + 1 child each.
The prune itself is proven behaviorally: `tests/test_ap_tracker_prune.py`
runs the real `prune_stale_mirror_rows()` against a stub db and asserts (a)
a row absent from `all_sheet_row_ids` is deleted when `fetch_complete` is
True, (b) nothing is deleted when `fetch_complete` is False even though the
same row looks vanished, (c) nothing is deleted when `mirror_load_failed` is
True, (d) a live twin pair (both row ids present) is never touched. The
composite index's reject/permit behavior was proven directly against
production in rolled-back transactions: a duplicate `(AP-036, false)` insert
raised `23505`; a twin-shaped insert (`AP-0234-1`, opposite `is_parent`)
succeeded and rolled back clean.

## What this does not do

Does not touch `ap_pending.py`'s settle logic, module-table (`action_items`/
`pc_projects`) orphan handling, or the write-back/mirror-migration phases —
all out of scope per the action item and untouched by this commit.
