"""
coverage_report.py
==================
Field FILL-RATE across a corpus of extractions. Answers "which fields actually
come back with data, and how often" — the common-core question. Needs NO gold
standard; it only counts populated cells, so run it the moment the batch outputs
land.

It canonicalizes characterization block casing (sem->SEM, tem->TEM, ...) so the
`sem`/`SEM` split that appears when response_schema is off is merged here instead
of double-counted.

Usage:
  python3 scripts/coverage_report.py                      # all *_extraction.json
  python3 scripts/coverage_report.py --min 0.5            # only fields filled in >=50% of records
  python3 scripts/coverage_report.py --out data/coverage.csv
"""

import argparse
import csv
import glob
import json
import math
import re
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUTS_DIR = REPO / "data" / "success"

# characterization blocks the schema spells uppercase; the model sometimes
# emits them lowercase with response_schema off. Merge to the canonical name.
CANON = {"sem": "SEM", "tem": "TEM", "ebsd": "EBSD", "xrd": "XRD"}


def _canon_key(k):
    return CANON.get(str(k).lower(), k)


def _populated(v):
    if v is None:
        return False
    if isinstance(v, float) and math.isnan(v):
        return False
    if isinstance(v, str) and v.strip() == "":
        return False
    if isinstance(v, (list, dict)) and len(v) == 0:
        return False
    return True


def _leaves(obj, prefix=""):
    """Flatten a record into {dotted_path: value}. A non-empty list is a terminal
    leaf (counts as populated); dicts recurse; block-key casing is canonicalized."""
    out = {}
    for k, v in obj.items():
        p = f"{prefix}{_canon_key(k)}"
        if isinstance(v, dict):
            if v:
                out.update(_leaves(v, p + "."))
            else:
                out[p] = v
        else:
            out[p] = v
    return out


def load_records(outputs_dir):
    records = []
    for f in sorted(glob.glob(str(Path(outputs_dir) / "*_extraction.json"))):
        data = json.loads(Path(f).read_text(encoding="utf-8"))
        recs = data.get("records")
        if isinstance(recs, list):
            for r in recs:
                records.append((Path(f).name, r))
    return records


def _san_col(name):
    return re.sub(r"[:\\/?*\[\]]", "-", str(name)).strip()[:200] or "run"


def append_report(path, run_id, rates, n_records, n_files):
    """Append this run to a coverage workbook, without regenerating anything.

    Two sheets:
      summary  — one row per run (headline numbers), newest kept in order.
      by_field — one column per run_id holding each field's fill-rate, so a
                 field's coverage across schema versions reads left to right.
    Re-running the same run_id replaces that run in both sheets."""
    import pandas as pd

    path = Path(path)
    run_id = _san_col(run_id)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rates = {str(f): float(r) for f, r in rates.items()}

    summary_row = {
        "run_id": run_id, "timestamp": ts, "records": n_records, "files": n_files,
        "distinct_fields": len(rates),
        "fields_100pct": sum(1 for r in rates.values() if r >= 0.999),
        "fields_ge_50pct": sum(1 for r in rates.values() if r >= 0.5),
        "mean_fill_rate": round(sum(rates.values()) / len(rates), 4) if rates else 0.0,
    }

    exists = path.exists()
    sheets = pd.ExcelFile(path).sheet_names if exists else []

    if exists and "summary" in sheets:
        summ = pd.read_excel(path, sheet_name="summary")
        summ = summ[summ["run_id"] != run_id]                       # replace on re-run
        summ = pd.concat([summ, pd.DataFrame([summary_row])], ignore_index=True)
    else:
        summ = pd.DataFrame([summary_row])

    col = pd.Series(rates, name=run_id)
    if exists and "by_field" in sheets:
        wide = pd.read_excel(path, sheet_name="by_field").set_index("field")
        wide = wide.drop(columns=[run_id], errors="ignore")         # replace on re-run
        wide = wide.join(col, how="outer")
    else:
        wide = col.to_frame()
    wide.index.name = "field"
    wide = wide.sort_values(by=run_id, ascending=False)             # order by newest run

    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as w:              # full rewrite of both sheets
        summ.to_excel(w, sheet_name="summary", index=False)
        wide.reset_index().to_excel(w, sheet_name="by_field", index=False)
    print(f"\nAppended run '{run_id}' to {path}  "
          f"(summary + by_field; {len(wide.columns)} run column(s) tracked)")


def main():
    ap = argparse.ArgumentParser(description="Field fill-rate across extractions.")
    ap.add_argument("--outputs-dir", default=str(DEFAULT_OUTPUTS_DIR))
    ap.add_argument("--min", type=float, default=0.0, help="only show fields with fill-rate >= this (0-1)")
    ap.add_argument("--out", default=None, help="optional CSV path for the full table (one-off snapshot)")
    ap.add_argument("--report", default="data/coverage_history.xlsx",
                    help="Excel workbook to append this run to (default data/coverage_history.xlsx)")
    ap.add_argument("--label", default=None,
                    help="run/schema-version label for the report column (default: timestamp)")
    ap.add_argument("--no-report", action="store_true", help="do not append to the report workbook")
    args = ap.parse_args()

    records = load_records(args.outputs_dir)
    n = len(records)
    if n == 0:
        print("No records found.")
        return

    counts = {}
    for _, rec in records:
        for path, val in _leaves(rec).items():
            if path.startswith("_"):
                continue
            counts.setdefault(path, 0)
            if _populated(val):
                counts[path] += 1

    rows = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    print(f"Records: {n}   (files: {len({f for f, _ in records})})\n")
    print(f"{'fill%':>6}  {'count':>5}  field")
    shown = 0
    for path, c in rows:
        rate = c / n
        if rate < args.min:
            continue
        print(f"{rate*100:6.1f}  {c:5d}  {path}")
        shown += 1
    print(f"\n{shown} field(s) shown; {len(rows)} total distinct fields seen.")

    if args.out:
        with open(args.out, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["field", "populated_count", "total_records", "fill_rate"])
            for path, c in rows:
                w.writerow([path, c, n, round(c / n, 4)])
        print(f"wrote {args.out}")

    if not args.no_report:
        run_id = args.label or datetime.now().strftime("%Y%m%d_%H%M%S")
        rates = {path: c / n for path, c in rows}
        append_report(args.report, run_id, rates, n, len({f for f, _ in records}))


if __name__ == "__main__":
    main()