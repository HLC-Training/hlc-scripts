# AP-pending reconciliation: settle per field against the live tracker, tracker wins when newer

**Date:** 2026-09-08
**Repos:** `hlc-scripts` (sync_ap.py, send_ap_pending_digest.py, new ap_pending.py, tests/test_ap_pending.py). orion-pll read only.
**Origin:** Jen Wright's 2026-09-04 reply to the AP-pending digest; bug `424c1637`
**Action item:** SAM COS `f86bd75e`
**Commits:** `hlc-scripts` fb9bbb7 (code) · docs commit follows
**Related rulings preserved:** `ac29261e` (wont_fix, blanked date clears as caught up), `86fd0c07` (orphans are flags-only), `372d294d` (closing-note reason bridge, untouched)

---

## The defect

`sync_ap.py` is the only writer that clears `ap_pending_update`, and it did so
with a whole-row rule: the flag cleared iff the sheet's projection matched the
ORiON row on **every** content field. That rule is wrong in exactly the
situation the flag exists for. A TPM edits one field in ORiON (the description),
Jen applies it in Smartsheet, and then rearranges the family — new start/finish
dates on the same row. The description now matches, but the dates differ, so the
whole-row diff is non-empty, the row stays "diverging", the flag never clears,
and Jen's digest re-lists the item every morning with a reason line telling her
to apply a change she already applied. Confirmed live on AP-0293-2/-3/-4 (single
tracker rows — not the duplicate-row issue).

The second half of the same defect: when Jen's later edit changed the very field
the TPM had edited (AP-0547-1-2 dates), the digest told her to hand-apply the
older ORiON value over her own newer one. No path existed for a tracker edit
that post-dates the ORiON edit to supersede it.

Two things reached her inbox that never should have: a seeded fixture
(AP-9902, still alive in `pc_projects` after its `ap_tracker` row was deleted)
and two Delivery rows that had been closed to Done in chat but were re-flagged
orphaned by the next sync — the removed-items section said "still open in ORiON"
about rows that were not.

## The rule (ap_pending.py — shared, one implementation)

Per flagged row, the **episode** is its `ap_change_log` entries since
`ap_pending_since`, latest per field, restricted to the sync-mirrored fields
(`SYNC_MIRRORED_FIELDS`, mirrors orion-pll `lib/ap-pending.ts`). Each episode
field is judged against the live sheet row's projection — `sync_ap.py` uses the
row it just fetched (`task['mirror']`), the digest uses the stored `ap_tracker`
row; both carry the same column shape, so the projection is byte-identical:

| state | condition | consequence |
|---|---|---|
| `matched` | tracker projection == the value (ORiON row value in the sync, `new_value` in the digest), whitespace-normalized | reconciled |
| `tracker_newer` | values differ **and** `smartsheet_modified_at > changed_at` | Jen's later edit wins: flag clears, sheet value lands on the next run, digest says nothing |
| `pending` | values differ, tracker not newer | still open: digest lists it, sync protects the row |
| `not_comparable` | no tracker-side projector (`owner_id`) | sync falls back to its own content diff for that field; digest keeps it listed |

A row's flag clears iff every episode field is `matched` or `tracker_newer`.
Fields the sheet changed that were **never** part of the ORiON edit no longer hold
the flag open — they were never a PLL/TPM edit to protect, and they flow to ORiON
on the next normal update once the flag drops. A row with an empty episode
(legacy flag, unlogged writer such as bug `6cc5e30c`) keeps the old whole-row rule.

### Superseded edits are recorded before they are overwritten

When `tracker_newer` clears a flag, the next run's normal update overwrites the
losing ORiON value with the sheet's. That overwrite is silent to the TPM. So the
sync writes one `ap_change_log` row per superseded field **before** the clear
(old = the losing ORiON value, new = the tracker value, reason
`superseded: ...`), and a failed insert withholds the clear and exits non-zero —
the 2026-08-26 "evidence row before the write that destroys the evidence" rule.

### Blank tracker cells

`None` on the tracker side counts as `matched` for every field except
`pc.description`. This is the ac29261e wont_fix behavior (a blanked Smartsheet
date releases the hold as "caught up") and the 8/12 per-field rulings (dates and
category guarded; a cleared P&C description is intent and mirrors). The rule was
carried over deliberately, not rediscovered — do not widen it.

### The episode window has a 5-second skew tolerance

The orion-pll writers stamp `ap_pending_since` from the `changed_at` of the
**last** insert of a save, but a save touching a reason-gated field and a
system-edit field performs two inserts ~80 ms apart. Observed live:
AP-0547-1-2's `target_end_date` entry at 14:37:28.430 vs since 14:37:28.513. A
strict `>=` dropped the first insert's fields from the episode — they were never
judged and never rendered. `in_episode()` allows `EPISODE_SKEW_SECONDS = 5`; a
re-flag after a clear is separated from the old episode by at least one 30-minute
sync cycle, so nothing older can leak in. App-side fix (stamp since from the
earliest insert) filed as a separate bug.

## What this means on live data (first run after deploy)

