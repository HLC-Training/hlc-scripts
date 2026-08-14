# Lessons — hlc-scripts

Append-only. Newest at the bottom.

## Exit 0 on a failed fetch is a lie the whole system believes

`sync_ap.py` exits 0 and writes its success heartbeat even when the Smartsheet
fetch fails. A run during a Smartsheet outage produces a green Actions run, a
fresh heartbeat, and zero synced rows. Observed live 2026-07-30: run
30549527981 concluded "success", produced no script output, wrote nothing.

Any sync script must exit non-zero when its source fetch fails. A green run
that did nothing is worse than a red one, because nobody investigates green.

## A heartbeat measures liveness, not staleness

A heartbeat proves a run started. It never proves a run was due and did not
happen, and it never proves the run accomplished anything. The AP sync went
31 hours without firing (2026-07-29 18:13Z to 2026-07-31 01:08Z) while the
health dashboard stayed green throughout.

Staleness needs an expected-interval check: a heartbeat older than roughly 3x
its nominal cron interval should alert. Liveness checks cannot catch absence.

## GitHub Actions cron contention is per-workflow, not per-account

AP sync on `5,35 * * * *` fired roughly one slot in seven, with 3-4 hour gaps.
`sync-xyleme` held ~17 minutes on an identical cron, in the same repo, on the
same account, through the entire AP outage.

So this is contention on the specific slot, not a rate limit. Minutes near
`:00` and `:30` are the most contended. Do not assume a cron expression
delivers its nominal cadence; measure the observed gaps before designing
anything around freshness.

## Writes to open-actions.md must use the marker, and the file looks fine either way

Vault writes to `open-actions.md` require `operation: insert_before_marker`
with `match_string: # ORION Sync Log`. A plain append writes below the marker,
where `sync_vault.py` skips it. The entry is in the file, the file is valid
markdown, and the content never reaches ORiON.

Entry headers require the leading `## ` prefix:
`## YYYY-MM-DD | Owner | Action | Status: Open`, exactly four pipe fields,
with Category and Tier as `>` metadata lines below. Without the `## ` the
Phase C validator rejects the queue row, and an entry written outside the
queue is silently skipped by sync.

Both failures are invisible on inspection. That is why they are here.

## Postgres and Supabase gotchas

Newlines in SQL content need PostgreSQL escape syntax: `E'line one\nline two'`.
A plain string with a literal newline will not do what you expect.

GE email lookups use `.ilike()`, never `.eq()`. GE Vernova addresses appear in
mixed case and an exact match silently returns nothing.

Multi-statement queries through Supabase MCP parse unreliably. Split them.

`vault_write_queue` inserts go one row per file at a time. Concurrent inserts
fire webhooks that clobber each other. Verify status before the next insert.

## 2026-08-03 — Generic Supabase key names are a repo-wide pattern, not a one-script bug

Fixed `sync_ap.py`'s Smartsheet-fetch-failure exit code (`sys.exit(1)`
instead of a silent `return`, closing the "exit 0 on a failed fetch" lesson
above) and renamed its `SUPABASE_SERVICE_KEY` read to
`ORION_SUPABASE_SERVICE_KEY` (bug 306cea89) — the generic name risked
resolving to the wrong project's key if ever set at Windows User scope on
HERMES across the three-project Supabase estate (this ORiON project, SAM
COS, GreenThumb). `sync-ap.yml`'s secret was already named
`ORION_SUPABASE_SERVICE_KEY` at the repo level; only the env var mapping
inside the workflow needed to stop renaming it on the way in.

While checking for a naming convention to match, found `sync_xyleme.py` and
`send_ap_pending_digest.py` read the exact same generic
`SUPABASE_SERVICE_KEY` name for this same ORiON project — same collision
risk, not fixed this session (out of the scoped bug). Worth its own
follow-up rather than assuming a fix in one script covers the pattern.

Also added a started-class heartbeat (`health:github:orion_ap_sync:last_run`,
written unconditionally before the Smartsheet fetch) alongside the existing
completion heartbeat (`health:github:orion_ap_sync`, unchanged, written only
at the end of a fully successful run) — mirrors the email-scan routine's
`last_run`/`last_success` split so "started" and "succeeded" stay two
different signals here too.

