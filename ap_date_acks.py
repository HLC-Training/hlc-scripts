"""
ap_date_acks.py
─────────────────────────────────────────────────────────────────
Shared data layer for the "AP date changes awaiting acknowledgment"
digest section (bug ca9beaeb Phase 3, 2026-09-14; the section deferred
from 1d039530 as SAM COS 5bd11694). Factored out the way ge_holidays.py
was: send_pll_digest.py and send_tpm_digest.py both import
fetch_unacked_ap_date_changes from here — do not fork a second copy.

WHAT "OUTSTANDING" MEANS — one definition, mirrored from the app
(orion-pll lib/ap-date-changes.ts fetchOwnOutstandingApDateChanges, the
same rule the in-app banner renders from):
  for an OWNER, every top-level AP where
    - they own at least one live row under it (the caller says which
      table and which statuses count as closed), and
    - ap_end_date_changes holds at least one event for that AP that is
      not dismissed (dismissed_at IS NULL) and that THIS owner has no
      ap_end_date_acks row for.
  One entry per AP: the latest old → new plus a count of earlier,
  still-unacked events — the same collapse the badge popover and the
  banner use, so a burst of moves on one AP is one line, not five.

Events are written by sync_ap.py when a top-level parent's Current
Finish moves in Smartsheet; acks are written only by the ORiON server
action (one ack per owner per event; one click acks every outstanding
event for the AP). The digest READS both, never writes either — the
only way to acknowledge is in ORiON. No email-side ack, no tokenized
link: that is the GE-mail-consumes-single-use-tokens trap from the
August TPM onboarding (bugs cab4810f / 065c166e).

Owner ids: action_items.owner_id / pc_projects.owner_id and
ap_end_date_acks.user_id share the portal_users id space (verified
2026-09-14: 0 of 7 PLLs differ between users.id and portal_users.id).
─────────────────────────────────────────────────────────────────
"""

import re
from collections import defaultdict

_TOP_LEVEL_AP = re.compile(r'^(AP-\d+)')   # same regex as orion-pll lib/ap-grouping.ts


def top_level_ap(ap_number) -> str | None:
    if not ap_number:
        return None
    m = _TOP_LEVEL_AP.match(ap_number)
    return m.group(1) if m else None


def fetch_unacked_ap_date_changes(db, owner_ids: list[str], table: str,
                                  closed_statuses: list[str]) -> dict[str, list[dict]]:
    """Return {owner_id: [entry, ...]} for owners with outstanding acks.

    table            — 'action_items' (Delivery) or 'pc_projects' (P&C)
    closed_statuses  — statuses whose rows do NOT count as the owner's
                       plate (['Done'] for Delivery; the P&C TERMINAL set)

    Each entry: {ap, title, old_end_date, new_end_date, detected_at,
                 earlier, rows}. Owners with nothing outstanding are
                 simply absent. Any Supabase failure raises — callers
                 treat this like every other fetch (abort, exit non-zero).
    """
    if not owner_ids:
        return {}
    rows = db.table(table).select('owner_id, ap_number') \
        .in_('owner_id', owner_ids) \
        .not_.in_('status', closed_statuses) \
        .not_.is_('ap_number', 'null') \
        .execute().data or []

    rows_by_owner_ap: dict[tuple[str, str], int] = defaultdict(int)
    for r in rows:
        top = top_level_ap(r.get('ap_number'))
        if top:
            rows_by_owner_ap[(r['owner_id'], top)] += 1
    if not rows_by_owner_ap:
        return {}
    aps = sorted({ap for (_, ap) in rows_by_owner_ap})

    events = db.table('ap_end_date_changes') \
        .select('id, ap_number, old_end_date, new_end_date, detected_at') \
        .in_('ap_number', aps) \
        .is_('dismissed_at', 'null') \
        .order('detected_at', desc=True) \
        .execute().data or []
    if not events:
        return {}

    acks = db.table('ap_end_date_acks').select('change_id, user_id') \
        .in_('change_id', [e['id'] for e in events]) \
        .in_('user_id', owner_ids) \
        .execute().data or []
    acked = {(a['user_id'], a['change_id']) for a in acks}

    titles = db.table('ap_titles').select('ap_number, title') \
        .in_('ap_number', aps).execute().data or []
    title_by_ap = {t['ap_number']: t['title'] for t in titles if t.get('title')}

    events_by_ap: dict[str, list[dict]] = defaultdict(list)
    for e in events:                      # already newest-first
        events_by_ap[e['ap_number']].append(e)

    out: dict[str, list[dict]] = defaultdict(list)
    for (owner_id, ap), n_rows in rows_by_owner_ap.items():
        outstanding = [e for e in events_by_ap.get(ap, [])
                       if (owner_id, e['id']) not in acked]
        if not outstanding:
            continue
        latest = outstanding[0]
        out[owner_id].append({
            'ap':           ap,
            'title':        title_by_ap.get(ap),
            'old_end_date': latest['old_end_date'],
            'new_end_date': latest['new_end_date'],
            'detected_at':  latest['detected_at'],
            'earlier':      len(outstanding) - 1,
            'rows':         n_rows,
        })
    for owner_id in out:
        out[owner_id].sort(key=lambda x: x['ap'])
    return dict(out)


def ap_change_label(entry: dict) -> str:
    """'AP-0785 — Title' or just 'AP-0785' when the tracker has no name."""
    return f"{entry['ap']} — {entry['title']}" if entry.get('title') else entry['ap']


def ap_change_meta_text(entry: dict, fmt_date) -> str:
    """Plain-text meta line; fmt_date is the caller's own date formatter."""
    s = (f"End date moved {fmt_date(entry['old_end_date'])} -> "
         f"{fmt_date(entry['new_end_date'])}")
    if entry['earlier']:
        s += (f" | +{entry['earlier']} earlier change"
              f"{'s' if entry['earlier'] > 1 else ''}, acknowledged together")
    s += f" | {entry['rows']} of your item{'s' if entry['rows'] != 1 else ''}"
    return s
