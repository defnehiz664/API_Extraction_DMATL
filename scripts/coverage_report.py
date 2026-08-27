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
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUTS_DIR = REPO / "data" / "outputs"

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


def main():
    ap = argparse.ArgumentParser(description="Field fill-rate across extractions.")
    ap.add_argument("--outputs-dir", default=str(DEFAULT_OUTPUTS_DIR))
    ap.add_argument("--min", type=float, default=0.0, help="only show fields with fill-rate >= this (0-1)")
    ap.add_argument("--out", default=None, help="optional CSV path for the full table")
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


if __name__ == "__main__":
    main()