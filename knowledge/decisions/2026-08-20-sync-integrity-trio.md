# Sync-integrity trio: flag-without-reason, dedup break on reassignment, orphan detection

**Repos:** hlc-scripts (`sync_ap.py`, `send_ap_pending_digest.py`), samcos
(`scripts/sync_vault.py`), orion-pll (`lib/ap-pending.ts`,
`app/(protected)/delivery/item-actions.ts`,
`app/(protected)/pc/projects/project-edit-actions.ts`, help docs)
**Decided/shipped on:** 2026-08-20
**Action item:** `86fd0c07` · **Bugs:** `17b1a835`, `545f0c88`, `c4494694`
**Migration:** `sync_integrity_vault_key_and_ap_orphaned` on ORiON
(`czdkctjbejnwuopigxta`), recorded in orion-pll
`docs/migrations/2026-08-20_vault_key_ap_orphaned.sql`
**Commits:** orion-pll `3091439`, samcos `18a2b4b`, hlc-scripts `87f8e0c`
(code) + this doc's commit

Three separately-filed bugs, one root problem: the sync layer between
Smartsheet, the vault, and ORiON handled the happy path and mishandled
every edge transition — a row that changed owner, a row that was deleted,
and a flag set by a writer with nothing to say about why.

## 1. The ap_pending_update writer inventory (bug 17b1a835)

Writers that SET the flag true — all three in orion-pll, all three shared
the same defect shape before this build:

| Writer | Raised flag | Wrote ap_change_log |
|---|---|---|
| `saveActionItem` (Delivery edit modal) | every save of an `ap_import` row | only due-date move / →Deferred (user reason) / first-date-set (system) |
| `quickStatus` (Delivery) | every status click on an `ap_import` row | only →Deferred |
| `updateProject` (P&C edit modal) | every save of an `ap_synced` row | only target-end-date move / →on_hold / first-date-set |

`applyPendingFlag` fired unconditionally; `diffForReasonCapture` (the only
path into `ap_change_log`) inspected exactly the date field and the
Deferred-equivalent status. Every other field change produced a flagged
row Jen's digest rendered as "not yet captured".

Non-writers, confirmed: `sync_ap.py` only clears the flag (and inserts
with it false); `sync_vault.py` and samcos contain zero references; the
legacy `orion` repo has no pending mechanism at all (its P&C edit paths
write mirrored fields with no flag — known gap on a dead surface, logged
in orion-pll's 2026-08-20 M2 decision doc). Delivery's `reactivateItem`
changes status *without* raising the flag — the inverse gap, now bug
`6cc5e30c`, explicitly out of scope for this build.

**Tamara's row (`a1563890`, AP-0379-2), the live instance:** one
`updateProject` save at 2026-08-19 20:48:24 set `ap_pending_since` (JS
clock, .59) and trigger-stamped `updated_at` (.611) 21 ms apart. The
save's only mirrored change was `description` NULL → text (proven by
diffing the live row against the live Smartsheet row: every other
mirrored field matched, and description is unguarded/mirrored so an
earlier divergence would have been overwritten by the 30-minute sync
while the row was unflagged; the sheet row was last modified 2026-08-10).
Description is not a reason-gated field → zero log rows → Jen's
unexplainable notice.

## 2. The fix: flag ⇔ log, structurally (Jim's rulings inline)

- **Raise only on sync-mirrored change.** New `diffSyncMirroredFields` in
  `lib/ap-pending.ts` diffs exactly the fields `sync_ap.py` mirrors:
  Delivery `{status, due_date, start_date}`, P&C `{title, description,
  status, category, start_date, target_end_date, owner_id}`. A save
  touching none of these (notes, next_step, links, priority) raises
  nothing — such flags never had a divergence behind them and were
  self-cleared by the next sync run as "caught up"; pure digest noise.
- **Delivery excludes `priority`** (Jim): sync force-resets Tier 2 and
  Smartsheet has no tier column, so a priority-raised flag could never
  clear — an unclearable flag in Jen's queue is worse than none. The
  underlying silent priority overwrite is logged as its own bug, not
  fixed here.
