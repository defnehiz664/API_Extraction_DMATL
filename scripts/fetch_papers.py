"""
fetch_papers.py
===============
Fetches full-text content from Elsevier's ScienceDirect API for a list of DOIs.
Saves results to the data/papers/ folder as XML files for downstream processing.

USAGE
-----
Run directly to fetch all DOIs in the DOIS list below:
    python fetch_papers.py

Or import and call fetch_paper() from another script:
    from fetch_papers import fetch_paper
    result = fetch_paper("10.1016/j.ijrmhm.2018.09.010")

OUTPUT
------
For each DOI, saves one file to data/papers/:
    - {sanitized_doi}.xml                  if full text was returned
    - {sanitized_doi}_abstract_only.xml    if only abstract returned (not subscribed)
    - {sanitized_doi}_error.txt            if the request failed entirely

Also prints a summary report at the end showing which papers succeeded,
which returned abstract-only, and which failed.

REQUIREMENTS
------------
Must be on ETH VPN for Elsevier full-text access.
ELSEVIER_API_KEY must be set in .env
"""

import os
import json
import time
import requests
from pathlib import Path
from dotenv import load_dotenv
from xml.etree import ElementTree as ET

# ── LOAD ENVIRONMENT ─────────────────────────────────────────────────────────

load_dotenv()
API_KEY = os.environ.get("ELSEVIER_API_KEY")

if not API_KEY:
    raise ValueError(
        "ELSEVIER_API_KEY not found in environment.\n"
        "Add it to your .env file: ELSEVIER_API_KEY=your_key_here"
    )

# ── CONFIGURATION ─────────────────────────────────────────────────────────────

OUTPUT_DIR    = Path(__file__).parent.parent / "data" / "papers"
REQUEST_DELAY = 2.0
ACCEPT_FORMAT = "text/xml"
ELSEVIER_DOI_PREFIX = "10.1016/"

# ── YOUR DOIS ────────────────────────────────────────────────────────────────
# Add all the DOIs from your source_DOI column here.

DOIS = [
    "10.1016/j.ijfatigue.2016.07.019",
    "10.1007/s10854-020-04333-3",
    "COPPER ALLOYS FOR HIGH HEAT FLUX STRUCTURE APPLICATIONS",
]

# ── HELPERS ──────────────────────────────────────────────────────────────────

def doi_to_filename(doi: str) -> str:
    return doi.replace("/", "_").replace(".", "-")


def is_elsevier(doi: str) -> bool:
    return doi.strip().startswith(ELSEVIER_DOI_PREFIX)


def download_supplementary(xml_path: Path, output_dir: Path, api_key: str, doi: str):
    """Find and download supplementary files referenced in the XML.
    Files are saved as {doi_filename}_{original_name} to avoid collisions
    when multiple papers share the same supplementary filename (e.g. mmc1.docx).
    """
    tree = ET.parse(xml_path)
    root = tree.getroot()
    doi_prefix = doi_to_filename(doi)

    for attachment in root.findall(".//{http://www.elsevier.com/xml/xocs/dtd}attachment"):
        eid = attachment.findtext(
            "{http://www.elsevier.com/xml/xocs/dtd}attachment-eid", ""
        )
        if not eid:
            continue
        if not (eid.endswith(".docx") or eid.endswith(".pdf")):
            continue

        url      = f"https://api.elsevier.com/content/object/eid/{eid}"
        filename = f"{doi_prefix}_{eid.split('-')[-1]}"  # e.g. 10-1016_..._mmc1.docx
        dest     = output_dir / filename

        print(f"  Downloading supplementary: {filename}")
        response = requests.get(url, headers={"X-ELS-APIKey": api_key})
        if response.status_code == 200:
            dest.write_bytes(response.content)
            print(f"  Saved {filename}")
        else:
            print(f"  Failed ({response.status_code})")


