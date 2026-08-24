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

## Raw REST calls to Supabase need their exact documented body shape, not SDK conventions

Bug 065c166e: `send_tpm_onboarding.py`'s raw HTTP POST to
`/auth/v1/admin/generate_link` nested `redirect_to` under `"options"`
following the Python SDK's pattern, but the REST endpoint expects
`redirect_to` as a flat top-level field. Supabase silently ignored the
malformed nesting and fell back to the Site URL for every generated link.

The official Python client's `.auth.admin.generate_link(...)` method
handles the translation to the REST shape; raw endpoints need the exact
documented body structure, and SDKs' nested-option conventions don't apply.
If using raw HTTP to a Supabase Admin API, read the exact shape from the
OpenAPI docs, not from an SDK example — and test the generated link's
redirect_to field (parsed from the returned action_link) to catch this
class of silent failure.

## 2026-08-14 — "Deployed" means the SHA the runner checked out, not the file on disk

The redirect_preserved=False that survived the 065c166e nesting fix was
neither a second payload bug nor a checker bug: the fix commit (79b87c4)
was sitting unpushed on local main, and every "post-fix" workflow_dispatch
test ran at origin/main's c4815fe — the pre-fix code. The confirmation
that the fix "was deployed" had been made by reading the local working
tree, which is not the thing GitHub Actions executes. Once pushed, the
very next test run logged redirect_preserved=True with zero code changes.

The check that settles this in one line: compare `gh run list`'s headSha
for the test run against `git log origin/main..main`. If the fix commit
appears in the second command's output, no run has ever executed it. For
any script whose only runtime is CI, "fixed locally" is indistinguishable
from "not fixed" — verify the run's SHA contains the fix before
interpreting its output as evidence about the fix.

Also ruled out along the way: the URL-encoding false-negative hypothesis
(that redirect_preserved() substring-matches an unencoded URL against an
encoded action_link). The checker uses urllib's parse_qs, which
percent-DECODES query values before the comparison, so an encoded
redirect_to compares equal — encoding cannot produce a false negative
there. Worth remembering the distinction anyway: a checker that string-
hunts `redirect_to=<url>` inside a raw link WOULD break on encoding;
parse-then-compare is the shape that survives.

## 2026-08-20 — A repo variable can drift true while every doc still says off; check gh, not the docstring

From the Mechanism 3 digest build (action item 7007b802). Every source —
the build brief, send_tpm_digest.py's own module docstring, orion-pll's
pll-digest.md help copy — agreed `TPM_DIGEST_LIVE` was off and this
digest "stays in TEST mode indefinitely." It wasn't: `gh variable list`
showed it flipped `true` on 2026-08-14, and `gh run list` confirmed every
scheduled run since (08-17 through 08-20) sent live to Michele. Nothing
that changed the flag ever touched the docstring or the help doc — a
live/test gate's *documented* state and its *actual* state can diverge
silently, with zero code-visible signal, the moment someone flips a GitHub
Actions repo variable without a corresponding doc update. Before trusting
any "this stays in test mode" comment near a live-send gate, run `gh
variable list` (or the equivalent for the actual switch) directly — don't
infer current state from a docstring, a help article, or a build brief,
all of which can lag the real flag by however long nobody rewrites them.

Separately: `lib/incomplete-rows.ts`'s own comment/label ("Action text")
had quietly drifted one word from Mechanism 1's real form validator
("Action item text") — a shared/duplicated definition can drift from its
own upstream source, not just from a reimplementation elsewhere. And a
decision doc's field-count table (3159216d: "five/six mandatory fields")
listed the full design intent, including a field (owner) that is
structurally never reachable as missing (NOT NULL / guaranteed-on-create)
— a design-intent list and a checkable-field list aren't automatically the
same thing; verify a fixture-count instruction against what the code can
actually produce before building to it.

Also: `findIncompleteDeliveryRows`/`findIncompletePcRows` (orion-pll,
`lib/incomplete-rows.ts`) call `createClient()` from `@/lib/supabase/
server`, which imports `cookies()` from `next/headers` — this throws
outside a real Next.js request scope, so it can't be driven from a plain
tsx script the way `lib/email-inbox.ts`'s functions can (those use
`createServiceClient` instead, no cookies involved — check which client
factory a function uses before assuming the authz-harness script pattern
applies). A Node `module.register()` hook to stub `next/headers` was tried
and abandoned: tsx's own module resolution for a script's *nested* TS
imports doesn't visibly yield to an externally `--import`-registered
loader, even though the loader's `resolve` hook does fire for the entry
file and Node's own dependency graph. The honest fix was a real request
context — a temporary, disposable API route calling the unmodified
functions, hit from a real authenticated browser session (a disposable
fixture director account, deleted after) — not a mock of the one thing
that was hard to fake.

