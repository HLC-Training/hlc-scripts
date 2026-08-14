# TPM onboarding email: recovery-link script + manual-only trigger, built and test-verified; live send BLOCKED on auth allow-list config

Date: 2026-08-14
Action item: `083cc7a5` (SAM COS). Decision context: `d3e753ae` (TPM beta
launch sequence, 2026-08-11). Bugs filed this session: `065c166e` (auth
allow-list, high), `75c8d79b` (must_change_password double-set, medium).

## What shipped

`scripts/send_tpm_onboarding.py` + `.github/workflows/tpm-onboarding.yml`.
For each `portal_users` row with `role='tpm'` (queried live, never
hardcoded), the script generates a one-time Supabase recovery link via the
Auth Admin API (`generate_link`, `type=recovery`) and sends the branded
onboarding email (copy supplied verbatim in the build brief) via Resend
from `orion@ofstraining.com`. No temp passwords exist anywhere in the flow.

Trigger is `workflow_dispatch` only — deliberately no cron. This is a
one-time send to 9 external GE colleagues with no next-run safety net;
Jim fires it manually Monday 2026-08-17 with `target=live`. The `target`
input defaults to `test`, which sends exactly one email to Jim using
Jim's OWN account's recovery link (no TPM auth state touched) with an
automatic `[TEST] ` subject prefix so a misclick is visible unopened.

Reuse: role is a `--role` flag (default `tpm`) and the copy lives in two
template functions — PLL onboarding later is a flag plus a copy review,
not a new script.

## Task 1 findings (the reason this brief front-loaded them)

**Redirect target.** `orion-pll/app/reset-password/page.tsx` is the
landing page; it handles both the PKCE `?code=` shape and the implicit
`#access_token=…&type=recovery` fragment that admin-generated links
produce (lines 35–52), and `middleware.ts:22` exempts `/reset-password`
from both the Entra gate and the login redirect. `redirect_to` is
therefore `https://orion.ofstraining.com/reset-password`.

