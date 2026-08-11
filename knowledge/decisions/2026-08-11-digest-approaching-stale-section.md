# Approaching-Stale early warning — PLL + TPM digest section (2026-08-11)

Action item `bde783ad-14ca-41bb-ade6-4c03f1de9f18` (SAM COS), Part 2 of 2.
Part 1 (`3f6af5fe`, orion-pll repo, `knowledge/decisions/
2026-08-11-approaching-stale-warning.md` there) shipped
`public.staleness_state()` and two views on the ORiON DB
(czdkctjbejnwuopigxta) — `action_items_staleness` / `pc_projects_staleness`,
both `(id, owner_id, staleness)`. This build wires both digest scripts to
consume those views directly. No SQL was touched — read-only consumer.

## Query shape (both scripts)

Two-step fetch, mirroring the existing `fetch_plls → fetch_items` /
`fetch_tpms → fetch_projects` pattern:

1. `fetch_approaching(db, owner_ids)` queries the staleness view —
   `select id, owner_id` filtered `staleness = 'approaching'` and
   `owner_id in (...)`.
2. The returned ids are used to pull full detail rows from the source
   table (`action_items` / `pc_projects`) — same columns the other
   sections already select, plus the staleness clock column
   (`last_updated` for action_items, `updated_at` for pc_projects).

`main()` fetches the approaching set once across all owners (same shape
as `items`/`projects`), and `build_digest` filters per-owner and sorts
oldest-clock-first, capped at 5 with a "+N more" line — identical
convention to the existing overdue/past-due sections (`OVERDUE_CAP`
reused, not a new constant).

**Deliberately no status re-filter beyond the view's own terminal set.**
`action_items_staleness`'s terminal set is `{Done}` only (not
`ACTIVE_STATUSES = {Open, In Progress}` that the other three PLL sections
use) — a `Deferred` item can appear in this section. Confirmed against
Part 1's decision doc: the Approaching Stale badge in the app "shows in
the normal, everyday view... for everyone who can see the list," not
gated by a status filter narrower than the view's own terminal check.
Reimplementing `ACTIVE_STATUSES` on top of the view would silently
diverge from what the badge shows for the same item — exactly the
Python-reimplementation risk Part 1's shared-function architecture exists
to avoid. Live data: of 25 total approaching items DB-wide, 2 are
`Deferred` (both landed in Linda Nelson's PLL digest during verification,
correctly rendered).

`pc_projects_staleness`'s terminal set (`{complete, cancelled}`) already
matches `DONE_STATUSES` in `send_tpm_digest.py`, so no equivalent
divergence exists there.

## Rendering

- PLL digest (`send_pll_digest.py`): new "Approaching Stale" section
  appended after "Past due" in both the per-PLL email and the text
  digest; roll-up table gains an "Approaching" column (per-PLL count),
  appended after the existing Overflow column. A PLL with items only in
  this section now gets an email — the four-section "any content → send"
  rule extends the existing three-section one, unchanged mechanism.
- TPM digest (`send_tpm_digest.py`): new "Approaching Stale" section
  appended after "Overdue" in the single combined email to Michele,
  sub-grouped by TPM, present only for TPMs with items in it — same
  `section_blocks`/absent-if-empty pattern as the other three sections.
- Both: a one-line pointer under the section heading ("...will flip to
  Stale soon — a note in ORiON resets the clock. See the Approaching
  Stale help article in ORiON for details.") — brief per the brief's
  scope boundary; the Rigel doc (orion-pll `knowledge/help/
  approaching-stale.md`) stays the canonical explanation, not duplicated
  here.

## Verification (dry-run, GitHub Actions workflow_dispatch)

Both scripts read live production data — no way to fabricate a failing
write to prove against, and none needed here (read-only build). Verified
by controlled A/B dry-run comparison instead:

1. Pushed pre-existing local commit (`9c42138`, unrelated to this build)
   to `origin/main`, then ran `workflow_dispatch --dry-run
   --force-date-gate` for both `pll-digest.yml` and `tpm-digest.yml`
   against that ref — **BEFORE** baseline (runs `31513066509` /
   `31513069646`, both `success`).
2. Committed + pushed this build's changes (`e955bc8`), ran the same
   dispatch again — **AFTER** (runs `31513170021` / `31513173519`, both
   `success`).
3. Diffed the two logs' `Send digest` step output line-for-line (ignoring
   only timestamps). Every pre-existing line in every PLL's "Added since
   your last update" / "Coming up in the next two weeks" / "Past due" and
   every TPM's "New projects" / "Coming up in the next two weeks" /
   "Overdue" sections is byte-for-byte identical between BEFORE and
   AFTER. The only diff lines are: the two new Supabase HTTP calls
   (staleness view + detail query), the new "Approaching Stale" section
   block inserted after each PLL/TPM's existing content, and (PLL roll-up
   only) an added `approaching=N` field per line — exactly the additive
   surface this build was scoped to.
4. AFTER run rendered real approaching-stale content: 6 of 7 PLLs got a
   populated section (16 items total across PLL owners, including the 2
   `Deferred` ones on Linda Nelson, and Sherif Khalifa's "+3 more" cap
   correctly triggered at 5+3=8); Michele's combined digest got 2 items
   across 2 TPMs (Luca Martino, Salim Messekine) — matches Part 1's
   verified parity counts (25 approaching action_items DB-wide, 2
   approaching pc_projects DB-wide).
5. Both runs' env dump confirms `PLL_DIGEST_LIVE: true` /
   `TPM_DIGEST_LIVE: <empty>` unchanged from before this build, and
   neither script reads the other's env var (grepped — only their own
   name appears in each file's `main()`). `--dry-run` short-circuited
   before any send/state-write branch in both scripts, matching
   `[DRY RUN] Nothing sent, no state written.` in both logs.
6. `build_html_body` is NOT exercised by `--dry-run` (only
   `build_text_body` prints) — separately verified locally, outside any
   network call, by constructing representative digest dicts (including
   a `Deferred`-status item, a no-due-date item, and an overflow count)
   and asserting the HTML renders the new section, omits it when empty,
   and doesn't disturb surrounding markup, for both scripts.

No real (non-dry-run) send was used anywhere in this verification.

## Out of scope, not touched

`staleness_state()` and the two views (Part 1's, read-only here);
`TPM_DIGEST_LIVE` (still unset — TPM digest stays in TEST mode, per
Jim's separate go-live call); either script's watermark logic or
send-gating mechanism (the four-section empty-check is the same
mechanism the three-section one used, just extended).
