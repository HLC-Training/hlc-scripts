# sync_ap.py — batch the WIP-overage check

**Decided:** 2026-08-11
**Repo:** hlc-scripts
**Bug:** 0514aaac-560e-4665-847f-18d4863bd131 (resolved)
**Action item:** 8b1835a3-067f-403a-8ee5-2b0211be4a89 (done)

## What changed

`check_and_flag_wip_overages` (`sync_ap.py`, ~lines 368-400 pre-fix) issued
one `action_items` SELECT per delivery owner inside `for owner_id in
owner_ids`, plus a conditional per-owner `users` UPDATE — ~9 owners ×
~20 scheduled runs/day ≈ 180 avoidable queries/day (Audit Pass 3 finding
A1, orion-pll `2026-08-11-audit-pass3-scale-readiness.md`).

Replaced with one `.in_('owner_id', list(owner_ids))` SELECT (now also
selecting `owner_id` itself, since a single query needs it to group rows
back per owner), counted in memory, then one `.in_('id', over_ids)` UPDATE
for whichever owners cleared the >5 threshold — mirroring the
load-once/batch-write discipline this script's own main reconcile loop
already uses for inserts (batches of 25) and the P&C/Delivery update
loops.

Pure query-shape change. Same threshold (`> 5`), same "currently working"
window (`start_date <= today <= due_date`, nulls unbounded), same table,
same field written (`pending_wip_review`). Nothing about what counts as a
WIP overage moved.

One behavioral tradeoff, inherent to batching and accepted as in scope for
"query shape only": the old per-owner `try/except` meant one owner's query
failure didn't block checking the others. The new single query means a
SELECT (or UPDATE) failure fails the whole check for this run's owner set
— logged and swallowed (returns 0), same as any other batch write already
in this script (see the Delivery/P&C insert-batch error handling above it
in `main()`). Not a regression class this script hasn't already accepted
elsewhere.

## Verification

**Method:** the WIP check only runs in live (non-dry-run) mode, and only
when a run inserts new Delivery rows — not something to force live against
production on demand. Instead, built an offline harness
(`check_and_flag_wip_overages` imported unmodified from the repo, fed a
fake Supabase client) backed by a real snapshot of every `Tier 2` /
`Open`+`In Progress` `action_items` row with a non-null `owner_id`,
pulled read-only from the production ORiON project (`czdkctjbejnwuopigxta`)
via Supabase MCP on 2026-08-11. The fake client executes the real
`.select()/.eq()/.in_()/.execute()` and `.update()/.eq()/.in_()/.execute()`
chains against the snapshot, counts every `.execute()` call per table, and
records what any `users` UPDATE would have matched — without ever making a
real network call, so no production write risk from testing.

Ran the harness against the pre-fix function, then again against the
post-fix function, same snapshot both times (8 real owners with active
Tier 2 items on 2026-08-11).

| | pre-fix (per-owner loop) | post-fix (batched) |
|---|---|---|
| `action_items` SELECT queries | 8 | **1** |
| `users` UPDATE queries | 2 | **1** |
| total queries | 10 | **2** |
| owners flagged | 2 | 2 |
| flagged owner ids | `0608aa17…`, `4bab65a8…` | `0608aa17…`, `4bab65a8…` (identical) |

A third, independent computation (plain Python over the same snapshot,
not calling either version of the function) also produced the same two
flagged owner ids and the same per-owner counts (6 and 16 active Tier 2
items respectively, both > 5) — cross-checking that neither implementation
has a shared bug the before/after comparison alone couldn't catch.

**Rest-of-sync unaffected:** the edit is contained entirely within
`check_and_flag_wip_overages`; nothing else in `main()`'s reconcile loop,
field diffs, or summary-line construction was touched (confirmed by
`git diff` — the function's signature and return type are unchanged, so
its one call site in `main()` needed no changes). The live scheduled run
immediately before this fix (2026-08-11T16:35:56Z, run 31513225338)
reported: `Delivery: inserted 0, updated 0, pending cleared 0 | P&C:
inserted 0, updated 0, pending cleared 0 | skipped: 91 ... WIP flags set:
0` — 0 flags because 0 new Delivery inserts that run, consistent with the
WIP check only firing on new inserts. A live run after this fix was
committed was used to confirm the summary line keeps the same shape/format
(see session verification-gate output for that run's numbers).

## Scope held

Touched only `check_and_flag_wip_overages`. No other function in
`sync_ap.py` was edited. No change to the overage threshold, the active
window definition, or what happens downstream of `pending_wip_review`.
No lessons.md entry — this is the batch-query pattern already established
elsewhere in this same script, applied to one function that hadn't caught
up yet.
