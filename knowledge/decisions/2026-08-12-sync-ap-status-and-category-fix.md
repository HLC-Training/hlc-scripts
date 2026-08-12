# sync_ap.py: status close + category guard

**Decided:** 2026-08-12
**Repo:** hlc-scripts
**Action item:** 4bed87b1-ac5e-4b37-bc76-45c2ec179ef2
**Bugs closed:** e6b35596-cf9d-4730-9829-9f4924827950 (status never reaches
complete/cancelled), b75a59f6-ea7c-452c-bc60-1f1d1ab898f3 (category
clobbered to NULL)

## What the code actually did (Task 1 findings, not the brief's guesses)

Both defects were real, and the status one had **one root cause with two
layers** — plus a Delivery-side sibling the brief told us to check for:

1. **The `ACTIVE_STATUSES` gate.** The main loop drops any inactive
   (Complete / Cancelled / On Hold) row unless `ap_pending_update` is set
   on either table. A row completed in Smartsheet while non-pending simply
   never syncs again — its ORiON status is frozen at whatever it was.
   Michele's 6 rows (`ap_pending_update` false on all) hit exactly this.
   There was no "inactive-row path that forgot to write status" — the
   inactive path is the same main loop, and these rows never entered it.

2. **`PC_STATUS_MAP` had no terminal mappings.** Only Not Started/In
   Progress were mapped, so `PC_STATUS_MAP.get('Complete')` returned
   `None`. Consequence: even a *pending* P&C row that went Complete in
   Smartsheet could never settle — the diff compared `None` to the stored
   status, always "diverged," and would have falsely escalated at 14 days.
   The docstring's settle promise ("ORiON Done vs Smartsheet Complete map
   to the same status and must clear the flag") only ever worked on the
   Delivery side, whose `STATUS_MAP` does map Complete/Cancelled→Done.

3. **Delivery had the same layer-1 defect.** Same gate, same freeze for
   non-pending rows completed in Smartsheet. Reported to Jim as a scope
   question per the brief's STOP rule; Jim expanded scope: fix both tables,
   and map On Hold too (it was unmapped in `PC_STATUS_MAP`, same root
   cause, so an on-hold pending P&C row could never settle either).

The category bug was exactly as the 8/11 doc quoted: an unconditional
`if category != ex.get('category')` diff that happily writes a computed
`None` over a stored value.

## The fixes

