"""
score_extraction.py
===================
Confusion-matrix scoring of a pipeline features_output.xlsx against a
hand-checked gold_standard.xlsx, sheet by sheet (Records, LCF, Tensile,
Hardness). Both files share the pipeline's structure, so this is a
like-for-like workbook diff.

Cell outcomes:
  TP  gold has a value, prediction matches
  TN  gold cell is 'null' (verified empty) and prediction is empty
  FP  gold cell is 'null' but prediction filled it (fabrication / conversion)
  FN  gold has a value, prediction left it empty (omission)
  wrong_value / format_unit  gold has a value, prediction filled a DIFFERENT one
       (format_unit = off by a clean x10/x100/x1000: a percent-vs-dimensionless slip)

Row key: paper + material_name, where 'paper' is record_id with its trailing
-index stripped (so re-ordered runs still match). LCF rows also key on
fit_regime + fit_range. Gold cell 'null'/'N/A' = verified empty.

Usage:
  python3 scripts/score_extraction.py --gold data/gold_standard.xlsx --pred data/features_output.xlsx
"""

import argparse
import math
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd

RTOL, ATOL = 1e-3, 1e-6
_BOOL = {"true": True, "false": False, "yes": True, "no": False, "y": True, "n": False, "1": True, "0": False}
_VERIFIED_EMPTY = {"null", "<null>", "∅", "n/a", "na", "verified_empty", "verified-empty"}
_SCALE = (10.0, 100.0, 1000.0, 0.1, 0.01, 0.001)
IDENTITY_COLS = {"record_id", "material_name", "source_doi"}

# Free-text columns excluded from scoring by default: the gold was built by
# pasting snippets from the paper, so wording differs from the model's summary
# without either being wrong. Override with --ignore.
IGNORE_COLS = {"notes", "source_figure_or_table"}

# Columns that tell apart several rows of the same material within one sheet.
# Hand-corrected data cells, so they only refine pairing inside a group; rows
# they cannot pair fall back to order of appearance.
DISCRIMINATORS = {"lcf": ("fit_range", "fit_regime"), "lcf_legend": ("symbol",)}

DEFAULT_REPORT = "data/success quantification/score_history.xlsx"


def _real_col(c) -> bool:
    """A genuine header, not a phantom Excel column (blank header -> pandas
    names it 'Unnamed: N') or an internal underscore column."""
    s = str(c).strip()
    return bool(s) and s.lower() != "nan" and not s.startswith("Unnamed:") and not s.startswith("_")


# ── value helpers ─────────────────────────────────────────────────────────────

def _blank(v):
    if v is None:
        return True
    if isinstance(v, float) and math.isnan(v):
        return True
    return isinstance(v, str) and v.strip() == ""


def _vempty(v):
    return isinstance(v, str) and v.strip().casefold() in _VERIFIED_EMPTY




