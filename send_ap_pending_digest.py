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
  - Reconciliation (2026-09-08, bug 424c1637, decision 2026-09-08-ap-
    pending-clear-and-direction.md): before rendering, every logged field
    is judged against the LIVE ap_tracker row with ap_pending.py — the
    same module sync_ap.py clears the flag with. A field whose tracker
    value already equals the ORiON value (matched), or whose tracker row
    was edited AFTER the ORiON edit (tracker_newer — Jen's later edit
    wins), is dropped from the reason lines; a row with nothing left
    pending is dropped from the email entirely, so Jen is never asked to
    re-apply something she already reconciled or to undo her own newer
    edit. The digest never writes the flag — the sync clears it on its
    next run with the same rule; this filter only keeps the two from
    disagreeing in the window between.
  - Fixtures never render: ap_pending.is_fixture() (AP-99xx range, "TEST
    FIXTURE" titles) filters both sections.
  - Removed-from-tracker section lists rows that are still OPEN in ORiON
    (the section's own wording) — a closed row that later dropped out of
    the sheet is not something Jen needs to review.
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
from ap_pending import (
    TERMINAL_STATUSES, is_fixture, in_episode, latest_episode_entries,
    classify_pending, row_is_reconciled, pick_tracker_row,
)

# ─── CONFIG ─────────────────────────────────────────────────────
SUPABASE_URL         = "https://czdkctjbejnwuopigxta.supabase.co"
# Project-scoped name (bug d2f44099, matching sync_ap.py / bug 306cea89) — the
# generic "SUPABASE_SERVICE_KEY" risked resolving to the wrong project's key
# across a three-project Supabase estate (this ORiON project, SAM COS,
# GreenThumb).
ORION_SUPABASE_SERVICE_KEY = os.environ.get("ORION_SUPABASE_SERVICE_KEY", "")
RESEND_API_KEY       = os.environ.get("RESEND_API_KEY", "")

ORION_URL            = "https://orion.ofstraining.com"

RESEND_API_URL = "https://api.resend.com/emails"
FROM_EMAIL     = "orion@ofstraining.com"

JENNIFER_EMAIL = "jennifer.b.wright@gevernova.com"
JIM_EMAIL      = "jim.rosen@gevernova.com"

# Same threshold as sync_ap.py's ESCALATION_DAYS — a row still pending
# past this many days gets a visible callout in the digest.
AGE_CALLOUT_DAYS = 14

LOG_FILE = Path(__file__).parent / "send_ap_pending_digest.log"

if not ORION_SUPABASE_SERVICE_KEY:
    raise SystemExit("ERROR: ORION_SUPABASE_SERVICE_KEY environment variable is not set.")

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
        .select('id, ap_number, action_text, status, due_date, original_due_date, ap_pending_since, owner_id, ap_is_parent, ap_is_child') \
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
        .select('id, ap_number, title, status, target_end_date, original_target_date, ap_pending_since, owner_id, ap_is_parent, ap_is_child') \
        .eq('ap_pending_update', True) \
        .execute()
    rows = resp.data or []
    for r in rows:
        r['module']            = 'pc'
        r['action_text']       = r.pop('title')
        r['due_date']          = r.pop('target_end_date')
        r['original_due_date'] = r.pop('original_target_date')
    return rows


def fetch_tracker_rows(db, ap_numbers: set) -> dict:
    """ap_number -> list of live ap_tracker rows (a number can carry more
    than one: parent/child twins, or a stale sibling a Smartsheet renumber
    left behind — pick_tracker_row() chooses per module row)."""
    aps = sorted(a for a in ap_numbers if a)
    if not aps:
        return {}
    cols = ('id, ap_number, is_parent, is_child, smartsheet_row_id, smartsheet_modified_at, '
            'last_synced_at, improvement, description, overall_status, sqdcgp, '
            'start_date_only, current_finish')
    by_ap: dict = {}
    for i in range(0, len(aps), 100):
        resp = db.table('ap_tracker').select(cols).in_('ap_number', aps[i:i + 100]).execute()
        for t in (resp.data or []):
            by_ap.setdefault(t['ap_number'], []).append(t)
    return by_ap


def reconcile_rows(rows: list[dict], reasons_by_item: dict, tracker_by_ap: dict) -> tuple[list[dict], list[dict]]:
    """Apply the shared per-field rule. Returns (still_pending_rows,
    dropped_rows). Each kept row gets its `reasons` trimmed to entries for
    fields still pending and a `field_states` map for the log. Rows with no
    logged mirrored edits are kept as-is (nothing to judge — the sync's
    legacy whole-row rule owns them)."""
    kept, dropped = [], []
    for r in rows:
        module = r['module']
        entries = reasons_by_item.get(r['id'], [])
        episode = latest_episode_entries(module, entries, r.get('ap_pending_since'))
        if not episode:
            r['field_states'] = {}
            kept.append(r)
            continue
        pure_parent = bool(r.get('ap_is_parent')) and not bool(r.get('ap_is_child'))
        candidates = tracker_by_ap.get(r['ap_number'], [])
        if len(candidates) > 1:
            log.warning(f"{r['ap_number']}: {len(candidates)} ap_tracker rows share this AP number — comparing against the freshest same-shape row")
        tracker = pick_tracker_row(candidates, pure_parent)
        if tracker is None:
            log.warning(f"{r['ap_number']}: no ap_tracker row — cannot judge reconciliation, listing as pending")
        states = classify_pending(module, tracker, episode, lambda _f, entry: entry.get('new_value'))
        r['field_states'] = states
        open_fields = {f for f, s in states.items() if s in ('pending', 'not_comparable')}
        if row_is_reconciled(states) or not open_fields:
            dropped.append(r)
            log.info(f"{r['ap_number']} ({module}): reconciled — not listed ({', '.join(f'{f}: {s}' for f, s in sorted(states.items()))})")
            continue
        settled = {f: s for f, s in states.items() if f not in open_fields}
        if settled:
            log.info(f"{r['ap_number']} ({module}): {len(settled)} field(s) settled, not shown ({', '.join(f'{f}: {s}' for f, s in sorted(settled.items()))})")
        r['reasons'] = [e for e in entries if e.get('field') in open_fields]
        kept.append(r)
    return kept, dropped


def fetch_orphaned_delivery_rows(db) -> list[dict]:
    resp = db.table('action_items') \
        .select('id, ap_number, action_text, status, due_date, ap_orphaned_since, owner_id') \
        .eq('source', 'ap_import') \
        .eq('ap_orphaned', True) \
        .execute()
    rows = resp.data or []
    for r in rows:
        r['module'] = 'delivery'
    return rows


def fetch_orphaned_pc_rows(db) -> list[dict]:
    resp = db.table('pc_projects') \
        .select('id, ap_number, title, status, target_end_date, ap_orphaned_since, owner_id') \
        .eq('source', 'ap_synced') \
        .eq('ap_orphaned', True) \
        .execute()
    rows = resp.data or []
    for r in rows:
        r['module']   = 'pc'
        r['action_text'] = r.pop('title')
        r['due_date'] = r.pop('target_end_date')
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
    # Episode membership is the shared rule (ap_pending.in_episode) so the
    # digest's reasons and the sync's settle judgement see the same entries.
    return {
        item_id: [e for e in entries if in_episode(e, since_by_item.get(item_id))]
        for item_id, entries in by_item.items()
    }


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
            # reconcile_rows() trims r['reasons'] to still-pending fields;
            # rows it never judged (no logged edits) fall back to the raw list.
            'reasons':       r.get('reasons', reasons_by_item.get(r['id'], [])),
        })
    # Oldest (most overdue) first — the rows most in need of attention lead.
    enriched.sort(key=lambda r: r['age_days'] if r['age_days'] is not None else -1, reverse=True)
    return enriched


