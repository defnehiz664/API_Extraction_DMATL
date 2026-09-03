"""
identity.py — deterministic record identity for the extraction pipeline.

A record_id must be identical across runs for the same physical data point, so it
is built from the fields that DEFINE the record (its coordinates), never from an
order-based counter or a raw free-text name the model rewords between runs.

Priority:
  1. a specimen label the paper itself provides  (record['specimen_id']) — the
     hardest anchor; use it verbatim when present.
  2. otherwise a composite of canonicalized distinguishing fields:
     slug(DOI) __ canon(material) [__ slug(condition) __ slug(test_point) ...]

canon_material() removes run-to-run wording drift (case, spacing, punctuation,
parentheticals, stopwords) and then maps known aliases to one canonical token, so
the same material yields the same string every run.
"""

import json
import re
from collections import defaultdict

# Aliases keyed in the COLLAPSED form _mech() produces (see below). Extend as new
# equivalents show up; this is the controlled-vocabulary tier.
_ALIASES = {
    "cunisi": "cunisi",
    "cuni2si": "cunisi",          # "CuNi2Si" and "Cu-Ni-Si" are the same material
    "cucrzr": "cucrzr",
    "cu069cr007zr": "cucrzr",     # "Cu-0.69Cr-0.07Zr" == "Cu-Cr-Zr"
}

_STOPWORDS = {"alloy", "sample", "specimen", "material", "the"}


def _mech(text):
    """Collapse to a single alphanumeric token: lowercase, drop parentheticals and
    stopwords, remove every separator so spacing can't split a token. This unifies
    'Cu-5 at.% Al' and 'Cu-5at.% Al'."""
    s = str(text or "").lower()
    s = re.sub(r"\([^)]*\)", " ", s)            # drop (CW111C), (No Hold), ...
    s = re.sub(r"[^a-z0-9]+", " ", s)           # every non-alnum -> space
    s = " ".join(w for w in s.split() if w not in _STOPWORDS)
    s = re.sub(r"\s+", "", s)                    # collapse to one token
    return s or "unknown"


def canon_material(name):
    """Canonical material token: mechanical collapse, then alias lookup."""
    m = _mech(name)
    return _ALIASES.get(m, m)


def _slug(text, maxlen=40):
    return re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")[:maxlen] or "na"


# Fields that DEFINE a record's identity (its coordinates). A different LCF test
# temperature is a physically different test, so it must split the id. The '::'
# form means "find this leaf ANYWHERE inside that block": with response_schema off
# the model drops the conditions/specimen/results grouping and emits temperature as
# lcf.test_temperature_K (flat) on some runs and lcf.conditions.test_temperature_K
# (nested) on others, so a fixed path would silently miss it. Scoping to the lcf
# block avoids grabbing hardness.test_temperature_K (the hardness rig's own temp).
# Strain amplitude is deliberately NOT here — a single LCF test sweeps many strain
# amplitudes by nature, and those live as rows in lcf.results.data_points (a list),
# so they UNION on merge instead of forcing a new record or a conflict.
DEFAULT_IDENTITY_FIELDS = ("material_condition", "lcf::test_temperature_K")


