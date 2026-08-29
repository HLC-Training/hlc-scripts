#!/usr/bin/env python3
"""
send_tpm_digest.py
─────────────────────────────────────────────────────────────────
Daily TPM P&C project roll-up digest, to Michele only
(SAM COS action item c5cf4442). Near-clone of send_pll_digest.py
(item 37425f71) — same pattern, different data source and a single
recipient instead of one-per-person.

In P&C, "project" (pc_projects) is the naming convention for what
Delivery calls an action item — same concept, different table and
different word. Weekday mornings (~6:00 America/Chicago), Michele gets
ONE combined ORiON-branded email covering every TPM's P&C projects,
grouped by TPM owner, with up to six sections:
  0. "Awaiting your review" — pc_projects rows in status 'submitted',
     with their business-day wait computed from submitted_at (Submission
     Tracking Loop, 2026-08-24 — orion-pll knowledge/decisions/
     2026-08-24-pc-submission-tracking-loop.md). Rendered FIRST and
     deliberately uncapped: it is the belt-and-suspenders backup to the
     in-app inbox badge, and the queue must never truncate.
  1. "New projects" — pc_projects rows created since the last digest
     actually sent.
  2. "Coming up in the next two weeks" — non-terminal projects with
     target_end_date within 14 days.
  3. "Overdue" — non-terminal projects past target_end_date, capped
     PER TPM at the 5 longest-overdue with a "+N more" line.
  4. "Approaching Stale" — projects the shared pc_projects_staleness
     view (public.staleness_state(), orion-pll knowledge/decisions/
     2026-08-11-approaching-stale-warning.md) flags as 'approaching'.
     The view's own terminal set ({complete, cancelled}) matches
     DONE_STATUSES here, so no extra filtering is needed. Capped PER
     TPM at 5 longest-quiet with a "+N more" line, same convention as
     Overdue. Present only for TPMs with items in it, same
     absent-if-empty behavior as the other sections.
  5. "Missing required information" — open projects missing a mandatory
     field, from the shared incomplete_pc_rows view (Mechanism 3, SAM COS
     action item 7007b802, orion-pll docs/migrations/
     2026-08-20_incomplete_rows_view.sql — single completeness definition,
     ported from lib/incomplete-rows.ts; this digest never reimplements
     the predicate). Closed rows (complete/cancelled) excluded, matching
     the in-app Mechanism 2 prompt exactly. Capped PER TPM at 5 with a
     "+N more" line, same convention as Overdue / Approaching Stale. This
     section ships behind the same TEST-mode gate as the rest of this
     digest — inert (renders to Jim only) until Jim flips TPM_DIGEST_LIVE.
If every TPM is empty across all five sections, NO email is sent —
there is nothing here for the once-a-day habit to protect.

Nothing is ever sent to a TPM. Michele is the sole live recipient; Jim
is cc'd on nothing (Michele-only, unlike the PLL digest's Jim roll-up —
Jim's explicit call, action item c5cf4442). There is no separate
roll-up email the way the PLL digest has one: this digest already IS
the one combined email, so the TEST-mode copy to Jim below is the full
render Michele will eventually see.

ACTIVE / DONE: pc_projects.status is free text (draft | submitted |
approved | rejected | active | on_hold | complete | cancelled per the
DB CHECK). Live data as of 2026-08-05 only has {active, approved,
on_hold} — none of the CHECK's terminal values are present. Rather than
assume a status string, the filter is built as "not in DONE_STATUSES"
where DONE_STATUSES = {complete, cancelled} — the app's own TERMINAL
set (ORiON CLAUDE.md: "complete / cancelled: TERMINAL — no reopen, no
edits, ever"). See knowledge/decisions/2026-08-05-tpm-daily-digest.md.

WATERMARK (tpm_digest_state, ORiON Supabase — separate table from
pll_digest_state, never shares a watermark):
  One row per successful run, inserted only after the send completed
  (or correctly no-op'd). Watermark = latest row's sent_at.
  pc_projects.created_at is a timestamptz (unlike action_items'
  created_date, a bare date) and has no update trigger (verified via
  pg_trigger — empty result), so "new since" is a direct
  created_at > watermark timestamp comparison. No boundary-date
  double-count is possible this way, so — unlike the PLL digest — no
  reported-ids exclusion list is needed. Empty state table → 72-hour
  fallback window, same as the PLL digest.

TEST-FIRST GATE: default mode is TEST — the full render goes to Jim
with a [TEST — would go to Michele] subject prefix. The live path
requires --live or TPM_DIGEST_LIVE=true (a SEPARATE flag from the PLL
digest's PLL_DIGEST_LIVE — enabling one must never enable the other).
Per Jim (2026-08-05): this stays in TEST mode indefinitely once
verified, until P&C's non-AP import + launch. Verified is not the same
as go-live.

READ-ONLY on pc_projects and portal_users. The only table this job
writes is tpm_digest_state.

Exit codes: any Supabase/Resend failure exits non-zero having sent as
little as possible — never a green run that silently did nothing.

Schedule: .github/workflows/tpm-digest.yml — '21 11 * * 1-5' UTC.
Minute 21 on purpose: distinct from the PLL digest's :07 and every
other cron slot in this repo (see cron list in that workflow's
comments) so the two jobs never contend for the same GitHub Actions
slot (lessons.md — :00/:30 are the contended minutes on this account).
Holiday list: ge_holidays.py (shared with the PLL digest — refresh for
2027 there, not here).
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

from ge_holidays import send_decision

# ─── CONFIG ─────────────────────────────────────────────────────
SUPABASE_URL = "https://czdkctjbejnwuopigxta.supabase.co"
# ORION_-prefixed on purpose — the generic SUPABASE_SERVICE_KEY name risks
# resolving to the wrong project across the three-project estate
# (lessons.md 2026-08-03). Do not shorten it.
ORION_SUPABASE_SERVICE_KEY = os.environ.get("ORION_SUPABASE_SERVICE_KEY", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")

RESEND_API_URL  = "https://api.resend.com/emails"
FROM_EMAIL      = "orion@ofstraining.com"
JIM_EMAIL       = "jim.rosen@gevernova.com"
MICHELE_EMAIL   = "michele.alcantar@gevernova.com"
ORION_URL       = "https://orion.ofstraining.com"

CHICAGO         = ZoneInfo("America/Chicago")
DONE_STATUSES   = {"complete", "cancelled"}   # the app's TERMINAL set (ORiON CLAUDE.md)
DUE_SOON_DAYS   = 14
OVERDUE_CAP     = 5
FALLBACK_WINDOW_HOURS = 72   # first run ever: no watermark row yet

# Brand values — identical to send_pll_digest.py
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

LOG_FILE = Path(__file__).parent / "send_tpm_digest.log"

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


# ─── DATA (all reads; any failure must end the run non-zero) ────
def fetch_tpms(db) -> list[dict]:
    resp = db.table('portal_users').select('id, name, email') \
        .eq('role', 'tpm').order('name').execute()
    tpms = resp.data or []
    if not tpms:
        # 0 TPMs is not a valid state of this system — treat as a
        # failure, not a quiet "nobody to roll up".
        raise RuntimeError("portal_users query returned zero role='tpm' rows")
    return tpms


def fetch_projects(db, owner_ids: list[str]) -> list[dict]:
    # No status filter here — DONE_STATUSES is applied in Python per
    # TPM so build_digest can be unit-tested against synthesized rows
    # without a second live query shape.
    resp = db.table('pc_projects') \
        .select('id, owner_id, title, status, created_at, submitted_at, target_end_date') \
        .in_('owner_id', owner_ids) \
        .execute()
    return resp.data or []


def fetch_approaching(db, owner_ids: list[str]) -> list[dict]:
    """Approaching-stale projects for these owners, from the shared
    pc_projects_staleness view (public.staleness_state() — orion-pll
    knowledge/decisions/2026-08-11-approaching-stale-warning.md), joined
    back to pc_projects for the detail fields the other sections already
    render. No status filter beyond what the view itself applies (its
    terminal set is {complete, cancelled} — same as DONE_STATUSES here,
    so no double-filtering needed)."""
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
    shared incomplete_pc_rows view (Mechanism 3, SAM COS action item
    7007b802, orion-pll docs/migrations/2026-08-20_incomplete_rows_view.sql).
    Defines completeness once — this digest must never reimplement the
    predicate in Python, same rule as the Approaching Stale view above."""
    resp = db.table('incomplete_pc_rows') \
        .select('id, owner_id, title, missing_fields') \
        .in_('owner_id', owner_ids) \
        .execute()
    return resp.data or []