# ─── RENDERING ──────────────────────────────────────────────────
def esc(value) -> str:
    return html.escape(str(value)) if value not in (None, '') else '—'


def build_subject(count: int, orphan_count: int = 0) -> str:
    subject = f"ORiON — AP updates pending your review ({count} item{'s' if count != 1 else ''})"
    if orphan_count:
        subject += f" + {orphan_count} removed from tracker"
    return subject


def enrich_orphans(rows: list[dict], owner_names: dict) -> list[dict]:
    now = datetime.now(timezone.utc)
    enriched = []
    for r in rows:
        since = parse_timestamp(r['ap_orphaned_since']) if r.get('ap_orphaned_since') else None
        enriched.append({
            **r,
            'owner_name':     owner_names.get(r.get('owner_id'), 'Unknown'),
            'orphaned_since': since,
            'orphan_age':     (now - since).days if since else None,
        })
    enriched.sort(key=lambda r: r['orphan_age'] if r['orphan_age'] is not None else -1, reverse=True)
    return enriched


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


def build_links_section() -> str:
    smartsheet_url = "https://app.smartsheet.com/sheets/JGw3fHfjgF57PqF8Jm4Q7f74V6FcgxC97FQ36fj1"
    orion_ops_url = f"{ORION_URL}/operations"
    return (
        "<p style=\"margin-top:24px;\">"
        f"<a href=\"{smartsheet_url}\" style=\"margin-right:16px;color:#0066cc;text-decoration:none;\">"
        "Open the AP Tracker in Smartsheet</a> | "
        f"<a href=\"{orion_ops_url}\" style=\"margin-left:16px;color:#0066cc;text-decoration:none;\">"
        "Review in ORiON</a>"
        "</p>"
    )


