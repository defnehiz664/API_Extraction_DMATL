"""
gold_port.py — carry your hand-verified values from an OLD gold_standard.xlsx into
a NEW gold (duplicated from the latest features_output.xlsx), so you don't re-check
what you already checked.

Two phases, by design:
  PREVIEW (default)  — computes every cell it WOULD change, writes a plan workbook,
                       and touches nothing. This is the "list the changes" step.
  APPLY  (--apply)   — performs exactly the changes in that plan and writes the
                       ported gold to --out. This is your "yes, go on".

Rows are matched with the SAME key ladder score_extraction uses (record_id, then
source_DOI+material, then material; LCF also keys on fit_regime+fit_range) and the
SAME numeric tolerance, so a cell that already agrees within 0.1% is not counted as
a change, and rows are placed the way they will be scored.

A verified value includes an old-gold cell holding 'null' (verified-empty): that is
checked truth too ("this must be empty"), so it ports.

IMPORTANT caveat this tool cannot fix for you: where a paper's identity or row
granularity changed between the two golds (the LCF temperature splits, the dib
hardness consolidation, the dropped creep-fatigue rows), one old row may not map to
the new rows. Those land in the plan's 'unmatched_old' sheet — verified data the
port could NOT place. You reconcile those by hand; nothing is silently lost.

Usage:
  python gold_port.py --old-gold data/gold_old.xlsx --new-gold data/gold_new.xlsx
  # review gold_port_plan.xlsx, then:
  python gold_port.py --old-gold data/gold_old.xlsx --new-gold data/gold_new.xlsx --apply
"""
import argparse
import re
from pathlib import Path

import pandas as pd

# Reuse the scorer's value semantics (blank test, tolerant compare) and its
# record_id key so a ported cell is judged exactly as it will later be scored.
# The port deliberately does NOT reuse the scorer's LOOSE fallbacks (material-only,
# DOI+material): those exist so re-ordered runs still match, but for gold-building
# a loose match writes a WRONG verified value. The port matches strictly instead
# (see _fb_key) and leaves anything ambiguous for the human.
from score_extraction import (
    _blank, _s, compare, _lowmap, _get_ci, _real_col,
    _rid_key, _lcf_tail, SKIP_COLS,
)

# Columns the port never overwrites: the scorer's identity/provenance set PLUS
# material_condition, which is a MATCHING KEY here (reduced to its last step), not a
# value to verify. The new gold's final-state condition is authoritative; porting
# the old verbose route over it would corrupt the key.
PORT_SKIP = SKIP_COLS | {"material_condition"}


# material_condition often records the whole PROCESSING HISTORY as a sequence of
# steps; the current identity keys on the FINAL state. Split on explicit step
# separators and keep the last step, so an old 'solution treated + aged' lines up
# with a new 'aged'. NOTE: only symbolic/keyword step joiners are split — a space
# is NOT one, so 'peak aged' and 'as cast' stay whole. If two conditions in one
# paper reduce to the same last step, the fallback's uniqueness guard still refuses
# the match (nothing is mis-placed); this only ADDS safe matches.
_STEP_SEP = re.compile(r"\s*(?:\+|,|;|/|->|→|»|\bthen\b|\bfollowed by\b|\band\b)\s*", re.I)


def _last_step(text):
    """Last processing step of a condition/route string (or the whole string if it
    has no step separators)."""
    parts = [p for p in _STEP_SEP.split(str(text or "")) if p.strip()]
    return parts[-1] if parts else str(text or "")


def _fb_key(row, lowmap, is_lcf):
    """Strict fallback identity: DOI + material + LAST processing step of the
    condition (+ LCF regime). Reducing to the last step matches an old full route
    to the new final-state condition; keeping condition (not dropping it) means
    'solution_treated' and 'aged' still never collide. Used only when UNIQUE on
    both sides, so it can't mis-place a row."""
    sd = _get_ci(row, lowmap, "source_doi")
    mat = _get_ci(row, lowmap, "material_name")
    if _blank(sd) or _blank(mat):
        return None
    cond = _get_ci(row, lowmap, "material_condition")
    cond_key = "" if _blank(cond) else _s(_last_step(cond))
    return (_s(sd).rstrip("/"), _s(mat), cond_key) + _lcf_tail(row, lowmap, is_lcf)


