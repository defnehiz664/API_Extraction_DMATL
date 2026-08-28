"""
extract_data.py
===============
Extracts structured data from papers using Gemini, matching dataset schema exactly.

Supports two input formats:
  - Elsevier XML  → body text + structured tables + individual figure downloads
  - PDF           → layout-aware Markdown (pymupdf4llm, OCR fallback) + page images

OUTPUT
------
    data/outputs/{doi}_extraction.json   — structured extraction
    data/figures/{doi}/                  — saved EBSD figures (by default)
"""

import os
import sys
import json
import base64
import argparse
import pymupdf4llm
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

MODEL        = "gemini-3.5-flash"

PROJECT_ROOT = Path(__file__).parent.parent
PAPERS_DIR   = PROJECT_ROOT / "data" / "papers"
OUTPUT_DIR   = PROJECT_ROOT / "data" / "outputs"
FIGURES_DIR  = PROJECT_ROOT / "data" / "figures"

PDF_DPI = 400

FIGURE_TYPES = [
    "microstructure",
    "property_curve",
    "phase_diagram",
    "EBSD",
    "fracture_surface",
    "map",
    "other",
]

# ── HELPERS ───────────────────────────────────────────────────────────────────

def doi_to_filename(doi: str) -> str:
    return doi.replace("/", "_").replace(".", "-")