def build_orphan_html_section(orphans: list[dict]) -> str:
    if not orphans:
        return ""
    intro = (
        f"<p style=\"margin-top:24px;\"><strong>Removed from the tracker — needs review "
        f"({len(orphans)} item{'s' if len(orphans) != 1 else ''}).</strong> "
        "These AP items are still open in ORiON but their AP number is no longer "
        "in the Action Plan Tracker (the row was deleted). Nothing has been "
        "changed in ORiON — please review whether the deletion was intentional "
        "and either close the ORiON item or restore the tracker row.</p>"
    )
    header = (
        "<tr>"
        "<th>Module</th><th>AP #</th><th>Action</th><th>Status</th><th>Owner</th>"
        "<th>Due Date</th><th>Not in tracker since</th>"
        "</tr>"
    )
    body_rows = []
    for r in orphans:
        since_str = r['orphaned_since'].strftime('%Y-%m-%d') if r['orphaned_since'] else '—'
        if r['orphan_age'] is not None:
            since_str += f" ({r['orphan_age']}d)"
        body_rows.append(
            "<tr>"
            f"<td>{esc(module_label(r['module']))}</td>"
            f"<td>{esc(r['ap_number'])}</td>"
            f"<td>{esc(r['action_text'])}</td>"
            f"<td>{esc(r['status'])}</td>"
            f"<td>{esc(r['owner_name'])}</td>"
            f"<td>{esc(r['due_date'])}</td>"
            f"<td>{esc(since_str)}</td>"
            "</tr>"
        )
    table = (
        '<table cellpadding="6" cellspacing="0" '
        'style="border-collapse:collapse;font-family:Calibri,Arial,sans-serif;font-size:13px;">'
        f"{header}{''.join(body_rows)}"
        "</table>"
    )
    return intro + table


def build_html_body(rows: list[dict], orphans: list[dict] | None = None) -> str:
    orphans = orphans or []
    if not rows:
        intro = ""
    else:
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
    if rows:
        table = (
            '<table cellpadding="6" cellspacing="0" '
            'style="border-collapse:collapse;font-family:Calibri,Arial,sans-serif;font-size:13px;">'
            f"{header}{''.join(body_rows)}"
            "</table>"
        )
        legend = f"<p style=\"font-size:12px;color:#555555;\">⚠ = pending more than {AGE_CALLOUT_DAYS} days</p>"
    else:
        table = ""
        legend = ""
    style = (
        "table, th, td { border: 1px solid #999999; }"
        "th { background-color: #eeeeee; text-align: left; }"
    )
    links_section = build_links_section()
    orphan_section = build_orphan_html_section(orphans)
    return f"<html><head><style>{style}</style></head><body>{intro}{table}{legend}{links_section}{orphan_section}</body></html>"


