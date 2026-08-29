# AP sync monitor: escalate on run health, not write staleness

**Decided:** 2026-08-29
**Repo:** hlc-scripts
**Board item:** cf6bbfb7
**Status:** active

## The bug

`ap_sync_monitor.py`'s staleness check compared `now` against
`health:github:orion_ap_sync:accounting`'s own `as_of` timestamp — a write-age
check standing in for a run-health check. That conflation had a real blind
spot in the failing direction: a run can complete with nonzero
`failed_inserts_delivery` / `failed_inserts_pc` / `mirror_failed` and still
write fresh `accounting`, so the old check read `healthy` regardless.
Verified live: replaying a captured accounting payload with
`failed_inserts_delivery: 3` against the pre-fix code (git `d98dac6`) via the
`--accounting-file`/`--state-file` harness logged `Evaluations: {'stale':
'healthy', ...}` — a run with real Supabase write failures raised no alert.

The originating brief hypothesized a different failure mode — "a clean run
that finds nothing to sync doesn't advance the write timestamp, so a healthy
quiet window reads as stale." That hypothesis doesn't hold against this
code: `sync_ap.py` writes `accounting` unconditionally on every run that
reaches the end of `main()` (build_sync_accounting is called regardless of
insert/update counts), so a genuinely on-time, genuinely quiet run always
produces a fresh `as_of`. Live behavioral test (fresh `last_run` 8 min old,
matching fresh `accounting`, all counts zero) confirmed both the pre-fix and
post-fix code already read `healthy` for that case.

What the live data actually showed at reproduction time (2026-08-29 ~15:30
CT): `sync-ap.yml` (nominal cron `13,43 * * * *`, every 30 min) is running
with routine 4–11 hour gaps, worsening through 2026-08-26 to 2026-08-29 —
the exact symptom the 2026-07-29 "GitHub Actions cron contention" lesson
described, which action item d2dd9a25 was recorded as having fixed. It
isn't holding. The `system:ap_sync_monitor:alert_state.stale` entry seen
mid-session (`status: "stale"`, `last_push` 2026-08-29T10:23Z, citing a
4h11m gap) was a true positive against a real cron gap, not a false one.
That cron-reliability problem is separate from this fix and unresolved —
flagged for its own board item, not addressed here (scope: monitor logic
only, per this item's boundaries).

## The fix

Replaced `evaluate_staleness(accounting, now)` with
`evaluate_run_health(last_run_value, accounting, now)`, reading a second
context_store key (`health:github:orion_ap_sync:last_run`, the
started-class heartbeat `sync_ap.py` already writes before the Smartsheet
fetch — see the 2026-08-03 lesson). Statuses: `healthy` | `run_overdue` |
`run_failed` | `absent`.

1. `run_overdue`: `now - last_run > 4h` (unchanged threshold value, already
   tuned past the observed contention noise — this fix changes what the
   threshold gates, not the threshold itself).
2. `run_failed`, three ways:
   - No `accounting` reached at all despite a non-overdue `last_run`.
   - `accounting.as_of` predates `last_run` by more than ~2 minutes — since
     `last_run` is written before the Smartsheet fetch and `accounting`
     near the end of the same run, a healthy run always has
     `as_of >= last_run` within seconds. An older `as_of` means the most
     recent run(s) started and crashed before reaching the accounting
     write — the exact class of failure a write-age-only check is
     structurally blind to (a crash loop between the two writes would keep
     `last_run` fresh forever while `accounting` never advances, and a
     naive last_run-only check would never catch it).
   - `accounting`'s own failure counters (`failed_inserts_delivery`,
     `failed_inserts_pc`, `mirror_failed`) are nonzero.
3. Otherwise `healthy` — including a clean, on-time, zero-change run.

`child` and `titles` sub-checks are unchanged: they already key off
`accounting`'s content (`child_identity_residual`, title counts), not off
any freshness timestamp, so they didn't share the flawed pattern.

## Verification (behavioral, `--accounting-file`/`--state-file`/`--now`/`--last-run` harness)

| Scenario | last_run | accounting | Result |
|---|---|---|---|
| Healthy quiet window | 8 min old | matches, all counts 0 | `healthy`, no push |
| Genuinely overdue | 6.5h old | — | `run_overdue`, pushes |
| Failed counters, fresh write | 8 min old | fresh, `failed_inserts_delivery: 3` | `run_failed`, pushes |
| Crash before accounting write | 2 min old | 6h stale (predates last_run) | `run_failed`, pushes |
| Control: same failed-counters payload against pre-fix code (`d98dac6`) | — | fresh, `failed_inserts_delivery: 3` | `healthy` — the bug, confirmed live |

Deploy verification (post-merge, live cron): confirm `alert_state.run_health`
appears in `system:ap_sync_monitor:alert_state` after the next scheduled
run, and that its status matches live `last_run`/`accounting` state at that
moment (per the table above — it may legitimately be `run_overdue` if the
cron gap pattern above is still live).
