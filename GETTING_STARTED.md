# Getting Started — Setting Up and Running This Pipeline From Scratch

This guide assumes **zero prior experience** with this codebase, Python tooling, or Google Cloud. Follow it top to bottom in order. Each step tells you exactly what to install, what account you need, what command to type, and what you should see if it worked.

If you want to understand *how the pipeline works internally* (what each script does, what bugs were found and fixed, why certain design choices were made), read `PIPELINE_EXPLAINED.md` after this — this document is only about getting it running on your machine.

---

## What this pipeline actually does (one paragraph)

You give it a list of scientific paper DOIs. It downloads each paper (full text + figures), sends the content to Google's Gemini AI with instructions to extract specific structured data (composition, grain size, mechanical properties, etc.) into a fixed set of fields, and saves that as JSON. A second script then takes that JSON and computes additional materials-science descriptors (things like valence electron concentration, enthalpy of mixing, elastic moduli from a public materials database) and writes everything out as one big spreadsheet-ready CSV file. Every step is designed to never guess a value — anything not explicitly reported in a paper is left blank (`null`), not filled in with an estimate.

---

## Part 1 — Things you need before you touch any code

Check these off one at a time. Don't skip ahead — later steps assume all of these already work.

### 1.1 — Install Python

You need **Python 3.11 or 3.12**. (Note: this project has been run successfully on Python 3.14 too, but 3.11/3.12 is the smoother path — some scientific packages this project depends on are slower to release support for brand-new Python versions, and you may hit installation friction on 3.14 that 3.11/3.12 avoids entirely. If you already have 3.14 and don't want to install another version, that's fine — just see the Troubleshooting section if package installation fails.)

