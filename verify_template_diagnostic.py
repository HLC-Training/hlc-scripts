#!/usr/bin/env python3
"""
Deliverability diagnostic: render the TPM welcome template and send to
jim.rosen@gevernova.com (mailbox known to receive from this sender daily).
Tests whether template filtering or recipient-group filtering explains
zero TPM confirmations despite Resend showing all 9 as delivered.

Uses the EXACT render path from send_tpm_onboarding.py. Sends one email
only, to Jim's real inbox. No account state touched.
"""

import os
import sys
import requests
from pathlib import Path

# Import render functions from the canonical script — do NOT duplicate them
sys.path.insert(0, str(Path(__file__).parent / 'scripts'))
from send_tpm_onboarding import (
    build_text_body,
    build_html_body,
    SUBJECT,
    FROM_EMAIL,
    REPLY_TO_EMAIL,
    RESEND_API_URL,
)

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
JIM_REAL_INBOX = "jim.rosen@gevernova.com"


def send_email(to: str, subject: str, html_body: str, text_body: str) -> dict:
    """Send via Resend. Returns the response."""
    payload = {
        'from':     FROM_EMAIL,
        'to':       [to],
        'reply_to': REPLY_TO_EMAIL,
        'subject':  subject,
        'html':     html_body,
        'text':     text_body,
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
    return resp.json()


def main():
    if not RESEND_API_KEY:
        raise SystemExit("ERROR: RESEND_API_KEY environment variable is not set.")

    # Placeholder credentials for rendering (never used to touch accounts)
    placeholder_name = "Jim Rosen"
    placeholder_link = "https://orion.ofstraining.com/reset-password?access_token=PLACEHOLDER_TOKEN"

    # Render with the canonical functions
    text_body = build_text_body(placeholder_name, placeholder_link)
    html_body = build_html_body(placeholder_name, placeholder_link)

    print("===== DIAGNOSTIC SEND =====")
    print(f"From: {FROM_EMAIL}")
    print(f"Reply-To: {REPLY_TO_EMAIL}")
    print(f"To: {JIM_REAL_INBOX}")
    print(f"Subject: {SUBJECT}")
    print()
    print("===== RENDERED TEXT BODY =====")
    print(text_body)
    print()
    print("===== RENDERED HTML BODY (first 500 chars) =====")
    print(html_body[:500])
    print("...")
    print()

    # Send exactly one email
    print("Sending...")
    result = send_email(JIM_REAL_INBOX, SUBJECT, html_body, text_body)

    message_id = result.get('id', 'UNKNOWN')
    print()
    print("===== SEND COMPLETE =====")
    print(f"Resend Message ID: {message_id}")
    print(f"To: {JIM_REAL_INBOX}")
    print(f"Status at send time: Check Resend dashboard for delivery status")
    print()
    print("NOTE: Jim will verify receipt in his actual mailbox. Do not infer")
    print("      arrival from Resend's 'delivered' status — that signal has been")
    print("      misleading for three days on this sender.")


if __name__ == '__main__':
    main()
