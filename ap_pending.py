"""
ap_pending.py
─────────────────────────────────────────────────────────────────
Shared reconciliation semantics for the ap_pending_update lifecycle —
the ONE place that decides whether an ORiON edit to an AP row has been
reconciled in the Smartsheet Action Plan Tracker. Imported by both
sync_ap.py (which clears the flag) and send_ap_pending_digest.py (which
renders what is still open), so the two can never disagree about what
"reconciled" means. Decision: knowledge/decisions/
2026-09-08-ap-pending-clear-and-direction.md (bug 424c1637).

Rules, per (row, field), where the field set is the ap_change_log entries
written during the row's CURRENT flag episode (changed_at >=
ap_pending_since; latest entry per field):

  matched        tracker value == the value ORiON holds / asked for
                 -> reconciled, drop.
  tracker_newer  values differ, but the tracker row was modified AFTER the
                 ORiON edit (smartsheet_modified_at > changed_at) -> the
                 tracker is source of truth and Jen's later edit wins; the
                 flag clears, the digest does NOT ask her to re-apply.
                 Row-grained on purpose: GET /sheets exposes only a row-level
                 modifiedAt; a sheet-wide save therefore supersedes every
                 older pending edit on the rows it touched (known, accepted,
                 documented in the decision).
  pending        values differ and the tracker is not newer -> still open;
                 the digest lists it, the sync protects the row.
  not_comparable the field has no tracker-side projection this module
                 knows how to compute (owner_id) -> treated as pending by
                 the digest (conservative: keep showing it) and left to the
                 sync's own content diff.

A row's flag clears iff every episode field is matched or tracker_newer.
Rows flagged with NO episode entries (legacy flags, or an unlogged writer)
fall back to the pre-existing whole-row rule in sync_ap.py, unchanged.

Blank tracker cells: a None tracker value counts as `matched` for every
field except pc.description. This preserves bug ac29261e (wont_fix) — a
blanked Smartsheet date releases the hold as "caught up" (source of truth
spoke) — and the 8/12 per-field null-guard rulings (dates and category are
guarded; a cleared description is intent and mirrors). Do not widen.

Fixtures: is_fixture() is the single rule that keeps seeded test rows out
of Jen's inbox. Conventions confirmed 2026-09-08 against orion-pll's
scripts/authz-harness seeds: AP-99xx is the reserved fixture AP range
(AP-9901 ops-closeout, AP-9902 the row that reached the 09-04 digest,
AP-9999 smartsheet.test), titles carry "TEST FIXTURE" (prefix "TEST FIXTURE
— ..." or bracketed "[TEST FIXTURE — safe to delete, ...]"), and mirror
rows carry a synthetic smartsheet_row_id >= 999999990000.
─────────────────────────────────────────────────────────────────
"""

import re
from datetime import datetime, timezone

# ─── SMARTSHEET → ORiON VOCAB (moved here from sync_ap.py 2026-09-08 so the
# digest can project tracker rows with the sync's own maps) ───────────────
# Smartsheet Overall Status → ORiON (Delivery / action_items) status
STATUS_MAP = {
    "Not Started": "Open",
    "In Progress":  "In Progress",
    "On Hold":      "Deferred",
    "Complete":     "Done",
    "Cancelled":    "Done",
}

# Smartsheet Overall Status → P&C (pc_projects) status.
# Active statuses flow through the normal update path; inactive ones
# (On Hold / Complete / Cancelled) reach an existing row only via the
# status-only close pass in sync_ap.py or the pending-settle diff — never
# as inserts. Before the inactive mappings existed (bug e6b35596), a
# pending P&C row that went Complete in Smartsheet diffed status against
# None and could never settle its flag.
PC_STATUS_MAP = {
    "Not Started": "approved",
    "In Progress": "active",
    "On Hold":     "on_hold",
    "Complete":    "complete",
    "Cancelled":   "cancelled",
}

