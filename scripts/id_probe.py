"""
id_probe.py — show, per record, exactly what the identity key is built from, so a
persistent merge conflict can be traced to its cause. Read-only, one paper at a
time. No merging; just prints each raw record's computed id and the coordinates
that went into it.

Usage:
  python id_probe.py data/outputs/10-1016_j-msea-2013-06-034_extraction.json
  python id_probe.py --paper 10-1016_j-msea-2013-06-034      # searches data/outputs
"""
import argparse, glob, json
from collections import Counter
from pathlib import Path

import identity
from identity import make_record_id, _resolve_field, DEFAULT_IDENTITY_FIELDS


def _load(f):
    recs = json.load(open(f, encoding="utf-8")).get("records")
    if isinstance(recs, dict):
        recs = list(recs.values())
    return [r for r in recs if isinstance(r, dict)] if isinstance(recs, list) else []


def _find_temp_paths(rec, prefix="", hits=None):
    """Report EVERY path in the record whose key looks temperature-related, so we
    see where the model actually put it even if it's not lcf.conditions.*"""
    hits = {} if hits is None else hits
    if isinstance(rec, dict):
        for k, v in rec.items():
            p = f"{prefix}{k}"
            if "temp" in k.lower() and not isinstance(v, (dict, list)):
                hits[p] = v
            _find_temp_paths(v, p + ".", hits)
    elif isinstance(rec, list):
        for i, v in enumerate(rec):
            _find_temp_paths(v, f"{prefix}{i}.", hits)
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("json", nargs="?", default=None)
    ap.add_argument("--paper", default=None)
    ap.add_argument("--outputs-dir", default="data/outputs")
    args = ap.parse_args()

    f = args.json
    if not f and args.paper:
        cand = glob.glob(str(Path(args.outputs_dir) / f"*{args.paper}*_extraction.json"))
        f = cand[0] if cand else None
    if not f or not Path(f).exists():
        raise SystemExit("give a JSON path or --paper that exists under --outputs-dir")

    paper = Path(f).name.removesuffix("_extraction.json")
    recs = _load(f)
    print(f"identity.py actually loaded from: {identity.__file__}")
    print(f"paper: {paper}   records: {len(recs)}")
    print(f"identity fields in use: {DEFAULT_IDENTITY_FIELDS}\n")

    ids = []
    for i, r in enumerate(recs):
        rid = make_record_id(r, paper)
        ids.append(rid)
        spec = r.get("specimen_id")
        resolved = {fld: _resolve_field(r, fld) for fld in DEFAULT_IDENTITY_FIELDS}
        temp_paths = _find_temp_paths(r)
        blocks = [k for k, v in r.items() if isinstance(v, dict) and v]
        print(f"[{i}] id = {rid}")
        print(f"     specimen_id      = {spec!r}   (non-empty short-circuits the id)")
        print(f"     material_name    = {r.get('material_name')!r}")
        for fld, val in resolved.items():
            print(f"     resolved {fld} = {val!r}")
        print(f"     any temp-like fields found: {temp_paths or 'NONE'}")
        print(f"     blocks present: {blocks}")
        print()

    dup = {k: n for k, n in Counter(ids).items() if n > 1}
    if dup:
        print("COLLIDING ids (these records will merge):")
        for k, n in dup.items():
            print(f"   {n}x  {k}")
    else:
        print("no colliding ids — every record is distinct, merge would do nothing.")


if __name__ == "__main__":
    main()