def fetch_recipient_open_questions(db, email: str) -> int:
    """Michele's open ask-a-question count (item_questions, orion-pll
    2026-08-29 build). asked_of is a portal_users id; the lookup goes
    through email (ilike — GE emails are mixed-case). Returns 0 when the
    portal row is missing rather than failing the whole digest run."""
    portal = db.table('portal_users').select('id').ilike('email', email).execute()
    rows = portal.data or []
    if not rows:
        return 0
    q = db.table('item_questions').select('id', count='exact') \
        .eq('asked_of', rows[0]['id']).eq('status', 'awaiting_response').execute()
    return q.count or 0


def fetch_state(db) -> datetime | None:
    """Return the watermark timestamp (latest successful run's sent_at)."""
    resp = db.table('tpm_digest_state').select('sent_at') \
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


def business_days_since(raw_ts: str, today: date) -> int:
    """Weekdays in (timestamp's Chicago day, today] — the SAME definition
    as orion-pll lib/business-days.ts (Submission Tracking Loop,
    2026-08-24): same-day = 0, Friday→Monday = 1, holidays not excluded
    (the clock informs judgment, it doesn't declare 'stuck'). Keep the two
    implementations in lockstep — the loop's core promise is one identical
    number on every surface."""
    start = parse_timestamp(raw_ts).astimezone(CHICAGO).date()
    if today <= start:
        return 0
    n, d = 0, start
    while d < today:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


