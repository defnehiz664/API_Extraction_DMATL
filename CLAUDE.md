# Tungsten DBTT Extraction Pipeline — Claude Reference

## What This Project Does
Automated pipeline that reads scientific papers (Elsevier XML + figures) and extracts structured materials science data using Gemini. Output is structured JSON reviewed by the researcher, then entered into an Excel dataset (Sheet2) for ML model training.

**Subject matter:** Tungsten ductile-to-brittle transition temperature (DBTT), grain size/morphology, dislocation density, mechanical properties.

**Stakes:** Data accuracy is mission-critical. Never guess values — use null. Always explain planned changes before making them.

---

## Google Cloud / Vertex AI Setup

| Setting | Value |
|---|---|
| ETH email | dabiakar@ethz.ch (NOT the student address) |
| Project ID | matmodel-literaturemining-govc |
| Location | europe-west4 (EU data residency required by ETH) |
| Role | roles/aiplatform.user |

**Auth (run once per machine):**
```
gcloud auth application-default login
# log in with dabiakar@ethz.ch
```

**Client instantiation (all scripts must use this — no personal API key):**
```python
client = genai.Client(
    vertexai=True,
    project="matmodel-literaturemining-govc",
    location="europe-west4",
)
```

**Model to use:** `gemini-2.5-flash` (upgraded from 2.0-flash for better extraction accuracy)

---

## Elsevier API

- Key in `.env`: `ELSEVIER_API_KEY`
- Requires ETH VPN for full-text access (without VPN you get abstract-only 200 responses)
- All Elsevier DOIs start with `10.1016/`
- Non-Elsevier papers: fallback to Unpaywall PDF route

---

## Script Responsibilities

| Script | Purpose |
|---|---|
| `scripts/fetch_papers.py` | DOI → Elsevier API → full-text XML + supplementary files |
| `scripts/extract_data.py` | XML + figures → Gemini → structured JSON |
| `scripts/ElSevier_test.py` | Old test file — superceded by fetch_papers.py, kept for reference |

---

## Elsevier XML Structure (important for parsing)

The full-text XML root element is `<full-text-retrieval-response>` with default namespace:
`http://www.elsevier.com/xml/svapi/article/dtd`

Figure URLs live in:
```xml
<objects>
  <object ref="gr1" category="high" ...>https://api.elsevier.com/content/object/eid/...</object>
</objects>
```
- `ref`: figure ID (gr1, gr2, ... — these are the main figures; skip ga1=graphical abstract, si*=SVG math)
- `category`: `high` = high-res JPEG (_lrg.jpg), `standard` = downsampled JPEG, `thumbnail` = small GIF
- Always prefer `high` category for Gemini

To query these in Python (ElementTree):
```python
SVAPI_NS = "http://www.elsevier.com/xml/svapi/article/dtd"
root.findall(f"{{{SVAPI_NS}}}objects/{{{SVAPI_NS}}}object")
```

Body text is in the `ja` namespace body:
```python
root.find(".//{http://www.elsevier.com/xml/ja/dtd}body")
```

---

## Structured Output (Pydantic)

`extract_data.py` uses `response_mime_type="application/json"` + `response_schema=list[MaterialRecord]` to force Gemini to return valid JSON matching the schema. This eliminates JSON parse failures.

The `MaterialRecord` Pydantic class in `extract_data.py` defines all 55 schema columns. All fields are `Optional` — never invent values.

---

## Dataset Schema (55 columns, Sheet2)

```
record_id, material_name, commercial_grade_name, composition_type,
logarithmic_strain, base_material_purity_pct, impurities,
impurity_concentration, dopant_or_alloying_element, dopant_concentration,
dopant_concentration_unit, second_dopant_element, second_dopant_concentration,
second_dopant_concentration_unit, grain_size_L_um, grain_size_T_um,
grain_size_S_um, grain_size_L_sd_um, grain_size_T_sd_um, grain_size_S_sd_um,
grain_size_estimated_dimensions, grain_aspect_ratio,
grain_morphology_qualitative, pre_existing_dislocation_density_m2,
dislocation_density_method, mobile_dislocation_fraction_pct, DBTT_K,
DBTT_uncertainty_K, DBTT_lower_K, DBBT_upper_K, DBTT_definition,
elastic_modulus_GPa, fracture_toughness_K_IC_MPa_m05,
charpy_upper_shelf_energy_J, UTS_MPa, yield_strength_MPa,
fracture_strength_MPa, total_elongation_pct, mechanical_property_test_temp_K,
hardness_HV, hardness_HV_error_plus_or_minus, hardness_HV_test_load_kgf,
hardness_HV_direction, recrystallization_temp_K, measurement_method,
loading_direction_relative_to_grain, notch_fabrication_method,
loading_rate_or_strain_rate, processing_history, specimen_form,
specimen_thickness_mm, specimen_diameter_mm, degree_of_cold_rolling,
source_DOI, source_figure_or_table, notes
```

**Extraction rules:**
- `composition_type`: exactly one of `pure`, `doped`, `alloy`
- DBTT range → midpoint = `DBTT_K`, half-range = `DBTT_uncertainty_K`
- `source_DOI`: always the paper's DOI string
- Record IDs: W_001 through W_029 convention

---

## Papers Fetched

| DOI | Status | File |
|---|---|---|
| 10.1016/j.jmst.2026.01.050 | full_text | 10-1016_j-jmst-2026-01-050.xml |
| 10.1016/j.ijrmhm.2018.09.010 | full_text | 10-1016_j-ijrmhm-2018-09-010.xml |

Note: the second paper is not in `fetch_log.json` — it was fetched separately.

---

## Collaboration Notes

- Always explain what you plan to change and why before making edits
- Never guess or infer data values — `null` is always correct when uncertain
- The researcher is the domain expert on materials science; defer to them on schema decisions
