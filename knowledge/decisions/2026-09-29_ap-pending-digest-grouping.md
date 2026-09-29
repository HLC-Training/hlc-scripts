# Decision: AP-pending digest grouping by parent AP number

**Action item:** 509ede2d  
**Date:** 2026-09-29  
**Owner:** Jen Wright feedback, implemented by Claude Haiku 4.5  
**Status:** Shipped in commit ce5c104

## Problem

Jen Wright works the AP-pending digest line by line against the Smartsheet AP tracker, which is sorted by parent AP number. The email lists entries in an order that doesn't match the tracker, requiring her to jump around the sheet to apply each update. She requested entries grouped by AP number in numerical order, so both email and tracker read top to bottom in the same order.

## Solution

Group and sort AP-pending digest entries by parent AP number using natural numeric ordering:

- **Grouping key:** Parent AP number (the first numeric segment after "AP-")
- **Sort order:** Numeric, not string (AP-189 < AP-0785, not lexicographic)
- **Within a group:** Parent row first (if present), then children sorted numerically by each hyphen segment
- **Group headers:** HTML (gray header row) and text (header line), showing parent title when available
- **Rows with no AP number:** Sorted last under "No AP number" group

### Implementation

- `parse_ap_key(ap_number)` → `(parent_int, tuple_of_child_ints) | None`
- `sort_rows_by_ap_key(rows)` → rows sorted by parent, children, age
- `group_rows_by_parent_ap(rows)` → grouped dict for rendering
- Applied to main table, orphan section, and master tracker section

### Behavior

Sort is natural numeric per hyphen segment (AP numbers are inconsistently padded); group headers show the AP number as stored so it matches the tracker. Delivery, P&C and Master tracker rows share one table (the Master tracker rows were already merged there before this change) and interleave within a parent group, module labels kept. Order only: which rows appear is unchanged and still governed by the reconciliation, orphan and dirty-mirror decisions.

### Padding preservation

AP numbers carry variable padding ("AP-0785" vs "AP-189"). Group headers extract the AP number string from the first row in each group (preferring the parent row's copy), preserving the original padding.

## Test results

- G1: Parser test — all 8 test cases pass, numeric sort verified
- G3: Text grouping — groups ascend numerically, children ascend, modules interleave
- G4: HTML headers — gray background rows with parent title, proper colspan
- Master tracker: Confirmed merged into main table with grouping applied

## Notes

- Dry-run mode writes previews to `out/` (gitignored)
- No changes to row filtering or reconciliation logic
- .env.local support added for API key storage on TALOS