## 2026-08-05 — last_updated is not an insert timestamp, and zoneinfo needs tzdata on Windows

Two from the PLL digest build (`send_pll_digest.py`):

`action_items.last_updated` defaults to `now()` at insert but a
`BEFORE UPDATE` trigger bumps it on every later edit — live vault rows
showed lags up to 55 days between `created_date` and `last_updated`. Any
"new since X" logic keyed on `last_updated` silently reports old,
recently-edited rows as new. This table has NO immutable insert
timestamp; the digest uses `created_date` (the vault header date) plus a
reported-ids exclusion list instead. Check for an update trigger before
trusting any timestamp column to mean "created."

Python's `zoneinfo` raises `ZoneInfoNotFoundError` on Windows unless the
`tzdata` pip package is installed — there is no OS tz database to fall
back on. Ubuntu runners mask this. Any script using `ZoneInfo(...)` must
list `tzdata` in its install line or it works in CI and dies on HERMES/
ARGUS.

## 2026-08-05 — a timestamptz insert column beats a bare-date one, and check for triggers before assuming either way

From the TPM digest build (`send_tpm_digest.py`, action item c5cf4442):
`pc_projects.created_at` is a `timestamptz` with `default now()`, and
`pg_trigger` returned zero rows for the whole table — no DB-level bump
on anything, unlike `action_items.last_updated`'s `BEFORE UPDATE`
trigger (previous entry, above). Confirmed in `sync_ap.py` too: its P&C
update path never includes `created_at` in the fields it writes on an
existing row. Given both a clean column and no trigger, a full
timestamp watermark comparison (`created_at > watermark_timestamp`) is
possible and is strictly better than the PLL digest's
`created_date >= watermark_date` — the date-truncated comparison there
was forced by `action_items.created_date` being a bare `date` column,
and it required a whole reported-ids exclusion list to avoid
same-day double-counting. A timestamptz insert column doesn't need
that workaround at all. Lesson either way: before designing a "new
since X" filter, query `pg_trigger` for the target table AND check
whether the candidate column is a date or a timestamptz — both change
what watermark comparison is even safe to write.

Also: when a second job needs a constant already hardcoded in a first
job (here, the 2026 GE Vernova holiday list in `send_pll_digest.py`),
factor it out to a shared module (`ge_holidays.py`) rather than
copy-pasting it — a second inline copy is a second place that silently
drifts when 2027's calendar refresh only touches one of them.

## 2026-08-06 — A newly merged schedule trigger can miss its first slot with no error

The PLL digest's cron (`7 11 * * 1-5`) never fired for its first eligible
weekday after the workflow was merged. No error, nothing in Actions history
except the manual dispatch from the day before. The script was fine — this
was GitHub's scheduler, not the code.

Two takeaways: don't trust a brand-new cron schedule to prove itself on its
first slot, and where the downstream consumer doesn't care about exact
timing (a morning digest nobody reads before 7am), schedule it hours earlier
than the deadline rather than right up against it. Slack absorbs a missed or
delayed trigger before it becomes a Jim-visible problem.

## 2026-08-10 — A secondary capture bolted onto a primary sync needs three separate failure behaviors, decided up front

From the AP title capture build (sync_ap.py → ap_titles): adding a
side-channel write to an existing production sync forces a decision that
"log it and move on" quietly dodges. The shape that held up:

1. Sequence the secondary WRITE after every primary write phase, even if
   the data was fetched/diffed earlier — then a secondary failure can
   never cost a primary row.
2. Exit non-zero at the very END on secondary failure. "Exit 0 on a
   failed fetch is a lie" (top of this file) applies to partial success
   too — a green run that silently dropped half its job invites nobody
   to look. But don't exit early: red exit code AND completed primary
   work are not in tension if the exit is last.
3. Let the completion heartbeat still fire — it's the PRIMARY sync's
   health signal and the primary succeeded. Put the secondary's state
   into the heartbeat notes (titles_captured/written/failed) so the two
   signals stay distinguishable instead of muddled into one.

