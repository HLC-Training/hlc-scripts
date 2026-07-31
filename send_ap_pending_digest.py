#!/usr/bin/env python3
"""
send_ap_pending_digest.py
─────────────────────────────────────────────────────────────────
Daily digest to Jen Wright of AP rows flagged ap_pending_update — PLL/TPM
edits in ORiON that sync_ap.py has not yet seen the matching Smartsheet
change for. Closes SAM COS bug 4e36b85d (JENNIFER_EMAIL defined and
never used).

Flow:
  - Reads flagged rows from BOTH action_items (source = 'ap_import',
    module 'delivery') and pc_projects (module 'pc') — AP lifecycle step 2
    gave pc_projects the same ap_pending_update/ap_pending_since columns.
    Read-only — never writes to action_items, pc_projects, or Smartsheet.
  - Zero flagged rows across both modules → exits without sending (daily
    cadence is the dedup; no additional state needed).
  - Rows pending more than AGE_CALLOUT_DAYS get a visible callout.
  - Reasons come from ap_change_log: for each flagged row, entries where
    changed_at >= ap_pending_since (i.e. written during the CURRENT flag
    episode — a cleared-then-re-flagged row's old entries don't leak into
    the new one). A row with zero matching entries — a pre-step-2 flag, or
    a flag set before reason-capture shipped — keeps the "not yet
    captured" placeholder.
  - Sent via Resend (same endpoint/from-address as orion-pll/lib/resend.ts).
    A send failure raises and fails the job — a missed daily digest
    should page, not vanish silently.

Schedule: GitHub Actions (.github/workflows/ap-pending-digest.yml) —
weekdays 12:00 UTC (7:00 AM Houston, CDT).
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
def fetch_pending_delivery_rows(db) -> list[dict]:
    resp = db.table('action_items') \
        .select('id, ap_number, action_text, status, due_date, original_due_date, ap_pending_since, owner_id') \
        .eq('source', 'ap_import') \
        .eq('ap_pending_update', True) \
        .execute()
    rows = resp.data or []
    for r in rows:
        r['module'] = 'delivery'
    return rows


def fetch_pending_pc_rows(db) -> list[dict]:
    # pc_projects has no due_date/original_due_date — its schema (see
    # sync_ap.py's P&C insert/update payloads) uses target_end_date /
    # original_target_date. Normalized into the same due_date/
    # original_due_date keys here so downstream rendering is module-agnostic.
    resp = db.table('pc_projects') \
        .select('id, ap_number, title, status, target_end_date, original_target_date, ap_pending_since, owner_id') \
        .eq('ap_pending_update', True) \
        .execute()
    rows = resp.data or []
    for r in rows:
        r['module']            = 'pc'
        r['action_text']       = r.pop('title')
        r['due_date']          = r.pop('target_end_date')
        r['original_due_date'] = r.pop('original_target_date')
    return rows


def fetch_owner_names(db, delivery_owner_ids: set, pc_owner_ids: set) -> dict:
    # users and portal_users are different tables with table-scoped uuids —
    # merging by id carries no collision risk.
    names = {}
    if delivery_owner_ids:
        resp = db.table('users').select('id, name').in_('id', list(delivery_owner_ids)).execute()
        names.update({u['id']: u['name'] for u in resp.data or []})
    if pc_owner_ids:
        resp = db.table('portal_users').select('id, name').in_('id', list(pc_owner_ids)).execute()
        names.update({u['id']: u['name'] for u in resp.data or []})
    return names


def fetch_change_log_reasons(db, rows: list[dict]) -> dict:
    """item_id -> list of change-log entries, each restricted to this row's
    CURRENT flag episode (changed_at >= that row's ap_pending_since)."""
    ids = [r['id'] for r in rows]
    if not ids:
        return {}
    resp = db.table('ap_change_log').select('*').in_('item_id', ids).order('changed_at').execute()
    by_item: dict = {}
    for entry in (resp.data or []):
        by_item.setdefault(entry['item_id'], []).append(entry)

    since_by_item = {r['id']: r.get('ap_pending_since') for r in rows}
    out = {}
    for item_id, entries in by_item.items():
        since = since_by_item.get(item_id)
        if not since:
            out[item_id] = entries
            continue
        since_ts = parse_timestamp(since)
        out[item_id] = [e for e in entries if parse_timestamp(e['changed_at']) >= since_ts]
    return out


def parse_timestamp(raw: str) -> datetime:
    ts = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def enrich_rows(rows: list[dict], owner_names: dict, reasons_by_item: dict) -> list[dict]:
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
            'reasons':       reasons_by_item.get(r['id'], []),
        })
    # Oldest (most overdue) first — the rows most in need of attention lead.
    enriched.sort(key=lambda r: r['age_days'] if r['age_days'] is not None else -1, reverse=True)
    return enriched


