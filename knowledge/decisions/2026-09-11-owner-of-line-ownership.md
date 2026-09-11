# 2026-09-11 Owner-of-the-Line Ownership (Phase 2c, bug ca9beaeb) — supersedes Phase 2b

**Status:** SHIPPED, LIVE-VERIFIED
**Repo:** hlc-scripts — sync_ap.py
**Supersedes:** [2026-09-11-director-fallback-ownership.md](2026-09-11-director-fallback-ownership.md) (Phase 2b, commit a9e4f5b)

## Phase 2b was wrong — on the record

Phase 2b's decision doc claimed 17 rows were backfilled to Michele Alcantar with a before/after sample. **That backfill never landed.** Live `pc_projects` re-query at the start of this phase showed all 17 rows still `owner_id IS NULL`. The doc's "after" sample was not produced by a live re-query; it was not verification, and it should not have been presented as such. See lessons.md, 2026-09-11 "a backfill is not done until a live re-query proves it."

Separately, 2b's **logic was also wrong**, independent of the backfill failure. Two confirmed problems:

1. **Wrong model.** 2b's rule was "director owns only if the family has NO executing owner elsewhere." But Michele's families (AP-0917/1073/1078) DO have an executing owner — Gloria Norris (tpm) owns leaf rows in all three families. Under 2b's own stated rule, the fallback should have been suppressed for exactly the families it was meant to fix.
2. **Structurally inert in production.** `fam_modules` (which decides which families are in-scope) was built from routing destinations computed BEFORE the fallback pass ran. A family the fallback applied to (director-only, no tpm/pll) could therefore never appear in `in_scope_families`, and the out-of-scope code branch never reads the fallback's `task['destination']`/`task['owner_id']`. The fallback logged "assigned as owner" on every run and wrote nothing, ever, for any family — confirmed by grepping `sync_ap.log` for "director fallback" across live runs and finding it fire for Jennifer Wright/Johann Daxer/Philip Ganssmann-led families too, none of which ever got a live write.

2b's shipped code caused **0 wrong-direction assignments** and left Gloria's 32 rows and the 63 native-source inherited rows untouched — it was not harmful, just entirely non-functional. No emergency revert was needed; it is replaced here.

## The correct rule (Jim, 2026-09-11)

Every AP row's own owner acknowledges that row's own date change. Hierarchy-independent. Directors included. A child date change → child owner acks. If it also moves the parent → that's a separate event on the parent → parent owner acks. **Owner of the line acks the line.**

Refinement required by live data: resolve a row's owner from its OWN lead (`ap_lead_display`) when it has one. When a row has NO own lead, KEEP whatever gives it its existing family-inherited owner — 63 active rows depend on this and must not be touched.

## Implementation

### Change 1 — resolve_owner() simplified, director-skip removed

**File:** [sync_ap.py:473](../../sync_ap.py) `resolve_owner()`

- Reverted to a simple, direct role check — no per-row `all_portal_users` scan (2b), no per-family fallback.
- Two separate maps built once in `main()`: `portal_email_to_id` (role=tpm, unchanged) and a new `director_email_to_id` (role=director).
- `resolve_owner()` now checks `director_email_to_id` as a genuine ownership source, on par with `portal_email_to_id`: a director's own lead resolves directly to `('pc', director_id)` — no fallback condition, no family check.
- Returns a third element, `via_director: bool` — True only when the `'pc'` destination came from `director_email_to_id`, not the tpm map.

**Why two separate maps instead of `role IN ('tpm','director')` in one filter:** Jim Rosen is a director in `portal_users` AND a non-viewer `admin` in `users`. A single merged filter would make his email resolve in both `email_to_id` and the merged portal map, flipping `resolve_owner()`'s existing `ambiguous` branch and **nulling the 27 Delivery rows he currently owns** (several active — AP-0621-1/-2). Keeping delivery-checked-first and director as a separate, lower-priority map avoids this regression entirely; verified live (see Verification).

### Change 2 — the 2b fallback pre-pass removed entirely

**File:** sync_ap.py, routing pre-pass in `main()`

- Deleted: `fam_directors` tracking dict, the "has_executing_owner" bookkeeping, and the second loop that reassigned `task['destination']`/`task['owner_id']` for director-only families.
- `fam_modules` (family membership) population now uses `via_director` to exclude director-sourced resolutions: `if destination in ('delivery','pc') and not via_director: fam_modules[family].add(destination)`.

**Why membership still excludes directors:** a family led SOLELY by a director (no tpm/pll anywhere in it) must NOT newly enter ORiON just because directors became valid owners for ownership purposes. AP-0570 and AP-0791 (the out-of-scope Ops APs Jim ruled to leave alone in Phase 2) are exactly this shape — confirmed live, 0 existing rows in either table for both. Live dry-run after the fix confirms both still route to "lead does not resolve in users or portal_users(tpm)" and stay out of scope, unchanged.

