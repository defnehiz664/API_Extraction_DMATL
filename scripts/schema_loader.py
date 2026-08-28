"""
schema_loader.py
================
Loads a project schema YAML and produces:
  1. A dynamic Pydantic model for Gemini structured output
  2. A prompt section describing each field for Gemini

YAML structure:
  project / material_system / domain_rules: injected verbatim
  fields:            flat scalar attributes of the material-condition
  tests:             mapping of block-name -> {conditions, specimen, results}
  characterization:  same shape as tests
A field may declare `item_fields` to become a list of sub-objects.
"""

#from dataclasses import field
#from xml.parsers.expat import model

import yaml
from pathlib import Path
from typing import Optional
from pydantic import create_model, BaseModel, ConfigDict
import re

_SCALARS = {"str": str, "float": float, "int": int, "bool": bool}
_LIST_SCALARS = {
    "list[str]": list[str],
    "list[float]": list[float],
    "list[int]": list[int],
    "list[bool]": list[bool],
}
_BLOCK_PARTS = ("conditions", "specimen", "results")


def _resolve(field: dict, ns: str) -> tuple:
    """Map one field dict to a (annotation, default) pair, recursing into item_fields."""
    name, ftype = field["name"], field.get("type", "str")

    if "item_fields" in field:
        item_model = _model(f"{ns}_{name}_item", field["item_fields"])
        return (Optional[list[item_model]], None)

    if ftype in _SCALARS:
        return (Optional[_SCALARS[ftype]], None)

    if ftype in _LIST_SCALARS:
        return (Optional[_LIST_SCALARS[ftype]], None)

    if ftype in {"list", "array", "list[dict]", "list[object]"}:
        return (Optional[list], None)

    if ftype in {"dict", "object", "mapping"}:
        return (Optional[dict], None)

    raise ValueError(
        f"Unknown type '{ftype}' for field '{name}' (in {ns}). "
        f"Supported types: str, float, int, bool, list[str], list[float], "
        f"list[int], list[bool], list, list[dict], dict, object.")

def _model(name: str, fields: list) -> type[BaseModel]:
    """Build a Pydantic model from a flat list of field dicts."""
    defs = {}
    for f in fields:
        if f["name"] in defs:
            raise ValueError(f"Duplicate field '{f['name']}' in model '{name}'")
        defs[f["name"]] = _resolve(f, name)
    return create_model(name, __config__=ConfigDict(extra="forbid"), **defs)


def _block_fields(block: dict) -> list:
    """Concatenate a block's conditions/specimen/results into one field list."""
    fields = [f for part in _BLOCK_PARTS for f in (block.get(part) or [])]
    return fields


def load_schema(yaml_path: Path) -> tuple:
    """Read a schema YAML and return (DynamicModel, config)."""
    with open(yaml_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    project = config.get("project", "DynamicRecord")
    top_defs = {}

    for field in config.get("fields", []):
        top_defs[field["name"]] = _resolve(field, project)

    for section in ("tests", "characterization"):
        for block_name, block in (config.get(section) or {}).items():
            if block_name in top_defs:
                raise ValueError(f"Block '{block_name}' collides with a material field")
            block_model = _model(block_name, _block_fields(block))
            top_defs[block_name] = (Optional[block_model], None)

    return create_model(project, __config__=ConfigDict(extra="forbid"), **top_defs), config


def _field_line(field: dict, indent: str = "- ") -> str:
    name, unit, desc = field["name"], field.get("unit", ""), field.get("description", "")
    header = name + (f" [{unit}]" if unit else "")
    if "item_fields" in field:
        subs = ", ".join(sf["name"] for sf in field["item_fields"])
        header += f"  (JSON array of objects, each with: {subs})"
    elif field.get("type", "").startswith("list"):
        header += "  (JSON array)"
    return f"{indent}{header}: {desc}" if desc else f"{indent}{header}"


def build_schema_prompt_section(config: dict) -> str:
    """Generate the FIELDS TO EXTRACT block, including nested blocks."""
    lines = ["FIELDS TO EXTRACT (return null for anything not explicitly reported):",
             "\n## MATERIAL (one record per material-condition)"]
    for field in config.get("fields", []):
        lines.append(_field_line(field))

    headers = {
        "tests": "## TESTS (fill a block only if that test was performed; else leave the whole block null)",
        "characterization": "## CHARACTERIZATION (fill a block only if that method was used; else null)",
    }
    for section, header in headers.items():
        blocks = config.get(section) or {}
        if not blocks:
            continue
        lines.append("\n" + header)
        for block_name, block in blocks.items():
            lines.append(f"\n[{block_name}]")
            for part in _BLOCK_PARTS:
                for f in (block.get(part) or []):
                    lines.append(_field_line(f))
    return "\n".join(lines)


# ── record identity ───────────────────────────────────────────────────────────

RECORD_ID_SEP = "__"
# Schema field(s) tried, in order, for the optional third id segment. Empty on
# purpose: adding a field here changes every id, so it must be one that is
# populated consistently or ids stop comparing across runs.
RECORD_ID_CONDITION_FIELDS: tuple[str, ...] = ()


def slugify(value) -> str:
    """Lowercase hyphen-joined ASCII. Cannot produce RECORD_ID_SEP."""
    s = re.sub(r"[^a-z0-9]+", "-", str(value).casefold()).strip("-")
    return s or "unknown"


def make_record_id(doi_slug: str, material_name, condition=None) -> str:
    """<doi_slug>__<material>[__<condition>]. doi_slug arrives already slugged
    (extract_data.doi_to_filename), keeping file naming out of this module."""
    parts = [str(doi_slug).casefold(), slugify(material_name)]
    if condition not in (None, ""):
        parts.append(slugify(condition))
    return RECORD_ID_SEP.join(parts)


def assign_record_ids(records: list, doi_slug: str) -> list:
    """Stamp deterministic ids on one paper's records, in place. Two records
    with the same material (+condition) are indistinguishable by content, so
    they get an ordinal segment: those rows are the only ones whose id still
    depends on extraction order, and the warning says so."""
    seen = {}
    for rec in records:
        if not isinstance(rec, dict):
            continue
        cond = next((rec[f] for f in RECORD_ID_CONDITION_FIELDS if rec.get(f)), None)
        rid = make_record_id(doi_slug, rec.get("material_name") or "unknown", cond)
        seen[rid] = seen.get(rid, 0) + 1
        if seen[rid] > 1:
            print(f"  WARNING: {seen[rid]} records share id {rid}; appending ordinal "
                  f"(material_name{'+condition' if cond else ''} not unique in this paper)")
            rid = f"{rid}{RECORD_ID_SEP}{seen[rid]}"
        rec["record_id"] = rid
    return records


if __name__ == "__main__":
    import sys

    schema_arg = "schemas/copper/schema.yaml"
    if schema_arg is None:
        print("No schema path provided. Usage: python scripts/schema_loader.py <schema.yaml>")
        sys.exit(1)

    schema_path = Path(schema_arg)
    model, config = load_schema(schema_path)
    print(f"Loaded schema from {schema_path}")
    print(f"Generated Pydantic model: {model.__name__}")
    print("schema_loader.py ran successfully.")

