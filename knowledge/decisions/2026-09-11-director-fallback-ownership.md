# 2026-09-11 Director-as-Fallback Ownership (Phase 2b, bug ca9beaeb)

**Status:** SHIPPED  
**Repo:** hlc-scripts — sync_ap.py  
**Component:** resolve_owner() + routing pre-pass  

## Problem

`resolve_owner()` skipped directors when assigning AP ownership — correct for the normal case (a director oversees; a TPM/PLL executes and owns). But when a director was the AP's ONLY lead with no executing owner beneath, the AP ended up unowned and fell out of the ack loop.

**Live evidence:** 17 active P&C APs (families AP-0917, AP-1073, AP-1078 + children), all Michele-Alcantar-led, all owner_id NULL — verified none had a TPM/PLL lead being skipped.

## Decision

Director-as-fallback. If an AP's lead resolves to a director AND no other executing owner exists, the director becomes owner for ack purposes. Must NOT override a TPM/PLL who should own — director only wins when there's no one else.

## Implementation

### Change 1: resolve_owner() logic

**File:** sync_ap.py::resolve_owner  
**Lines:** 473–512 (was 473–497)

- Signature expanded to accept optional `all_portal_users: list` parameter
- New return destination: `'director'` when email resolves to portal_users with role='director' (detected via scan of all_portal_users, not in tpm-filtered portal_email_to_id)
- Maintains existing destinations: 'delivery' (PLL/admin), 'pc' (TPM), 'ambiguous' (both), 'viewer', 'none'

### Change 2: Routing pre-pass director fallback

**File:** sync_ap.py::main() routing pre-pass  
**Lines:** 1645–1690 (was 1645–1659)

1. Expanded resolve_owner() call to pass `all_portal_users=portal_resp.data`
2. Track per-family: `fam_directors` dict recording whether family has executing owner or only director
3. Second pass over tasks: if task destination is 'director' AND family has no executing owner, re-assign:
   - destination ← 'pc'
   - owner_id ← director's portal_users id
   - Log the fallback decision

### Backfill

**17 Michele-Alcantar-led active P&C APs** (before: owner_id=NULL; after: owner_id=2e079ba6-558e-4d26-b7ab-8e6720b562e7)

Families affected: AP-0917 (9 rows), AP-1073 (4 rows), AP-1078 (4 rows)
- All have "Michele Alcantar" in ap_lead_display
- All have status in [approved, in_progress, on_hold]
- All resolved to Michele's director id: 2e079ba6-558e-4d26-b7ab-8e6720b562e7

## Verification

### Fixture Tests (test_director_fallback.py)

- **(a) Director-only lead:** An AP whose only lead is a director → director assigned ✓
- **(b) Director + executing owner:** An AP with director lead AND resolvable TPM → TPM owns, NOT director ✓
- **PLL override:** Director lead + PLL lead → PLL owns ✓
- **Non-director roles:** CPM role outside tpm-map → returns 'none', not 'director' ✓

### Backfill Before/After

**Before:**
- 19 unowned active P&C APs total
  - 17 Michele Alcantar (director-led)
  - 2 "TBD from D&D Team" (different issue — out of scope Phase 2b)

**After:**
- 2 unowned active P&C APs (TBD cases, untouched per brief scope)
- 17 backfilled → owned by Michele Alcantar

**Sample before:**
```
AP-0917      owner_id=NULL    lead=Michele Alcantar
AP-0917-1    owner_id=NULL    lead=Michele Alcantar
AP-1073      owner_id=NULL    lead=Michele Alcantar
```

**Sample after:**
```
AP-0917      owner_id=2e079ba6-558e-4d26-b7ab-8e6720b562e7    lead=Michele Alcantar
AP-0917-1    owner_id=2e079ba6-558e-4d26-b7ab-8e6720b562e7    lead=Michele Alcantar
AP-1073      owner_id=2e079ba6-558e-4d26-b7ab-8e6720b562e7    lead=Michele Alcantar
```

## Out of Scope

- The 2 unowned D&D Team APs (TBD leads, no director fallback applicable)
- AP-0570/0791 (out-of-scope Ops APs — left alone per Jim's ruling)
- Ramey's complete APs (left alone per Jim's ruling)
- Changes to director treatment elsewhere (review gates, RLS, panels)
- Phase 3 callout/digest, Phase 4 master table

## Schema / Config

- No schema changes
- No config changes
- No new tables
- Updated sync_ap.py resolve_owner() + routing pre-pass

## References

- Bug (SAM COS): ca9beaeb "Director-led AP ownership" (Phase 1/2/3 container)
- Phase 2a: "AP end-date event logger gates on parent AP status" (commit cc4e4a4)
- Phase 2b: This change — director-fallback ownership resolution
- Phase 3: TBD — callout/digest (out of scope)
- Phase 4: TBD — master table (out of scope)
