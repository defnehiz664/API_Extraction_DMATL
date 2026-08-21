"""
gold_diff.py
============
Accuracy harness. Compares extracted records against a hand-checked
gold_standard.xlsx and reports precision/recall on row matching plus
field-level agreement. This is the "is a run correct" check that
validate_output.py cannot give you: the validator checks shape, this
checks values against ground truth.

Gold-sheet contract (first sheet, one row per material):
  - Two key columns identify each row: source_DOI and material_name.
    For a paper with no DOI, put the same local slug in source_DOI on
    both the gold sheet and the extraction (e.g. inis-9gj78-zk527).
  - Every other column is a field you hand-verified. A BLANK cell means
    "not checked" and is skipped, so you only fill the columns you care
    to audit, not all 76.
  - Nested block fields use a dotted column header, e.g.
    lcf.results.fatigue_ductility_coefficient
    lcf.conditions.temperature_C
  - list[dict] fields (additional_parameters, data_points) are not
    compared here; audit those by eye or via digitization.

Matching is key-based, never row-position: a gold row and an extraction
record match iff (source_DOI, material_name) agree after normalization.
  recall    = matched / gold_rows      (did we find what should be there)
  precision = matched / extraction_rows (did we invent rows that shouldn't)
Field accuracy is computed only over matched pairs and populated gold cells.

Usage:
  python scripts/gold_diff.py                          # default gold + all outputs
  python scripts/gold_diff.py --gold data/gold_standard.xlsx \
                              --outputs-dir data/outputs
  python scripts/gold_diff.py --outputs data/outputs/foo_extraction.json
  python scripts/gold_diff.py --rtol 0.01              # loosen numeric tolerance
"""

import argparse
import json
import math
import re
import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
DEFAULT_GOLD = REPO / "data" / "gold_standard.xlsx"
DEFAULT_OUTPUTS_DIR = REPO / "data" / "outputs"

KEY_FIELDS = ["source_DOI", "material_name"]

# A gold sheet duplicated from features_output.xlsx carries columns added
# downstream by pipeline.py (mendeleev frac_*/mean_*, Materials Project mp_*,
# usfe_*). These are computed from composition, not read from the paper, so
# they never appear as fields in the extraction JSON. Rather than maintain a
# name blocklist, any gold column that is absent from every extraction record
# is treated as out of extraction scope and skipped (and listed, so a schema
# field the extractor produced nowhere is still visible). --include-derived
# forces every gold column to be compared.
DEFAULT_RTOL = 1e-3   # relative tolerance for numeric compare
DEFAULT_ATOL = 1e-6   # absolute floor so near-zero values don't blow up rtol

_BOOL = {"true": True, "false": False, "yes": True, "no": False,
         "y": True, "n": False, "1": True, "0": False}


# ── value normalization / comparison ──────────────────────────────────────────

def _is_blank(v) -> bool:
    if v is None:
        return True
    if isinstance(v, float) and math.isnan(v):
        return True
    return isinstance(v, str) and v.strip() == ""


def _as_float(v):
    """Parse a number or return None. Does not strip commas: a comma is
    ambiguous (thousands vs European decimal) and silently guessing is
    exactly the kind of fabrication this project bans. Flagged instead."""
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


def _norm_str(v) -> str:
    return re.sub(r"\s+", " ", str(v).strip()).casefold()


def _norm_key_part(v) -> str:
    s = _norm_str(v)
    return s.rstrip("/")  # DOIs sometimes carry a trailing slash


def compare(gold, extracted, rtol: float, atol: float):
    """Return (match: bool, note: str). note carries a warning for the
    ambiguous-comma case so a real disagreement is never hidden as a pass."""
    if _is_blank(extracted):
        return False, "extracted is null/missing"

    gf, ef = _as_float(gold), _as_float(extracted)
    if gf is not None and ef is not None:
        if math.isclose(gf, ef, rel_tol=rtol, abs_tol=atol):
            return True, ""
        return False, f"numeric {gf} vs {ef}"

    # one side numeric, other not parseable as float
    if (gf is None) != (ef is None):
        gs, es = str(gold), str(extracted)
        if ("," in gs or "," in es):
            return False, f"unparseable number (comma?) {gs!r} vs {es!r} — check delimiter"

    gb, eb = _BOOL.get(_norm_str(gold)), _BOOL.get(_norm_str(extracted))
    if gb is not None and eb is not None:
        return (gb == eb), ("" if gb == eb else f"bool {gb} vs {eb}")

    if _norm_str(gold) == _norm_str(extracted):
        return True, ""
    return False, f"text {str(gold)!r} vs {str(extracted)!r}"