# ─── DIGEST ASSEMBLY ────────────────────────────────────────────
def build_digest(tpm: dict, projects: list[dict], approaching_items: list[dict],
                 incomplete_items: list[dict],
                 watermark: datetime, today: date) -> dict | None:
    """Five sections for one TPM; None if all empty."""
    own = [p for p in projects
           if p['owner_id'] == tpm['id'] and p.get('status') not in DONE_STATUSES]
    own_approaching = [p for p in approaching_items if p['owner_id'] == tpm['id']]
    own_incomplete = [p for p in incomplete_items if p['owner_id'] == tpm['id']]

    new_items = sorted(
        (p for p in own if parse_timestamp(p['created_at']) > watermark),
        key=lambda p: p['created_at'],
    )
    due_soon = sorted(
        (p for p in own
         if parse_date(p.get('target_end_date')) is not None
         and today <= parse_date(p['target_end_date']) <= today + timedelta(days=DUE_SOON_DAYS)),
        key=lambda p: p['target_end_date'],
    )
    overdue_all = sorted(
        (p for p in own
         if parse_date(p.get('target_end_date')) is not None
         and parse_date(p['target_end_date']) < today),
        key=lambda p: p['target_end_date'],   # oldest target first = longest overdue first
    )
    approaching_all = sorted(
        own_approaching,
        key=lambda p: p.get('updated_at') or '',   # oldest updated_at first = longest quiet
    )
    incomplete_all = sorted(own_incomplete, key=lambda p: p['title'])

    # Submission Tracking Loop (2026-08-24, orion-pll decision
    # 2026-08-24-pc-submission-tracking-loop.md): projects sitting in
    # 'submitted' awaiting Michele's approve/reject. Belt-and-suspenders
    # backup to the in-app inbox badge — deliberately UNCAPPED, unlike the
    # other sections: this is the queue the loop exists to keep visible,
    # so it must never truncate. Oldest wait first.
    pending_review = sorted(
        (p for p in own if p.get('status') == 'submitted'),
        key=lambda p: p.get('submitted_at') or p['created_at'],
    )

    if not new_items and not due_soon and not overdue_all and not approaching_all \
            and not incomplete_all and not pending_review:
        return None

    return {
        'tpm':                  tpm,
        'pending_review':       pending_review,
        'new_items':            new_items,
        'due_soon':             due_soon,
        'overdue':              overdue_all[:OVERDUE_CAP],
        'overdue_overflow':     max(0, len(overdue_all) - OVERDUE_CAP),
        'approaching':          approaching_all[:OVERDUE_CAP],
        'approaching_overflow': max(0, len(approaching_all) - OVERDUE_CAP),
        'incomplete':           incomplete_all[:OVERDUE_CAP],
        'incomplete_overflow':  max(0, len(incomplete_all) - OVERDUE_CAP),
    }


