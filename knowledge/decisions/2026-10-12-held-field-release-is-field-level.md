# 2026-10-12 — A held field is released on its own sheet value, never on the row's modified time

**Status:** active. Refines hlc-scripts decision `2026-09-08-ap-pending-clear-and-direction.md`
(per-field settle, "tracker wins when newer"), orion-pll decision `93113ee9` (mirror
conflict: last-write-wins, never silent) and `e217604f` (loop prevention: per-row sync
state + timestamp cursor). None of the three is superseded: the conflict is still logged,
the tracker still wins a genuine two-sided change, and the sync is still strictly
Smartsheet→ORiON. What changes is the **evidence** each rule accepts for "the sheet changed
this field".

Action items: SAM COS `7bf22072` (mmr-date-revert), `dc6f52d6` (mmr-parent-date-lock).
Origin: Jen Wright feedback `da8a19f1`, 2026-10-12 10:56 CT — "What happens when I update the
target date from within the mid-month review pop-up screen. It doesn't seem to be saving
that value anywhere."

## The rule

> For a field in a flagged row's episode, `tracker_newer` requires that the sheet's
> projection of **that field** differs from the episode's `old_value` for that field. If the
> sheet still holds the pre-edit value, the sheet never touched the field, and the state is
> `pending` regardless of the row's modified time. Row modified time remains the tiebreaker
> only when the field itself changed on both sides. The same test applies to the mirror
> conflict branch: a two-sided conflict on a field exists only if the sheet's value for that
> field moved away from the value ORiON last saw.

"The episode's old_value" is the `old_value` of the field's **first** `ap_change_log` entry
in the current flag episode — the value the sheet held when the ORiON edit began. Two ORiON
edits in one episode (10-30 → 11-30 → 12-15) still start from 10-30. For the mirror the
same origin is derived from the person-authored entries since the sync last wrote the row
(`last_synced_at`): the `ops` rail against the mirror id for any key, the `delivery`/`pc`
rail against the projection for the date keys only (status is stored in a mapped vocabulary
in the module tables and is not shape-comparable). A field with no origin on record keeps the
pre-existing row-time rule; a run that cannot read the change log protects every differing
dirty row rather than judging a conflict on unknown evidence.

## Why — the evidence (ORiON `ap_change_log`, 2026-10-12, times CT)

| AP | Jen held | Held at | Discarded at | Discard reason | `ap_tracker.current_finish` at 14:45 |
|---|---|---|---|---|---|
| AP-0619-5 | 2026-11-30 | 09:08 | 09:15 | conflict (93113ee9) + superseded (2026-09-08) | 2026-11-30 (re-entered 10:46) |
| AP-0769 | 2026-11-30 | 09:12 | 09:15 | conflict | 2026-11-30 |
| AP-0293-1-2-2 | 2026-11-30 | 09:15 | 09:30 | conflict + superseded | 2026-11-30 |
| AP-0293-1-3-2 | 2026-11-30 | 09:16 | 09:30 | conflict + superseded | 2026-11-30 |
| AP-0785-3-1 | 2026-12-31 | 09:23 | 09:30 | conflict + superseded | 2026-12-31 |
| AP-0761 | 2026-12-31 | 09:27 | 09:30 | conflict | 2026-12-31 |
| AP-0952-2 | 2026-11-30 | 09:32 | 09:45 | conflict + superseded | **2026-10-30 (lost)** |
| AP-0370-5 | 2026-11-30 | 09:41 | 09:45 | conflict + superseded | 2026-11-30 |
| AP-0521-5 | 2026-11-30 | 09:43 | 09:45 | conflict + superseded | 2026-11-30 (re-entered 10:41) |

Every discard row recorded the sheet value as the **pre-edit** value (2026-10-30). The sheet's
Current Finish had not moved when ORiON decided "Smartsheet changed after ORiON write."

