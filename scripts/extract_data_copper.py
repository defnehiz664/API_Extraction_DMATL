"""
extract_data.py
===============
Extracts structured data from papers using Gemini, matching dataset schema exactly.

Supports two input formats:
  - Elsevier XML  → body text + individual figure downloads via Elsevier API
  - PDF           → each page rendered as an image (for non-Elsevier papers)

Before extraction, runs a second Gemini call to classify every figure by
type. Only figures matching SAVED_FIGURE_TYPES (currently: EBSD) are
saved to disk — all figures are still sent to the main extraction call
regardless, so property plots, micrographs, and other relevant figures are
still read for data, they're just not kept as image files afterward.

SETUP
-----
    pip install -r requirements.txt

    .env file needs:
        ELSEVIER_API_KEY=your_elsevier_key
        GOOGLE_CLOUD_PROJECT=matmodel-literaturemining-govc
        GOOGLE_CLOUD_LOCATION=europe-west4
        GOOGLE_GENAI_USE_ENTERPRISE=True

    Authenticate once per machine:
        gcloud auth application-default login
        gcloud auth application-default set-quota-project matmodel-literaturemining-govc
        (use dabiakar@ethz.ch — NOT the student address)

USAGE
-----
    # Process ALL fetched papers not yet extracted (recommended):
    python scripts/extract_data.py --batch

    # Process a single paper by DOI:
    python scripts/extract_data.py 10.1016/j.jmst.2026.01.050

    For paywalled non-Elsevier papers:
        1. Download the PDF and save as data/papers/{sanitized-doi}.pdf
        2. Add the DOI to data/papers/manual_papers.json
        3. Run --batch

OUTPUT
------
    data/outputs/{doi}_extraction.json   — structured extraction + figure classifications
    data/figures/{doi}/                  — only figures classified as one of
                                            SAVED_FIGURE_TYPES (EBSD by default)
"""

import os
import sys
import json
import base64
import argparse
import requests
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET
from dotenv import load_dotenv
from pydantic import BaseModel
from google import genai
from google.genai import types
from schema_loader import load_schema, build_schema_prompt_section

# ── CONFIG ────────────────────────────────────────────────────────────────────

load_dotenv()
ELSEVIER_API_KEY      = os.environ.get("ELSEVIER_API_KEY")
GOOGLE_CLOUD_PROJECT  = os.environ.get("GOOGLE_CLOUD_PROJECT")
GOOGLE_CLOUD_LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "europe-west4")

if not GOOGLE_CLOUD_PROJECT:
    raise ValueError(
        "GOOGLE_CLOUD_PROJECT not set in .env file.\n"
        "Add: GOOGLE_CLOUD_PROJECT=matmodel-literaturemining-govc\n"
        "Then run: gcloud auth application-default login"
    )

MODEL        = "gemini-2.5-flash" 


PROJECT_ROOT = Path(__file__).parent.parent
PAPERS_DIR   = PROJECT_ROOT / "data" / "papers"
OUTPUT_DIR   = PROJECT_ROOT / "data" / "outputs"
FIGURES_DIR  = PROJECT_ROOT / "data" / "figures"

PDF_DPI = 200

FIGURE_TYPES = [
    "microstructure",          # optical/SEM/TEM images of grain structure, porosity, precipitates, inclusions
    "property_curve",         # stress-strain, hardness, strength/ductility, conductivity/resistivity vs. temperature/processing plots
    "phase_diagram",          # phase diagrams, constitution/processing maps, precipitation maps
    "EBSD",                   # IPF maps, pole figures, grain boundary maps, misorientation plots
    "fracture_surface",       # SEM images of fracture surfaces (intergranular, cleavage, etc.)
    "map",                    # EDS/WDS maps, strain maps, dislocation density maps, texture maps
    "other",                  # schematics, XRD patterns, photographs, tables, flow charts
]

# ── CLASSIFICATION SCHEMA ─────────────────────────────────────────────────────

class FigureInfo(BaseModel):
    figure_id: str        # matches the label used in the extraction (e.g. "gr1", "page_3")
    figure_type: str      # one of FIGURE_TYPES
    description: str      # one sentence describing what the figure shows
    contains_data: bool   # True if the figure contains quantitative data useful for extraction


