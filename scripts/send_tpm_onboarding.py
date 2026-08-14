#!/usr/bin/env python3
"""
send_tpm_onboarding.py
─────────────────────────────────────────────────────────────────
One-time ORiON onboarding email for P&C TPMs (SAM COS action item
083cc7a5, decision d3e753ae). For each portal_users row with
role='tpm' on ORiON, generates a one-time Supabase recovery link via
the Auth Admin API and sends a branded email via Resend from
orion@ofstraining.com. No temp passwords anywhere — reset-link flow
only, so no plaintext password ever exists to leak.

This is NOT a digest. There is no cron, no watermark, no next-run
safety net — the trigger is workflow_dispatch only
(.github/workflows/tpm-onboarding.yml), fired by Jim manually.

TARGETS
  test (default): generates a recovery link for JIM'S OWN account and
      sends exactly one email to Jim, subject prefixed "[TEST] ". No
      TPM's auth state is touched, no TPM receives anything.
  live: queried live from portal_users (role='tpm'), one link + one
      email per person. Jim's trigger alone, D-Day morning.

REDIRECT PRESERVATION GUARD
  GoTrue silently replaces a redirect_to that is not on the Auth URL
  allow-list with the project Site URL — which on this shared project
  is still the legacy hlc-pll-dashboard GitHub Pages URL (orion-pll
  knowledge/decisions/2026-08-04-redirect-fix.md, bug 42ca9229; the
  allow-list entry for orion.ofstraining.com was confirmed still
  missing 2026-08-14). A generated link that came back rewritten is a
  BROKEN link. Live mode therefore generates and validates every link
  BEFORE sending anything, and refuses to send at all on a mismatch.
  Test mode warns loudly but still sends, so the rest of the pipeline
  stays provable while the dashboard config is pending.

TOKEN HYGIENE
  The action link contains an unexpired recovery token. It goes into
  the outbound email and NOWHERE else — logs and CI output carry only
  the recipient, the link's sha256 prefix, and its length. GitHub
  Actions logs are not a safe place to persist a live recovery token.

REUSE
  Role is a --role flag (default 'tpm') and the copy lives in two
  template functions, so PLL onboarding later is a flag flip plus a
  copy review, not a new script.

Exit codes: any fetch/generate/send failure exits non-zero at the END
having attempted every recipient — never a green run that silently
dropped someone (lessons.md), and never an early abort that leaves an
ambiguous half-sent state on rerun.
─────────────────────────────────────────────────────────────────
"""

import os
import sys
import html
import time
import json
import hashlib
import logging
import argparse
import requests
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from supabase import create_client

