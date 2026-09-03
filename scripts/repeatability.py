"""
repeatability.py
================
Run-to-run stability of the pipeline. Scores several pipeline outputs (from
repeated extraction runs of the SAME papers) against one gold standard, and
reports:
  - mean and spread of precision / recall / accuracy across the runs
  - a per-field FLIP-RATE: how often a field's value changed across runs
    (the direct measure of the model's non-determinism you flagged)

This is repeatability, distinct from the single-run precision score_extraction
already gives. Generate the runs by extracting the same papers N times, each
into its own features_output (e.g. features_output_run1.xlsx ... run5.xlsx).

Usage:
  python scripts/repeatability.py --gold data/gold_standard.xlsx \
      --preds data/run1.xlsx data/run2.xlsx data/run3.xlsx data/run4.xlsx data/run5.xlsx

To check for row-level stability use to see if the rows are the same across runs, run:
    python3 scripts/score_extraction.py --gold data/gold_standard.xlsx --pred data/reps_v12/features_output_v12_run1.xlsx --no-report | grep -E "rows:|MISSED|SPURIOUS"
    python3 scripts/score_extraction.py --gold data/gold_standard.xlsx --pred data/reps_v12/features_output_v12_run2.xlsx --no-report | grep -E "rows:|MISSED|SPURIOUS"
    python3 scripts/score_extraction.py --gold data/gold_standard.xlsx --pred data/reps_v12/features_output_v12_run3.xlsx --no-report | grep -E "rows:|MISSED|SPURIOUS"
"""

import argparse
import re
import statistics as st
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from score_extraction import score

_METRICS = ["precision", "recall", "accuracy"]
_COUNTS = ["TP", "FP", "FN", "TN", "omission", "wrong_value", "format_unit", "fabrication"]


def _agg(values):
    if not values:
        return (0.0, 0.0)
    return (st.mean(values), st.pstdev(values) if len(values) > 1 else 0.0)


def write_report(path, label, gold, results, field_rate, overall_stability):
    """Append one repeatability session to an Excel log, without regenerating.
      summary   — one row per session: metric mean/sd + overall cell stability.
      <label>   — that session's per-field flip-rate table (its own worksheet).
    Re-running the same label replaces its summary row and its detail sheet."""
    import pandas as pd

    path = Path(path)
    run_id = (re.sub(r"[:\\/?*\[\]]", "-", str(label)).strip() or "run")[:31]
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    n = len(results)

    # one summary row per scope (overall + each sheet), mirroring the terminal
    # blocks: mean/sd of the three metrics and all eight counts across the runs.
    def _scope_stats(getter):
        row = {}
        for m in _METRICS:
            mean, sd = _agg([float(getter(r).get(m, 0.0)) for r in results])
            row[f"{m}_mean"], row[f"{m}_sd"] = round(mean, 4), round(sd, 4)
        for c in _COUNTS:
            mean, sd = _agg([float(getter(r).get(c, 0)) for r in results])
            row[f"{c}_mean"], row[f"{c}_sd"] = round(mean, 2), round(sd, 2)
        return row

    head = {"run_id": run_id, "timestamp": ts, "n_runs": n}
    summary_rows = [{
        **head, "scope": "overall",
        **_scope_stats(lambda r: r["overall"]),
        "cell_stability": round(overall_stability, 4), "gold": str(gold),
    }]
    for s in results[0]["per_sheet"].keys():
        summary_rows.append({
            **head, "scope": s,
            **_scope_stats(lambda r, s=s: r["per_sheet"].get(s, {})),
            "cell_stability": "", "gold": str(gold),
        })

    detail = pd.DataFrame(
        [{"field": f"{sheet}.{col}", "flip_rate": round(rate, 4),
          "cells_varied": nf, "cells_total": nc}
         for rate, sheet, col, nf, nc in field_rate]
    ) if field_rate else pd.DataFrame(
        [{"field": "", "flip_rate": "", "cells_varied": "", "cells_total": ""}])

    # a missing, 0-byte, or corrupt report is treated as absent (start fresh)
    # instead of crashing on an unreadable workbook.
    def _sheets(p):
        p = Path(p)
        if not p.exists() or p.stat().st_size == 0:
            return []
        try:
            with pd.ExcelFile(p, engine="openpyxl") as xl:
                return list(xl.sheet_names)
        except Exception:
            return []

    sheets = _sheets(path)
    if "summary" in sheets:
        summ = pd.read_excel(path, sheet_name="summary", engine="openpyxl")
        summ = summ[summ["run_id"] != run_id]                    # replace this session's rows
        summ = pd.concat([summ, pd.DataFrame(summary_rows)], ignore_index=True)
    else:
        summ = pd.DataFrame(summary_rows)

    # preserve every other schema's detail sheet; replace only this label's.
    others = {s: pd.read_excel(path, sheet_name=s, engine="openpyxl")
              for s in sheets if s not in ("summary", run_id)}

    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as w:
        summ.to_excel(w, sheet_name="summary", index=False)
        for s, df in others.items():
            df.to_excel(w, sheet_name=s, index=False)
        detail.to_excel(w, sheet_name=run_id, index=False)
    print(f"\nAppended session '{run_id}' to {path}  (summary + per-schema flip-rate sheet)")


