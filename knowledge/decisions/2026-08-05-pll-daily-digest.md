# PLL daily digest — design decisions (2026-08-05)

Action item of record: SAM COS `37425f71`. Job: `send_pll_digest.py` +
`.github/workflows/pll-digest.yml`. State: `pll_digest_state` (ORiON
Supabase `czdkctjbejnwuopigxta`).

## Watermark design

`pll_digest_state` is **one row per successful run**, inserted only after
every send in that run completed (or all PLLs correctly no-op'd). The
watermark is the latest row's `sent_at`. A crashed or partly-failed run
inserts nothing, so its window is automatically re-covered next run —
failure can duplicate "new" items but never lose them. Monday's digest
covers the weekend because the window is "since last successful send,"
never "yesterday."

Test-mode runs DO advance the watermark (rows carry `test_mode` for
audit, but the watermark query doesn't filter on it). Rationale: each
test email is then an exact preview of what the live run would have sent
that morning. Consequence, accepted deliberately: at go-live the first
live email shows only items new since the last test run — items shown as
"new" only to Jim during the test window won't re-appear as "new" to
PLLs (they still appear under due-soon/past-due as applicable).

First run ever (empty table): 72-hour fallback window.

## Source filter — "added via vault sync"

`source = 'vault_import'` (stamped by `sync_vault.py` on every vault→
Supabase insert, confirmed in code) **AND** `created_date >= ` the
watermark's America/Chicago **date**, restricted to active statuses
(Open/In Progress).

Why `created_date` and not `last_updated`: `last_updated` is set to
insert-time by column default but a `BEFORE UPDATE` trigger
(`action_items_last_updated`) bumps it on **any** later edit — live data
shows lags up to 55 days on vault rows, so it cannot mean "inserted at."
`created_date` is the vault entry's own header date — the date the item
was logged in `open-actions.md` — which is exactly Jim's definition of
"added for the period." Known limitation, accepted: an entry transcribed
into the vault days later with a backdated header older than the
watermark won't appear as "new" (it still surfaces in the due-soon /
past-due sections).

Because `created_date` is a date compared against a timestamp watermark,
an item dated the same day as the last send could double-report. Killed
by an exclusion list: each state row's `summary.section1_ids` records
what section 1 reported; the next run excludes those ids. One run of
memory is sufficient — anything older is excluded by the date comparison
itself.

## Cron slot

`7 11 * * 1-5` — 11:07 UTC = 6:07 AM Chicago during CDT (5:07 CST).
Minute 7 because :00/:30 are the contended Actions cron slots on this
account (lessons.md: AP sync on :05/:35 fired ~1 in 7 with 3–4 h gaps).
Early-side error is fine — the goal is "in the inbox when they arrive."
The script re-checks weekday + holiday on the Chicago date, so DST skew,
delays, and duplicate firings all stay safe.

## Holidays

The 13 GE Vernova 2026 dates (FieldCore US Staff/RETX calendar,
sam-drop) are hardcoded in `HOLIDAYS`. **Refresh for 2027 before the
year turns** — a live calendar feed was deliberately not built.

## Roll-up, not cc

Jim gets one consolidated roll-up email per run (per-PLL counts +
sent/suppressed), not a cc on each of the 7 — seven cc's a morning is
noise that would train him to ignore the thing he's supposed to audit.

## Test-first rollout (lessons.md 2026-07-31)

Default mode is TEST: all digests go to Jim with a `[TEST — would go to
X]` subject prefix, plus the roll-up. Live requires the repo variable
`PLL_DIGEST_LIVE=true` (or `--live`), set only on Jim's explicit go
after reviewing the test bundle. The action item stays open until the 7
are receiving it and Jim has confirmed the first live run.

## Env naming

`ORION_SUPABASE_SERVICE_KEY`, never the generic name (lessons.md
2026-08-03 — collision risk across the three-project Supabase estate).

---

## Amendment 2026-08-06 — cron moved to 07:07 UTC

The first live run (06:07 CT / 11:07 UTC, 2026-08-06) never fired on
schedule — Actions showed no scheduled run, only the prior day's manual
dispatch. Jim ran it manually; the send itself worked cleanly, so the miss
was the schedule trigger, most likely GitHub's known quirk where a newly
merged `schedule:` block can miss its first eligible slot.

Cron changed from `7 11 * * 1-5` to `7 7 * * 1-5` (06:07 CT → 02:07 CT
during CDT, 01:07 CT during CST). Jim's reasoning: recipients read the
digest in the morning regardless of exact send time, so pushing it four
hours earlier trades away nothing and buys a wide buffer against exactly
this kind of delayed-or-dropped trigger before anyone notices. Same
tradeoff already accepted for the diary job's `0 7 * * *` cron
(`samcos/knowledge/decisions/2026-07-19-personal-vault-journal.md`).

Minute offset (`:07`) unchanged — still dodging the `:00`/`:30` contention
window from the cron-contention lesson.
