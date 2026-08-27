# Unresolved-lead leaves project into every module their family occupies

**Date:** 2026-08-27 (follow-up to the family-scope widening shipped earlier
the same day — see `2026-08-27-ap-family-scope-widening.md`).
**Action item:** 37bc44dd (carries the follow-up ruling verbatim).

The widening build deliberately left one shape unruled: a leaf whose Lead
resolves to `none`, inside an in-scope SPLIT family (members in both
modules), had no single-module answer under the fallback clause. The build
mirrored those rows without projecting them and warned on every run.

**Ruling (Jim, 2026-08-27):** a null-owner row — leaf or parent — projects
into EVERY module its family occupies, as a null-owner leaf
(`owner_id = null` + `ap_lead_display`). General rule, not an AP-0492
one-off. The second module's ops ride the existing `split_projection`
accounting stream, exactly as parent headers do.

Implementation (sync_ap.py, commit 1825dbc): the unresolved-lead branch
collapses to `row_modules = sorted(fam_modules[family])` for all null-owner
rows. Single-module families are byte-identical to the prior behavior; only
split-family leaves gain the second projection. The
`skipped_split_unresolved` counter and its warning are retired — the child
accounting identity simply has one fewer term.

Live trigger and first projection: `AP-0492-2` (Marcela Bohorquez) and
`AP-0492-3` (Philip Ganssmann), both Cancelled leaves of the terminal
split family AP-0492 — each now a null-owner leaf in BOTH `action_items`
and `pc_projects`. No UI change was needed: null-owner leaves already
render as plain non-filterable cards with the display-text fallback (the
main build shipped that path and other null-owner leaves were already
live, e.g. AP-0956-2-4).
