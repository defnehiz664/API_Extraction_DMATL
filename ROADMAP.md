# Pipeline Roadmap — Feasibility, Architecture, and Game Plan

**Current state:** Single-researcher tungsten DBTT pipeline. Gemini reads Elsevier XML or PDF,
extracts ~60 hardcoded fields, classifies figures by type, saves structured JSON.

**Target state:** Multi-student platform where each student brings their own material system,
feature set, and source corpus — and the pipeline finds data wherever it lives, including
inside graphs and in datasets referenced by the papers.

---

## Contents

1. [Feature 1 — Data extraction from graphs](#1-data-extraction-from-graphs)
2. [Feature 2 — Per-student configurable schema](#2-per-student-configurable-schema)
3. [Feature 3 — Referenced dataset discovery and retrieval](#3-referenced-dataset-discovery-and-retrieval)
4. [Integration architecture](#4-integration-architecture)
5. [Recommended sequencing](#5-recommended-sequencing)

---

## 1. Data Extraction from Graphs

### What the student expects

Most values of interest (grain size vs. temperature, DBTT vs. dose, hardness vs. strain)
are not stated in the text — they live on axes of scatter plots or line graphs. She needs
the pipeline to read coordinates off those graphs.

### Feasibility assessment — updated after empirical test

**Tested 2026-07-01:** Gemini read a clean stress-strain curve (Flow Stress MPa vs. Strain %)
and achieved ≤1 MPa error across the full curve. This is better than typical manual digitization.

| Approach | Accuracy (clean figure) | Accuracy (complex figure) | Effort | Verdict |
|---|---|---|---|---|
| Gemini direct read | ≤1 MPa demonstrated | Degrades (see below) | Zero — already built | **Primary approach** |
| WebPlotDigitizer API | ±1–3% | ±1–3% | Medium | Fallback only |
| Manual WebPlotDigitizer | <1% | <1% | Low per graph | Last resort |

**Gemini is the primary approach.** The digitizer pipeline is not needed for the common case.

**Cases where Gemini accuracy will degrade — keep WebPlotDigitizer as a fallback for these:**
- Multiple overlapping series with similar colors or densely packed symbols
- Log-scale axes (a misread tick can cause order-of-magnitude errors)
- Very dense scatter plots (100+ discrete points — Gemini samples, it doesn't enumerate all)
- Figures reproduced at very small print size in the original PDF

For these edge cases the student should run WebPlotDigitizer manually or flag the record
for manual correction. The pipeline should make this easy by tagging graph-read records
with a confidence level.

### Architecture (revised — Gemini-primary)

```
FIGURE IMAGE
     │
     ▼
[Gemini — Graph Detection + Read]
  ┌──────────────────────────────────────────┐
  │ "Does this figure contain a graph?"      │
  │ → yes: extract axis labels, units,       │
  │        series labels, all data points    │
  │        as (x, y, series) tuples          │
  │        confidence: high / low            │
  │ → no:  skip graph extraction             │
  └──────────────────────────────────────────┘
     │
     ├── high confidence ──► use values directly, tag [graph_read][high]
     │
     └── low confidence ───► use values, tag [graph_read][low]
                              student reviews flagged records in Excel
                              fallback: WebPlotDigitizer manually if critical
```

### What to implement now

Add graph-reading instructions to the extraction prompt in `extract_data.py`:

```
For any figure that shows a graph or plot:
- Read the axis labels and units carefully
- Extract all data points as (x_value, y_value, series_label) tuples
- For continuous curves: extract enough points to reconstruct the curve shape
  (aim for ~10–20 points per series, more at inflection points)
- For discrete scatter plots: extract every visible data point
- Flag confidence per figure:
    high = clear gridlines, unambiguous axis ticks, single series
    low  = overlapping series, log scale, small symbols, poor image quality
- Record as: source_figure_or_table = "Fig.3a [graph_read]"
             notes includes "[graph_read][high]" or "[graph_read][low]"
```

### Game plan

```
Phase G1 (now, 0.5 days) — PRIMARY:
  Add graph-reading instructions to extraction prompt.
  Test on 5 figures from your current corpus.
  Accept output directly for high-confidence reads.

Phase G2 (if and when log-scale / multi-series figures appear):
  Install WebPlotDigitizer as a fallback tool (not automated).
  Student runs it manually on specific flagged figures.
  No engineering required — it is a standalone GUI tool.

Phase G3 (only if manual fallback becomes a bottleneck at scale):
  Automate WebPlotDigitizer via its Python subprocess API.
  Triggered only for figures tagged [graph_read][low].
```

---

## 2. Per-Student Configurable Schema

### What the student expects

Each student studies a different material system with different relevant fields. One needs
grain boundary character distribution; another needs irradiation dose and He bubble density.
The pipeline should adapt to each person's feature set without requiring code changes.

### Honest feasibility assessment

**Fully feasible.** This is a configuration problem, not an AI problem. The current pipeline's
only hardcoded part is the `MaterialRecord` Pydantic class. Making it schema-driven requires:

1. A schema config file (YAML) per student/project
2. Runtime Pydantic model generation from that config
3. Prompt generation that describes only the student's fields to Gemini
4. An Excel template that matches the schema

### Schema config format (proposed)

Each student maintains a file like `schemas/my_project.yaml`:

```yaml
project: "W_irradiation_embrittlement"
material_system: "Tungsten under neutron irradiation"
record_id_prefix: "WI"

fields:
  - name: material_name
    type: str
    description: "Material name (e.g. pure W, W-Re)"
    required: true

  - name: irradiation_dose_dpa
    type: float
    description: "Irradiation dose in displacements per atom (dpa)"
    unit: dpa

  - name: irradiation_temperature_K
    type: float
    description: "Temperature during irradiation in Kelvin"
    unit: K

  - name: he_bubble_density_per_m3
    type: float
    description: "Helium bubble number density per cubic metre"
    unit: m^-3

  - name: DBTT_K
    type: float
    description: "Ductile-to-brittle transition temperature in Kelvin"

  - name: source_DOI
    type: str
    required: true
```

### Dynamic Pydantic model generation

```python
# schema_loader.py
import yaml
from pydantic import create_model
from typing import Optional

def load_schema(yaml_path: str):
    with open(yaml_path) as f:
        config = yaml.safe_load(f)

    type_map = {"str": str, "float": float, "int": int, "bool": bool}
    field_definitions = {}

    for field in config["fields"]:
        py_type = type_map.get(field["type"], str)
        field_definitions[field["name"]] = (Optional[py_type], None)

    model = create_model(config["project"], **field_definitions)
    return model, config
```

Gemini receives a prompt that describes only the fields in this student's schema —
no tungsten-specific language, no DBTT-specific rules unless they apply.

### Architecture for multi-student use

```
schemas/
  └── w_dbtt/               ← your current project
      └── schema.yaml
  └── w_irradiation/        ← new student
      └── schema.yaml
  └── hea_strength/         ← another student
      └── schema.yaml

data/
  ├── w_dbtt/
  │   ├── papers/
  │   ├── figures/
  │   └── outputs/
  ├── w_irradiation/
  │   ├── papers/
  │   ├── figures/
  │   └── outputs/
  ...
```

Usage:
```bash
python scripts/extract_data.py --schema schemas/w_irradiation/schema.yaml --batch
```

### What does NOT need to change

- `fetch_papers.py` — paper fetching is schema-agnostic
- Figure saving and classification — schema-agnostic
- Vertex AI setup — shared GCP project, each student authenticates separately

### Game plan

```
Phase S1 (medium term, 3–4 days):
  Convert your current MaterialRecord into schemas/w_dbtt/schema.yaml.
  Build schema_loader.py that generates a Pydantic model from YAML.
  Test that extraction with the generated model matches current output exactly.

Phase S2:
  Add --schema flag to extract_data.py (defaults to schemas/w_dbtt/schema.yaml
  for backwards compatibility).
  Prompt generation becomes schema-aware: field descriptions from YAML are
  injected into the extraction prompt automatically.

Phase S3:
  Per-project data directories.
  Excel template generator that creates a Sheet2 with the right column headers
  from the schema YAML.
```

---

## 3. Referenced Dataset Discovery and Retrieval

### What the student expects

Papers often say "compared with the dataset of Bonnekoh et al. [14]" or "data replotted
from [5, 8, 12]". She wants the pipeline to notice this, find those source papers, and
extract data from them too — without manually hunting down 12 references.

### Honest feasibility assessment

This is a **multi-step problem** with very different difficulty levels at each step:

| Step | What it involves | Feasibility | Accuracy |
|---|---|---|---|
| A. Identify in-text dataset citations | Gemini reads text and flags "data from [X]" | High | ~85–90% |
| B. Extract reference metadata (author, year, title) | Parse reference list from XML/PDF | Medium | ~90%+ for XML |
| C. Resolve to DOI | CrossRef API lookup by title+author | Medium | ~70–80% |
| D. Fetch the referenced paper | Existing fetch pipeline | High (already built) | — |
| E. Extract data from that paper | Existing extract pipeline | High (already built) | — |
| F. Avoid infinite cascade | Stop after depth=1 or depth=2 | Easy | — |

**Steps A–B are the hardest in practice.** Authors write "data from [5]" but reference [5]
may be a review paper that itself cites three primary datasets. Gemini will occasionally
confuse "we cite this paper for context" with "we took data points from this paper."

**Step C (DOI resolution) is imperfect.** CrossRef finds DOIs by fuzzy title match but
fails on older papers, proceedings, and non-English titles (~20–30% miss rate).

### What "reference tracking" can realistically do

**Tier R1 — Citation flagging (easy, high value):**
Gemini identifies passages like "DBTT values taken from [5, 8]" and adds to each extracted
record: `data_from_reference: ["[5]", "[8]"]` and `reference_titles: ["Smith 2019", "Jones 2021"]`.
Student manually decides whether to chase those papers. No automation yet but no missed
provenance either.

**Tier R2 — Reference list extraction (medium):**
For Elsevier XML, references are structured in the `<bibliography>` section with author, year,
title, and sometimes DOI already included. Parser extracts these and writes a
`references_{doi}.json`. Cross-links with Tier R1 output to produce:
"Record W_014 draws from: Smith 2019 (10.1016/...), Jones 2021 (unknown DOI)".

**Tier R3 — Auto-cascade fetch (high complexity, use with caution):**
For each resolved DOI not already in the corpus, run `fetch_paper()` automatically.
Must be gated behind a `--follow-references` flag and a depth limit (`--max-depth 1`)
because one paper can reference 40 others, each of which references 40 more.

### Reference cascade control

```
                  Paper A (DOI you gave)
                  └── references [5], [8], [14]
                            │
                    depth=1 auto-fetch
                    (if --follow-references)
                            │
              ┌─────────────┼─────────────┐
           Paper [5]    Paper [8]     Paper [14]
              │
          depth=2 — STOP (never go deeper without explicit flag)
```

Without a depth cap, the reference graph of a mature field can easily reach 500+ papers.

### Game plan

```
Phase R1 (now, 0.5 days):
  Add to extraction prompt:
    "For each extracted record, list any reference numbers the authors
     cite as the SOURCE of that specific data point (not background citations).
     Return as data_from_reference: ['[5]', '[8]']"
  Adds provenance tagging with zero extra API calls.

Phase R2 (medium term, 1 week):
  Build reference_parser.py:
    - For XML: parse <bibliography> section, extract author/year/title/DOI
    - For PDF: ask Gemini to extract reference list as JSON
    - Cross-link with R1 output
    - Save as data/outputs/{doi}_references.json
  Build CrossRef resolver:
    - requests.get("https://api.crossref.org/works?query.title=...&query.author=...")
    - Returns DOI candidates with confidence score
    - Flag any match below 90% confidence for manual review

Phase R3 (long term, only if R1+R2 prove valuable):
  Add --follow-references flag to fetch_papers.py
  Reads data/outputs/{doi}_references.json
  Queues resolved DOIs into fetch pipeline
  --max-depth 1 enforced as hard limit
  Student reviews fetch_log before running extraction on cascade papers
```

---

## 4. Integration Architecture

### Current architecture (your pipeline today)

```
┌─────────────────────────────────────────────────────┐
│                 RESEARCHER INPUT                     │
│   DOI list in fetch_papers.py                       │
└───────────────────┬─────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────────────┐
│              fetch_papers.py                        │
│  Elsevier DOI ──► ScienceDirect API ──► .xml        │
│  Other DOI ─────► Unpaywall ──────────► .pdf        │
│                              (or manual placement)  │
│  Saves fetch_log.json                               │
└───────────────────┬─────────────────────────────────┘
                    │
              data/papers/
                    │
                    ▼
┌─────────────────────────────────────────────────────┐
│              extract_data.py                        │
│                                                     │
│  XML ──► body text + figure download                │
│  PDF ──► page rendering (PyMuPDF, 200 DPI)          │
│  DOCX ──► supplementary text extraction             │
│                    │                                │
│          [Gemini Call 1: Classify figures]          │
│          microstructure / DBTT_curve / EBSD / ...   │
│                    │                                │
│          [Gemini Call 2: Extract data]              │
│          MaterialRecord × N records                 │
│                    │                                │
│  Saves: outputs/{doi}.json                          │
│         figures/{doi}/{type}/                       │
└───────────────────┬─────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────────────┐
│           HUMAN REVIEW → EXCEL (Sheet2)             │
└─────────────────────────────────────────────────────┘
```

---

### Target architecture (multi-student, all three features)

```
┌──────────────────────────────────────────────────────────────────────┐
│                        PROJECT CONFIG                                │
│   schemas/{project}/schema.yaml  ← student defines their fields     │
│   schemas/{project}/prompt.md    ← optional domain-specific rules   │
└────────────────────────────┬─────────────────────────────────────────┘
                             │
                             ▼
┌──────────────────────────────────────────────────────────────────────┐
│                      fetch_papers.py                                 │
│                    (unchanged, schema-agnostic)                      │
│                                                                      │
│   Elsevier ──────► ScienceDirect API ──► .xml + .docx               │
│   Other ─────────► Unpaywall ──────────► .pdf                       │
│                                  (or manual)                         │
│                                                                      │
│   --follow-references [FUTURE]                                       │
│   reads outputs/{doi}_references.json                                │
│   queues newly resolved DOIs                  ┐                      │
│                                               │ cascade              │
│   ◄────────────────────────────────────────── ┘ (depth ≤ 1)         │
└────────────────────────────┬─────────────────────────────────────────┘
                             │
                       data/{project}/papers/
                             │
                             ▼
┌──────────────────────────────────────────────────────────────────────┐
│                      extract_data.py                                 │
│                                                                      │
│   schema_loader.py                                                   │
│   reads schema.yaml ──► dynamic Pydantic model                      │
│   generates schema-aware extraction prompt                           │
│                                                                      │
│   ┌─────────────────────────────────────────────────────┐           │
│   │  CONTENT ASSEMBLY                                   │           │
│   │  XML ──► body text + figure download               │           │
│   │  PDF ──► page render (PyMuPDF)                     │           │
│   │  DOCX ──► supplementary text                       │           │
│   └────────────────────┬────────────────────────────────┘           │
│                        │                                             │
│                        ▼                                             │
│   ┌─────────────────────────────────────────────────────┐           │
│   │  Gemini Call 1 — FIGURE TRIAGE                      │           │
│   │  a) Classify by type                                │           │
│   │     microstructure / DBTT_curve / EBSD / ...        │           │
│   │  b) Flag graphs with extractable numerical data     │           │
│   │  c) Extract axis labels, units, series labels       │           │
│   └────────┬─────────────────────┬───────────────────── ┘           │
│            │                     │                                   │
│    non-graph figures         graph figures                           │
│            │                     │                                   │
│            │                     ▼                                   │
│            │       ┌─────────────────────────────────┐              │
│            │       │  graph_digitizer.py [FUTURE]    │              │
│            │       │  WebPlotDigitizer API           │              │
│            │       │  axis calibration from Gemini   │              │
│            │       │  output: (x, y, series) CSV     │              │
│            │       └─────────────┬───────────────────┘              │
│            │                     │                                   │
│            └──────────┬──────────┘                                  │
│                       │                                              │
│                       ▼                                              │
│   ┌─────────────────────────────────────────────────────┐           │
│   │  Gemini Call 2 — DATA EXTRACTION                    │           │
│   │  prompt = schema fields + domain rules              │           │
│   │  input = text + figures + digitized graph CSVs      │           │
│   │  output = list[DynamicModel]  (matches schema.yaml) │           │
│   │                                                     │           │
│   │  Per record:                                        │           │
│   │    data_from_reference: ["[5]", "[8]"]   [R1]       │           │
│   │    graph_read_confidence: "high"/"low"   [G1]       │           │
│   └─────────────────────────────────────────────────────┘           │
│                        │                                             │
│   ┌─────────────────────────────────────────────────────┐           │
│   │  reference_parser.py [FUTURE R2]                   │           │
│   │  parse bibliography from XML or Gemini             │           │
│   │  CrossRef DOI resolution                           │           │
│   │  output: outputs/{doi}_references.json             │           │
│   └─────────────────────────────────────────────────────┘           │
│                        │                                             │
│   Saves:                                                             │
│     data/{project}/outputs/{doi}_extraction.json                    │
│     data/{project}/figures/{doi}/{type}/                            │
│     data/{project}/outputs/{doi}_references.json  [FUTURE]          │
└────────────────────────┬─────────────────────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────────────────────┐
│                   HUMAN REVIEW LAYER                                 │
│                                                                      │
│   data/{project}/outputs/{doi}_extraction.json                       │
│     ├── records[]           ← main extraction                        │
│     ├── figure_classifications[]  ← type + description               │
│     ├── graph_digitized[]   ← raw coordinates [FUTURE]              │
│     └── references[]        ← resolved DOIs + confidence [FUTURE]   │
│                                                                      │
│   Student reviews JSON → copies to Excel (Sheet2)                    │
│   [FUTURE] write_to_excel.py automates this step                    │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 5. Recommended Sequencing

The table below ranks by value-to-effort ratio given the current codebase.

```
┌────┬──────────────────────────────────────┬──────────┬────────────┬──────────────────────────────┐
│ #  │ Task                                 │ Effort   │ Value      │ Dependency                   │
├────┼──────────────────────────────────────┼──────────┼────────────┼──────────────────────────────┤
│ 1  │ Graph-reading prompt (G1)            │ 0.5 day  │ Very high  │ None — Gemini accuracy proven │
│ 2  │ Fix batch mode: skip abstract_only   │ 0.5 day  │ High       │ None — known bug             │
│ 3  │ Citation flagging in records (R1)    │ 0.5 day  │ High       │ None — prompt only           │
│ 4  │ Schema YAML + dynamic Pydantic (S1)  │ 3–4 days │ Very high  │ None                         │
│ 5  │ Per-project data directories (S2)    │ 1 day    │ High       │ S1                           │
│ 6  │ write_to_excel.py                    │ 2 days   │ High       │ S1                           │
│ 7  │ XML reference parser (R2 partial)    │ 2 days   │ Medium     │ None                         │
│ 8  │ CrossRef DOI resolver (R2 full)      │ 2 days   │ Medium     │ R2 partial                   │
│ 9  │ --follow-references cascade (R3)     │ 1 week   │ Medium     │ R2 full                      │
│ 10 │ WebPlotDigitizer (G2, fallback only) │ As needed│ Low        │ Needed only for log/multi    │
└────┴──────────────────────────────────────┴──────────┴────────────┴──────────────────────────────┘
```

Note: WebPlotDigitizer has been **demoted to last place**. Gemini direct reading is accurate
enough (~1 MPa error demonstrated empirically on 2026-07-01) that the digitizer is only needed
as a manual fallback for pathological figures (log-scale, 5+ overlapping series).

### What to do right now (this week)

Items 1, 2, 3 are all prompt edits or one-line code fixes. Do these first — G1 in particular
is now confirmed to work well and should go in immediately.

Items 4–5 (configurable schema) are the most important architectural decision. Everything else
(multi-student directories, Excel writer, reference cascade) depends on the schema structure
being settled. Do not build R2/R3 before S1 or you will restructure it twice.

### What to defer

WebPlotDigitizer automation (old G2/G3) is now low priority. If a specific figure type
consistently produces [graph_read][low] results in practice, revisit then. Do not build it
speculatively.

Reference cascade (R3) should wait until at least 20 papers are in the corpus. At small
scale, the student can track references manually. The system earns its complexity only
when the paper count makes manual tracking impossible.

---

*Last updated: 2026-07-01*