# ── record access ─────────────────────────────────────────────────────────────

def get_path(rec: dict, dotted: str):
    """Resolve 'lcf.results.x' through nested dicts. None if any hop misses."""
    cur = rec
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def path_present(rec: dict, dotted: str) -> bool:
    """True if every segment of the path exists as a key, even if its value is
    null. Distinguishes 'field the extractor emits (possibly null)' from
    'column that is not an extraction field at all' (pipeline-derived)."""
    cur = rec
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return False
    return True


def rec_key(get) -> tuple:
    return tuple(_norm_key_part(get(k)) for k in KEY_FIELDS)


_MISSING = object()


def _maybe_dict(v):
    """The value as a dict if it is one, or a dict-repr string (the block blob
    an xlsx cell holds, e.g. lcf) parsed back into one. None otherwise."""
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        s = v.strip()
        if s.startswith("{") and s.endswith("}"):
            try:
                import ast
                out = ast.literal_eval(s)
                return out if isinstance(out, dict) else None
            except (ValueError, SyntaxError):
                return None
    return None


def _leaves(d: dict, prefix: str = "") -> dict:
    """Flatten a block into scalar leaves keyed by dotted path. list-valued
    fields (additional_parameters, data_points) are not compared, so skipped."""
    out = {}
    for k, v in d.items():
        p = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_leaves(v, p + "."))
        elif isinstance(v, list):
            continue
        else:
            out[p] = v
    return out


def load_extraction_records(paths) -> list:
    records = []
    for p in paths:
        data = json.loads(Path(p).read_text(encoding="utf-8"))
        recs = data.get("records")
        if not isinstance(recs, list):
            print(f"  [skip] {Path(p).name}: no 'records' list (extraction failed?)")
            continue
        for rec in recs:
            rec = dict(rec)
            rec["_source_file"] = Path(p).name
            records.append(rec)
    return records


def load_gold_rows(gold_path: Path) -> tuple:
    df = pd.read_excel(gold_path, sheet_name=0, dtype=object)
    df = df.where(pd.notnull(df), None)
    cols = list(df.columns)
    missing = [k for k in KEY_FIELDS if k not in cols]
    if missing:
        raise SystemExit(
            f"gold sheet is missing key column(s) {missing}. "
            f"It must contain {KEY_FIELDS} plus any fields to check."
        )
    rows = df.to_dict(orient="records")
    candidate = [c for c in cols if c not in KEY_FIELDS and not str(c).startswith("_")]
    return rows, candidate


# ── main comparison ───────────────────────────────────────────────────────────