def build_subject(today: date) -> str:
    return f"Your team's P&C projects — {today.strftime('%A')}, {today.strftime('%B')} {today.day}"


# ─── RENDERING ──────────────────────────────────────────────────
FONT = "Calibri,Arial,Helvetica,sans-serif"


def esc(value) -> str:
    return html.escape(str(value)) if value not in (None, '') else '—'


def fmt_date(raw) -> str:
    d = parse_date(raw)
    return d.strftime('%b %d') if d else '—'


def fmt_logged(raw) -> str:
    ts = parse_timestamp(raw).astimezone(CHICAGO)
    return ts.strftime('%b %d')


def days_overdue(raw, today: date) -> str:
    n = (today - parse_date(raw)).days
    return f"{n} day{'s' if n != 1 else ''} past"


def days_quiet(raw, today: date) -> str:
    n = (today - parse_timestamp(raw).astimezone(CHICAGO).date()).days
    return f"{n} day{'s' if n != 1 else ''} without an update"


def item_row_html(project: dict, meta: str, zebra: bool) -> str:
    bg = f'background-color:{ZEBRA};' if zebra else ''
    return (
        f'<tr><td style="{bg}padding:8px 14px 8px 28px;border-bottom:1px solid {BORDER};'
        f'font-family:{FONT};font-size:14px;color:{NIGHT};line-height:1.4;">'
        f'{esc(project["title"])}'
        f'<br><span style="font-size:12px;color:{MUTED};">{meta}</span>'
        f'</td></tr>'
    )


def tpm_group_html(tpm_name: str, rows: list[str], extra_row: str = '') -> str:
    return (
        f'<tr><td style="padding:10px 0 0 0;font-family:{FONT};font-size:13px;'
        f'font-weight:bold;color:{ION_INK};">{esc(tpm_name)}</td></tr>'
        f'<tr><td><table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        f'{"".join(rows)}{extra_row}</table></td></tr>'
    )


def section_html(heading: str, tpm_blocks: list[str], intro: str = '') -> str:
    if not tpm_blocks:
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
        f'{"".join(tpm_blocks)}'
        f'</table></td></tr>'
        f'</table></td></tr>'
    )


def incomplete_row_html(project: dict, zebra: bool) -> str:
    # Single-line row per Task 4's verbatim per-row format — no second
    # meta line, unlike item_row_html's other-section shape.
    bg = f'background-color:{ZEBRA};' if zebra else ''
    labels = esc(', '.join(project['missing_fields']))
    return (
        f'<tr><td style="{bg}padding:8px 14px 8px 28px;border-bottom:1px solid {BORDER};'
        f'font-family:{FONT};font-size:14px;color:{NIGHT};line-height:1.4;">'
        f'{esc(project["title"])} &mdash; missing: {labels}'
        f'</td></tr>'
    )


def incomplete_blocks(results: list[dict]) -> list[str]:
    """One tpm_group_html block per TPM that has incomplete projects."""
    blocks = []
    for r in results:
        d = r['digest']
        if d is None or not d['incomplete']:
            continue
        rows = [incomplete_row_html(p, idx % 2 == 1)
                for idx, p in enumerate(d['incomplete'])]
        extra = ''
        if d['incomplete_overflow']:
            extra = (
                f'<tr><td style="padding:8px 14px 8px 28px;font-family:{FONT};font-size:13px;'
                f'color:{MUTED};">Plus {d["incomplete_overflow"]} more missing required '
                f'information in ORiON.</td></tr>'
            )
        blocks.append(tpm_group_html(r['tpm']['name'], rows, extra))
    return blocks