def fetch_pdf_via_unpaywall(doi: str, output_dir: Path) -> dict:
    """Attempt to download a PDF for non-Elsevier papers via Unpaywall."""
    email = "dabiakar@ethz.ch"
    url   = f"https://api.unpaywall.org/v2/{doi}?email={email}"

    try:
        response = requests.get(url, timeout=15)
        if response.status_code != 200:
            return {"doi": doi, "status": "error",
                    "message": f"Unpaywall returned {response.status_code}"}

        data    = response.json()
        best_oa = data.get("best_oa_location")

        if not best_oa:
            manual_filename = f"{doi_to_filename(doi)}.pdf"
            msg = (
                f"No open-access PDF found via Unpaywall.\n"
                f"Download the PDF manually and save it as:\n"
                f"  data/papers/{manual_filename}\n"
                f"Then run: python scripts/extract_data.py {doi}"
            )
            print(f"  No OA PDF found. Manual download required.")
            print(f"  Save as: data/papers/{manual_filename}")
            return {"doi": doi, "status": "no_oa_pdf", "message": msg}

        pdf_url = best_oa.get("url_for_pdf") or best_oa.get("url")
        if not pdf_url:
            manual_filename = f"{doi_to_filename(doi)}.pdf"
            msg = (
                f"Unpaywall found an OA record but no direct PDF URL.\n"
                f"Download the PDF manually and save it as:\n"
                f"  data/papers/{manual_filename}\n"
                f"Then run: python scripts/extract_data.py {doi}"
            )
            print(f"  OA record exists but no direct PDF link. Manual download required.")
            print(f"  Save as: data/papers/{manual_filename}")
            return {"doi": doi, "status": "no_oa_pdf", "message": msg}

        pdf_response = requests.get(pdf_url, timeout=30)
        if pdf_response.status_code != 200:
            return {"doi": doi, "status": "error",
                    "message": f"PDF download failed: HTTP {pdf_response.status_code}"}

        filename = output_dir / f"{doi_to_filename(doi)}.pdf"
        filename.write_bytes(pdf_response.content)
        print(f"  PDF saved via Unpaywall: {filename.name}")
        return {"doi": doi, "status": "pdf_downloaded",
                "filename": str(filename),
                "message": f"PDF from {best_oa.get('host_type', 'unknown')} source"}

    except Exception as e:
        return {"doi": doi, "status": "error", "message": str(e)}


