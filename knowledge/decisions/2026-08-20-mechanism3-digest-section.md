# Mechanism 3 — missing-required-information digest section (2026-08-20)

Action item `7007b802` (SAM COS). Repos: hlc-scripts (both digest scripts,
this doc), orion-pll (the completeness view, help docs, verification
scaffolding under `scripts/verification-2026-08-20/`).

## What this build is, and isn't

Mechanism 3 was scoped as two halves: an in-app surface and an email
surface. Jim ruled 2026-08-20 that M2 and M3 share one in-app surface — M2's
new-arrival completion prompt (`orion-pll` 2026-08-19,
`knowledge/decisions/2026-08-19-mandatory-field-data-integrity.md`) already
computes over every open incomplete row an owner has, not only new
arrivals, and its own decision doc says M3 "reuses this check" and
"inherits the same scope." So the in-app half of M3 shipped on 2026-08-19
under M2's name; this build is the email half only — one new section in
`send_pll_digest.py` and `send_tpm_digest.py`. No new notifier, schedule,
or surface was built, matching the brief's explicit scope boundary.

## The completeness definition: one SQL view, deliberate temporary TS duplication

The rule already existed in TypeScript, duplicated across orion-pll and
(until this morning) `orion` — `lib/incomplete-rows.ts`,
`findIncompleteDeliveryRows` / `findIncompletePcRows`. A third Python copy
in the digests would be a third place the rule could drift. Per Jim's
standing principle, the divergence detector ships in the same build as the
duplication, not as a follow-up.

Shipped as a migration in orion-pll (the repo that owns ORiON schema
migrations — `docs/migrations/`, same convention as the Approaching Stale
views this pattern is modeled on):
`docs/migrations/2026-08-20_incomplete_rows_view.sql`, applied to
`czdkctjbejnwuopigxta` — `public.incomplete_delivery_rows` and
`public.incomplete_pc_rows`, one view per module (not one view with a
module column: the two modules have different column names and mandatory-
field counts, and the TS source of truth is already two separate
functions, not one polymorphic one — a per-module view avoids adding a
normalization layer that isn't in the source of truth). `security_invoker`,
`GRANT SELECT` to `service_role` only — narrower than the Approaching Stale
views' `authenticated, service_role`, because nothing in the app UI reads
this view; only the two Python digests (service role) do.

**The TypeScript keeps its own separate copy, deliberately, for now.** Both
digests query the view; `lib/incomplete-rows.ts` and the app's Launch Pad
prompt are untouched. This is a temporary divergence and the Group A gate
below is what makes it safe — it proves the view and the TS agree on the
exact row set, per owner, today. Repointing the TS to the view was
explicitly out of scope (touches shipped, verified M2 code for no
functional gain) and wasn't done.

`orion`'s copy of `lib/incomplete-rows.ts` (the brief assumed a second
copy existed there for legacy `pnc.ofstraining.com`) was deleted this same
morning, commit `117f12d`, when `orion`'s own M2 mount was removed as dead
weight (`orion-pll` decision `2026-08-20-m2-launchpad-only-and-first-date-
reason-guard.md`). Confirmed before Task 2: `orion-pll`'s copy is the only
one, so "the shipped TypeScript" this view must match is unambiguous.

## Two corrections to the source-of-record found during discovery

**Field count.** Decision `3159216d` (2026-08-19, orion-pll) lists
Delivery's mandatory fields as "title/owner/category/start/end" (five) and
P&C's as those plus description (six) — the brief's Task 5 verification
gate inherited this as "eleven fixtures." But `owner` is never actually
checkable as missing: `pc_projects.owner_id` is `NOT NULL` at the DB level,
and Delivery's owner is guaranteed by `resolveOwner` on create — both
`item-actions.ts` and `pc-validate.ts` say so explicitly in their own
mandatory-field comments, and neither's `missing.push(...)` list ever
includes an owner check. Confirmed empirically: zero open rows on either
table are null on `owner_id` (queried 2026-08-20). The view and this
build's fixtures use the real, checkable counts — **4 fields on Delivery,
5 on P&C, 9 fixtures total** — matching `lib/incomplete-rows.ts` exactly.
`3159216d`'s field table should be read as the full design intent (owner
included as a principle) but not as the checkable-field count; noting it
here so the next reader doesn't rebuild the same over-counted gate.

**Field label.** `lib/incomplete-rows.ts`'s own comment/label for
Delivery's text field says `"Action text"`. The real form — Mechanism 1's
actual validator, `item-actions.ts`'s `validateInput` — says
`"Action item text"`. M2's TS had drifted one word from M1's real copy.
Per Task 4's instruction to confirm every label against the actual form,
the view and both digests use `"Action item text"`, not `incomplete-
rows.ts`'s own (slightly wrong) string. This is the one intentional,
expected difference between the view's rendered output and
`findIncompleteDeliveryRows`'s output in the Group A proof below.

## Group A — view vs. shipped TypeScript, per-owner row-set equality

