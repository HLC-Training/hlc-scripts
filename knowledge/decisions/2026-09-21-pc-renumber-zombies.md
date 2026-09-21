# Module-row orphan detection keys on liveness, not AP-number existence; an orphaned row never raises a pending ask

**Date:** 2026-09-21
**Repos:** `hlc-scripts` (`sync_ap.py`, `send_ap_pending_digest.py`, two new
tests); `orion-pll` (migration record only — `docs/migrations/2026-09-21_module_rows_smartsheet_row_id.sql`).
DB: ORiON `czdkctjbejnwuopigxta` (`pc_projects`, `action_items`, `ap_change_log`).
**Origin:** Jen Wright's 2026-09-21 AP-pending notice labeled her edit to
"Import ICS Level 2 Competency Map Structure in Kahuna" as AP-0785-3-3 — a
number that now belongs to "ICS Level 2 Storyboard & Content Gathering".
SAM COS action item `35988c9e`, bug `4dfe07fd`.
**Governing, not relitigated:** `9ef26816` (per-field settle, tracker wins
when newer), `88125a27` (ap_tracker prune + `(ap_number, is_parent)` guard;
no bare `unique(ap_number)`), `86fd0c07` (orphans are flagged, never
deleted), the 2026-08-26 mirror architecture.

---

## What actually happened (Task 1 discovery, from source)

