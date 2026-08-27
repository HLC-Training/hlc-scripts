# AP sync widens to the full P&C + Delivery family tree (family-scoped membership)

**Date:** 2026-08-27
**Action item:** 37bc44dd (scope of record; carries the Task-1 discovery + Jim's four rulings). **Bug:** 3e1cdc00 (Luca Martino — standalone APs invisible in ORiON).
**Repos:** hlc-scripts (`sync_ap.py`, this doc) + orion-pll (rendering/consumer side — see that repo's `knowledge/decisions/2026-08-27-ap-family-projection-ui.md`).

## What changed

`sync_ap.py` fetched only `is_child` rows, so every childless top-level AP
(126 standalones in the live sheet) never reached ORiON. The fetch now
captures EVERY AP-numbered row — children, mid-nodes (both flags, bug
59cd7d7b), umbrella parents, standalones — and membership is decided at the
FAMILY level:

- **Family key** = the top-level `AP-\d+` prefix of `ap_number`
  (`family_key()`, mirroring orion-pll's `topLevelApNumber`).
- **A family (or standalone) is in-scope iff ≥1 member's Lead routes to a
  module** via the unchanged per-row rule (`resolve_lead_email` →
  `resolve_owner`: users-non-viewer → delivery, portal_users-tpm → pc).
  The Lead rule still decides each row's OWNER; the family decides
  MEMBERSHIP. Families with zero routed members (Customer/Internal/Ops-led)
  stay out — that population's widening is deferred (2026-08-26 phasing).
- **Every in-scope row mirrors** into `ap_tracker` and **projects** into a
  module table, ALL statuses included (Complete/Cancelled — ORiON is
  becoming the system of record). First-run backfill measured at planning:
  495 mirror candidates across 143 in-scope families (from 148 mirror rows).

## The projection contract

- **One parameterized projection path** (`plan_module_projection` + per-
  module spec dicts + `build_delivery_*` / `build_pc_*` builders) replaces
  the two parallel destination branches. Field rules preserved verbatim:
  date/category clobber guards (74ebd314 / 6f43d355 / b75a59f6), priority
  never re-forced (aaaa96ea), Delivery's no-text-sync asymmetry.
- **Pure parents (`is_parent AND NOT is_child`) project as family headers**
  into EVERY module their family occupies — a split family (leaves in both
  modules; 5 exist) gets the header in BOTH tables. The second-module
  projection is tracked in a separate `split_projection` accounting stream
  so the child identity still counts each ROW exactly once.
- **Unresolved/viewer/ambiguous leads on in-scope rows** project with
  `owner_id = NULL` plus the raw Smartsheet lead text in the new
  `ap_lead_display` column (both module tables; `pc_projects.owner_id`
  NOT NULL was dropped — user-facing RLS keys on `owner_id = auth.uid()`
  or lead helpers, so a NULL-owner row is writable only by service_role).
- **Module rows key on `(ap_number, pure_parent)`**, not bare ap_number:
  AP-036 / AP-093 / AP-174 exist as a parent row AND a child row sharing
  one flat number. Legacy rows (all leaves) key `(ap, False)` and keep
  matching — including AP-175, the pre-existing flat `is_child` row, which
  the backfill updates rather than double-inserts.
- **Shape flags** (`ap_is_parent`, `ap_is_child`) ride on every projection
  row and are diffed as METADATA, separate from content: the pending-settle
  logic judges divergence on content fields only, so the first run's
  flag backfill on ~146 legacy rows cannot hold a pending flag open or
  escalate as a PLL's change (verified in the 2026-08-27 CI dry run: the 3
  pre-existing pending rows skipped on genuine content divergence, zero
  escalations fired).
- **Statuses**: lookups are case-tolerant (`"Not started"` exists in the
  sheet) and default with a warning (`Open` / `approved`) — all statuses
  must land somewhere CHECK-legal now that terminal rows insert.

## Unruled shape, deliberately not guessed

An unresolved-lead LEAF inside a SPLIT family has no module answer under
the rulings (the fallback clause names a single family module; a split
family has two). Exactly two rows exist today — **AP-0492-2 and AP-0492-3**
— and they are MIRRORED but NOT projected, each run logging a warning and
counting `skipped_split_unresolved`. If Jim rules a destination, the
projection is a small change in the `row_modules` branch of main().

## Mechanics preserved unchanged

- Mirror loop-prevention (echo/conflict/protect, decisions 69ba45bd /
  e217604f / 93113ee9) — the fetch widened; `plan_mirror_writes` did not.
- Out-of-scope rows run the PRE-widening path verbatim: inactive
  settle/close only (e6b35596), active skip counters, never insert.
- Orphan detection now keys on the ap_numbers of the FULL fetched row set
  (all shapes, scope-blind), so parents/standalones can never be
  mis-orphaned and a family dropping out of scope cannot orphan its rows.
- WIP overage check excludes pure parents
  (`.or_('ap_is_parent.eq.false,ap_is_child.eq.true')`) — mid-nodes and
  standalones still count (ruling #4: they are real work).
- Reconciliation-monitor accounting: same identity, new terms
  (`skipped_split_unresolved`) and a new non-identity stream
  (`split_projection`). `child_identity_residual` closed at 0 on the
  widened population in the CI dry run (720 rows).

## Verified before going live

Branch `feat/family-scope-sync` + a new `dry_run` workflow_dispatch input;
CI dry run against production data: 143 in-scope families (5 split),
495/495 mirror candidates plan cleanly, Delivery would insert 162 / update
64, P&C insert 189 / update 80, orphan flags 0, AP-175 plans as UPDATE,
AP-0823/0824/0833 insert to the correct modules with the correct owners.