# SQDCGP first letter → ORiON category (P = People added 2026-09-01,
# decision 2026-09-01-sync-sqdcgp-people-map-and-backfill.md).
SQDCG_MAP = {
    "S": "Safety",
    "Q": "Quality",
    "D": "Delivery",
    "C": "Cost",
    "G": "Strategy",
    "P": "People",
}

# Module statuses that mean "closed" — used by the digest's removed-from-
# tracker section, which speaks of rows "still open in ORiON". Values
# confirmed against the live tables 2026-09-08 (action_items: Open /
# In Progress / Deferred / Done; pc_projects: approved / active / on_hold /
# complete / cancelled).
TERMINAL_STATUSES = {
    'delivery': {'Done'},
    'pc':       {'complete', 'cancelled'},
}


def map_status(status_map: dict, status_raw) -> str | None:
    """Case-tolerant Overall Status lookup ('Not started' exists in the
    sheet). None for blank or unmapped — the caller decides the default
    (sync_ap.map_module_status warns and defaults; the digest treats None
    as a blank cell)."""
    if not status_raw:
        return None
    s = str(status_raw).strip()
    if s in status_map:
        return status_map[s]
    for k, v in status_map.items():
        if k.lower() == s.lower():
            return v
    return None


def map_category(sqdcg_raw: str | None) -> str | None:
    """Map first SQDCG letter to ORiON category. Unmapped and blank both
    return None by design (blank-cell ruling 8/11); sync_ap.py's caller
    warns on the seen-but-unmapped case."""
    if not sqdcg_raw:
        return None
    for char in sqdcg_raw.upper():
        if char in SQDCG_MAP:
            return SQDCG_MAP[char]
    return None


# ─── FIXTURE EXCLUSION ────────────────────────────────────────────────────
FIXTURE_AP_RE      = re.compile(r"^\s*AP-99\d{2}(?:-|\s*$)", re.IGNORECASE)
FIXTURE_TITLE_RE   = re.compile(r"TEST\s+FIXTURE", re.IGNORECASE)
# Seeds use 12-digit ids in the 9999999xxxxx band (999999990001, ...0901);
# real Smartsheet row ids are 15-16 digits (e.g. 4660350745706372), so the
# band must be bounded on both sides — a plain ">=" would match every real
# row (caught by tests/test_ap_pending.py on first run).
FIXTURE_ROW_ID_MIN = 999_999_990_000
FIXTURE_ROW_ID_MAX = 999_999_999_999


def is_fixture(ap_number, title=None, smartsheet_row_id=None) -> bool:
    """True for any row that matches a seeded-fixture convention. Any one
    signal is enough — the goal is "no fixture ever reaches Jen's inbox",
    not a precise classifier."""
    if ap_number and FIXTURE_AP_RE.match(str(ap_number)):
        return True
    if title and FIXTURE_TITLE_RE.search(str(title)):
        return True
    try:
        if smartsheet_row_id is not None and FIXTURE_ROW_ID_MIN <= int(smartsheet_row_id) <= FIXTURE_ROW_ID_MAX:
            return True
    except (TypeError, ValueError):
        pass
    return False


# ─── TRACKER-SIDE PROJECTION ──────────────────────────────────────────────
# Which ap_tracker column each sync-mirrored module field is projected
# from, and how. These MUST agree with sync_ap.py's task capture:
#   Delivery/P&C start_date  <- COL_START  ("Start Date (date only)")  = ap_tracker.start_date_only
#   Delivery due_date /
#   P&C target_end_date      <- COL_FINISH ("Current Finish")          = ap_tracker.current_finish
#   P&C title                <- COL_IMPROVEMENT                        = ap_tracker.improvement
#   P&C description          <- COL_DESCRIPTION                        = ap_tracker.description
#   status                   <- COL_STATUS via STATUS_MAP/PC_STATUS_MAP = ap_tracker.overall_status
#   P&C category             <- COL_SQDCG via SQDCG_MAP                = ap_tracker.sqdcgp
# NOTE: ap_tracker.current_start is a DIFFERENT sheet column (Current
# Start) from the one the module start_date is built from — do not
# "fix" this to current_start.
NOT_COMPARABLE = object()

