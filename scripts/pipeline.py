"""
pipeline.py
===========
Feature enrichment pipeline. Reads every *_extraction.json in
data/outputs/ (the output of extract_data.py), builds a composition for
each record, and appends database-derived features from three tracks:

  Track A — mendeleev_features.py  (element mixture-rule descriptors)
  Track B — mp_features.py         (Materials Project elastic/thermo data)
  Track C — usfe_features.py       (unstable stacking fault energy estimate)

VEC, delta_H_mix_kJ_mol, delta_S_mix_J_mol_K, and delta_atomic_size are
now computed authoritatively by this pipeline (Gemini is instructed to
leave them null). The primary column is overwritten with the pipeline
value whenever the pipeline could compute one, falling back to whatever
was already there otherwise. No _gemini/_mendeleev suffixed audit
columns are kept in the output — only the authoritative unsuffixed name.

Every record is queried against Materials Project; no composition is
filtered, flagged, or skipped.

Output:
  data/features_output.csv   — one row per record, original fields +
                                 mendeleev_/mp_/usfe_ prefixed features
  data/feature_log.txt       — per-record processing log (composition
                                 format used, warnings, skip reasons)

Usage:
  python scripts/pipeline.py
  python scripts/pipeline.py --outputs-dir data/outputs --out data/features_output.csv
"""

import argparse
import json
from pathlib import Path
from datetime import datetime

import pandas as pd

from composition_builder import build_composition
from element_table import build_element_table
from mendeleev_features import compute_mendeleev_features
from mp_features import compute_mp_features
#from usfe_features import compute_usfe_features

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUTS_DIR = REPO_ROOT / "data" / "outputs"
DEFAULT_CSV_OUT = REPO_ROOT / "data" / "features_output.csv"
DEFAULT_XLSX_OUT = REPO_ROOT / "data" / "features_output.xlsx"
DEFAULT_LOG_OUT = REPO_ROOT / "data" / "feature_log.txt"

# Materials Project enrichment is a MODELLING-phase feature, not needed for paper
# extraction (and it nulls for solid-solution alloys anyway). Disabled here to avoid
# API calls; set True to reintroduce it when building the modelling dataset.
ENABLE_MP_FEATURES = False

# The pipeline's mendeleev-computed values are authoritative over whatever
# Gemini put in these columns (Gemini is now instructed to leave them null
# anyway; older records may still have Gemini-computed values). The
# original value — whatever it was — is preserved under a _gemini suffix
# for auditing, and the primary column is overwritten with the pipeline
# value. If the pipeline itself couldn't compute a value (e.g. a missing
# Takeuchi-Inoue pair), the original value is left in place rather than
# being nulled out.
OVERWRITE_FROM_MENDELEEV = {
    "VEC": "VEC_mendeleev",
    "delta_S_mix_J_mol_K": "delta_S_mix_J_mol_K_mendeleev",
    "delta_atomic_size": "delta_atomic_size_mendeleev",
    "delta_H_mix_kJ_mol": "delta_H_mix_kJ_mol_mendeleev",
}


def load_all_records(outputs_dir: Path) -> list:
    """Flatten every record across every *_extraction.json into one list,
    tagging each with its source file for traceability."""
    records = []
    for f in sorted(outputs_dir.glob("*_extraction.json")):
        with open(f, encoding="utf-8") as fh:
            data = json.load(fh)
        for rec in data.get("records", []):
            rec = dict(rec)
            rec["_source_file"] = f.name
            records.append(rec)
    return records

def _write_excel_output(df: pd.DataFrame, excel_out: Path):
    """records sheet keeps every column (blocks as blobs). Each nested block (lcf, SEM,
    TEM, XRD, ...) gets ONE sheet in long format: the block's scalar fields plus, expanded
    into rows, its primary list-of-objects (e.g. additional_parameters). One row per entry,
    record fields repeated. No separate per-list sheet."""
    excel_out.parent.mkdir(parents=True, exist_ok=True)

    def _rid(row):
        return row.get("record_id") or row.get("material_name") or "unknown"

    def _is_dict_col(series):
        return any(isinstance(v, dict) for v in series)

    def _obj_list_keys(block):
        return [k for k, v in block.items()
                if isinstance(v, list) and v and all(isinstance(x, dict) for x in v)]

    dict_cols = [c for c in df.columns if _is_dict_col(df[c])]

    with pd.ExcelWriter(excel_out, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="records", index=False)   # blobs kept here

        for block_name in dict_cols:
            # pick the primary object-list to expand into rows (the one with most entries)
            counts = {}
            for _, row in df.iterrows():
                block = row.get(block_name)
                if isinstance(block, dict):
                    for k in _obj_list_keys(block):
                        counts[k] = counts.get(k, 0) + len(block[k])
            primary = max(counts, key=counts.get) if counts else None

            rows_out = []
            for _, row in df.iterrows():
                block = row.get(block_name)
                if not isinstance(block, dict):
                    continue
                rid = _rid(row)
                scalar = {"record_id": rid}
                for k, v in block.items():
                    if k == primary:
                        continue
                    scalar[k] = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
                entries = block.get(primary) if primary else None
                if entries:
                    for e in entries:
                        rows_out.append({**scalar, **e})   # entry values win on overlap (fit-level precedence)
                else:
                    rows_out.append(scalar)

            if rows_out:
                pd.DataFrame(rows_out).to_excel(writer, sheet_name=block_name[:31], index=False)


    

