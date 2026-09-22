#!/usr/bin/env python3
"""For every gold record_id missing from some runs, show what those runs had instead.

Reports per sheet, because a record can be stable on one sheet and not another,
and that difference is itself a finding rather than noise to average away.

A candidate is matched to a gold id when it shares the DOI segment and the gold
does not already contain it. Among those, the one differing in the fewest
segments wins, which mirrors the near-miss rule in score_extraction.py.

    python scripts/alternative_identities.py --gold data/gold_standard.xlsx \
        --runs data/reps_v15/features_output_v15_run*.xlsx
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

SEP = "__"


def _ids(path: Path, sheet: str) -> set[str] | None:
    try:
        df = pd.read_excel(path, sheet_name=sheet, dtype=object, keep_default_na=False)
    except ValueError:
        return None
    col = next((c for c in df.columns if str(c).strip().lower() == "record_id"), None)
    if col is None:
        return None
    return {str(v).strip() for v in df[col] if str(v).strip()}


def _compare(gold_id: str, cand: str):
    """(distance, description) for a candidate, or None if it is a different paper."""
    a, b = gold_id.split(SEP), cand.split(SEP)
    if a[0] != b[0]:
        return None
    if len(a) != len(b):
        return (99, f"segment count {len(a)} -> {len(b)}")
    diffs = [(i, x, y) for i, (x, y) in enumerate(zip(a, b)) if x != y]
    if not diffs:
        return None
    return (len(diffs), ", ".join(f"segment {i}: {x!r} -> {y!r}" for i, x, y in diffs))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gold", required=True, type=Path)
    ap.add_argument("--runs", required=True, nargs="+", type=Path)
    args = ap.parse_args()

    names = [p.stem for p in args.runs]

    for sheet in pd.ExcelFile(args.gold).sheet_names:
        gold = _ids(args.gold, sheet)
        if not gold:
            continue
        runs = {n: _ids(p, sheet) for n, p in zip(names, args.runs)}
        live = {n: s for n, s in runs.items() if s is not None}
        if not live:
            continue

        unstable = [rid for rid in sorted(gold)
                    if not all(rid in s for s in live.values())]
        print(f"\n=== {sheet}: {len(unstable)} of {len(gold)} gold ids unstable ===")
        if not unstable:
            continue

        for rid in unstable:
            have = [n for n, s in live.items() if rid in s]
            print(f"\n  {rid}")
            print(f"    present in {len(have)}/{len(live)}: {', '.join(have) or 'none'}")
            for name, s in live.items():
                if rid in s:
                    continue
                best = None
                for cand in s - gold:
                    d = _compare(rid, cand)
                    if d and (best is None or d[0] < best[1][0]):
                        best = (cand, d)
                if best:
                    print(f"    {name}: {best[0]}")
                    print(f"      {best[1][1]}")
                else:
                    print(f"    {name}: absent, and no candidate shares the DOI "
                          f"(the row was not produced at all)")

    for sheet in pd.ExcelFile(args.gold).sheet_names:
        gold = _ids(args.gold, sheet)
        if not gold:
            print(f"\n=== {sheet}: skipped, no record_id column in the gold ===")
            continue
        runs = {n: _ids(p, sheet) for n, p in zip(names, args.runs)}
        live = {n: s for n, s in runs.items() if s is not None}
        if not live:
            print(f"\n=== {sheet}: skipped, no run workbook has a sheet by this name ===")
            continue

if __name__ == "__main__":
    main()