def _f(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return None
    return None


def _s(v):
    return re.sub(r"\s+", " ", str(v).strip()).casefold()


def _scaling(g, p):
    gf, pf = _f(g), _f(p)
    if gf is None or pf is None or gf == 0 or pf == 0:
        return False
    r = gf / pf
    return any(math.isclose(r, f, rel_tol=0.02) for f in _SCALE)


def compare(g, p):
    if _blank(p):
        return False, "prediction empty"
    gf, pf = _f(g), _f(p)
    if gf is not None and pf is not None:
        if math.isclose(gf, pf, rel_tol=RTOL, abs_tol=ATOL):
            return True, ""
        return False, f"numeric {gf} vs {pf}"
    gb, pb = _BOOL.get(_s(g)), _BOOL.get(_s(p))
    if gb is not None and pb is not None:
        return (gb == pb), ("" if gb == pb else f"bool {gb} vs {pb}")
    if _s(g) == _s(p):
        return True, ""
    return False, f"text {str(g)!r} vs {str(p)!r}"


def _canon_val(v):
    """A normalized form of a predicted value, for run-to-run flip detection."""
    f = _f(v)
    if f is not None:
        return round(f, 6)
    return None if _blank(v) else _s(v)


# ── sheet / row access ────────────────────────────────────────────────────────

def _lowmap(df):
    return {str(c).lower(): c for c in df.columns}


def _get_ci(row, lowmap, name):
    col = lowmap.get(name.lower())
    return row.get(col) if col is not None else None


def _paper(row, lowmap):
    """Run-stable paper identifier. New ids are <doi>__<material>[__<condition>];
    legacy ids are <doi>-<index>. Both reduce to the doi slug, so a gold built
    before make_record_id still matches a fresh run at the strict tier."""
    rid = _get_ci(row, lowmap, "record_id")
    if not _blank(rid):
        s = _s(rid)
        return s.split("__", 1)[0] if "__" in s else re.sub(r"-\d+$", "", s)
    sd = _get_ci(row, lowmap, "source_doi")
    if not _blank(sd):
        return _s(sd).rstrip("/")
    return ""


def _norm(v):
    """Key-normalized cell. Blank of any kind and the verified-empty marker both
    collapse to '': as a key they assert the same thing. The distinction still
    matters during auditing, where _vempty is what separates TN from FP."""
    return "" if _blank(v) or _vempty(v) else _s(v)


def _group_key(row, lowmap):
    return (_paper(row, lowmap), _norm(_get_ci(row, lowmap, "material_name")))


def _disc(row, lowmap, cols):
    return tuple(_norm(_get_ci(row, lowmap, c)) for c in cols)


def _row_label(row, lowmap, cols):
    """Row identifier for reports and flip-detection keys."""
    return _group_key(row, lowmap) + _disc(row, lowmap, cols)


def _pair_rows(grows, prows, glow, plow, disc_cols):
    """Pair gold rows to prediction rows. Group on paper + material_name; inside
    a group pair on the discriminator where it is populated on both sides, then
    pair whatever is left in order of appearance. A group that finds no
    prediction bucket retries on material_name alone, covering a changed or
    absent paper id. Returns (pairs, missed_gold_idx, spurious_pred_idx)."""
    full, mat_only = defaultdict(list), defaultdict(list)
    for i, r in enumerate(prows):
        gk = _group_key(r, plow)
        full[gk].append(i)
        mat_only[gk[1]].append(i)

    gbuckets = defaultdict(list)
    for j, r in enumerate(grows):
        gbuckets[_group_key(r, glow)].append(j)

    pairs, missed, used = [], [], set()
    for gk, gidx in gbuckets.items():
        pidx = [i for i in full.get(gk, []) if i not in used]
        if not pidx:
            pidx = [i for i in mat_only.get(gk[1], []) if i not in used]

        if disc_cols:
            by_disc = defaultdict(list)
            for i in pidx:
                by_disc[_disc(prows[i], plow, disc_cols)].append(i)
            unpaired = []
            for j in gidx:
                d = _disc(grows[j], glow, disc_cols)
                cand = by_disc.get(d) if any(d) else None   # all-blank identifies nothing
                if cand:
                    i = cand.pop(0)
                    used.add(i)
                    pairs.append((j, i))
                else:
                    unpaired.append(j)
            gidx = unpaired
            pidx = [i for i in pidx if i not in used]

        for j, i in zip(gidx, pidx):
            used.add(i)
            pairs.append((j, i))
        missed += gidx[len(pidx):]

    return pairs, missed, [i for i in range(len(prows)) if i not in used]


def _blank_counts():
    return dict(tp=0, tn=0, fab=0, omit=0, wrong=0, fmt=0)


def _rates(c):
    fp = c["fab"] + c["wrong"] + c["fmt"]
    fn = c["omit"] + c["wrong"] + c["fmt"]
    gold_cells = c["tp"] + c["omit"] + c["wrong"] + c["fmt"]
    return {
        "TP": c["tp"], "TN": c["tn"], "FP": fp, "FN": fn,
        "omission": c["omit"], "wrong_value": c["wrong"],
        "format_unit": c["fmt"], "fabrication": c["fab"],
        "precision": c["tp"] / (c["tp"] + fp) if (c["tp"] + fp) else 1.0,
        "recall": c["tp"] / (c["tp"] + fn) if (c["tp"] + fn) else 1.0,
        "accuracy": (c["tp"] + c["tn"]) / (c["tp"] + c["tn"] + fp + fn) if (c["tp"] + c["tn"] + fp + fn) else 1.0,
    }


# ── scoring ───────────────────────────────────────────────────────────────────

def score(gold_path, pred_path, ignore_cols=IGNORE_COLS, count_missed=True):
    """Return {'per_sheet', 'overall', 'instances', 'pred_cells', 'reconcile', 'row_match'}."""
    ignore_cols = {str(c).lower() for c in ignore_cols}

    gxl, pxl = pd.ExcelFile(gold_path), pd.ExcelFile(pred_path)
    pred_sheets = {n.lower(): n for n in pxl.sheet_names}

    per_sheet, instances, pred_cells, reconcile, row_match = {}, [], {}, {}, {}
    overall = _blank_counts()

    for gname in gxl.sheet_names:
        if gname.lower() not in pred_sheets:
            reconcile[gname] = "sheet missing from prediction"
            continue
        gdf = pd.read_excel(gxl, sheet_name=gname, dtype=object, keep_default_na=False)
        pdf = pd.read_excel(pxl, sheet_name=pred_sheets[gname.lower()], dtype=object, keep_default_na=False)
        gdf = gdf.where(pd.notnull(gdf), None)
        pdf = pdf.where(pd.notnull(pdf), None)

        disc_cols = DISCRIMINATORS.get(gname.lower(), ())
        glow, plow = _lowmap(gdf), _lowmap(pdf)
        audit = [c for c in gdf.columns if _real_col(c)
                 and str(c).lower() not in IDENTITY_COLS
                 and str(c).lower() in plow # absent from prediction: reported, not scored
                 and str(c).lower() not in ignore_cols]

        grows = [r for _, r in gdf.iterrows()]
        prows = [r for _, r in pdf.iterrows()]
        pairs, missed_idx, spurious_idx = _pair_rows(grows, prows, glow, plow, disc_cols)

        c = _blank_counts()
        missing_cols = [col for col in audit if col.lower() not in plow]
        if missing_cols:
            reconcile.setdefault(gname, []).append(f"columns absent from prediction: {missing_cols}")

        for j, i in pairs:
            grow, prow = grows[j], prows[i]
            k = _row_label(grow, glow, disc_cols)
            for col in audit:
                gv = grow[col]
                if _blank(gv):
                    continue
                pv = _get_ci(prow, plow, col)
                pred_cells[(gname, k, col.lower())] = _canon_val(pv)

                if _vempty(gv):
                    if _blank(pv):
                        c["tn"] += 1
                    else:
                        c["fab"] += 1
                        instances.append((gname, k, col, "fabrication", f"gold null but predicted {pv!r}"))
                    continue

                ok, note = compare(gv, pv)
                if ok:
                    c["tp"] += 1
                elif _blank(pv):
                    c["omit"] += 1
                    instances.append((gname, k, col, "omission", note))
                elif _scaling(gv, pv):
                    c["fmt"] += 1
                    instances.append((gname, k, col, "format_unit", note))
                else:
                    c["wrong"] += 1
                    instances.append((gname, k, col, "wrong_value", note))

        # An unmatched gold row is a record the prediction never produced, so every
        # value it holds is an omission. Verified-empty cells are skipped: with
        # nothing predicted, "should be empty" is trivially satisfied.
        if count_missed:
            for j in missed_idx:
                grow = grows[j]
                k = _row_label(grow, glow, disc_cols)
                for col in audit:
                    gv = grow[col]
                    if _blank(gv) or _vempty(gv):
                        continue
                    c["omit"] += 1
                    instances.append((gname, k, col, "omission", "row absent from prediction"))

        missed_rows = [_row_label(grows[j], glow, disc_cols) for j in missed_idx]
        spurious_rows = [_row_label(prows[i], plow, disc_cols) for i in spurious_idx]
        rates = _rates(c)
        rates["matched_rows"] = len(pairs)
        rates["missed_rows"] = len(missed_rows)
        rates["spurious_rows"] = len(spurious_rows)
        per_sheet[gname] = rates
        row_match[gname] = {"missed": missed_rows, "spurious": spurious_rows}
        for kk in overall:
            overall[kk] += c[kk]        
            

    return {"per_sheet": per_sheet, "overall": _rates(overall),
            "instances": instances, "pred_cells": pred_cells,
            "reconcile": reconcile, "row_match": row_match}


# ── printing ──────────────────────────────────────────────────────────────────

def _san_sheet(name):
    s = re.sub(r"[:\\/?*\[\]]", "-", str(name)).strip()
    return (s or "run")[:31]


def _unique_sheet(name, existing):
    base = _san_sheet(name)
    if base not in existing:
        return base
    i = 2
    while f"{base[:27]}_{i}" in existing:
        i += 1
    return f"{base[:27]}_{i}"


def write_report(path, results, gold, pred, label, schema_version=None, count_missed=True):
    """Append this run to an Excel log: one detail sheet of error instances,
    plus a cumulative 'summary' sheet gaining one row per run."""
    path = Path(path)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    run_id = label or datetime.now().strftime("%Y%m%d_%H%M%S")
    ov = results["overall"]

    summary_row = {
        "run_id": run_id, "timestamp": ts, 
        "schema_version": schema_version or "",
        "count_missed": count_missed,
        "gold": str(gold), "pred": str(pred),
        "TP": ov["TP"], "FP": ov["FP"], "FN": ov["FN"], "TN": ov["TN"],
        "precision": round(ov["precision"], 4), "recall": round(ov["recall"], 4),
        "accuracy": round(ov["accuracy"], 4),
        "omission": ov["omission"], "wrong_value": ov["wrong_value"],
        "format_unit": ov["format_unit"], "fabrication": ov["fabrication"],
    }

    inst = results["instances"]
    detail = pd.DataFrame(
        [{"sheet": s, "row_key": " | ".join(map(str, k)), "column": c, "category": cat, "note": note}
         for s, k, c, cat, note in inst]
    ) if inst else pd.DataFrame([{"sheet": "", "row_key": "", "column": "", "category": "no_errors", "note": ""}])

    exists = path.exists()
    existing = pd.ExcelFile(path).sheet_names if exists else []
    if exists and "summary" in existing:
        summ = pd.concat([pd.read_excel(path, sheet_name="summary"),
                          pd.DataFrame([summary_row])], ignore_index=True)
    else:
        summ = pd.DataFrame([summary_row])

    run_sheet = _unique_sheet(f"run_{run_id}", existing)
    path.parent.mkdir(parents=True, exist_ok=True)
    if exists:
        writer = pd.ExcelWriter(path, engine="openpyxl", mode="a", if_sheet_exists="replace")
    else:
        writer = pd.ExcelWriter(path, engine="openpyxl", mode="w")
    with writer as w:
        summ.to_excel(w, sheet_name="summary", index=False)
        detail.to_excel(w, sheet_name=run_sheet, index=False)
    print(f"\nAppended run '{run_id}' to {path}  (detail sheet: {run_sheet})")


def _print_block(name, r):
    print(f"\n── {name} ──")
    print(f"  TP {r['TP']}   FP {r['FP']}   FN {r['FN']}   TN {r['TN']}")
    print(f"  precision {r['precision']:.1%}   recall {r['recall']:.1%}   accuracy {r['accuracy']:.1%}")
    print(f"  omission {r['omission']}   wrong_value {r['wrong_value']}   "
          f"format_unit {r['format_unit']}   fabrication {r['fabrication']}")


def main():
    ap = argparse.ArgumentParser(description="Confusion-matrix scoring, gold vs pipeline output.")
    ap.add_argument("--gold", required=True)
    ap.add_argument("--pred", required=True)
    ap.add_argument("--report", default=DEFAULT_REPORT,
                    help=f"Excel log to append this run to (default {DEFAULT_REPORT})")
    ap.add_argument("--label", default=None, help="run label for the report sheet/row (default: timestamp)")
    ap.add_argument("--no-report", action="store_true", help="do not write to the report log")
    ap.add_argument("--ignore", default=None,
                    help=f"comma-separated columns excluded from scoring "
                         f"(default: {','.join(sorted(IGNORE_COLS))})")
    ap.add_argument("--no-count-missed-rows", action="store_true",
                    help="do not charge a gold row absent from the prediction as omissions")
    ap.add_argument("--schema-version", default=None,
                    help="schema version this run scored, e.g. v12 (recorded in the history)")

    args = ap.parse_args()
    ignore = {x.strip() for x in args.ignore.split(",")} if args.ignore else IGNORE_COLS
    res = score(args.gold, args.pred, ignore_cols=ignore,
        count_missed=not args.no_count_missed_rows)
    
    print(f"Gold: {args.gold}\nPred: {args.pred}")
    for sheet, r in res["per_sheet"].items():
        _print_block(sheet, r)
        print(f"  rows: matched {r['matched_rows']}, missed {r['missed_rows']}, spurious {r['spurious_rows']}")
    _print_block("OVERALL", res["overall"])

    rm = res.get("row_match", {})
    if any(v["missed"] or v["spurious"] for v in rm.values()):
        print("\n── UNMATCHED ROWS (not scored per-cell) ──")
        for sheet, v in rm.items():
            for k in v["missed"]:
                print(f"  MISSED   {sheet}: {k}  (gold row with no prediction match)")
            for k in v["spurious"]:
                print(f"  SPURIOUS {sheet}: {k}  (prediction row with no gold match)")

    if res["instances"]:
        print("\n── ERROR INSTANCES ──")
        for sheet, k, col, cat, note in res["instances"]:
            print(f"  [{cat}] {sheet} {k} :: {col}: {note}")
    if res["reconcile"]:
        print("\n── RECONCILIATION ──")
        for sheet, msg in res["reconcile"].items():
            print(f"  {sheet}: {msg}")


    ignore = {x.strip() for x in args.ignore.split(",")} if args.ignore else IGNORE_COLS
    res = score(args.gold, args.pred, ignore_cols=ignore,
        count_missed=not args.no_count_missed_rows)

    if not args.no_report:
        write_report(args.report, res, args.gold, args.pred, args.label,
            schema_version=args.schema_version,
            count_missed=not args.no_count_missed_rows)

    ok = not res["instances"] and not res["reconcile"]
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()