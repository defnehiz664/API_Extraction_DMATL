"""
summarize_schema_versions.py
============================
Structural change-log across schema versions. Reads every schema_history/*.yaml
(name them so they sort oldest->newest, e.g. v01_..., v02_...) and prints, per
consecutive step, only the FIELD names added or removed. Description/rule wording
changes are ignored on purpose — this is the skeleton, which is what maps a gold
check to a schema version.

Run from the repo root (needs PyYAML, which the pipeline already uses):
  python3 summarize_schema_versions.py
"""

import glob
import os
import yaml


def _collect(fields, prefix):
    out = set()
    for f in fields or []:
        if not isinstance(f, dict) or "name" not in f:
            continue
        p = prefix + f["name"]
        if "item_fields" in f:                      # data_points, additional_parameters, ...
            for sub in f["item_fields"]:
                if isinstance(sub, dict) and "name" in sub:
                    out.add(f"{p}.{sub['name']}")
        else:
            out.add(p)
    return out


def field_paths(schema):
    paths = _collect(schema.get("fields"), "")
    for section in ("tests", "characterization"):
        for block, spec in (schema.get(section) or {}).items():
            if not isinstance(spec, dict):
                continue
            for sub in ("conditions", "specimen", "results"):
                paths |= _collect(spec.get(sub), f"{block}.{sub}.")
    return paths


def main():
    files = sorted(glob.glob("schema_history/*.yaml"))
    if not files:
        print("No files in schema_history/. Run this from the repo root.")
        return

    prev, prev_name = None, None
    for path in files:
        with open(path, encoding="utf-8") as fh:
            schema = yaml.safe_load(fh)
        cur = field_paths(schema)
        name = os.path.basename(path)

        if prev is None:
            print(f"{name}  (baseline: {len(cur)} fields)")
        else:
            added = sorted(cur - prev)
            removed = sorted(prev - cur)
            print(f"\n{prev_name}  ->  {name}   ({len(cur)} fields)")
            if not added and not removed:
                print("  no field-level change (descriptions/rules only)")
            if added:
                print(f"  + added   ({len(added)}): {added}")
            if removed:
                print(f"  - removed ({len(removed)}): {removed}")
        prev, prev_name = cur, name


if __name__ == "__main__":
    main()