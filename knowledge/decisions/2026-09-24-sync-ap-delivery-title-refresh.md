# Delivery action_text refreshes every sync; the field becomes sync-owned in ORiON

**Date:** 2026-09-24
**Repos:** `hlc-scripts` (`sync_ap.py`); `orion-pll`
(`lib/sync-owned-fields.ts`, `components/action-item-modal.tsx`,
`knowledge/help/delivery-module.md`). DB: ORiON `czdkctjbejnwuopigxta`
(`action_items`).
**Origin:** Jen Wright, ORiON feedback `ff35f47f` — AP-189-2's title in
Delivery didn't match the AP Tracker. SAM COS action item `5330c69a`, bug
`9b8caf18`.
**Governing, not relitigated:** `2026-08-20-sync-integrity-trio.md`
(`SYNC_MIRRORED_FIELDS`, `ap_pending_update` flag/log discipline),
`2026-09-08-ap-pending-clear-and-direction.md` (per-field settle rule),
`2026-09-14-sync-row-edit-gate.md` (the sync-owned-mandatory-field pattern
this build reused).

---

## What was wrong

`build_pc_diff` (P&C, `sync_ap.py`) refreshed `pc_projects.title` from the
tracker's Improvement value on every run, unconditionally. `build_delivery_diff`
(Delivery) only ever set `action_items.action_text` on insert — the update
path never touched it. Diagnosis 2026-09-24 against live ORiON: 22 of 279
`ap_import` rows had `action_text != ap_tracker.improvement` on the same
`smartsheet_row_id`, across AP-189, AP-0785, AP-0733 and AP-0956; `pc_projects`
had 0 mismatches. Worst case: the whole AP-189-4 branch read "Control Server
Thin Client" in ORiON while the tracker had already renamed it to "Cyber
Security" — the sync caught those rows mid-rename and then never revisited
the title.

## T2 — field table (insert vs. update, both modules)

| Field | Delivery insert | Delivery update (before) | Delivery update (after) | P&C insert | P&C update |
|---|---|---|---|---|---|
| title/`action_text` | write | — | refresh (unconditional, no blank guard) | write | refresh (unconditional) |
| owner_id | write | refresh | refresh | write | refresh |
| status | write | refresh | refresh | write | refresh |
| due/start date | write | refresh (blank-guarded) | refresh (blank-guarded) | write | refresh (blank-guarded, bumps `target_date_moves`) |
| category | write | — | — | write | refresh (blank-guarded) |
| notes/description | write | — | — | write | refresh |
| no_report_out | write | refresh (unguarded) | refresh (unguarded) | write | refresh |

`build_pc_diff`'s title refresh carries no guard beyond the plain
inequality check — same tier as its other content fields, no
`ap_pending_update`/`orion_dirty` gate specific to title. `build_delivery_diff`'s
new `action_text` line matches that exactly (`sync_ap.py:1245-1246`).

## T3(a) — the collision, and Jim's ruling

Before this build, ORiON let a user edit a Delivery item's text on an
`ap_import` row with zero protection:

- `lib/sync-owned-fields.ts` (orion-pll, pre-fix) listed only
  `xyleme_import` in `SYNC_OWNED_MANDATORY_FIELDS`; `ap_import` had no
  entry, so `syncOwnedMandatoryFields("ap_import")` returned `[]`.
- `components/action-item-modal.tsx`'s Action Item textarea had no
  `disabled`/`readOnly` binding — editable for every source.
- `app/(protected)/delivery/item-actions.ts`'s `saveActionItem` wrote the
  client's `action_text` straight through on `.update(payload)` since
  `syncOwned` was empty for `ap_import`.
