#!/usr/bin/env python3
"""
ap_sync_monitor.py
─────────────────────────────────────────────────────────────────
AP sync reconciliation monitor. Phase 2 of the reconciliation-monitor
design (decision 2026-08-25-ap-sync-reconciliation-monitor-design.md,
action item 9278f68e). Runs on its own GitHub Actions cron, separate
from sync-ap.yml.

Read-only against sync state. This script:
  - Reads SAM COS context_store keys health:github:orion_ap_sync:accounting
    (Phase 1's structured per-run accounting) and health:github:orion_ap_sync
    (the main heartbeat, read only for diagnostic context when the
    accounting key is absent).
  - Audits three conditions every run: the child-row identity closes
    (child_identity_residual == 0), the parent-titles delta-integrity
    holds, and the accounting is fresh (as_of within 4 hours).
  - Pushes via SAM COS notification_queue (the house Pushover pattern —
    see sync_ap.py's escalation page) on a healthy→broken transition, a
    re-nag every >=12h a break stays open, and once on broken→healthy
    recovery. Stays quiet otherwise, including while merely still-broken
    inside the 12h window.
  - Writes ONLY its own dedup-state key (system:ap_sync_monitor:alert_state).

It never writes to the accounting key, the heartbeat key, or anything
sync_ap.py owns, and it never blocks or triggers a sync run. A broken
identity means "the count is unexplained, investigate" — not "halt."
─────────────────────────────────────────────────────────────────
"""

import os
import sys
import json
import logging
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from supabase import create_client

# ─── CONFIG ─────────────────────────────────────────────────────
SAMCOS_SUPABASE_URL = "https://hucrkbomqsxpmokgypxg.supabase.co"
SAMCOS_SERVICE_KEY  = os.environ.get("SAMCOS_SERVICE_KEY", "")

ACCOUNTING_KEY  = "health:github:orion_ap_sync:accounting"
HEARTBEAT_KEY   = "health:github:orion_ap_sync"
ALERT_STATE_KEY = "system:ap_sync_monitor:alert_state"

STALE_THRESHOLD = timedelta(hours=4)
DEDUP_WINDOW    = timedelta(hours=12)

CHICAGO = ZoneInfo("America/Chicago")

LOG_FILE = Path(__file__).parent / "ap_sync_monitor.log"

# ─── LOGGING ────────────────────────────────────────────────────
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger(__name__)


# ─── PURE EVALUATION (no I/O — testable with fixture payloads) ──

def format_ct(dt: datetime) -> str:
    return dt.astimezone(CHICAGO).strftime('%H:%M CT')


def evaluate_child(accounting: dict) -> str:
    """'healthy' | 'residual_positive' | 'residual_impossible'"""
    residual = accounting['child']['child_identity_residual']
    if residual == 0:
        return 'healthy'
    return 'residual_positive' if residual > 0 else 'residual_impossible'


def evaluate_titles(accounting: dict) -> str:
    """'healthy' | 'mismatch'"""
    pt = accounting['parent_titles']
    expected = pt['titles_written'] + (1 if pt['title_capture_failed'] else 0)
    return 'healthy' if pt['titles_new_or_changed'] == expected else 'mismatch'


def evaluate_staleness(accounting: dict | None, now: datetime) -> str:
    """'healthy' | 'stale' | 'absent'"""
    if not accounting or not accounting.get('as_of'):
        return 'absent'
    try:
        as_of = datetime.fromisoformat(accounting['as_of'])
    except ValueError:
        return 'absent'
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    return 'healthy' if (now - as_of) <= STALE_THRESHOLD else 'stale'


