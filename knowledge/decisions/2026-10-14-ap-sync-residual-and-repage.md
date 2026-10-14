# AP sync residual: count the secondary-only projection, and page the child check on change

**Decided:** 2026-10-14
**Repo:** hlc-scripts
**Bug:** 6a9e3545 (AP sync monitor pages "child identity broke: 915 fetched, residual 1")
**Status:** active — shipped and live-verified
**Builds on:** `2026-08-25-ap-sync-reconciliation-monitor-design.md`,
`2026-08-29-ap-sync-monitor-run-health.md` (no decision there is reopened)

## The row

**AP-0775** (Smartsheet row id `8303958378614660`, "OFS Evaluation Gradebook").
A Complete pure parent (`is_parent` true, `is_child` false), led by Philip
Ganssmann, a `director` in `portal_users` (not in `users`). Its family
(AP-0775 / -1 / -2 / -3) is routed delivery-only for MEMBERSHIP, because
family membership deliberately ignores director leads (`via_director`, the
2026-09-11 owner-of-line ruling). Evidence: a production dry run of a throwaway
diagnostic branch that logs any row whose primary-disposition counters do not
move by exactly one printed one line:

    [RESIDUAL-DEBUG] ap='AP-0775' delta=0 row_id=8303958378614660 family='AP-0775'
    in_scope=True destination='pc' ... is_parent=True is_child=False pure_parent=True
    active=False status_raw='Complete' fam_modules=['delivery']
    sec_planned_before={... 'unchanged': 14 ...} sec_planned_after={... 'unchanged': 15 ...}

Same run, pre-fix code: `child_identity_residual 1` (control, `e299023`).

## The exit