# ── HELPERS ───────────────────────────────────────────────────────────────────

def doi_to_filename(doi: str) -> str:
    return doi.replace("/", "_").replace(".", "-")


def build_extraction_prompt(doi: str, schema_config: dict) -> str:
    domain_rules   = schema_config.get("domain_rules", "").strip()
    material_system = schema_config.get("material_system", "materials science")
    field_section  = build_schema_prompt_section(schema_config)

    return f"""You are a materials science data extraction assistant.
Your task is to extract structured data from a scientific paper about {material_system}.

═══════════════════════════════════════════════════════════════
STEP 1 — CONFIRM WHAT YOU CAN SEE
═══════════════════════════════════════════════════════════════
Before extracting, briefly confirm in the first record's notes field:
- Which figures you can read (e.g. "Confirmed: can read hysteresis loop, cyclic stress response curve, strain-life (ε-N) plot, XRD diffractogram, SEM/BSE micrograph")
- Which figures contain data you extracted from
- Any figures that were unreadable or ambiguous
- Whether supplementary material was provided and what it contained

═══════════════════════════════════════════════════════════════
STEP 2 — EXTRACTION RULES
═══════════════════════════════════════════════════════════════

GENERAL:
- Extract data for EVERY distinct material/specimen in the paper including
  from supplementary tables and figures
- Each material/specimen gets its own JSON object
- Use null for values not reported — never guess or invent values
- For numeric fields return only the number, never include units in the value
- - Flag ALL uncertainties, methodology notes, and caveats in the notes field
  using [tag] format: [methodology] [fit_parameter] [grain_size_methodology]
  [surrogate] [graph_read] [derived] [scope_caveat] [finding]

  Tag meanings (use exactly):
    [fit_parameter]  - a CM-Basquin/cyclic coefficient; say if tabulated, author-fit, or needs re-fitting
    [surrogate]      - value is for a near-neighbor alloy/temper, not this specimen
    [scope_caveat]   - value valid only under a stated restriction (temperature, R-ratio, environment)
    [graph_read]     - digitized off a plot; add [high]/[low]
    [derived]        - computed from other reported values, not measured
    ... (one line each)

RECORD ID:
- Leave record_id as null — it will be assigned manually in Excel

GRAPHS AND FIGURES:
- For any figure that shows a graph or plot: read the axis labels and units,
  then extract data points as (x_value, y_value, series_label) tuples
- For continuous curves: extract ~10-20 points per series, more at inflection points
- For discrete scatter plots: extract every visible point
- Tag confidence in notes: [graph_read][high] for clear gridlines / single series,
  [graph_read][low] for overlapping series, log scale, or small symbols

CITATION PROVENANCE:
- For each record, check if the paper's authors explicitly state that specific
  data values were TAKEN FROM another work (e.g. "DBTT values from [5]",
  "data replotted from Bonnekoh et al. [12]")
- If yes: fill data_from_reference with the reference numbers, e.g. ["[5]", "[8]"]
  and fill reference_titles with the author-year strings in the same order,
  e.g. ["Bonnekoh 2019", "Riesch et al. 2021"]
- DO NOT fill these for general background or comparison citations —
  only for explicit data provenance
- If data was generated by the authors of this paper, leave both null

{domain_rules}

═══════════════════════════════════════════════════════════════
STEP 3 — FIELDS
═══════════════════════════════════════════════════════════════
{field_section}

═══════════════════════════════════════════════════════════════
STEP 4 — OUTPUT FORMAT
═══════════════════════════════════════════════════════════════
- Return ONLY a JSON array — no preamble, no explanation, no markdown fences
- Every object must have exactly the keys listed above
- Use null (not empty string, not 0) for unreported values
- source_DOI = "{doi}" for all rows from this paper
- source_figure_or_table: cite the specific figure/table for each value, e.g. "Table 2, Fig.3a"
"""