def build_extraction_prompt(doi: str, schema_config: dict) -> str:
    domain_rules    = schema_config.get("domain_rules", "").strip()
    material_system = schema_config.get("material_system", "materials science")
    field_section   = build_schema_prompt_section(schema_config)

    return f"""You are a materials science data extraction assistant.
Your task is to extract structured data from a scientific paper about {material_system}.

═══════════════════════════════════════════════════════════════
STEP 1 — CONFIRM WHAT YOU CAN SEE
═══════════════════════════════════════════════════════════════
Before extracting, briefly confirm in the first record's notes field:
- Which figures you can read
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
- Tabular values: structured tables are provided — an Elsevier "=== TABLES ===" block for XML,
  and/or Markdown pipe tables inside the paper text for PDFs. Read every tabular value from those
  structured tables, matching each number to its row and column header. They are authoritative
  over any flattened running text AND over the page images. Cite the table's label in
  source_figure_or_table.
- Flag ALL uncertainties, methodology notes, and caveats in the notes field
  using [tag] format: [methodology] [fit_parameter] [grain_size_methodology]
  [surrogate] [graph_read] [derived] [scope_caveat] [finding]

RECORD ID:
- Leave record_id as null — it will be assigned manually in Excel

NUMERIC FIDELITY (tables and text):
- A PAPER TEXT / TABLE TEXT block (the paper's text layer) is provided. Take EVERY numeric
  value verbatim from that text, character for character. Do NOT read numbers off the page
  images; images are for figures, plots, and layout only.
- Copy digits and decimal places exactly as printed. Never round, rescale, or clean up a value:
  12.34 stays 12.34, not 12.3 or 12.
- If a number exists only in an image (a plot), extract it as a plot reading and tag [graph_read].

CITATION PROVENANCE:
- For each record, check if the authors explicitly state that specific data values were TAKEN
  FROM another work (e.g. "composition values from [5]"). If yes, fill data_from_reference with the
  reference numbers and reference_titles with the author-year strings in the same order. Do NOT fill
  these for general background or comparison citations. If the data is the authors' own, leave null.

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


# ── ELSEVIER XML ROUTE ────────────────────────────────────────────────────────

SVAPI_NS = "http://www.elsevier.com/xml/svapi/article/dtd"


def extract_xml_tables(xml_path: Path) -> str:
    """Render each XML <table> as a structured Markdown table so row/column
    associations survive (extract_body_text flattens them)."""
    def local(tag):
        return tag.rsplit("}", 1)[-1]

    def text_of(el):
        return " ".join(t.strip() for t in el.itertext() if t.strip()) if el is not None else ""

    def row_cells(row):
        return [" ".join(t.strip() for t in e.itertext() if t.strip())
                for e in row.iter() if local(e.tag) == "entry"]

    root   = ET.parse(xml_path).getroot()
    tables = [e for e in root.iter() if local(e.tag) == "table"]
    if not tables:
        return ""

    blocks = []
    for table in tables:
        label   = next((e for e in table.iter() if local(e.tag) == "label"), None)
        caption = next((e for e in table.iter() if local(e.tag) == "caption"), None)
        header_rows, body_rows = [], []
        for sec in table.iter():
            if local(sec.tag) == "thead":
                header_rows += [r for r in sec.iter() if local(r.tag) == "row"]
            elif local(sec.tag) == "tbody":
                body_rows += [r for r in sec.iter() if local(r.tag) == "row"]
        if not header_rows and not body_rows:
            body_rows = [r for r in table.iter() if local(r.tag) == "row"]

        lines = [f"### {text_of(label) or 'Table'}"]
        if caption is not None:
            lines.append(f"Caption: {text_of(caption)}")
        ncols = max((len(row_cells(r)) for r in header_rows + body_rows), default=0)
        for r in header_rows:
            lines.append("| " + " | ".join(row_cells(r)) + " |")
        if header_rows:
            lines.append("| " + " | ".join(["---"] * ncols) + " |")
        for r in body_rows:
            lines.append("| " + " | ".join(row_cells(r)) + " |")
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)


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


def extract_pdf_markdown(pdf_path: Path) -> str:
    """Layout-aware Markdown for the PDF, with tables rendered as Markdown pipe tables.
    If the text layer is thin/absent (a scan), OCR the PDF with ocrmypdf and retry."""
    md = pymupdf4llm.to_markdown(str(pdf_path))
    if len(md.strip()) >= 200:
        return md
    # thin/absent text layer -> likely a scan: add a text layer with OCR, then retry
    ocr_path = pdf_path.with_name(pdf_path.stem + "_ocr.pdf")
    if not ocr_path.exists():
        import subprocess
        try:
            subprocess.run(
                ["ocrmypdf", "--skip-text", str(pdf_path), str(ocr_path)],
                check=True, capture_output=True)
        except Exception as e:
            print(f"  OCR unavailable/failed ({e}); using thin text layer as-is")
            return md
    md_ocr = pymupdf4llm.to_markdown(str(ocr_path))
    print(f"  OCR applied: {len(md.strip())} -> {len(md_ocr.strip())} chars")
    return md_ocr if len(md_ocr.strip()) > len(md.strip()) else md


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


def _ebsd_figure_ids_from_env() -> set[str]:
    raw = os.environ.get("EBSD_FIGURE_LABELS", "")
    return {label.strip() for label in raw.split(",") if label.strip()}


def save_selected_figures(
    doi_slug: str,
    content_parts: list[tuple[str, bytes, str]],
    allowed_labels: set[str] | None = None,
) -> Path:
    """Save only explicitly allowed EBSD figures without an extra Gemini classification pass."""
    figures_dir = FIGURES_DIR / doi_slug
    allowed = allowed_labels if allowed_labels is not None else _ebsd_figure_ids_from_env()

    if not allowed:
        print("  No EBSD figure IDs configured; skipping figure saving.")
        return figures_dir

    saved = 0
    for label, img_bytes, mime in content_parts:
        if label not in allowed:
            continue
        figures_dir.mkdir(parents=True, exist_ok=True)
        ext      = "png" if "png" in mime else "jpg"
        out_path = figures_dir / f"{doi_slug}_{label}.{ext}"
        out_path.write_bytes(img_bytes)
        saved += 1

    print(f"  Saved {saved}/{len(content_parts)} EBSD figures to {figures_dir} (allowed IDs: {sorted(allowed)})")
    return figures_dir


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
    tables_md    = ""
    input_format = None
    content_parts: list[tuple[str, bytes, str]] = []

    if xml_path.exists():
        input_format = "xml"
        print(f"Input: XML ({xml_path.name})")
        body_text, content_parts = get_xml_content(xml_path)
        tables_md = extract_xml_tables(xml_path)
        print(f"  Body text: {len(body_text):,} characters")
        print(f"  Tables parsed: {tables_md.count('###')}")

    elif pdf_path.exists():
        input_format = "pdf"
        print(f"Input: PDF ({pdf_path.name})")
        content_parts = render_pdf_pages(pdf_path)
        body_text = extract_pdf_markdown(pdf_path)
        print(f"  Markdown: {len(body_text):,} characters")
        if len(body_text.strip()) < 200:
            print("  WARNING: little or no text layer even after OCR; "
                  "numbers will rely on the page images (OCR-quality).")

    else:
        print(f"\nNo paper file found for DOI: {doi}")
        print(f"Expected: {xml_path}  or  {pdf_path}")
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

    # ── Step 4: Save only explicitly configured EBSD figures ──────────────────
    ebsd_labels = _ebsd_figure_ids_from_env() or {"gr12"}
    figures_dir = save_selected_figures(doi_slug, content_parts, ebsd_labels)

    # ── Step 5: Extract structured data ───────────────────────────────────────
    n_parts = len(content_parts)
    unit    = "pages" if input_format == "pdf" else "figures"
    print(f"\nExtracting data with Gemini ({MODEL}): {n_parts} {unit}"
          + (" + supplementary" if supp_text else ""))

    contents = [build_extraction_prompt(doi, schema_config)]
    if body_text:
        contents.append(f"\n\n=== PAPER BODY TEXT ===\n{body_text}\n=== END BODY TEXT ===")
    if tables_md:
        contents.append(
            "\n\n=== TABLES (structured; authoritative for tabular numbers) ===\n"
            + tables_md + "\n=== END TABLES ==="
        )
    if supp_text:
        contents.append(supp_text)
    for label, img_bytes, mime in content_parts:
        contents.append(f"\n[{label}:]")
        contents.append(types.Part.from_bytes(data=img_bytes, mime_type=mime))

    max_output_tokens = 65536
    if "-pro" in MODEL:
        thinking_config = types.ThinkingConfig(thinking_budget=4096)
    elif "3.5" in MODEL:
        thinking_config = types.ThinkingConfig(thinking_level = types.ThinkingLevel.HIGH)
    else:
        thinking_config = types.ThinkingConfig(thinking_budget=0)

    response = client.models.generate_content(
        model=MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            temperature=0.0,
            max_output_tokens=max_output_tokens,
            thinking_config=thinking_config,
            response_mime_type="application/json",
            response_schema=list[schema_model],
        ),
    )

    um = response.usage_metadata
    print(f" (doi): in=(um.prompt_token_count) out=(um.candidates_token_count) "f"thinking={getattr(um, 'thoughts_token_count', 0)})")
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
        if "MAX_TOKENS" in str(finish_reason):
            print(f"  --> Response was TRUNCATED by max_output_tokens={max_output_tokens}. "
                  f"Raise max_output_tokens or split this paper's extraction into fewer records per call.")
        data = {"error": "parse_failed", "finish_reason": str(finish_reason), "raw": raw}

    # give each record a stable id tied to the source
    if isinstance(data, list):
        for i, rec in enumerate(data, start=1):
            if isinstance(rec, dict) and not rec.get("record_id"):
                rec["record_id"] = f"{doi_slug}-{i}"

    output = {
        "model":           MODEL,
        "doi":             doi,
        "input_format":    input_format,
        "n_content_parts": n_parts,
        "supp_chars":      len(supp_text),
        "figures_dir":     str(figures_dir),
        "records":         data,
    }

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\nSaved: {output_file}")
    print(f"Figures: {figures_dir}")
    print("Review the JSON before copying anything into Excel.")

    if isinstance(data, list):
        print(f"\n── EXTRACTION SUMMARY ({len(data)} records) ──")
        for record in data:
            rid     = record.get("record_id") or "?"
            mat     = record.get("material_name") or "?"
            comp    = record.get("composition_type") or "?"
            has_lcf = "yes" if record.get("lcf") else "no"
            print(f"  {rid:10s}  {mat:24s}  {comp:20s}  lcf={has_lcf}")

    return len(data) if isinstance(data, list) else 0


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    DEFAULT_SCHEMA = str(PROJECT_ROOT / "schemas" / "copper" / "schema.yaml")

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
        help="Path to schema YAML (default: schemas/copper/schema.yaml)",
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