- `lib/ap-pending.ts`'s `SYNC_MIRRORED_FIELDS.delivery = ["status",
  "due_date", "start_date"]` never included `action_text` — an ORiON-side
  title edit raised no `ap_pending_update` flag and logged no
  `ap_change_log` row. Invisible to the whole reconciliation mechanism.

Shipping the refresh as briefed (mirroring P&C's unconditional title logic)
would have meant the next sync silently clobbered any such edit, forever,
with no trace.

**Jim's ruling:** the tracker wins for `action_text` on `ap_import` rows,
and the field becomes fully sync-owned — not extended into
`SYNC_MIRRORED_FIELDS.delivery` the way P&C's title is (P&C's title stays
editable in its own modal and protected via the pending flag; that is a
different, existing design choice, not one this build should mirror or
"fix" toward). Instead: grey the field in the Edit modal on `ap_import`
rows and have the server refuse the write, the same pattern already
shipped 2026-09-14 for the Xyleme dates (`2026-09-14-sync-row-edit-gate.md`).

**What shipped in orion-pll:**
- `lib/sync-owned-fields.ts`: `ap_import: ["action_text"]` added to
  `SYNC_OWNED_MANDATORY_FIELDS`. This single map entry does three things at
  once (the existing mechanism, unchanged): skips `action_text` from the
  missing-field gate on edit, makes `saveActionItem`'s write-back loop
  (`for (const f of syncOwned) payload[f] = item[f] ?? null`) silently
  discard a client-submitted `action_text` and write the row's own current
  value back instead, and (via the modal's use of the same map) marks the
  field for read-only rendering.
- `components/action-item-modal.tsx`: the Action Item textarea renders as a
  greyed div (`isApItem`, same `--gv-bg`/`--text-muted` treatment as the
  Xyleme date fields) with an italic hint ("Synced from the AP Tracker.
  Edit there — Smartsheet.") when `item.source === "ap_import"`.
- No change needed in `item-actions.ts` itself — the write-back refusal
  falls out of the existing `syncOwned` mechanism once the map entry
  exists; that is what "no hand-editable field" already meant for Xyleme.
- `knowledge/help/delivery-module.md`: new paragraph after "Required
  fields" documenting the read-only AP action-item text, mirroring the
  existing Xyleme-date paragraph's wording and pointing owners at Jen/
  Smartsheet for a rename.

## T3(a) follow-up — P&C has the identical UI gap, deliberately not fixed here

`components/pc-project-form.tsx`'s title input (`lib/491`) has no
`disabled`/`readOnly` binding either — a TPM/PLL *can* type over a P&C
project's title on an `ap_synced` row in the edit modal, exactly like
Delivery before this fix. The difference: P&C's `title` **is** in
`SYNC_MIRRORED_FIELDS.pc` (`lib/ap-pending.ts`), so that edit raises
`ap_pending_update`, logs to `ap_change_log`, and reconciles through the
2026-09-08 per-field settle rule — a real, working protection, just not a
UI gate. Reported per the brief's instruction; not touched. Whether P&C's
title should also become read-only/sync-owned like Delivery's is Jim's
call, not decided here.

## T3(b) — vault, cleared

`vault_synced` is set `True` at `ap_import` insert
(`build_delivery_insert`, `sync_ap.py:1222`) and never touched by
`build_delivery_diff` — adding `action_text` to that diff doesn't change
that. `export_to_vault` (`samcos/scripts/sync_vault.py`) only picks up
rows where `vault_synced = False`; no DB trigger on `action_items` sets it
(checked `information_schema.triggers` on `czdkctjbejnwuopigxta`: only
`ap_date_ack_guard`, `clear_escalation_needed_on_close`,
`action_items_guard_owner_change`, `update_last_updated`). No path from
this fix to a duplicate vault entry.

## A dormant interaction with the per-field pending-settle rule

`plan_module_projection`'s legacy/unlogged whole-row fallback (`sync_ap.py`,
"A flagged row with NO episode entries ... keeps the pre-2026-09-08
whole-row rule: content diff empty -> clear, non-empty -> protect")
computes `content` from `build_delivery_diff`, which now includes
`action_text` whenever it differs. For a Delivery row that is (a) flagged
`ap_pending_update` with zero `ap_change_log` entries since
`ap_pending_since` (a legacy/unlogged flag) AND (b) has a stale title, this
would keep the row's *entire* update blocked — not just the title — until
something makes the flag clear, because `content` is no longer empty for a
reason unrelated to whatever raised the flag. Checked against live data
2026-09-24: exactly one Delivery row is currently `ap_pending_update = true`
(`AP-0732`), it has 2 logged episode entries (not the legacy/unlogged
case), and its title is not stale. Zero live rows hit this interaction
today. Flagged here, not fixed — the legacy whole-row fallback predates
this build and this is a pre-existing shape of that fallback, not something
introduced by adding one more field to the content diff.

## Verification (T5 dry run, branch `fix/ap-delivery-title-refresh`)

Baseline (`czdkctjbejnwuopigxta`, captured before dispatch): 22 `ap_import`
rows with `action_text != ap_tracker.improvement`, same AP-numbers as
diagnosis (AP-189, AP-0785, AP-0733, AP-0956 families); `284` `ap_import`
rows total (up from 279 at diagnosis time — ordinary sync growth in the
intervening hours), notes fingerprint `md5` over all rows'
`(id, md5(notes))` = `f07e788008c2df53f2cfeb2ac59b335b`.

Dry run (GitHub Actions run `35997866122`, `workflow_dispatch` with
`dry_run=true`): `Delivery — would insert: 0, would update: 22, would
close: 0, would clear pending flag: 0`; `P&C — would insert: 0, would
update: 0`. All 22 planned Delivery updates are `action_text`-only, exact
same AP-number set as the baseline. Zero planned writes touch `notes`.
Zero errors in the run log.

Full gate evidence (all 10 criteria, live-run proof) is in the session's
build report, not duplicated here.

## Rejected / out of scope

- Extending `SYNC_MIRRORED_FIELDS.delivery` to include `action_text` so it
  behaves like P&C's title (protected-but-editable) — Jim's ruling was
  tracker-wins/sync-owned instead; P&C's own asymmetric exposure (above) is
  reported, not resolved, by this build.
- Fixing P&C's title-editability gap to match Delivery's new read-only
  treatment — same reasoning; a UI-parity decision for Jim, not implied by
  this bug.
- `action_items.notes` / description drift — out of scope per the brief;
  `notes` is the append-only human log (decision 2026-08-21 notes-log
  routing) and was never touched by this build (T5 baseline fingerprint
  proof).
- A separate backfill script — the next sync heals all 22 rows on its own.

## Cross-links

Sync-owned mandatory fields pattern: `2026-09-14-sync-row-edit-gate.md`.
Pending flag / mirrored-fields discipline:
`2026-08-20-sync-integrity-trio.md`. Per-field settle rule:
`2026-09-08-ap-pending-clear-and-direction.md`.