def build_classification_prompt() -> str:
    types_formatted = "\n".join(f'  - "{t}"' for t in FIGURE_TYPES)
    return f"""You are a materials science figure classification assistant.

For each figure or page image provided, classify it into exactly one of these types:
{types_formatted}

Definitions:
- microstructure: optical microscopy, SEM, or TEM images showing grain structure, porosity, precipitates, or inclusions
- property_curve: stress-strain curves, hardness/strength/ductility plots, conductivity/resistivity versus temperature or processing
- phase_diagram: phase diagrams, constitution maps, precipitation/processing diagrams
- EBSD: inverse pole figure maps, pole figures, grain boundary maps, misorientation angle distributions
- fracture_surface: SEM images of fracture surfaces showing intergranular or cleavage fracture
- map: EDS/WDS elemental maps, strain maps, dislocation density maps, texture maps
- other: schematics, XRD patterns, photographs of specimens, tables, flow charts

For each figure, return:
- figure_id: the label shown before the image (e.g. "gr1", "page_3")
- figure_type: one of the types above
- description: one sentence describing what is shown
- contains_data: true if the figure contains quantitative data relevant to copper-alloy composition, microstructure, or mechanical/electrical properties

Return a JSON array — one object per figure.
"""


# ── ELSEVIER XML ROUTE ────────────────────────────────────────────────────────

SVAPI_NS = "http://www.elsevier.com/xml/svapi/article/dtd"


def extract_body_text(xml_path: Path) -> str:
    tree = ET.parse(xml_path)
    root = tree.getroot()
    body = root.find(".//{http://www.elsevier.com/xml/ja/dtd}body")
    if body is None:
        return " ".join(root.itertext())
    lines = []
    for elem in body.iter():
        text = (elem.text or "").strip()
        tail = (elem.tail or "").strip()
        if text:
            lines.append(text)
        if tail:
            lines.append(tail)
    return " ".join(lines)


def extract_figure_urls(xml_path: Path) -> dict:
    tree = ET.parse(xml_path)
    root = tree.getroot()
    figures = {}
    for obj in root.findall(f"{{{SVAPI_NS}}}objects/{{{SVAPI_NS}}}object"):
        ref      = obj.get("ref", "")
        category = obj.get("category", "")
        url      = obj.text.strip() if obj.text else ""
        if not ref or not url:
            continue
        if ref.startswith("si") or ref.startswith("ga"):
            continue
        if "svg" in (obj.get("type", "") + url).lower():
            continue
        if ref not in figures:
            figures[ref] = {}
        figures[ref][category] = url
    return figures


def download_figure_as_base64(url: str, api_key: str) -> tuple[str, str]:
    headers  = {"X-ELS-APIKey": api_key} if api_key else {}
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    mime = response.headers.get("Content-Type", "image/jpeg").split(";")[0]
    b64  = base64.standard_b64encode(response.content).decode("utf-8")
    return b64, mime


def get_xml_content(xml_path: Path) -> tuple[str, list[tuple[str, bytes, str]]]:
    body_text    = extract_body_text(xml_path)
    figure_urls  = extract_figure_urls(xml_path)
    main_figures = {ref: urls for ref, urls in figure_urls.items() if ref.startswith("gr")}
    print(f"  Found {len(main_figures)} figures: {sorted(main_figures.keys())}")

    figure_parts = []
    for ref in sorted(main_figures.keys()):
        urls = main_figures[ref]
        url  = urls.get("high") or urls.get("standard") or urls.get("thumbnail")
        if not url:
            continue
        print(f"  Downloading {ref}...", end=" ", flush=True)
        try:
            b64, mime = download_figure_as_base64(url, ELSEVIER_API_KEY)
            figure_parts.append((ref, base64.standard_b64decode(b64), mime))
            print(f"OK ({len(b64)//1024} KB)")
        except Exception as e:
            print(f"FAILED: {e}")

    return body_text, figure_parts


# ── PDF ROUTE ─────────────────────────────────────────────────────────────────

def render_pdf_pages(pdf_path: Path, dpi: int = PDF_DPI) -> list[tuple[str, bytes, str]]:
    try:
        import fitz
    except ImportError:
        raise ImportError("PyMuPDF not installed. Run: pip install PyMuPDF")

    doc    = fitz.open(pdf_path)
    pages  = []
    matrix = fitz.Matrix(dpi / 72, dpi / 72)

    print(f"  Rendering {len(doc)} pages at {dpi} DPI...")
    for i, page in enumerate(doc, start=1):
        pix       = page.get_pixmap(matrix=matrix, alpha=False)
        png_bytes = pix.tobytes("png")
        pages.append((f"page_{i}", png_bytes, "image/png"))
        print(f"  Page {i}/{len(doc)}: {len(png_bytes)//1024} KB")

    doc.close()
    return pages


