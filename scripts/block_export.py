"""
block_export.py  (was lcf_export_patch.py)
==========================================
Drop-in replacement for _write_excel_output in pipeline.py. Reshapes each nested
block onto its own sheet. Two reshape modes, chosen per block:

  pivot   — a parameter list becomes COLUMNS; rows split only by group_by
            (used by LCF: one row per (material x fit regime), coefficients as
            columns, meaning/unit kept in a *_legend sheet).
  spread  — a measurement list becomes COLUMNS on ONE row; rows are reserved for
            distinct specimens. Used by hardness: HV and HB of the same specimen
            become columns (HV, HV_load_kgf, HB, HB_load_kgf) on a single row, so
            a second measurement never adds a row or a record.

Blocks with no reshape config are flattened to scalars as before.

- records sheet: unchanged, one row per record.
- block sheets: reshaped per the config below; identity columns (record_id,
  source_DOI, material_name) lead every block row so subsheets stay matchable.

Add an entry to BLOCK_RESHAPE for any future block that carries a list.
"""

import re

import pandas as pd

BLOCK_RESHAPE = {
    "lcf": {
        "mode": "pivot",
        "list_field": "additional_parameters",
        "group_by": ["fit_regime", "fit_range"],    # what MAY split into rows
        "column_key": "symbol_as_printed",           # becomes the column header
        "value_key": "value",
        "unit_key": "unit",
        "meaning_key": "parameter_meaning",
        "framework_key": "framework",
        "source_key": "source_figure_or_table",
    },
    "hardness": {
        "mode": "spread",
        "list_field": "measurements",
        "group_by": [],                              # one row per specimen; measurements do NOT add rows
        "key_field": "hardness_scale",               # HV, HB -> column names
        "canon_key": True,                           # 'HV0.5','HV5'->'HV'; 'HBW2.5/62.5'->'HB' 
        "value_key": "hardness_value",               # the bare-scale column holds the value
        "extra_fields": {"test_load_kgf": "load_kgf"},  # -> HV_load_kgf, HB_load_kgf
    },
}


def _canon_scale(s):
    """Base hardness designation from a scale string that may carry load/indenter
    detail: 'HV0.5'->'HV', 'HV5'->'HV', 'HBW2.5/62.5'->'HB', 'HRC'->'HRC'. The load
    lives in its own column, so the scale column should be the bare type."""
    m = re.match(r"[A-Za-z]+", str(s or "").strip())
    base = (m.group(0) if m else "H").upper()
    return {"HBW": "HB", "HBS": "HB"}.get(base, base)


def _slug(s, maxlen=30):
    return re.sub(r"[^a-z0-9]+", "-", str(s or "").lower()).strip("-")[:maxlen] or "na"


def _first_non_null(series):
    for v in series:
        if isinstance(v, dict):
            return v
    return None


def _find_list_of_dicts(block: dict, field: str):
    """Locate the list whether it sits directly on the block or one level down
    (e.g. lcf['additional_parameters'] or hardness['results']['measurements'])."""
    v = block.get(field)
    if isinstance(v, list):
        return v
    for sub in block.values():
        if isinstance(sub, dict) and isinstance(sub.get(field), list):
            return sub[field]
    return None


def _flat_scalars(block: dict) -> dict:
    """Scalar fields from the block and its immediate sub-dicts, so a reshaped row
    still carries conditions/specimen scalars. Lists are skipped."""
    out = {}
    for k, v in block.items():
        if isinstance(v, dict):
            for k2, v2 in v.items():
                if not isinstance(v2, (list, dict)):
                    out.setdefault(k2, v2)
        elif not isinstance(v, list):
            out.setdefault(k, v)
    return out


def _pivot_block(ident: dict, block: dict, cfg: dict, legend: dict) -> list:
    params = _find_list_of_dicts(block, cfg["list_field"])
    base = {**ident, **_flat_scalars(block)}
    if not params:
        return [base]  # single (non-piecewise) fit: flat coefficients only

    groups = {}
    for p in params:
        gkey = tuple(p.get(g) for g in cfg["group_by"])
        row = groups.get(gkey)
        if row is None:
            row = dict(base)
            for g in cfg["group_by"]:
                row[g] = p.get(g)
            suffix = "-".join(_slug(p.get(g)) for g in cfg["group_by"] if p.get(g) not in (None, ""))
            if suffix:
                row["record_id"] = f"{ident.get('record_id', 'unknown')}__{suffix}"
            if cfg.get("source_key"):
                row[cfg["source_key"]] = p.get(cfg["source_key"])
            groups[gkey] = row

        col = p.get(cfg["column_key"])
        if not col:
            continue
        if col in row and row[col] not in (None, ""):
            n = 2
            while f"{col}#{n}" in row:
                n += 1
            col = f"{col}#{n}"
        row[col] = p.get(cfg["value_key"])

        sym = p.get(cfg["column_key"])
        if sym and sym not in legend:
            legend[sym] = {
                "symbol": sym,
                "parameter_meaning": p.get(cfg.get("meaning_key")),
                "framework": p.get(cfg.get("framework_key")),
                "unit": p.get(cfg.get("unit_key")),
            }
    return list(groups.values())