_PROJECTORS = {
    'delivery': {
        'status':     lambda t: map_status(STATUS_MAP, t.get('overall_status')),
        'due_date':   lambda t: t.get('current_finish'),
        'start_date': lambda t: t.get('start_date_only'),
    },
    'pc': {
        'title':           lambda t: t.get('improvement'),
        'description':     lambda t: t.get('description'),
        'status':          lambda t: map_status(PC_STATUS_MAP, t.get('overall_status')),
        'category':        lambda t: map_category(t.get('sqdcgp')),
        'start_date':      lambda t: t.get('start_date_only'),
        'target_end_date': lambda t: t.get('current_finish'),
        # owner_id needs the sync's lead-resolution chain (aliases, name
        # match, role filter); not reproduced here.
    },
}

# Fields where a blank tracker cell is a real value (cleared on purpose),
# not a guarded gap. Everything else treats None as "no divergence" — see
# the module docstring (ac29261e, 8/12 null-guard rulings).
BLANK_IS_INTENT = {('pc', 'description')}

_WS_RE = re.compile(r"\s+")


def project_tracker_field(module: str, field: str, tracker_row: dict):
    """The module-shaped value the sync would write for `field` from this
    tracker row, or NOT_COMPARABLE when this module has no projector for
    the field."""
    proj = _PROJECTORS.get(module, {}).get(field)
    if proj is None:
        return NOT_COMPARABLE
    return proj(tracker_row)


def normalize(value) -> str | None:
    """Comparison form: None/'' -> None; otherwise stripped text with
    internal whitespace runs collapsed. Dates from Supabase ('YYYY-MM-DD')
    and from parse_due_date are already the same shape; text fields differ
    between a modal (newlines) and a sheet cell (single line) in ways that
    carry no meaning for Jen."""
    if value is None:
        return None
    s = _WS_RE.sub(" ", str(value)).strip()
    return s or None


def parse_ts(raw) -> datetime | None:
    if not raw:
        return None
    if isinstance(raw, datetime):
        ts = raw
    else:
        try:
            ts = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
        except ValueError:
            return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def classify_field(module: str, field: str, expected, tracker_value,
                   tracker_modified_at, changed_at) -> str:
    """One field's reconciliation state — see the module docstring."""
    if tracker_value is NOT_COMPARABLE:
        return 'not_comparable'
    if tracker_value is None and (module, field) not in BLANK_IS_INTENT:
        return 'matched'
    if normalize(expected) == normalize(tracker_value):
        return 'matched'
    sheet_mod = parse_ts(tracker_modified_at)
    edited_at = parse_ts(changed_at)
    if sheet_mod and edited_at and sheet_mod > edited_at:
        return 'tracker_newer'
    return 'pending'


# The fields sync_ap.py mirrors from Smartsheet per module — the only
# fields whose ORiON edit can diverge from the sheet, and therefore the
# only ones a flag episode is judged on. Mirrors SYNC_MIRRORED_FIELDS in
# orion-pll lib/ap-pending.ts (the raise side). A change-log entry for any
# other field (Delivery `priority` — tier reason capture, never mirrored,
# ap-tier-authority decision 2026-08-20) rides in the same episode but must
# not hold the flag open: the tracker has no column it could ever match.
SYNC_MIRRORED_FIELDS = {
    'delivery': {'status', 'due_date', 'start_date'},
    'pc': {'title', 'description', 'status', 'category', 'start_date',
           'target_end_date', 'owner_id'},
}


