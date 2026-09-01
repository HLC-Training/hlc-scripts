# sync_ap.py SQDCGP map gains P → People, plus one-time backfill

**Decided:** 2026-09-01
**Repo:** hlc-scripts
**Action item:** 1dcbfeaa-2308-4e9a-bf4a-fbd561dfad9f (done)
**Related:** decision 2026-08-11-sync-ap-category-tier.md (established
`SQDCG_MAP`, `map_category`, the blank-cell→None ruling), decision
2026-08-12-sync-ap-status-and-category-fix.md (the category non-null
guard)

## Root cause

The Smartsheet SQDCG picklist on the OFS Training Action Plan Tracker
gained a sixth option, `P` (People), after the 2026-08-11 sync-category
build. The live sheet column (id `5718885853253508`, unchanged — only its
title and options changed) now reads `SQDCGP` with options S/Q/D/C/G/P.
`pc_projects.category`'s CHECK constraint already allowed six values
including `People`. But `sync_ap.py`'s `SQDCG_MAP` had only five keys —
no `P → People` — so `map_category('P')` returned `None`, and every
People-categorized AP row synced into ORiON with `category = NULL`.
Ankita Gupta's four active projects (AP-0365, AP-0368, AP-0370, AP-214)
were all `P` on the sheet and all landed null, which she reported as
"missing: Category." This was a live, recurring mapping hole, not a
one-time data gap.

## Fix

One key added to `SQDCG_MAP`: `"P": "People"`. The other five keys and
`map_category`'s blank-handling are unchanged. Also corrected a stale
inline comment (`COL_SQDCG` was still labeled "SQDCG" though the sheet
column is now `SQDCGP`) — the column ID itself did not change, so no
`COL_SQDCG` value update was needed.

The `if category is not None` guard from 2026-08-12 was **not touched**
— it stays exactly as-is. The asymmetry (sync sets but never clears) is
by design and is what protects manual backfills like Ankita's four rows
from being re-nulled by a future run.

## Verification (real code, not a retyped copy)

`SQDCG_MAP` and `map_category` were extracted from the actual
`sync_ap.py` via `ast` and executed directly:

- `map_category('P')` → `'People'`
- `map_category('D')` → `'Delivery'` (unchanged keys still work)
- `map_category('')` → `None`, `map_category(None)` → `None` (8/11 blank
  rule intact, no invented default)

`build_pc_diff` (the real P&C update-path diff function) was also
extracted and run against fixtures:

- Stored `category='Quality'`, blank sheet cell (`shared['category'] =
  None`) → returned `fields = {}` — guard intact, no clobber.
- Same row, sheet now maps to `People` → returned `fields =
  {'category': 'People'}` — a real category change still syncs.

## Live sheet check

Column id `5718885853253508` unchanged; `get_columns` on sheet
`1362792971980676` confirmed title `SQDCGP`, options
`["S","Q","D","C","G","P"]`. No `COL_SQDCG` id correction was needed —
only the comment was stale.

## Backfill

One-time reconciliation script:
`scripts/backfill_ap_category_people.py` (not wired into the scheduled
sync; invoked explicitly). Imports `sync_ap`'s real `map_category` and
column/sheet constants directly rather than reimplementing them, so it
can't drift from what the scheduled sync does.

Scope:
- `pc_projects`: `category IS NULL AND status NOT IN ('complete',
  'cancelled')`
- `action_items`: `source = 'ap_import' AND category IS NULL AND status
  != 'Done'`

The `action_items` filter departs from the brief's literal
`status NOT IN ('complete', 'cancelled')` — those strings don't exist in
`action_items`' status vocabulary at all (`STATUS_MAP` maps both
Smartsheet `Complete` and `Cancelled` to the single value `Done`), so a
literal copy would have excluded nothing and backfilled 3 completed
rows along with the 21 active ones. Confirmed with Jim before writing:
treat `Done` as `action_items`' one terminal status, matching the
*intent* of the pc_projects exclusion rather than its literal string
values.

Only rows where the sheet's SQDCGP cell mapped to a non-null category
were written; a genuinely blank cell was left null (no invented
default, per the 8/11 ruling). Each write was scoped `WHERE id = ...
AND category IS NULL`, so a row categorized by another process between
the read and the write could not be clobbered.

### Run (2026-09-01, attended, against production ORiON —
`czdkctjbejnwuopigxta`)

Executed by hand-driving the exact same read → map → scoped-write logic
the script encodes (live SQDCGP cells pulled via the Smartsheet MCP,
writes applied via direct scoped SQL against ORiON, since no
`ORION_SUPABASE_SERVICE_KEY` was available in the local shell this
session ran in — the script itself remains the artifact for a future
run with that env var set).

**pc_projects:** 10 candidates. 9 written (all `P → People`), 1 left
null — `AP-9902` no longer exists as a row on the live sheet at all (not
a blank-cell case; there is no source cell to read). Named example:
`AP-0340`, SQDCGP=`P` → `category='People'`.

**action_items (Delivery, ap_import):** 21 candidates, all 21 written —
17 `D → Delivery`, 4 `Q → Quality`. Zero left null (every candidate's
sheet cell was populated). Named example: `AP-0805-3`, SQDCGP=`Q` →
`category='Quality'`.

**Untouched, verified:** Ankita's four (AP-0365/0368/0370/214) all still
read `category='People'` with `updated_at = 2026-09-01 16:55:27` — the
backfill ran at `17:03:21`–`17:03:41` and none of the three write
statements' `id IN (...)` lists included them; the `IS NULL` filter
excluded them at read time regardless. Blast radius double-checked:
exactly 9 `pc_projects` rows and exactly 21 `action_items` rows carry a
timestamp `>= 17:03:00` on 2026-09-01 — matches the write counts
exactly, nothing extra touched.

**Post-run state:** `pc_projects` null-category (non-terminal) count:
1 (`AP-9902`, the sheet-deleted row above). `action_items` (ap_import,
non-Done) null-category count: 0.

All writes scoped to `czdkctjbejnwuopigxta` (ORiON) only; no other
Supabase project touched.

## Session close

Action item `1dcbfeaa` → `done`, notes record the map fix and backfill
counts. `tasks/lessons.md` gained an entry: a source picklist can grow a
value the sync map doesn't know, and the failure is silent (null, not
an error) — logged as a standing note for any future category/enum
source the sync map could drift from.
