#!/usr/bin/env python3
"""
send_ap_pending_digest.py
─────────────────────────────────────────────────────────────────
Daily digest to Jen Wright of AP rows flagged ap_pending_update — PLL
edits in ORiON that sync_ap.py has not yet seen the matching Smartsheet
change for. Closes SAM COS bug 4e36b85d (JENNIFER_EMAIL defined and
never used).

Flow:
  - Reads action_items where source = 'ap_import' AND
    ap_pending_update = true from ORiON Supabase. Read-only — never
    writes to action_items or Smartsheet. Delivery-only in v1;
    pc_projects has no pending columns until AP lifecycle step 2 (see
    orion/knowledge/decisions/2026-07-31-ap-lifecycle-step2-design.md).
  - Zero flagged rows → exits without sending (daily cadence is the
    dedup; no additional state needed).
  - Rows pending more than AGE_CALLOUT_DAYS get a visible callout.
  - The change-log/reason table doesn't exist yet (step 2) — each row
    shows a "Reason: not yet captured" placeholder so the template
    doesn't need a rewrite once reasons exist.
  - Sent via Resend (same endpoint/from-address as orion/lib/resend.ts).
    A send failure raises and fails the job — a missed daily digest
    should page, not vanish silently.

Schedule: GitHub Actions (.github/workflows/ap-pending-digest.yml) —
daily 12:00 UTC (7:00 AM Houston, CDT).
─────────────────────────────────────────────────────────────────
"""

import os
import html
import logging
import argparse
import requests
from datetime import datetime, timezone
from pathlib import Path
from supabase import create_client

# ─── CONFIG ─────────────────────────────────────────────────────
SUPABASE_URL         = "https://czdkctjbejnwuopigxta.supabase.co"
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
RESEND_API_KEY       = os.environ.get("RESEND_API_KEY", "")

RESEND_API_URL = "https://api.resend.com/emails"
FROM_EMAIL     = "orion@ofstraining.com"

JENNIFER_EMAIL = "jennifer.b.wright@gevernova.com"
JIM_EMAIL      = "jim.rosen@gevernova.com"

# Same threshold as sync_ap.py's ESCALATION_DAYS — a row still pending
# past this many days gets a visible callout in the digest.
AGE_CALLOUT_DAYS = 14

LOG_FILE = Path(__file__).parent / "send_ap_pending_digest.log"

if not SUPABASE_SERVICE_KEY:
    raise SystemExit("ERROR: SUPABASE_SERVICE_KEY environment variable is not set.")

# ─── LOGGING ────────────────────────────────────────────────────
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
# Echo to stdout so warnings/errors are visible in the GitHub Actions run
# log — the logfile above is discarded with the runner.
_console = logging.StreamHandler()
_console.setFormatter(logging.Formatter(
    '%(asctime)s | %(levelname)s | %(message)s', '%Y-%m-%d %H:%M:%S'))
logging.getLogger().addHandler(_console)
log = logging.getLogger(__name__)


# ─── DATA ───────────────────────────────────────────────────────
def fetch_pending_rows(db) -> list[dict]:
    resp = db.table('action_items') \
        .select('ap_number, action_text, status, due_date, original_due_date, ap_pending_since, owner_id') \
        .eq('source', 'ap_import') \
        .eq('ap_pending_update', True) \
        .execute()
    return resp.data or []


def fetch_owner_names(db, owner_ids: set) -> dict:
    if not owner_ids:
        return {}
    resp = db.table('users').select('id, name').in_('id', list(owner_ids)).execute()
    return {u['id']: u['name'] for u in resp.data or []}


def parse_timestamp(raw: str) -> datetime:
    ts = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def enrich_rows(rows: list[dict], owner_names: dict) -> list[dict]:
    now = datetime.now(timezone.utc)
    enriched = []
    for r in rows:
        pending_since = parse_timestamp(r['ap_pending_since']) if r.get('ap_pending_since') else None
        age_days = (now - pending_since).days if pending_since else None
        enriched.append({
            **r,
            'owner_name':    owner_names.get(r.get('owner_id'), 'Unknown'),
            'pending_since': pending_since,
            'age_days':      age_days,
            'flagged':       age_days is not None and age_days > AGE_CALLOUT_DAYS,
        })
    # Oldest (most overdue) first — the rows most in need of attention lead.
    enriched.sort(key=lambda r: r['age_days'] if r['age_days'] is not None else -1, reverse=True)
    return enriched


# ─── RENDERING ──────────────────────────────────────────────────
def esc(value) -> str:
    return html.escape(str(value)) if value not in (None, '') else '—'


def build_subject(count: int) -> str:
    return f"ORiON — AP updates pending your review ({count} item{'s' if count != 1 else ''})"


