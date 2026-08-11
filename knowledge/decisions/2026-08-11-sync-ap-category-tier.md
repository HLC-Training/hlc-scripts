# sync_ap.py category/tier — investigation closes with no code change

**Decided:** 2026-08-11
**Repo:** hlc-scripts
**Action item:** ea28fc07-06e6-443e-b1f8-9df00f347e87 (deferred, not done)
**Related bug (opened this session):** b75a59f6-ea7c-452c-bc60-1f1d1ab898f3

## What this was supposed to close

P5.4 (category integrity, action item 43cedd3b) can't take a NOT NULL
constraint on `category` while any write path still lands nulls. Verified
live 2026-08-11: `action_items` (Delivery) is fully backfilled — 0 nulls
across 365 rows. `pc_projects` native (Michele's manual import) rows are
also clean — 0 nulls across 83 rows. The one remaining gap was 35 of 41
`pc_projects` rows with `source = 'ap_synced'` (everything `sync_ap.py`
writes from Smartsheet for P&C) sitting at `category IS NULL`.

The brief assumed this was a code gap: either the Smartsheet source had no
category-equivalent field to map, or `sync_ap.py`'s P&C update path was
missing `category` from its field list (mirroring the `created_at` gap
found in an earlier session, per `tasks/lessons.md`).

## What was actually found

Neither assumption held.

**Task 1 — does the source have a category field?** Yes. `SQDCG` is a
real column on the OFS Training Action Plan Tracker sheet (column ID
`5718885853253508`, a `MULTI_PICKLIST` with options S/Q/D/C/G), and
`sync_ap.py` already reads it (`COL_SQDCG`) and maps it to ORiON's
six-value category set via `SQDCG_MAP` (S→Safety, Q→Quality, D→Delivery,
C→Cost, G→Strategy).

**Task 2 — is the mapping wired into both P&C and Delivery inserts?** Yes,
already. `map_category(task['sqdcg_raw'])` is computed once per row and
used for both the Delivery insert (`action_items`) and the P&C insert
(`pc_projects`).

**Task 3 — does the P&C update path include `category`?** Yes, already.
Unlike the `created_at` precedent this brief was modeled on, `category` has
been in the `pc_projects` update-path field diff since the P&C routing
feature was first built:

```python
if category != ex.get('category'):
    fields['category'] = category
```

All three of the above trace to commit `96ba729` ("Extend sync_ap.py to
TPMs and P&C; refactor owner lookup") — they predate action item
`ea28fc07` (filed 2026-07-28) entirely. There was nothing to add.

## Why the 35 rows are null anyway

Confirmed live against the Smartsheet source (not inferred): pulled the
`SQDCG` cell for all 41 `ap_synced` AP numbers currently in `pc_projects`.
**Zero of the 41 have `SQDCG` populated right now.** Sheet-wide, only 38 of
702 rows (5%) have any `SQDCG` value at all — it is a sparsely-used field
on this sheet, and none of the P&C-routed rows currently carry one.

The 6 exceptions that already show `category = 'Delivery'` in
`pc_projects` (AP-0329-1, AP-0329-2, AP-0521-3, AP-0662-1, AP-0662-2,
AP-0698-4) are not evidence the mapping is broken — they're evidence it
worked. All 6 are now `Overall Status = Complete`, and `sync_ap.py`'s
inactive-row path only ever revisits a Complete/Cancelled row to settle a
pending flag (see the docstring on `fetch_child_ap_tasks`). Their category
values are stale reads from whenever `SQDCG` was last `D` on those rows,
before it was cleared and before the rows went inactive — direct proof the
mapping and the update path both fire correctly when the source has data.

**Conclusion:** the 35 nulls are a Smartsheet data-entry gap, not a
`sync_ap.py` defect. No sync run — scheduled or manual — can backfill them,
because `map_category()` correctly returns `None` when the source cell is
genuinely blank, and returning a guessed default was explicitly ruled out
("if no source field exists to map from, stop and report... don't invent a
default mapping" — and here a field exists, it's just unfilled for these
41 rows). Closing this gap requires someone (Jennifer / the TPMs) to
populate `SQDCG` in Smartsheet for the AP items that route to P&C, or a
deliberate decision from Jim to apply a default at import — not a code
change.

## Before/after null counts

No code shipped this session, so before and after are identical:

| source | count | category IS NULL |
|---|---|---|
| `native` | 83 | 0 |
| `ap_synced` | 41 | 35 |

## Bug opened, not fixed, this session

While confirming the update path already included `category`, found it
does so unconditionally — it overwrites `pc_projects.category` with
whatever the current sync computes, including `None`, any time that
differs from the stored value. That's the same clobber pattern action item
`ea28fc07`'s own notes warned about for the Delivery side ("do not extend
the UPDATE path to write category or priority... would be overwritten with
NULL, silently") — except the P&C side already has it live, unguarded. Not
observed happening to a real hand-categorized active row yet (nothing in
the current 41-row set is both active and non-null), but the 6 stale
`Delivery` rows above prove the overwrite-to-NULL branch is reachable, not
hypothetical. Logged as bug `b75a59f6-ea7c-452c-bc60-1f1d1ab898f3` — open,
not fixed. A fix needs a design call (e.g. never write a null over an
existing non-null category; or extend the `ap_pending_update` protection
pattern to cover `category` the way it already covers status/dates).

## Action item disposition

`ea28fc07` → `deferred`, not `done`. The code-side ask is already
satisfied and needs no further work; what remains is upstream Smartsheet
data population, which this repo doesn't control.
