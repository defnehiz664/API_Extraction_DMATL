"""
lcf_export_patch.py
===================
Drop-in replacement for _write_excel_output in pipeline.py.

Change vs. the exploding version: a nested block's parameter list
(additional_parameters) is no longer written one-row-per-parameter.
It is pivoted so that a row means (material x fit regime) and every
parameter becomes its own column. A different strain range is the ONLY
thing that adds a row on the block sheet; parameters never do.

- records sheet: unchanged, one row per material, lcf blob preserved.
- lcf sheet: one row per (record x group_by tuple), parameter columns.
- lcf_legend sheet: symbol -> (parameter_meaning, framework, unit), so
  the meaning and unit dropped from the pivoted headers are not lost.

Per-block reshape config. Add an entry here for any future block that
carries a pivotable parameter list.
"""

import pandas as pd

BLOCK_PIVOT = {
    "lcf": {
        "list_field": "additional_parameters",
        "group_by": ["fit_regime", "fit_range"],   # what MAY split into rows
        "column_key": "symbol_as_printed",          # becomes the column header
        "value_key": "value",
        "unit_key": "unit",
        "meaning_key": "parameter_meaning",
        "framework_key": "framework",
        "source_key": "source_figure_or_table",
    },
}


def _first_non_null(series):
    for v in series:
        if isinstance(v, dict):
            return v
    return None


def _find_list_of_dicts(block: dict, field: str):
    """Locate the parameter list whether it sits directly on the block or
    one level down (e.g. lcf['additional_parameters'] or lcf['results'][...])."""
    v = block.get(field)
    if isinstance(v, list):
        return v
    for sub in block.values():
        if isinstance(sub, dict) and isinstance(sub.get(field), list):
            return sub[field]
    return None


def _flat_scalars(block: dict) -> dict:
    """Scalar fields from the block and its immediate sub-dicts, so a pivoted
    row still carries conditions/specimen/results scalars. Lists are skipped."""
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
            if cfg.get("source_key"):
                row[cfg["source_key"]] = p.get(cfg["source_key"])
            groups[gkey] = row

        col = p.get(cfg["column_key"])
        if not col:
            continue
        # never overwrite a filled cell silently: a repeated symbol within one
        # regime is suffixed so the collision is visible, not hidden.
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


def _norm_block(name) -> str:
    """Block identity for sheet grouping. Collapses casing/whitespace variants
    (SEM, Sem, 'SEM ') so the same technique from different papers lands on ONE
    sheet instead of colliding into SEM / SEM1 under Excel's case-insensitive
    sheet naming."""
    return str(name).strip().upper()


def _safe_sheet(name: str, used: set) -> str:
    """<=31 chars and unique case-insensitively (Excel's own rule), so a real
    remaining collision is suffixed deterministically rather than by openpyxl."""
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


_PIVOT_BY_NORM = {_norm_block(k): v for k, v in BLOCK_PIVOT.items()}


def _write_excel_output(df: pd.DataFrame, excel_out):
    """Records sheet plus one sheet per nested block, blocks grouped by
    normalized name so a technique split across papers merges into one sheet."""
    excel_out.parent.mkdir(parents=True, exist_ok=True)
    used_sheets = {"records"}

    # group block columns by normalized identity; keep the first-seen label as
    # the display name and remember every raw column that maps to it.
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
            cfg = _PIVOT_BY_NORM.get(nb)
            rows, legend = [], {}
            for _, r in df.iterrows():
                for col in g["cols"]:
                    block = r.get(col)
                    if not isinstance(block, dict):
                        continue
                    ident = {
                        "record_id": r.get("record_id") or r.get("material_name") or "unknown",
                        "material_name": r.get("material_name"),
                    }
                    if cfg:
                        rows.extend(_pivot_block(ident, block, cfg, legend))
                    else:
                        rows.append({**ident, **_flat_scalars(block)})

            if rows:
                pd.DataFrame(rows).to_excel(
                    writer, sheet_name=_safe_sheet(g["display"], used_sheets), index=False)
            if legend:
                pd.DataFrame(list(legend.values())).to_excel(
                    writer, sheet_name=_safe_sheet(f"{g['display'][:24]}_legend", used_sheets), index=False)