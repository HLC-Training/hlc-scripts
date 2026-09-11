# sync_xyleme.py — stop overwriting the notes log; module-count moves to a dedicated field

**Decided:** 2026-09-10 (Jim). **Shipped:** 2026-09-11.
**Repo:** hlc-scripts (sync), orion-pll (UI + help doc), ORiON Supabase (migration)
**Bug:** 8ffd6cb7 (SAM COS). **Action item:** 0df174e7 (SAM COS).
**Source:** ORiON portal_feedback 2b12bbd6 (Delivery, 2026-09-09).

## Root cause
sync_xyleme.py wrote an auto-generated module-count string into action_items.notes
on every source='xyleme_import' row every run. notes is contractually append-only
(orion-pll delivery-module help doc) and the help doc permits normal notes edits on
Xyleme rows, so any PLL-typed log line was clobbered on the next sync. Confirmed
live: all 6 xyleme_import rows held only the module string in notes, no dated
entries. The module string is not a fixed format — at least three shapes exist —
so guards must key off the `Modules:` prefix, not a literal template.

The 8/11 (owner_id) and 8/12 (date null-guard) hardening passes guarded
owner_id/due_date/start_date/status but never notes.

## Fix
Module-count moves out of notes into a dedicated `xyleme_progress text` column on
action_items (migration `add_xyleme_progress_to_action_items`, czdkctjbejnwuopigxta).
Sync writes the count there and diffs against it; the Delivery card sub-line and
the Edit modal's "Synced from Xyleme" notice read it there; notes becomes a field
the sync never writes on xyleme_import rows. `assert_notes_untouched()` raises if
any action_items write payload ever carries `notes` again, so a regression fails
loudly instead of silently wiping a log. One-time backfill copied the count into
the new field and cleared notes per-row only where notes matched
`^Modules: \d+/\d+ complete( \| [^|\n]+)*$` — all 6 rows matched; no human line
existed to preserve.

Gate proof (2026-09-11): a dated test line placed in notes on GT - Maintenance,
with xyleme_progress set to a stale sentinel, survived a live sync run while the
sentinel was overwritten with the real count. Test line removed afterwards.

Jim ruled the dedicated field over two alternatives (2026-09-10): not an appended
notes line (would spam the log every run), and not next_step (keeps its meaning).

## Scope held
Did not touch owner_id/status/date diff logic (already correct). Did not build
ORiON→Xyleme write-back (settled non-goal, 2026-08-31).
