"""
validate_output.py
==================
Validate extracted records against the schema's Pydantic model. Catches renamed,
extra, wrong-typed, or malformed fields that the prompt alone cannot enforce.

Requires schema_loader.py to build models with extra="forbid" (see ConfigDict edit),
otherwise extra/renamed fields are silently ignored and not reported.

Usage:
  python scripts/validate_output.py                       # all *_extraction.json
  python scripts/validate_output.py data/outputs/foo.json # one file
"""

import json
import sys
from pathlib import Path

from pydantic import ValidationError
from schema_loader import load_schema

REPO    = Path(__file__).resolve().parent.parent
SCHEMA  = REPO / "schemas" / "copper" / "schema.yaml"
OUTPUTS = REPO / "data" / "outputs"


def validate_file(model, path: Path) -> bool:
    data = json.loads(path.read_text(encoding="utf-8"))
    records = data.get("records")

    if not isinstance(records, list):
        print(f"  {path.name}: 'records' is missing or not a list (extraction may have failed)")
        return False

    bad = []
    for i, rec in enumerate(records):
        try:
            model.model_validate(rec)
        except ValidationError as e:
            bad.append((i, e.errors()))

    ok = len(records) - len(bad)
    print(f"  {path.name}: {ok}/{len(records)} valid" + ("" if not bad else "   <-- ISSUES"))
    for i, errs in bad:
        for err in errs:
            loc = ".".join(str(x) for x in err["loc"])
            print(f"      record {i}  {loc}: {err['msg']}  (type={err['type']})")
    return not bad


def main():
    model, _ = load_schema(SCHEMA)

    args = sys.argv[1:]
    paths = [Path(a) for a in args] if args else sorted(OUTPUTS.glob("*_extraction.json"))
    if not paths:
        print("No extraction JSON files found.")
        return

    print(f"Validating {len(paths)} file(s) against {model.__name__}:\n")
    all_ok = True
    for p in paths:
        all_ok &= validate_file(model, p)

    print("\nAll records valid." if all_ok else "\nValidation found issues (see above).")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()