All 18 flagged P&C rows settle. Ten of them clear on `matched` alone. Eight
clear on `tracker_newer` and will have their ORiON value **replaced by the sheet
value on the run after** — the sync dry run names each:

- AP-0365-4-2, AP-0368-4-2, AP-214-7-1, AP-214-10-3, AP-214-11-1 — description (Jen's wording vs the TPM's; hers stands)
- AP-0365-5, AP-0547-1-1, AP-0547-1-2, AP-214-8-1 — dates (Jen's rearranged dates stand)
- **AP-0547-1 — status `complete` → `on_hold` and `target_end_date` 2026-01-30 → 2027-03-31.** Gloria marked it complete in ORiON on 09-01; the sheet still says On Hold and Jen edited that row on 09-03. Under this ruling the sheet stands and Gloria's completion is reverted, with her values in the supersede audit row. This is the one row where "tracker wins" reverts a human status decision rather than a date; Jim should know before the next sync runs.

### Known hazard: row-grained timestamps

`GET /sheets` exposes only a row-level `modifiedAt`. Many tracker rows share the
exact timestamp `2026-09-03 15:56:56` — a sheet-wide save. Any such bulk touch
marks every older pending edit on those rows as `tracker_newer`. That is the
accepted cost of the ruling; the refinement, if it is ever needed, is cell-level
history (`GET /sheets/{id}/rows/{rowId}/columns/{colId}/history`), not a change
to this rule.

## Fixtures

`is_fixture(ap_number, title, smartsheet_row_id)` — AP-99xx range (ops-closeout
seeds AP-9901, the 09-04 digest's AP-9902, smartsheet.test's AP-9999), any
title containing "TEST FIXTURE" (both the `TEST FIXTURE — ...` prefix and the
`[TEST FIXTURE — safe to delete, ...]` tag), and synthetic row ids in the
12-digit 9999999xxxxx band. Real Smartsheet ids are 16 digits — a `>=` check
matched every real row on the first test run, which is why the band is bounded.
Both digest sections filter on it. The leftover AP-9902 `pc_projects` row was
deleted this session (its owner probe `portal_users` row "AP-9902 Owner Probe",
role viewer, is still there — Jim's call).

## Removed-from-tracker section

Source unchanged (`action_items.ap_orphaned` ∪ `pc_projects.ap_orphaned`, both
`source = ap_*`), now filtered to rows whose status is not terminal
(`Done` / `complete` / `cancelled`) and not fixtures. The brief expected the
live orphan set to be empty; it was not — the bug's diagnosis queried only
`action_items`, and `pc_projects` holds three genuine open orphans
(AP-0785-3-5, AP-0785-3-6 — Gloria's, absent from the sheet and from
`ap_tracker` — and the P&C twin of AP-0785-4-4, whose Delivery twin was closed
in chat). Dropping the P&C source would have hidden real orphans, so the source
stays and the deviation is reported. Orphan detection in `sync_ap.py` is
untouched: flags-only, no auto-close, no delete (Jim's ruling `86fd0c07`).

## Dedupe — and why the uniqueness guard did NOT ship

The eight "duplicate" AP numbers were two different things:

1. **Six stale siblings** (AP-0293-3-2, AP-0785-1, AP-214-8-1, AP-214-8-2,
   AP-214-9-1, AP-214-9-2): each pair was two *different* Smartsheet rows; the
   older row was deleted or renumbered when Jen rearranged on 09-01 and the sync
   never prunes the mirror (it upserts on `smartsheet_row_id`, never deletes).
   Each stale row's `smartsheet_row_id` returned HTTP 404 from the Smartsheet
   API; each keep-row returned 200. Deleted the six, guarded by id, row id,
   `last_synced_at < 2026-09-02` and `orion_dirty = false`.
2. **Two live twins** (AP-036, AP-174): a parent row and a child row that share
   one flat AP number in the sheet, both live as of today's sync. The sync keys
   module rows on `(ap_number, pure_parent)` precisely because of these (see
   `module_row_key`). They are not duplicates and were not touched.

`create unique index ap_tracker_ap_number_key on ap_tracker (ap_number)` cannot
be created while the twins exist and would break the next sync if it could
(the sync's write key is `smartsheet_row_id`, and a renumber would then throw on
the new row instead of landing it). Handed to Jim as a design item: the durable
shape is (a) the sync pruning mirror rows whose `smartsheet_row_id` is absent
from a *complete* fetch, and (b) a composite unique on `(ap_number, is_parent)`.
Until then the digest is made deterministic in code: `pick_tracker_row()`
matches shape first, then the freshest `last_synced_at`, so a stale sibling or a
twin can never be the comparison anchor (AP-214-8-1 compared against the live
row in every render).

## What did not change

- `ac29261e` blank-date behavior: preserved by the blank rule above.
- Orphan logic: flags-only, unchanged in `sync_ap.py`.
- Closing-note reason bridge (`372d294d`): untouched.
- The digest still never writes the flag — the sync clears it with the same rule on its next run; the digest's filter only closes the window between.
