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
from block_export import _write_excel_output
from schema_loader import assign_record_ids
from identity import merge_records, drop_empty_records, assign_record_ids


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


CANON_BLOCKS = {"sem": "SEM", "tem": "TEM", "ebsd": "EBSD", "xrd": "XRD"}

def _canon_record(rec: dict) -> dict:
    """Merge case-variant block keys (sem/SEM) so each record uses one canonical name."""
    out = {}
    for k, v in rec.items():
        ck = CANON_BLOCKS.get(str(k).lower(), k)
        if ck in out and isinstance(out[ck], dict) and isinstance(v, dict):
            out[ck] = {**out[ck], **v}          # both variants present: merge
        elif ck not in out or out[ck] in (None, "", {}, []):
            out[ck] = v
    return out


def load_all_records(outputs_dir: Path) -> list:
    records = []
    for f in sorted(outputs_dir.glob("*_extraction.json")):
        with open(f, encoding="utf-8") as fh:
            data = json.load(fh)
        raw = data.get("records", [])
        if isinstance(raw, dict):
            if "error" in raw or "finish_reason" in raw:
                print(f"WARNING [{f.name}]: extraction FAILED "
                      f"(finish_reason={raw.get('finish_reason')}); no records — re-extract this paper.")
                continue
            if raw and all(isinstance(v, dict) for v in raw.values()):
                raw = list(raw.values())
            else:
                raw = [raw]
        if not isinstance(raw, list):
            print(f"WARNING [{f.name}]: 'records' is {type(raw).__name__}; skipping")
            continue
        doi_slug = f.stem.removesuffix("_extraction")
        recs = [_canon_record(dict(rec)) for rec in raw if isinstance(rec, dict)]
        recs, n_merged = merge_records(recs, doi_slug)
        if n_merged:
            print(f"NOTE [{f.name}]: merged {n_merged} fragmented record(s) into their specimen")
        #recs, dropped = drop_empty_records(recs)
        #if dropped:
            #print(f"NOTE [{f.name}]: dropped {len(dropped)} record(s) with no measured data: "
                  #f"{[r.get('specimen_id') or r.get('material_condition') or r.get('material_name') for r in dropped]}")
        collisions = assign_record_ids(recs, doi_slug)
        if collisions:
            print(f"WARNING [{f.name}]: {len(collisions)} collision(s) after merge: {collisions}")
        for rec in recs:
            rec["_source_file"] = f.name
            records.append(rec)
    return records


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
    print("record_id in rows:", any("record_id" in r for r in rows))
    df = pd.DataFrame(rows)

    lead = [c for c in ("record_id", "material_name", "source_DOI",
                        "specimen_id", "material_condition") if c in df.columns]
    if "record_id" not in df.columns:
        print("WARNING: record_id missing from every row — assign_record_ids did not run")
    df = df[lead + [c for c in df.columns if c not in lead]]
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
    