def _spread_block(ident: dict, block: dict, cfg: dict) -> list:
    """Spread a measurement list across COLUMNS on one row (rows are for distinct
    specimens, not measurements). Each entry's key_field value names a column that
    holds value_key; extra_fields ride along as key_value_suffix columns. A
    repeated scale is suffixed (HV#2) so a collision is visible, never overwritten.
    group_by (usually empty) is the only thing that may add a row."""
    items = _find_list_of_dicts(block, cfg["list_field"])
    base = {**ident, **_flat_scalars(block)}
    if not items:
        return [base]

    group_by = cfg.get("group_by", [])
    extra = cfg.get("extra_fields", {})
    groups = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        gkey = tuple(it.get(g) for g in group_by)
        row = groups.get(gkey)
        if row is None:
            row = dict(base)
            for g in group_by:
                row[g] = it.get(g)
            groups[gkey] = row

        raw_scale = it.get(cfg["key_field"])
        scale = _canon_scale(raw_scale) if cfg.get("canon_key") else \
            str(raw_scale or "x").strip().replace(" ", "")
        if scale in row and row[scale] not in (None, ""):
            n = 2
            while f"{scale}#{n}" in row:
                n += 1
            scale = f"{scale}#{n}"
        row[scale] = it.get(cfg["value_key"])
        for src_field, suffix in extra.items():
            row[f"{scale}_{suffix}"] = it.get(src_field)
    return list(groups.values())


def _norm_block(name) -> str:
    return str(name).strip().upper()


def _safe_sheet(name: str, used: set) -> str:
    base = str(name)[:31]
    if base.lower() not in used:
        used.add(base.lower())
        return base
    n = 1
    while f"{base[:29]}_{n}".lower() in used:
        n += 1
    out = f"{base[:29]}_{n}"
    used.add(out.lower())
    return out


_RESHAPE_BY_NORM = {_norm_block(k): v for k, v in BLOCK_RESHAPE.items()}


def _write_excel_output(df: pd.DataFrame, excel_out):
    """Records sheet plus one sheet per nested block, blocks grouped by normalized
    name so a technique split across papers merges into one sheet."""
    excel_out.parent.mkdir(parents=True, exist_ok=True)
    used_sheets = {"records"}

    groups = {}
    if not df.empty:
        for c in df.columns:
            if _first_non_null(df[c]) is None:
                continue
            nb = _norm_block(c)
            g = groups.get(nb)
            if g is None:
                groups[nb] = {"display": c, "cols": [c]}
            else:
                g["cols"].append(c)

    with pd.ExcelWriter(excel_out, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="records", index=False)

        for nb, g in groups.items():
            cfg = _RESHAPE_BY_NORM.get(nb)
            rows, legend = [], {}
            for _, r in df.iterrows():
                for col in g["cols"]:
                    block = r.get(col)
                    if not isinstance(block, dict):
                        continue
                    ident = {
                        "record_id": r.get("record_id") or r.get("material_name") or "unknown",
                        "source_DOI": r.get("source_DOI"),
                        "material_name": r.get("material_name"),
                    }
                    mode = cfg.get("mode") if cfg else None
                    if mode == "pivot":
                        rows.extend(_pivot_block(ident, block, cfg, legend))
                    elif mode == "spread":
                        rows.extend(_spread_block(ident, block, cfg))
                    else:
                        rows.append({**ident, **_flat_scalars(block)})

            if rows:
                pd.DataFrame(rows).to_excel(
                    writer, sheet_name=_safe_sheet(g["display"], used_sheets), index=False)
            if legend:
                pd.DataFrame(list(legend.values())).to_excel(
                    writer, sheet_name=_safe_sheet(f"{g['display'][:24]}_legend", used_sheets), index=False)