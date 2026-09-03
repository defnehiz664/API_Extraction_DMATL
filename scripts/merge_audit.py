"""
merge_audit.py — write ONE Excel workbook that shows the merge explicitly, for
every paper at once. Read-only; touches no pipeline output.

Sheets:
  unmerged  — every record as the model emitted it (before any merge), one row each
  merged    — records after merge_records, with n_source = how many collapsed in
  conflicts — every value two merged copies disagreed on (dotted path, both values)

Both the unmerged and merged sheets carry the FULL record: every scalar leaf is
flattened to its own dotted-path column (e.g. hardness.results, lcf.conditions.
strain_ratio), so when the conflicts sheet reports two rows whose ids clash you
can line them up on the unmerged sheet and confirm every other value matches for
yourself. Identity columns (paper, record_id, specimen_id, material_name,
material_condition, blocks) are pinned to the left; the rest follow. Sort or
filter in Excel as you like; the rows that merged are the ones with n_source > 1
on the 'merged' sheet.

Usage:
  python scripts/merge_audit.py                      # all papers -> data/merge_audit.xlsx
  python scripts/merge_audit.py --paper dib
  python scripts/merge_audit.py --out data/audit.xlsx
"""
import argparse, glob, json
from collections import Counter
from pathlib import Path

import pandas as pd
from identity import make_record_id, merge_records

# identity columns pinned to the left of every full-record sheet, in this order.
_ID_COLS = ["paper", "record_id", "specimen_id", "material_name",
            "material_condition", "blocks"]


def _blocks(r):
    return ",".join(k for k, v in r.items() if isinstance(v, dict) and v) or ""


def _flatten(obj, prefix=""):
    """Flatten a record to {dotted_path: cell_value}. Nested dicts recurse so a
    value inside a block gets its own column (hardness.test_load_kgf, ...). Lists
    are rendered as a compact JSON string so multi-reading blocks stay in one
    cell and remain comparable across rows. Internal keys (_merge_conflicts) and
    the identity keys handled separately are skipped."""
    out = {}
    for k, v in obj.items():
        if k.startswith("_"):
            continue
        p = f"{prefix}{k}"
        if isinstance(v, dict):
            if v:
                out.update(_flatten(v, p + "."))
            else:
                out[p] = ""
        elif isinstance(v, list):
            out[p] = json.dumps(v, sort_keys=True, ensure_ascii=False, default=str) if v else ""
        else:
            out[p] = v
    return out


def _row(paper, rid, r, extra=None):
    """A full-record row: pinned identity columns + every flattened leaf. The
    identity keys are dropped from the flattened part so they aren't duplicated,
    and the model's own emitted record_id is pulled out so it can NEVER clobber
    the computed one; it is surfaced separately as 'model_record_id' for audit."""
    flat = _flatten(r)
    model_rid = flat.pop("record_id", None)          # Gemini's raw id, kept out of the way
    for k in ("specimen_id", "material_name", "material_condition"):
        flat.pop(k, None)
    row = {"paper": paper, "record_id": rid,
           "specimen_id": r.get("specimen_id"),
           "material_name": r.get("material_name"),
           "material_condition": r.get("material_condition"),
           "blocks": _blocks(r)}
    row.update(flat)
    if model_rid not in (None, ""):
        row["model_record_id"] = model_rid
    if extra:
        row.update(extra)
    return row


def _frame(rows, lead=()):
    """Build a DataFrame with identity columns (then any `lead` extras like
    n_source) ordered first, remaining columns after in first-seen order."""
    if not rows:
        return pd.DataFrame()
    seen = []
    for row in rows:
        for k in row:
            if k not in seen:
                seen.append(k)
    pinned = _ID_COLS + [c for c in ("model_record_id",) if c in seen] + [c for c in lead if c in seen]
    front = [c for c in pinned if c in seen]
    ordered = front + [c for c in seen if c not in front]
    return pd.DataFrame(rows).reindex(columns=ordered)


def _load(f):
    recs = json.load(open(f, encoding="utf-8")).get("records")
    if isinstance(recs, dict):
        if "error" in recs or "finish_reason" in recs:
            return None
        recs = list(recs.values())
    return [r for r in recs if isinstance(r, dict)] if isinstance(recs, list) else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outputs-dir", default="data/outputs")
    ap.add_argument("--paper", default=None)
    ap.add_argument("--out", default="data/merge_audit.xlsx")
    args = ap.parse_args()

    files = sorted(glob.glob(str(Path(args.outputs_dir) / "*_extraction.json")))
    if args.paper:
        files = [f for f in files if args.paper.lower() in Path(f).name.lower()]

    unmerged, merged, conflicts = [], [], []
    for f in files:
        recs = _load(f)
        if not recs:
            continue
        paper = Path(f).name.removesuffix("_extraction.json")
        keycount = Counter(make_record_id(r, paper) for r in recs)
        for r in recs:
            unmerged.append(_row(paper, make_record_id(r, paper), r))
        m, _ = merge_records([dict(r) for r in recs], paper)
        for r in m:
            key = make_record_id(r, paper)
            merged.append(_row(paper, key, r, {"n_source": keycount[key]}))
            for c in r.get("_merge_conflicts", []):
                conflicts.append({"paper": paper, "record_id": key,
                                  "field": c[0], "value_A": c[1], "value_B": c[2]})

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(out, engine="openpyxl") as w:
        _frame(unmerged).to_excel(w, sheet_name="unmerged", index=False)
        _frame(merged, lead=("n_source",)).to_excel(w, sheet_name="merged", index=False)
        (pd.DataFrame(conflicts) if conflicts else
         pd.DataFrame([{"paper": "", "record_id": "", "field": "no conflicts", "value_A": "", "value_B": ""}])
         ).to_excel(w, sheet_name="conflicts", index=False)

    dropped = len(unmerged) - len(merged)
    print(f"unmerged {len(unmerged)} rows -> merged {len(merged)} rows "
          f"({dropped} collapsed) | conflicts {len(conflicts)}")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()