#!/usr/bin/env python3
"""
send_pll_digest.py
─────────────────────────────────────────────────────────────────
Daily per-PLL action-item digest (SAM COS action item 37425f71).

Weekday mornings (~6:00 America/Chicago), each PLL gets an
ORiON-branded email with up to three sections:
  1. "Added since your last update" — items imported by sync_vault.py
     (source = 'vault_import') since the last digest actually sent.
     Watermark-based, NOT "yesterday": Monday covers the weekend.
  2. "Coming up in the next two weeks" — active items due in <= 14 days.
  3. "Past due" — active items past due, capped at the 5 longest-overdue
     with a "+N more" line.
A PLL with zero items across all three sections gets NO email.
Jim gets one consolidated roll-up per run (not a cc on each email).

WATERMARK (pll_digest_state, ORiON Supabase):
  One row per successful run, inserted only after every send completed
  (or all PLLs correctly no-op'd). Watermark = latest row's sent_at.
  A crashed or partly-failed run inserts nothing, so its window is
  re-covered next run. "New since" compares action_items.created_date
  (the vault header date — the date the item was logged in
  open-actions.md) against the watermark's America/Chicago date;
  last_updated is NOT usable (its BEFORE UPDATE trigger bumps it on any
  later edit). Item ids reported in the previous run's section 1 are
  excluded to prevent boundary-date double-reports. Empty state table →
  72-hour fallback window.

TEST-FIRST GATE (tasks/lessons.md 2026-07-31):
  Default mode is TEST: every rendered digest goes to Jim with a
  [TEST] subject prefix, plus the roll-up. The live-recipient path
  requires --live or PLL_DIGEST_LIVE=true — set deliberately, never
  by default. No PLL receives anything until Jim flips it.

READ-ONLY on all portal/legacy tables. The only table this job writes
is pll_digest_state.

Exit codes: any Supabase/Resend failure exits non-zero having sent as
little as possible — never a green run that silently did nothing.

Schedule: .github/workflows/pll-digest.yml — '7 11 * * 1-5' UTC.
Off-hour minute on purpose: minutes near :00/:30 are the contended
cron slots (lessons.md); 11:07 UTC = 6:07 AM CDT. Weekday + 2026
GE Vernova holiday gate is enforced in-script on the Chicago date
(refresh HOLIDAYS for 2027 — see knowledge/decisions/).
─────────────────────────────────────────────────────────────────
"""

import os
import sys
import html
import json
import logging
import argparse
import requests
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
from zoneinfo import ZoneInfo
from supabase import create_client

