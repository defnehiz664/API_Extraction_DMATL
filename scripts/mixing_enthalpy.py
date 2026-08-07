"""
mixing_enthalpy.py
==================
Binary mixing enthalpies ΔH_mix (kJ/mol, Miedema model) for every pair among a
set of elements, generated from matminer's tabulation of:

    Takeuchi, A. & Inoue, A. (2005), "Classification of Bulk Metallic Glasses by
    Atomic Size Difference, Heat of Mixing and Period of Constituent Elements and
    Its Application to Characterization of the Main Alloying Element",
    Materials Transactions 46(12), 2817-2829.

    matminer is trusted as the source; we do not re-audit its numbers against the
    paper. The element list comes from element_table.VEC_TABLE, the project's single
    source of truth for supported elements, so this matrix and the property table
    cannot drift apart.
 
    Because requirements.txt allows matminer to upgrade (matminer>=0.10.1), the one
    validation we keep is a golden-snapshot check: the full matrix is diffed against
    a file committed in the repo, so a data change in a newer matminer release is
    surfaced and reviewed instead of silently shifting everyone's features. This is a
    reproducibility lock, not a correctness proof. Bless an intended change with
    `python mixing_enthalpy.py --write-snapshot`.

Three views are exported:
    H_MIX_FULL    -- every pair, NaN where no Miedema value exists.
    ABSENT_PAIRS  -- ["A-B", ...] pairs with no value; explicit 'no data' signal.
    H_MIX_USABLE  -- H_MIX_FULL minus the NaN entries; safe for arithmetic and for
                     consumers that detect a gap with `.get(key) is None`.
"""

from __future__ import annotations

import math
import sys
from importlib.metadata import PackageNotFoundError, version
from itertools import combinations

from click import Path

from matminer.utils.data import MixingEnthalpy
from pymatgen.core import Element
from element_table import VEC_TABLE

# --- configuration ---------------------------------------------------------
ELEMENTS = sorted(VEC_TABLE)      # single source of truth for supported elements
REFERENCE_SNAPSHOT = Path(__file__).resolve().parent / "mixing_enthalpy_reference.tsv"
_TOL = 1e-9


# impute_nan=False is required. The default (True) replaces absent pairs with the
# dataset mean (~-11 kJ/mol), silently fabricating values.
_MIXING = MixingEnthalpy(impute_nan=False)


def build_pairwise_matrix(elements):
    """Return (full, absent) over all unordered pairs of `elements`.

    full   -- {frozenset({A, B}): ΔH_mix}, NaN where no Miedema value exists.
    absent -- ["A-B", ...] pairs with no value.
    """
    full, absent = {}, []
    for a, b in combinations(elements, 2):
        val = _MIXING.get_mixing_enthalpy(Element(a), Element(b))
        full[frozenset((a, b))] = val
        if math.isnan(val):
            absent.append(f"{a}-{b}")
    return full, absent


H_MIX_FULL, ABSENT_PAIRS = build_pairwise_matrix(ELEMENTS)
H_MIX_USABLE = {pair: v for pair, v in H_MIX_FULL.items() if not math.isnan(v)}


def hmix(a, b):
    """ΔH_mix for one pair; raises KeyError if no Miedema value exists."""
    val = H_MIX_FULL.get(frozenset((a, b)))
    if val is None or math.isnan(val):
        raise KeyError(f"No Takeuchi-Inoue mixing enthalpy for {a}-{b}")
    return val


def _label(pair):
    """frozenset({'A','B'}) -> 'A-B' with a stable order, for readable reports."""
    return "-".join(sorted(pair))


# --- up-to-date check: full matrix vs a committed golden snapshot -----------
# verify_against_snapshot() compares the freshly generated matrix to the
# committed snapshot file. If a pair changed, was added, or was removed,
# the check fails.
#
# If the change is intentional, regenerate and bless the snapshot with:
#     python mixing_enthalpy.py --write-snapshot
#
# If the change is unexpected, investigate the cause before updating the snapshot.