def section_blocks(results: list[dict], key: str, meta_fn) -> list[str]:
    """One tpm_group_html block per TPM that has items in this section."""
    blocks = []
    for r in results:
        d = r['digest']
        if d is None or not d[key]:
            continue
        rows = [item_row_html(p, meta_fn(p), idx % 2 == 1)
                for idx, p in enumerate(d[key])]
        extra = ''
        if key == 'overdue' and d['overdue_overflow']:
            extra = (
                f'<tr><td style="padding:8px 14px 8px 28px;font-family:{FONT};font-size:13px;'
                f'color:{MUTED};">Plus {d["overdue_overflow"]} more past due in ORiON.</td></tr>'
            )
        elif key == 'approaching' and d['approaching_overflow']:
            extra = (
                f'<tr><td style="padding:8px 14px 8px 28px;font-family:{FONT};font-size:13px;'
                f'color:{MUTED};">Plus {d["approaching_overflow"]} more approaching stale in ORiON.</td></tr>'
            )
        blocks.append(tpm_group_html(r['tpm']['name'], rows, extra))
    return blocks


def build_html_body(results: list[dict], today: date, questions_count: int = 0) -> str:
    greeting = (
        "Hi Michele, here's where your team's P&C projects stand this morning "
        "— a quick daily rundown across all TPMs so nothing slips. If a target "
        "date has moved or a project's already wrapped up, updating it in "
        "ORiON keeps this list honest."
    )

    def pending_meta(p) -> str:
        ts = p.get('submitted_at') or p['created_at']
        n = business_days_since(ts, today)
        return (f"Submitted {fmt_logged(ts)} &middot; "
                f"waiting {n} business day{'s' if n != 1 else ''}")

    sections = [
        # First on purpose — this is her action queue; everything below is
        # informational. Same business-day number as the inbox badge and
        # the submitter's login prompt (Submission Tracking Loop).
        section_html("Awaiting your review", section_blocks(
            results, 'pending_review', pending_meta),
            intro="Submissions waiting on your approve or reject decision "
                  "&mdash; the same queue as the ORiON inbox, with the same "
                  "wait the submitter sees."),
        section_html("New projects", section_blocks(
            results, 'new_items',
            lambda p: f"Logged {fmt_logged(p['created_at'])} &middot; {esc(p.get('status'))}"
                      + (f" &middot; target {fmt_date(p['target_end_date'])}" if p.get('target_end_date') else ""))),
        section_html("Coming up in the next two weeks", section_blocks(
            results, 'due_soon',
            lambda p: f"Target {fmt_date(p['target_end_date'])} &middot; {esc(p.get('status'))}")),
        section_html("Overdue", section_blocks(
            results, 'overdue',
            lambda p: (f'<span style="color:{CRITICAL};">Target {fmt_date(p["target_end_date"])} '
                       f'&middot; {days_overdue(p["target_end_date"], today)}</span> '
                       f'&middot; {esc(p.get("status"))}'))),
        section_html("Approaching Stale", section_blocks(
            results, 'approaching',
            lambda p: f"{days_quiet(p['updated_at'], today)} &middot; {esc(p.get('status'))}"
                      + (f" &middot; target {fmt_date(p['target_end_date'])}" if p.get('target_end_date') else "")),
            intro="These haven't been updated in a while and will flip to Stale soon "
                  "&mdash; a note in ORiON resets the clock. See the Approaching Stale "
                  "help article in ORiON for details."),
        section_html("Missing required information", incomplete_blocks(results),
            intro="These projects are missing required information. Owners see the "
                  "same list when they sign in."),
    ]
    # Ask-a-question nag (orion-pll 2026-08-29 build): Michele's own open
    # questions, not her TPMs' — each TPM's nag surface is their Launch Pad.
    if questions_count:
        n = questions_count
        row = (
            f'<tr><td style="padding:10px 14px;border-bottom:1px solid {BORDER};'
            f'font-family:{FONT};font-size:14px;color:{NIGHT};line-height:1.4;">'
            f'{n} question{"s" if n != 1 else ""} awaiting your response &mdash; '
            f'they&rsquo;re listed on your Launch Pad, each with a link to its item.'
            f'</td></tr>'
        )
        sections.append(section_html("Questions awaiting your response", [row]))

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
        Want the full picture? Open ORiON to see every P&amp;C project.
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