def run(gold_path: Path, out_paths, rtol: float, atol: float, include_derived: bool) -> bool:
    gold_rows, candidate = load_gold_rows(gold_path)
    ext_records = load_extraction_records(out_paths)

    if include_derived:
        field_cols, ignored = candidate, []
    else:
        field_cols = [c for c in candidate if any(path_present(r, c) for r in ext_records)]
        ignored = [c for c in candidate if c not in field_cols]

    ext_by_key = {}
    dup_ext = []
    for rec in ext_records:
        k = rec_key(lambda f: rec.get(f))
        if k in ext_by_key:
            dup_ext.append(k)
        ext_by_key[k] = rec

    gold_by_key = {}
    dup_gold = []
    for row in gold_rows:
        k = rec_key(lambda f: row.get(f))
        if k in gold_by_key:
            dup_gold.append(k)
        gold_by_key[k] = row

    gold_keys, ext_keys = set(gold_by_key), set(ext_by_key)
    matched = sorted(gold_keys & ext_keys)
    missed = sorted(gold_keys - ext_keys)      # in gold, not extracted
    spurious = sorted(ext_keys - gold_keys)    # extracted, not in gold

    n_gold, n_ext = len(gold_by_key), len(ext_by_key)
    recall = len(matched) / n_gold if n_gold else 0.0
    precision = len(matched) / n_ext if n_ext else 0.0

    print(f"\nGold:       {gold_path}")
    print(f"Extraction: {len(out_paths)} file(s), {n_ext} record(s)")
    print(f"Fields audited per row: {len(field_cols)}  {field_cols}")
    if ignored:
        print(f"Skipped {len(ignored)} column(s) not present in any extraction record "
              f"(pipeline-derived, or extractor produced nowhere; --include-derived to force): {ignored}")
    print()

    print("── ROW MATCHING ──")
    print(f"  gold rows        {n_gold}")
    print(f"  extraction rows  {n_ext}")
    print(f"  matched          {len(matched)}")
    print(f"  recall           {recall:.1%}   (matched / gold)")
    print(f"  precision        {precision:.1%}   (matched / extraction)")
    if dup_gold:
        print(f"  ! duplicate keys in gold:       {dup_gold}")
    if dup_ext:
        print(f"  ! duplicate keys in extraction: {dup_ext}   <-- spurious extra row?")
    for k in missed:
        print(f"  MISSED (in gold, not extracted):   {k}")
    for k in spurious:
        print(f"  SPURIOUS (extracted, not in gold): {k}")

    print("\n── FIELD ACCURACY (matched rows, populated gold cells) ──")
    compared = correct = 0
    mismatches = []   # (key, label, note); label is dotted for block sub-fields
    for k in matched:
        gold, rec = gold_by_key[k], ext_by_key[k]
        for col in field_cols:
            gv = gold.get(col)
            if _is_blank(gv):
                continue
            ev = get_path(rec, col)

            gdict = _maybe_dict(gv)
            if gdict is not None:
                # block: drill into leaves so the exact differing sub-field is
                # named, instead of dumping the whole blob.
                edict = ev if isinstance(ev, dict) else _maybe_dict(ev)
                if edict is None:
                    compared += 1
                    mismatches.append((k, col, "extracted block is null/missing"))
                    continue
                el = _leaves(edict)
                for path, gval in _leaves(gdict).items():
                    if _is_blank(gval):
                        continue
                    compared += 1
                    eval_ = el.get(path, _MISSING)
                    ok, note = compare(gval, None if eval_ is _MISSING else eval_, rtol, atol)
                    if ok:
                        correct += 1
                    else:
                        mismatches.append((k, f"{col}.{path}", note))
            else:
                compared += 1
                ok, note = compare(gv, ev, rtol, atol)
                if ok:
                    correct += 1
                else:
                    mismatches.append((k, col, note))

    acc = correct / compared if compared else 1.0
    print(f"  cells compared   {compared}")
    print(f"  correct          {correct}")
    print(f"  field accuracy   {acc:.1%}")

    scalar_mm = [m for m in mismatches if "." not in m[1]]
    block_mm = [m for m in mismatches if "." in m[1]]
    for k, col, note in scalar_mm:
        print(f"    MISMATCH  {k}  {col}: {note}")
    block_groups = {}
    for k, label, note in block_mm:
        block, sub = label.split(".", 1)
        block_groups.setdefault(block, []).append((k, sub, note))
    for block, items in block_groups.items():
        print(f"    MISMATCH in block '{block}'  ({len(items)} field(s)):")
        for k, sub, note in items:
            print(f"        {k}  {sub}: {note}")

    # ── column reconciliation: what each side has that the other does not ──
    ext_field_keys = set()
    for r in ext_records:
        ext_field_keys |= set(r.keys())
    ext_field_keys.discard("_source_file")
    gold_all = set(candidate) | set(KEY_FIELDS)
    ext_only = sorted(ext_field_keys - gold_all)

    print("\n── COLUMN RECONCILIATION ──")
    print(f"  gold columns absent from extraction: {ignored or 'none'}")
    print(f"  extraction fields absent from gold:  {ext_only or 'none'}")
    if ext_only:
        print("    (extractor produced these but the gold sheet does not audit them — "
              "possible schema drift, or columns you removed from gold)")

    clean = not missed and not spurious and not mismatches and not dup_ext
    print("\nGold check passed." if clean else "\nGold check found discrepancies (see above).")
    return clean


def main():
    ap = argparse.ArgumentParser(description="Diff extracted records against a gold standard.")
    ap.add_argument("--gold", default=str(DEFAULT_GOLD))
    ap.add_argument("--outputs-dir", default=str(DEFAULT_OUTPUTS_DIR))
    ap.add_argument("--outputs", nargs="*", help="explicit extraction JSON paths (overrides --outputs-dir)")
    ap.add_argument("--rtol", type=float, default=DEFAULT_RTOL)
    ap.add_argument("--atol", type=float, default=DEFAULT_ATOL)
    ap.add_argument("--include-derived", action="store_true",
                    help="also compare mendeleev/MP/usfe columns (skipped by default)")
    args = ap.parse_args()

    gold_path = Path(args.gold)
    if not gold_path.exists():
        raise SystemExit(f"gold sheet not found: {gold_path}")

    if args.outputs:
        out_paths = [Path(p) for p in args.outputs]
    else:
        out_paths = sorted(Path(args.outputs_dir).glob("*_extraction.json"))
    if not out_paths:
        raise SystemExit("no extraction JSON files found.")

    ok = run(gold_path, out_paths, args.rtol, args.atol, args.include_derived)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()