## 2026-08-20 — A dedup key for a synced file must be the file's own identity, stamped once, never derived from mutable destination state

From the vault dedup fix (bug 545f0c88, decision
2026-08-20-sync-integrity-trio.md). sync_vault.py matched vault entries
to ORiON rows on (owner_id, action_text) — both mutable in ORiON — so
reassigning a row's owner made its entry match nothing and the next run
re-inserted a duplicate under the original owner. The durable shape: the
key is the SOURCE record's own immutable identity (here the header line's
date|owner-as-written|text), stamped on the destination row at import,
compared on every later run. Three sub-rules that mattered:

1. Enumerate the source's real identity before picking fields — text
   alone and (date, text) were both disproven by live data (four
   legitimate same-text same-date rows under four owners). The file's
   identity genuinely includes its owner field.
2. Use the source's RAW bytes, not anything that passes through a
   resolution layer — keying on the alias-resolved owner name would
   silently re-key every entry the day someone edits OWNER_ALIASES, and
   a re-key at scale is a mass re-import.
3. Exclude the one field with change pressure (status) — a key
   containing anything editable re-creates the bug it fixes.

Deploy mechanics: a stamped-key cutover has a race window (old code
inserts keyless rows between backfill and deploy). A legacy-match
fallback restricted to vault_key IS NULL rows, stamping on hit, closes
it without reviving the mutable-key path — each row is legacy-matched at
most once, ever. And a partial UNIQUE index on the key turns the
catastrophic failure mode (mass re-duplication) into a loud constraint
violation.

Also from the same build: prove a behavioral gate can FAIL before
trusting its pass — the harness ran the pre-fix code via git show as a
control and watched it insert the duplicate the new code refuses.

## 2026-08-20 — An exclusion added to work around one defect needs its reason re-derived, not just re-approved, when that defect is fixed

From the AP tier authority build (bugs aaaa96ea, 6cc5e30c, decision
2026-08-20-ap-tier-authority.md). The `86fd0c07` build excluded
`priority` from `ap_pending_update`'s raise set because `sync_ap.py`'s
force-reset made a priority-raised flag unclearable — a real reason, but
a transient one, tied to a bug that was itself scheduled to be fixed a
few hours later in the same day's work. When the force-reset fix landed,
the exclusion's *stated* reason (the code comment) went stale, while the
exclusion itself stayed correct — for a different, durable reason that
had been true the whole time and nobody had written down: the Smartsheet
tracker has no tier column at all, so a tier-raised flag could never
clear regardless of what the sync does. Two ways to get this wrong: (1)
"the force-reset is gone, so re-add priority to the raise set" — treats
the stated reason as the only reason, and misses that the flag would
still be structurally unclearable; (2) leave the comment saying "because
of the force-reset" — technically still excluded, but the next reader
who fixes some *other* sync behavior has no way to know the exclusion
doesn't depend on it, and will "correct" it back in. The fix for both is
the same: when removing whatever justified an exclusion, don't just
re-approve the exclusion — verify independently whether a *different*,
durable reason also holds (here: check the tracker's actual columns, not
the sync code), and if it does, rewrite the reason in place so the next
session inherits the real one instead of the expired one.

## 2026-08-24 — A pagination loop keyed on an assumed response field fails silently to page 1 forever

Bug 0644145e: fetch_child_ap_tasks() terminated on
`data.get("totalPages", 1)` — a field Smartsheet's GET /sheets/{id}
response does not return (it returns totalRowCount). The .get() default
of 1 always won, so every run since the script existed read exactly rows
1–500 of the 709-row tracker: ~78 active rows never evaluated, zero
errors, green runs throughout. Both the 108-row gap and the ap_titles
gap were this one loop. Two rules: (1) never loop on a response field
you haven't seen in a real response — the default you pass .get() is the
behavior you ship when the assumption is wrong, and a default that
terminates the loop makes the failure silent; (2) terminate pagination
on ground truth the response demonstrably carries (a short page), with
the count field as cross-check and a loud post-loop mismatch warning.

Verification shape that worked: dry-run the fix branch via workflow
dispatch, then DELTA it against the prior fix's own dry-run log — the
new rows classified exactly (63 insert + 7 unmapped-lead + 8 viewer-lead
= 78 page-2 actives, none unaccounted), and identical summary counts
everywhere else proved zero effect outside the newly-reachable rows.
A gap "explained" without that arithmetic closing to zero is a guess.