def text_section(heading: str, results: list[dict], key: str, meta_fn, intro: str = '') -> list[str]:
    lines = []
    any_block = False
    for r in results:
        d = r['digest']
        if d is None or not d[key]:
            continue
        if not any_block:
            lines.append(heading)
            lines.append("-" * len(heading))
            if intro:
                lines.append(intro)
            any_block = True
        lines.append(f"{r['tpm']['name']}:")
        for p in d[key]:
            lines.append(f"  - {p['title']}")
            lines.append(f"      {meta_fn(p)}")
        if key == 'overdue' and d['overdue_overflow']:
            lines.append(f"      Plus {d['overdue_overflow']} more past due in ORiON.")
        elif key == 'approaching' and d['approaching_overflow']:
            lines.append(f"      Plus {d['approaching_overflow']} more approaching stale in ORiON.")
    if any_block:
        lines.append("")
    return lines


def incomplete_text_section(results: list[dict]) -> list[str]:
    heading = "Missing required information"
    lines = []
    any_block = False
    for r in results:
        d = r['digest']
        if d is None or not d['incomplete']:
            continue
        if not any_block:
            lines.append(heading)
            lines.append("-" * len(heading))
            lines.append("These projects are missing required information. Owners "
                          "see the same list when they sign in.")
            any_block = True
        lines.append(f"{r['tpm']['name']}:")
        for p in d['incomplete']:
            labels = ', '.join(p['missing_fields'])
            lines.append(f"  - {p['title']} -- missing: {labels}")
        if d['incomplete_overflow']:
            lines.append(f"      Plus {d['incomplete_overflow']} more missing required "
                          f"information in ORiON.")
    if any_block:
        lines.append("")
    return lines


