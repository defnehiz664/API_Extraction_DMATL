"""
composition_builder.py
=======================
Turns an extracted record into a clean composition dict for Cu-based alloys,
for example {"Cu": 0.97, "Zn": 0.03} — atomic (mole) fractions.

Two input formats are supported:
  Format A: a `composition_string` field on the record, pymatgen-parseable
            (e.g. "Cu0.97Zn0.03"). This is supported so schemas can add
            a direct composition string when available.
  Format B: reconstructed from the discrete fields emitted by the schema:
            composition_type, base_material_purity_pct,
            dopant_or_alloying_element, dopant_concentration (+unit),
            second_dopant_element, second_dopant_concentration (+unit).

All downstream feature modules (mendeleev_features, mp_features,
usfe_features) consume the output of build_composition() and never touch
the raw record fields directly.
"""

from mendeleev import element as mdlv_element

BASE_ELEMENT = "Cu"


def _atomic_weight(symbol: str) -> float:
    return mdlv_element(symbol).atomic_weight


def wt_to_at_fractions(wt_fractions: dict) -> dict:
    """Convert a dict of {element: wt_fraction} to {element: at_fraction}."""
    moles = {el: w / _atomic_weight(el) for el, w in wt_fractions.items()}
    total = sum(moles.values())
    return {el: m / total for el, m in moles.items()}


def _from_composition_string(comp_str: str):
    """
    Parse a pymatgen-style formula string, e.g. "Cu0.982Zn0.018", into
    atomic (mole) fractions using pymatgen's own Composition parser.
    """
    from pymatgen.core import Composition

    try:
        comp = Composition(comp_str)
    except Exception as e:
        return {
            "fractions": None,
            "format_used": "A_composition_string",
            "warnings": [f"composition_string='{comp_str}' failed to parse via pymatgen: {e}"],
        }

    fractions = {str(el): comp.get_atomic_fraction(el) for el in comp.elements}
    return {
        "fractions": fractions,
        "format_used": "A_composition_string",
        "warnings": [],
    }


def _dopant_fraction_as_wt_and_at(element_symbol: str, concentration: float, unit: str):
    """
    Returns (kind, fraction) where kind is 'wt' or 'at' and fraction is 0-1.
    Unit must be one of: ppm, wt%, at% (per schema domain rules).
    """
    if concentration is None or unit is None:
        return None, None
    if unit == "ppm":
        return "wt", concentration / 1e6
    if unit == "wt%":
        return "wt", concentration / 100.0
    if unit == "at%":
        return "at", concentration / 100.0
    return None, None


def _from_discrete_fields(record: dict):
    comp_type = record.get("composition_type")
    warnings = []

    if comp_type == "pure":
        return {
            "fractions": {BASE_ELEMENT: 1.0},
            "format_used": "B_pure",
            "warnings": warnings,
        }

    if comp_type not in ("doped", "alloy"):
        return None

    dopants = []
    d1_el = record.get("dopant_or_alloying_element")
    if d1_el:
        kind, frac = _dopant_fraction_as_wt_and_at(
            d1_el, record.get("dopant_concentration"), record.get("dopant_concentration_unit")
        )
        if frac is not None:
            dopants.append((d1_el, kind, frac))
        else:
            warnings.append(f"dopant_or_alloying_element='{d1_el}' present but concentration/unit missing")

    d2_el = record.get("second_dopant_element")
    if d2_el:
        kind, frac = _dopant_fraction_as_wt_and_at(
            d2_el, record.get("second_dopant_concentration"), record.get("second_dopant_concentration_unit")
        )
        if frac is not None:
            dopants.append((d2_el, kind, frac))
        else:
            warnings.append(f"second_dopant_element='{d2_el}' present but concentration/unit missing")

    if not dopants:
        return None

    # Elements not recognised by mendeleev (e.g. compound dopants like "La2O3")
    # can't be placed in a mole-fraction composition. Flag and drop them.
    clean_dopants = []
    for sym, kind, frac in dopants:
        try:
            mdlv_element(sym)
            clean_dopants.append((sym, kind, frac))
        except Exception:
            warnings.append(f"element '{sym}' not resolvable via mendeleev (likely a compound, e.g. oxide) — dropped from composition")

    if not clean_dopants:
        return None

    wt_kinds = [d for d in clean_dopants if d[1] == "wt"]
    at_kinds = [d for d in clean_dopants if d[1] == "at"]

    if at_kinds and wt_kinds:
        warnings.append("dopants reported in mixed units (wt% and at%) — converting at% dopants to wt% via atomic weight before combining; treat as approximate")
        for sym, kind, frac in at_kinds:
            # Approximate: convert a single at% dopant to wt% assuming balance is base element.
            at_dict = {sym: frac, BASE_ELEMENT: 1 - frac}
            aw = _atomic_weight(sym)
            aw_base = _atomic_weight(BASE_ELEMENT)
            mass_dop = frac * aw
            mass_base = (1 - frac) * aw_base
            wt_frac = mass_dop / (mass_dop + mass_base)
            wt_kinds.append((sym, "wt", wt_frac))
        at_kinds = []

    if wt_kinds:
        wt_fractions = {sym: frac for sym, _, frac in wt_kinds}
        base_wt = 1.0 - sum(wt_fractions.values())
        if base_wt < 0:
            warnings.append("dopant wt fractions sum to >100% — composition invalid, clamping base to 0")
            base_wt = 0.0
        wt_fractions[BASE_ELEMENT] = base_wt
        at_fractions = wt_to_at_fractions(wt_fractions)
    else:
        at_fractions = {sym: frac for sym, _, frac in at_kinds}
        base_at = 1.0 - sum(at_fractions.values())
        if base_at < 0:
            warnings.append("dopant at fractions sum to >100% — composition invalid, clamping base to 0")
            base_at = 0.0
        at_fractions[BASE_ELEMENT] = base_at

    return {
        "fractions": at_fractions,
        "format_used": "B_discrete_fields",
        "warnings": warnings,
    }


def build_composition(record: dict):
    """
    Build a clean atomic-fraction composition dict from an extracted record.

    Returns a dict:
      {
        "fractions": {"Cu": 0.97, "Zn": 0.03},   # atomic (mole) fractions, sum to 1.0
        "format_used": "A_composition_string" | "B_pure" | "B_discrete_fields",
        "warnings": [str, ...],
      }
    or None if no composition could be built (record too sparse).
    """
    comp_str = record.get("composition_string")
    if comp_str:
        result = _from_composition_string(comp_str)
        if result["fractions"] is not None:
            return result
        # composition_string was present but unparseable — fall back to
        # discrete fields, carrying the parse-failure warning along.
        fallback = _from_discrete_fields(record)
        if fallback:
            fallback["warnings"] = result["warnings"] + fallback["warnings"]
            return fallback
        return None

    return _from_discrete_fields(record)
