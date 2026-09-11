# AP owner provisioning: the 5 module/role pairs, two new director seats, and the first email-safe account-creation path

**Date:** 2026-09-11
**Repo:** `hlc-scripts` (`onboarding/create-account.mjs`, `scripts/find_missing_ap_owners.py`, `knowledge/runbooks/account-provisioning.md`). ORiON `czdkctjbejnwuopigxta` (`portal_users`, `portal_modules`, `auth.users`) only.
**Origin:** Phase 1 of bug `ca9beaeb` (AP date-change ack loop) / the OPS module launch — 34 of 46 unacked AP date-change events belonged to APs with no ORiON owner because no user row existed to point ownership at. This phase creates the missing users; it does not assign ownership (Phase 2) or build the ack callout (Phase 3).

---

## The vocabulary (locked with Jim, 2026-09-11)

| Module | Role | Population | Built? |
|---|---|---|---|
| delivery | pll | PLLs | yes |
| pc | tpm | TPMs | yes |
| operations | ops | OPS owners | module active, users now provisioned |
| internal | tdm | TDMs | module stub (active=false), users now provisioned |
| customer | cpm | CPMs | module stub (active=false), users now provisioned |

Role = job title, module = area. `operations`/`ops` is a deliberate near-match — OPS has no distinct job-title role, so the role mirrors the function; every other pairing is a real title with a distinct module.

Two module director seats were also created — leadership defined in data before either module's UI exists, mirroring Philip Ganssmann (`director`/`operations`) and Michele Alcantar (`director`/`pc`):

- **Leonardo Monzo** — `director`/`internal` (Internal Training Director, over the TDM population)
- **Johann Josef Daxer** — `director`/`customer` (Customer Training Director; first `customer`-module user)

## `tdm` is a role, never a module