# ── SUPPLEMENTARY DOCX ────────────────────────────────────────────────────────

def extract_docx_text(docx_path: Path) -> str:
    try:
        from docx import Document
    except ImportError:
        print("  python-docx not installed. Run: pip install python-docx")
        return ""
    doc   = Document(docx_path)
    parts = []
    for para in doc.paragraphs:
        if para.text.strip():
            parts.append(para.text.strip())
    for table in doc.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def find_supplementary_docx(doi: str) -> list[Path]:
    doi_prefix = doi_to_filename(doi)
    files      = sorted(PAPERS_DIR.glob(f"{doi_prefix}_mmc*.docx"))
    if files:
        return files
    files = sorted(PAPERS_DIR.glob("mmc*.docx"))
    if files:
        print(f"  Note: legacy-named supplementary found ({[f.name for f in files]}). "
              f"Rename to '{doi_prefix}_mmc1.docx' to avoid ambiguity.")
    return files


# ── FIGURE SAVING ─────────────────────────────────────────────────────────────

SAVED_FIGURE_TYPES = {"EBSD"}


def save_selected_figures(
    doi_slug: str,
    content_parts: list[tuple[str, bytes, str]],
    classifications: list[dict],
) -> Path:
    """
    Save only figures whose classified type is in SAVED_FIGURE_TYPES to
    data/figures/{doi_slug}/, named {doi_slug}_{label}.{ext}. Figures are
    still sent to Gemini for the main extraction call regardless of
    whether they're saved here — this only controls what's kept on disk.
    """
    figures_dir = FIGURES_DIR / doi_slug
    type_by_label = {c.get("figure_id", ""): c.get("figure_type", "") for c in classifications}

    saved = 0
    for label, img_bytes, mime in content_parts:
        if type_by_label.get(label) not in SAVED_FIGURE_TYPES:
            continue
        figures_dir.mkdir(parents=True, exist_ok=True)
        ext      = "png" if "png" in mime else "jpg"
        out_path = figures_dir / f"{doi_slug}_{label}.{ext}"
        out_path.write_bytes(img_bytes)
        saved += 1

    print(f"  Saved {saved}/{len(content_parts)} figures to {figures_dir} (types kept: {sorted(SAVED_FIGURE_TYPES)})")
    return figures_dir


# ── FIGURE CLASSIFICATION ─────────────────────────────────────────────────────

def classify_figures(
    content_parts: list[tuple[str, bytes, str]],
    client: genai.Client,
) -> list[dict]:
    """
    Send all figures to Gemini in a separate lightweight call and ask it to
    classify each one by type and describe what it shows.
    """
    if not content_parts:
        return []

    print(f"  Classifying {len(content_parts)} figures...")

    contents = [build_classification_prompt()]
    for label, img_bytes, mime in content_parts:
        contents.append(f"\n[{label}:]")
        contents.append(types.Part.from_bytes(data=img_bytes, mime_type=mime))

    response = client.models.generate_content(
        model=MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            temperature=0.0,
            max_output_tokens=4096,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
            response_mime_type="application/json",
            response_schema=list[FigureInfo],
        ),
    )

    try:
        return json.loads(response.text)
    except json.JSONDecodeError:
        print("  Classification parse failed — no figures will be saved (can't determine type)")
        return []


# ── BATCH MODE ────────────────────────────────────────────────────────────────