def build_message(check: str, status: str, accounting: dict | None,
                   now: datetime, heartbeat_value: str | None) -> str:
    as_of_dt = None
    if accounting and accounting.get('as_of'):
        try:
            as_of_dt = datetime.fromisoformat(accounting['as_of'])
            if as_of_dt.tzinfo is None:
                as_of_dt = as_of_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            as_of_dt = None
    as_of_ct = format_ct(as_of_dt) if as_of_dt else 'unknown'

    if check == 'child':
        c = accounting['child']
        if status == 'residual_positive':
            return (f"AP sync child identity broke: {c['child_tasks_total']} fetched, "
                     f"residual {c['child_identity_residual']} (as_of {as_of_ct}). "
                     f"Investigate sync_ap.py writes.")
        if status == 'residual_impossible':
            return (f"AP sync child identity IMPOSSIBLE: {c['child_tasks_total']} fetched, "
                     f"residual {c['child_identity_residual']} (as_of {as_of_ct}) — negative, "
                     f"a counter is double-firing. Investigate sync_ap.py accounting.")
        return f"AP sync monitor: recovered, identity closing again (as_of {as_of_ct})."

    if check == 'titles':
        pt = accounting['parent_titles']
        if status == 'mismatch':
            failed_txt = '1 failed' if pt['title_capture_failed'] else '0 failed'
            return (f"AP sync titles mismatch: {pt['titles_new_or_changed']} changed, "
                     f"{pt['titles_written']} written, {failed_txt} (as_of {as_of_ct}).")
        return f"AP sync monitor: recovered, titles delta-integrity closing again (as_of {as_of_ct})."

    if check == 'stale':
        if status == 'stale':
            age = now - as_of_dt
            hours, rem = divmod(int(age.total_seconds()), 3600)
            minutes = rem // 60
            return (f"AP sync stale: last accounting {hours}h{minutes}m ago "
                     f"(as_of {as_of_ct}), expected within 4h.")
        if status == 'absent':
            if heartbeat_value:
                try:
                    hb_dt = datetime.fromisoformat(heartbeat_value.rstrip('Z'))
                    if hb_dt.tzinfo is None:
                        hb_dt = hb_dt.replace(tzinfo=timezone.utc)
                    hb_txt = f", last heartbeat {format_ct(hb_dt)}"
                except ValueError:
                    hb_txt = ", heartbeat present but unparsable"
            else:
                hb_txt = ", no heartbeat found either"
            return f"AP sync stale: no accounting emitted yet{hb_txt}."
        return f"AP sync monitor: recovered, accounting fresh again (as_of {as_of_ct})."

    raise ValueError(f"unknown check {check!r}")


def evaluate_and_dedup(check: str, status: str, state: dict, now: datetime):
    """
    Decides whether this check's status change warrants a push, per the
    transition + 12h re-nag rule (decision doc §3.4):
      healthy -> broken:        push (transition)
      broken  -> different broken: push (transition — a distinct failure mode)
      still broken, <12h since last push: quiet
      still broken, >=12h since last push: re-nag
      broken  -> healthy:       push once (recovery), state resets
      healthy -> healthy:       quiet
    Returns (should_push, new_state_entry_for_this_check).
    """
    prev = state.get(check, {"status": "healthy", "last_push": None})
    push = False
    if status != prev["status"]:
        push = True
    elif status != 'healthy':
        if prev["last_push"] is None:
            push = True
        else:
            last_push_dt = datetime.fromisoformat(prev["last_push"])
            if (now - last_push_dt) >= DEDUP_WINDOW:
                push = True
    new_entry = {
        "status": status,
        "last_push": now.isoformat() if push else prev["last_push"],
    }
    return push, new_entry


def run_monitor(accounting: dict | None, heartbeat_value: str | None,
                 now: datetime, state: dict):
    """
    Pure orchestration over the three checks. Returns
    (evaluations: dict[str, str|None], new_state: dict, pushes: list[(title, message)]).
    child/titles are evaluated only when accounting is present — with no
    accounting payload there is no child/parent_titles data to check, and
    forcing a verdict would just be a second way of saying "absent" (the
    stale check already covers that).
    """
    new_state = {k: dict(v) for k, v in state.items()}
    evaluations = {}
    pushes = []

    stale_status = evaluate_staleness(accounting, now)
    evaluations['stale'] = stale_status
    push, entry = evaluate_and_dedup('stale', stale_status, state, now)
    new_state['stale'] = entry
    if push:
        title = ("AP sync monitor — recovered (staleness)" if stale_status == 'healthy'
                  else "AP sync monitor — sync stale/absent")
        pushes.append((title, build_message('stale', stale_status, accounting, now, heartbeat_value)))

    if accounting is not None:
        for check, evaluator in (('child', evaluate_child), ('titles', evaluate_titles)):
            status = evaluator(accounting)
            evaluations[check] = status
            push, entry = evaluate_and_dedup(check, status, state, now)
            new_state[check] = entry
            if push:
                title = (f"AP sync monitor — recovered ({check})" if status == 'healthy'
                          else f"AP sync monitor — {check} broken")
                pushes.append((title, build_message(check, status, accounting, now, heartbeat_value)))
    else:
        evaluations['child'] = None
        evaluations['titles'] = None

    return evaluations, new_state, pushes