**Status (both tables).** `PC_STATUS_MAP` extended with
`On Hold→on_hold`, `Complete→complete`, `Cancelled→cancelled` (verified
against the live `pc_projects_status_check` constraint before trusting the
brief's copy). A new close pass runs inside the top-level inactive block,
for rows where neither table's pending flag is set: if the row already
exists in `action_items` or `pc_projects` and its stored status differs
from the mapped Smartsheet status, write **status only** (plus the
last_updated/updated_at stamp each update path already writes). Shaped
that way deliberately:

- *Update-only, never insert* — preserves the invariant that inactive rows
  never create anything.
- *Independent of lead resolution* — a cleared Lead cell must not leave a
  finished row showing active forever, and close candidates never touch
  the skip counters or unmapped-lead logs.
- *Status-only* — the row is inactive in the source; nothing else should
  move. In particular all 6 of Michele's rows had `category='Delivery'`
  with SQDCG since cleared, so a full-diff close would have nulled their
  categories on the way out (the other bug, fired by the fix for this one).
- Pending rows are untouched by the close pass; the settle diff keeps sole
  authority over the flag lifecycle. With the map extension, terminal
  settles now actually work on the P&C side.

Also added the Delivery-branch mirror of the P&C inactive guard: an
inactive row let through the top-level filter by the *other* table's
pending flag could previously have inserted into `action_items` if its
lead had moved from a TPM to a PLL. Latent, never observed, closed while
in the file.

**Category.** The one-line non-null guard the bug's notes named:
`if category is not None and category != ex.get('category')`. A blank or
cleared SQDCG can no longer null a stored category; a real category change
still syncs. Asymmetry (sync can set but never clear) is the accepted
design.

## Verification gate — all six criteria PASS, evidence inline

1. **Live Smartsheet re-check (6 rows):** all six (AP-0329-1, AP-0329-2,
   AP-0521-3, AP-0662-1, AP-0662-2, AP-0698-4) confirmed `Overall Status =
   Complete` in the live sheet on 2026-08-12, via direct API pull of column
   `890549111574404`. Also cross-checked **all 41** `ap_synced` rows and
   all 21 open `ap_import` Delivery rows against the sheet: first-run blast
   radius was exactly these 6 P&C closes and 0 Delivery closes. Nothing
   hidden.
2. **Manual production run:** no local ORiON key exists (GitHub-secret
   only), so the run was a `workflow_dispatch` of the fix pushed to a
   temporary branch (`gh workflow run sync-ap.yml --ref
   verify/ap-status-category-fix`, run 31625429868, conclusion `success`).
   Before: 5 rows `active`, AP-0521-3 `approved`, all stuck at 2026-07-30.
   After: **all 6 `complete`**, `updated_at` 2026-08-12 18:00:20Z, summary
   line `P&C: … closed 6`. Categories on all 6 still `Delivery` — the
   status-only close touched nothing else.
3. **Native non-regression:** 3 native rows with hand-set categories
   (d03c1a12…, 8c50743e…, d141c6b7…) byte-identical before/after —
   status, category, and `updated_at` (2026-08-10 19:35:16.44409+00)
   all unchanged.
4. **Category guard, positive proof:** disposable fixture — AP-0340-4
   (active in Smartsheet, SQDCG blank) hand-set to `category='Quality'`
   at 17:59:31Z, run at 18:00, after: category still `Quality`,
   `updated_at` still 17:59:31 (no write of any kind; `pc_updated=0`
   in the heartbeat corroborates). Old code would have written `None`.
   Fixture reverted to NULL afterward.
5. **Exit codes:** dispatch run exited 0 on success. The
   Smartsheet-fetch-failure exit-1 path was proven with the stub harness
   pattern from the 2026-08-10 lesson (real `main()`, stubbed
   `create_client` + `requests.get` raising): `SystemExit code == 1`.
   The same harness proved every new close path, the guard, and the
   pending settle in one run (8/8 checks). No new early returns were
   added; the close writes follow the existing log-and-continue batch
   discipline.
6. **Heartbeats:** `health:github:orion_ap_sync:last_run` (18:00:18Z) and
   `health:github:orion_ap_sync` (18:00:20Z) both updated normally; the
   completion heartbeat notes now carry `delivery_closed=` / `pc_closed=`.

## Reported, deliberately not fixed this session

- **Description/date clobber (same shape as category):** the P&C update
  path still writes a computed `None` over stored `description`,
  `start_date`, and `target_end_date` (the last also bumping
  `target_date_moves`). Smartsheet is the source of truth for those
  fields, so mirroring a cleared cell is arguably correct — but it's the
  same silent-null shape and deserves its own decision.
- **DB-load failures exit 0:** the `users` / `portal_users` /
  `ap_lead_aliases` / existing-rows load failures in `main()` all
  `return` with exit code 0 — the same "green run, did nothing" lie shape
  as the 2026-07-30 fetch incident, just for the Supabase side.
- **`pc_projects` now has triggers.** The 2026-08-05 lesson's "pg_trigger
  returned zero rows" is stale: `pc_projects_stamp_updated_at`,
  `pc_projects_status_transition_guard`, and `pc_projects_ap_date_ack_guard`
  now exist. All three gate on `auth.uid() is not null`, so service-role
  sync writes bypass them — verified before dispatching, since a status
  transition guard could otherwise have rejected the close writes.
- AP-181-3 is `ap_pending_update=true` in Delivery but its child row no
  longer exists in the sheet at all — no loop path (settle, close, or
  escalation) will ever see it; only the end-of-run pending report lists
  it. Needs a human look.