# ─── RENDERING ──────────────────────────────────────────────────
def esc(value) -> str:
    return html.escape(str(value)) if value not in (None, '') else '—'


def build_subject(count: int) -> str:
    return f"ORiON — AP updates pending your review ({count} item{'s' if count != 1 else ''})"


def module_label(module: str) -> str:
    return "P&C" if module == 'pc' else "Delivery"


def reason_lines(r: dict, escaped: bool) -> list[str]:
    if not r['reasons']:
        return ["not yet captured"]
    fmt = esc if escaped else (lambda v: v if v not in (None, '') else '—')
    # ASCII arrow on purpose — same reason as sync_ap.py's divergence
    # rendering: this string is printed on Windows consoles (cp1252), which
    # has no glyph for U+2192 and raises UnicodeEncodeError on print().
    return [
        f"{fmt(e['field'])}: {fmt(e['old_value'])} -> {fmt(e['new_value'])} — {fmt(e['reason'])}"
        for e in r['reasons']
    ]


def build_html_body(rows: list[dict]) -> str:
    intro = (
        f"<p>{len(rows)} AP item(s) have been edited in ORiON and are "
        f"waiting for the matching update in the Action Plan Tracker.</p>"
    )
    header = (
        "<tr>"
        "<th>Module</th><th>AP #</th><th>Action</th><th>Status</th><th>Owner</th>"
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
        reason_cell = "<br>".join(reason_lines(r, escaped=True))
        body_rows.append(
            "<tr>"
            f"<td>{esc(module_label(r['module']))}</td>"
            f"<td>{esc(r['ap_number'])}</td>"
            f"<td>{esc(r['action_text'])}</td>"
            f"<td>{esc(r['status'])}</td>"
            f"<td>{esc(r['owner_name'])}</td>"
            f"<td>{esc(r['due_date'])}</td>"
            f"<td>{esc(r['original_due_date'])}</td>"
            f"<td>{esc(pending_since_str)}</td>"
            f"<td>{age_cell}</td>"
            f"<td>{reason_cell}</td>"
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
        lines.append(f"[{module_label(r['module'])}] {r['ap_number']} — {r['action_text']}")
        lines.append(
            f"  Status: {r['status'] or '—'} | Owner: {r['owner_name']} | "
            f"Due: {r['due_date'] or '—'}"
            + (f" (orig: {r['original_due_date']})" if r['original_due_date'] else "")
        )
        lines.append(f"  Pending since: {pending_since_str} ({age_str}){callout}")
        for rl in reason_lines(r, escaped=False):
            lines.append(f"  Reason: {rl}")
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

    delivery_rows = fetch_pending_delivery_rows(db)
    pc_rows = fetch_pending_pc_rows(db)
    rows = delivery_rows + pc_rows
    if not rows:
        log.info("No AP rows flagged ap_pending_update (Delivery or P&C) — nothing to send.")
        print("No flagged rows — digest skipped.")
        return

    delivery_owner_ids = {r['owner_id'] for r in delivery_rows if r.get('owner_id')}
    pc_owner_ids = {r['owner_id'] for r in pc_rows if r.get('owner_id')}
    owner_names = fetch_owner_names(db, delivery_owner_ids, pc_owner_ids)
    reasons_by_item = fetch_change_log_reasons(db, rows)
    enriched = enrich_rows(rows, owner_names, reasons_by_item)

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
