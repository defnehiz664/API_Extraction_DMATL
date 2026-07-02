"""
schema_loader.py
================
Loads a project schema YAML and produces:
  1. A dynamic Pydantic model for Gemini structured output
  2. A prompt section describing each field for Gemini

YAML format expected:
  project: str
  material_system: str
  domain_rules: str  (injected verbatim into extraction prompt)
  fields:
    - name: str
      type: str        (str | float | int | bool | list[str] | list[float])
      unit: str        (optional, shown in prompt)
      description: str (shown in prompt, guides Gemini)
"""

import yaml
from pathlib import Path
from typing import Optional
from pydantic import create_model

_TYPE_MAP: dict = {
    "str":          (Optional[str],         None),
    "float":        (Optional[float],       None),
    "int":          (Optional[int],         None),
    "bool":         (Optional[bool],        None),
    "list[str]":    (Optional[list[str]],   None),
    "list[float]":  (Optional[list[float]], None),
}


def load_schema(yaml_path: Path) -> tuple:
    """
    Read a schema YAML and return (DynamicModel, config_dict).

    DynamicModel is a Pydantic BaseModel class with all Optional fields.
    config_dict is the raw parsed YAML (use for prompt generation).
    """
    with open(yaml_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    field_defs = {}
    for field in config.get("fields", []):
        type_key = field.get("type", "str")
        if type_key not in _TYPE_MAP:
            raise ValueError(
                f"Unknown type '{type_key}' for field '{field['name']}' "
                f"in {yaml_path}. Supported: {list(_TYPE_MAP)}"
            )
        field_defs[field["name"]] = _TYPE_MAP[type_key]

    model = create_model(config.get("project", "DynamicRecord"), **field_defs)
    return model, config


def build_schema_prompt_section(config: dict) -> str:
    """
    Generate the FIELDS TO EXTRACT block for the Gemini prompt.
    Each field becomes one bullet point.
    """
    lines = ["FIELDS TO EXTRACT (return null for anything not explicitly reported):\n"]

    for field in config.get("fields", []):
        name  = field["name"]
        ftype = field.get("type", "str")
        unit  = field.get("unit", "")
        desc  = field.get("description", "")

        # Build the header: name [unit] (type)
        header = name
        if unit:
            header += f" [{unit}]"
        if ftype.startswith("list"):
            header += "  (return as JSON array, or null)"

        line = f"- {header}: {desc}" if desc else f"- {header}"
        lines.append(line)

    return "\n".join(lines)
