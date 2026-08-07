"""
mendeleev_features.py
======================
Computes mixture-rule descriptors for a composition dict from
composition_builder.py, using per-element properties from element_table.py.

Naming convention: everything here is suffixed `_mendeleev` where a field
of the same conceptual meaning (VEC, delta_atomic_size, delta_H_mix_kJ_mol,
delta_S_mix_J_mol_K) is already produced by Gemini during extraction. These
are kept SEPARATE from the Gemini-extracted values — never overwritten —
so the two can be compared as a data-quality check.
"""

import math
from itertools import combinations

R_GAS = 8.314  # J / (mol K)

from mixing_enthalpy import H_MIX_USABLE as TAKEUCHI_INOUE_H_MIX


def _delta_h_mix(fractions: dict):
    """
    Takeuchi-Inoue enthalpy of mixing: delta_H_mix = sum_{i<j} 4 * H_ij * x_i * x_j.
    Returns (value_kJ_mol, missing_pairs). value is None if any required
    pair is absent from TAKEUCHI_INOUE_H_MIX (never silently treated as 0).
    """
    elements = list(fractions.keys())
    if len(elements) <= 1:
        return 0.0, []

    total = 0.0
    missing = []
    for a, b in combinations(elements, 2):
        h_ab = TAKEUCHI_INOUE_H_MIX.get(frozenset((a, b)))
        if h_ab is None:
            missing.append(f"{a}-{b}")
            continue
        total += 4 * h_ab * fractions[a] * fractions[b]

    if missing:
        return None, missing
    return total, missing


def _weighted(fractions: dict, table, column: str):
    vals, weights = [], []
    for el, frac in fractions.items():
        v = table.loc[el, column]
        if v is None or (isinstance(v, float) and math.isnan(v)):
            continue
        vals.append(v)
        weights.append(frac)
    if not vals:
        return None
    total_w = sum(weights)
    return sum(v * w for v, w in zip(vals, weights)) / total_w


def _solute_vs_base_difference(fractions: dict, table, column: str, base_element: str = "Cu") -> float | None:
    """
    Compute a solute-vs-base descriptor as:
      Des_solute = weighted average of solute properties
      Delta_Des = Des_base - Des_solute

    This is more informative for Cu-rich alloys than a plain composition-weighted
    average, because it preserves the contrast between the Cu matrix and the
    dopant/alloying additions.
    """
    if base_element not in fractions:
        return None

    base_value = table.loc[base_element, column]
    if base_value is None or (isinstance(base_value, float) and math.isnan(base_value)):
        return None

    solute_fractions = {el: frac for el, frac in fractions.items() if el != base_element}
    if not solute_fractions:
        return 0.0

    solute_values = []
    solute_weights = []
    for el, frac in solute_fractions.items():
        v = table.loc[el, column]
        if v is None or (isinstance(v, float) and math.isnan(v)):
            continue
        solute_values.append(v)
        solute_weights.append(frac)

    if not solute_values:
        return 0.0

    total_w = sum(solute_weights)
    solute_value = sum(v * w for v, w in zip(solute_values, solute_weights)) / total_w
    return base_value - solute_value


def _fcc_equivalent_lattice_constant(metallic_radius_pm: float) -> float:
    """
    For elements whose native lattice_structure isn't FCC, estimate the
    lattice constant they WOULD have in an FCC arrangement from atomic
    (metallic) radius: a_fcc = (4/sqrt(2)) * r, with r in angstrom.
    """
    r_angstrom = metallic_radius_pm / 100.0
    return (4.0 / math.sqrt(2.0)) * r_angstrom


