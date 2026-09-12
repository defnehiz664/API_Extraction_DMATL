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
        "mode": "lcf",                               # dedicated: canonical columns + one leftover cell
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


def _range_tag(v, maxlen=20):
    """Slug for a fit-range/regime column suffix that PRESERVES the comparator and
    unit sign, so '>1.2%' -> 'gt1-2pct' stays distinct from '<1%' -> 'lt1pct'."""
    s = str(v or "")
    for a, b in (("≥", "ge"), ("≤", "le"), (">", "gt"), ("<", "lt"), ("%", "pct")):
        s = s.replace(a, b)
    return _slug(s, maxlen)


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


def _spread_block(ident: dict, block: dict, cfg: dict, legend: dict = None) -> list:
    """Spread a list across COLUMNS on one row (rows are for distinct specimens, not
    for readings or fit segments). Each entry's key_field value names a column that
    holds value_key; extra_fields ride along as key_value_suffix columns.

    A repeated key means one specimen carries that quantity more than once — a
    second hardness reading, or a coefficient refit on another strain-amplitude
    range (a piecewise LCF fit). The first stays in the bare column; the repeat goes
    to a tagged column built from disambiguate_by (e.g. c__gt1-2pct), falling back
    to #2. So a piecewise fit becomes extra COLUMNS, never an extra row.

    group_by (usually empty) is the only thing that may add a row. When legend keys
    are configured, each base symbol's meaning/framework/unit is recorded in
    `legend` for the companion legend sheet."""
    items = _find_list_of_dicts(block, cfg["list_field"])
    base = {**ident, **_flat_scalars(block)}
    if not items:
        return [base]

    group_by = cfg.get("group_by", [])
    extra = cfg.get("extra_fields", {})
    disamb = cfg.get("disambiguate_by", [])
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

        raw_key = it.get(cfg["key_field"])
        key = _canon_scale(raw_key) if cfg.get("canon_key") else \
            str(raw_key or "x").strip().replace(" ", "")
        col = key
        if col in row and row[col] not in (None, ""):          # this specimen already has this key
            tagval = next((it.get(d) for d in disamb if it.get(d) not in (None, "")), None)
            tag = _range_tag(tagval)
            col = f"{key}__{tag}" if tag else f"{key}#2"
            while col in row and row[col] not in (None, ""):    # still colliding -> number it
                m = re.search(r"#(\d+)$", col)
                col = re.sub(r"#\d+$", "", col) + f"#{(int(m.group(1)) + 1) if m else 2}"
        row[col] = it.get(cfg["value_key"])
        for src_field, suffix in extra.items():
            row[f"{col}_{suffix}"] = it.get(src_field)

        if legend is not None and cfg.get("meaning_key") and key not in legend:
            legend[key] = {
                "symbol": key,
                "parameter_meaning": it.get(cfg.get("meaning_key")),
                "framework": it.get(cfg.get("framework_key")),
                "unit": it.get(cfg.get("unit_key")),
            }
    return list(groups.values())


# ── LCF canonical-parameter dictionary ────────────────────────────────────────
# EIGHT parameters get their own columns; every other reported LCF coefficient
# collapses into a single 'additional_parameters' cell. Papers use many symbols for
# the same quantity, so map each KNOWN symbol (normalized by _norm_symbol) to its
# column here. When extraction meets a symbol NOT listed, the pipeline prints a
# warning with its stated meaning/framework: if it's one of these eight under a new
# notation, ADD the symbol below; otherwise ignore it and it stays in the leftover
# cell. Only add symbols you are sure of — a wrong entry silently mis-slots a value.
_FSC = "fatigue_strength_coefficient_MPa"          # sigma'f
_FSE = "fatigue_strength_exponent"                 # b
_FDC = "fatigue_ductility_coefficient"             # epsilon'f
_FDE = "fatigue_ductility_exponent"                # c
_KPR = "cyclic_strength_coefficient_K_prime_MPa"   # K'
_NPR = "cyclic_strain_hardening_exponent_n_prime"  # n'
_W0  = "W_0"                                        # hysteresis energy W0
_BETA = "beta"                                      # hysteresis energy exponent beta
LCF_CANONICAL_COLUMNS = [_FSC, _FSE, _FDC, _FDE, _KPR, _NPR, _W0, _BETA]