def _read_all(path):
    """Every sheet as a dict name->DataFrame, read the way the scorer reads (object
    dtype, blanks kept as-is), so values round-trip without pandas re-typing them."""
    xl = pd.ExcelFile(path)
    out = {}
    for name in xl.sheet_names:
        df = pd.read_excel(xl, sheet_name=name, dtype=object, keep_default_na=False)
        out[name] = df.where(pd.notnull(df), None)
    return out


def build_plan(old_book, new_book):
    """Return (changes, unmatched_old, unfilled_new). `changes` carry the exact new
    sheet+row+column to write, so apply is unambiguous.

    Matching is STRICT: a new row takes an old row only by exact record_id, or by a
    DOI+material+condition key that is unique on BOTH sides. Ambiguous cases (the
    granularity that changed: temperature splits, hardness consolidation) are never
    guessed — the old row goes to unmatched_old, the new row to unfilled_new."""
    from collections import defaultdict, Counter
    changes, unmatched_old, unfilled_new = [], [], []
    new_lower = {n.lower(): n for n in new_book}

    for oname, odf in old_book.items():
        if oname.lower() not in new_lower:
            for _, orow in odf.iterrows():
                unmatched_old.append({"sheet": oname, "reason": "sheet not in new gold",
                                      "record_id": _get_ci(orow, _lowmap(odf), "record_id"),
                                      "material_name": _get_ci(orow, _lowmap(odf), "material_name")})
            continue
        nname = new_lower[oname.lower()]
        ndf = new_book[nname]
        is_lcf = oname.lower() == "lcf"
        olow, nlow = _lowmap(odf), _lowmap(ndf)
        orows = [r for _, r in odf.iterrows()]

        rid_old, fb_old = defaultdict(list), defaultdict(list)
        for i, r in enumerate(orows):
            kr = _rid_key(r, olow, is_lcf)
            if kr is not None:
                rid_old[kr].append(i)
            kf = _fb_key(r, olow, is_lcf)
            if kf is not None:
                fb_old[kf].append(i)
        fb_new_count = Counter(k for k in (_fb_key(r, nlow, is_lcf) for _, r in ndf.iterrows()) if k is not None)
        consumed = set()

        def _match(nrow):
            kr = _rid_key(nrow, nlow, is_lcf)
            if kr is not None and len(rid_old.get(kr, [])) == 1 and rid_old[kr][0] not in consumed:
                i = rid_old[kr][0]; consumed.add(i); return i, "record_id"
            kf = _fb_key(nrow, nlow, is_lcf)
            if kf is not None and fb_new_count.get(kf, 0) == 1 and len(fb_old.get(kf, [])) == 1 \
                    and fb_old[kf][0] not in consumed:
                i = fb_old[kf][0]; consumed.add(i); return i, "doi+material+condition"
            return None, None

        portable = [c for c in ndf.columns
                    if _real_col(c) and str(c).lower() not in PORT_SKIP and str(c).lower() in olow]

        for nidx, nrow in ndf.iterrows():
            oi, how = _match(nrow)
            if oi is None:
                unfilled_new.append({"sheet": nname,
                                     "record_id": _get_ci(nrow, nlow, "record_id"),
                                     "material_name": _get_ci(nrow, nlow, "material_name")})
                continue
            orow = orows[oi]
            for col in portable:
                ov = _get_ci(orow, olow, col)
                if _blank(ov):
                    continue
                nv = nrow[col]
                if compare(ov, nv)[0]:
                    continue
                changes.append({
                    "sheet": nname, "_nidx": nidx, "_ncol": col,
                    "record_id": _get_ci(nrow, nlow, "record_id"),
                    "material_name": _get_ci(nrow, nlow, "material_name"),
                    "column": col,
                    "current_new_value": "" if _blank(nv) else nv,
                    "incoming_old_value": ov,
                    "kind": "fill" if _blank(nv) else "overwrite",
                    "matched_by": how,
                })

        for i, orow in enumerate(orows):
            if i in consumed:
                continue
            verified = [c for c in portable if not _blank(_get_ci(orow, olow, c))]
            if verified:
                unmatched_old.append({
                    "sheet": nname,
                    "record_id": _get_ci(orow, olow, "record_id"),
                    "material_name": _get_ci(orow, olow, "material_name"),
                    "material_condition": _get_ci(orow, olow, "material_condition"),
                    "reason": "no unambiguous match in new gold",
                    "verified_columns": ", ".join(verified),
                })
    return changes, unmatched_old, unfilled_new