def compute_mendeleev_features(fractions: dict, element_table) -> dict:
    """
    fractions: {"Cu": 0.97, "Zn": 0.03} atomic fractions, from composition_builder.
    element_table: DataFrame from element_table.build_element_table(), indexed
                   by symbol, containing all elements in `fractions`.

    Returns a flat dict of mendeleev_-prefixed features plus one frac_{el}
    column per element present in the composition. Instead of a simple
    composition-weighted average, the Cu-alloy descriptors are built as
    solute-vs-base differences: the property of the solute/alloying fraction
    is averaged first, then compared against the Cu matrix property.
    """
    out = {}

    for el, frac in fractions.items():
        out[f"frac_{el}"] = frac

    # Solute-vs-base descriptors for Cu-rich alloys.
    out["mendeleev_atomic_radius_pm_delta_vs_Cu"] = _solute_vs_base_difference(fractions, element_table, "atomic_radius", "Cu")
    out["mendeleev_metallic_radius_pm_delta_vs_Cu"] = _solute_vs_base_difference(fractions, element_table, "metallic_radius", "Cu")
    out["mendeleev_atomic_weight_delta_vs_Cu"] = _solute_vs_base_difference(fractions, element_table, "atomic_weight", "Cu")
    out["mendeleev_melting_point_K_delta_vs_Cu"] = _solute_vs_base_difference(fractions, element_table, "melting_point", "Cu")
    out["mendeleev_density_g_cm3_delta_vs_Cu"] = _solute_vs_base_difference(fractions, element_table, "density", "Cu")
    out["mendeleev_electronegativity_delta_vs_Cu"] = _solute_vs_base_difference(fractions, element_table, "en_pauling", "Cu")

    out["mean_boiling_point_K"] = _weighted(fractions, element_table, "boiling_point")
    out["mean_fusion_heat_kJ_mol"] = _weighted(fractions, element_table, "fusion_heat")
    out["mean_thermal_conductivity_W_mK"] = _weighted(fractions, element_table, "thermal_conductivity")
    out["mean_en_allen"] = _weighted(fractions, element_table, "en_allen")
    out["mean_electron_affinity_eV"] = _weighted(fractions, element_table, "electron_affinity")

    # FCC-equivalent lattice constant per element, then a composition-weighted mixture.
    a_fcc_per_el = {}
    for el in fractions:
        row = element_table.loc[el]
        if row.get("lattice_structure") == "FCC" and row.get("lattice_constant"):
            a_fcc_per_el[el] = row["lattice_constant"]
        elif row.get("metallic_radius"):
            a_fcc_per_el[el] = _fcc_equivalent_lattice_constant(row["metallic_radius"])
        else:
            a_fcc_per_el[el] = None

    valid_a = {el: a for el, a in a_fcc_per_el.items() if a is not None}
    if valid_a:
        total_w = sum(fractions[el] for el in valid_a)
        out["mendeleev_a_fcc_equiv_angstrom"] = sum(fractions[el] * a for el, a in valid_a.items()) / total_w
    else:
        out["mendeleev_a_fcc_equiv_angstrom"] = None

    # VEC (metallurgical convention table, see element_table.VEC_TABLE)
    vec_vals = element_table["VEC"]
    if vec_vals.notna().all():
        out["VEC_mendeleev"] = sum(fractions[el] * element_table.loc[el, "VEC"] for el in fractions)
    else:
        missing = [el for el in fractions if pd_isna(element_table.loc[el, "VEC"])]
        out["VEC_mendeleev"] = None
        out["mendeleev_vec_missing_elements"] = missing

    # Atomic size mismatch (delta), Takeuchi-Inoue style, using metallic radius.
    out["mendeleev_metallic_radius_pm_weighted"] = _weighted(fractions, element_table, "metallic_radius")
    r_bar = out["mendeleev_metallic_radius_pm_weighted"]
    if r_bar:
        variance = sum(
            fractions[el] * (1 - element_table.loc[el, "metallic_radius"] / r_bar) ** 2
            for el in fractions
            if element_table.loc[el, "metallic_radius"] is not None
        )
        out["delta_atomic_size_mendeleev"] = math.sqrt(variance) * 100  # expressed in %
    else:
        out["delta_atomic_size_mendeleev"] = None

    # Ideal entropy of mixing (composition-only, no interaction term).
    n = len(fractions)
    if n > 1:
        out["delta_S_mix_J_mol_K_mendeleev"] = -R_GAS * sum(
            f * math.log(f) for f in fractions.values() if f > 0
        )
    else:
        out["delta_S_mix_J_mol_K_mendeleev"] = 0.0

    # Enthalpy of mixing (Takeuchi-Inoue / Miedema binary pairs, see table above).
    h_mix, missing_pairs = _delta_h_mix(fractions)
    out["delta_H_mix_kJ_mol_mendeleev"] = h_mix
    if missing_pairs:
        out["mendeleev_h_mix_missing_pairs"] = missing_pairs

    return out


def pd_isna(v):
    """Return True for missing values such as None or NaN, and False otherwise."""
    try:
        return v is None or (isinstance(v, float) and math.isnan(v))
    except Exception:
        return v is None
