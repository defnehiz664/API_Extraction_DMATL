"""
element_table.py
=================
Builds a per-element property table dynamically from the mendeleev
database (never hardcoded). Caches the result to
data/element_properties.csv so repeated pipeline runs don't re-query
mendeleev for every record.

VEC (valence electron concentration) is NOT available as a clean
mendeleev attribute for transition metals in the metallurgical sense
used here, so a small lookup table is kept for the elements this
project actually encounters. This is a metallurgy convention table,
not a substitute for the elemental property data mendeleev provides.
"""

import os
from pathlib import Path
from typing import Optional
import pandas as pd
from mendeleev import element as mdlv_element

CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "element_properties.csv"

# Metallurgical VEC convention (group-derived valence electron count used
# in HEA/refractory-alloy mixture rules) — not a mendeleev attribute.
VEC_TABLE = {
    "Cu": 11, "Ag": 11, "Al": 3, "Si": 4, "Zn": 12, "Sn": 4, "Ni": 10,
    "Co": 9, "Mn": 7, "Mg": 2, "Fe": 8, "Cr": 6, "P": 5, "Pb": 4,
    "Sb": 5, "Bi": 5, "Ti": 4, "Zr": 4, "W": 6, "Nb": 5, "Ta": 5,
    "Li": 1, "Cd": 2, "S": 6, "Be": 2, "O": 6, "C": 4, "N": 5,
    "Te": 6,
}

_PROPERTY_ATTRS = [
    "atomic_number",
    "atomic_weight",
    "atomic_radius",       # pm
    "metallic_radius",     # pm
    "metallic_radius_c12", # pm, Metallic radius with 12 nearest neighbors
    "covalent_radius_pyykko",  # pm
    "melting_point",       # K
    "is_transition",       # indicates if the listed melting_point value represents a solid-state phase transition (allotropic transformation) rather than a true solid-to-liquid melting point
    "boiling_point",       # K
    "density",             # g/cm3
    "lattice_structure",
    "lattice_constant",    # angstrom
    "en_pauling",          # electronegativity (Pauling scale)
    "en_allen",            # electronegativity (Allen scale)
    "fusion_heat",         # kJ/mol
    "thermal_conductivity",  # W/(m*K)
    "electron_affinity",   # eV
    "ionenergies",         # dict keyed by ionization stage; [1] = first ionization energy
    "en_miedema"            # electronegativity (Miedema scale, for solid and liquid metal alloys)
    "miedema_electron_density" # electron density at the Wigner-Seitz cell boundary (Miedema scale)
    "nvalence"                # number of valence electrons
    "pettifor_number"          # Pettifor chemical scale number allows to predict the crystal structures of binary compounds and alloys based on their chemical composition.
]


def _normalize_symbol(value: object) -> Optional[str]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    if not text:
        return None
    if text[0].isalpha():
        return text[0].upper() + text[1:].lower()
    return text


def _load_manual_element_table(
    file_path: Optional[str | Path] = None,
    column_map: Optional[dict] = None,
    manual_filters: Optional[dict] = None,
    keep_columns: Optional[list] = None,
    sheet_name: Optional[str] = None,
    remove_column_numbers: Optional[list[int]] = None,
    remove_column_names: Optional[list[str]] = None,
) -> pd.DataFrame:
    """
    Load optional manual element-property data from an Excel or CSV file.

    Expected input format:
    - one column identifying the element symbol (e.g. 'symbol', 'element', 'element_symbol')
    - one or more property columns to add to the element cache

    Columns are prefixed with 'DFT_' unless a custom column_map is supplied.
    """
    if file_path is None:
        file_path = os.environ.get("ELEMENT_MANUAL_DATA_FILE")
    if not file_path:
        return pd.DataFrame().set_index(pd.Index([], name="symbol"))

    path = Path(file_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"Manual element data file not found: {path}")

    if path.suffix.lower() in {".xlsx", ".xls"}:
        if sheet_name is None:
            sheet_name = 0
        raw = pd.read_excel(path, sheet_name=sheet_name)
    else:
        raw = pd.read_csv(path)

    if raw.empty:
        return pd.DataFrame().set_index(pd.Index([], name="symbol"))

    symbol_col = None
    for candidate in ("symbol", "element", "element_symbol", "elem", "elements"):
        if candidate in raw.columns:
            symbol_col = candidate
            break
    if symbol_col is None:
        raise ValueError(
            f"Could not find a symbol column in manual data file {path}. "
            "Use one of: symbol, element, element_symbol"
        )

    manual = raw[[symbol_col] + [c for c in raw.columns if c != symbol_col]].copy()
    manual["symbol"] = manual[symbol_col].apply(_normalize_symbol)
    manual = manual.dropna(subset=["symbol"]).drop(columns=[symbol_col])

    if manual_filters:
        for col, allowed in manual_filters.items():
            if col not in manual.columns:
                continue
            if isinstance(allowed, (list, tuple, set, pd.Index)):
                allowed_set = {str(v).strip().lower() for v in allowed if str(v).strip()}
                manual = manual[manual[col].astype(str).str.strip().str.lower().isin(allowed_set)]
            else:
                manual = manual[manual[col].astype(str).str.strip().str.lower() == str(allowed).strip().lower()]

    if keep_columns:
        keep = [c for c in keep_columns if c in manual.columns]
        if keep:
            manual = manual[["symbol"] + keep]

    if remove_column_numbers:
        if not isinstance(remove_column_numbers, list):
            remove_column_numbers = [remove_column_numbers]
        for idx in remove_column_numbers:
            if idx is None:
                continue
            if 0 <= idx < len(manual.columns):
                manual = manual.drop(columns=manual.columns[idx])
            else:
                raise IndexError(f"Column number {idx} is out of range for manual DFT table with {len(manual.columns)} columns")

    if remove_column_names:
        if not isinstance(remove_column_names, list):
            remove_column_names = [remove_column_names]
        for name in remove_column_names:
            if name in manual.columns:
                manual = manual.drop(columns=[name])
            elif name in column_map.values() if column_map else False:
                continue
            else:
                raise KeyError(f"Column name {name} was not found in manual DFT table")

    if column_map:
        rename_map = {col: column_map[col] for col in column_map if col in manual.columns}
        if rename_map:
            manual = manual.rename(columns=rename_map)
    else:
        rename_map = {}
        for col in manual.columns:
            if col == "symbol":
                continue
            if col in _PROPERTY_ATTRS or col in {"VEC"}:
                continue
            rename_map[col] = f"DFT_{col}"
        if rename_map:
            manual = manual.rename(columns=rename_map)

    if manual.empty:
        return pd.DataFrame().set_index(pd.Index([], name="symbol"))

    return manual.set_index("symbol")