Also: you can't force a production Supabase table write to fail on demand
(service role bypasses RLS; breaking prod with a bad CHECK to watch CI go
red is vandalism). The honest failure-path proof is running the real
main() with a stubbed DB layer where ONLY the new table's write raises —
assert the primary writes landed first and the exit code went non-zero.
Prints, not vibes.

## 2026-08-11 — A narrow view means a two-step fetch, not a wider select

From the Approaching Stale digest build (send_pll_digest.py /
send_tpm_digest.py → action_items_staleness / pc_projects_staleness,
action item bde783ad): `supabase-py`'s `.table(name)` works identically
for a view as for a table — no special call, no `.rpc()`, filters and
`.select()` work the same way. That part held no surprise.

The wrinkle: these views expose only `(id, owner_id, staleness)` — they
don't re-expose the base table's other columns, and there's no declared
FK relationship PostgREST could use to embed them even if asked. Trying
`.select('id, owner_id, action_text, ...')` against the view itself just
errors on the unknown columns. The fix is the two-step fetch this repo's
digests already do elsewhere for unrelated reasons (fetch_plls →
fetch_items): query the view for matching ids filtered on the state you
want, then a second `.in_('id', ids)` query against the real source
table for every detail field to render. Don't assume a "thin" view can
carry a wider select just because a table with the same name-ish shape
usually can.

Also worth the explicit check next time a digest section is built on a
shared threshold/classification view: read what triggers the
classification in the app that owns it (here, orion-pll's Approaching
Stale badge decision doc) before assuming the digest's own established
status filter (here, `ACTIVE_STATUSES`) should apply on top. The view's
own terminal-status set is the intended filter — re-narrowing it in
Python risks the digest silently disagreeing with the UI badge for the
same row.

## 2026-08-12 — A bug found while investigating an adjacent one is a finding, not a footnote

The 8/11 category investigation named the exact 6 rows that were Complete in
Smartsheet but active in ORiON, described the inactive-row path that would
never touch them, and used both facts as *evidence for its own conclusion* —
then closed without asking whether that behavior was itself a defect. It
was. Michele hit it the next day, twice, through portal_feedback, and it
became a trust-critical fix on a deadline instead of a quiet one-line map
extension a day earlier.

The lesson isn't "investigate everything" — it's that when a session writes
a sentence of the form "the sync only ever revisits a Complete row to
settle a pending flag" as supporting evidence, that sentence is a claim
about intended behavior being asserted from observed behavior. File it as a
question (bug row, action item, anything queryable) even when it's not the
thing being investigated. The 8/11 session did file the category clobber it
tripped over; the status gap got described in prose and lost. The
difference between the two outcomes was one INSERT.

Two smaller ones from the same session: a "status transition guard" trigger
had appeared on pc_projects since the 8/05 zero-triggers check — re-query
pg_trigger before any new write path to a table, because a BEFORE UPDATE
guard can veto a sync write silently (these turned out to gate on
auth.uid(), so service-role writes pass); and when the only credentials
live in GitHub secrets, `gh workflow run --ref <branch>` is the honest way
to run unmerged sync code against production once — main's cron never sees
the branch, and main still gets exactly one reviewed commit.

## 2026-08-12 — Enumerate an exit-path class from the code, and rule null-guards per field

Two from the sync-family cleanup (exit-0 hardening + P&C date guards,
action item 83271599):

Bug 2ae04371 said sync_ap.py main() had FIVE bare-return exit-0 paths; the
code had SIX — the report never counted the client-init failure separately
from the five table loads. When converting a class of early exits, grep
the function and enumerate from the code, not from the bug report's list;
a miscount here means one path keeps lying green after the "fix" ships.

"Never write a computed None over a stored value" is not a blanket rule —
it's a per-field ruling about what a blank source cell MEANS. Dates:
accident, guard them (nobody deliberately clears a date, and target_end_date's
None-write also phantom-incremented target_date_moves). Category: accident,
guarded 8/11 (sparse source). Description: intent, MIRROR it — free text
the owner cleared should clear downstream, and guarding it pins stale text
forever. Ask "is a blank here a decision or a gap?" field by field before
reaching for the guard; the same question is still open for the
Delivery-side due_date/start_date diff, deliberately unfixed.

## 2026-08-12 (round 2) — A guard on a field that also feeds divergence logic changes two behaviors

