"""
mp_features.py
===============
Queries the Materials Project for elastic/thermodynamic properties of a
composition, with disk caching and transparency about phase selection.

Requires MP_API_KEY in .env. If missing, or if mp_api isn't installed,
every mp_-prefixed feature is returned as None and mp_query_skipped
explains why — the pipeline degrades gracefully rather than failing.

Two endpoints are used (nothing is computed here):
  - summary   : selection (energy_above_hull) + thermodynamic/structural
                fields (formation energy, symmetry, density, efermi,
                work function, surface energy).
  - elasticity: elastic fields for the chosen material_id — bulk/shear
                (voigt/reuss/vrh dicts), young_modulus, homogeneous_poisson,
                universal_anisotropy, sound_velocity, debye_temperature,
                thermal_conductivity.

Field caveats:
  - young_modulus is often None in MP (not populated); left null, not derived.
  - thermal_conductivity here is the Clarke lattice (minimum) estimate from
    elastic constants — for a metal like Cu this is the phonon-only part, NOT
    the real electron-dominated conductivity. Source real k from the literature.

Phase selection:
  - Single-element systems: the ground state is energy_above_hull == 0.0 exactly.
  - Multi-element systems: keep entries with energy_above_hull <= 0.1 eV/atom.
  - Trace elements below MP_ELEMENT_FRACTION_THRESHOLD (1% atomic) are dropped.
"""

import os
import json
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

CACHE_DIR = Path(__file__).resolve().parent.parent / "mp_cache"
HULL_THRESHOLD_EV_ATOM = 0.1
GROUND_STATE_EV_ATOM_TOLERANCE = 1e-6
MP_ELEMENT_FRACTION_THRESHOLD = 0.01

_EMPTY_FEATURES = {
    "mp_material_id": None,
    "mp_formula": None,
    "mp_bulk_modulus_GPa": None,
    "mp_shear_modulus_GPa": None,
    "mp_young_modulus_GPa": None,
    "mp_homogeneous_poisson": None,
    "mp_universal_anisotropy": None,
    "mp_sound_velocity_m_s": None,
    "mp_debye_temperature_K": None,
    "mp_thermal_conductivity_clarke_W_mK": None,
    "mp_formation_energy_eV_atom": None,
    "mp_energy_above_hull_eV_atom": None,
    "mp_density_g_cm3": None,
    "mp_crystal_system": None,
    "mp_spacegroup_symbol": None,
    "mp_is_stable": None,
    "mp_efermi_eV": None,
    "mp_weighted_work_function_eV": None,
    "mp_weighted_surface_energy_J_m2": None,
    "mp_surface_anisotropy": None,
    "mp_all_phases_found": None,
    "mp_query_skipped": None,
}


def skipped_mp_features(reason: str) -> dict:
    """Public helper: the all-None mp_ feature shape with a given skip reason."""
    out = dict(_EMPTY_FEATURES)
    out["mp_query_skipped"] = reason
    return out


def _reduced_formula(fractions: dict) -> str:
    return "".join(f"{el}{round(frac, 4)}" for el, frac in sorted(fractions.items()))


def _cache_path(name: str) -> Path:
    safe = name.replace("/", "_")
    return CACHE_DIR / f"{safe}.json"


def _query_summary(elements: list, api_key: str) -> list:
    from mp_api.client import MPRester
    with MPRester(api_key) as mpr:
        docs = mpr.materials.summary.search(
            elements=elements,
            num_elements=len(elements),
            fields=[
                "material_id", "formula_pretty", "symmetry",
                "formation_energy_per_atom", "energy_above_hull",
                "is_stable", "efermi", "density",
                "weighted_work_function", "weighted_surface_energy",
                "surface_anisotropy",
            ],
        )
    return [d.dict() if hasattr(d, "dict") else dict(d) for d in docs]


def _query_elasticity(material_id: str, api_key: str) -> dict:
    """Full elasticity doc for one material_id, or {} if MP has none."""
    from mp_api.client import MPRester
    with MPRester(api_key) as mpr:
        docs = mpr.materials.elasticity.search(material_ids=[material_id])
    if not docs:
        return {}
    d = docs[0]
    return d.model_dump() if hasattr(d, "model_dump") else dict(d)


def _e_hull(d: dict) -> float:
    """energy_above_hull, missing -> large sentinel. Never use `or`: 0.0 is falsy."""
    v = d.get("energy_above_hull")
    return v if v is not None else 1e6


def _select_phase(docs: list, n_elements: int) -> tuple:
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


def _elastic_scalar(ela: dict, key: str):
    """bulk/shear are voigt/reuss/vrh dicts; return the VRH average."""
    v = ela.get(key)
    return v.get("vrh") if isinstance(v, dict) else v