**The allow-list drops it.** Empirical probe (generate_link against
Jim's own account, nothing sent): the returned action link's
`redirect_to` came back rewritten to the legacy Site URL
`https://pages.github.apps.gevernova.net/210077026/hlc-pll-dashboard/`.
Also probed `pll.ofstraining.com`, `orion-pll.vercel.app`, and
`localhost:3100` — none survive. The allow-list is effectively
empty/stale, worse than the 2026-08-04 state (bug `42ca9229`), whose
"actual fix" (adding `orion.ofstraining.com/**` in the dashboard) was
left to Jim and never happened. Consequence beyond this build: the live
forgot-password flow on orion.ofstraining.com is broken today.
**Jim must add `https://orion.ofstraining.com/**` under Authentication →
URL Configuration before Monday.** The script encodes this as a runtime
guard: live mode generates and validates all links BEFORE the first
send and refuses to send if any link came back rewritten. Test mode
warns loudly but sends, so the pipeline stays provable meanwhile.

**Link expiry.** Could not read the exact GoTrue `mailer_otp_exp` — no
Management API token on this machine, no Supabase CLI, and the dashboard
session was logged out (signing in is outside what a Claude session may
do). Best available evidence: the project's security advisors report
auth-level lints but NOT `auth_otp_long_expiry`, which Supabase raises
when email OTP expiry exceeds 3600s → **expiry is ≤ 1 hour**. That makes
Monday logistically tight (links generated at send time; 9 people
unlikely to all open within an hour). Per the brief's stop rule this is
flagged as a product decision, not worked around: options are Jim
raising the expiry in the dashboard (Auth settings, where he'll already
be for the allow-list) or accepting that late openers use the
self-service "Link Invalid or Expired → request a new one" path on
`/reset-password`. The email's "use it soon" sentence was left verbatim
because the exact window is unconfirmed; if Jim confirms 1 hour and
wants it stated, it is a one-sentence edit in both template functions.

**must_change_password double-set.** All 9 TPM rows have
`must_change_password=true`, and only `/change-password` clears it — so
after setting a password via the recovery link, middleware bounces the
TPM into a second forced password set. Works, but clumsy; filed as bug
`75c8d79b` (orion-pll side) to decide before Monday.

## Mechanics worth keeping

- **429 handling:** 1.5s pacing between generate_link calls plus
  Retry-After-honoring exponential backoff, max 5 retries. Unexercised
  in the single-recipient test (no 429 observed) — stated plainly, not
  claimed proven.
- **All-or-nothing live sequencing:** phase 1 generates + validates all
  links, phase 2 sends; send failures don't abort the loop but exit
  non-zero at the END (lessons.md 2026-08-10 pattern) so a rerun never
  double-sends the people before a failure point.
- **Token hygiene:** logs and CI output carry only recipient + sha256
  prefix + length of each link. The full link exists only in the
  outbound email.

## Verification evidence (gate from the build brief)

1. **Redirect target: CONFIRMED** — route file
   `orion-pll/app/reset-password/page.tsx` (implicit-fragment handling
   lines 35–52), middleware exemption `middleware.ts:22`;
   `redirect_to=https://orion.ofstraining.com/reset-password`.
   **Allow-list preservation: FAIL** (by config, not build) — see above;
   live send guarded off until fixed.
2. **Expiry: BOUNDED, not exact** — ≤ 3600s via absence of the
   `auth_otp_long_expiry` advisor (auth lints proven present via
   `auth_leaked_password_protection` in the same response). Copy left
   verbatim; flagged to Jim.
3. **Test run: PASS** — Actions run 31832155307 (workflow_dispatch,
   `TARGET: test`, conclusion success, 2026-08-14 19:11Z). Log shows 1
   link generated for Jim.Rosen@gevernova.com
   (`sha256:783c37c2540a/len:213`, `redirect_preserved=False` warned),
   1 email sent. Resend email `3c9dba41-55a4-4de4-bc16-a4092a5437e3`:
   status **delivered** to jim.rosen@gevernova.com, subject
   `[TEST] Welcome to ORiON — set up your account`.
4. **Content: PASS** — delivered plain-text body matches the brief copy
   byte-for-byte after merges (`Hi Jim,` + real action link; verified
   both by fetching the delivered email from Resend and by a
   programmatic local diff of `build_text_body` against the brief copy).
5. **9-row query: PASS** — live `portal_users WHERE role='tpm'` returned
   exactly 9 rows (Hayes, Gupta, Wall, Norris, Ponce, Kirby, Martino,
   Messekine, Biediger), re-queried at report time.
6. **No live send: PASS** — the workflow's full Actions run list
   contains exactly one run ever (31832155307, `TARGET: test`). No
   `target=live` run exists.
7. **Rate limit: backoff exists, unexercised** — no 429 was triggered
   during the single-user test; retry logic is in
   `generate_recovery_link()` but has not seen a real 429.

## Deviations from the brief

- **Two commits, not one.** A NEW `workflow_dispatch` workflow cannot be
  dispatched (404) until its file exists on the default branch — the
  lessons.md 8/12 `--ref <branch>` pattern only works for
  already-registered workflows. So the code had to land on main before
  the test run, and this doc lands in a second commit. (Lessons entry
  added.)
- Script lives in `scripts/` per the brief header, unlike the root-level
  digest siblings.
- The HTML rendering places the link as a branded button
  ("Sign In & Set Your Password" — same visual as the digests' "Open
  ORiON" button); the plain-text part carries the raw link on its own
  line exactly as the verbatim copy shows. Button label is presentation,
  not copy.

## Before Monday (Jim)

1. Dashboard: add `https://orion.ofstraining.com/**` to the auth
   redirect allow-list (bug `065c166e`) — without this, live mode
   refuses to send (by design).
2. Same dashboard visit: read the actual email OTP expiry; decide
   whether to raise it and/or firm up the "use it soon" sentence.
3. Decide on the double password-set (bug `75c8d79b`).
4. Optionally re-run `target=test` after the config fix — the log line
   should then read `redirect_preserved=True` with no warning.
5. Monday morning: Actions → ORION TPM Onboarding Email → Run workflow
   → `target=live`.