def fetch_paper(doi: str, output_dir: Path = OUTPUT_DIR) -> dict:
    """
    Fetch a single paper from Elsevier ScienceDirect API by DOI.

    Returns a dict with keys:
        doi         - the DOI requested
        status      - "full_text", "abstract_only", "pdf_downloaded", or "error"
        filename    - path to saved file (if successful)
        message     - human-readable description of outcome
    """
    if not is_elsevier(doi):
        print(f"\nFetching: {doi}")
        print(f"  Publisher: non-Elsevier -> PDF route (Unpaywall)")
        return fetch_pdf_via_unpaywall(doi, output_dir)

    url = f"https://api.elsevier.com/content/article/doi/{doi}"
    headers = {
        "X-ELS-APIKey": API_KEY,
        "Accept": ACCEPT_FORMAT,
    }
    params = {"view": "FULL"}

    print(f"\nFetching: {doi}")
    print(f"  Publisher: Elsevier -> XML route")
    print(f"  URL: {url}")

    try:
        response = requests.get(url, headers=headers, params=params, timeout=30)

        print(f"  Status code: {response.status_code}")
        print(f"  Content-Type: {response.headers.get('Content-Type', 'unknown')}")
        print(f"  Response size: {len(response.content)} bytes")

        filename_base = doi_to_filename(doi)

        if response.status_code == 200:
            content = response.text
            is_full_text = (
                "<xocs:rawtext>" in content or
                "<ce:sections>" in content or
                "<body>" in content.lower()
            )

            if is_full_text:
                filename = output_dir / f"{filename_base}.xml"
                filename.write_text(content, encoding="utf-8")
                print(f"  Full text saved: {filename.name}")
                download_supplementary(filename, output_dir, API_KEY, doi)
                return {
                    "doi": doi,
                    "status": "full_text",
                    "filename": str(filename),
                    "message": f"Full text XML saved ({len(content)} chars)"
                }
            else:
                filename = output_dir / f"{filename_base}_abstract_only.xml"
                filename.write_text(content, encoding="utf-8")
                print(f"  Abstract only (check VPN / subscription): {filename.name}")
                return {
                    "doi": doi,
                    "status": "abstract_only",
                    "filename": str(filename),
                    "message": (
                        "Only abstract returned. Possible causes:\n"
                        "  1. Not on ETH network/VPN\n"
                        "  2. ETH does not subscribe to this specific journal\n"
                        "  3. Paper is behind a different publisher's paywall"
                    )
                }

        elif response.status_code == 403:
            error_text = (
                f"HTTP 403 - Not entitled to full text.\n"
                f"DOI: {doi}\n"
                f"Response: {response.text[:500]}"
            )
            filename = output_dir / f"{filename_base}_error.txt"
            filename.write_text(error_text, encoding="utf-8")
            print(f"  Not entitled (403) - check VPN and ETH subscription")
            return {
                "doi": doi,
                "status": "error",
                "filename": str(filename),
                "message": "HTTP 403 - Not entitled. Are you on ETH VPN?"
            }

        elif response.status_code == 404:
            error_text = f"HTTP 404 - DOI not found on ScienceDirect.\nDOI: {doi}"
            filename = output_dir / f"{filename_base}_error.txt"
            filename.write_text(error_text, encoding="utf-8")
            print(f"  Not found (404) - DOI may not be an Elsevier paper")
            return {
                "doi": doi,
                "status": "error",
                "filename": str(filename),
                "message": "HTTP 404 - DOI not found. Is this an Elsevier journal?"
            }

        elif response.status_code == 429:
            print(f"  Rate limited (429) - waiting 30s before continuing")
            time.sleep(30)
            return {
                "doi": doi,
                "status": "error",
                "filename": None,
                "message": "HTTP 429 - Rate limited. Try again later or increase REQUEST_DELAY."
            }

        else:
            error_text = (
                f"HTTP {response.status_code}\n"
                f"DOI: {doi}\n"
                f"Response: {response.text[:500]}"
            )
            filename = output_dir / f"{filename_base}_error.txt"
            filename.write_text(error_text, encoding="utf-8")
            print(f"  Unexpected status: {response.status_code}")
            return {
                "doi": doi,
                "status": "error",
                "filename": str(filename),
                "message": f"HTTP {response.status_code} - unexpected error"
            }

    except requests.exceptions.Timeout:
        print(f"  Request timed out after 30s")
        return {
            "doi": doi,
            "status": "error",
            "filename": None,
            "message": "Request timed out - check your network connection"
        }

    except requests.exceptions.ConnectionError as e:
        print(f"  Connection error: {e}")
        return {
            "doi": doi,
            "status": "error",
            "filename": None,
            "message": f"Connection error: {e}"
        }


def print_summary(results: list):
    print(f"\n{'='*60}")
    print("FETCH SUMMARY")
    print(f"{'='*60}")

    full_text     = [r for r in results if r["status"] == "full_text"]
    abstract_only = [r for r in results if r["status"] == "abstract_only"]
    errors        = [r for r in results if r["status"] == "error"]

    print(f"\n  Full text retrieved ({len(full_text)}/{len(results)}):")
    for r in full_text:
        print(f"    {r['doi']}")

    if abstract_only:
        print(f"\n  Abstract only - check VPN/subscription ({len(abstract_only)}):")
        for r in abstract_only:
            print(f"    {r['doi']}")

    if errors:
        print(f"\n  Errors ({len(errors)}):")
        for r in errors:
            print(f"    {r['doi']}: {r['message']}")

    print(f"\n{'='*60}")
    print(f"Total: {len(results)} | Success: {len(full_text)} | "
          f"Abstract only: {len(abstract_only)} | Errors: {len(errors)}")
    print(f"{'='*60}\n")


# ── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Output directory: {OUTPUT_DIR}")
    print(f"Fetching {len(DOIS)} papers...")
    print(f"IMPORTANT: Make sure you are on ETH VPN before running this.\n")

    results = []
    for i, doi in enumerate(DOIS):
        result = fetch_paper(doi)
        results.append(result)
        if i < len(DOIS) - 1:
            print(f"  Waiting {REQUEST_DELAY}s before next request...")
            time.sleep(REQUEST_DELAY)

    print_summary(results)

    log_path = OUTPUT_DIR / "fetch_log.json"
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print(f"Full results log saved to: {log_path}")


if __name__ == "__main__":
    main()