# The orion-pll writers stamp ap_pending_since from the changed_at of the
# LAST ap_change_log insert of a save, but a save that touches both a
# reason-gated field and a system-edit field performs TWO inserts ~80 ms
# apart (observed live 2026-09-08: AP-0547-1-2 target_end_date at
# 14:37:28.430 vs since 14:37:28.513) — so a strict `changed_at >= since`
# silently drops the first insert's entries from the episode. A short
# grace window keeps them; a re-flag after a clear is separated from the
# previous episode by at least one sync cycle (30 min), so nothing older
# can leak in. App-side fix tracked separately (stamp since from the
# earliest insert).
EPISODE_SKEW_SECONDS = 5


def in_episode(entry: dict, pending_since) -> bool:
    """True when a change-log entry belongs to the row's current flag
    episode (every entry counts when pending_since is unset)."""
    since = parse_ts(pending_since)
    if since is None:
        return True
    ts = parse_ts(entry.get('changed_at'))
    if ts is None:
        return True
    return (since - ts).total_seconds() <= EPISODE_SKEW_SECONDS


def latest_episode_entries(module: str, entries: list[dict], pending_since) -> dict:
    """field -> the latest ap_change_log entry for that field within the
    current flag episode (see in_episode), restricted to the module's
    sync-mirrored fields. Empty when the row has no logged mirrored edits."""
    mirrored = SYNC_MIRRORED_FIELDS.get(module, set())
    latest: dict = {}
    for e in entries:
        if e.get('field') not in mirrored:
            continue
        if not in_episode(e, pending_since):
            continue
        ts = parse_ts(e.get('changed_at'))
        cur = latest.get(e['field'])
        if cur is None or (ts and (parse_ts(cur.get('changed_at')) or ts) <= ts):
            latest[e['field']] = e
    return latest


def classify_pending(module: str, tracker_row: dict | None, episode: dict,
                     expected_for_field) -> dict:
    """field -> state for every field in `episode`. `expected_for_field(field,
    entry)` returns the value to compare the tracker against — the digest
    passes the entry's new_value (what it would ask Jen to apply); the sync
    passes the ORiON row's current value (what the row actually holds).
    With no tracker row at all nothing can be judged: every field is
    'pending' (the row is on its way to orphan detection, not to
    reconciliation)."""
    out = {}
    for field, entry in episode.items():
        if tracker_row is None:
            out[field] = 'pending'
            continue
        out[field] = classify_field(
            module, field,
            expected_for_field(field, entry),
            project_tracker_field(module, field, tracker_row),
            tracker_row.get('smartsheet_modified_at'),
            entry.get('changed_at'),
        )
    return out


def row_is_reconciled(states: dict) -> bool:
    """A flagged row settles iff every episode field is matched or
    superseded by a newer tracker edit. An empty episode is NOT settled by
    this rule — callers fall back to their legacy whole-row check."""
    return bool(states) and all(s in ('matched', 'tracker_newer') for s in states.values())


def pick_tracker_row(rows: list[dict], pure_parent: bool) -> dict | None:
    """Choose the ap_tracker row a module row compares against. Module
    rows key on (ap_number, pure_parent) — three sheet families (AP-036,
    AP-093, AP-174) carry a parent row and a child row under one flat AP
    number — so match the shape first; among same-shape rows prefer the
    most recently synced (a stale sibling left behind by a Smartsheet
    renumber has an older last_synced_at). Returns None when nothing
    matches."""
    same_shape = [
        r for r in rows
        if (bool(r.get('is_parent')) and not bool(r.get('is_child'))) == pure_parent
    ]
    candidates = same_shape or rows
    if not candidates:
        return None
    return max(candidates, key=lambda r: (
        parse_ts(r.get('last_synced_at')) or datetime.min.replace(tzinfo=timezone.utc),
        parse_ts(r.get('smartsheet_modified_at')) or datetime.min.replace(tzinfo=timezone.utc),
    ))