Both sides run for real: the view via direct query against
`czdkctjbejnwuopigxta`, and the TypeScript via a genuine Next.js request
context (a temporary, unauthenticated diagnostic route,
`app/api/verify-m3-incomplete-rows/route.ts`, added and deleted within this
session — `findIncompleteDeliveryRows`/`findIncompletePcRows` need
`next/headers`'s `cookies()`, which throws outside a real request scope; a
Node module-loader stub was tried first and abandoned when tsx's own
loader chain didn't yield to it — the honest fix was a real request, not a
mock). Auth for the request came from a disposable fixture director
account (`m3-verify-fixture.ts`, auth-only, no owned rows, deleted after);
directors see all owners' rows in-module via RLS (`has_delivery_module_
access()` / `has_pc_module_access()`), so one session sufficed for every
owner.

**Per-owner, all 8 real owners with incomplete rows today, full id sets
(not counts) — every set matched exactly:**

| Owner | Table | Row count | Match |
|---|---|---|---|
| `0608aa17…` | Delivery | 2 | exact |
| `327a06f4…` | Delivery | 2 | exact |
| `4bab65a8…` | Delivery | 3 | exact |
| `bb75b6d6…` (Kelly) | P&C | 49 | exact |
| `1ac4b5a2…` (Tamara) | P&C | 13 | exact |
| `08a4f9bb…` | P&C | 3 | exact |
| `68accf9e…` (Salim) | P&C | 3 | exact |
| `6cb4b844…` | P&C | 2 | exact |

79 real rows total (7 Delivery + 70 P&C), matching action item `7007b802`'s
recorded volume at brief time exactly (Delivery 7 across 3 PLLs, P&C 70 —
Kelly 49, Tamara 13, Luca 3, Salim 3, Ankita 2).

**Per-field, 9 disposable fixtures** (one per mandatory field, each
missing only that field, owned by the auth fixture, deleted after): all 9
appeared in both the view and the TS output with exactly the one field
each was built to be missing and no other. The only difference: the
Delivery text-field fixture reports `"Action item text"` from the view vs.
`"Action text"` from the TS — the deliberate label correction above, not a
divergence.

**Complete row in neither:** one fully-populated fixture per table, absent
from both the view and the TS output — confirmed.

**Closed-row exclusion, both tables:** one `status='Done'` Delivery row and
one `status='complete'` P&C row, both missing every field, both absent
from both the view and the TS output — confirmed.

All fixture rows, the fixture director account, and the temporary
diagnostic route were deleted before this doc was written; `SELECT count(*)
... WHERE owner_id = <fixture>` returns 0 on both tables post-cleanup.

## Group B — the emails

Dry-run text renders (`--dry-run --force-date-gate`) diffed against a
`git show HEAD:`-rendered pre-build baseline for both scripts: the *only*
diff lines beyond timestamps are one new Supabase query per script and the
new section block appearing exactly for owners with incomplete rows —
every other PLL/TPM's pre-existing content is byte-for-byte identical.
Kelly's 49-row P&C block correctly caps at 5 with "Plus 44 more..."; Tamara
's 13 caps at 5 with "Plus 8 more...". `--dry-run` never exercises
`build_html_body` (same gap the 2026-08-11 Approaching Stale build noted),
so both scripts' HTML paths were separately checked by constructing
representative digest dicts directly against the real functions: section
renders with row + overflow when populated, is absent (with surrounding
markup intact) when empty, for both scripts. Empty-state suppression
(`build_digest` returning `None` when every section including this one is
empty) was proven the same way — no real PLL or TPM is fully empty today,
so both scripts' actual `build_digest()` were called directly with all-
empty inputs and asserted `None`. No `--live` run and no `RESEND_API_KEY`
was ever set anywhere in this session — the only path that could send mail
was structurally unavailable throughout.

## Why the TPM roll-up section does *not* ship inert

The brief assumed `TPM_DIGEST_LIVE` was still off ("ships inert... until
Jim flips it. Do not flip it") — every prior doc agreed, including this
digest's own module docstring ("stays in TEST mode indefinitely... until
P&C's non-AP import + launch"). That's stale: `gh variable list` shows
`TPM_DIGEST_LIVE` was set `true` on **2026-08-14**, and the scheduled runs
on 08-17, 08-18, 08-19, and 08-20 all ran live — Michele has been receiving
the real combined P&C digest for four weekday mornings already. This
build's new section therefore ships **live**, in Michele's next scheduled
email, not dormant. Confirmed with the user before pushing: proceed, Jim
is already aware the flag is live. `TPM_DIGEST_LIVE` itself was not
touched by this build (confirmed unchanged before/after in the dry-run
verification above) — only what a change to the flag's *known state*
means for this section's rollout.

## Out of scope, not touched

The completeness rule itself (reports it, doesn't change it); bug
`17b1a835` (AP-pending digest, adjacent template); Mechanism 4 (NOT NULL
backstop); repointing the TypeScript to the view; the M2 in-app prompt or
`lib/incomplete-rows.ts`; either digest's schedule, weekday gate, or
holiday gate; `PLL_DIGEST_LIVE`.