Download from [python.org/downloads](https://www.python.org/downloads/). During installation on Windows, **check the box that says "Add Python to PATH"** — this is the single most common setup mistake.

Verify it worked by opening a terminal (PowerShell on Windows) and running:
```powershell
python --version
```
You should see something like `Python 3.12.4`.

### 1.2 — Install Git

Needed to download (clone) this codebase. Download from [git-scm.com](https://git-scm.com/downloads). Verify:
```powershell
git --version
```

### 1.3 — Install the Google Cloud CLI (`gcloud`)

This is how you'll authenticate to use Gemini. Download and install from [cloud.google.com/sdk/docs/install](https://cloud.google.com/sdk/docs/install). Verify:
```powershell
gcloud --version
```

### 1.4 — Get access to a Google Cloud project with Vertex AI enabled

This pipeline calls Gemini through **Vertex AI**, not a personal API key. You have two options:

- **You're joining an existing team/project** (e.g. continuing this exact ETH research project): ask whoever administers the Google Cloud project to grant your account the `roles/aiplatform.user` role on their project. You'll need their exact project ID (for this project it's `matmodel-literaturemining-govc`) and the region they use (`europe-west4` for this project — EU data residency).
- **You're starting your own, separate project:** create a new Google Cloud project at [console.cloud.google.com](https://console.cloud.google.com), enable billing, and enable the **Vertex AI API** (`aiplatform.googleapis.com`) for it. Note down your project ID and pick a region.

Either way, you'll need this project ID and region in Part 3.

### 1.5 — Get an Elsevier API key (only needed if you're pulling papers from Elsevier/ScienceDirect journals)

Register a free developer account at [dev.elsevier.com](https://dev.elsevier.com) and create an API key. **Having the key alone is not enough to get full-text papers** — you (or your institution) also need an actual subscription entitlement to that journal, and you generally need to be on your institution's network or VPN for Elsevier to recognize you as entitled. If you don't have institutional access, that's fine — the pipeline has a separate route for open-access papers that doesn't need this key at all (see Part 5).

### 1.6 — Get a Materials Project API key (only needed for the feature-enrichment step)

Free account at [materialsproject.org](https://materialsproject.org) — after logging in, your API key is on your dashboard. This is only used by the third pipeline stage (`pipeline.py`) to pull elastic/thermodynamic data; the first two stages (fetching and extracting papers) work fine without it.

---

## Part 2 — Get the code onto your machine

```powershell
git clone <the repository URL you were given>
cd API_Extraction_D-MATL
```

(If you were handed this as a folder directly rather than a git repository, just open a terminal inside that folder instead — everywhere below, "the project root" means this folder, the one containing `README.md` and the `scripts/` folder.)

---

## Part 3 — Install the Python packages this project needs

From the project root:
```powershell
pip install -r requirements.txt
```

This installs everything the scripts import: the Gemini/Vertex AI client, PDF rendering, Word document reading, YAML parsing, the `mendeleev` chemistry database, `pymatgen` (chemistry formula parsing), and `mp-api` (Materials Project client).

**This step can take a few minutes and may print warnings about dependency version conflicts — that's usually fine.** If it fails outright with an error, see Troubleshooting below (there's a known issue on very new Python versions).

---

## Part 4 — Set up your credentials

### 4.1 — Create your `.env` file

In the project root, create a new file literally named `.env` (no filename before the dot). Paste this in, replacing the placeholder values with your own:

```env
ELSEVIER_API_KEY=your_elsevier_key_here
MP_API_KEY=your_materials_project_key_here
GOOGLE_CLOUD_PROJECT=your-gcp-project-id-here
GOOGLE_CLOUD_LOCATION=europe-west4
GOOGLE_GENAI_USE_ENTERPRISE=True
```

- `ELSEVIER_API_KEY` — from step 1.5. If you're not using Elsevier papers at all, you can leave this as a placeholder string, but `fetch_papers.py` will refuse to start without *something* here — put a dummy value if truly unused.
- `MP_API_KEY` — from step 1.6. If left unset, the feature-enrichment stage still runs fine, it just skips the Materials Project columns.
- `GOOGLE_CLOUD_PROJECT` — the exact project ID from step 1.4.
- `GOOGLE_CLOUD_LOCATION` — the region your project uses. `europe-west4` if you were told to match this project's ETH setup; otherwise whatever region your own Vertex AI project uses.

**Critical rule: never commit `.env` to git, never share it, never paste its contents anywhere public.** These are real credentials. If one ever leaks (e.g. accidentally pushed to a public GitHub repo), rotate it immediately — go generate a new key and revoke the old one at its source (dev.elsevier.com, materialsproject.org, or your Google Cloud console).

### 4.2 — Authenticate with Google Cloud (do this once per machine)

```powershell
gcloud auth application-default login
```

A browser window opens — log in with the Google account that has access to your Vertex AI project (for the ETH project specifically: your ETH-issued email, e.g. `you@ethz.ch` — **not** a personal Gmail or student-suffix address, those won't have the right permissions).

Then set your quota project so API usage is billed/tracked correctly:
```powershell
gcloud auth application-default set-quota-project your-gcp-project-id-here
```

That's it — after this, every script automatically uses these credentials. There's no API key to paste anywhere for Gemini itself.

---

## Part 5 — Run it for real: a full walkthrough with one paper

Everything happens in three stages, run as three separate commands, always in this order. Do a full pass with just one paper first so you know what "working" looks like before doing anything in bulk.

### Stage 1 — Fetch the paper

Open `scripts/fetch_papers.py` in a text editor. Near the top you'll see:

```python
DOIS = [
    "10.1016/j.jnucmat.2020.152664",
]
```

Replace this with the DOI(s) of the paper(s) you want. A DOI looks like `10.1234/journal.name.year.number` — you can usually copy it directly from the paper's page on the publisher's website.

- If the DOI starts with `10.1016/` (an Elsevier journal), you'll need to be connected to your institution's VPN for full-text access.
- If it's from a different publisher, no VPN is needed — the script automatically tries to find a free, legal open-access copy via Unpaywall instead.

Then run:
```powershell
python scripts/fetch_papers.py
```

Watch the output. For an Elsevier paper you should see something like:
```
Fetching: 10.1016/j.jnucmat.2020.152664
  Publisher: Elsevier -> XML route
  Status code: 200
  Full text saved: 10-1016_j-jnucmat-2020-152664.xml
```

If instead you see `Abstract only`, you're not actually getting full text — usually means you're not on VPN, or your institution doesn't subscribe to that journal.

**What to check afterward:** open `data/papers/` — you should see a new `.xml` or `.pdf` file matching your DOI (slashes and dots get replaced with underscores/dashes in the filename).

### Stage 2 — Extract structured data with Gemini

```powershell
python scripts/extract_data.py --batch
```

`--batch` processes every paper that's been fetched but not yet extracted — safe to run repeatedly, it automatically skips anything already done. (To process just one specific paper instead: `python scripts/extract_data.py 10.1016/j.jnucmat.2020.152664`.)

This step calls Gemini and takes anywhere from 30 seconds to a couple of minutes per paper depending on length and figure count. You'll see live progress: figures being downloaded, figures being classified, then the extraction call itself, then a summary table printed to your terminal listing every material record it found with its key values.

**What to check afterward:** open `data/outputs/{your-doi}_extraction.json` in a text editor. You'll see a `records` array — one JSON object per distinct material/specimen the paper describes, with dozens of fields filled in where the paper reported them and `null` everywhere it didn't. **Always read through this before trusting it** — this is the file a human should sanity-check before any of this data goes into a real dataset.

### Stage 3 — Compute additional features

```powershell
python scripts/pipeline.py
```

This reads every extraction JSON currently in `data/outputs/` (not just the one you just did) and produces two files:
- `data/features_output.csv` — one row per material record, with dozens of extra computed columns appended (composition breakdowns, elastic moduli looked up from a public database, thermodynamic mixing quantities, etc.)
- `data/feature_log.txt` — a plain-text explanation of exactly what was computed for every single record, and why anything was skipped

**Open `data/features_output.csv` in Excel or Google Sheets to see your final result.** Cross-check anything that looks odd against `data/feature_log.txt` — it will tell you, in plain English, why a given value is blank if one is.

---

## Part 6 — Adapting this for your own project

If you're using this codebase as a starting point for a *different* material system or dataset (not tungsten DBTT specifically), the two things to change are:

1. **The schema** — `schemas/w_dbtt/schema.yaml` defines every field Gemini extracts and all the domain-specific extraction rules (units, definitions, how to compute derived quantities). Copy this file to a new folder under `schemas/`, rewrite the `fields:` list for what your project needs, and rewrite `domain_rules:` with your own field. Then point to it: `python scripts/extract_data.py --batch --schema schemas/your_project/schema.yaml`. You never need to touch the Python code in `extract_data.py` itself to change what gets extracted.

2. **The feature-enrichment tables** — `scripts/mendeleev_features.py` and `scripts/usfe_features.py` contain small, explicitly-cited lookup tables (binary mixing-enthalpy values, stacking-fault energies) that were sourced from specific published papers for the tungsten-alloy elements this project actually encounters. If your project involves different elements or a different physical descriptor entirely, these tables will need new entries — and whoever adds them should look up and cite the actual source, not estimate. This project's rule throughout has been: never invent a numeric constant, always trace it to a real, citable source, and leave a value blank rather than guess if the real source isn't available yet.

---

## Troubleshooting

### `ELSEVIER_API_KEY not found in environment` / `GOOGLE_CLOUD_PROJECT not set`
Your `.env` file is missing, misnamed, or in the wrong folder. It must be named exactly `.env` (not `.env.txt`) and sit directly in the project root — the same folder as `README.md`.

### `Reauthentication is needed. Please run 'gcloud auth application-default login' to reauthenticate.`
Your Google Cloud login session expired (this happens periodically, not a real problem). Just run the login command again:
```powershell
gcloud auth application-default login
```
then re-run whatever script failed.

### `403 PERMISSION_DENIED` from Vertex AI
Either your account doesn't have the `aiplatform.user` role on the project yet (ask whoever administers it), or you haven't set a quota project — run:
```powershell
gcloud auth application-default set-quota-project your-gcp-project-id-here
```

### `Abstract only` XML saved instead of full text
You're not on your institution's VPN, or your institution doesn't subscribe to that specific journal, or the paper is actually published somewhere other than where you think. Connect to VPN and re-run `fetch_papers.py` for that DOI.

### `No paper file found for DOI: ...` when running `extract_data.py`
You haven't fetched that paper yet — run `fetch_papers.py` first. For a paywalled non-Elsevier paper with no open-access copy, `fetch_papers.py` will tell you the exact filename to save a manually-downloaded PDF as, and to add the DOI to `data/papers/manual_papers.json`.

### A `PermissionError` mentioning a `.csv` file when running `pipeline.py`
The output CSV is currently open in Excel or another program, which locks the file on Windows so Python can't overwrite it. Close the file and re-run.

### `yaml.parser.ParserError` when running `extract_data.py`
Something in `schema.yaml` has broken indentation — YAML is whitespace-sensitive. This usually happens after manually editing the `domain_rules:` block. Make sure every line inside that block lines up with consistent indentation (the file uses 2-space indents throughout).

### Package installation fails on a very new Python version (e.g. 3.14+)
Some scientific packages (particularly `mendeleev`'s dependency chain) can lag behind the newest Python releases. If `pip install -r requirements.txt` fails outright, try:
```powershell
pip install numpy
pip install mendeleev --no-deps
pip install sqlalchemy pyfiglet pygments
pip install -r requirements.txt
```
If that still doesn't work, the more reliable fix is installing Python 3.11 or 3.12 alongside your existing Python and using that specifically for this project (you can have multiple Python versions installed at once without conflict).

### Extraction returns records but `DBTT_K`, grain sizes, etc. are all `null`
This usually means Gemini genuinely couldn't find that data in the paper's text or figures — check the `notes` field of that record, which is required to explain what was and wasn't readable. It is not a bug if a paper simply doesn't report a value; the pipeline is deliberately built to never fill in a guess.

### Something else entirely broke
Check `data/feature_log.txt` (for `pipeline.py` issues) or the console output right above the error (for `fetch_papers.py`/`extract_data.py` issues) — both are written specifically to explain, in plain language, what happened and why, before you go digging into the actual Python code.
