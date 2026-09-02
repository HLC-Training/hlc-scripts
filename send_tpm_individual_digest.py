#!/usr/bin/env python3
"""
send_tpm_individual_digest.py
─────────────────────────────────────────────────────────────────
WEEKLY per-TPM P&C digest — one email per TPM, scoped to that TPM's
OWN projects (ORiON feedback 76944b43, Jim relaying Michele's ask;
SAM COS action item 8627d13f).

This is the THIRD digest in this repo. It is a near-clone of
send_tpm_digest.py's data layer, re-scoped from "all TPMs, to Michele"
to "one TPM's own projects, to that TPM":

  send_pll_digest.py    daily  → each of 7 PLLs, their Delivery work.
  send_tpm_digest.py    daily  → MICHELE only, all-TPM P&C roll-up.
  THIS FILE             weekly → each TPM, their own P&C projects.

Nothing here touches the other two. They keep separate scripts,
separate workflows, separate state tables and separate LIVE flags.

FOUR SECTIONS, one TPM's own projects, all filtered to
pc_projects.owner_id = that TPM and active (status NOT IN
DONE_STATUSES — the app's TERMINAL set):

  1. "Newly assigned to you this week" — created_at > watermark.
     FRAMING IS DELIBERATE: P&C projects arrive via sync_ap.py from
     Smartsheet, not by human authorship in ORiON, so a row appearing
     in this window means it landed in the TPM's bucket — not that
     someone wrote it. The heading and intro say "assigned", never
     "new" or "created". Do not re-word this to "New projects" to
     match send_tpm_digest.py; that digest's audience (Michele) reads
     it as intake, a TPM reads it as their inbox.
  2. "Overdue" — active, past target_end_date.
  3. "Missing required information" — the shared incomplete_pc_rows
     view (Mechanism 3, SAM COS 7007b802, orion-pll docs/migrations/
     2026-08-20_incomplete_rows_view.sql). Read-only consumer: this
     script must NEVER reimplement the completeness predicate.
  4. "Approaching stale" — the shared pc_projects_staleness view
     (public.staleness_state(), orion-pll knowledge/decisions/
     2026-08-11-approaching-stale-warning.md), staleness='approaching'.
     Read-only consumer, same rule.

NO DEDUP, NO PRECEDENCE: a project appears in EVERY section it
qualifies for. A newly-assigned project that is also missing required
fields shows under both — each section answers its own question
("what's new for me?" vs "what do I have to fix?"). This is a settled
decision (2026-09-02 grill, option (a)); do not "tidy" it into a
first-match-wins list.

UNCAPPED: unlike send_tpm_digest.py — which caps Overdue / Approaching
/ Incomplete at 5 PER TPM because Michele reads nine TPMs' worth in one
email — each TPM here sees only their own projects, so every section
renders in full with no "+N more" line. Deliberate (2026-09-02 grill
6b). Note that this makes a heavy TPM's email genuinely long; that is
the accepted trade for "your list is your list".

SUPPRESSION: a TPM with nothing in ANY of the four sections gets NO
email. This is a STANDING WEEKLY STATUS email, not a change-only one —
a TPM with nothing newly assigned but one overdue project still gets
their email. The only silence is a genuinely empty week.

WATERMARK (tpm_individual_digest_state, ORiON Supabase — its OWN table,
never shares with tpm_digest_state or pll_digest_state):
  One row per run, inserted only after a successful send (or a
  correctly-suppressed all-empty run). Watermark = latest sent_at.
  pc_projects.created_at is a timestamptz with no update trigger, so
  "newly assigned since" is a direct created_at > watermark comparison
  and no reported-ids exclusion list is needed.

  First run ever (empty state table) → 7-DAY fallback window, NOT the
  PLL digest's 72 hours. This job is weekly: a 3-day first window would
  silently under-report "newly assigned" by four days.

  MISSED-MONDAY SELF-HEAL: a skipped or failed run does not advance the
  watermark, so the next successful send covers everything since the
  last SUCCESSFUL send — possibly 14+ days. Newly-assigned items fold
  forward into the next week rather than being lost. Because of this
  the render must always describe the window as "since your last
  digest (<date>)" and must NEVER hardcode "in the last 7 days" —
  window_phrase() is the single place that wording lives.

CADENCE — MONDAYS, HOLIDAY OR NOT:
  This job does NOT use ge_holidays.send_decision(). The daily digests
  gate on weekday AND holiday because skipping one of five weekly sends
  costs a day. A WEEKLY job that skipped a holiday Monday would drop an
  entire week of coverage, so the gate here is Monday-only: is it Monday
  in Chicago, yes or no. ge_holidays.py is intentionally NOT imported.

TEST-FIRST GATE: default mode is TEST — every TPM's render goes to Jim
with a "[TEST — would go to <TPM name>]" subject prefix and nothing
reaches a TPM. LIVE requires --live or TPM_INDIVIDUAL_DIGEST_LIVE=true.
That variable is READ NOWHERE ELSE and this script reads NO OTHER live
flag: PLL_DIGEST_LIVE and TPM_DIGEST_LIVE are never READ here (they
appear only in prose like this line and in --live's help text, never
in an os.environ call — `grep -n os.environ` on this file returns
exactly three hits and only one of them is a live flag), so enabling
either of those can never enable this one. Unlike
send_tpm_digest.py (TEST indefinitely), this digest IS meant to go
live — Jim flips the flag after reviewing the test bundle.

RECIPIENTS: portal_users where role='tpm', and only those. Non-TPM P&C
owners (verified 2026-09-02: 4 pll-owned + 1 viewer-owned active
projects) get NO individual email — Michele's all-TPM digest is their
coverage. Accepted boundary, not an oversight.

READ-ONLY on pc_projects, portal_users and both shared views. The only
table this job writes is tpm_individual_digest_state.

Exit codes: any Supabase/Resend failure exits non-zero having sent as
little as possible — never a green run that silently did nothing.

Schedule: NOT a GitHub Actions cron. Fired by a Vercel cron
(orion-pll vercel.json '9 11 * * 1') which calls the authenticated
endpoint /api/cron/tpm-individual-digest, which sends a GitHub
repository_dispatch (event type 'tpm-individual-digest') that triggers
.github/workflows/tpm-individual-digest.yml. Vercel is the scheduler;
GitHub Actions is only the runner. See knowledge/decisions/
2026-09-02-tpm-individual-digest.md.
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

# NOTE: ge_holidays is deliberately NOT imported — see CADENCE above.

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

CHICAGO       = ZoneInfo("America/Chicago")
DONE_STATUSES = {"complete", "cancelled"}   # the app's TERMINAL set (ORiON CLAUDE.md)
MONDAY        = 0                           # date.weekday() — Monday is 0
FALLBACK_WINDOW_DAYS = 7                    # first run ever: no watermark row yet

# Brand values — identical to send_pll_digest.py / send_tpm_digest.py
# (orion-pll app/styles/vernova-tokens.css + orion-tokens.css).
EVERGREEN   = "#005e60"
HEADER_DEEP = "#004a4c"
ION         = "#3fd2ff"
ION_INK     = "#0a7ea4"
NIGHT       = "#212121"
BODY_TEXT   = "#444444"
MUTED       = "#888888"
CRITICAL    = "#e63946"
BORDER      = "#e5e7eb"
ZEBRA       = "#f8fafa"
EMAIL_WIDTH = 600

LOG_FILE = Path(__file__).parent / "send_tpm_individual_digest.log"

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


# ─── DATE GATE (Monday-only, NO holiday skip) ───────────────────
def monday_decision(d: date) -> tuple[bool, str]:
    """Monday-only gate on a Chicago-local date.

    Deliberately does NOT consult ge_holidays.HOLIDAYS: this is a weekly
    job, and skipping a holiday Monday would drop a whole week of
    coverage rather than one day of five. See CADENCE in the module
    docstring — do not "fix" this by importing the shared gate.
    """
    if d.weekday() != MONDAY:
        return False, f"{d} is a {d.strftime('%A')} — this digest sends Mondays only, no send."
    return True, f"{d} is a Monday — send (holidays do not skip this weekly digest)."


# ─── DATA (all reads; any failure must end the run non-zero) ────
def fetch_tpms(db) -> list[dict]:
    resp = db.table('portal_users').select('id, name, email') \
        .eq('role', 'tpm').order('name').execute()
    tpms = resp.data or []
    if not tpms:
        # 0 TPMs is not a valid state of this system — treat as a
        # failure, not a quiet "nobody to email".
        raise RuntimeError("portal_users query returned zero role='tpm' rows")
    return tpms


def fetch_projects(db, owner_ids: list[str]) -> list[dict]:
    # No status filter here — DONE_STATUSES is applied in Python inside
    # build_digest so that function stays unit-testable against
    # synthesized rows without a second live query shape.
    resp = db.table('pc_projects') \
        .select('id, owner_id, title, status, created_at, target_end_date') \
        .in_('owner_id', owner_ids) \
        .execute()
    return resp.data or []


def fetch_approaching(db, owner_ids: list[str]) -> list[dict]:
    """Approaching-stale projects for these owners, from the SHARED
    pc_projects_staleness view (public.staleness_state() — orion-pll
    knowledge/decisions/2026-08-11-approaching-stale-warning.md), joined
    back to pc_projects for the detail fields the render needs.

    Read-only consumer. The view's own terminal set is {complete,
    cancelled} — identical to DONE_STATUSES — so no extra filtering.
    Never reimplement the staleness predicate here."""
    resp = db.table('pc_projects_staleness').select('id, owner_id') \
        .eq('staleness', 'approaching') \
        .in_('owner_id', owner_ids) \
        .execute()
    stale_ids = [r['id'] for r in (resp.data or [])]
    if not stale_ids:
        return []
    detail = db.table('pc_projects') \
        .select('id, owner_id, title, status, target_end_date, updated_at') \
        .in_('id', stale_ids) \
        .execute()
    return detail.data or []


def fetch_incomplete(db, owner_ids: list[str]) -> list[dict]:
    """Missing-required-information projects for these owners, from the
    SHARED incomplete_pc_rows view (Mechanism 3, SAM COS 7007b802,
    orion-pll docs/migrations/2026-08-20_incomplete_rows_view.sql).

    Read-only consumer. Completeness is defined once, in the view —
    this script must never reimplement the predicate in Python."""
    resp = db.table('incomplete_pc_rows') \
        .select('id, owner_id, title, missing_fields') \
        .in_('owner_id', owner_ids) \
        .execute()
    return resp.data or []


def fetch_state(db) -> datetime | None:
    """Watermark = latest successful run's sent_at, from THIS digest's
    own state table. Never reads tpm_digest_state or pll_digest_state."""
    resp = db.table('tpm_individual_digest_state').select('sent_at') \
        .order('sent_at', desc=True).limit(1).execute()
    rows = resp.data or []
    return parse_timestamp(rows[0]['sent_at']) if rows else None


def parse_timestamp(raw: str) -> datetime:
    ts = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def parse_date(raw) -> date | None:
    return date.fromisoformat(raw) if raw else None


# ─── DIGEST ASSEMBLY (pure — no DB, no I/O) ─────────────────────
def build_digest(tpm: dict, projects: list[dict], approaching_items: list[dict],
                 incomplete_items: list[dict],
                 watermark: datetime, today: date) -> dict | None:
    """Four uncapped sections for ONE TPM; None if all four are empty.

    Pure function: takes rows, returns a render model. Per-TPM scoping
    and empty-suppression are therefore provable in memory against
    synthesized rows, with no live send and no rolled-back Supabase
    transaction (multi-statement Supabase-MCP txns are unreliable —
    tasks/lessons.md; the 2026-08-05 TPM digest verification did the
    same thing).

    NO DEDUP between sections by design — see the module docstring.
    """
    own = [p for p in projects
           if p['owner_id'] == tpm['id'] and p.get('status') not in DONE_STATUSES]
    own_approaching = [p for p in approaching_items if p['owner_id'] == tpm['id']]
    own_incomplete = [p for p in incomplete_items if p['owner_id'] == tpm['id']]

    newly_assigned = sorted(
        (p for p in own if parse_timestamp(p['created_at']) > watermark),
        key=lambda p: p['created_at'],
    )
    overdue = sorted(
        (p for p in own
         if parse_date(p.get('target_end_date')) is not None
         and parse_date(p['target_end_date']) < today),
        key=lambda p: p['target_end_date'],   # oldest target first = longest overdue first
    )
    approaching = sorted(
        own_approaching,
        key=lambda p: p.get('updated_at') or '',   # oldest updated_at first = longest quiet
    )
    incomplete = sorted(own_incomplete, key=lambda p: p['title'])

    if not newly_assigned and not overdue and not approaching and not incomplete:
        return None

    # No caps and no overflow counts anywhere — each TPM sees their own
    # full list (2026-09-02 grill 6b).
    return {
        'tpm':            tpm,
        'newly_assigned': newly_assigned,
        'overdue':        overdue,
        'incomplete':     incomplete,
        'approaching':    approaching,
    }


def build_subject(today: date) -> str:
    return f"Your P&C projects this week — {today.strftime('%B')} {today.day}"


def window_phrase(watermark: datetime) -> str:
    """How the 'newly assigned' window is described to the reader.

    ALWAYS anchored to the actual watermark, NEVER "the last 7 days".
    A missed Monday makes the real window 14+ days, and this digest's
    self-heal promise is that those items fold forward rather than
    disappearing — so the copy has to tell the truth about the span.
    """
    return f"since your last digest ({watermark.astimezone(CHICAGO).strftime('%b %d')})"


# ─── RENDERING ──────────────────────────────────────────────────
FONT = "Calibri,Arial,Helvetica,sans-serif"


def esc(value) -> str:
    return html.escape(str(value)) if value not in (None, '') else '—'


def fmt_date(raw) -> str:
    d = parse_date(raw)
    return d.strftime('%b %d') if d else '—'


def fmt_logged(raw) -> str:
    return parse_timestamp(raw).astimezone(CHICAGO).strftime('%b %d')


def days_overdue(raw, today: date) -> str:
    n = (today - parse_date(raw)).days
    return f"{n} day{'s' if n != 1 else ''} past"


def days_quiet(raw, today: date) -> str:
    n = (today - parse_timestamp(raw).astimezone(CHICAGO).date()).days
    return f"{n} day{'s' if n != 1 else ''} without an update"


def item_row_html(project: dict, meta: str, zebra: bool) -> str:
    bg = f'background-color:{ZEBRA};' if zebra else ''
    return (
        f'<tr><td style="{bg}padding:8px 14px;border-bottom:1px solid {BORDER};'
        f'font-family:{FONT};font-size:14px;color:{NIGHT};line-height:1.4;">'
        f'{esc(project["title"])}'
        f'<br><span style="font-size:12px;color:{MUTED};">{meta}</span>'
        f'</td></tr>'
    )


def incomplete_row_html(project: dict, zebra: bool) -> str:
    # Single-line row — same shape as send_tpm_digest.py's incomplete row.
    bg = f'background-color:{ZEBRA};' if zebra else ''
    labels = esc(', '.join(project['missing_fields']))
    return (
        f'<tr><td style="{bg}padding:8px 14px;border-bottom:1px solid {BORDER};'
        f'font-family:{FONT};font-size:14px;color:{NIGHT};line-height:1.4;">'
        f'{esc(project["title"])} &mdash; missing: {labels}'
        f'</td></tr>'
    )


def section_html(heading: str, rows: list[str], intro: str = '') -> str:
    """One section. Absent entirely when the TPM has nothing in it."""
    if not rows:
        return ''
    intro_row = (
        f'<tr><td style="padding:0 0 6px 0;font-family:{FONT};font-size:12px;'
        f'color:{MUTED};line-height:1.4;">{intro}</td></tr>' if intro else ''
    )
    return (
        f'<tr><td style="padding:22px 0 0 0;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        f'<tr><td style="font-family:{FONT};font-size:16px;font-weight:bold;'
        f'color:{EVERGREEN};padding:0 0 4px 0;">{heading}</td></tr>'
        f'<tr><td style="font-size:0;line-height:0;height:3px;background-color:{ION};'
        f'width:44px;">&nbsp;</td></tr>'
        f'{intro_row}'
        f'<tr><td style="padding:2px 0 0 0;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        f'{"".join(rows)}'
        f'</table></td></tr>'
        f'</table></td></tr>'
    )


def build_html_body(digest: dict, today: date, watermark: datetime) -> str:
    first = (digest['tpm'].get('name') or '').split(' ')[0] or 'there'
    greeting = (
        f"Hi {esc(first)}, here's where your P&amp;C projects stand at the start of the week "
        "&mdash; your own list, not the team's. If a target date has moved or a project's "
        "already wrapped up, updating it in ORiON keeps this list honest."
    )

    sections = [
        section_html(
            "Newly assigned to you this week",
            [item_row_html(
                p,
                f"Assigned {fmt_logged(p['created_at'])} &middot; {esc(p.get('status'))}"
                + (f" &middot; target {fmt_date(p['target_end_date'])}" if p.get('target_end_date') else ""),
                idx % 2 == 1)
             for idx, p in enumerate(digest['newly_assigned'])],
            intro=f"Projects that landed in your bucket {window_phrase(watermark)}. "
                  "P&amp;C projects arrive from the AP sync rather than being written "
                  "in ORiON, so these are newly <em>assigned</em> to you, not newly created."),
        section_html(
            "Overdue",
            [item_row_html(
                p,
                f'<span style="color:{CRITICAL};">Target {fmt_date(p["target_end_date"])} '
                f'&middot; {days_overdue(p["target_end_date"], today)}</span> '
                f'&middot; {esc(p.get("status"))}',
                idx % 2 == 1)
             for idx, p in enumerate(digest['overdue'])],
            intro="Past their target end date and still open."),
        section_html(
            "Missing required information",
            [incomplete_row_html(p, idx % 2 == 1)
             for idx, p in enumerate(digest['incomplete'])],
            intro="These are missing required information. You'll see the same list "
                  "when you sign in to ORiON."),
        section_html(
            "Approaching stale",
            [item_row_html(
                p,
                f"{days_quiet(p['updated_at'], today)} &middot; {esc(p.get('status'))}"
                + (f" &middot; target {fmt_date(p['target_end_date'])}" if p.get('target_end_date') else ""),
                idx % 2 == 1)
             for idx, p in enumerate(digest['approaching'])],
            intro="These haven't been updated in a while and will flip to Stale soon "
                  "&mdash; a note in ORiON resets the clock. See the Approaching Stale "
                  "help article in ORiON for details."),
    ]

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
        Want the full picture? Open ORiON to see all of your P&amp;C projects.
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


def text_section(heading: str, items: list[dict], line_fn, intro: str = '') -> list[str]:
    if not items:
        return []
    lines = [heading, "-" * len(heading)]
    if intro:
        lines.append(intro)
    for p in items:
        lines += line_fn(p)
    lines.append("")
    return lines


def build_text_body(digest: dict, today: date, watermark: datetime) -> str:
    first = (digest['tpm'].get('name') or '').split(' ')[0] or 'there'
    lines = [
        f"Hi {first}, here's where your P&C projects stand at the start of the week "
        "-- your own list, not the team's. If a target date has moved or a project's "
        "already wrapped up, updating it in ORiON keeps this list honest.",
        "",
    ]
    lines += text_section(
        "Newly assigned to you this week", digest['newly_assigned'],
        lambda p: [f"  - {p['title']}",
                   f"      Assigned {fmt_logged(p['created_at'])} | {p.get('status')}"],
        intro=f"Projects that landed in your bucket {window_phrase(watermark)}. "
              "P&C projects arrive from the AP sync rather than being written in "
              "ORiON, so these are newly ASSIGNED to you, not newly created.")
    lines += text_section(
        "Overdue", digest['overdue'],
        lambda p: [f"  - {p['title']}",
                   f"      Target {fmt_date(p['target_end_date'])} | "
                   f"{days_overdue(p['target_end_date'], today)} | {p.get('status')}"],
        intro="Past their target end date and still open.")
    lines += text_section(
        "Missing required information", digest['incomplete'],
        lambda p: [f"  - {p['title']} -- missing: {', '.join(p['missing_fields'])}"],
        intro="These are missing required information. You'll see the same list "
              "when you sign in to ORiON.")
    lines += text_section(
        "Approaching stale", digest['approaching'],
        lambda p: [f"  - {p['title']}",
                   f"      {days_quiet(p['updated_at'], today)} | {p.get('status')}"],
        intro="These haven't been updated in a while and will flip to Stale soon "
              "-- a note in ORiON resets the clock.")
    lines += [
        "Want the full picture? Open ORiON to see all of your P&C projects.",
        f"Open ORiON: {ORION_URL}",
        "",
        "See something that looks off? Reply to this email and it'll reach the right place.",
        "",
        "-- ORiON",
    ]
    return "\n".join(lines)


# ─── SEND ───────────────────────────────────────────────────────
def send_email(to: str, subject: str, html_body: str, text_body: str) -> None:
    if not RESEND_API_KEY:
        raise SystemExit("ERROR: RESEND_API_KEY environment variable is not set.")
    resp = requests.post(
        RESEND_API_URL,
        headers={'Authorization': f'Bearer {RESEND_API_KEY}',
                 'Content-Type': 'application/json'},
        json={'from': FROM_EMAIL, 'to': [to], 'subject': subject,
              'html': html_body, 'text': text_body},
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(f"Resend send failed ({resp.status_code}): {resp.text}")


# ─── MAIN ───────────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(
        description="Weekly per-TPM P&C digest — one email per TPM, their own projects.")
    p.add_argument("--dry-run", action="store_true",
                   help="Assemble and print every TPM's digest; send nothing, write no state.")
    p.add_argument("--live", action="store_true",
                   help="LIVE MODE: send each TPM their own email. Without this flag "
                        "(and without TPM_INDIVIDUAL_DIGEST_LIVE=true) every render goes "
                        "to Jim only. Separate flag from PLL_DIGEST_LIVE and "
                        "TPM_DIGEST_LIVE — neither of those is read by this script.")
    p.add_argument("--check-date", metavar="YYYY-MM-DD",
                   help="Print the Monday send/no-send decision for a date and exit.")
    p.add_argument("--force-date-gate", action="store_true",
                   help="Bypass the Monday gate (manual test runs).")
    p.add_argument("--only", metavar="NAME",
                   help="Restrict the run to TPMs whose name contains NAME "
                        "(case-insensitive) — test bundles without emailing nine renders.")
    return p.parse_args()


def main():
    args = parse_args()

    if args.check_date:
        ok, reason = monday_decision(date.fromisoformat(args.check_date))
        print(reason)
        return

    # Live requires a deliberate, explicit switch — default is TEST.
    # This is the ONLY live flag this script reads.
    live = args.live or os.environ.get("TPM_INDIVIDUAL_DIGEST_LIVE", "").lower() == "true"
    log.info(f"Mode: {'LIVE (each TPM)' if live else 'TEST (all email to Jim only)'}"
             + (" [dry-run]" if args.dry_run else ""))

    now_chicago = datetime.now(CHICAGO)
    today = now_chicago.date()
    ok, reason = monday_decision(today)
    if not ok and not args.force_date_gate:
        log.info(reason)
        print(reason)
        return
    if not ok and args.force_date_gate:
        log.info(f"Monday gate bypassed by --force-date-gate ({reason})")

    if not ORION_SUPABASE_SERVICE_KEY:
        raise SystemExit("ERROR: ORION_SUPABASE_SERVICE_KEY environment variable is not set.")

    # All reads: any failure exits non-zero. A digest job that silently
    # sends nothing and reports success is the failure mode this repo
    # exists to never repeat (tasks/lessons.md).
    try:
        db = create_client(SUPABASE_URL, ORION_SUPABASE_SERVICE_KEY)
        tpms = fetch_tpms(db)
        if args.only:
            tpms = [t for t in tpms if args.only.lower() in (t.get('name') or '').lower()]
            log.info(f"--only {args.only!r} → {len(tpms)} TPM(s).")
        owner_ids = [t['id'] for t in tpms]
        projects = fetch_projects(db, owner_ids)
        approaching = fetch_approaching(db, owner_ids)
        incomplete = fetch_incomplete(db, owner_ids)
        watermark = fetch_state(db)
    except Exception as e:
        log.error(f"Supabase fetch failed — aborting without sending: {e}")
        sys.exit(1)

    if watermark is None:
        # 7 days, not 72 hours — this is a weekly digest.
        watermark = now_chicago.astimezone(timezone.utc) - timedelta(days=FALLBACK_WINDOW_DAYS)
        log.info(f"No watermark row — {FALLBACK_WINDOW_DAYS}-day fallback window "
                 f"since {watermark.isoformat()}.")

    results = [{'tpm': t,
                'digest': build_digest(t, projects, approaching, incomplete, watermark, today)}
               for t in tpms]
    to_send = [r for r in results if r['digest'] is not None]
    log.info(f"{len(tpms)} TPMs — {len(to_send)} with items to report, "
             f"{len(tpms) - len(to_send)} suppressed (nothing in any section).")

    if args.dry_run:
        if not to_send:
            print("\n[DRY RUN] Every TPM is empty across all four sections — no email would be sent.")
            return
        for r in to_send:
            print(f"\n===== To {r['tpm']['name']} <{r['tpm']['email']}> — "
                  f"subject: {build_subject(today)} =====")
            print(build_text_body(r['digest'], today, watermark))
        for r in results:
            if r['digest'] is None:
                print(f"\n[SUPPRESSED] {r['tpm']['name']} — nothing in any section, no email.")
        print("\n[DRY RUN] Nothing sent, no state written.")
        return

    if not to_send:
        # Empty-suppression across the board: no email, but the run still
        # succeeded and still advances the watermark (there is nothing to
        # re-cover; the next window should start from now).
        log.info("Every TPM empty across all four sections — no email sent (empty-suppression).")
        _advance_watermark(db, watermark, live, results, sent=0)
        print("Digest run complete — nothing to report, no email sent.")
        return

    subject = build_subject(today)
    sent = 0
    for r in to_send:
        tpm = r['tpm']
        html_body = build_html_body(r['digest'], today, watermark)
        text_body = build_text_body(r['digest'], today, watermark)
        if live:
            send_email(tpm['email'], subject, html_body, text_body)
            log.info(f"Sent to {tpm['name']} <{tpm['email']}>.")
        else:
            send_email(JIM_EMAIL, f"[TEST — would go to {tpm['name']}] {subject}",
                       html_body, text_body)
            log.info(f"TEST: rendered to Jim (would go to {tpm['name']} <{tpm['email']}>).")
        sent += 1

    _advance_watermark(db, watermark, live, results, sent=sent)
    print(f"Digest run complete ({'LIVE' if live else 'TEST'}) — {sent} email(s) sent.")


def _advance_watermark(db, watermark: datetime, live: bool, results: list[dict], sent: int) -> None:
    # Watermark advances ONLY here — after every send above succeeded (or
    # the run correctly no-op'd). Any exception on the way down skipped
    # this insert, so a failed or missed run leaves the previous watermark
    # in place and its window is re-covered by the next successful run.
    # That is the missed-Monday self-heal.
    summary = {
        'mode': 'live' if live else 'test',
        'emails_sent': sent,
        'tpms': {
            r['tpm']['name']: (
                {'suppressed': True} if r['digest'] is None else {
                    'newly_assigned': len(r['digest']['newly_assigned']),
                    'overdue':        len(r['digest']['overdue']),
                    'incomplete':     len(r['digest']['incomplete']),
                    'approaching':    len(r['digest']['approaching']),
                }
            ) for r in results
        },
    }
    try:
        db.table('tpm_individual_digest_state').insert({
            'watermark_used': watermark.isoformat() if watermark else None,
            'test_mode': not live,
            'summary': summary,
        }).execute()
    except Exception as e:
        log.error(f"State insert failed AFTER successful send: {e}")
        sys.exit(1)
    log.info(f"Run complete — watermark advanced. {json.dumps(summary['tpms'])}")


if __name__ == '__main__':
    main()