def _load_snapshot(path=REFERENCE_SNAPSHOT):
    """Read the golden snapshot into {frozenset({A,B}): value|nan}. '#' lines skipped."""
    snap = {}
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("elem_A"):
                continue
            a, b, raw = line.split("\t")
            snap[frozenset((a, b))] = math.nan if raw == "nan" else float(raw)
    return snap
 
 
def _values_match(x, y):
    """NaN-aware equality: two NaNs count as equal; otherwise compare within tolerance."""
    if math.isnan(x) and math.isnan(y):
        return True
    if math.isnan(x) or math.isnan(y):
        return False
    return math.isclose(x, y, abs_tol=_TOL)
 
 
def verify_against_snapshot(path=REFERENCE_SNAPSHOT):
    """Compare the full generated matrix to the committed snapshot.
 
    Returns {"changed": {"A-B": (got, expected)}, "added": [...], "removed": [...]}.
    'added'/'removed' cover pairs that exist on only one side, e.g. after an element
    is added to VEC_TABLE or matminer starts/stops covering a pair. Empty dicts and
    lists mean the matrix is identical to the blessed snapshot.
    """
    snap = _load_snapshot(path)
    changed, added = {}, []
    for pair, got in H_MIX_FULL.items():
        if pair not in snap:
            added.append(_label(pair))
        elif not _values_match(got, snap[pair]):
            changed[_label(pair)] = (got, snap[pair])
    removed = [_label(p) for p in snap if p not in H_MIX_FULL]
    return {"changed": changed, "added": sorted(added), "removed": sorted(removed)}
 
 
def write_snapshot(path=REFERENCE_SNAPSHOT):
    """(Re)write the golden snapshot from the current matrix. Run deliberately."""
    header = [
        "# Golden snapshot of the full pairwise Miedema mixing-enthalpy matrix.",
        f"# Source: matminer {matminer_version()} (Takeuchi & Inoue 2005), impute_nan=False.",
        "# Regenerate deliberately with:  python mixing_enthalpy.py --write-snapshot",
        "# 'nan' = no Miedema value for that pair (e.g. anything with O, S, Te).",
        "elem_A\telem_B\tdelta_Hf",
    ]
    rows = []
    for pair in sorted(H_MIX_FULL, key=_label):
        a, b = sorted(pair)
        v = H_MIX_FULL[pair]
        rows.append(f"{a}\t{b}\t{'nan' if math.isnan(v) else repr(v)}")
    Path(path).write_text("\n".join(header + rows) + "\n")
    return len(rows)
 
 
def matminer_version():
    try:
        return version("matminer")
    except PackageNotFoundError:
        return "unknown"
 
 
if __name__ == "__main__":
    if "--write-snapshot" in sys.argv:
        n = write_snapshot()
        print(f"# wrote {n} pairs to {REFERENCE_SNAPSHOT.name} (matminer {matminer_version()})")
        sys.exit(0)
 
    print(f"# matminer = {matminer_version()}   elements = {len(ELEMENTS)}")
    print(f"# {len(H_MIX_USABLE)} usable pairs   {len(ABSENT_PAIRS)} absent")
 
    diff = verify_against_snapshot()
    if any(diff.values()):
        print("# FAIL: full matrix differs from the committed snapshot:")
        for pair, (got, expected) in diff["changed"].items():
            print(f"#   changed {pair}: got {got}, snapshot {expected}")
        if diff["added"]:
            print(f"#   added (not in snapshot): {diff['added']}")
        if diff["removed"]:
            print(f"#   removed (only in snapshot): {diff['removed']}")
        print("#   -> if this change is expected (e.g. a matminer upgrade), rerun")
        print("#      with --write-snapshot to bless it")
        sys.exit(1)
    print(f"# OK: all {len(H_MIX_FULL)} pairs match the committed snapshot")