def run_pipeline(outputs_dir: Path, csv_out: Path, excel_out: Path, log_out: Path):
    records = load_all_records(outputs_dir)
    print(f"[pipeline] loaded {len(records)} record(s) from {outputs_dir}")

    log_lines = [f"feature_log.txt — run at {datetime.now().isoformat()}", "=" * 70]

    # Pre-fetch the element table for every element that appears anywhere,
    # so element_table.py hits mendeleev once per element for the whole run.
    all_elements = set()
    compositions = []
    for rec in records:
        comp = build_composition(rec)
        compositions.append(comp)
        if comp:
            all_elements.update(comp["fractions"].keys())

    element_table = build_element_table(sorted(all_elements)) if all_elements else None

    rows = []
    for rec, comp in zip(records, compositions):
        rid = rec.get("record_id") or rec.get("material_name") or "unknown"
        source = rec.get("_source_file", "?")
        log_lines.append(f"\nRecord: {rid}  (source: {source})")

        row = dict(rec)

        if comp is None:
            log_lines.append("  SKIPPED — could not build composition (insufficient composition fields)")
            rows.append(row)
            continue

        fractions = comp["fractions"]
        log_lines.append(f"  composition format: {comp['format_used']}  fractions: {fractions}")
        for w in comp["warnings"]:
            log_lines.append(f"  WARNING: {w}")

        try:
            sub_table = element_table.loc[list(fractions.keys())]
            mendeleev_feats = compute_mendeleev_features(fractions, sub_table)
            row.update(mendeleev_feats)
            log_lines.append("  mendeleev features: OK")

            overwritten, kept_original = [], []
            for gemini_col, mendeleev_col in OVERWRITE_FROM_MENDELEEV.items():
                pipeline_value = mendeleev_feats.get(mendeleev_col)
                if pipeline_value is not None:
                    row[gemini_col] = pipeline_value
                    overwritten.append(gemini_col)
                else:
                    row[gemini_col] = rec.get(gemini_col)
                    kept_original.append(gemini_col)
                # Suffixed audit column was never wanted in the output —
                # the authoritative unsuffixed column above is sufficient.
                row.pop(mendeleev_col, None)
            if overwritten:
                log_lines.append(f"  overwrote from pipeline: {overwritten}")
            if kept_original:
                log_lines.append(f"  kept original (pipeline could not compute): {kept_original}")
        except Exception as e:
            log_lines.append(f"  mendeleev features FAILED: {e}")
            mendeleev_feats = {}

        try:
            if ENABLE_MP_FEATURES:
                mp_feats = compute_mp_features(fractions)
                row.update(mp_feats)
                if mp_feats.get("mp_query_skipped"):
                    log_lines.append(f"  mp features skipped: {mp_feats['mp_query_skipped']}")
                else:
                    log_lines.append(f"  mp features: OK (material_id={mp_feats.get('mp_material_id')}, all_phases_found={mp_feats.get('mp_all_phases_found')})")
        except Exception as e:
            log_lines.append(f"  mp features FAILED: {e}")
            mp_feats = {}

        # Track C (usfe) not yet enabled:
        # try:
        #     usfe_feats = compute_usfe_features(fractions, mp_features=mp_feats, mendeleev_features=mendeleev_feats)
        #     row.update(usfe_feats)
        #     log_lines.append(f"  usfe features: OK ({usfe_feats['usfe_approximation_note']})")
        # except Exception as e:
        #     log_lines.append(f"  usfe features FAILED: {e}")

        rows.append(row)

    df = pd.DataFrame(rows)
    csv_out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(csv_out, index=False)
    _write_excel_output(df, excel_out)
    log_out.write_text("\n".join(log_lines), encoding="utf-8")

    print(f"[pipeline] wrote {len(df)} row(s) to {csv_out}")
    print(f"[pipeline] wrote {len(df)} row(s) to {excel_out}")
    print(f"[pipeline] wrote log to {log_out}")


def main():
    parser = argparse.ArgumentParser(description="Feature enrichment pipeline for extracted records")
    parser.add_argument("--outputs-dir", default=str(DEFAULT_OUTPUTS_DIR))
    parser.add_argument("--out", default=str(DEFAULT_CSV_OUT))
    parser.add_argument("--excel", default=str(DEFAULT_XLSX_OUT))
    parser.add_argument("--log", default=str(DEFAULT_LOG_OUT))
    args = parser.parse_args()

    run_pipeline(Path(args.outputs_dir), Path(args.out), Path(args.excel), Path(args.log))


if __name__ == "__main__":
    main()