- **Every raising change writes a log row**: user reason where the
  existing gate captured one (that path is byte-unchanged), otherwise
  `SYSTEM_EDIT_REASON` = "Edited in ORiON (no reason required for this
  field)" — following the `FIRST_VALUE_SET_REASON` precedent. The honest
  content is the field/old/new the digest already renders. P&C owner
  changes log names, not uuids (Jen is the only reader).
- **Sequenced, not an RPC** (Jim): no new SECURITY DEFINER surface —
  `ap_change_log` is service-role-write-only by design and the 2026-08-11
  lesson covers a DEFINER function whose grant drifted open. Order is
  log-first, flag-after: a failure strands a log row with no flag
  (harmless) rather than a flag with no log row (the bug). Log/flag
  write failures surface as save errors.
- **`ap_pending_since` = the log's DB-assigned `changed_at`** — no
  app/DB clock skew in the digest's `changed_at >= ap_pending_since`
  match. Verified exactly equal in every gate row.
- **Tamara's row: fix-forward, untouched** (decision, not accident).
  Backfilling `ap_change_log` is out of scope (append-only, honest
  history). The flag is *correct* — the description divergence is real —
  so the resolution is Jim's one-time note to Jen (drafted and approved
  2026-08-20): she mirrors the description into the tracker's Description
  cell and the next sync run clears the flag through the normal
  caught-up path. Verified untouched at close: flagged since 2026-08-19
  20:48:24.59, zero log rows, digest renders the placeholder.

## 3. The vault dedup key (bug 545f0c88)

**Chosen: `vault_key` = `created_date|owner_raw|action_text`** — the
entry's own immutable header identity, exactly as written in
open-actions.md, stamped on the ORiON row at import and matched on every
later run. Stored in `action_items.vault_key` with a partial UNIQUE
index (`action_items_vault_key_uniq`) so mass-duplication is a
constraint violation, not a silent outcome.

Rejected alternatives, and why:
- **`action_text` alone** — disproven by live data before the build: the
  green-card item exists as four legitimate rows under four owners, all
  created 2026-08-05. Collapsing them loses three rows.
- **`(created_date, action_text)`** — same four-row counterexample.
- **Resolved owner name in the key** — alias/user-table resolution can
  drift (an `OWNER_ALIASES` edit would silently re-key every "Jim" entry
  and mass-duplicate); the file's raw bytes cannot. Raw owner text
  ("Jim" stays "Jim") is the stable choice.
- **Status in the key** — the one header field with change pressure;
  including it re-duplicates on edit.
- **Rewriting the vault owner on ORiON reassignment** — depends on the
  vault file being editable from the app; the bug notes ruled that out.

**Matching:** vault_key first; keyless rows (pre-backfill or old-code
deploy-window inserts) fall back to the legacy `(owner_id, action_text)`
match once and are stamped on the hit — a row is legacy-matched at most
one run before it owns its key. This fallback is deliberately kept (a
protective deviation reported to Jim in the build report): it closes the
race window between the backfill and the code deploy without
reintroducing the mutable-key path, because it only ever consults rows
with `vault_key IS NULL`. Duplicate headers in the file are skipped with
a warning.