def write_plan(path, changes, unmatched_old, unfilled_new):
    cols = ["sheet", "record_id", "material_name", "column",
            "current_new_value", "incoming_old_value", "kind", "matched_by"]
    ch = pd.DataFrame([{k: c[k] for k in cols} for c in changes]) if changes \
        else pd.DataFrame([{k: "" for k in cols}])
    um = pd.DataFrame(unmatched_old) if unmatched_old else pd.DataFrame([{"sheet": "", "note": "none"}])
    uf = pd.DataFrame(unfilled_new) if unfilled_new else pd.DataFrame([{"sheet": "", "note": "none"}])
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as w:
        ch.to_excel(w, sheet_name="changes", index=False)
        um.to_excel(w, sheet_name="unmatched_old", index=False)
        uf.to_excel(w, sheet_name="unfilled_new", index=False)


def apply_changes(new_book, changes):
    for c in changes:
        new_book[c["sheet"]].at[c["_nidx"], c["_ncol"]] = c["incoming_old_value"]
    return new_book


def write_book(path, book):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as w:
        for name, df in book.items():
            df.to_excel(w, sheet_name=str(name)[:31], index=False)


def main():
    ap = argparse.ArgumentParser(description="Port verified values from an old gold into a new one.")
    ap.add_argument("--old-gold", required=True, help="your hand-filled previous gold_standard.xlsx")
    ap.add_argument("--new-gold", required=True, help="new gold, duplicated from the latest features_output.xlsx")
    ap.add_argument("--out", default=None, help="ported gold output (apply mode; default <new-gold>_ported.xlsx)")
    ap.add_argument("--plan", default="gold_port_plan.xlsx", help="where to write the change plan")
    ap.add_argument("--apply", action="store_true", help="perform the changes (default is preview only)")
    args = ap.parse_args()

    old_book = _read_all(args.old_gold)
    new_book = _read_all(args.new_gold)
    changes, unmatched_old, unfilled_new = build_plan(old_book, new_book)

    fills = sum(1 for c in changes if c["kind"] == "fill")
    overs = sum(1 for c in changes if c["kind"] == "overwrite")
    write_plan(args.plan, changes, unmatched_old, unfilled_new)

    print(f"proposed changes: {len(changes)}  ({fills} fill, {overs} overwrite)")
    print(f"old rows with verified data that could NOT be placed: {len(unmatched_old)}")
    print(f"new rows with no old match (need fresh verification):  {len(unfilled_new)}")
    print(f"plan written: {args.plan}")
    for c in changes[:20]:
        print(f"  [{c['kind']:9}] {c['sheet']} | {c['record_id']} | {c['column']}: "
              f"{c['current_new_value']!r} -> {c['incoming_old_value']!r}")
    if len(changes) > 20:
        print(f"  ... and {len(changes) - 20} more (see {args.plan})")

    if not args.apply:
        print("\nPREVIEW ONLY — nothing was written to the gold. Review the plan, then")
        print("re-run the same command with --apply to commit these changes.")
        return

    out = args.out or str(Path(args.new_gold).with_name(Path(args.new_gold).stem + "_ported.xlsx"))
    apply_changes(new_book, changes)
    write_book(out, new_book)
    print(f"\nAPPLIED {len(changes)} change(s). Ported gold written: {out}")
    print(f"(your inputs were left untouched; unmatched_old in {args.plan} still needs your hand.)")


if __name__ == "__main__":
    main()