`portal_modules` had a stub row `tdm` ("Future module — not yet built", active=false, 0 users) predating this vocabulary. It collided with the locked vocabulary, where `tdm` is a *role* under module `internal`. Resolved by data migration (module keys, not the role-constraint migration — that was a separate, earlier step): `internal` and `customer` were added as `portal_modules` keys (both active=false), the 2 `portal_role_modules` grants that pointed at the `tdm` stub were repointed to `internal`, and the `tdm` module row was deleted. Live module keys after: `delivery, pc, internal, operations, ehs, capacity, customer`. The role constraint (`portal_users_role_check`) separately gained `tdm` and `cpm` as roles, alongside the prior `executive, director, programs_lead, tpm, viewer, pll, ops` — `programs_lead` (Michele's pre-director legacy role) was kept, not removed.

## Provisioning required a new capability, not just an insert

`portal_users.id` is a foreign key to `auth.users(id)` — a profile row cannot exist without a real Supabase Auth account behind it, and none of the 29 people below had one. This repo had no script that *creates* a new auth account; the existing `onboarding/*.mjs` scripts (`generate-tpm-links.mjs`, `set-temp-password.mjs`, `generate-one-link.mjs`) only ever acted on accounts that already existed by some other, undocumented path.

`onboarding/create-account.mjs` (bug `fbad5027`) closes that gap: `auth.admin.createUser({ email_confirm: true, ... })` creates a confirmed account and sends no mail — proven empirically on a throwaway non-gevernova.com test account before touching any real person (Gate 1): the resulting `auth.users` row had `confirmation_sent_at`/`invited_at`/`recovery_sent_at` all NULL, and the Resend send log showed zero activity anywhere in the entire window the script touched auth, not merely zero sends to the test address. `inviteUserByEmail()` and the signup flow both send mail and were never used. Documented as the standard path in `knowledge/runbooks/account-provisioning.md`, including the boundary between this (provisioning a dormant account) and the existing temp-password/recovery-link scripts (actual onboarding — a separate, later, Jim-timed step).

The repo also had no `package.json` despite five `.mjs` scripts depending on `@supabase/supabase-js` — added this session (`npm init` + `npm install @supabase/supabase-js`), `node_modules/` gitignored.

## What got created

29 accounts (`auth.users` + matching `portal_users`, `must_change_password=true`, `is_admin=false`, `is_ap_manager=false`, no welcome/onboarding mail of any kind — provisioned-dormant):

- `operations`/`ops`: 8 — Andrew Ruch, Andrew Samples, Lee Fielding, Daniel Barrera, Iuliia Zgonnik, Konstantina Georga, Nelsa Bernal, Diana Torres Sanchez
- `internal`/`tdm`: 12 — Carlo Fabregas, Paula Andrea Pedroza Nino, Nimra Kazmi, Mouna Boucherka, Erica Mayeli Peredo Vazquez, Brenda Roman Valle, Marcela Bohorquez, Howayda Sabry Maher, Alyssa Mays, Shaimaa Galal, Beyza Yaver, Alice Pagani
- `internal`/`director`: 1 — Leonardo Monzo
- `customer`/`cpm`: 7 — Gavin Yoong, Maria Medina, Patrick Molumby, Abuobaida El Mansoor, Anna Carmela Petrella, Siaw Wei Tan, Alejandra María Saldaña
- `customer`/`director`: 1 — Johann Josef Daxer

All 29 identified by pulling the distinct Lead values off the AP Tracker sheet (834 rows, 53 distinct leads, 20 already had ORiON users) and diffing against `portal_users`; every module/role assignment was Jim's, made against that pulled list — none inferred from sheet structure (the sheet's `Bucket`/`Source` columns don't segment Delivery/P&C/OPS/Internal/Customer, so no reliable auto-suggestion was possible).

**Daniel Barrera correction**: the sheet's Lead *display* text reads "Armando Barrera" for AP-197 and family, but the linked contact cell's raw email is already `daniel.barrera1@gevernova.com` — confirmed directly against the fetched cell, not via `ap_lead_aliases` (no alias row exists for this name at all; one was assumed to exist in an earlier draft of this brief and doesn't). Provisioned as "Daniel Barrera" under that email; nothing needed to be "left intact" in `ap_lead_aliases` because nothing was ever there — the sheet's own contact-cell email already resolves correctly regardless of the stale display name.

## Skipped, not created

- **David Ramey** — no longer in the training org; Michele Alcantar took his P&C director place. Lead on 2 AP Tracker rows with no current valid owner. **Phase 2 carry-forward: those 2 APs need reassignment, not a pointer at any user.**
- **"TDM TBD", "TBD from D&D Team", "n/a- See Child Leads"** — placeholder text, not people.

Zero email-resolution failures among the 29 — every address resolved cleanly from the sheet's own lead resolution.

## Verification

- `portal_users_role_check` and `portal_modules` both confirmed live via `pg_get_constraintdef`/`select key from portal_modules` immediately before provisioning — neither migration was re-run.
- 29 `portal_users` rows created, grouped counts match exactly (8/12/1/7/1); every `id` matches a real `auth.users.id` (29/29, zero orphans); every row `must_change_password=true`, zero `is_admin`/`is_ap_manager`.
- Ramey and the 3 placeholders confirmed absent from both `portal_users` and `auth.users`.
- Gate 1 proof (test account, no mail, deleted clean) preceded the real run; the real 29-account batch was re-checked against the same no-mail signals (auth flags + Resend log) after the fact.

## What this does not do

No `owner_id` / AP-ownership writes (Phase 2 — also the sync-scope question of why some flagged APs are in neither `action_items` nor `pc_projects`, and the Ramey reassignment). No ack callout or digest (Phase 3). No Internal or Customer module UI — both stay `active=false` stubs; only roles/users/vocabulary exist now. No onboarding of any of the 29 — dormant accounts, onboarding timing is Jim's call via the existing temp-password runbook.
