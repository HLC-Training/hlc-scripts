# AP sync reconciliation monitor — design

**Decided:** 2026-08-25 (grill-me with Jim)
**Repo:** hlc-scripts (Phase 1); a Phase 2 monitor job to follow
**Action item:** 9278f68e
**Status:** active — Phase 1 not yet built
**Origin:** bug 0644145e (AP-0214 class: 709 rows fetched, loop stopped at 500,
209 silently dropped, sync exited green). The durable fix so a silent sync drop
is system-caught, not user-caught.

## The core idea

The sync emits its own accounting every run; a separate monitor audits that the
math closes. The governing check is an arithmetic identity: every child row the
sync fetches must land in exactly one counted disposition, so

    len(child_tasks) == sum(all child dispositions)

If that identity ever fails to close, rows went unaccounted — which is exactly
the AP-0214 failure. This is grounded in the 2026-08-25 recon of sync_ap.py,
which established the real disposition set and, critically, found two uncounted
exit points that make the identity un-closeable as the code stands today.

## Decisions

1. **Sync emits its books; monitor audits the identity.** The inclusion rule
   lives in exactly one place — sync_ap.py. The monitor does not re-derive it
   (that would be a second copy that drifts and false-alarms). Rejected the
   alternative of an independent re-derivation in the monitor.

2. **Close the identity at the source.** The recon found two child-loop exits
   that increment no counter (bare `continue` at sync_ap.py:759 and at
   :811/:919). A monitor auditing an identity that structurally cannot close is
   worse than useless. Phase 1 fixes the sync so every child row is counted,
   rather than having the monitor tolerate an uncounted residual (which would
   reintroduce the AP-0214 blind spot by design).

3. **Two new fall-through buckets, not one.** The two uncounted exits are
   distinct: :759 is inactive/non-pending/no-op (no existing row, or status
   unchanged, or status unmapped); :811/:919 is an inactive row pending on one
   table that resolved to the *other* table. They get separate counters
   (`skipped_inactive_noop`, `skipped_inactive_wrongtable`). The wrong-table
   case is a latent edge-transition smell worth watching alone — if it's always
   zero the bucket proves it for free; if it ever isn't, that's a signal, not
   noise to bury in a generic total.

4. **The monitor is a separate job, not an in-sync check.** Only a separate
   job (cron or health-signal reader) can also catch "the sync didn't run at
   all" and "the heartbeat is stale" — which is half of what AP-0214 was. An
   in-process check has every number for free but is blind to the sync's own
   absence.

5. **Push alert, not dashboard-only.** A broken identity pushes (Pushover
   minimum). "Nobody investigates green" — a dashboard color change relies on
   someone looking, which is the exact failure a silent-drop alarm must not
   depend on.

6. **Warn and keep syncing; do not halt.** A failed identity means "the count
   is unexplained, investigate," not "the rows are poison." The rows the sync
   wrote aren't necessarily wrong — the count is just unaccounted. Halting
   writes would turn a monitoring signal into a self-inflicted outage of stale
   ORiON data. So: alert, but let the sync write.

## Build shape — two phases, never concurrent

**Phase 1 (precondition, this repo, touches sync_ap.py):** count the two
currently-uncounted exits as two new buckets; emit the full per-reason
accounting durably and structured (today the six skip reasons are rolled into a
single `skipped_total` in the heartbeat notes — the per-reason split only exists
in ephemeral log lines). After Phase 1, the child identity provably closes and
the accounting is machine-readable off the heartbeat.

**Phase 2 (after Phase 1 is shipped and live-verified):** a separate monitor
job reads the structured accounting from the SAM COS `context_store` heartbeat,
asserts the child identity closes AND the parent-titles identity closes, and
pushes on any failure, stale heartbeat, or missing run. Phase 2 is not designed
in full yet — its open questions (cadence, exact heartbeat read contract, the
precise terms of the parent-titles identity) are deliberately deferred until
Phase 1's emit format is real rather than hypothetical.

**Do not build both at once.** The lessons rule stands: debugging the sync
change and the monitor simultaneously is how a partial loop hides. Phase 1
ships and verifies live first.

## Identity, precisely (from the recon)

- The child identity closes against `len(child_tasks)` (the post-`Is Child`
  filter count), NOT `len(all_rows)` (the whole sheet including parents).
- `parent_titles` is a SEPARATE stream with its own identity
  (`len(parent_titles) == titles_written + its own skips/failures`), audited
  separately — never folded into the child identity.
- Rows dropped at fetch time before either stream (mid-level Is-Parent, blank-AP,
  duplicate/blank-title top-level parents) are currently uncounted; whether they
  need their own accounting is a Phase 2 open question, not a Phase 1 blocker.
- `escalations` is an overlay on `skipped_pending`, not a competing bucket — the
  monitor must not treat it as a term in the sum.
