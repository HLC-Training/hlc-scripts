# Decision: sync_xyleme exit-0 hardening, Delivery-side date null-guards

Decided 2026-08-12 (Jim's brief; action item SAM COS
`dac56281-bbf7-41b7-af32-6b6988665b3b`; bugs bcd87bc4, 74ebd314). Round 2
of the sync-family cleanup — both findings were surfaced by the 8/12 first
pass (commit `2b5417a`) and deliberately left unfixed there under the
builder-cannot-fix-its-own-flags rule. Fresh session, same fix-the-class
lesson from 8/3 proving itself one file over.

## sync_xyleme.py — four exit-0 paths (bug bcd87bc4)

`main()` had four bare-`return` early exits that logged an error and let a
failed run finish green. Same "green run lies" class as the six paths fixed
in `sync_ap.py` on 8/12 and the fetch path on 8/3 — the 8/12 session fixed
the service-key rename across the whole family but left this shape live in
the neighbouring file.

All four are now `sys.exit(1)`:

| Site | Failure |
|------|---------|
| ~421 | `create_client` — Supabase connection |
| ~430 | `users` load |
| ~438 | Smartsheet fetch (`fetch_sheet`) |
| ~456 | existing `xyleme_import` items load |

`import sys` was not present in this file (unlike `sync_ap.py`) and was
added.

**Deliberately unchanged.** The two per-item handlers (~486 update, ~514
insert) still `log.error` and continue: one bad row must not kill the whole
run. That is the opposite case from a load failure, where the run cannot
proceed at all. The heartbeat block at the bottom is non-critical by design
and sits after all four exits, so no ordering concern — a load failure now
exits before the heartbeat writes, which is correct (no heartbeat means the
run genuinely didn't complete).

## Delivery UPDATE diff — date guards (bug 74ebd314)

The Delivery `action_items` UPDATE diff carried the same unguarded `!=`
shape the P&C side had before 8/12: a blank/cleared Smartsheet date cell
computed `None` and wrote it over a stored `due_date` / `start_date`.

Jim's ruling (8/12): **guard both**, identical accident-when-blank basis as
the P&C side (bug 6f43d355). A blank date cell reads as accident, not
intent, and the failure is silent unrecoverable data loss. The guards are
shaped exactly like the P&C and category guards
(`X is not None and X != ex.get(...)`).

Unchanged, deliberately: `status`, `owner_id`, `priority`, and the `notes`
/ `description` mirror (a cleared free-text cell is intent — ruled 8/12).
The Delivery INSERT path is untouched: an insert writes the full computed
row, and a `None` there means "no value yet," not a clobber.

## The non-obvious part: `fields` feeds two behaviors, not one

This is the reason the guard needed a behavioral gate rather than a content
check, and it is the lesson worth carrying forward.

The `fields` dict built by this diff drives **two** consumers:

1. the write path — `if fields:` → queue an UPDATE; and
2. the `ap_pending_update` caught-up/divergence logic immediately below it
   (`if ex.get('ap_pending_update'): if not fields: ...`), which decides
   whether a PLL's protected change has been reconciled or is still
   diverging (and, past `ESCALATION_DAYS`, whether to escalate to a human).

So guarding the dates does not only stop a bad write — it changes what
counts as divergence. A blanked Smartsheet cell used to land in `fields`
and therefore read as "still diverging"; it now leaves `fields` empty and
reads as "Smartsheet caught up." That is the correct reading (a blanked
cell is not the PLL's change diverging), and it also removes a real prior
misbehavior: a blank cell could never "catch up," so the flag stayed set
indefinitely and eventually escalated a phantom divergence to a human as
`due_date: '2026-09-30' -> None` — an escalation about a change that was
never made.

**Residual trade-off, accepted, worth knowing.** Clearing the flag on a
blanked cell drops the PLL's protection without Smartsheet having genuinely
matched the PLL's value. If Smartsheet later gets a real, different date,
that date now overwrites the PLL edit instead of being held back. This is
narrower and less harmful than the old behavior (permanent stuck flag +
phantom escalation), so it stands as-is. Flagged to Jim rather than fixed —
changing it would mean editing the pending logic, which was explicitly out
of scope.

### How it was verified

Both consumers were proven, not reasoned about. The gate harness extracts
`sync_ap.py` lines 782-847 **verbatim** from the file — the field diff, the
pending branch, and the normal update branch — dedents them, and `exec`s
them against synthetic rows. Nothing is retyped, so the proof cannot drift
from the shipped code. No DB, no network, no module import (hence no env
keys).

| Case | `fields` | Branch taken |
|------|----------|--------------|
| not pending, stored due, incoming `None` | `{}` | no changes (skip) |
| not pending, stored due, incoming real change | `{'due_date': ...}` | normal update |
| not pending, stored start, incoming `None` | `{}` | no changes (skip) |
| **pending**, blanked date is the only diff | `{}` | **caught-up → clear flag** |
| **pending**, real differing date | `{'due_date': ...}` | **still diverging → escalate** |

The xyleme exits were proven the same way, with the lessons.md 2026-08-10
stub harness (dummy keys, stubbed `create_client` and `fetch_sheet`, zero
production contact): all four stages exit 1. The harness was also run
against `git show 2b5417a:sync_xyleme.py` as a control — all four exit 0
there, confirming the harness actually detects the bug rather than passing
vacuously.

## Lesson

When you guard a field, check what else reads the structure you are
guarding. A null-guard on a value that only feeds a write is a one-behavior
change; the same guard on a value that also feeds divergence, reset, or
escalation logic is a two-behavior change, and the second one is invisible
in the diff. Verify both.