def compute_mp_features(fractions: dict) -> dict:
    """
    fractions: {"Cu": 0.97, "Zn": 0.03} atomic fractions, from composition_builder.
    Returns a flat dict of mp_-prefixed features, cached to mp_cache/.
    """
    api_key = os.getenv("MP_API_KEY")
    if not api_key:
        return skipped_mp_features("MP_API_KEY not set in .env")

    try:
        import mp_api  # noqa: F401
    except ImportError:
        return skipped_mp_features("mp_api package not installed (pip install mp-api)")

    query_fractions = {el: f for el, f in fractions.items() if f >= MP_ELEMENT_FRACTION_THRESHOLD}
    dropped = {el: f for el, f in fractions.items() if f < MP_ELEMENT_FRACTION_THRESHOLD}
    print(f"  mp_query_composition: {query_fractions}"
          + (f"  (dropped as trace, <{MP_ELEMENT_FRACTION_THRESHOLD:.0%}): {dropped}" if dropped else ""))

    if not query_fractions:
        return skipped_mp_features("all elements below MP_ELEMENT_FRACTION_THRESHOLD — nothing to query")

    formula = _reduced_formula(query_fractions)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # --- summary (cached by formula) ---
    summary_cache = _cache_path(formula)
    if summary_cache.exists():
        docs = json.loads(summary_cache.read_text(encoding="utf-8"))
    else:
        try:
            docs = _query_summary(list(query_fractions.keys()), api_key)
        except Exception as e:
            return skipped_mp_features(f"MP summary query failed: {e}")
        summary_cache.write_text(json.dumps(docs, default=str, indent=2), encoding="utf-8")

    if not docs:
        out = skipped_mp_features("no matching entries returned by Materials Project")
        out["mp_formula"] = formula
        out["mp_all_phases_found"] = False
        return out

    chosen, all_phases_found = _select_phase(docs, len(query_fractions))
    if chosen is None:
        out = skipped_mp_features("no usable phase after filtering")
        out["mp_formula"] = formula
        out["mp_all_phases_found"] = False
        return out

    # --- elasticity for the chosen material_id (cached by id; failures not cached) ---
    mid = chosen.get("material_id")
    ela = {}
    if mid:
        ela_cache = _cache_path(f"elasticity_{mid}")
        if ela_cache.exists():
            ela = json.loads(ela_cache.read_text(encoding="utf-8"))
        else:
            try:
                ela = _query_elasticity(mid, api_key)
                ela_cache.write_text(json.dumps(ela, default=str, indent=2), encoding="utf-8")
            except Exception as e:
                print(f"  mp elasticity query failed for {mid}: {e}")
                ela = {}

    sym = chosen.get("symmetry") or {}

    out = dict(_EMPTY_FEATURES)
    out.update({
        "mp_material_id": mid,
        "mp_formula": chosen.get("formula_pretty"),
        # elastic (direct from elasticity doc, nothing computed):
        "mp_bulk_modulus_GPa": _elastic_scalar(ela, "bulk_modulus"),
        "mp_shear_modulus_GPa": _elastic_scalar(ela, "shear_modulus"),
        "mp_young_modulus_GPa": ela.get("young_modulus"),
        "mp_homogeneous_poisson": ela.get("homogeneous_poisson"),
        "mp_universal_anisotropy": ela.get("universal_anisotropy"),
        "mp_sound_velocity_m_s": ela.get("sound_velocity"),
        "mp_debye_temperature_K": ela.get("debye_temperature"),
        # Clarke lattice (minimum) estimate — phonon-only, NOT the real metal k:
        "mp_thermal_conductivity_clarke_W_mK": ela.get("thermal_conductivity"),
        # thermodynamic / structural (from summary):
        "mp_formation_energy_eV_atom": chosen.get("formation_energy_per_atom"),
        "mp_energy_above_hull_eV_atom": chosen.get("energy_above_hull"),
        "mp_density_g_cm3": chosen.get("density"),
        "mp_crystal_system": sym.get("crystal_system") if isinstance(sym, dict) else None,
        "mp_spacegroup_symbol": sym.get("symbol") if isinstance(sym, dict) else None,
        "mp_is_stable": chosen.get("is_stable"),
        "mp_efermi_eV": chosen.get("efermi"),
        "mp_weighted_work_function_eV": chosen.get("weighted_work_function"),
        "mp_weighted_surface_energy_J_m2": chosen.get("weighted_surface_energy"),
        "mp_surface_anisotropy": chosen.get("surface_anisotropy"),
        "mp_all_phases_found": all_phases_found,
        "mp_query_skipped": None,
    })
    return out