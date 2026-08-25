# Sync the "No report-out at this level" AP flag into ORiON

**Decided:** 2026-08-25
**Repo:** hlc-scripts
**Action item:** 8858295f-c5eb-4b5f-abdd-54df5af8669f

## What this closes

The Ops Action Plan Phase 1.5 (Report-Out view) needed the Smartsheet
"No report-out at this level" flag, which was not synced into ORiON. This
brings that one column in — the column only, not the view itself — so 1.5
can be built next against real data.

## Column identity

OFS Training Action Plan Tracker (sheet `1362792971980676`), column
"No report-out at this level", column ID `5308689965338500`, type
`CHECKBOX`, index 19, not hidden. Populated: 297 of 720 rows checked
(True), 423 blank. Applies to every AP item (Delivery, P&C, Ops per Jim),
so it lands on both synced tables.

## The checkbox-blank-is-false ruling — inverse of the category rule

`SQDCG_MAP`/`map_category()` (see `2026-08-11-sync-ap-category-tier.md`)
established that a blank Smartsheet cell maps to `None` — no invented
default, because blank there means "no data entered yet" on a sparse
picklist field.

This column is a **CHECKBOX**, not a picklist, and that changes what blank
means. Smartsheet reports an unchecked box as a blank/absent cell, not as
an explicit `False` — but for a checkbox, blank IS the answer: the 423
blank rows are genuinely "reports out at this level" (flag off), not "no
data." Mapping blank→`None` here would be wrong in the opposite direction
from the category case — it would make a `no_report_out` column unable to
distinguish "reports out" from "unknown," and would wrongly exclude the 423
rows that should render in a report-out view.

The ruling, implemented in `map_no_report_out()`:

```python
def map_no_report_out(raw) -> bool:
    return bool(raw)
```

checked (`True`) → `True`; blank/absent/explicit `False` → `False`. Never
`None`. Same shape as `map_category`'s one-line function, opposite
blank-handling — the lesson is "ask what blank means for *this* field,"
not "reuse the last field's rule."

## Scope: both tables

`no_report_out boolean NOT NULL DEFAULT false` added to both
`pc_projects` and `action_items` on ORiON (`czdkctjbejnwuopigxta`) via one
migration (`add_no_report_out_flag`). Confirmed via `information_schema`
pre-migration that neither table had a report-out column yet, and that the
existing boolean-column naming convention on these tables is plain
snake_case with no required prefix (`vault_synced`, `escalation_needed`,
`ap_pending_update`, `ap_orphaned`) — `no_report_out` fits directly, no
rename needed. `DEFAULT false` so every existing row is immediately valid
("reports out") until the next sync stamps the real value.

Read via a new module-level column-ID constant (`COL_NO_REPORT_OUT`),
computed once per row (mirrors the existing `COL_SQDCG`/`map_category`
precedent), and wired into all four write paths: the Delivery insert, the
Delivery update-diff, the P&C insert, and the P&C update-diff.

## No guard on the update-diff — deliberate

The update-diff NULL-guards on dates (`due_date`/`start_date`/
`target_end_date`) and on `category` exist because a *computed None*
overwriting a *stored real value* is data loss from an accidental blank
cell (bugs `74ebd314`, `b75a59f6`). `no_report_out` never computes `None`
— `map_no_report_out()` always returns a real boolean — so that clobber
class does not apply here, and no guard was added:

```python
if no_report_out != ex.get('no_report_out'):
    fields['no_report_out'] = no_report_out
```

This intentionally writes both `False→True` and `True→False`. Unlike a
cleared date (which reads as an accident) or a sparse category (which
reads as "no data yet"), a checkbox toggling in Smartsheet is a real,
deliberate user action — checking or unchecking it — and it must propagate
in both directions. A "never write false over true" guard would be wrong
here: it would silently pin the flag on after someone unchecked the box in
Smartsheet, exactly the kind of stale-state bug the guards elsewhere exist
to prevent, not one they'd be preventing here.

## Verified live

Checkbox mapping tested behaviorally against the real function (checked →
`True`, blank/absent → `False`, explicit `False` → `False` — never
`None`). Migration confirmed via `information_schema`: `no_report_out
boolean NOT NULL DEFAULT false` present on both tables. Both update-diff
call sites (Delivery line ~933, P&C line ~1063) confirmed identical and
toggling both directions by executing the real extracted/dedented code
against synthetic stored/computed pairs (mirrors the `2026-08-20`
Delivery-date-guard harness discipline — run the real code, not a retyped
copy). A live (non-dry-run) sync run and a post-run row-count query against
ORiON are the closing evidence for this build; see the session's reported
gate output for the actual run numbers.

## Out of scope

The Phase 1.5 Report-Out view in `orion-pll` — deliberately sequenced
after this column is live and Jim has seen real rows populate, same
validate-on-real-data discipline as the collision rule and the category
sync. Widening the sync to all APs regardless of lead — separate design
track, tracked on its own action item; this column rides whatever the
existing inclusion filter (`ACTIVE_STATUSES` + lead-resolution routing)
already brings in, unchanged.