def build_text_body(rows: list[dict], orphans: list[dict] | None = None) -> str:
    orphans = orphans or []
    lines = []
    if rows:
        lines += [
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
    if orphans:
        lines += [
            f"REMOVED FROM THE TRACKER — NEEDS REVIEW ({len(orphans)} item{'s' if len(orphans) != 1 else ''})",
            "These AP items are still open in ORiON but their AP number is no",
            "longer in the Action Plan Tracker (the row was deleted). Nothing has",
            "been changed in ORiON — please review whether the deletion was",
            "intentional and either close the ORiON item or restore the tracker row.",
            "",
        ]
        for r in orphans:
            since_str = r['orphaned_since'].strftime('%Y-%m-%d') if r['orphaned_since'] else '—'
            age_str = f" ({r['orphan_age']}d)" if r['orphan_age'] is not None else ""
            lines.append(f"[{module_label(r['module'])}] {r['ap_number']} — {r['action_text']}")
            lines.append(
                f"  Status: {r['status'] or '—'} | Owner: {r['owner_name']} | "
                f"Due: {r['due_date'] or '—'} | Not in tracker since: {since_str}{age_str}"
            )
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
    db = create_client(SUPABASE_URL, ORION_SUPABASE_SERVICE_KEY)

    def _not_fixture(r: dict) -> bool:
        if is_fixture(r.get('ap_number'), r.get('action_text')):
            log.warning(f"{r.get('ap_number')} ({r['module']}): test fixture excluded from the digest — {r.get('action_text')!r}")
            return False
        return True

    pending_raw = [r for r in fetch_pending_delivery_rows(db) + fetch_pending_pc_rows(db) if _not_fixture(r)]
    orphan_raw  = [r for r in fetch_orphaned_delivery_rows(db) + fetch_orphaned_pc_rows(db) if _not_fixture(r)]

    # Reconcile against the live tracker (shared rule with sync_ap.py).
    reasons_by_item = fetch_change_log_reasons(db, pending_raw)
    tracker_by_ap = fetch_tracker_rows(db, {r['ap_number'] for r in pending_raw})
    rows, reconciled = reconcile_rows(pending_raw, reasons_by_item, tracker_by_ap)
    if reconciled:
        log.info(f"{len(reconciled)} flagged row(s) already reconciled or superseded in the tracker — omitted: "
                 + ", ".join(sorted(r['ap_number'] for r in reconciled)))

    # Removed-from-tracker: only rows still open in ORiON (the section says
    # so); a closed row that was later deleted from the sheet needs no review.
    orphan_rows = []
    for r in orphan_raw:
        if r.get('status') in TERMINAL_STATUSES[r['module']]:
            log.info(f"{r['ap_number']} ({r['module']}): orphaned but already {r['status']} in ORiON — not listed")
            continue
        orphan_rows.append(r)

    if not rows and not orphan_rows:
        log.info("No open AP rows flagged ap_pending_update or ap_orphaned (Delivery or P&C) — nothing to send.")
        print("No flagged rows — digest skipped.")
        return

    delivery_rows = [r for r in rows if r['module'] == 'delivery']
    pc_rows = [r for r in rows if r['module'] == 'pc']
    delivery_owner_ids = {r['owner_id'] for r in delivery_rows + [o for o in orphan_rows if o['module'] == 'delivery'] if r.get('owner_id')}
    pc_owner_ids = {r['owner_id'] for r in pc_rows + [o for o in orphan_rows if o['module'] == 'pc'] if r.get('owner_id')}
    owner_names = fetch_owner_names(db, delivery_owner_ids, pc_owner_ids)
    enriched = enrich_rows(rows, owner_names, reasons_by_item)
    enriched_orphans = enrich_orphans(orphan_rows, owner_names)

    subject = build_subject(len(enriched), len(enriched_orphans))
    html_body = build_html_body(enriched, enriched_orphans)
    text_body = build_text_body(enriched, enriched_orphans)
    flagged_count = sum(1 for r in enriched if r['flagged'])

    if args.dry_run:
        print(f"Subject: {subject}\n")
        print(text_body)
        log.info(f"[DRY RUN] {len(enriched)} row(s), {flagged_count} over {AGE_CALLOUT_DAYS}d, {len(enriched_orphans)} orphan(s), {len(reconciled)} reconciled row(s) omitted — not sent.")
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
