# Decision: sync-family exit-0 hardening, P&C date null-guard, service-key rename

Decided 2026-08-12 (Jim's brief; action item SAM COS
`83271599-a1b0-47bf-8c6a-ebf30f8561c3`; bugs 2ae04371, 3aae0392 (dupe),
6f43d355, d2f44099). Bundled per the 8/3 fix-the-class lesson: three
failure shapes across the sync family, one session.

## Exit-0 hardening in sync_ap.py — six paths, not five

Bug 2ae04371 reported FIVE bare-`return` early exits in `main()` that let
a failed Supabase load produce a green GitHub Actions run. Code review
found SIX — the report didn't count the ORiON client-init failure
(`create_client`, ~line 476) separately from the five table loads
(`users`, `portal_users`, `ap_lead_aliases`, existing Delivery items,
existing P&C items). All six are now `sys.exit(1)`; the Smartsheet-fetch
path already exited 1 (bug 306cea89, 8/3). The lesson from the miscount:
when converting a class of early exits, enumerate them from the code
(`grep` the function), not from the bug report's list.

Heartbeat semantics are unchanged: the started-class heartbeat writes at
~464 before all six sites, and the completion heartbeat at the bottom of
`main()` was never reachable from these paths. Only the exit code CI
reports changed. The two remaining bare `return`s in `main()` (no active
tasks ~567, dry-run complete ~1022) are genuine success paths and stay.

## P&C update-diff date guards — per-field ruling, not a blanket rule

The P&C UPDATE diff wrote a computed `None` over stored values whenever a
Smartsheet source cell was blank. For `target_end_date` the same branch
also incremented `target_date_moves`, so a cleared cell recorded a
phantom schedule slip in the P&C timeline's drift counter.

Jim's per-field ruling (8/12), now encoded as guards shaped exactly like
the existing category guard (`X is not None and X != ex.get(...)`):

- **`start_date`, `target_end_date`: GUARD.** A blank date cell reads as
  accident — dates aren't deliberately cleared in the tracker, and a
  None-write destroys a stored value (and, for target_end_date, corrupts
  the drift counter with an increment for a move that never happened).
- **`description`: MIRROR (unchanged, deliberately).** Free text the
  source owner cleared is intent — guarding it would pin stale text in
  ORiON forever. The `description` diff at ~885-886 is untouched.
- **`category`: already guarded** (bug b75a59f6, commit 3674066) —
  sparse source, same accident class as dates.

The INSERT path (~953) is deliberately NOT guarded: a `None` on insert is
a genuine "no value yet," not a clobber of anything.

**Flag, not fixed:** the Delivery-side diff (`action_items`) has the same
shape for `due_date` and `start_date` (bare `!=`, no None guard). Whether
Delivery dates get the same accident-when-blank ruling is a separate
per-field decision Jim hasn't made. Do not assume this doc's ruling
transfers.

## Service-key rename — sync_xyleme.py, send_ap_pending_digest.py

Both read the generic `SUPABASE_SERVICE_KEY` for the ORiON project
(`czdkctjbejnwuopigxta`) — the exact wrong-project-key collision risk
that motivated the sync_ap.py rename (bug 306cea89, lessons.md 8/3).
Renamed to `ORION_SUPABASE_SERVICE_KEY` at all three sites in each file
(env read, startup guard, create_client), matching sync_ap.py's pattern.
`SAMCOS_SERVICE_KEY` in sync_xyleme.py is the correct cross-project write
key and is untouched, as is `RESEND_API_KEY` in the digest.

No repo-settings step was needed: the secret was already named
`ORION_SUPABASE_SERVICE_KEY` at the repo level, and both workflows
(`sync-xyleme.yml`, `ap-pending-digest.yml`) were mapping it DOWN to the
generic name in their `env:` blocks. The workflow change deletes that
renaming-on-the-way-in, same as sync-ap.yml on 8/3.

**Flag, not fixed (out of scope):** `sync_xyleme.py main()` has FOUR
bare-return-on-failure exit-0 paths of its own (client init ~420, users
load ~429, Smartsheet fetch ~437, existing-items load ~455) — the same
class this session fixed in sync_ap.py. Reported for a follow-up ruling;
not in the 8/12 scope. `send_ap_pending_digest.py` has no swallowed
failures (no try/except at all — any failure raises and exits non-zero;
its two bare returns are success paths).

## Verification

Real `main()` run with a stubbed Supabase client raising on the `users`
load: process exited 1. The actual P&C diff block (extracted from the
file, not a copy) run against a stored row with `due_date=None`,
`start_date=None`: `fields` came out empty — no date clobber, no phantom
`target_date_moves` increment; a real date change still synced and
bumped the counter to 2; a cleared description still mirrored. All three
scripts `py_compile` clean.