def _merge_manual_properties(cached: pd.DataFrame, manual: pd.DataFrame) -> pd.DataFrame:
    """
    Merge manual DFT properties into the cached element table.

    Rule: keep mendeleev-derived values whenever they already exist.
    Only fill columns that are currently missing/empty in the cached table.
    """
    if manual.empty:
        return cached

    manual = manual[~manual.index.duplicated(keep="first")]
    merged = cached.join(manual, how="outer")

    for col in manual.columns:
        if col not in merged.columns:
            continue
        if col in cached.columns:
            existing = merged[col]
            manual_only = manual[col]
            merged[col] = existing.where(existing.notna() & ~existing.eq(""), manual_only)
        else:
            merged[col] = manual[col]

    return merged


def _fetch_element_row(symbol: str) -> dict:
    el = mdlv_element(symbol)
    row = {"symbol": symbol}
    for attr in _PROPERTY_ATTRS:
        try:
            row[attr] = getattr(el, attr)
        except Exception:
            row[attr] = None
    row["VEC"] = VEC_TABLE.get(symbol)
    if row["VEC"] is None:
        print(f"  [element_table] WARNING: no VEC convention value for '{symbol}' — add it to VEC_TABLE if needed")
    return row


def build_element_table(
    symbols: Optional[list] = None,
    force_refresh: bool = False,
    manual_data_path: Optional[str | Path] = None,
    manual_column_map: Optional[dict] = None,
    manual_filters: Optional[dict] = None,
    keep_manual_columns: Optional[list] = None,
    sheet_name: Optional[str | int] = None,
    remove_column_numbers: Optional[list[int]] = None,
    remove_column_names: Optional[list[str]] = None,
) -> pd.DataFrame:
    """
    Return a DataFrame indexed by element symbol with the properties needed
    by the mendeleev/USFE feature modules. Reads from and writes to
    data/element_properties.csv so the mendeleev DB is only queried once
    per element across the whole project lifetime.

    Optionally merge manually curated DFT values from an Excel/CSV file.
    Example:
        build_element_table(
            ["Cu", "Ni"],
            manual_data_path="data/shang_2024_dft.xlsx",
            manual_column_map={"cohesive_energy": "DFT_cohesive_energy_eV_atom"},
            manual_filters={"structure": ["fcc"]},
            keep_manual_columns=["cohesive_energy"],
            sheet_name="Sheet1",
        )
    """
    if symbols is None:
        symbols = list(VEC_TABLE.keys())
    symbols = sorted(set(symbols))

    if CACHE_PATH.exists() and not force_refresh:
        cached = pd.read_csv(CACHE_PATH).set_index("symbol")
    else:
        cached = pd.DataFrame().set_index(pd.Index([], name="symbol"))

    missing = [s for s in symbols if s not in cached.index]
    if missing:
        print(f"  [element_table] fetching {len(missing)} new element(s) from mendeleev: {missing}")
        new_rows = [_fetch_element_row(s) for s in missing]
        new_df = pd.DataFrame(new_rows).set_index("symbol")
        cached = pd.concat([cached, new_df])

    if manual_filters is None:
        manual_filters = {"structure": ["fcc"]}

    if manual_data_path is not None and manual_filters is not None and "structure" not in manual_filters:
        manual_filters = {**manual_filters, "structure": ["fcc"]}

    manual_df = _load_manual_element_table(
        manual_data_path,
        manual_column_map,
        manual_filters=manual_filters,
        keep_columns=keep_manual_columns,
        sheet_name=sheet_name,
        remove_column_numbers=remove_column_numbers,
        remove_column_names=remove_column_names,
    )
    if not manual_df.empty:
        relevant_symbols = [s for s in symbols if s in manual_df.index]
        if relevant_symbols:
            manual_df = manual_df.loc[relevant_symbols]
        cached = _merge_manual_properties(cached, manual_df)

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    cached.reset_index().to_csv(CACHE_PATH, index=False)

    return cached.loc[symbols]


def get_element_properties(
    symbol: str,
    manual_data_path: Optional[str | Path] = None,
    manual_column_map: Optional[dict] = None,
    manual_filters: Optional[dict] = None,
    keep_manual_columns: Optional[list] = None,
    sheet_name: Optional[str | int] = None,
    remove_column_numbers: Optional[list[int]] = None,
    remove_column_names: Optional[list[str]] = None,
) -> dict:
    """Convenience single-element accessor, uses the same cache."""
    return build_element_table(
        [symbol],
        manual_data_path=manual_data_path,
        manual_column_map=manual_column_map,
        manual_filters=manual_filters,
        keep_manual_columns=keep_manual_columns,
        sheet_name=sheet_name,
        remove_column_numbers=remove_column_numbers,
        remove_column_names=remove_column_names,
    ).loc[symbol].to_dict()