# ─── CONFIG ─────────────────────────────────────────────────────
SUPABASE_URL = "https://czdkctjbejnwuopigxta.supabase.co"
# ORION_-prefixed on purpose — the generic SUPABASE_SERVICE_KEY name risks
# resolving to the wrong project across the three-project estate
# (lessons.md 2026-08-03). Do not shorten it.
ORION_SUPABASE_SERVICE_KEY = os.environ.get("ORION_SUPABASE_SERVICE_KEY", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")

RESEND_API_URL = "https://api.resend.com/emails"
GENERATE_LINK_URL = f"{SUPABASE_URL}/auth/v1/admin/generate_link"
FROM_EMAIL = "orion@ofstraining.com"
JIM_EMAIL = "jim.rosen@gevernova.com"
ORION_URL = "https://orion.ofstraining.com"
# The recovery-link landing page (orion-pll app/reset-password/page.tsx).
# It consumes the implicit #access_token fragment that admin-generated
# links produce and is exempt from the middleware login/Entra redirects.
REDIRECT_TO = f"{ORION_URL}/reset-password"

SUBJECT = "Welcome to ORiON — set up your account"

# Supabase Admin API applies abuse-prevention backoff on repeated
# generate_link calls; Resend's default limit is 2 req/s. One shared
# pace covers both.
CALL_DELAY_SECONDS = 1.5
MAX_RETRIES = 5

# Brand values — identical to send_pll_digest.py / send_tpm_digest.py
# (orion-pll app/styles/vernova-tokens.css + orion-tokens.css).
EVERGREEN   = "#005e60"
HEADER_DEEP = "#004a4c"
ION         = "#3fd2ff"
NIGHT       = "#212121"
BODY_TEXT   = "#444444"
MUTED       = "#888888"
BORDER      = "#e5e7eb"
EMAIL_WIDTH = 600
FONT = "Calibri,Arial,Helvetica,sans-serif"

LOG_FILE = Path(__file__).parent.parent / "send_tpm_onboarding.log"

# ─── LOGGING ────────────────────────────────────────────────────
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
_console = logging.StreamHandler()
_console.setFormatter(logging.Formatter(
    '%(asctime)s | %(levelname)s | %(message)s', '%Y-%m-%d %H:%M:%S'))
logging.getLogger().addHandler(_console)
log = logging.getLogger(__name__)


def link_ref(link: str) -> str:
    """Audit-trail reference for a generated link that is safe to log:
    sha256 prefix + length, never the link or token itself."""
    return f"sha256:{hashlib.sha256(link.encode()).hexdigest()[:12]}/len:{len(link)}"


# ─── DATA ───────────────────────────────────────────────────────
def fetch_recipients(db, role: str) -> list[dict]:
    resp = db.table('portal_users').select('id, name, email') \
        .eq('role', role).order('name').execute()
    users = resp.data or []
    if not users:
        raise RuntimeError(f"portal_users query returned zero role='{role}' rows")
    return users


def fetch_test_recipient(db) -> dict:
    # .ilike, never .eq — GE addresses appear in mixed case and an exact
    # match silently returns nothing (lessons.md).
    resp = db.table('portal_users').select('id, name, email') \
        .ilike('email', JIM_EMAIL).execute()
    rows = resp.data or []
    if len(rows) != 1:
        raise RuntimeError(f"expected exactly 1 portal_users row for {JIM_EMAIL}, got {len(rows)}")
    return rows[0]


# ─── LINK GENERATION ────────────────────────────────────────────
def generate_recovery_link(email: str) -> str:
    """One-time recovery link via the Auth Admin API, with 429 backoff.
    Returns the action_link. Raises on any terminal failure."""
    payload = {
        "type": "recovery",
        "email": email,
        "options": {"redirect_to": REDIRECT_TO},
    }
    headers = {
        "apikey": ORION_SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {ORION_SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }
    for attempt in range(1, MAX_RETRIES + 1):
        resp = requests.post(GENERATE_LINK_URL, headers=headers, json=payload, timeout=30)
        if resp.status_code == 429:
            wait = float(resp.headers.get("Retry-After", 2 ** attempt))
            log.warning(f"429 from generate_link for {email} — retry {attempt}/{MAX_RETRIES} in {wait:.0f}s")
            time.sleep(wait)
            continue
        if not resp.ok:
            raise RuntimeError(f"generate_link failed for {email} ({resp.status_code}): {resp.text[:300]}")
        link = resp.json().get("action_link", "")
        if not link:
            raise RuntimeError(f"generate_link returned no action_link for {email}")
        return link
    raise RuntimeError(f"generate_link for {email} still rate-limited after {MAX_RETRIES} retries")


def redirect_preserved(link: str) -> bool:
    """True if GoTrue kept our redirect_to; False means the auth URL
    allow-list dropped it and substituted the Site URL — a broken link."""
    qs = parse_qs(urlparse(link).query)
    return qs.get("redirect_to", [""])[0] == REDIRECT_TO


# ─── EMAIL CONTENT (copy supplied verbatim in the 2026-08-14 build
#     brief — place it, do not rewrite it) ──────────────────────
def first_name(full: str) -> str:
    # Same convention as send_pll_digest.py.
    return (full or '').split()[0] if full else 'there'


def build_text_body(name: str, sign_in_link: str) -> str:
    return f"""Hi {first_name(name)},

You now have access to ORiON, the system we're using to track P&C
projects going forward. It replaces the spreadsheet tracking with one
place to see status, deadlines, and updates in real time.

Use the link below to sign in and set your password. It's a one-time
link tied to your account, so use it soon after this email arrives.

{sign_in_link}

Once you're in, there's a short guided tour on your first visit, and
a help widget (the Rigel icon) if you get stuck. Michele's team is
also starting a daily digest of project activity, so ORiON is now
the system of record for status.

Questions, or if anything doesn't work right, reply here or ping me
directly.

Jim"""


def build_html_body(name: str, sign_in_link: str) -> str:
    para = (f'<tr><td style="padding:0 0 16px 0;font-family:{FONT};font-size:14px;'
            f'color:{BODY_TEXT};line-height:1.55;">')
    link_esc = html.escape(sign_in_link, quote=True)
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width"></head>
<body style="margin:0;padding:0;background-color:#f0f2f0;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f0f2f0;">
<tr><td align="center" style="padding:24px 12px;">
<table role="presentation" width="{EMAIL_WIDTH}" cellpadding="0" cellspacing="0" border="0" style="width:{EMAIL_WIDTH}px;max-width:100%;background-color:#ffffff;">
  <tr><td style="background-color:{HEADER_DEEP};padding:18px 28px;">
    <span style="font-family:{FONT};font-size:20px;font-weight:bold;letter-spacing:2px;color:#ffffff;">ORiON</span>
    <span style="font-family:{FONT};font-size:12px;letter-spacing:1px;color:{ION};">&nbsp;&nbsp;OFS TRAINING</span>
  </td></tr>
  <tr><td style="font-size:0;line-height:0;height:4px;background-color:{ION};">&nbsp;</td></tr>
  <tr><td style="padding:26px 28px 8px 28px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
      {para}Hi {html.escape(first_name(name))},</td></tr>
      {para}You now have access to ORiON, the system we're using to track P&amp;C
projects going forward. It replaces the spreadsheet tracking with one
place to see status, deadlines, and updates in real time.</td></tr>
      {para}Use the link below to sign in and set your password. It's a one-time
link tied to your account, so use it soon after this email arrives.</td></tr>
      <tr><td style="padding:2px 0 18px 0;">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0">
          <tr><td style="background-color:{EVERGREEN};">
            <a href="{link_esc}" target="_blank"
               style="display:inline-block;padding:11px 26px;font-family:{FONT};font-size:14px;
                      font-weight:bold;color:#ffffff;text-decoration:none;">Sign In &amp; Set Your Password</a>
          </td></tr>
        </table>
      </td></tr>
      {para}Once you're in, there's a short guided tour on your first visit, and
a help widget (the Rigel icon) if you get stuck. Michele's team is
also starting a daily digest of project activity, so ORiON is now
the system of record for status.</td></tr>
      {para}Questions, or if anything doesn't work right, reply here or ping me
directly.</td></tr>
      {para}Jim</td></tr>
      <tr><td style="border-top:1px solid {BORDER};padding:16px 0 4px 0;font-family:{FONT};
                     font-size:12px;color:{MUTED};line-height:1.5;">
        &mdash; ORiON
      </td></tr>
    </table>
  </td></tr>
</table>
</td></tr>
</table>
</body>
</html>"""


# ─── SEND ───────────────────────────────────────────────────────
def send_email(to: str, subject: str, html_body: str, text_body: str) -> None:
    payload = {
        'from':    FROM_EMAIL,
        'to':      [to],
        'subject': subject,
        'html':    html_body,
        'text':    text_body,
    }
    resp = requests.post(
        RESEND_API_URL,
        headers={'Authorization': f'Bearer {RESEND_API_KEY}',
                 'Content-Type': 'application/json'},
        json=payload,
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(f"Resend send failed ({resp.status_code}): {resp.text[:300]}")


# ─── MAIN ───────────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(description="ORiON onboarding email (recovery-link) sender")
    p.add_argument("--target", choices=["test", "live"], default="test",
                   help="test (default): one email to Jim using Jim's own account. "
                        "live: every portal_users row with --role, real links, real "
                        "sends. Live is Jim's manual trigger only.")
    p.add_argument("--role", default="tpm",
                   help="portal_users.role to onboard in live mode (default: tpm). "
                        "Exists so PLL onboarding later is a flag, not a new script.")
    p.add_argument("--render-only", action="store_true",
                   help="Render the email with a placeholder link and print it. No "
                        "network calls, no secrets needed. For copy verification.")
    return p.parse_args()


def main():
    args = parse_args()

    if args.render_only:
        placeholder = "https://example.invalid/SIGN_IN_LINK_PLACEHOLDER"
        print("===== SUBJECT =====")
        print(f"[TEST] {SUBJECT}" if args.target == "test" else SUBJECT)
        print("===== TEXT BODY =====")
        print(build_text_body("Jim Rosen", placeholder))
        print("===== HTML BODY =====")
        print(build_html_body("Jim Rosen", placeholder))
        return

    if not ORION_SUPABASE_SERVICE_KEY:
        raise SystemExit("ERROR: ORION_SUPABASE_SERVICE_KEY environment variable is not set.")
    if not RESEND_API_KEY:
        raise SystemExit("ERROR: RESEND_API_KEY environment variable is not set.")

    live = args.target == "live"
    log.info(f"Mode: {'LIVE — real onboarding send' if live else 'TEST (one email, to Jim only)'}"
             f" | role={args.role} | redirect_to={REDIRECT_TO}")

    try:
        db = create_client(SUPABASE_URL, ORION_SUPABASE_SERVICE_KEY)
        recipients = fetch_recipients(db, args.role) if live else [fetch_test_recipient(db)]
    except Exception as e:
        log.error(f"Supabase fetch failed — aborting before any link generation: {e}")
        sys.exit(1)
    log.info(f"{len(recipients)} recipient(s): {', '.join(r['email'] for r in recipients)}")

    # Phase 1 — generate ALL links first, then validate redirect
    # preservation, and only then send anything. A broken redirect on any
    # link means the auth allow-list is wrong for every link, so live
    # mode must refuse to send 9 broken emails rather than discover the
    # problem after the third send.
    links: dict[str, str] = {}
    failures: list[str] = []
    for i, user in enumerate(recipients):
        if i:
            time.sleep(CALL_DELAY_SECONDS)
        try:
            link = generate_recovery_link(user['email'])
            links[user['id']] = link
            log.info(f"generated link for {user['email']} ({link_ref(link)}, "
                     f"redirect_preserved={redirect_preserved(link)})")
        except Exception as e:
            failures.append(f"generate:{user['email']}: {e}")
            log.error(f"link generation failed for {user['email']}: {e}")

    broken = [u['email'] for u in recipients
              if u['id'] in links and not redirect_preserved(links[u['id']])]
    if broken:
        msg = (f"redirect_to was NOT preserved for {len(broken)}/{len(links)} links — "
               f"the Supabase Auth URL allow-list is missing {REDIRECT_TO} "
               f"(dashboard > Authentication > URL Configuration; see orion-pll "
               f"knowledge/decisions/2026-08-04-redirect-fix.md). These links land on "
               f"the legacy Site URL fallback instead of the reset page.")
        if live:
            log.error(f"REFUSING LIVE SEND: {msg}")
            sys.exit(1)
        log.warning(f"TEST MODE CONTINUING DESPITE BROKEN REDIRECT: {msg}")

    if live and failures:
        log.error(f"REFUSING LIVE SEND: {len(failures)} link generation failure(s) — "
                  f"live is all-or-nothing before the first send.")
        sys.exit(1)

    # Phase 2 — send.
    sent = 0
    for i, user in enumerate(recipients):
        if user['id'] not in links:
            continue   # generation failure already recorded
        if i:
            time.sleep(CALL_DELAY_SECONDS)
        to = user['email'] if live else JIM_EMAIL
        subject = SUBJECT if live else f"[TEST] {SUBJECT}"
        try:
            send_email(to, subject,
                       build_html_body(user['name'], links[user['id']]),
                       build_text_body(user['name'], links[user['id']]))
            sent += 1
            log.info(f"sent to {to} ({link_ref(links[user['id']])})")
        except Exception as e:
            failures.append(f"send:{to}: {e}")
            log.error(f"send failed for {to}: {e}")

    print(f"Onboarding run complete ({args.target}): {len(recipients)} recipient(s), "
          f"{len(links)} link(s) generated, {sent} email(s) sent, "
          f"{len(failures)} failure(s).")
    if failures:
        # Exit non-zero at the END having attempted everyone — a green run
        # that silently dropped a recipient is the repo's cardinal sin
        # (lessons.md), and an early abort would make a rerun double-send
        # the people before the failure point.
        for f in failures:
            print(f"  FAILED — {f}")
        sys.exit(1)


if __name__ == '__main__':
    main()