# ─── I/O ──────────────────────────────────────────────────────────

def fetch_context_value(sc, key: str) -> str | None:
    resp = sc.table('context_store').select('value').eq('key', key).execute()
    if resp.data and resp.data[0].get('value') is not None:
        return resp.data[0]['value']
    return None


def save_alert_state(sc, state: dict) -> None:
    sc.table('context_store').upsert({
        'key':    ALERT_STATE_KEY,
        'value':  json.dumps(state),
        'domain': 'system',
        'notes':  (
            'AP sync reconciliation monitor — internal dedup state '
            '(Phase 2, decision 2026-08-25-ap-sync-reconciliation-monitor-design.md, '
            'action item 9278f68e). Not a health signal itself; read only by this script.'
        ),
    }, on_conflict='key').execute()


def send_push(sc, title: str, message: str) -> None:
    sc.table('notification_queue').insert({
        'title':   title,
        'message': message[:1000],
    }).execute()
    log.info(f"Pushed: {title} — {message}")


# ─── MAIN ───────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="AP sync reconciliation monitor (Phase 2, 9278f68e)")
    parser.add_argument(
        "--accounting-file",
        help="Load the accounting payload from this local JSON file instead of SAM COS. "
             "Testing only — requires --state-file too, so a synthetic run can never "
             "read or write the live alert-state key.",
    )
    parser.add_argument(
        "--state-file",
        help="Read/write dedup alert-state to this local JSON file instead of SAM COS. "
             "Testing only.",
    )
    parser.add_argument(
        "--now",
        help="Override 'now' as an ISO 8601 UTC timestamp. Testing only.",
    )
    args = parser.parse_args()
    if bool(args.accounting_file) != bool(args.state_file):
        parser.error("--accounting-file and --state-file must be used together — "
                      "a synthetic accounting payload must never be evaluated against "
                      "(or write into) the live alert-state key.")
    return args


def main():
    args = parse_args()
    testing = bool(args.accounting_file)

    now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    sc = None
    heartbeat_value = None

    if testing:
        accounting_raw = Path(args.accounting_file).read_text()
        accounting = json.loads(accounting_raw) if accounting_raw.strip() else None
        state_path = Path(args.state_file)
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
    else:
        if not SAMCOS_SERVICE_KEY:
            raise SystemExit("ERROR: SAMCOS_SERVICE_KEY environment variable is not set.")
        sc = create_client(SAMCOS_SUPABASE_URL, SAMCOS_SERVICE_KEY)
        accounting_raw = fetch_context_value(sc, ACCOUNTING_KEY)
        heartbeat_value = fetch_context_value(sc, HEARTBEAT_KEY)
        accounting = json.loads(accounting_raw) if accounting_raw else None
        state_raw = fetch_context_value(sc, ALERT_STATE_KEY)
        state = json.loads(state_raw) if state_raw else {}

    log.info(f"Accounting: {json.dumps(accounting) if accounting else 'MISSING'}")
    log.info(f"Heartbeat: {heartbeat_value or 'MISSING'}")

    evaluations, new_state, pushes = run_monitor(accounting, heartbeat_value, now, state)
    log.info(f"Evaluations: {evaluations}")

    for title, message in pushes:
        if testing:
            log.info(f"[TEST] would push: {title} — {message}")
        else:
            send_push(sc, title, message)

    if testing:
        state_path.write_text(json.dumps(new_state, indent=2))
    else:
        save_alert_state(sc, new_state)

    if not pushes:
        log.info("Healthy — no alert.")


if __name__ == '__main__':
    main()