def build_html_body(rows: list[dict]) -> str:
    intro = (
        f"<p>{len(rows)} AP item(s) have been edited in ORiON and are "
        f"waiting for the matching update in the Action Plan Tracker.</p>"
    )
    header = (
        "<tr>"
        "<th>AP #</th><th>Action</th><th>Status</th><th>Owner</th>"
        "<th>Due Date</th><th>Orig. Due</th><th>Pending Since</th>"
        "<th>Age</th><th>Reason</th>"
        "</tr>"
    )
    body_rows = []
    for r in rows:
        age_cell = f"{r['age_days']}d" if r['age_days'] is not None else "—"
        if r['flagged']:
            age_cell += " ⚠"
        pending_since_str = r['pending_since'].strftime('%Y-%m-%d') if r['pending_since'] else '—'
        body_rows.append(
            "<tr>"
            f"<td>{esc(r['ap_number'])}</td>"
            f"<td>{esc(r['action_text'])}</td>"
            f"<td>{esc(r['status'])}</td>"
            f"<td>{esc(r['owner_name'])}</td>"
            f"<td>{esc(r['due_date'])}</td>"
            f"<td>{esc(r['original_due_date'])}</td>"
            f"<td>{esc(pending_since_str)}</td>"
            f"<td>{age_cell}</td>"
            f"<td>not yet captured</td>"
            "</tr>"
        )
    table = (
        '<table cellpadding="6" cellspacing="0" '
        'style="border-collapse:collapse;font-family:Calibri,Arial,sans-serif;font-size:13px;">'
        f"{header}{''.join(body_rows)}"
        "</table>"
    )
    style = (
        "table, th, td { border: 1px solid #999999; }"
        "th { background-color: #eeeeee; text-align: left; }"
    )
    legend = f"<p style=\"font-size:12px;color:#555555;\">⚠ = pending more than {AGE_CALLOUT_DAYS} days</p>"
    return f"<html><head><style>{style}</style></head><body>{intro}{table}{legend}</body></html>"


def build_text_body(rows: list[dict]) -> str:
    lines = [
        f"{len(rows)} AP item(s) have been edited in ORiON and are waiting "
        "for the matching update in the Action Plan Tracker.",
        "",
    ]
    for r in rows:
        pending_since_str = r['pending_since'].strftime('%Y-%m-%d') if r['pending_since'] else '—'
        age_str = f"{r['age_days']} days" if r['age_days'] is not None else 'unknown'
        callout = f" — OVER {AGE_CALLOUT_DAYS} DAYS" if r['flagged'] else ""
        lines.append(f"{r['ap_number']} — {r['action_text']}")
        lines.append(
            f"  Status: {r['status'] or '—'} | Owner: {r['owner_name']} | "
            f"Due: {r['due_date'] or '—'}"
            + (f" (orig: {r['original_due_date']})" if r['original_due_date'] else "")
        )
        lines.append(f"  Pending since: {pending_since_str} ({age_str}){callout}")
        lines.append("  Reason: not yet captured")
        lines.append("")
    return "\n".join(lines)


# ─── SEND ───────────────────────────────────────────────────────
def send_email(to: str, cc: list, subject: str, html_body: str, text_body: str) -> None:
    if not RESEND_API_KEY:
        raise SystemExit("ERROR: RESEND_API_KEY environment variable is not set.")
    payload = {
        'from':    FROM_EMAIL,
        'to':      [to],
        'subject': subject,
        'html':    html_body,
        'text':    text_body,
    }
    if cc:
        payload['cc'] = cc
    resp = requests.post(
        RESEND_API_URL,
        headers={
            'Authorization': f'Bearer {RESEND_API_KEY}',
            'Content-Type':  'application/json',
        },
        json=payload,
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(f"Resend send failed ({resp.status_code}): {resp.text}")


# ─── MAIN ───────────────────────────────────────────────────────
def parse_args():
    parser = argparse.ArgumentParser(description="Daily AP-pending digest email to Jen")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Render the digest and print it without sending (no RESEND_API_KEY required).",
    )
    parser.add_argument(
        "--to",
        default=None,
        help="Override the recipient (default: Jen). Skips the Jim CC when set — for acceptance testing.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    db = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

    rows = fetch_pending_rows(db)
    if not rows:
        log.info("No AP rows flagged ap_pending_update — nothing to send.")
        print("No flagged rows — digest skipped.")
        return

    owner_ids = {r['owner_id'] for r in rows if r.get('owner_id')}
    owner_names = fetch_owner_names(db, owner_ids)
    enriched = enrich_rows(rows, owner_names)

    subject = build_subject(len(enriched))
    html_body = build_html_body(enriched)
    text_body = build_text_body(enriched)
    flagged_count = sum(1 for r in enriched if r['flagged'])

    if args.dry_run:
        print(f"Subject: {subject}\n")
        print(text_body)
        log.info(f"[DRY RUN] {len(enriched)} row(s), {flagged_count} over {AGE_CALLOUT_DAYS}d — not sent.")
        return

    to_email = args.to or JENNIFER_EMAIL
    cc = [] if args.to else [JIM_EMAIL]

    send_email(to_email, cc, subject, html_body, text_body)
    log.info(
        f"Digest sent to {to_email} (cc: {cc or 'none'}) — "
        f"{len(enriched)} row(s), {flagged_count} over {AGE_CALLOUT_DAYS}d."
    )
    print(f"Digest sent to {to_email} — {len(enriched)} row(s).")


if __name__ == '__main__':
    main()