def get_pending_dois() -> list[str]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    dois = []

    fetch_log = PAPERS_DIR / "fetch_log.json"
    if fetch_log.exists():
        with open(fetch_log, encoding="utf-8") as f:
            log = json.load(f)
        for entry in log:
            if entry.get("status") in ("full_text", "pdf_downloaded"):
                dois.append(entry["doi"])
    else:
        print("  No fetch_log.json found — run fetch_papers.py first")

    manual_log = PAPERS_DIR / "manual_papers.json"
    if manual_log.exists():
        with open(manual_log, encoding="utf-8") as f:
            dois.extend(json.load(f))
    else:
        manual_log.write_text('[]\n', encoding="utf-8")

    seen, unique_dois = set(), []
    for doi in dois:
        if doi not in seen:
            seen.add(doi)
            unique_dois.append(doi)

    pending = []
    for doi in unique_dois:
        output_file = OUTPUT_DIR / f"{doi_to_filename(doi)}_extraction.json"
        if output_file.exists():
            print(f"  [skip] {doi}  (already extracted)")
        else:
            pending.append(doi)

    return pending


# ── MAIN EXTRACTION ───────────────────────────────────────────────────────────

def run_extraction(doi: str, schema_model, schema_config: dict):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    doi_slug    = doi_to_filename(doi)
    xml_path    = PAPERS_DIR / f"{doi_slug}.xml"
    pdf_path    = PAPERS_DIR / f"{doi_slug}.pdf"
    output_file = OUTPUT_DIR / f"{doi_slug}_extraction.json"

    # ── Step 1: Detect input and get content ──────────────────────────────────
    body_text    = ""
    input_format = None
    content_parts: list[tuple[str, bytes, str]] = []

    if xml_path.exists():
        input_format = "xml"
        print(f"Input: XML ({xml_path.name})")
        body_text, content_parts = get_xml_content(xml_path)
        print(f"  Body text: {len(body_text):,} characters")

    elif pdf_path.exists():
        input_format = "pdf"
        print(f"Input: PDF ({pdf_path.name})")
        content_parts = render_pdf_pages(pdf_path)

    else:
        print(f"\nNo paper file found for DOI: {doi}")
        print(f"Expected: {xml_path}  or  {pdf_path}")
        print(f"\nFor Elsevier: add DOI to fetch_papers.py and run it (ETH VPN required)")
        print(f"For others:   save PDF as data/papers/{doi_slug}.pdf")
        sys.exit(1)

    # ── Step 2: Supplementary docx ─────────────────────────────────────────────
    supp_text  = ""
    supp_files = find_supplementary_docx(doi)
    if supp_files:
        print(f"\nSupplementary: {[f.name for f in supp_files]}")
        for sf in supp_files:
            print(f"  Reading {sf.name}...", end=" ", flush=True)
            text       = extract_docx_text(sf)
            supp_text += f"\n\n=== SUPPLEMENTARY: {sf.name} ===\n{text}\n"
            print(f"OK ({len(text):,} chars)")
    else:
        print("\nNo supplementary .docx found")

    # ── Step 3: Gemini client ─────────────────────────────────────────────────
    client = genai.Client(
        vertexai=True,
        project=GOOGLE_CLOUD_PROJECT,
        location=GOOGLE_CLOUD_LOCATION,
    )

    # ── Step 4: Classify figures (separate lightweight call) ──────────────────
    print(f"\nClassifying figures...")
    classifications = classify_figures(content_parts, client)

    # ── Step 5: Save only figures matching SAVED_FIGURE_TYPES ─────────────────
    figures_dir = save_selected_figures(doi_slug, content_parts, classifications)

    # ── Step 6: Extract structured data ───────────────────────────────────────
    n_parts = len(content_parts)
    unit    = "pages" if input_format == "pdf" else "figures"
    print(f"\nExtracting data with Gemini ({MODEL}): {n_parts} {unit}"
          + (" + supplementary" if supp_text else ""))

    contents = [build_extraction_prompt(doi, schema_config)]
    if body_text:
        contents.append(f"\n\n=== PAPER BODY TEXT ===\n{body_text}\n=== END BODY TEXT ===")
    if supp_text:
        contents.append(supp_text)
    for label, img_bytes, mime in content_parts:
        contents.append(f"\n[{label}:]")
        contents.append(types.Part.from_bytes(data=img_bytes, mime_type=mime))

    max_output_tokens = 65536
    response = client.models.generate_content(
        model=MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            temperature=0.0,
            max_output_tokens=max_output_tokens,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
            response_mime_type="application/json",
            response_schema=list[schema_model],
        ),
    )

    raw = response.text
    print(f"  Response: {len(raw):,} characters")

    finish_reason = None
    if response.candidates:
        finish_reason = response.candidates[0].finish_reason
        usage = response.usage_metadata
        print(f"  finish_reason: {finish_reason}  "
              f"(output_tokens={getattr(usage, 'candidates_token_count', '?')}/{max_output_tokens})")

    # ── Step 7: Parse and save ────────────────────────────────────────────────
    try:
        data = json.loads(raw)
        print(f"  Parsed: {len(data)} material records")
    except json.JSONDecodeError as e:
        print(f"  JSON parse failed: {e}")
        if str(finish_reason) == "MAX_TOKENS" or "MAX_TOKENS" in str(finish_reason):
            print(f"  --> Response was TRUNCATED by max_output_tokens={max_output_tokens}. "
                  f"Raise max_output_tokens or split this paper's extraction into fewer records per call.")
        data = {"error": "parse_failed", "finish_reason": str(finish_reason), "raw": raw}

    output = {
        "model":              MODEL,
        "doi":                doi,
        "input_format":       input_format,
        "n_content_parts":    n_parts,
        "supp_chars":         len(supp_text),
        "figures_dir":        str(figures_dir),
        "figure_classifications": classifications,
        "records":            data,
    }

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\nSaved: {output_file}")
    print(f"Figures: {figures_dir}")
    print("Review the JSON before copying anything into Excel.")

    if isinstance(data, list):
        print(f"\n── EXTRACTION SUMMARY ({len(data)} records) ──")
        for record in data:
            rid  = record.get("record_id") or "?"
            mat  = record.get("material_name") or "?"
            dbtt = record.get("DBTT_K", "null")
            gs_l = record.get("grain_size_L_um", "null")
            print(f"  {rid:10s}  {mat:20s}  DBTT={dbtt}K  grain_L={gs_l}µm")

    if classifications:
        print(f"\n── FIGURE CLASSIFICATION ──")
        for item in classifications:
            fid   = item.get("figure_id", "?")
            ftype = item.get("figure_type", "?")
            desc  = item.get("description", "")
            data_flag = "[DATA]" if item.get("contains_data") else ""
            print(f"  {fid:8s}  {ftype:18s}  {data_flag:6s}  {desc[:60]}")

    return len(data) if isinstance(data, list) else 0


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    DEFAULT_SCHEMA = str(PROJECT_ROOT / "schemas" / "copper / "schema.yaml")

    parser = argparse.ArgumentParser(
        description="Extract structured materials data from paper XMLs or PDFs using Gemini."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("doi", nargs="?", help="DOI of a single paper")
    group.add_argument("--batch", action="store_true",
                       help="Process all fetched papers not yet extracted")
    parser.add_argument(
        "--schema",
        default=DEFAULT_SCHEMA,
        help=f"Path to schema YAML (default: schemas/copper/schema.yaml)",
    )

    args = parser.parse_args()

    schema_path = Path(args.schema)
    if not schema_path.exists():
        print(f"Schema file not found: {schema_path}")
        sys.exit(1)

    print(f"Schema: {schema_path.name}")
    schema_model, schema_config = load_schema(schema_path)
    print(f"  Project: {schema_config.get('project')}  |  "
          f"{len(schema_config.get('fields', []))} fields loaded\n")

    if args.batch:
        print("── BATCH MODE ──────────────────────────────────────────")
        print("Scanning for papers pending extraction...\n")
        pending = get_pending_dois()

        if not pending:
            print("\nAll fetched papers have already been extracted.")
            sys.exit(0)

        print(f"\n{len(pending)} paper(s) to process: {pending}\n")
        total_records, failed = 0, []

        for i, doi in enumerate(pending, start=1):
            print(f"\n{'='*60}")
            print(f"[{i}/{len(pending)}] {doi}")
            print(f"{'='*60}")
            try:
                total_records += run_extraction(doi, schema_model, schema_config)
            except Exception as e:
                print(f"  ERROR: {e}")
                failed.append(doi)

        print(f"\n{'='*60}")
        print(f"BATCH COMPLETE: {len(pending)-len(failed)}/{len(pending)} succeeded, "
              f"{total_records} total records extracted")
        if failed:
            print(f"Failed: {failed}")

    else:
        run_extraction(args.doi, schema_model, schema_config)
