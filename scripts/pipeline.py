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

Records containing Ni, Co, or Cu are flagged dataset_scope_flag =
"W_heavy_alloy_no_DBTT" (out-of-scope heavy-alloy systems) and are not
queried against Materials Project.

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
from mp_features import compute_mp_features, skipped_mp_features
from usfe_features import compute_usfe_features

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUTS_DIR = REPO_ROOT / "data" / "outputs"
DEFAULT_CSV_OUT = REPO_ROOT / "data" / "features_output.csv"
DEFAULT_LOG_OUT = REPO_ROOT / "data" / "feature_log.txt"

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

# Records containing any of these elements are tungsten heavy-alloy
# (liquid-phase-sintered W-Ni-Fe/Co/Cu) systems — structurally unrelated
# to the W-DBTT dataset (no DBTT reported, different processing route).
# They're flagged rather than dropped, and skipped for MP queries since
# their elastic/thermo data isn't useful for the DBTT model and querying
# every W-Ni-Fe-Co-Cu combination isn't worth the effort.
HEAVY_ALLOY_INDICATOR_ELEMENTS = {"Ni", "Co", "Cu"}
HEAVY_ALLOY_SCOPE_FLAG = "W_heavy_alloy_no_DBTT"


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


def run_pipeline(outputs_dir: Path, csv_out: Path, log_out: Path):
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

        is_heavy_alloy = bool(HEAVY_ALLOY_INDICATOR_ELEMENTS & set(fractions.keys()))
        row["dataset_scope_flag"] = HEAVY_ALLOY_SCOPE_FLAG if is_heavy_alloy else None
        if is_heavy_alloy:
            log_lines.append(f"  dataset_scope_flag: {HEAVY_ALLOY_SCOPE_FLAG} (contains {HEAVY_ALLOY_INDICATOR_ELEMENTS & set(fractions.keys())})")

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
            if is_heavy_alloy:
                mp_feats = skipped_mp_features(
                    f"skipped — {HEAVY_ALLOY_SCOPE_FLAG}: out of DBTT dataset scope, not worth an MP query"
                )
                row.update(mp_feats)
                log_lines.append(f"  mp features skipped: {mp_feats['mp_query_skipped']}")
            else:
                mp_feats = compute_mp_features(fractions)
                row.update(mp_feats)
                if mp_feats.get("mp_query_skipped"):
                    log_lines.append(f"  mp features skipped: {mp_feats['mp_query_skipped']}")
                else:
                    log_lines.append(f"  mp features: OK (material_id={mp_feats.get('mp_material_id')}, bcc_assumption_applied={mp_feats.get('mp_bcc_assumption_applied')}, all_phases_found={mp_feats.get('mp_all_phases_found')})")
        except Exception as e:
            log_lines.append(f"  mp features FAILED: {e}")
            mp_feats = {}

        try:
            usfe_feats = compute_usfe_features(fractions, mp_features=mp_feats, mendeleev_features=mendeleev_feats)
            row.update(usfe_feats)
            log_lines.append(f"  usfe features: OK ({usfe_feats['usfe_approximation_note']})")
        except Exception as e:
            log_lines.append(f"  usfe features FAILED: {e}")

        rows.append(row)

    df = pd.DataFrame(rows)
    csv_out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(csv_out, index=False)
    log_out.write_text("\n".join(log_lines), encoding="utf-8")

    print(f"[pipeline] wrote {len(df)} row(s) to {csv_out}")
    print(f"[pipeline] wrote log to {log_out}")


def main():
    parser = argparse.ArgumentParser(description="Feature enrichment pipeline for extracted W-DBTT records")
    parser.add_argument("--outputs-dir", default=str(DEFAULT_OUTPUTS_DIR))
    parser.add_argument("--out", default=str(DEFAULT_CSV_OUT))
    parser.add_argument("--log", default=str(DEFAULT_LOG_OUT))
    args = parser.parse_args()

    run_pipeline(Path(args.outputs_dir), Path(args.out), Path(args.log))


if __name__ == "__main__":
    main()