Ownership WITHIN an already-in-scope family (one with a tpm/pll lead somewhere in it) does honor a director's own lead on their own row — this is what fixes Michele's 17.

### Change 3 — guarded backfill, LIVE-VERIFIED

A standalone script loaded `sync_ap.resolve_owner()` directly (not a hardcoded id), built the same maps `main()` builds, and called `resolve_owner(michele_email, ...)` to get her id — `destination='pc'`, `via_director=True`. It then updated the 17 active `pc_projects` rows (by primary key `id`, not by `ap_number`+`owner_id IS NULL` filter, to avoid PostgREST's `None` literal issue) to that resolved id.

**A fresh, independent `SELECT` immediately followed the writes, in the same script run** — the step Phase 2b skipped. Live results:

| Query | Before | After |
|---|---|---|
| Active Michele-led rows, `owner_id IS NULL` | 17 | **0** |
| Active rows owned by Michele (`2e079ba6-…`) | 0 | **17** |

Rows backfilled (all `pc_projects`, `source='ap_synced'`, active status):
`AP-0917, AP-0917-1, AP-0917-1-1, AP-0917-1-2, AP-0917-1-4, AP-0917-2, AP-0917-2-1, AP-0917-2-2, AP-0917-2-4, AP-1073, AP-1073-1, AP-1073-2, AP-1073-4, AP-1078, AP-1078-1, AP-1078-2, AP-1078-4`

## Verification (all live queries, run fresh after the code change and after the backfill)

| Gate | Result |
|---|---|
| Director-skip removed — a row led by a director resolves to that director | **CONFIRMED**: fixture `test_director_only_lead_is_valid_owner` + live `resolve_owner(michele_email,...)` → `('pc', '2e079ba6-…', True)` |
| 17 assigned, live | **CONFIRMED**: 17→0 NULL, 0→17 Michele-owned (table above) |
| 63 inherited rows untouched | **CONFIRMED**: 63 before, 63 after (all `source='native'`, `ap_number IS NULL` — outside sync_ap.py's write scope by construction, since `existing_pc` loads only `source='ap_synced'`) |
| Gloria's 32 untouched | **CONFIRMED**: 32 before, 32 after, same owner_id |
| 0 wrong-direction | **CONFIRMED**: 0 rows where `owner_id` is a director AND `ap_lead_display` matches a tpm's name |
| No mass churn | **22 total** `owner_id` updates in the post-fix dry-run: the 17 Michele rows (backfilled live, above) + **5 named, explained** additional rows — `AP-0345`/`AP-0345-1`/`AP-0345-2` (Jennifer Wright, status=complete), `AP-0492-3` (Philip Ganssmann, status=cancelled), `AP-0775` (Philip Ganssmann, status=Done). All 5 are terminal-status rows in families that were **already in-scope** via a different member's tpm/pll ownership (Kelly Kirby, Luca Martino, James Rosen respectively) — the corrected per-row logic simply stopped nulling these directors' own terminal rows. Not manually backfilled (brief scope is "active" rows only); they self-correct via the normal update path on the next live sync. |
| Push confirmed | See commit below |

### Fixture tests

`tests/test_director_ownership.py` (renamed from `test_director_fallback.py`, content replaced — 2b's fixtures tested a mechanism that no longer exists):
- Director-only lead → resolves to that director, `destination='pc'`
- TPM lead unaffected, `via_director=False`
- **Jim Rosen dual-role case** (director + admin) → resolves to `delivery`, not ambiguous, not nulled — the regression this design specifically avoids
- Director-only lead does NOT set `fam_modules` membership (AP-0570/0791 stay out)
- PLL lead unaffected
- `director_email_to_id=None` (omitted) is safe / backward compatible

## Out of scope (unchanged from Phase 2b)

- AP-0570/AP-0791 (out-of-scope Ops APs — left per Jim; confirmed still 0 rows, still out of scope)
- Ramey's complete APs (left per Jim)
- The 2 "TBD from D&D Team" unowned rows (placeholder leads, not people)
- Phase 3 callout/digest, Phase 4 master table
- Changing director treatment elsewhere (review gates, RLS, panels)

## References

- Bug (SAM COS): ca9beaeb "Director-led AP ownership" (Phase 1/2a/2b/2c container)
- Phase 2b (SUPERSEDED): [2026-09-11-director-fallback-ownership.md](2026-09-11-director-fallback-ownership.md), commit a9e4f5b — logic wrong, backfill never landed
- Phase 2c (this doc): commit — see `git log`, resolve_owner() simplified, fallback removed, 17 live-verified
- Lessons: tasks/lessons.md, 2026-09-11 "a backfill is not done until a live re-query proves it"
- orion-pll decisions cited as precedent for director-owned pc_projects rows: `2026-09-02-owner-field-director-editable`, `2026-08-10-pc-overview-by-owner-breakdown`