`sync_ap.py`, in-scope branch of the child loop. For a pure parent with a
resolved destination, `row_modules = sorted(fam_modules[family])` (the header
projects into every module the family occupies) but `primary_module =
task['destination']`. For AP-0775 the destination is `pc` (director) and the
family's modules are `['delivery']`, so every iteration of `for m in
row_modules` had `secondary = (m != primary_module)` true. The row's only
disposition landed in `sec_planned` (the split_projection overlay) and the
trailing `continue` exited with no primary counter touched.

Why the page started 2026-09-11 17:08Z: before the owner-of-line change that
day (`389d4fb`), a director lead resolved to `none`, the unrouted branch took
`primary_module = row_modules[0]`, and the row counted normally. Making a
director a valid owner gave the pure parent a `pc` destination that its
family's module set does not contain. (Timing fits; the pre-change resolution
was read from the code, not replayed.)

## Classification: counted wrongly, not a missed write

The sync did what the rules say. ORiON `action_items` holds the AP-0775 header
(`ap_is_parent` true, status Done, `smartsheet_row_id` matching the sheet,
unchanged this run), and the secondary overlay reported it `unchanged`. No
`pc_projects` header exists for AP-0775 because the family does not occupy the
P&C module by the 2c ruling. Nothing was dropped. Only the bookkeeping missed
the row, so the STOP rule for a missed write did not trigger.

Flagged to Jim, deliberately not changed (it is what the sync does with the
row): that Delivery header's `owner_id` is the director's `portal_users` id,
which does not exist in `users`.

## The fix

New child bucket **`projected_secondary_only`**: an in-scope row whose own
primary module is outside its family's module set. Counted once per row,
after the module loop, when `primary_module not in row_modules`. The
projection itself is untouched and keeps reporting in `split_projection`.
Threaded through `build_sync_accounting` (it joins the identity sum
automatically; the identity's arithmetic is not changed), both accounting call
sites, the dry-run Scope line, the live summary line, and the heartbeat notes
as its own `secondary_only=` field. It is NOT folded into `skipped=`
(`skipped_total`), which keeps its meaning.

Readers of the accounting structure, checked before adding a key: the monitor
(`child_identity_residual` plus the failed-insert and mirror-failed counters,
by key), the heartbeat notes string, and the two summary lines. None sums the
child dict by position; the new key changes no consumer's behaviour.

### Child-loop exit map (every exit, its counter)

| # | Exit (sync_ap.py after fix) | Counter |
|---|---|---|
| 1 | in-scope, blank AP# or text (`continue`, ~2160) | `skipped_no_ap` |
| 2 | in-scope, primary-module disposition `insert` / `update` | `planned_primary_insert/update` (live: `inserted_*`/`updated_*`, `failed_inserts_*`) |
| 3 | in-scope, `unchanged` | `skipped_unchanged` |
| 4 | in-scope, `clear_pending` | `cleared_pending` / `cleared_pending_pc` |
| 5 | in-scope, `skip_pending` | `skipped_pending` |
| 6 | **in-scope, primary module outside the family's modules** (`continue`, ~2222) | **`projected_secondary_only`** (new) |
| 7 | out-of-scope inactive, non-pending, a close queued (`continue`, ~2268) | `closed_delivery` / `closed_pc` (queue lengths) |
| 8 | out-of-scope inactive, non-pending, no close queued (same `continue`) | `skipped_inactive_noop` |
| 9 | out-of-scope, blank AP# or text (`continue`, ~2272) | `skipped_no_ap` |
| 10 | out-of-scope ambiguous / viewer / otherwise unresolved (`continue`, ~2294, ~2305, ~2312) | `skipped_ambiguous` / `skipped_viewer` / `skipped_unmapped` |
| – | `skipped_inactive_wrongtable` | preserved, nothing produces it |

There is no `break` or `return` in the loop. Exit 6 was the only uncounted one
found. Two latent identity hazards were noticed and are NOT changed here (the
brief forbids touching the identity or what the sync does):

- Exit 7 can double-count: an out-of-scope inactive row with existing rows in
  BOTH modules queues two closes, adding 1 to each of `closed_delivery` and
  `closed_pc`, so the residual would go to -1 (`residual_impossible`). No row
  takes it today (zero closes queued).
- `skipped_unmapped` (exit 10) also receives out-of-scope rows whose lead is a
  director (`via_director`, destination `pc`) — the Ops APs left alone on
  purpose — and logs them as "does not resolve", which is misleading.

## Monitor: page on change, remind slowly

The monitor re-pushed an identical "residual 1" every ~12h: 58 pages in 34
days, until the page was noise (decision 5 of the 2026-08-25 design, "nobody
investigates green", from the other side). For the **child check only**
(`ap_sync_monitor.py`):

- push on a status transition (healthy to broken, broken to healthy, one broken
  mode to another) and whenever the residual VALUE changes;
- while the same status AND the same residual persist, remind at most once per
  7 days (`CHILD_REMINDER_WINDOW`);
- the residual is stored in the check's state entry (`value`); a state entry
  written before this rule has no value and is treated as unchanged, so the
  upgrade itself does not page.

`titles` and `run_health` keep the 12h re-nag: the brief left everything else
about the monitor unchanged, and a stuck cron should not go quiet for a week.
Recovery pushes are unchanged.

## Verification (G1–G7)

- **Control (G1):** production dry run of `main` @ `e299023`, run 38585037469:
  `"skipped_inactive_noop": 166, ..., "child_identity_residual": 1`.
- **Row (G2):** the `[RESIDUAL-DEBUG]` line above (run 38585078237, branch
  `diag/ap-residual-6a9e3545`, since deleted).
- **Post-fix dry run (G4):** run 38585790531 @ `6b5cdf7`, production data. Diff
  of the full accounting payload against the control: only
  `child.child_identity_residual 1 -> 0` and `child.projected_secondary_only
  None -> 1`. Every other bucket, `split_projection` and `mirror` identical.
- **Harness (G3, G5):** `tests/test_ap_sync_child_identity.py` extracts the real
  counter-init + routing + child loop from `sync_ap.py` source and execs it; the
  control runs the same rows through `e299023`'s source and gets residual 1.
  `tests/test_ap_sync_monitor_repage.py` drives the real monitor through
  `--accounting-file/--state-file/--now/--last-run`, each case also against the
  pre-fix script: (a) same residual <7d no push (control pushed), (b) same
  residual >=7d one reminder, (c) residual 1->2 push (control missed it), (d)
  healthy->broken push, (e) broken->healthy push, identical to before.
- **Live (G6, G7):** runs 38587699919 (15:15Z) and 38589896271 (15:30Z) on
  `6b5cdf7` both logged `child_identity_residual=0`; the 15:30:33Z payload in
  `health:github:orion_ap_sync:accounting` has `projected_secondary_only: 1`,
  `child_identity_residual: 0`. The monitor sent one `recovered (child)` push at
  15:20Z and no `child broken` page after the merge.

## What this does not do

It does not change what the sync does with AP-0775, tolerate a residual, or
touch ORiON schema. The two latent hazards above are the next places the
identity could close or break unexpectedly; each would need its own decision.
