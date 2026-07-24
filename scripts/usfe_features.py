"""
usfe_features.py
=================
Estimates unstable stacking fault energy (USFE) for a composition, since
USFE is essentially never reported directly in W-DBTT papers but is a
known correlate of DBTT via dislocation mobility.

Only estimate computed: a composition-weighted (linear mixture) average
of pure-element USFE values on the {110}<111> slip system, from Table 1
of Zhang et al. (2023), "Unstable stacking fault energies of BCC metals",
J. Mater. Res. Technol., DOI: 10.1016/j.jmrt.2022.12.162.

An empirical formula relating USFE to elastic constants and lattice
parameter (Zhang et al. 2022) was previously also computed here, but was
removed: the unit scaling between GPa/Angstrom inputs and mJ/m^2 output
was never verified against the source and produced results off by a
factor of ~44-50x from known values (e.g. ~88,600 instead of ~1786 for
pure W). Do not re-add it without verifying the exact formula and units
against the primary source first.
"""

# Elemental USFE on the {110}<111> slip system, mJ/m^2. Zhang et al.
# (2023), Table 1, DOI: 10.1016/j.jmrt.2022.12.162. Do not extend this
# table with values from other sources/slip systems without a citation —
# mixing slip systems or DFT methodologies defeats the point of using a
# single consistent reference table.
USFE_110 = {
    "Li": 71,  "Na": 48,  "K": 26,
    "Ti": 190, "V": 713,  "Cr": 1549,
    "Fe": 980, "Nb": 674, "Mo": 1425,
    "Ta": 725, "W": 1786,
    # Re: not reported in Zhang et al. 2023 — deliberately absent, not 0.
}

# Elements present above this atomic fraction but missing from USFE_110
# invalidate the whole linear-mixture estimate (it would be silently
# ignoring a significant fraction of the composition). Below this
# threshold, a missing element is noted but doesn't null the result.
USFE_SIGNIFICANCE_THRESHOLD = 0.01


def _linear_mixture_usfe(fractions: dict) -> tuple:
    missing_elements = [el for el in fractions if el not in USFE_110]
    significant_missing = [el for el in missing_elements if fractions[el] > USFE_SIGNIFICANCE_THRESHOLD]

    if significant_missing:
        return None, missing_elements

    known = {el: f for el, f in fractions.items() if el in USFE_110}
    if not known:
        return None, missing_elements

    total_known_frac = sum(known.values())
    value = sum(f * USFE_110[el] for el, f in known.items()) / total_known_frac
    return value, missing_elements


def compute_usfe_features(fractions: dict, mp_features: dict = None, mendeleev_features: dict = None) -> dict:
    """
    fractions: {"W": 0.97, "K": 0.03} atomic fractions.
    mp_features, mendeleev_features: accepted for call-signature compatibility
        with pipeline.py, not currently used by this module.

    Returns a flat dict: usfe_linear_mixture_mJ_m2, usfe_missing_elements
    (only present if non-empty), usfe_approximation_note.
    """
    linear_value, missing_elements = _linear_mixture_usfe(fractions)

    out = {
        "usfe_linear_mixture_mJ_m2": linear_value,
        "usfe_approximation_note": (
            "Composition-weighted average of pure element USFE values from "
            "Zhang et al. (2023) Table 1, {110}<111> slip system. Not a DFT "
            "calculation for this specific alloy."
        ),
    }
    if missing_elements:
        out["usfe_missing_elements"] = missing_elements

    return out
