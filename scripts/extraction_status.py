"""
extraction_status.py — per-paper health of the extraction outputs. Read-only.

Scans data/outputs/*_extraction.json and reports, for each paper: input format,
status (ok / failed / empty / malformed), and record count. Cross-checks against
the papers present on disk to flag any that produced NO output (missing), and flags
orphan `_am`/`_ocr` outputs left over from the old duplicate runs. Ends with totals,
including the grand total record count — the number to compare run-to-run.

Usage:
  python scripts/extraction_status.py
  python scripts/extraction_status.py --outputs-dir data/outputs --papers-dir data/papers
"""
import argparse
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUTS = REPO / "data" / "outputs"
DEFAULT_PAPERS = REPO / "data" / "papers"


def _status(path):
    """Return (status, n_records, input_format) for one output file."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as e:
        return ("malformed", 0, f"unreadable: {e}")
    fmt = data.get("input_format", "?")
    recs = data.get("records")
    if isinstance(recs, list):
        n = len(recs)
        return (("ok" if n else "empty"), n, fmt)
    if isinstance(recs, dict):
        if "error" in recs or "finish_reason" in recs:
            return ("failed", 0, f"{fmt} ({recs.get('finish_reason') or recs.get('error')})")
        vals = [v for v in recs.values() if isinstance(v, dict)]
        return (("ok" if vals else "empty"), len(vals), fmt + " (dict-shaped)")
    return ("malformed", 0, fmt)


def _present_paper_stems(papers_dir):
    """Real papers on disk: xml + non-auxiliary pdf stems (skip _am / _ocr twins)."""
    p = Path(papers_dir)
    if not p.exists():
        return set()
    xml = {f.stem for f in p.glob("*.xml")}
    pdf = {f.stem for f in p.glob("*.pdf") if not (f.stem.endswith("_am") or f.stem.endswith("_ocr"))}
    return xml | pdf


def main():
    ap = argparse.ArgumentParser(description="Per-paper extraction health and record counts.")
    ap.add_argument("--outputs-dir", default=str(DEFAULT_OUTPUTS))
    ap.add_argument("--papers-dir", default=str(DEFAULT_PAPERS))
    args = ap.parse_args()

    out_dir = Path(args.outputs_dir)
    outputs = sorted(out_dir.glob("*_extraction.json")) if out_dir.exists() else []

    rows, totals, orphans = [], {"ok": 0, "empty": 0, "failed": 0, "malformed": 0}, []
    total_records = 0
    have_stems = set()
    for f in outputs:
        stem = f.name.removesuffix("_extraction.json")
        status, n, fmt = _status(f)
        totals[status] = totals.get(status, 0) + 1
        total_records += n
        have_stems.add(stem)
        if stem.endswith("_am") or stem.endswith("_ocr"):
            orphans.append(f.name)
        rows.append((stem, fmt, status, n))

    print(f"outputs dir: {out_dir}   ({len(outputs)} output file(s))\n")
    print(f"{'paper':52} {'format':22} {'status':10} {'records':>7}")
    print("-" * 95)
    for stem, fmt, status, n in rows:
        print(f"{stem[:52]:52} {str(fmt)[:22]:22} {status:10} {n:>7}")

    # papers present on disk with no output at all
    present = _present_paper_stems(args.papers_dir)
    missing = sorted(present - have_stems)
    if missing:
        print("\nMISSING (paper file on disk, no extraction output):")
        for s in missing:
            print(f"  {s}")

    if orphans:
        print("\nORPHAN _am/_ocr outputs (duplicates from an old run — safe to delete):")
        for o in orphans:
            print(f"  {o}")

    print("\n── TOTALS ──")
    print(f"  papers with output: {len(outputs)}   "
          f"(ok {totals['ok']}, empty {totals['empty']}, failed {totals['failed']}, "
          f"malformed {totals['malformed']})")
    print(f"  missing (no output): {len(missing)}")
    print(f"  GRAND TOTAL RECORDS: {total_records}")


if __name__ == "__main__":
    main()