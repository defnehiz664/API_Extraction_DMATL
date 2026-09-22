#!/usr/bin/env python3
"""Cell stability across repeat runs, split by cause.

Of the cells that differ between runs of an identical configuration, how many
differ because a value was read differently, and how many because the row's
identity moved so the cell was never compared in that run at all?

The two are reported separately because only the first is about reading papers.
The second is a join failure and is fixed in the id scheme, not in the prompt.

Records with unstable identity are derived from the data, not supplied, so the
split cannot be steered by choosing which rows to exclude.

Audit rules mirror score_extraction.py: the same SKIP_COLS, the same blank-gold
skip, the same value canonicalization, and keep_default_na=False so a literal
'null' stays a verified-empty value instead of becoming NaN.

    python scripts/cell_stability.py --gold data/gold_standard.xlsx \
        --runs data/reps_v15/features_output_v15_run*.xlsx
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

IDENTITY_COLS = {"record_id", "material_name", "source_doi"}
PROVENANCE_COLS = {"source_figure_or_table"}
FREETEXT_COLS = {"notes"}
BLOCK_BLOB_COLS = {"tensile", "lcf", "hardness"}
SKIP_COLS = IDENTITY_COLS | PROVENANCE_COLS | FREETEXT_COLS | BLOCK_BLOB_COLS

MISSING = object()          # the row was absent from that run
LIST_SEP = ";"


def _real_col(c) -> bool:
    s = str(c).strip()
    return bool(s) and s.lower() != "nan" and not s.startswith("Unnamed:") and not s.startswith("_")


def _canon(v):
    """Normalized value, so formatting differences do not read as changes."""
    s = "" if v is None else str(v).strip()
    if s == "":
        return None
    try:
        return round(float(s), 6)
    except ValueError:
        pass
    if LIST_SEP in s:
        return tuple(sorted(x.strip().casefold() for x in s.split(LIST_SEP) if x.strip()))
    return " ".join(s.split()).casefold()


def _load(path: Path, sheet: str):
    """(rows keyed by record_id, lowercased column map), or (None, None)."""
    try:
        df = pd.read_excel(path, sheet_name=sheet, dtype=object, keep_default_na=False)
    except ValueError:
        return None, None
    col = next((c for c in df.columns if str(c).strip().lower() == "record_id"), None)
    if col is None:
        return None, None
    df[col] = df[col].astype(str).str.strip()
    lowmap = {str(c).strip().lower(): c for c in df.columns}
    return {r[col]: r for _, r in df.iterrows()}, lowmap


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gold", required=True, type=Path)
    ap.add_argument("--runs", required=True, nargs="+", type=Path)
    args = ap.parse_args()
    n_runs = len(args.runs)

    compared = varied = churn_cells = value_cells = 0
    unstable: dict[str, int] = {}
    per_sheet: dict[str, tuple[int, int, int]] = {}

    for sheet in pd.ExcelFile(args.gold).sheet_names:
        grows, _ = _load(args.gold, sheet)
        if grows is None:
            continue
        gdf = pd.read_excel(args.gold, sheet_name=sheet, dtype=object, keep_default_na=False)
        audit = [c for c in gdf.columns
                 if _real_col(c) and str(c).strip().lower() not in SKIP_COLS]
        if not audit:
            continue

        runs = [_load(p, sheet) for p in args.runs]
        s_cmp = s_churn = s_value = 0

        for rid, grow in grows.items():
            if not rid or rid.lower() in ("", "nan", "none"):
                continue
            present = [rows is not None and rid in rows for rows, _ in runs]
            if not any(present):
                continue                              # never compared, not measurable
            if not all(present):
                unstable[(sheet, rid)] = sum(present)

            for col in audit:
                if str(grow[col]).strip() == "":
                    continue                          # gold blank means not checked
                vals = []
                for (rows, lowmap), ok in zip(runs, present):
                    if not ok:
                        vals.append(MISSING)
                        continue
                    pc = lowmap.get(str(col).strip().lower())
                    vals.append(_canon(rows[rid].get(pc)) if pc else None)

                compared += 1
                s_cmp += 1
                if len(set(vals)) > 1:
                    varied += 1
                    # A cell whose row vanished in some run is attributed to churn
                    # even if the present runs also disagree: had the row matched,
                    # we cannot know what it would have held.
                    if MISSING in vals:
                        churn_cells += 1
                        s_churn += 1
                    else:
                        value_cells += 1
                        s_value += 1

        per_sheet[sheet] = (s_cmp, s_churn, s_value)

    if not compared:
        raise SystemExit("no comparable cells found; check the sheet names and paths")

    print(f"runs compared: {n_runs}\n")
    print(f"{'sheet':12s} {'compared':>9s} {'row churn':>10s} {'value':>7s} {'stable':>8s}")
    for sheet, (c, ch, v) in per_sheet.items():
        if c:
            print(f"{sheet:12s} {c:9d} {ch:10d} {v:7d} {(c - ch - v) / c:7.1%}")

    print(f"\ncells compared in at least one run : {compared}")
    print(f"cells that varied                  : {varied}")
    print(f"  row went missing in some run     : {churn_cells}")
    print(f"  value was read differently       : {value_cells}")
    print(f"\noverall cell stability             : {(compared - varied) / compared:.1%}")
    print(f"stability of values alone          : {(compared - value_cells) / compared:.1%}")

    print(f"\nrecords whose identity is unstable : {len(unstable)}")
    for (sheet, rid), n in sorted(unstable.items()):
        print(f"  {sheet:10s} present in {n}/{n_runs} runs  {rid}")


if __name__ == "__main__":
    main()