def main():
    ap = argparse.ArgumentParser(description="Run-to-run repeatability of the pipeline.")
    ap.add_argument("--gold", required=True)
    ap.add_argument("--preds", nargs="+", required=True, help="two or more pipeline output .xlsx files")
    ap.add_argument("--report", default="data/repeatability_history.xlsx",
                    help="Excel workbook to append this session to")
    ap.add_argument("--label", default=None,
                    help="schema-version label for the summary row and detail sheet (default: timestamp)")
    ap.add_argument("--no-report", action="store_true", help="do not append to the report workbook")
    args = ap.parse_args()

    if len(args.preds) < 2:
        raise SystemExit("give at least two --preds runs to measure repeatability.")

    results = [score(args.gold, p) for p in args.preds]
    n = len(results)
    print(f"Gold: {args.gold}\nRuns: {n}  ({', '.join(args.preds)})")

    # ── metric spread (overall) ──
    print("\n── OVERALL METRICS (mean +/- sd across runs) ──")
    for m in _METRICS:
        vals = [r["overall"][m] for r in results]
        mean, sd = _agg(vals)
        print(f"  {m:10s} {mean:6.1%}  +/- {sd:5.1%}   runs: {['%.0f%%' % (v*100) for v in vals]}")
    print("  counts (mean +/- sd):")
    for cnt in _COUNTS:
        vals = [r["overall"][cnt] for r in results]
        mean, sd = _agg(vals)
        print(f"    {cnt:12s} {mean:5.1f} +/- {sd:4.1f}")

    # ── metric spread (per sheet) ──
    sheets = list(results[0]["per_sheet"].keys())
    for sheet in sheets:
        print(f"\n── {sheet} (mean +/- sd) ──")
        for m in _METRICS:
            vals = [r["per_sheet"].get(sheet, {}).get(m, 0.0) for r in results]
            mean, sd = _agg(vals)
            print(f"  {m:10s} {mean:6.1%}  +/- {sd:5.1%}")

    # ── per-field flip-rate ──
    # collect each cell's predicted value across runs; a cell flips if the runs disagree
    cell_runs = defaultdict(lambda: [])
    all_cell_ids = set()
    for r in results:
        for cid in r["pred_cells"]:
            all_cell_ids.add(cid)
    for cid in all_cell_ids:
        for r in results:
            cell_runs[cid].append(r["pred_cells"].get(cid, "<row-missing>"))

    field_cells = defaultdict(list)     # (sheet, col) -> list of flip flags (0/1)
    for (sheet, key, col), vals in cell_runs.items():
        flipped = 1 if len(set(vals)) > 1 else 0
        field_cells[(sheet, col)].append(flipped)

    field_rate = []
    total_cells = flipped_cells = 0
    for (sheet, col), flags in field_cells.items():
        rate = sum(flags) / len(flags)
        field_rate.append((rate, sheet, col, sum(flags), len(flags)))
        total_cells += len(flags)
        flipped_cells += sum(flags)
    field_rate.sort(reverse=True)

    overall_stability = 1 - (flipped_cells / total_cells) if total_cells else 1.0
    print(f"\n── PER-FIELD FLIP-RATE (across {n} runs) ──")
    print(f"  overall cell stability: {overall_stability:.1%}  "
          f"({total_cells - flipped_cells}/{total_cells} cells identical across all runs)")
    unstable = [row for row in field_rate if row[0] > 0]
    if not unstable:
        print("  every audited field was identical across all runs.")
    for rate, sheet, col, nf, nc in unstable:
        print(f"  {rate:5.0%}  {sheet}.{col}   ({nf}/{nc} cells varied)")

    if not args.no_report:
        label = args.label or datetime.now().strftime("%Y%m%d_%H%M%S")
        write_report(args.report, label, args.gold, results, field_rate, overall_stability)


if __name__ == "__main__":
    main()