1. **The write-path that flagged the zombie** was an ordinary P&C UI save:
   `updateProject` in orion-pll
   `app/(protected)/pc/projects/project-edit-actions.ts` (flag write at the
   `ap_pending_update: true` block, log-first/flag-after). `ap_change_log`
   row `35f690ca`: module `pc`, `item_id 8e5e4fd1` (the pc_projects zombie),
   field `description`, reason `SYSTEM_EDIT_REASON`, `changed_by` = Gloria
   Norris (the row's owner), 2026-09-21 11:18:10. The orion-pll app has no
   notion of `ap_orphaned` anywhere, so a zombie stays fully editable in the
   P&C UI and can be flagged again at any time. Not a sync write-back.
2. **The orphan detector** was `sync_ap.py` main(), the "Orphan detection
   (bug c4494694)" block (pre-fix lines 2005–2027):
   `sheet_ap_numbers = {t['ap_number'] for t in tasks ...}` then per module
   row `if ap not in sheet_ap_numbers and not r.get('ap_orphaned'): flag`.
   Confirmed: keyed on **ap_number existence** in the fetch — exactly the
   check a reassigned number defeats.
3. **`pc_projects` had no prune step at all** — `prune_stale_mirror_rows()`
   is ap_tracker-only (by design, 88125a27). And a `pc_projects` row carries
   **no Smartsheet identity**: no `smartsheet_row_id`, no autonum, no
   parent-row id (`information_schema` verified). It carries `ap_number`,
   `ap_is_parent`/`ap_is_child`, `title`, `ap_lead_display`. The sync
   matches module rows by `module_row_key(ap_number, pure_parent)` only.
   Why the zombies survived untouched: the new actions issued the old
   numbers (Mohammed Nizami's Level-2 design rows) route to **Delivery**,
   so no P&C projection ever visited the old P&C rows at those keys — they
   were neither updated (which would have morphed them) nor flagged.
4. **True zombie population** (reassigned-number class, both modules):
   - P&C: `AP-0785-3` (8ca67d0e), `-3-1` (6099013b), `-3-2` (db6a6218),
     `-3-3` (8e5e4fd1, the pending one).
   - Delivery: `AP-0785-1` (afb0bfb4), `AP-0785-2` (6cfb0e56) — same
     renumber, old Delivery rows whose numbers now carry Benali's P&C
     Level-1/Level-2 headers; `AP-189-1` (a5ae0303, "Develop List of
     Networks Competencies…", Deferred) — its number now carries Michele's
     P&C "Review/Create ALT Networks Competencies".
   - `AP-0785-3-4` is **not** a zombie (the brief listed it): the tracker's
     `-3-4` is the same "Build ICS Level 2 Media" action, so the row is a
     live projection.
   - Two rows of a *different* defect, left alone: `AP-0732` (action_items
     edc7d1b3, leaf-shaped) and `AP-0845` (pc_projects 8cf0bcd4, leaf-
     shaped) are same-title duplicates of the live pure-parent projection of
     the same sheet row — a widening-era key-shape duplicate, not a
     renumber. The new detector deliberately reads them as live (same
     action under the same number) and backfills their stamp. Filed for
     Jim, not fixed here.
   - `not_flagged_but_number_gone = 0` in both tables, as the brief said.

The brief's mechanism held on every point; the one correction is the
population (7 zombies across both modules, not 4–5 in P&C only).

## The decision

**Liveness, judged per module row in this order** (`plan_orphan_flags()`,
pure, called once per module table):

1. **visited** — this run's projection matched the row by `module_row_key`
   (update / unchanged / pending-skip / clear / close all count). Live.
2. **stamped** — the row's `smartsheet_row_id` (new column) is still in the
   fetch's `all_sheet_row_ids`. Live iff present; absent → orphan,
   reason `deleted`. This is the same permanent identity `ap_tracker` is
   keyed and pruned on, and the precise rule going forward: a reassigned
   number cannot fool it because the *row* is gone.
3. **legacy** (never stamped, not visited): no sheet row carries the number
   → orphan `deleted` (the old rule, preserved — the -3-5/-3-6/-4-4 class
   stays flagged). The number is present → live only if one sheet row
   under it is the **same action** (normalised title equality; a live row
   would otherwise have been visited) — then it is stamped; otherwise
   orphan, reason **`reassigned`**. This is the false negative being fixed.

**Guards.** New flags require `fetch_complete` (the prune's 2026-09-08
lesson — an incomplete fetch reads un-fetched rows as vanished); unflag and
stamp verdicts never need the gate. The zero-AP-number sheet-damage guard
stays. Still scope-blind: a family dropping out of scope does not orphan
its rows (stamp or same-title match keeps them live; zero such rows exist
today, verified).

**Flags only, never deletes** (86fd0c07, unchanged). Nothing in this build
deletes or closes a module row; the seven zombies stay in ORiON with
`ap_orphaned=true` for Jim and Jen to disposition.

**Orphaned state wins over pending.** Two places, both needed:
- *Sync side* (`sync_ap.py`): a row that is orphaned after this run and
  still carries `ap_pending_update` gets an `ap_change_log` audit row
  (module = its module, field `ap_pending_update`, `true → false`,
  `ORPHAN_PENDING_CLEAR_REASON`, `changed_by` NULL) and then the flag
  cleared — log-first/clear-after through the existing `_log_supersede`
  discipline; a failed audit insert withholds the clear. Rationale: under
  9ef26816 an orphan can never settle (no tracker row to judge against),
  so without this the flag lives forever and the digest asks Jen to apply
  an edit to a sheet row that does not exist.
- *Digest side* (`send_ap_pending_digest.py`): `suppress_orphaned_pending()`
  partitions flagged rows; an `ap_orphaned` row is never rendered in the
  pending "apply in Smartsheet" table — it appears only in the
  removed-from-tracker section (if still open). This covers the window
  between a UI re-flag and the next sync run. The 9ef26816 reconciliation
  path is untouched — suppression happens before it, on a different
  column.

**Raise side left alone, deliberately.** orion-pll's `updateProject` still
raises the flag on an orphaned row (it cannot see `ap_orphaned`). With
both gates above, that re-flag is harmless: never rendered, cleared on the
next sync. Teaching the P&C UI about orphans (a badge, a read-only state)
is a portal-visible change that needs its own help note and Jim's call on
what TPMs should see — filed as a follow-up, not shipped here.

**Identity stamp.** `pc_projects.smartsheet_row_id` and
`action_items.smartsheet_row_id` (bigint, nullable, no index, never
unique — pure parents project into both modules). Written on insert and,
for every visited/backfilled row, in a dedicated stamp write after the
orphan writes. **Not yet applied on 2026-09-21** — the session's Supabase
DDL was blocked by tool permission policy (shared-resource modification);
the SQL is recorded in orion-pll `docs/migrations/2026-09-21_module_rows_smartsheet_row_id.sql`
for Jim. `load_module_rows()` degrades: on a `smartsheet_row_id` select
error it reloads with the pre-migration column list, logs a warning every
run, and the detector runs on rules 1 and 3 only (which is what caught all
seven zombies). The moment the column exists, the next run stamps every
matched row and rule 2 takes over.

## Rejected / out of scope

- `unique(ap_number)` on either table — rejected 88125a27, twins are real.
- Pruning (deleting) `pc_projects` rows — ruled out 86fd0c07.
- Rewriting how the digest prints AP numbers — the number was correct for
  the row it described; the row was the defect.
- Title-only detection without a stamp — Delivery never syncs
  `action_text` on update (21 live rows differ from the sheet by title
  today), so titles alone would false-positive on renamed rows; hence the
  stamp is the durable key and titles are only the legacy/backfill rule.

## Evidence (verification gate — full output in the build report)

- Unit: `tests/test_module_orphan_detection.py` — 15 tests, one fixture per
  shape ((a) gone, (b) reassigned legacy + reassigned stamped, (c) visited /
  stamped-present / legacy same-title), plus true-positives-stay, unflag,
  incomplete-fetch, column-missing fallback, orphan-pending clear scoped.
  `tests/test_ap_pending_digest_orphan_suppression.py` — 7 tests: zombie
  suppressed from pending and listed as removed; live twin still renders;
  matched and tracker_newer fixtures still drop per 9ef26816; closed
  orphan not listed. All pre-existing suites still pass.
- Live dry-run on the branch (GitHub run 35642383200, fetch 876/876 =
  complete): `Orphans — would flag: 3 delivery + 4 P&C (7 reassigned-number,
  0 deleted), would clear: 0 + 0, orphaned-pending flags to clear: 1,
  identity stamps: 0 + 0 [smartsheet_row_id column missing — stamps off]`.
  The seven named rows above, `CLEAR AP-0785-3-3 P&C`, and nothing else —
  no live row flagged; delivery/P&C/mirror plans all zero, as on any quiet
  run.
- Live run after merge and the post-run row states: see the build report
  and action item 35988c9e.

## Cross-links

Orphan flagging origin: `2026-08-20-sync-integrity-trio.md`. Reconciliation
rule: `2026-09-08-ap-pending-clear-and-direction.md`. Prune + guard:
`2026-09-08-ap-tracker-prune-and-composite-guard.md`. Master table:
orion-pll `2026-09-14-master-ap-table-phase4.md`. Lessons:
`tasks/lessons.md` 2026-09-21 (wrong-table join; identity vs. number).