**Backfill (executed 2026-08-20, before the code push):** parsed the live
vault file with the REAL modified parser (316 entries, 316 unique keys),
bulk-matched keyless `vault_import` rows on the legacy key (315 stamped;
zero (owner, text) collisions verified first), plus one manual
adjudication per Jim's ruling: the key for the reassigned "Send Philip
the combustion removal storyboard…" entry was stamped on `9478948e` (the
live row under Linda), NOT on its closed duplicate `56990248`, which
stays keyless and closed — its fate is Jim's, not this build's. Result:
316/331 `vault_import` rows keyed; the 15 keyless remainder (the closed
duplicate, 12 old 2026-06-12 fixture rows, and 2 rows whose file entries
were later reworded — "HA outage"→"Double Tree outage", "Command
board"→"Kanban board") have no current vault entry and can never match
or duplicate.

## 4. Orphan detection flags, never deletes (bug c4494694)

Jim's ruling, implemented literally: the only verbs are flag and unflag.
An orphaned row carries notes, status history, and `ap_change_log`
references a deletion would destroy, and the source system being wrong is
at least as likely as the row being wrong.

- `sync_ap.py` diffs existing `ap_import`/`ap_synced` rows against the
  FULL child fetch each run (active AND inactive — absence means deleted
  from the tracker, distinct from Complete/Cancelled/On Hold, which stay
  in the sheet and take the status-only close path). Absent →
  `ap_orphaned = true` + `ap_orphaned_since`; reappearing → cleared
  (covers a restored row and the Is-Child-flag restructure false
  positive). Detection keys on the child row's own AP number — a deleted
  child whose parent still exists is an orphan.
- Zero-AP-number guard: a fetch whose child rows all lost their AP#
  reads as sheet damage, never mass-flags.
- Surfaces as the "Removed from the tracker — needs review" section of
  Jen's digest (`send_ap_pending_digest.py`), which now sends when
  pending rows OR orphans exist; subject gains "+ N removed from
  tracker". Shipped after the Task 2 fix so the first thing Jen sees is
  not a new section inside a template with a known defect.

## 5. Verification gate evidence (summary — full output in the build report)

- **Group A** — non-mirrored edit (next_step) on an ap_synced fixture:
  saved, no flag, zero log rows (also proves the deploy — old code would
  have flagged). Description-only edit (Tamara's exact shape): flag +
  one log row, system reason, `since == changed_at`. quickStatus
  Open→In Progress: flag + `status: Open -> In Progress` log row.
  Due-date move: rejected without a reason with the unchanged message,
  saved with one, user reason logged verbatim. Digest dry-run
  (dispatched, run 32393004445): every flagged row rendered with its
  reason; Tamara's row rendered the placeholder. All driven through the
  real deployed UI with a disposable fixture director account (created
  via SQL, session minted via the auth token API, deleted after).
- **Group B** — behavioral harness ran the REAL `import_from_vault`
  (samcos `scripts/verification/vault_dedup_harness_2026-08-20.py`):
  reassigned row not re-inserted and untouched; four green-card rows
  stay four; new entry imports with key + due date; unchanged entry
  no-ops; resurrection guard holds; keyless row stamped not re-inserted;
  and the PRE-fix code (git show HEAD~) reproduced the duplicate
  (inserted=2 vs 1) — the gate was seen to fail on the old code.
  Post-backfill dispatched dry-run against production: would insert 0,
  update 0, stamp 0 (run 32391617819). Live run at the fix SHA:
  Import 0 new, 0 status updates (run 32391853611).
- **Group C** — three disposable fixtures (standalone fake AP, child of
  live parent AP-0379, P&C fake AP): all flagged by the live dispatched
  sync run (run 32391858711, "orphans flagged: 3"), statuses and row
  counts untouched, and the orphan set contained ONLY the fixtures —
  zero false positives across every real ap_import/ap_synced row.
- **Group D** — no email sent (digest runs were dry-run only; nothing
  else in scope sends); both cron schedules and the weekday gate
  byte-unchanged (`sync-ap.yml` 13,43 * * * *; `sync-vault.yml`
  3,33 * * * *; `ap-pending-digest.yml` 0 12 * * 1-5); pushes confirmed
  HEAD == @{u} in all three repos; migration confirmed via
  `information_schema.columns` + `pg_indexes`.

## Cross-links

Flag-mechanism history: orion-pll
`knowledge/decisions/2026-07-31-ap-lifecycle-step2-design.md`,
`2026-08-20-m2-launchpad-only-and-first-date-reason-guard.md`. Vault
write path: samcos `2026-08-07-sync-vault-phasec-writepath.md`.
Won't-fix neighbor: bug `ac29261e` (blanked-cell clears pending flag) —
closed 2026-08-20, deliberately not re-opened by this build. New bug
filed from this build's discovery: `6cc5e30c` (reactivateItem raises no
flag).
