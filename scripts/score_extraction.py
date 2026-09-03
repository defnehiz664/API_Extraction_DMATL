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
  python3 scripts/coverage_report.py \
    --outputs-dir data/outputs \
    --report data/success/coverage_history.xlsx \
    --label schema_v12
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
IDENTITY_COLS = {"record_id", "material_name", "source_doi", "fit_regime", "fit_range"}
# Provenance/citation columns: dropped from scoring. A mis-cited but numerically
# correct value is a traceability defect, not a data defect, and free-text
# locators don't compare cleanly (a value can appear in both a table and a figure).
PROVENANCE_COLS = {"source_figure_or_table"}
SKIP_COLS = IDENTITY_COLS | PROVENANCE_COLS


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


def _lcf_tail(row, lowmap, is_lcf):
    if not is_lcf:
        return ()
    return (_s(_get_ci(row, lowmap, "fit_regime")), _s(_get_ci(row, lowmap, "fit_range")))


def _rid_key(row, lowmap, is_lcf):
    """Primary key: record_id (a stable identity you don't edit during gold
    correction), plus the strain regime for LCF. None if record_id is blank."""
    rid = _get_ci(row, lowmap, "record_id")
    if _blank(rid):
        return None
    return (_s(rid),) + _lcf_tail(row, lowmap, is_lcf)


def _doi_key(row, lowmap, is_lcf):
    """Fallback: source_DOI + material_name. Stable across runs; source_DOI never
    changes. None if either is missing."""
    sd, mat = _get_ci(row, lowmap, "source_doi"), _get_ci(row, lowmap, "material_name")
    if _blank(sd) or _blank(mat):
        return None
    return (_s(sd).rstrip("/"), _s(mat)) + _lcf_tail(row, lowmap, is_lcf)


def _mat_key(row, lowmap, is_lcf):
    """Last-resort fallback: material_name only. Breaks if a material name was
    corrected in the gold, so it runs only after the two keys above."""
    return (_s(_get_ci(row, lowmap, "material_name")) or "?",) + _lcf_tail(row, lowmap, is_lcf)


def _blank_counts():
    return dict(tp=0, tn=0, fab=0, omit=0, wrong=0, fmt=0)


def _rates(c):
    fp = c["fab"] + c["wrong"] + c["fmt"]
    fn = c["omit"] + c["wrong"] + c["fmt"]
    # every audited cell is exactly one of these six categories, so this sum
    # counts each cell once (unlike TP+TN+FP+FN, where wrong/fmt sit in both FP and FN).
    checked = c["tp"] + c["tn"] + c["fab"] + c["omit"] + c["wrong"] + c["fmt"]
    return {
        "TP": c["tp"], "TN": c["tn"], "FP": fp, "FN": fn,
        "omission": c["omit"], "wrong_value": c["wrong"],
        "format_unit": c["fmt"], "fabrication": c["fab"],
        "precision": c["tp"] / (c["tp"] + fp) if (c["tp"] + fp) else 1.0,
        "recall": c["tp"] / (c["tp"] + fn) if (c["tp"] + fn) else 1.0,
        # four-box accuracy: correct fills AND correct blanks over all checked
        # cells. Credits the pipeline for leaving verified-empty cells empty.
        "accuracy": (c["tp"] + c["tn"]) / checked if checked else 1.0,
        "checked": checked,
    }


# ── scoring ───────────────────────────────────────────────────────────────────

