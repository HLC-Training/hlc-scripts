# sync_xyleme.py — owner_id change-detection fix

**Decided:** 2026-08-11
**Repo:** hlc-scripts
**Bug:** adee6b56-9023-4890-8e33-a9b3ce4b6891 (resolved)
**Action item:** cd3c8ad2-b20b-4930-aace-9718ae5e5ef7 (done)

## Root cause

In the write loop (`sync_xyleme.py`, ~line 468 pre-fix), `fields["owner_id"]`
was set unconditionally whenever a task had an owner ("always keep owner
current in case PLL mapping changes"). Since every xyleme course has a
resolvable owner, `fields` was never empty and `if fields:` was always
truthy — every owned `xyleme_import` row got a full `UPDATE` on every
scheduled run, whether anything actually changed or not.

`action_items.last_updated` carries a `BEFORE UPDATE` trigger (per the
2026-08-05 lesson) that bumps to `now()` on any `UPDATE`, regardless of
what the caller sends for that column. So the unconditional `owner_id`
write didn't just set a stale timestamp — it forced the trigger to fire on
every run, making `last_updated` track sync cadence instead of real edit
activity. This directly undermined the Approaching-Stale feature shipped
the same day (`staleness_state()` and the exec panel read `last_updated` as
a proxy for real activity): a xyleme item could never show as approaching
or stale, no matter how genuinely untouched it was.

Confirmed live before the fix: all 6 `xyleme_import` rows carried
`last_updated` timestamps within ~1.5 seconds of each other, matching the
last scheduled run exactly.

## Fix

Two changes to the write loop:

1. Added `owner_id` to the `existing_resp` select — it wasn't being loaded,
   so there was nothing to diff against.
2. Changed `if task["owner_id"]:` to
   `if task["owner_id"] and task["owner_id"] != ex.get("owner_id"):` —
   `owner_id` now only enters `fields` when it actually differs from the
   stored value, matching the diff-before-write pattern already used for
   `status`, `due_date`, and `notes` in this same loop (and established
   elsewhere in `sync_ap.py`).

No other field's write logic was touched — `status`, `due_date`, and
`notes` were already diffed correctly before this fix; only `owner_id` was
unconditional.

## Verification (real runs against production ORiON Supabase)

This script has no `--dry-run` flag (unlike `sync_ap.py`), and adding one
was out of scope for this fix. Verification used controlled real runs
instead, per the brief.

**Baseline (pre-fix, unfixed code):** ran the live script as-is.
Confirmed the bug: `inserted: 0, updated: 6, skipped: 0` — all 6 rows
rewritten, all `last_updated` bumped to within the same second
(16:56:58–59 UTC).

**Fix applied**, then a genuine owner mismatch was manufactured by hand
(UPDATE `action_items` directly) on one row — `Xyleme Modernization: ELT
Gas Turbine` — flipping its `owner_id` from the correct value
(`4bab65a8…`, Sherif Khalifa, per `COURSE_PLL_MAP`) to an incorrect one
(`a31f601a…`, Ben Smith's id). This gave the fixed script one real change
to detect and correct, alongside five rows with nothing to change.

**Post-fix run:** `inserted: 0, updated: 1, skipped: 5`.

| row | last_updated before | last_updated after | owner_id after |
|---|---|---|---|
| ELT Gas Turbine (manufactured mismatch) | 16:57:30 | **16:57:42 (bumped)** | corrected to `4bab65a8…` |
| EX2100e | 16:57:01.688 | 16:57:01.688 (unchanged) | unchanged |
| GT - Maintenance | 16:57:01.840 | 16:57:01.840 (unchanged) | unchanged |
| GT - Operation | 16:57:02.003 | 16:57:02.003 (unchanged) | unchanged |
| ST - Maintenance | 16:57:02.094 | 16:57:02.094 (unchanged) | unchanged |
| ST - Operation | 16:57:02.198 | 16:57:02.198 (unchanged) | unchanged |

The five unaffected rows kept their exact pre-run timestamps and owner
values — the negative case. The manufactured-mismatch row got both a
corrected `owner_id` and a fresh `last_updated` — the positive case,
proving the fix doesn't silently stop real changes from being written; it
self-healed the manufactured bad state back to the correct owner.

**Sync behavior otherwise unaffected:** both runs report identical sheet
fetch counts (466 modernization rows, 500 exam rows), identical courses
found (6) and tasks to sync (6), identical `inserted: 0` (no new courses),
and no errors in either run. Only the `updated`/`skipped` split changed,
exactly as expected — 6/0 pre-fix (every row rewritten) vs. 1/5 post-fix
(only the genuinely-changed row rewritten).

## Scope held

Did not touch `staleness_state()`, either staleness view, or any digest
script. Did not change `status`, `due_date`, or `notes` diff logic — they
were already correct. No lessons.md entry needed beyond the existing
2026-08-11 entry from the Approaching-Stale build ("a sync that
unconditionally keeps a field current poisons every consumer of
recency") — this fix is that exact lesson applied to `sync_xyleme.py`,
nothing new to log.