From the second sync-family pass (sync_xyleme exit-0 + Delivery date
guards, action item dac56281, bugs bcd87bc4 / 74ebd314).

The Delivery `due_date`/`start_date` null-guard looked like a mechanical
repeat of the P&C one. It wasn't, because the `fields` dict it builds has
two consumers, not one: the `if fields:` write path, AND the
`ap_pending_update` caught-up/divergence check right below it, which
decides whether a PLL's protected change has been reconciled and whether
to escalate to a human past ESCALATION_DAYS. Guarding the dates therefore
changed what counts as divergence — a blanked Smartsheet cell used to land
in `fields` and read as "still diverging"; it now leaves `fields` empty and
reads as "Smartsheet caught up."

That reading is correct, and it killed a real prior misbehavior: a blank
cell could never catch up, so the flag stayed set indefinitely and
eventually escalated `due_date: '2026-09-30' -> None` — a phantom
divergence about a change nobody made. The accepted residual: clearing the
flag on a blank drops the PLL's protection without Smartsheet actually
matching, so a later real date change overwrites the PLL edit instead of
being held back. Narrower than the old failure, so it stands.

The lesson, distinct from the plain null-guard one above: before adding a
guard, find every reader of the structure you're guarding. A guard on a
value that only feeds a write is a one-behavior change; the same guard on
a value that also feeds divergence, reset, or escalation logic is a
two-behavior change — and the second behavior is invisible in the diff.
Verify both, and never edit the consumer to make the gate pass; the guard
is what changed, not the logic reading it.

Harness note: both proofs ran the REAL code, not a retyped copy — the
Delivery gate extracts sync_ap.py's line range verbatim, dedents it, and
execs it against synthetic rows, so the proof can't drift from what ships.
The xyleme stub was also run against `git show 2b5417a:sync_xyleme.py` as
a control: all four paths exit 0 there and 1 after the fix. A behavioral
gate you never saw fail hasn't told you anything yet.

## 2026-08-14 — Trust the generate_link response over the auth config you remember, and a new dispatch workflow needs main first

Three from the TPM onboarding build (send_tpm_onboarding.py, action
item 083cc7a5):

GoTrue silently REWRITES a redirect_to that fails the auth URL
allow-list — generate_link still returns 200 and a working-looking
action link whose redirect_to is now the Site URL fallback. The only
honest check is parsing the returned action_link and comparing
redirect_to to what was sent; the script does this and refuses a live
send on mismatch. Related: a dashboard-config fix deferred to a human
("Jim adds the allow-list entry") is not done until re-verified — the
2026-08-04 fix for exactly this never happened, and by 2026-08-14 even
the entries that DID exist then (vercel.app, localhost:3100) no longer
survived. Config drifts; probe it empirically before building on it.

A NEW workflow_dispatch workflow 404s on dispatch until its file exists
on the DEFAULT branch — gh resolves the workflow against main, and
pushing the file to a topic branch never registers it. The 8/12 lesson's
`gh workflow run --ref <branch>` pattern works only for workflows main
already has. Consequence: a brand-new manually-triggered workflow cannot
be test-run before its first main commit, so "1 commit including the
verification-evidence doc" is structurally impossible — code commit
first, docs commit after the verified run.

When the Management API and dashboard are both unreachable, advisor
ABSENCE is usable config evidence: get_advisors returning auth-level
lints (leaked-password-protection) but not auth_otp_long_expiry bounds
the email OTP expiry at <= 3600s without ever reading the setting. An
absence argument needs the presence of a sibling lint to prove the
category is being checked at all.

## reply_to lives on the Resend payload, not the recipient list

`send_tpm_onboarding.py`'s copy says "reply here or ping me directly," but
the Resend `.emails.send` call had no `reply_to` field, so a reply from a
TPM went to nowhere anyone reads — `from:` controls where mail is sent
FROM, not where a reply lands. Found and fixed 2026-08-14 (action item
`4ba69961`). `send_pll_digest.py` already has this as an optional
per-call parameter (`send_email(..., reply_to=None)`); onboarding only
ever needs one fixed reply-to, so it's a module-level constant instead —
same field, no per-call plumbing needed since there's only one caller
shape.