def score(gold_path, pred_path):
    """Return {'per_sheet', 'overall', 'instances', 'pred_cells', 'reconcile'}."""
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

        is_lcf = gname.lower() == "lcf"
        glow, plow = _lowmap(gdf), _lowmap(pdf)
        audit = [c for c in gdf.columns if _real_col(c) and str(c).lower() not in SKIP_COLS]

        # index prediction rows by three keys, best-first: record_id, then
        # source_DOI+material, then material only. A pred row is consumed on
        # first match so it is used once.
        pred_list = [r for _, r in pdf.iterrows()]
        idx_rid, idx_doi, idx_mat = defaultdict(list), defaultdict(list), defaultdict(list)
        for i, r in enumerate(pred_list):
            kr = _rid_key(r, plow, is_lcf)
            if kr is not None:
                idx_rid[kr].append(i)
            kd = _doi_key(r, plow, is_lcf)
            if kd is not None:
                idx_doi[kd].append(i)
            idx_mat[_mat_key(r, plow, is_lcf)].append(i)
        consumed = set()

        def _take(index, key):
            if key is None:
                return None
            for i in index.get(key, []):
                if i not in consumed:
                    consumed.add(i)
                    return pred_list[i]
            return None

        c = _blank_counts()
        missing_cols = [col for col in audit if col.lower() not in plow]
        if missing_cols:
            reconcile.setdefault(gname, []).append(f"columns absent from prediction: {missing_cols}")

        missed_rows = []
        for _, grow in gdf.iterrows():
            k = _rid_key(grow, glow, is_lcf) or _mat_key(grow, glow, is_lcf)
            prow = _take(idx_rid, _rid_key(grow, glow, is_lcf))
            if prow is None:
                prow = _take(idx_doi, _doi_key(grow, glow, is_lcf))
            if prow is None:
                prow = _take(idx_mat, _mat_key(grow, glow, is_lcf))
            if prow is None:
                missed_rows.append(k)
                continue
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

        spurious_rows = [_mat_key(pred_list[i], plow, is_lcf)
                         for i in range(len(pred_list)) if i not in consumed]
        rates = _rates(c)
        rates["matched_rows"] = len(consumed)
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


def write_report(path, results, gold, pred, label):
    """Append this run to an Excel log: one detail sheet of error instances,
    plus a cumulative 'summary' sheet gaining one row per run."""
    path = Path(path)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    run_id = label or datetime.now().strftime("%Y%m%d_%H%M%S")
    ov = results["overall"]

    # Two lines per run: the "cells" view (precision, denominator = cells the
    # pipeline filled) and the "full" view (recall, denominator = every gold cell
    # that has a value). Same run_id on both so they read as a pair.
    def _row(scope, num, den, metric):
        return {
            "run_id": run_id, "timestamp": ts, "scope": scope,
            "metric": round(metric, 4), "numerator_TP": num, "denominator": den,
            "TP": ov["TP"], "FP": ov["FP"], "FN": ov["FN"], "TN": ov["TN"],
            "omission": ov["omission"], "wrong_value": ov["wrong_value"],
            "format_unit": ov["format_unit"], "fabrication": ov["fabrication"],
            "gold": str(gold), "pred": str(pred),
        }

    summary_rows = [
        _row("cells (precision)", ov["TP"], ov["TP"] + ov["FP"], ov["precision"]),
        _row("full (recall)",     ov["TP"], ov["TP"] + ov["FN"], ov["recall"]),
        _row("overall (accuracy)", ov["TP"] + ov["TN"], ov["checked"], ov["accuracy"]),
    ]

    inst = results["instances"]
    detail = pd.DataFrame(
        [{"sheet": s, "row_key": " | ".join(map(str, k)), "column": c, "category": cat, "note": note}
         for s, k, c, cat, note in inst]
    ) if inst else pd.DataFrame([{"sheet": "", "row_key": "", "column": "", "category": "no_errors", "note": ""}])

    exists = path.exists()
    existing = pd.ExcelFile(path).sheet_names if exists else []
    if exists and "summary" in existing:
        prior = pd.read_excel(path, sheet_name="summary")
        prior = prior[prior["run_id"] != run_id]                    # replace both lines on re-run
        summ = pd.concat([prior, pd.DataFrame(summary_rows)], ignore_index=True)
    else:
        summ = pd.DataFrame(summary_rows)

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
    ap.add_argument("--report", default="data/success/score_history.xlsx",
                    help="Excel log to append this run to (default data/success/score_history.xlsx)")
    ap.add_argument("--label", default=None, help="run label for the report sheet/row (default: timestamp)")
    ap.add_argument("--no-report", action="store_true", help="do not write to the report log")
    args = ap.parse_args()

    res = score(args.gold, args.pred)
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

    if not args.no_report:
        write_report(args.report, res, args.gold, args.pred, args.label)

    ok = not res["instances"] and not res["reconcile"]
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()