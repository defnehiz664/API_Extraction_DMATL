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

from pathlib import Path
import pandas as pd
from mendeleev import element as mdlv_element

CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "element_properties.csv"

# Metallurgical VEC convention (group-derived valence electron count used
# in HEA/refractory-alloy mixture rules) — not a mendeleev attribute.
VEC_TABLE = {
    "W": 6, "Re": 7, "Mo": 6, "Ta": 5, "Nb": 5, "Hf": 4, "Ti": 4, "Zr": 4,
    "V": 5, "Cr": 6, "K": 1, "La": 3, "Y": 3, "Fe": 8, "Ni": 10, "Co": 9,
    "Cu": 11, "C": 4, "O": 6, "N": 5, "Si": 4, "Al": 3,
}

_PROPERTY_ATTRS = [
    "atomic_number",
    "atomic_weight",
    "atomic_radius",       # pm
    "metallic_radius",     # pm
    "covalent_radius_pyykko",  # pm
    "melting_point",       # K
    "boiling_point",       # K
    "density",             # g/cm3
    "lattice_structure",
    "lattice_constant",    # angstrom
    "en_pauling",          # electronegativity (Pauling scale)
    "en_allen",            # electronegativity (Allen scale)
    "fusion_heat",         # kJ/mol
    "thermal_conductivity",  # W/(m*K)
    "electron_affinity",   # eV
]


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


def build_element_table(symbols: list, force_refresh: bool = False) -> pd.DataFrame:
    """
    Return a DataFrame indexed by element symbol with the properties needed
    by the mendeleev/USFE feature modules. Reads from and writes to
    data/element_properties.csv so the mendeleev DB is only queried once
    per element across the whole project lifetime.
    """
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
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        cached.reset_index().to_csv(CACHE_PATH, index=False)

    return cached.loc[symbols]


def get_element_properties(symbol: str) -> dict:
    """Convenience single-element accessor, uses the same cache."""
    return build_element_table([symbol]).loc[symbol].to_dict()