Smartsheet cell history (read-only, sheet 1362792971980676, columns Current Finish /
Overall Status / Comments) confirmed it for all nine: the only cell edits on each row before
its discard were the Comments (and on five rows the Overall Status) cells the **same
Mid-Month save had pushed**, timestamped within a second of Jen's ORiON edit. Those pushes
bumped the row's `modifiedAt` past `orion_written_at` / `changed_at`. Every Current Finish
cell still carried its pre-edit value at discard time; where it later changed, it was Jen
applying the date by hand between 10:26 and 11:21 CT. AP-0952-2's Current Finish cell has a
single history entry (2026-08-19) — never applied, still lost.

Mechanism: `smartsheet_modified_at` is a **row** timestamp (GET /sheets exposes no cell-level
modified time). Both `ap_pending.classify_field` and `sync_ap.plan_mirror_writes` compared
that row stamp to the ORiON write time and read "newer row" as "newer field". Any sheet touch
to the row — the save's own push, a later note, a dependency recalculation — released a held
field within one sync cycle. The pilot's premise (a held date survives until Jen applies it
from the digest or `/operations/reconcile`) could not hold.

Under the new rule all nine stay `pending` and appear on the worklist, which is the pilot's
intended behavior. A genuine Jen-in-the-sheet change to a different date still wins
(`tracker_newer`, superseded row logged). Jen applying the same date in the sheet still
reconciles as `matched` / an echo.

## What changed

hlc-scripts (this repo):
- `ap_pending.py` — `classify_field` takes `origin_value`; `sheet_untouched()` is the one
  test; `latest_episode_entries` stamps each returned entry with `_episode_origin` (first
  entry's `old_value`); `classify_pending` passes it. Both the sync's clear and the digest's
  render go through this, so neither re-derives the rule.
- `sync_ap.py` — `mirror_origin_values()` derives per-key origins for dirty mirror rows from
  the change log (loaded in `main()` right after the mirror load); `plan_mirror_writes()`
  judges each differing key as untouched / moved. Only moved keys conflict; untouched keys
  on a partially conflicting row keep ORiON's written value and the row stays dirty for
  exactly those keys. A load failure passes `None` and protects every differing dirty row.
  The `owner_id` branch (no tracker projector) applies the same origin test to the resolved
  lead.
- Tests: `tests/test_ap_pending.py` (cases a–d from the evidence table, plus the two-edit
  origin case), `tests/test_mirror_conflict_field_level.py` (planner cases a–d, legacy,
  load-failure, origin derivation). Case (a) failed against pre-fix code with
  `AssertionError: {'due_date': 'tracker_newer'}` — that run was the repro.

orion-pll (same day):
- No release rule exists on the app side — `lib/ap-reconcile.ts` renders the raw flag state,
  which is why the worklist never disagreed with the sheet; only the sync and the digest
  judged. Nothing to mirror. The duplicated **episode** rule (`latestEpisodeEntries` /
  `inEpisode` / `EPISODE_SKEW_MS`) is unchanged on both sides.
- Parent-row date lock (`dc6f52d6`): `parentDateLockViolations` in `lib/ap-reconcile-rules.ts`,
  enforced in `saveMidMonthEdit` and reflected in the Mid-Month card (both date fields
  disabled with "Set by its child rows. Change the child dates instead.").
- `knowledge/help/ops-mid-month-review.md` rewritten: Current Finish is held, survives other
  row edits, parent dates are locked.

## Known limits (accepted)

- A row with an untouched held key and NO moved key is PROTECTED whole — other sheet facts
  on that row (Lead, Bucket…) are not ingested into the mirror until the hold settles. This
  is the pre-existing PROTECT behavior for an un-echoed write, now simply lasting as long as
  the hold does; the module projections are protected the same way by the pending flag.
- Origin derivation for the mirror needs a person-authored change-log entry. A dirty row
  with none (a push that failed before anything was logged) keeps the row-time rule.
- Today's nine reversal rows in `ap_change_log` stay as written — true history, append-only.
  AP-0952-2 is restored by Jen re-entering one date, not by a script.
