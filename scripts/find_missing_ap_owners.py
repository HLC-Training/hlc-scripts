#!/usr/bin/env python3
"""
find_missing_ap_owners.py
─────────────────────────────────────────────────────────────────
Phase 1 discovery (AP owner provisioning, foundation for bug ca9beaeb /
the OPS module launch). Read-only. Pulls the distinct, non-empty Lead
values from the AP Tracker sheet (SHEET_ID, COL_LEAD — reusing sync_ap's
own fetch/resolve machinery so this never re-derives that logic), then
diffs them against portal_users (per the brief: portal_users is the
diff target, not `users` — PLLs already have portal_users rows too).

This is a discovery report ONLY. It creates nothing. Its output is the
missing-owner table for the hard-stop gate; Jim assigns module/role from
it, and a separate provisioning step (not this script) acts on his list.

Not scheduled. Run manually:
    python scripts/find_missing_ap_owners.py
"""

import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import sync_ap as s  # noqa: E402 — reuse fetch_ap_rows/resolve_lead_email verbatim
from supabase import create_client  # noqa: E402


def normalize_name(raw: str) -> str:
    """Lowercase/trim, and fold 'Last, First' into 'First Last' so both
    orderings compare equal. Collapses internal whitespace."""
    if not raw:
        return ""
    v = raw.strip()
    if "," in v:
        parts = [p.strip() for p in v.split(",", 1)]
        if len(parts) == 2 and parts[0] and parts[1]:
            v = f"{parts[1]} {parts[0]}"
    return " ".join(v.lower().split())


def main():
    db = create_client(s.SUPABASE_URL, s.ORION_SUPABASE_SERVICE_KEY)

    portal_resp = db.table('portal_users').select('id, name, email, role, module').execute()
    users_resp = db.table('users').select('id, name, email, role').execute()
    portal_users = portal_resp.data
    users = users_resp.data

    portal_email_set = {p['email'].strip().lower() for p in portal_users if p.get('email')}
    portal_name_set = {normalize_name(p['name']) for p in portal_users if p.get('name')}
    users_email_set = {u['email'].strip().lower() for u in users if u.get('email')}
    users_name_set = {normalize_name(u['name']) for u in users if u.get('name')}

    print(f"portal_users: {len(portal_users)} rows loaded")
    print(f"users: {len(users)} rows loaded")

    # Same alias/name-to-email construction sync_ap.main() uses, so a Lead
    # that already resolves via an existing alias or exact name match isn't
    # miscounted as "missing" just because this script built its own maps.
    aliases_resp = db.table('ap_lead_aliases').select('smartsheet_value, resolved_email').execute()
    alias_map = {
        a['smartsheet_value'].strip().lower(): a['resolved_email'].strip().lower()
        for a in aliases_resp.data if a.get('smartsheet_value') and a.get('resolved_email')
    }
    name_to_email = {}
    for u in users:
        if u.get('name') and u.get('email'):
            name_to_email[u['name'].strip().lower()] = u['email'].strip().lower()
    for p in portal_users:
        if p.get('name') and p.get('email'):
            name_to_email[p['name'].strip().lower()] = p['email'].strip().lower()

    tasks, parent_titles, fetch_complete, all_row_ids = s.fetch_ap_rows()
    print(f"Smartsheet fetch_complete={fetch_complete}, {len(tasks)} AP rows")

    # identity -> aggregated context
    groups = defaultdict(lambda: {
        "display_variants": set(),
        "ap_numbers": [],
        "buckets": set(),
        "sources": set(),
        "active_count": 0,
        "total_count": 0,
        "resolved_email": None,
    })

    blank_lead_count = 0
    for t in tasks:
        lead_value = t.get('lead_value') or ""
        lead_display = t.get('lead_display') or ""
        if not lead_value.strip() and not lead_display.strip():
            blank_lead_count += 1
            continue

        resolved = s.resolve_lead_email(lead_value, lead_display, alias_map, name_to_email)
        display_text = lead_display.strip() or lead_value.strip()

        if resolved:
            identity = resolved  # lowercased email already
        else:
            identity = normalize_name(display_text)
            if not identity:
                blank_lead_count += 1
                continue

        g = groups[identity]
        g["display_variants"].add(display_text)
        g["resolved_email"] = resolved or g["resolved_email"]
        g["total_count"] += 1
        if t.get('active'):
            g["active_count"] += 1
        if len(g["ap_numbers"]) < 5:
            g["ap_numbers"].append(t.get('ap_number') or '(blank)')
        if t.get('bucket'):
            g["buckets"].add(t['bucket'])
        if t.get('source_raw'):
            g["sources"].add(t['source_raw'])

    print(f"Distinct lead identities: {len(groups)} (blank-lead rows skipped: {blank_lead_count})")

    existing = []
    missing = []
    for identity, g in groups.items():
        is_existing = False
        if g["resolved_email"]:
            if g["resolved_email"] in portal_email_set or g["resolved_email"] in users_email_set:
                is_existing = True
        else:
            if identity in portal_name_set or identity in users_name_set:
                is_existing = True
        (existing if is_existing else missing).append((identity, g))

    print(f"Already ORiON users: {len(existing)}")
    print(f"MISSING (no ORiON user): {len(missing)}")

    print("\n" + "=" * 100)
    print("MISSING LEADS — needs Jim's module/role assignment (see hard-stop gate)")
    print("=" * 100)
    print(f"{'Name (as seen)':32} | {'#rows(active)':14} | {'sample AP#s':28} | Bucket context")
    print("-" * 100)
    for identity, g in sorted(missing, key=lambda kv: -kv[1]["total_count"]):
        name_disp = " / ".join(sorted(g["display_variants"]))[:32]
        rows_str = f"{g['total_count']}({g['active_count']} active)"
        aps = ", ".join(g["ap_numbers"])[:28]
        buckets = ", ".join(sorted(g["buckets"])) or "(none)"
        resolved_note = f" [resolves to: {g['resolved_email']}]" if g["resolved_email"] else " [no email — free text]"
        print(f"{name_disp:32} | {rows_str:14} | {aps:28} | {buckets}{resolved_note}")

    print("\n" + "=" * 100)
    print("ALREADY EXISTING (excluded from missing list)")
    print("=" * 100)
    for identity, g in sorted(existing, key=lambda kv: kv[0]):
        name_disp = " / ".join(sorted(g["display_variants"]))[:40]
        print(f"{name_disp:40} rows={g['total_count']}")


if __name__ == '__main__':
    main()