def build_text_body(results: list[dict], today: date, questions_count: int = 0) -> str:
    lines = [
        "Hi Michele, here's where your team's P&C projects stand this morning "
        "-- a quick daily rundown across all TPMs so nothing slips. If a target "
        "date has moved or a project's already wrapped up, updating it in "
        "ORiON keeps this list honest.",
        "",
    ]
    def pending_meta_text(p) -> str:
        ts = p.get('submitted_at') or p['created_at']
        n = business_days_since(ts, today)
        return (f"Submitted {fmt_logged(ts)} | "
                f"waiting {n} business day{'s' if n != 1 else ''}")

    lines += text_section("Awaiting your review", results, 'pending_review',
                          pending_meta_text,
                          intro="Submissions waiting on your approve or reject decision "
                                "-- the same queue as the ORiON inbox, with the same "
                                "wait the submitter sees.")
    lines += text_section("New projects", results, 'new_items',
                          lambda p: f"Logged {fmt_logged(p['created_at'])} | {p.get('status')}")
    lines += text_section("Coming up in the next two weeks", results, 'due_soon',
                          lambda p: f"Target {fmt_date(p['target_end_date'])} | {p.get('status')}")
    lines += text_section("Overdue", results, 'overdue',
                          lambda p: f"Target {fmt_date(p['target_end_date'])} | "
                                    f"{days_overdue(p['target_end_date'], today)} | {p.get('status')}")
    lines += text_section("Approaching Stale", results, 'approaching',
                          lambda p: f"{days_quiet(p['updated_at'], today)} | {p.get('status')}",
                          intro="These haven't been updated in a while and will flip to Stale soon "
                                "-- a note in ORiON resets the clock. See the Approaching Stale "
                                "help article in ORiON for details.")
    lines += incomplete_text_section(results)
    if questions_count:
        n = questions_count
        lines += [
            "Questions awaiting your response",
            "-" * 31,
            f"{n} question{'s' if n != 1 else ''} awaiting your response -- "
            "they're listed on your Launch Pad, each with a link to its item.",
            "",
        ]
    lines += [
        "Want the full picture? Open ORiON to see every P&C project.",
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
        raise RuntimeError(f"Resend send failed ({resp.status_code}): {resp.text}")


# ─── MAIN ───────────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(description="Daily TPM P&C digest to Michele")
    p.add_argument("--dry-run", action="store_true",
                   help="Assemble and print the digest; send nothing, write no state.")
    p.add_argument("--live", action="store_true",
                   help="LIVE MODE: send to Michele. Without this flag (and without "
                        "TPM_DIGEST_LIVE=true) the render goes to Jim only — "
                        "test-first, per brief c5cf4442. Separate flag from the "
                        "PLL digest's PLL_DIGEST_LIVE.")
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
    live = args.live or os.environ.get("TPM_DIGEST_LIVE", "").lower() == "true"
    log.info(f"Mode: {'LIVE (Michele)' if live else 'TEST (all email to Jim only)'}"
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
        tpms = fetch_tpms(db)
        projects = fetch_projects(db, [t['id'] for t in tpms])
        approaching = fetch_approaching(db, [t['id'] for t in tpms])
        incomplete = fetch_incomplete(db, [t['id'] for t in tpms])
        questions_count = fetch_recipient_open_questions(db, MICHELE_EMAIL)
        watermark = fetch_state(db)
    except Exception as e:
        log.error(f"Supabase fetch failed — aborting without sending: {e}")
        sys.exit(1)

    if watermark is None:
        watermark = now_chicago.astimezone(timezone.utc) - timedelta(hours=FALLBACK_WINDOW_HOURS)
        log.info(f"No watermark row — fallback window since {watermark.isoformat()}.")

    results = [{'tpm': t, 'digest': build_digest(t, projects, approaching, incomplete, watermark, today)} for t in tpms]
    to_report = [r for r in results if r['digest'] is not None]
    log.info(f"{len(tpms)} TPMs — {len(to_report)} with items to report, "
             f"{len(tpms) - len(to_report)} suppressed (no items).")

    if args.dry_run:
        if not to_report and questions_count == 0:
            print("\n[DRY RUN] Every TPM is empty across all sections — no email would be sent.")
            return
        print(f"\n===== Digest to Michele — subject: {build_subject(today)} =====")
        print(build_text_body(results, today, questions_count))
        print("\n[DRY RUN] Nothing sent, no state written.")
        return

    if not to_report and questions_count == 0:
        # Empty-suppression: nothing to report, so no email — but the run
        # still succeeded and still advances the watermark (there is
        # nothing to re-cover; a future run's "new since" window should
        # start from now, not silently grow forever).
        log.info("All TPMs empty across all sections — no email sent (empty-suppression).")
        _advance_watermark(db, watermark, live, results, sent=False)
        print("Digest run complete — nothing to report, no email sent.")
        return

    subject = build_subject(today)
    html_body = build_html_body(results, today, questions_count)
    text_body = build_text_body(results, today, questions_count)
    if live:
        send_email(MICHELE_EMAIL, subject, html_body, text_body)
        log.info(f"Sent to {MICHELE_EMAIL}.")
    else:
        send_email(JIM_EMAIL, f"[TEST — would go to Michele] {subject}", html_body, text_body)
        log.info(f"TEST: rendered to Jim (would go to {MICHELE_EMAIL}).")

    _advance_watermark(db, watermark, live, results, sent=True)
    print(f"Digest run complete ({'LIVE' if live else 'TEST'}) — 1 email sent.")


def _advance_watermark(db, watermark: datetime, live: bool, results: list[dict], sent: bool) -> None:
    # Watermark advances ONLY here — after the send above succeeded (or
    # correctly no-op'd). Any exception on the way down skipped this
    # insert, so a failed run leaves the previous watermark in place and
    # its window is re-covered next time.
    summary = {
        'mode': 'live' if live else 'test',
        'sent': sent,
        'tpms': {
            r['tpm']['name']: (
                {'suppressed': True} if r['digest'] is None else {
                    'pending_review': len(r['digest']['pending_review']),
                    'new':      len(r['digest']['new_items']),
                    'due_soon': len(r['digest']['due_soon']),
                    'overdue':  len(r['digest']['overdue']),
                    'overflow': r['digest']['overdue_overflow'],
                }
            ) for r in results
        },
    }
    try:
        db.table('tpm_digest_state').insert({
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