LCF_SYMBOL_MAP = {
    # OVERRIDE/FALLBACK ONLY. Routing is by parameter_meaning first (LCF_MEANING_MAP),
    # which is per-entry and collision-proof; this map is consulted only when the
    # meaning is unhelpful. So do NOT add paper-specific letters here (Kp, Ce, Cp…):
    # they mean different things in different papers and are already placed by meaning.
    # Keep this to unambiguous, notation-stable symbols for the canonical parameters.
    "sigma'f": _FSC, "sigmaf'": _FSC, "s'f": _FSC, "sf'": _FSC,   # sigma'f
    "b": _FSE,                                                    # b
    "epsilon'f": _FDC, "epsilonf'": _FDC, "e'f": _FDC, "ef'": _FDC,  # epsilon'f
    "c": _FDE,                                                    # c
    "k'": _KPR, "kprime": _KPR,                                   # K'
    "n'": _NPR, "nprime": _NPR,                                   # n'
    "w0": _W0, "deltaw0": _W0,                                    # W0 (inverted-Wa energy law only)
    "beta": _BETA,                                                # beta (that same law's exponent)
}

# The model already classifies each parameter in its `parameter_meaning` field, and
# for the standard coefficients it emits the canonical field name directly. Routing
# on that is per-entry, so it never suffers the cross-paper letter collisions a
# global symbol map would (Kp = ductility coefficient here, K' elsewhere). This map
# is consulted FIRST; LCF_SYMBOL_MAP is only the fallback/override. Energy-framework
# params come through as parameter_meaning "other" and stay in the leftover cell
# until their mapping is decided.
LCF_MEANING_MAP = {
    "fatigue_strength_coefficient": _FSC, "fatigue_strength_coefficient_mpa": _FSC,
    "fatigue_strength_exponent": _FSE,
    "fatigue_ductility_coefficient": _FDC,
    "fatigue_ductility_exponent": _FDE,
    "cyclic_strength_coefficient": _KPR, "cyclic_strength_coefficient_k_prime": _KPR,
    "cyclic_strength_coefficient_k_prime_mpa": _KPR, "k_prime": _KPR,
    "cyclic_strain_hardening_exponent": _NPR,
    "cyclic_strain_hardening_exponent_n_prime": _NPR, "n_prime": _NPR,
    "elastic_strain_line_coefficient": "elastic_strain_line_coefficient",
    "elastic_strain_line_exponent": "elastic_strain_line_exponent",
}


def _norm_meaning(s) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(s or "").lower()).strip("_")


# LCF sheet column order: identity, then all experiment parameters (conditions +
# specimen), then the canonical eight together, then the elastic-strain-line trio,
# then higher-range columns, then any remaining scalars, with additional_parameters
# last. Columns not present are skipped; unlisted columns fall into the tail.
_LCF_EXPERIMENT = [
    "fatigue_standard", "test_temperature_K", "control_mode", "strain_ratio_R",
    "loading_mode", "waveform", "frequency_Hz", "life_basis", "strain_measure",
    "failure_criterion", "environment", "strain_rate_per_s", "sample_size", "stress_measure",
    "specimen_form", "specimen_gauge_diameter_mm", "specimen_gauge_length_mm",
    "specimen_surface_roughness_Rz_um",
]
_LCF_CANONICAL_ORDER = [_FSC, _FSE, _FDC, _FDE, _KPR, _NPR, _W0, _BETA]
_LCF_ELASTIC = ["elastic_strain_line_coefficient", "elastic_strain_line_coefficient_unit",
                "elastic_strain_line_exponent"]


def _order_lcf_columns(cols) -> list:
    cols = list(cols)
    present = lambda names: [c for c in names if c in cols]
    front = (present(["record_id", "source_DOI", "material_name"])
             + present(_LCF_EXPERIMENT) + present(_LCF_CANONICAL_ORDER) + present(_LCF_ELASTIC))
    higher = sorted(c for c in cols if "__higher_range" in c)
    used = set(front) | set(higher) | {"additional_parameters"}
    rest = [c for c in cols if c not in used]            # units, transition_2Nt, cyclic_response, ...
    tail = ["additional_parameters"] if "additional_parameters" in cols else []
    return front + higher + rest + tail


_GREEK = {"σ": "sigma", "ς": "sigma", "ε": "epsilon", "β": "beta",
          "δ": "delta", "γ": "gamma", "α": "alpha"}

# subscript ₀-₉ and superscript ⁰-⁹ -> normal digits, so W₀ / W⁰ read as W0
_SUBSUP = {0x2080 + i: str(i) for i in range(10)}
_SUBSUP.update({0x2070: "0", 0x00B9: "1", 0x00B2: "2", 0x00B3: "3"})
_SUBSUP.update({cp: str(4 + i) for i, cp in enumerate(range(0x2074, 0x207A))})


def _norm_symbol(s) -> str:
    """Normalize a parameter symbol for LCF_SYMBOL_MAP lookup: sub/superscript
    digits->ASCII, greek->latin, all prime glyphs->', lowercase, keep only
    letters/digits/prime."""
    s = str(s or "").translate(_SUBSUP)
    for g, l in _GREEK.items():
        s = s.replace(g, l).replace(g.upper(), l.upper())
    for pr in ("′", "´", "’", "`"):
        s = s.replace(pr, "'")
    s = s.lower()
    return re.sub(r"[^a-z0-9']", "", s)