def _deep_find(obj, leaf):
    """First SCALAR value under obj whose key equals `leaf` (case-insensitive),
    searched depth-first through dicts and dicts inside lists. Direct keys win over
    deeper ones, so a flat lcf.test_temperature_K is found before recursing."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() == leaf.lower() and not isinstance(v, (dict, list)):
                return v
        for v in obj.values():
            found = _deep_find(v, leaf)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _deep_find(v, leaf)
            if found is not None:
                return found
    return None


def _resolve_field(record, path):
    """Resolve an identity field, three path forms, all case-insensitive on keys:
      'material_condition'                 -> a top-level key
      'lcf.conditions.test_temperature_K'  -> strict walk down named keys
      'lcf::test_temperature_K'            -> resolve the block on the left, then
                                              find the leaf anywhere inside it (flat
                                              or nested), tolerating shape drift."""
    if "::" in path:
        block, leaf = path.split("::", 1)
        return _deep_find(_resolve_field(record, block), leaf)
    cur = record
    for seg in path.split("."):
        if not isinstance(cur, dict):
            return None
        hit = next((k for k in cur if k.lower() == seg.lower()), None)
        if hit is None:
            return None
        cur = cur[hit]
    return cur


def _id_val(v):
    """Normalize a value used in an id so it's stable run-to-run: an integral float
    (573.0) and its int (573) must slug identically, or the id would flip runs."""
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def make_record_id(record, doi=None, identity_fields=DEFAULT_IDENTITY_FIELDS):
    """Deterministic id for one record. specimen_id short-circuits everything;
    otherwise compose DOI + canonical material + any non-empty identity fields, each
    resolved through _resolve_field so nested coordinates (LCF temperature) count."""
    doi_slug = _slug(doi or record.get("source_DOI"), 80)

    specimen = record.get("specimen_id")
    if specimen not in (None, ""):
        return f"{doi_slug}__{_slug(specimen)}"

    parts = [doi_slug, canon_material(record.get("material_name"))]
    for f in identity_fields:
        v = _resolve_field(record, f)
        if v not in (None, ""):
            parts.append(_slug(_id_val(v)))
    return "__".join(parts)


def assign_record_ids(records, doi=None, identity_fields=DEFAULT_IDENTITY_FIELDS):
    """Stamp record_id on every record in-place. Records that collide (identical by
    their identity fields) are ordered by CONTENT, not emission order, so the suffix
    is stable across runs; collisions are returned so the caller can warn — a
    collision means the records aren't distinguishable by their fields, which is a
    granularity/field problem, not an id-scheme problem."""
    keyed = [(r, make_record_id(r, doi, identity_fields)) for r in records]
    groups = defaultdict(list)
    for r, rid in keyed:
        groups[rid].append(r)

    collisions = {}
    for rid, group in groups.items():
        if len(group) == 1:
            group[0]["record_id"] = rid
            continue
        ordered = sorted(group, key=lambda r: json.dumps(r, sort_keys=True, default=str))
        for i, r in enumerate(ordered, 1):
            r["record_id"] = f"{rid}__{i}"
        collisions[rid] = len(group)
    return collisions


def _merge_dict(a, b, prefix, conflicts):
    """Merge dict b into dict a in place: fill a's missing/empty values from b,
    union lists, recurse into nested dicts so conflicts INSIDE blocks (hardness,
    tensile, ...) are caught rather than silently resolved. Each disagreement is
    appended to `conflicts` with a dotted path, e.g. 'hardness.hv'."""
    for k, v in b.items():
        if v is None or v == "":
            continue
        cur = a.get(k)
        if k not in a or cur in (None, ""):
            a[k] = v
        elif isinstance(cur, dict) and isinstance(v, dict):
            _merge_dict(cur, v, f"{prefix}{k}.", conflicts)
        elif isinstance(cur, list) and isinstance(v, list):
            a[k] = cur + v
        elif cur != v:
            conflicts.append((f"{prefix}{k}", cur, v))


def _merge_into(a, b):
    """Merge record b into record a. Missing/empty fields fill from b, lists union,
    nested blocks merge recursively; every conflicting value (top level OR inside a
    block) lands in a['_merge_conflicts'] with a dotted path, so nothing is
    silently overwritten."""
    conflicts = a.setdefault("_merge_conflicts", [])
    _merge_dict(a, b, "", conflicts)
    return a


def merge_records(records, doi=None, identity_fields=DEFAULT_IDENTITY_FIELDS):
    """Within one paper, combine records that share the same identity key into one.
    On this data that means collapsing the same specimen the model emitted twice;
    distinct specimens (different specimen_id) never merge. Every value the two
    copies disagreed on is recorded under _merge_conflicts for your review.
    Returns (merged_records, n_merged)."""
    groups = {}
    order = []
    for r in records:
        key = make_record_id(r, doi, identity_fields)
        if key in groups:
            _merge_into(groups[key], r)
        else:
            groups[key] = dict(r)
            order.append(key)
    merged = [groups[k] for k in order]
    for r in merged:                             # drop the empty conflict list on clean merges
        if not r.get("_merge_conflicts"):
            r.pop("_merge_conflicts", None)
    return merged, len(records) - len(merged)


def _dict_has_data(d):
    for v in d.values():
        if isinstance(v, dict):
            if _dict_has_data(v):
                return True
        elif isinstance(v, list):
            if any(x not in (None, "") for x in v):
                return True
        elif v not in (None, ""):
            return True
    return False


# Real measured content. A record is a phantom (e.g. HT1, a route only mentioned)
# unless at least one of these is populated. SEM/EBSD/XRD/TEM are gone from the
# schema so they are NOT listed. Microstructure is kept as FLAT fields, so those
# are listed here too — otherwise a grain-size-only record (no mechanical test)
# would be wrongly dropped. Update these two sets if the schema fields change.
MEASUREMENT_BLOCKS = {"lcf", "tensile", "hardness"}
MEASUREMENT_FIELDS = {"grain_size_um", "dislocation_density_m2",
                      "crystallographic_texture", "dislocation_structure_description"}


def _has_measurement(rec):
    """True if the record carries real measured content: a populated test block OR a
    populated flat microstructure field. Identity/composition/provenance scalars do
    NOT count, so a route only named (no data) is a phantom."""
    for k, v in rec.items():
        kl = k.lower()
        if kl in MEASUREMENT_BLOCKS:
            if isinstance(v, dict) and _dict_has_data(v):
                return True
            if isinstance(v, list) and any(isinstance(x, dict) and x for x in v):
                return True
        elif kl in MEASUREMENT_FIELDS and v not in (None, ""):
            return True
    return False


def drop_empty_records(records):
    """Remove records that report no measured data (a mentioned-but-unmeasured
    route like HT1). Returns (kept, dropped) so the caller can log exactly what
    went, never a silent drop."""
    kept, dropped = [], []
    for r in records:
        (kept if _has_measurement(r) else dropped).append(r)
    return kept, dropped