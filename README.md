#Extraction Pipeline

Automated pipeline for extracting structured materials science data from scientific papers using Google Gemini. Reads Elsevier full-text XML or PDFs, sends content to Gemini, and outputs structured JSON matching a dataset schema from a schema folder (.yaml) for manual review before entry into Excel.

**Research context:** ETH Zurich Materials Modeling Laboratory. The extracted data populates a dataset of ductile-to-brittle transition temperature (DBTT) measurements, microstructural parameters, and mechanical properties for future use in machine learning model training.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Prerequisites](#prerequisites)
3. [Setup](#setup)
4. [Workflow](#workflow)
5. [Script Reference](#script-reference)
6. [Dataset Schema](#dataset-schema)
7. [File Structure](#file-structure)
8. [Adding New Papers](#adding-new-papers)
9. [Troubleshooting](#troubleshooting)
10. [Sharing with Other Users](#sharing-with-other-users)

---

## Architecture Overview

```
DOI list
   │
   ▼
fetch_papers.py
   ├── Elsevier journals (10.1016/...) ──► Elsevier ScienceDirect API
   │                                           ► full-text XML + supplementary .docx
   └── Non-Elsevier ──────────────────► Unpaywall (open-access PDF)
                                            ► or manual PDF placement
   │
   ▼
data/papers/
   ├── {doi}.xml          ← Elsevier papers
   ├── {doi}_mmc1.docx    ← supplementary data tables
   └── {doi}.pdf          ← non-Elsevier papers
   │
   ▼
extract_data.py
   ├── (1) Saves all figures to  data/figures/{doi}/
   ├── (2) Classifies figures ──► Gemini (classify call)
   │         ├── data/figures/{doi}/microstructure/
   │         ├── data/figures/{doi}/DBTT_curve/
   │         ├── data/figures/{doi}/hardness/
   │         ├── data/figures/{doi}/EBSD/
   │         ├── data/figures/{doi}/fracture_surface/
   │         ├── data/figures/{doi}/map/
   │         └── data/figures/{doi}/other/
   └── (3) Extracts data ──────► Gemini (extraction call)
   │
   ▼
data/outputs/{doi}_extraction.json
   (contains records + figure_classifications)
   │
   ▼
Human review  ──►  Excel dataset (Sheet2)
```

Two input routes are supported transparently:
- **XML route (Elsevier):** body text extracted from XML + figures downloaded via Elsevier API + supplementary .docx parsed. Gemini receives structured text and high-resolution figure images.
- **PDF route (non-Elsevier):** each PDF page is rendered as a PNG image at 200 DPI. Gemini receives the pages as images, seeing figures, tables, and layout exactly as they appear.

---

## Prerequisites

### Software

- Python 3.11 or later
- Google Cloud SDK (`gcloud` CLI) — [install here](https://cloud.google.com/sdk/docs/install)

### Accounts and Access

| What | Details |
|---|---|
| ETH Google Cloud project | `matmodel-literaturemining-govc` |
| Role required | `roles/aiplatform.user` (ask lab admin if not granted) |
| Elsevier API key | In `.env` as `ELSEVIER_API_KEY` |
| ETH VPN | Required for Elsevier full-text and some PDF downloads |


## Setup

### 1. Clone the repository

```bash
git clone <repo-url>
cd API_Extraction_D-MATL
```

### 2. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 3. Create your `.env` file

Copy the template below and save it as `.env` in the project root. **Never commit this file to git. Always kept in .gitignore**

```env
ELSEVIER_API_KEY=your_elsevier_api_key_here
GOOGLE_CLOUD_PROJECT=matmodel-literaturemining-govc
GOOGLE_CLOUD_LOCATION=europe-west4
GOOGLE_GENAI_USE_ENTERPRISE=True
```

The Elsevier API key is shared per ETH subscription, you need to ask the lab admin for it.

### 4. Authenticate with Google Cloud

Run this once per machine and a browser window will open.

```bash
gcloud auth application-default login
# Log in with dabiakar@ethz.ch (NOT the student address)

gcloud auth application-default set-quota-project matmodel-literaturemining-govc
```

After this, every script call uses your credentials automatically. No API key is needed for Gemini because access is through the institutional Vertex AI project.

---

## Workflow

### Normal run (recommended)

Add DOIs to `scripts/fetch_papers.py`, connect to ETH VPN if not on campus, then run both steps:

```bash
# Step 1: Fetch papers
python scripts/fetch_papers.py

# Step 2: Extract data from everything not yet processed
python scripts/extract_data.py --batch
```

`--batch` automatically skips papers whose output JSON already exists. You can safely run it repeatedly and it only processes new papers.

### Processing a single paper (example doi below)

```bash
python scripts/extract_data.py 10.1016/j.jmst.2026.01.050 
```

### Output location

Each paper produces one file:
```
data/outputs/10-1016_j-jmst-2026-01-050_extraction.json
```

**Always review this file before copying anything into Excel.** Check that:
- Record IDs are correct (W_001... convention)
- All temperatures are in Kelvin
- Grain sizes are in micrometres
- Uncertain values are `null`, not guessed

---

## Script Reference

### `scripts/fetch_papers.py`

Fetches papers from publishers and saves them to `data/papers/`.

**Configuration:** edit the `DOIS` list at the top of the file to add papers.

```python
DOIS = [
    "10.1016/j.jmst.2026.01.050",
    "10.1016/j.ijrmhm.2018.09.010",
    # add more here
]
```

**Run:**
```bash
python scripts/fetch_papers.py
```

**What it does per DOI:**
- Elsevier DOIs (`10.1016/...`): calls the ScienceDirect full-text API, saves XML + supplementary files.
- Non-Elsevier DOIs: queries Unpaywall for an open-access PDF and downloads it if found.
- If no OA PDF is available: prints the exact filename to save a manual download as (see [Non-Elsevier Papers](#non-elsevier-papers)).

Saves a log to `data/papers/fetch_log.json` after every run.

**Requirements:** ETH VPN must be active for Elsevier access.

---

### `scripts/extract_data.py`

Sends fetched paper content to Gemini and saves structured JSON.

**Batch mode (process all pending):**
```bash
python scripts/extract_data.py --batch
```

**Single paper:**
```bash
python scripts/extract_data.py 10.1016/j.jmst.2026.01.050
```

**What it does:**
1. Detects whether the paper is an XML or PDF based on what file exists in `data/papers/`
2. **XML route:** extracts body text, downloads all figures in high resolution via Elsevier API
3. **PDF route:** renders every page as a 200 DPI PNG image
4. Reads supplementary `.docx` if present and extracts all paragraph and table text
5. Saves all figures/pages to `data/figures/{doi}/`
6. Runs a lightweight Gemini classification call to categorise each figure as: `microstructure`, `DBTT_curve`, `hardness`, `EBSD`, `fracture_surface`, `map`, or `other`
7. Copies figures into type-specific subfolders under `data/figures/{doi}/`
8. Sends everything to `gemini-2.5-flash` on Vertex AI for extraction
9. Uses Pydantic-enforced structured output — Gemini is constrained to return valid JSON matching the schema exactly
10. Saves output JSON (includes both material records and figure classifications) and prints a summary

**Batch mode logic:** reads `fetch_log.json` and `manual_papers.json`, skips DOIs that already have an output file in `data/outputs/`.

---

### `scripts/ElSevier_test.py`

Old test script, kept for reference. Superseded by `fetch_papers.py`.

---

## Dataset Schema FOR DBTT PROJECT

The extraction schema has 59 fields. All are optional — Gemini returns `null` for anything not reported in the paper.

| Field | Type | Description |
|---|---|---|
| `record_id` | string | e.g. `W_001`, `HR-W` |
| `material_name` | string | e.g. `Tungsten` |
| `commercial_grade_name` | string | e.g. `Plansee W1` |
| `composition_type` | string | `pure`, `doped`, or `alloy` |
| `base_material_purity_pct` | float | e.g. `99.99` |
| `dopant_or_alloying_element` | string | e.g. `K`, `Re` |
| `dopant_concentration` | float | numeric only, no units |
| `dopant_concentration_unit` | string | `ppm`, `wt%`, or `at%` |
| `grain_size_L_um` | float | longitudinal grain size (µm) |
| `grain_size_T_um` | float | transverse grain size (µm) |
| `grain_size_S_um` | float | short-transverse grain size (µm) |
| `grain_size_L/T/S_sd_um` | float | standard deviations |
| `grain_boundary_area_to_volume_per_um` | float | Σ_r descriptor (µm⁻¹) |
| `grain_size_estimated_dimensions` | string | `none`, `partial`, `3D`, etc. |
| `grain_aspect_ratio` | float | |
| `grain_morphology_qualitative` | string | e.g. `pancake`, `equiaxed` |
| `pre_existing_dislocation_density_m2` | float | e.g. `1e14` |
| `dislocation_density_method` | string | e.g. `EBSD`, `TEM` |
| `DBTT_K` | float | midpoint if reported as range |
| `DBTT_uncertainty_K` | float | half-range if DBTT is a range |
| `DBTT_lower_K` | float | lower bound |
| `DBBT_upper_K` | float | upper bound |
| `DBTT_definition` | string | how DBTT was defined |
| `elastic_modulus_GPa` | float | |
| `fracture_toughness_K_IC_MPa_m05` | float | K_IC in MPa√m |
| `charpy_upper_shelf_energy_J` | float | |
| `UTS_MPa` | float | |
| `yield_strength_MPa` | float | |
| `hardness_HV` | float | |
| `measurement_method` | string | `SENT`, `Charpy`, `SPT`, `tensile`, etc. |
| `processing_history` | string | full description |
| `specimen_form` | string | e.g. `plate`, `rod` |
| `specimen_thickness_mm` | float | |
| `degree_of_cold_rolling` | float | |
| `VEC` | float | valence electron concentration |
| `delta_H_mix_kJ_mol` | float | enthalpy of mixing |
| `delta_S_mix_J_mol_K` | float | entropy of mixing |
| `delta_atomic_size` | float | |
| `source_DOI` | string | always the paper's DOI |
| `source_figure_or_table` | string | e.g. `Table 2, Fig. 3` |
| `notes` | string | caveats, uncertainties, methodology notes |


---
### Schema is easily adjustable for differrent data mining needs by simply creating a .yaml file in the schema folder with the needed features. 
## File Structure

```
API_Extraction_D-MATL/
├── scripts/
│   ├── fetch_papers.py        # Add DOIs here; fetches XML/PDF
│   ├── extract_data.py        # Runs extraction; use --batch for automation
│
├── data/
│   ├── papers/
│   │   ├── fetch_log.json                    # Auto-updated by fetch_papers.py
│   │   ├── manual_papers.json                # Add DOIs for manually placed PDFs
│   │   ├── {doi}.xml                         # Elsevier full-text
│   │   ├── {doi}_mmc1.docx                   # Supplementary material
│   │   └── {doi}.pdf                         # Non-Elsevier PDFs
│   │
│   ├── figures/
│   │   └── {doi}/                            # All figures saved here after extraction
│   │       ├── gr1.jpg                       # Raw figures (XML) or page_N.png (PDF)
│   │       ├── microstructure/               # Copies sorted by Gemini classification
│   │       ├── DBTT_curve/
│   │       ├── hardness/
│   │       ├── EBSD/
│   │       ├── fracture_surface/
│   │       ├── map/
│   │       └── other/
│   │
│   └── outputs/
│       └── {doi}_extraction.json             # Gemini output (records + figure classifications)
│
├── .env                       # API keys (never commit to git)
├── .gitignore
├── requirements.txt
├── FIXES.md                   # Changelog of fixes made to the pipeline
└── README.md                  # This file
```

**File naming convention:** DOIs are sanitised for use as filenames by replacing `/` with `_` and `.` with `-`.

Example: `10.1016/j.jmst.2026.01.050` → `10-1016_j-jmst-2026-01-050`

---

## Adding New Papers

### Elsevier papers

1. Open `scripts/fetch_papers.py`
2. Add the DOI to the `DOIS` list
3. Connect to ETH VPN
4. Run `python scripts/fetch_papers.py`
5. Run `python scripts/extract_data.py --batch`

### Non-Elsevier open-access papers

1. Add the DOI to the `DOIS` list in `fetch_papers.py`
2. Run `python scripts/fetch_papers.py` — Unpaywall will find and download the PDF automatically
3. Run `python scripts/extract_data.py --batch`

### Non-Elsevier paywalled papers

`fetch_papers.py` will detect that no OA version exists and print:
```
No OA PDF found. Manual download required.
Save as: data/papers/10-1103_PhysRevMaterials-5-013602.pdf
```

1. Download the PDF from the journal website (using ETH VPN, campus network, Shibboleth login, or whichever access method works for you)
2. Save it with the exact filename shown above in `data/papers/`
3. Add the DOI to `data/papers/manual_papers.json`:
   ```json
   [
     "10.1103/PhysRevMaterials.5.013602"
   ]
   ```
4. Run `python scripts/extract_data.py --batch`

---

## Troubleshooting

### `GOOGLE_CLOUD_PROJECT not set`
Your `.env` file is missing or not being found. Make sure it is in the project root (same folder as `README.md`) and contains `GOOGLE_CLOUD_PROJECT=matmodel-literaturemining-govc`.

### `403 PERMISSION_DENIED` from Vertex AI
Two possible causes:
- **Quota project not set:** run `gcloud auth application-default set-quota-project matmodel-literaturemining-govc`
- **Vertex AI API not enabled:** ask the lab admin to enable `aiplatform.googleapis.com` in the GCP project

### `403 Forbidden` on figure downloads
ETH VPN is not active. Connect to VPN and re-run. Figure downloads from Elsevier require institutional IP access.

### `abstract_only` XML returned
You are not on ETH VPN, or ETH does not subscribe to this specific journal. The script saves an `_abstract_only.xml` file. Connect to VPN and re-run `fetch_papers.py` for that DOI.

### `No paper file found for DOI: ...`
The paper has not been fetched yet. Run `fetch_papers.py` first (for Elsevier), or place a PDF manually (for non-Elsevier paywalled papers).

### `parse_failed` in output JSON
The JSON output was truncated before Gemini finished. This should be very rare with `max_output_tokens=65536`. If it happens, re-run the extraction for that DOI — Gemini responses are non-deterministic and it will usually succeed on the second attempt.

### `PyMuPDF is not installed`
Run `pip install -r requirements.txt`. This installs `PyMuPDF` (imported as `fitz`) which is needed for the PDF route.

---

## Sharing with Other Users

The pipeline is designed to be portable. All paths derive from the script location. No hardcoded machine-specific paths.

Each new user needs to:

1. **Clone the repo** — all scripts and schema are version-controlled
2. **Create a `.env` file** — copy the template from the Setup section; ask the lab admin for the Elsevier API key
3. **Install dependencies** — `pip install -r requirements.txt`
4. **Authenticate with Google Cloud** — `gcloud auth application-default login` with their ETH account; the lab admin must grant them `roles/aiplatform.user` on the project `matmodel-literaturemining-govc`
5. **Connect to ETH VPN** before fetching Elsevier papers

The `data/` folder (containing fetched XMLs, PDFs, and extracted JSONs) is not committed to git and each user builds it locally by running the pipeline.


## Setting up ETH VPN (really easy) 
1) First go to https://sslvpn.ethz.ch
2) For students select student-net under Group. for staff select staff-net
3) For students the username slot should be username@student-net.ethz.ch, for staff the username slot should be username@staff-net.ethz.ch
4) Password should be the net password
5) 2nd password is the microsoft authenticator OTP
6) This will allow you to download the cisco installer
7) Once you run the installer and cisco is set up, follow the same username and passwor dinstructions in app
