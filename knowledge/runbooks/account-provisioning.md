# Provisioning a new ORiON portal_users account

`portal_users.id` is a foreign key to `auth.users(id)` — a `portal_users`
row cannot exist without a real Supabase Auth account behind it. There is
no way to "just insert a profile row."

## Creating a new dormant account (no email sent)

Use `onboarding/create-account.mjs`:

```
export ORION_SERVICE_ROLE_KEY=<ORiON service role key>
node onboarding/create-account.mjs <email> "<name>" <role> <module>
```

This calls `auth.admin.createUser({ email, email_confirm: true, password:
<random, never shared> })` then inserts the matching `portal_users` row
(`must_change_password: true`, `is_admin: false`, `is_ap_manager: false`).
If the `portal_users` insert fails, the script deletes the just-created
auth user rather than leaving an orphan.

**Never use `inviteUserByEmail()` or the signup flow** — both send mail.
`email_confirm: true` marks the address confirmed directly and sends
nothing. Proven empirically (bug `fbad5027`, 2026-09-11): a throwaway
non-gevernova.com test account showed `confirmation_sent_at`,
`invited_at`, and `recovery_sent_at` all NULL on the `auth.users` row
after creation, and the Resend send log had zero activity in the entire
window the script touched auth — not just zero sends to the test address.

Valid `role` values: `executive, director, programs_lead, tpm, viewer,
pll, ops, tdm, cpm`. Valid `module` values: `delivery, pc, internal,
operations, ehs, capacity, customer`. Both are enforced by live DB
constraints (`portal_users_role_check`, `portal_users_module_fkey`) —
the script also checks them up front for a clearer error.

Requires `@supabase/supabase-js` (`npm install` at the repo root — added
2026-09-11; previously the `onboarding/*.mjs` scripts depended on it with
no `package.json` anywhere in the repo).

## This does NOT onboard the person

Creating the account is provisioning, not onboarding — the account is
dormant (`must_change_password=true`, no credential in their hands yet).
To actually onboard someone:

- **A brand-new account** (no login has ever worked): use the existing
  temp-password path, `onboarding/set-temp-password.mjs <email>` — it
  refuses unless `must_change_password=true` is confirmed first, sets a
  random temp password, and prints it (hand-deliver it; the script sends
  no mail either).
- **An account being reset**: `onboarding/generate-one-link.mjs <email>`
  or `generate-tpm-links.mjs` for a recovery link, per the pattern in
  `knowledge/decisions/2026-08-14-tpm-onboarding-email.md`.

Timing of onboarding is a separate decision from provisioning — a
dormant account can sit provisioned indefinitely with no risk, since it
has no credential anyone can use.

## Cleaning up a mistake / test account

```
node onboarding/create-account.mjs --delete <email>
```

Deletes both the `portal_users` row and the `auth.users` row for that
address. Used for the Gate 1 throwaway proof above; also the right tool
if a provisioning row was created in error.
