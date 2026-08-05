"""
mp_features.py
===============
Queries the Materials Project for elastic/thermodynamic properties of a
composition, with disk caching and full transparency about assumptions
made during phase selection.

Requires MP_API_KEY in .env. If missing, or if mp_api isn't installed,
every mp_-prefixed feature is returned as None and mp_query_skipped
explains why — the pipeline degrades gracefully rather than failing.

Phase selection logic:
  - Single-element (pure) systems: select the entry with
    energy_above_hull == 0.0 exactly — the defined ground state — rather
    than "lowest energy_above_hull among near-hull candidates". That
    phrasing is for choosing among competing near-degenerate multi-element
    phases; applied to an element's own allotropes it can pick a
    metastable polymorph (e.g. beta-W, Pm-3n) instead of the real ground
    state (alpha-W, Im-3m) if the metastable one happens to be the only
    "Cubic" entry within the hull window.
  - Multi-element systems: keep entries with energy_above_hull <= 0.1
    eV/atom (near-hull, since exact experimental phases are rarely the
    DFT ground state).
  - Trace elements below MP_ELEMENT_FRACTION_THRESHOLD (1% atomic) are
    dropped from the query composition entirely — MP has no entries for
    arbitrary dilute alloys like W + 80 wtppm K, so those systems are
    queried as their majority composition only.
"""

import os
import json
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

CACHE_DIR = Path(__file__).resolve().parent.parent / "mp_cache"
HULL_THRESHOLD_EV_ATOM = 0.1
GROUND_STATE_EV_ATOM_TOLERANCE = 1e-6

# Trace dopants below this atomic fraction are dropped from the MP query —
# MP has no entries for arbitrary dilute alloys (e.g. W + 80 wtppm K), and
# querying with a trace element as a required component either returns
# nothing or returns an irrelevant ordered compound. Below this threshold
# the system is queried as its majority composition only.
MP_ELEMENT_FRACTION_THRESHOLD = 0.01

_EMPTY_FEATURES = {
    "mp_material_id": None,
    "mp_formula": None,
    "mp_bulk_modulus_GPa": None,
    "mp_shear_modulus_GPa": None,
    "mp_formation_energy_eV_atom": None,
    "mp_energy_above_hull_eV_atom": None,
    "mp_crystal_system": None,
    "mp_spacegroup_symbol": None,
    "mp_is_stable": None,
    "mp_all_phases_found": None,
    "mp_query_skipped": None,
}


def skipped_mp_features(reason: str) -> dict:
    """Public helper: the all-None mp_ feature shape with a given skip reason.
    Used both internally and by pipeline.py for records that shouldn't be
    queried at all (e.g. out-of-scope alloy systems)."""
    out = dict(_EMPTY_FEATURES)
    out["mp_query_skipped"] = reason
    return out


def _reduced_formula(fractions: dict) -> str:
    return "".join(f"{el}{round(frac, 4)}" for el, frac in sorted(fractions.items()))


def _cache_path(formula: str) -> Path:
    safe = formula.replace("/", "_")
    return CACHE_DIR / f"{safe}.json"


def _query_materials_project(elements: list, api_key: str) -> list:
    from mp_api.client import MPRester

    with MPRester(api_key) as mpr:
        docs = mpr.materials.summary.search(
            elements=elements,
            num_elements=len(elements),
            fields=[
                "material_id", "formula_pretty", "symmetry",
                "formation_energy_per_atom", "energy_above_hull",
                "bulk_modulus", "shear_modulus", "is_stable",
            ],
        )
    return [d.dict() if hasattr(d, "dict") else dict(d) for d in docs]


def _e_hull(d: dict) -> float:
    """
    energy_above_hull, defaulting missing (None) values to a large sentinel
    so they sort last. NEVER use `d.get(...) or default` for this field —
    energy_above_hull == 0.0 (the ground state) is falsy in Python, so
    `0.0 or default` silently evaluates to `default` and throws away
    exactly the value that matters most.
    """
    v = d.get("energy_above_hull")
    return v if v is not None else 1e6