def _same_value(a, b) -> bool:
    try:
        return abs(float(a) - float(b)) <= 1e-9 + 1e-6 * abs(float(b))
    except (TypeError, ValueError):
        return str(a).strip() == str(b).strip()


def _fmt_leftover(p) -> str:
    """Compact 'symbol=value unit [framework, range]' for the leftover cell."""
    s = f"{p.get('symbol_as_printed') or '?'}={p.get('value')}"
    if p.get("unit"):
        s += f" {p.get('unit')}"
    extra = ", ".join(x for x in (p.get("framework"), p.get("fit_range") or p.get("fit_regime")) if x)
    if extra:
        s += f" [{extra}]"
    return s


def _lcf_reshape(ident: dict, block: dict, warn_sink: set) -> list:
    """One row per specimen. The eight canonical parameters go in their own columns
    (symbols normalized via LCF_SYMBOL_MAP; a value recurring on another fit range
    gets a range-tagged column, e.g. fatigue_ductility_exponent__gt1-2pct). Every
    other reported coefficient collapses into one 'additional_parameters' cell.
    Unrecognized symbols are added to warn_sink for the caller to report."""
    row = {**ident, **_flat_scalars(block)}
    paper = ident.get("source_DOI") or ident.get("record_id") or "?"
    params = _find_list_of_dicts(block, "additional_parameters") or []
    leftovers = []
    for p in params:
        if not isinstance(p, dict):
            continue
        col = (LCF_MEANING_MAP.get(_norm_meaning(p.get("parameter_meaning")))
               or LCF_SYMBOL_MAP.get(_norm_symbol(p.get("symbol_as_printed"))))
        if not col:
            leftovers.append(p)
            if p.get("symbol_as_printed"):
                warn_sink.add((str(p.get("symbol_as_printed")),
                               str(p.get("parameter_meaning") or ""),
                               str(p.get("framework") or ""),
                               str(paper)))
            continue
        val = p.get("value")
        cur = row.get(col)
        if cur in (None, ""):
            row[col] = val                               # primary (lower) range -> clean numeric
        elif _same_value(cur, val):
            pass                                         # same value already present
        else:
            # A second value on a HIGHER strain range (a piecewise fit). One GENERIC
            # column per parameter, reused across papers; the paper's own range
            # threshold goes INSIDE the cell next to the value, e.g. "-0.72 (>1.2%)",
            # so no threshold-specific column is ever created.
            rng = p.get("fit_range") or p.get("fit_regime")
            cell = f"{val} ({rng})" if rng not in (None, "") else f"{val}"
            hcol, n = f"{col}__higher_range", 2
            while hcol in row and row[hcol] not in (None, ""):   # rare third+ range
                hcol = f"{col}__higher_range#{n}"; n += 1
            row[hcol] = cell
    if leftovers:
        row["additional_parameters"] = "; ".join(_fmt_leftover(p) for p in leftovers)
    return [row]


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

        lcf_warnings = set()
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
                    if mode == "lcf":
                        rows.extend(_lcf_reshape(ident, block, lcf_warnings))
                    elif mode == "pivot":
                        rows.extend(_pivot_block(ident, block, cfg, legend))
                    elif mode == "spread":
                        rows.extend(_spread_block(ident, block, cfg, legend))
                    else:
                        rows.append({**ident, **_flat_scalars(block)})

            if rows:
                out_df = pd.DataFrame(rows)
                if cfg and cfg.get("mode") == "lcf":
                    out_df = out_df.reindex(columns=_order_lcf_columns(out_df.columns))
                out_df.to_excel(
                    writer, sheet_name=_safe_sheet(g["display"], used_sheets), index=False)
            if legend:
                pd.DataFrame(list(legend.values())).to_excel(
                    writer, sheet_name=_safe_sheet(f"{g['display'][:24]}_legend", used_sheets), index=False)

    if lcf_warnings:
        by_sym = {}
        for sym, meaning, framework, paper in lcf_warnings:
            e = by_sym.setdefault(sym, {"meaning": "", "framework": "", "papers": set()})
            e["meaning"] = e["meaning"] or meaning
            e["framework"] = e["framework"] or framework
            if paper and paper != "?":
                e["papers"].add(paper)
        print(f"\n  [LCF] {len(by_sym)} parameter symbol(s) not in LCF_SYMBOL_MAP "
              f"(kept in the additional_parameters cell). If any is one of the eight canonical "
              f"parameters under a new notation, add its symbol to LCF_SYMBOL_MAP in block_export.py:")
        for sym in sorted(by_sym):
            e = by_sym[sym]
            detail = ", ".join(x for x in (e["meaning"], e["framework"]) if x)
            print(f"    - {sym!r}" + (f"  ({detail})" if detail else ""))
            print(f"        in: {', '.join(sorted(e['papers'])) if e['papers'] else '(unknown paper)'}")