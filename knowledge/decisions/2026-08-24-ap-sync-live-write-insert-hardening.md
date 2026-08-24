# AP sync live write: is_parent + pagination fixes landed; insert path hardened against date-poisoned batches

**Repos:** hlc-scripts (`sync_ap.py`, `.github/workflows/sync-ap.yml`)
**Decided/shipped on:** 2026-08-24
**Bugs:** `59cd7d7b` (is_parent child drop), `0644145e` (totalPages pagination) — both
resolved this session; the batch-poisoning defect below was found and fixed in the
same session, fix commit `98264b6`.
**Commits:** `bd91432` (is_parent), `504c205` (pagination), merge `f224c60`,
hardening `98264b6`, merge `13491bf`
**Runs:** dry-run baseline `32772272719`; live `32773634985` (45 of 70 — exposed the
third bug); live `32774923177` (remaining 25 — contract closed at exactly 70)

## What shipped

The two proven fixes merged to main and ran live: 70 new rows total, exactly the
dry-run prediction — 31 Delivery `action_items` (`ap_import` 27→58), 39 P&C
`pc_projects` (`ap_synced` 42→81), 29 `ap_titles` (72→101), zero effect on
pre-existing rows across both runs.

## The third bug, and the decisions it forced

The first live run inserted 6 of 31 Delivery rows. Root cause chain: Smartsheet
renders formula errors as literal cell text — `DATEONLY()` over a blank
`Current Start` yields `#INVALID DATA TYPE` in the "Start Date (date only)" helper
column the sync reads — and `parse_due_date`'s blind `[:10]` slice shipped
`"#INVALID D"` to Postgres, which rejected the entire 25-row batch it rode in
(supabase-py batch INSERT is one statement, all-or-nothing). The error was
swallowed by `except: log.error`, the run exited 0, and the heartbeat reported
healthy. The dry run could not have caught it: `--dry-run` returns before any
INSERT executes, so it validates row *selection*, never row *landing*.

Decisions, all in `98264b6`:

1. **Junk date means no date, not no row.** `parse_due_date` validates the slice
   as a real `YYYY-MM-DD`; anything else returns None and warns with the raw
   value. The four poisoned rows now sync with null dates instead of blocking
   themselves and 21 batch-mates.
2. **Per-row fallback on batch failure** (Delivery and P&C): a failed batch is
   retried row-by-row, each failure counted and named. One bad row's blast
   radius is itself.
3. **WIP check keys on rows that actually landed**, never a positional slice of
   the attempt list — under partial failure the slice flagged an owner
   (Sherif Khalifa) whose rows had all failed.
4. **Insert failures are loud**: summary tag, `insert_failures=N` in the
   heartbeat notes, and a non-zero exit after both heartbeats — the same
   discipline as the 2026-08-10 title-capture block, for the same exit-0-lie
   reason.
5. **The workflow always cats `sync_ap.log`** — the live failure had to be
   reconstructed from Supabase API logs because per-row detail never reached
   the Actions UI.

## Sheet-side residue (open, not blocking)

The junk source remains in the tracker: `AP-210-2`, `AP-210-2-1`, `AP-210-2-2`,
`AP-210-3`, `AP-211-3` have no `Current Start`/`Current Finish` (never set —
cell history is empty), so the DATEONLY helper columns error. Two more error
rows exist outside any synced family. The clean sheet fix is wrapping the two
helper column formulas in `IFERROR(..., "")` — attempted this session, blocked
by 403 (the API account lacks sheet-admin rights for column-formula changes).
Needs Jennifer or a sheet admin; with the code hardening it is cosmetic.
