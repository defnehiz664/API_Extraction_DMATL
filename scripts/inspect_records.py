"""
inspect_records.py — read-only structural x-ray of the extraction JSONs.
Shows, per paper, what the model ACTUALLY emitted: each record's material,
specimen_id, condition, and which blocks it carries — so fragmentation,
parent-aggregate rows, and inconsistent conditions are visible at a glance.
Makes NO changes to anything.

Usage:
  python scripts/inspect_records.py --paper dib      # just the dataset paper
  python scripts/inspect_records.py                  # every paper
"""
import argparse, glob, json, re
from collections import defaultdict
from pathlib import Path


def _canon(s):
    s = re.sub(r"\([^)]*\)", " ", str(s or "").lower())
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", "", s) or "?"


def _blocks(rec):
    return [k for k, v in rec.items() if isinstance(v, dict) and v]


def _scalars(rec):
    return sum(1 for k, v in rec.items()
               if not isinstance(v, (dict, list)) and v not in (None, ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outputs-dir", default="data/outputs")
    ap.add_argument("--paper", default=None, help="substring filter on filename")
    args = ap.parse_args()

    files = sorted(glob.glob(str(Path(args.outputs_dir) / "*_extraction.json")))
    if args.paper:
        files = [f for f in files if args.paper.lower() in Path(f).name.lower()]

    for f in files:
        d = json.load(open(f, encoding="utf-8"))
        recs = d.get("records")
        if isinstance(recs, dict):
            if "error" in recs or "finish_reason" in recs:
                print(f"\n{Path(f).name}: FAILED ({recs.get('finish_reason')})")
                continue
            recs = list(recs.values())
        if not isinstance(recs, list):
            print(f"\n{Path(f).name}: records is {type(recs).__name__}")
            continue

        print(f"\n=== {Path(f).name}  ({len(recs)} records) ===")
        by_mat = defaultdict(list)
        for i, r in enumerate(recs):
            if isinstance(r, dict):
                by_mat[_canon(r.get("material_name"))].append((i, r))
        for mat, group in by_mat.items():
            has_spec = any(g[1].get("specimen_id") for g in group)
            print(f"  material '{mat}'  ({len(group)} record(s))")
            for i, r in group:
                spec = r.get("specimen_id") or "-"
                cond = str(r.get("material_condition") or "-")[:22]
                bl = ",".join(_blocks(r)) or "(no blocks)"
                flag = "  <-- PARENT? (no specimen while siblings have one)" if (has_spec and not r.get("specimen_id")) else ""
                print(f"    #{i:2}  spec={spec:8}  cond={cond:22}  scalars={_scalars(r):2}  blocks=[{bl}]{flag}")
    print()


if __name__ == "__main__":
    main()