def _select_phase(docs: list, n_elements: int) -> dict:
    # Single-element (pure) system: the ground state is defined as
    # energy_above_hull == 0.0 exactly, by construction of the convex
    # hull. Select it explicitly rather than via "lowest e_above_hull" —
    # that phrasing is for competing near-degenerate multi-element phases,
    # not for picking among an element's own allotropes/polymorphs, some
    # of which can sit inside the 0.1 eV/atom window without being the
    # actual reference states.
    if n_elements == 1:
        ground_state = [d for d in docs if abs(_e_hull(d)) <= GROUND_STATE_EV_ATOM_TOLERANCE]
        if ground_state:
            # Should be unique; if MP has duplicate hull entries, prefer the stable one.
            chosen = min(ground_state, key=lambda d: 0 if d.get("is_stable") else 1)
            return chosen, True
       

    near_hull = [d for d in docs if _e_hull(d) <= HULL_THRESHOLD_EV_ATOM]
    all_phases_found = len(near_hull) > 0
    pool = near_hull if near_hull else docs

    if not pool:
        return None, False

    chosen = min(pool, key=lambda d: (_e_hull(d), 0 if d.get("is_stable") else 1))
    return chosen, all_phases_found



def compute_mp_features(fractions: dict) -> dict:
    """
    fractions: {"Cu": 0.97, "Zn": 0.03} atomic fractions, from composition_builder.
    Returns a flat dict of mp_-prefixed features. Cached to mp_cache/ by
    composition formula so repeat pipeline runs don't re-hit the API.
    """
    api_key = os.getenv("MP_API_KEY")
    if not api_key:
        out = dict(_EMPTY_FEATURES)
        out["mp_query_skipped"] = "MP_API_KEY not set in .env"
        return out

    try:
        import mp_api  # noqa: F401
    except ImportError:
        out = dict(_EMPTY_FEATURES)
        out["mp_query_skipped"] = "mp_api package not installed (pip install mp-api)"
        return out

    query_fractions = {el: f for el, f in fractions.items() if f >= MP_ELEMENT_FRACTION_THRESHOLD}
    dropped = {el: f for el, f in fractions.items() if f < MP_ELEMENT_FRACTION_THRESHOLD}
    print(f"  mp_query_composition: {query_fractions}"
          + (f"  (dropped as trace, <{MP_ELEMENT_FRACTION_THRESHOLD:.0%}): {dropped}" if dropped else ""))

    if not query_fractions:
        out = dict(_EMPTY_FEATURES)
        out["mp_query_skipped"] = "all elements fell below MP_ELEMENT_FRACTION_THRESHOLD — nothing to query"
        return out

    formula = _reduced_formula(query_fractions)
    cache_file = _cache_path(formula)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if cache_file.exists():
        docs = json.loads(cache_file.read_text(encoding="utf-8"))
    else:
        try:
            docs = _query_materials_project(list(query_fractions.keys()), api_key)
        except Exception as e:
            out = dict(_EMPTY_FEATURES)
            out["mp_query_skipped"] = f"MP query failed: {e}"
            return out
        cache_file.write_text(json.dumps(docs, default=str, indent=2), encoding="utf-8")

    if not docs:
        out = dict(_EMPTY_FEATURES)
        out["mp_formula"] = formula
        out["mp_all_phases_found"] = False
        out["mp_query_skipped"] = "no matching entries returned by Materials Project"
        return out

    chosen, all_phases_found = _select_phase(docs, len(query_fractions))
    if chosen is None:
        out = dict(_EMPTY_FEATURES)
        out["mp_formula"] = formula
        out["mp_all_phases_found"] = False
        out["mp_query_skipped"] = "no usable phase after filtering"
        return out

    bulk = chosen.get("bulk_modulus") or {}
    shear = chosen.get("shear_modulus") or {}
    sym = chosen.get("symmetry") or {}

    return {
        "mp_material_id": chosen.get("material_id"),
        "mp_formula": chosen.get("formula_pretty"),
        "mp_bulk_modulus_GPa": bulk.get("vrh") if isinstance(bulk, dict) else None,
        "mp_shear_modulus_GPa": shear.get("vrh") if isinstance(shear, dict) else None,
        "mp_formation_energy_eV_atom": chosen.get("formation_energy_per_atom"),
        "mp_energy_above_hull_eV_atom": chosen.get("energy_above_hull"),
        "mp_crystal_system": sym.get("crystal_system") if isinstance(sym, dict) else None,
        "mp_spacegroup_symbol": sym.get("symbol") if isinstance(sym, dict) else None,
        "mp_is_stable": chosen.get("is_stable"),
        "mp_all_phases_found": all_phases_found,
        "mp_query_skipped": None,
    }