# ─── CONFIG ─────────────────────────────────────────────────────
SUPABASE_URL = "https://czdkctjbejnwuopigxta.supabase.co"
# ORION_-prefixed on purpose — the generic SUPABASE_SERVICE_KEY name risks
# resolving to the wrong project across the three-project estate
# (lessons.md 2026-08-03). Do not shorten it.
ORION_SUPABASE_SERVICE_KEY = os.environ.get("ORION_SUPABASE_SERVICE_KEY", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")

RESEND_API_URL = "https://api.resend.com/emails"
FROM_EMAIL     = "orion@ofstraining.com"
JIM_EMAIL      = "jim.rosen@gevernova.com"
ORION_URL      = "https://orion.ofstraining.com"

CHICAGO         = ZoneInfo("America/Chicago")
ACTIVE_STATUSES = ["Open", "In Progress"]
DUE_SOON_DAYS   = 14
OVERDUE_CAP     = 5
FALLBACK_WINDOW_HOURS = 72   # first run ever: no watermark row yet

# FieldCore US Staff/RETX 2026 calendar (sam-drop). HARDCODED for 2026 —
# refresh this list for 2027 before the year turns.
HOLIDAYS = {
    date(2026, 1, 1),    # New Year's Day
    date(2026, 1, 19),   # Martin Luther King Jr. Day
    date(2026, 2, 16),   # Presidents Day
    date(2026, 4, 3),    # Good Friday
    date(2026, 5, 25),   # Memorial Day
    date(2026, 6, 19),   # Juneteenth
    date(2026, 7, 3),    # Independence Day (observed)
    date(2026, 9, 7),    # Labor Day
    date(2026, 11, 11),  # Veterans Day
    date(2026, 11, 26),  # Thanksgiving Day
    date(2026, 11, 27),  # Day After Thanksgiving
    date(2026, 12, 24),  # Christmas Eve
    date(2026, 12, 25),  # Christmas Day
}

# Brand values from orion-pll app/styles/vernova-tokens.css + orion-tokens.css.
# Ion Blue is identity/accent only — status meaning belongs to RAG.
EVERGREEN   = "#005e60"   # --gv-evergreen (canonical brand)
HEADER_DEEP = "#004a4c"   # --gv-evergreen-deep (--or-header)
ION         = "#3fd2ff"   # --or-ion (accent rule/fills only)
ION_INK     = "#0a7ea4"   # --or-ion-ink (text-safe Ion on white)
NIGHT       = "#212121"   # --gv-night (headings)
BODY_TEXT   = "#444444"   # --gv-body
MUTED       = "#888888"   # --gv-muted
CRITICAL    = "#e63946"   # --gv-critical (RAG — past-due dates only)
BORDER      = "#e5e7eb"   # --gv-border
ZEBRA       = "#f8fafa"   # --gv-zebra
EMAIL_WIDTH = 600         # --container-email

LOG_FILE = Path(__file__).parent / "send_pll_digest.log"

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


# ─── DATE GATE ──────────────────────────────────────────────────
def send_decision(d: date) -> tuple[bool, str]:
    """Weekday + holiday gate on a Chicago-local date."""
    if d.weekday() >= 5:
        return False, f"{d} is a {d.strftime('%A')} — weekend, no send."
    if d in HOLIDAYS:
        return False, f"{d} is a GE Vernova 2026 holiday — no send."
    return True, f"{d} is an ordinary weekday — send."


# ─── DATA (all reads; any failure must end the run non-zero) ────
def fetch_plls(db) -> list[dict]:
    resp = db.table('users').select('id, name, email, product_line') \
        .eq('role', 'pll').order('name').execute()
    plls = resp.data or []
    if not plls:
        # 0 PLLs is not a valid state of this system — treat as a failure,
        # not a quiet "nobody to email".
        raise RuntimeError("users query returned zero role='pll' rows")
    return plls


def fetch_items(db, owner_ids: list[str]) -> list[dict]:
    resp = db.table('action_items') \
        .select('id, owner_id, action_text, status, priority, source, '
                'created_date, start_date, due_date') \
        .in_('owner_id', owner_ids) \
        .in_('status', ACTIVE_STATUSES) \
        .execute()
    return resp.data or []


def fetch_state(db) -> tuple[datetime | None, set]:
    """Return (watermark, ids reported in the previous run's section 1)."""
    resp = db.table('pll_digest_state').select('sent_at, summary') \
        .order('sent_at', desc=True).limit(1).execute()
    rows = resp.data or []
    if not rows:
        return None, set()
    watermark = parse_timestamp(rows[0]['sent_at'])
    prev_ids = set((rows[0].get('summary') or {}).get('section1_ids', []))
    return watermark, prev_ids


def parse_timestamp(raw: str) -> datetime:
    ts = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def parse_date(raw) -> date | None:
    return date.fromisoformat(raw) if raw else None


# ─── DIGEST ASSEMBLY ────────────────────────────────────────────
def build_digest(pll: dict, items: list[dict], watermark_date: date,
                 prev_section1_ids: set, today: date) -> dict | None:
    """Three sections for one PLL; None if all empty (→ no email)."""
    own = [i for i in items if i['owner_id'] == pll['id']]

    new_items = sorted(
        (i for i in own
         if i.get('source') == 'vault_import'
         and parse_date(i.get('created_date')) is not None
         and parse_date(i['created_date']) >= watermark_date
         and i['id'] not in prev_section1_ids),
        key=lambda i: i['created_date'],
    )
    due_soon = sorted(
        (i for i in own
         if parse_date(i.get('due_date')) is not None
         and today <= parse_date(i['due_date']) <= today + timedelta(days=DUE_SOON_DAYS)),
        key=lambda i: i['due_date'],
    )
    overdue_all = sorted(
        (i for i in own
         if parse_date(i.get('due_date')) is not None
         and parse_date(i['due_date']) < today),
        key=lambda i: i['due_date'],   # oldest due date first = longest overdue first
    )

    if not new_items and not due_soon and not overdue_all:
        return None

    return {
        'pll':              pll,
        'new_items':        new_items,
        'due_soon':         due_soon,
        'overdue':          overdue_all[:OVERDUE_CAP],
        'overdue_overflow': max(0, len(overdue_all) - OVERDUE_CAP),
    }


def first_name(full: str) -> str:
    return (full or '').split()[0] if full else 'there'


def build_subject(today: date) -> str:
    # Verbatim copy: Your ORiON items for {WeekdayName}, {Month} {D}
    return f"Your ORiON items for {today.strftime('%A')}, {today.strftime('%B')} {today.day}"


# ─── RENDERING ──────────────────────────────────────────────────
FONT = "Calibri,Arial,Helvetica,sans-serif"


def esc(value) -> str:
    return html.escape(str(value)) if value not in (None, '') else '—'


def fmt_date(raw) -> str:
    d = parse_date(raw)
    return d.strftime('%b %d') if d else '—'


def days_overdue(raw, today: date) -> str:
    n = (today - parse_date(raw)).days
    return f"{n} day{'s' if n != 1 else ''} past"


def item_row_html(item: dict, meta: str, meta_color: str, zebra: bool) -> str:
    bg = f'background-color:{ZEBRA};' if zebra else ''
    return (
        f'<tr><td style="{bg}padding:10px 14px;border-bottom:1px solid {BORDER};'
        f'font-family:{FONT};font-size:14px;color:{NIGHT};line-height:1.4;">'
        f'{esc(item["action_text"])}'
        f'<br><span style="font-size:12px;color:{meta_color};">{meta}</span>'
        f'</td></tr>'
    )


def section_html(heading: str, rows: list[str], extra_row: str = '') -> str:
    return (
        f'<tr><td style="padding:22px 0 0 0;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        f'<tr><td style="font-family:{FONT};font-size:16px;font-weight:bold;'
        f'color:{EVERGREEN};padding:0 0 4px 0;">{heading}</td></tr>'
        f'<tr><td style="font-size:0;line-height:0;height:3px;background-color:{ION};'
        f'width:44px;">&nbsp;</td></tr>'
        f'<tr><td style="padding:6px 0 0 0;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        f'{"".join(rows)}{extra_row}'
        f'</table></td></tr>'
        f'</table></td></tr>'
    )


def build_html_body(digest: dict, today: date) -> str:
    pll = digest['pll']
    greeting = (
        f"Hi {esc(first_name(pll['name']))}, here's where your ORiON items stand "
        "this morning. It's a quick daily rundown so nothing slips. If a date has "
        "moved or something's already wrapped up, updating it in ORiON keeps this "
        "list honest."
    )

    sections = []
    if digest['new_items']:
        rows = [
            item_row_html(
                i,
                f"Logged {fmt_date(i.get('created_date'))} &middot; {esc(i.get('status'))}"
                + (f" &middot; due {fmt_date(i['due_date'])}" if i.get('due_date') else ""),
                MUTED, idx % 2 == 1)
            for idx, i in enumerate(digest['new_items'])
        ]
        sections.append(section_html("Added since your last update", rows))
    if digest['due_soon']:
        rows = [
            item_row_html(
                i,
                f"Due {fmt_date(i['due_date'])} &middot; {esc(i.get('status'))}",
                MUTED, idx % 2 == 1)
            for idx, i in enumerate(digest['due_soon'])
        ]
        sections.append(section_html("Coming up in the next two weeks", rows))
    if digest['overdue']:
        rows = [
            item_row_html(
                i,
                f'<span style="color:{CRITICAL};">Due {fmt_date(i["due_date"])} '
                f'&middot; {days_overdue(i["due_date"], today)}</span> '
                f'&middot; {esc(i.get("status"))}',
                MUTED, idx % 2 == 1)
            for idx, i in enumerate(digest['overdue'])
        ]
        extra = ''
        if digest['overdue_overflow']:
            extra = (
                f'<tr><td style="padding:10px 14px;font-family:{FONT};font-size:13px;'
                f'color:{MUTED};">Plus {digest["overdue_overflow"]} more past due in ORiON.</td></tr>'
            )
        sections.append(section_html("Past due", rows, extra))

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
      <tr><td style="font-family:{FONT};font-size:14px;color:{BODY_TEXT};line-height:1.55;">{greeting}</td></tr>
      {''.join(sections)}
      <tr><td style="padding:28px 0 6px 0;font-family:{FONT};font-size:14px;color:{BODY_TEXT};">
        Want the full picture? Open ORiON to see everything on your plate.
      </td></tr>
      <tr><td style="padding:6px 0 22px 0;">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0">
          <tr><td style="background-color:{EVERGREEN};">
            <a href="{ORION_URL}" target="_blank"
               style="display:inline-block;padding:11px 26px;font-family:{FONT};font-size:14px;
                      font-weight:bold;color:#ffffff;text-decoration:none;">Open ORiON</a>
          </td></tr>
        </table>
      </td></tr>
      <tr><td style="border-top:1px solid {BORDER};padding:16px 0 4px 0;font-family:{FONT};
                     font-size:12px;color:{MUTED};line-height:1.5;">
        See something that looks off? Reply to this email and it'll reach the right place.
        <br><br>&mdash; ORiON
      </td></tr>
    </table>
  </td></tr>
</table>
</td></tr>
</table>
</body>
</html>"""


def build_text_body(digest: dict, today: date) -> str:
    pll = digest['pll']
    lines = [
        f"Hi {first_name(pll['name'])}, here's where your ORiON items stand this "
        "morning. It's a quick daily rundown so nothing slips. If a date has moved "
        "or something's already wrapped up, updating it in ORiON keeps this list honest.",
        "",
    ]
    if digest['new_items']:
        lines.append("Added since your last update")
        lines.append("-" * 28)
        for i in digest['new_items']:
            due = f" | due {fmt_date(i['due_date'])}" if i.get('due_date') else ""
            lines.append(f"- {i['action_text']}")
            lines.append(f"    Logged {fmt_date(i.get('created_date'))} | {i.get('status')}{due}")
        lines.append("")
    if digest['due_soon']:
        lines.append("Coming up in the next two weeks")
        lines.append("-" * 31)
        for i in digest['due_soon']:
            lines.append(f"- {i['action_text']}")
            lines.append(f"    Due {fmt_date(i['due_date'])} | {i.get('status')}")
        lines.append("")
    if digest['overdue']:
        lines.append("Past due")
        lines.append("-" * 8)
        for i in digest['overdue']:
            lines.append(f"- {i['action_text']}")
            lines.append(
                f"    Due {fmt_date(i['due_date'])} | "
                f"{days_overdue(i['due_date'], today)} | {i.get('status')}")
        if digest['overdue_overflow']:
            lines.append(f"Plus {digest['overdue_overflow']} more past due in ORiON.")
        lines.append("")
    lines += [
        "Want the full picture? Open ORiON to see everything on your plate.",
        f"Open ORiON: {ORION_URL}",
        "",
        "See something that looks off? Reply to this email and it'll reach the right place.",
        "",
        "-- ORiON",
    ]
    return "\n".join(lines)


def build_rollup(results: list[dict], today: date, live: bool,
                 watermark: datetime | None, watermark_date: date) -> tuple[str, str, str]:
    """Jim's consolidated roll-up: (subject, html, text)."""
    mode = "LIVE" if live else "TEST — nothing sent to PLLs"
    wm = (watermark.astimezone(CHICAGO).strftime('%Y-%m-%d %H:%M %Z')
          if watermark else f"none (fallback window, since {watermark_date})")
    header = (
        '<tr><th style="text-align:left;padding:6px 10px;">PLL</th>'
        '<th style="padding:6px 10px;">New</th><th style="padding:6px 10px;">Due 14d</th>'
        '<th style="padding:6px 10px;">Overdue</th><th style="padding:6px 10px;">Overflow</th>'
        '<th style="text-align:left;padding:6px 10px;">Result</th></tr>'
    )
    body_rows, text_lines = [], []
    for r in results:
        d = r['digest']
        if d is None:
            body_rows.append(
                f'<tr><td style="padding:6px 10px;">{esc(r["pll"]["name"])}</td>'
                f'<td align="center">0</td><td align="center">0</td>'
                f'<td align="center">0</td><td align="center">—</td>'
                f'<td style="padding:6px 10px;">no items — no email</td></tr>')
            text_lines.append(f"{r['pll']['name']}: no items -- no email")
        else:
            n, s, o = len(d['new_items']), len(d['due_soon']), len(d['overdue'])
            ov = d['overdue_overflow'] or '—'
            body_rows.append(
                f'<tr><td style="padding:6px 10px;">{esc(r["pll"]["name"])}</td>'
                f'<td align="center">{n}</td><td align="center">{s}</td>'
                f'<td align="center">{o}</td><td align="center">{ov}</td>'
                f'<td style="padding:6px 10px;">{esc(r["result"])}</td></tr>')
            text_lines.append(
                f"{r['pll']['name']}: new={n} due14={s} overdue={o} "
                f"overflow={d['overdue_overflow']} -> {r['result']}")
    html_body = (
        f'<html><body style="font-family:{FONT};color:{NIGHT};">'
        f'<p><strong>ORiON PLL digest roll-up</strong> — {today.strftime("%A, %B %d, %Y")}<br>'
        f'Mode: <strong>{mode}</strong> · Watermark used: {wm}</p>'
        f'<table cellpadding="0" cellspacing="0" '
        f'style="border-collapse:collapse;font-size:13px;" border="1" bordercolor="{BORDER}">'
        f'{header}{"".join(body_rows)}</table>'
        f'</body></html>'
    )
    text = (
        f"ORiON PLL digest roll-up — {today} — mode: {mode}\n"
        f"Watermark used: {wm}\n\n" + "\n".join(text_lines)
    )
    subject = f"ORiON PLL digest roll-up — {today.strftime('%b %d')} ({'LIVE' if live else 'TEST'})"
    return subject, html_body, text


# ─── SEND ───────────────────────────────────────────────────────
def send_email(to: str, subject: str, html_body: str, text_body: str,
               reply_to: str | None = None) -> None:
    if not RESEND_API_KEY:
        raise SystemExit("ERROR: RESEND_API_KEY environment variable is not set.")
    payload = {
        'from':    FROM_EMAIL,
        'to':      [to],
        'subject': subject,
        'html':    html_body,
        'text':    text_body,
    }
    if reply_to:
        payload['reply_to'] = reply_to
    resp = requests.post(
        RESEND_API_URL,
        headers={'Authorization': f'Bearer {RESEND_API_KEY}',
                 'Content-Type': 'application/json'},
        json=payload,
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(f"Resend send failed ({resp.status_code}): {resp.text}")


# ─── MAIN ───────────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(description="Daily per-PLL ORiON digest")
    p.add_argument("--dry-run", action="store_true",
                   help="Assemble and print digests; send nothing, write no state.")
    p.add_argument("--live", action="store_true",
                   help="LIVE MODE: send to the actual PLLs. Without this flag "
                        "(and without PLL_DIGEST_LIVE=true) everything goes to "
                        "Jim only — test-first, lessons.md 2026-07-31.")
    p.add_argument("--check-date", metavar="YYYY-MM-DD",
                   help="Print the send/no-send decision for a date and exit.")
    p.add_argument("--force-date-gate", action="store_true",
                   help="Bypass the weekday/holiday gate (manual test runs).")
    return p.parse_args()


def main():
    args = parse_args()

    if args.check_date:
        ok, reason = send_decision(date.fromisoformat(args.check_date))
        print(reason)
        return

    # Live requires a deliberate, explicit switch — default is TEST.
    live = args.live or os.environ.get("PLL_DIGEST_LIVE", "").lower() == "true"
    log.info(f"Mode: {'LIVE' if live else 'TEST (all email to Jim only)'}"
             + (" [dry-run]" if args.dry_run else ""))

    now_chicago = datetime.now(CHICAGO)
    today = now_chicago.date()
    ok, reason = send_decision(today)
    if not ok and not args.force_date_gate:
        log.info(reason)
        print(reason)
        return
    if not ok and args.force_date_gate:
        log.info(f"Date gate bypassed by --force-date-gate ({reason})")

    if not ORION_SUPABASE_SERVICE_KEY:
        raise SystemExit("ERROR: ORION_SUPABASE_SERVICE_KEY environment variable is not set.")

    # All reads: any failure exits non-zero. A digest job that silently
    # sends nothing and reports success is the failure mode this repo
    # exists to never repeat (lessons.md).
    try:
        db = create_client(SUPABASE_URL, ORION_SUPABASE_SERVICE_KEY)
        plls = fetch_plls(db)
        items = fetch_items(db, [p['id'] for p in plls])
        watermark, prev_section1_ids = fetch_state(db)
    except Exception as e:
        log.error(f"Supabase fetch failed — aborting without sending: {e}")
        sys.exit(1)

    if watermark is None:
        watermark_date = (now_chicago - timedelta(hours=FALLBACK_WINDOW_HOURS)).date()
        log.info(f"No watermark row — fallback window since {watermark_date}.")
    else:
        watermark_date = watermark.astimezone(CHICAGO).date()

    results = []
    for pll in plls:
        digest = build_digest(pll, items, watermark_date, prev_section1_ids, today)
        results.append({'pll': pll, 'digest': digest, 'result': None})

    to_send = [r for r in results if r['digest'] is not None]
    log.info(f"{len(plls)} PLLs — {len(to_send)} digest(s) to send, "
             f"{len(plls) - len(to_send)} suppressed (no items).")

    if args.dry_run:
        for r in results:
            r['result'] = 'dry-run (not sent)' if r['digest'] is not None else None
            name = r['pll']['name']
            if r['digest'] is None:
                print(f"\n===== {name}: NO EMAIL (no items in any section) =====")
                continue
            print(f"\n===== {name} — subject: {build_subject(today)} =====")
            print(build_text_body(r['digest'], today))
        _, _, rollup_text = build_rollup(results, today, live, watermark, watermark_date)
        print(f"\n===== ROLL-UP (to Jim) =====\n{rollup_text}")
        print("\n[DRY RUN] Nothing sent, no state written.")
        return

    # Send digests. In TEST mode every one goes to Jim, subject-prefixed
    # with who it WOULD have gone to.
    subject = build_subject(today)
    for r in to_send:
        pll = r['pll']
        html_body = build_html_body(r['digest'], today)
        text_body = build_text_body(r['digest'], today)
        if live:
            send_email(pll['email'], subject, html_body, text_body)
            r['result'] = f"sent to {pll['email']}"
        else:
            send_email(JIM_EMAIL, f"[TEST — would go to {pll['name']}] {subject}",
                       html_body, text_body)
            r['result'] = f"TEST: rendered to Jim (would go to {pll['email']})"
        log.info(r['result'] + f" — {pll['name']}")

    rollup_subject, rollup_html, rollup_text = build_rollup(
        results, today, live, watermark, watermark_date)
    send_email(JIM_EMAIL, rollup_subject, rollup_html, rollup_text)
    log.info("Roll-up sent to Jim.")

    # Watermark advances ONLY here — after every send above succeeded.
    # Any exception on the way down skipped this insert, so a failed run
    # leaves the previous watermark in place and its window is re-covered.
    section1_ids = sorted({
        i['id'] for r in to_send for i in r['digest']['new_items']
    })
    summary = {
        'mode': 'live' if live else 'test',
        'plls': {
            r['pll']['name']: (
                {'suppressed': True} if r['digest'] is None else {
                    'new':      len(r['digest']['new_items']),
                    'due_soon': len(r['digest']['due_soon']),
                    'overdue':  len(r['digest']['overdue']),
                    'overflow': r['digest']['overdue_overflow'],
                }
            ) for r in results
        },
        'section1_ids': section1_ids,
    }
    try:
        db.table('pll_digest_state').insert({
            'watermark_used': watermark.isoformat() if watermark else None,
            'test_mode': not live,
            'summary': summary,
        }).execute()
    except Exception as e:
        # Sends happened but the watermark didn't advance — next run will
        # re-cover this window (duplicate "new" items, never lost ones).
        log.error(f"State insert failed AFTER successful sends: {e}")
        sys.exit(1)

    log.info(f"Run complete — watermark advanced. {json.dumps(summary['plls'])}")
    print(f"Digest run complete ({'LIVE' if live else 'TEST'}) — "
          f"{len(to_send)} digest(s) + roll-up.")


if __name__ == '__main__':
    main()
