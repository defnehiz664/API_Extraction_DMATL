# How This Pipeline Works — Complete Walkthrough

This document explains the entire pipeline end to end: what happens from the moment you put a DOI in, to the moment you get a finished CSV row out. It also documents every bug that was found and fixed, why it mattered, and what it means for the data you're producing. Read it top to bottom once, then use it as a reference.

The pipeline has **three stages**, run as three separate commands, in this order:

```
1. python scripts/fetch_papers.py        (get the paper's raw text/figures onto disk)
2. python scripts/extract_data.py        (Gemini reads the paper -> structured JSON)
3. python scripts/pipeline.py            (enrich the JSON with computed materials-science features -> CSV)
```

Each stage reads what the previous stage wrote to `data/`. Nothing is automatic between them — you run each one on purpose, and you should look at the output before moving to the next stage.

---

## STAGE 1 — `fetch_papers.py`: get the paper

**What you do:** open `scripts/fetch_papers.py`, edit the `DOIS` list near the top (line ~61) to contain the DOI(s) you want, and run:

```
python scripts/fetch_papers.py
```

**What it does, step by step:**

1. Reads `ELSEVIER_API_KEY` from `.env`. Refuses to run at all if it's missing.
2. For each DOI in your list, checks: does it start with `10.1016/`?
   - **Yes (Elsevier)** → calls the Elsevier ScienceDirect API directly, requesting the full-text XML. You must be on ETH VPN for this to return full text — off VPN, Elsevier still returns HTTP 200 but with abstract-only content, and the script detects this and saves it as `..._abstract_only.xml` instead, with a note explaining the likely cause (not on VPN, no subscription, or wrong publisher).
   - **No (non-Elsevier)** → calls Unpaywall (`api.unpaywall.org`) to look for a free open-access PDF. If found, downloads it directly. If not found, it does **not** guess or scrape anywhere else — it prints instructions telling you to manually download the PDF and save it as `data/papers/{doi_slug}.pdf` yourself.
3. If a full-text XML was successfully saved, it also looks inside that XML for supplementary file attachments (`.docx` or `.pdf` — usually where authors put extra tables of data) and downloads those too, named `{doi_slug}_mmc1.docx` etc. so they're traceable back to their paper.
4. Writes a result file per DOI to `data/papers/` (`.xml`, `.pdf`, `_abstract_only.xml`, or `_error.txt` depending on outcome), and one summary file: `data/papers/fetch_log.json` — **this file is what Stage 2 reads to know which papers are ready.**

**What you get after Stage 1:** raw paper files sitting in `data/papers/`. Nothing has been read or interpreted yet — this stage is pure "go get the document."

---

## STAGE 2 — `extract_data.py`: Gemini reads the paper

**What you do:**

```
python scripts/extract_data.py --batch
```

This processes every paper in `fetch_log.json` (plus anything you've manually added to `data/papers/manual_papers.json`) that doesn't already have a matching file in `data/outputs/`. Or, to do just one paper:

```
python scripts/extract_data.py 10.1016/j.jmst.2026.01.050
```

**What it does, step by step, for each paper:**

### Step 1 — Load the schema
Reads `schemas/w_dbtt/schema.yaml`. This file defines **every field Gemini is asked to fill in** (68 fields currently) plus a big block of free-text `domain_rules` (unit conventions, how to compute derived quantities, what "pure/doped/alloy" means, etc.) that gets pasted directly into the prompt. `schema_loader.py` turns this YAML into two things: (a) a dynamically-generated Pydantic model, used to force Gemini's JSON output to have exactly these fields and nothing else, and (b) a human-readable "FIELDS TO EXTRACT" bullet list that also goes into the prompt. **If you want to change what gets extracted, edit `schema.yaml` — never hardcode fields into `extract_data.py`.**

### Step 2 — Load the paper content
- If `data/papers/{doi_slug}.xml` exists (Elsevier route): parses the body text out of the XML, and separately finds every real figure (`gr1`, `gr2`, ...) referenced in the XML and downloads each one as an image via the Elsevier API. Graphical abstracts (`ga*`) and inline SVG equations (`si*`) are explicitly excluded.
- If instead `data/papers/{doi_slug}.pdf` exists (non-Elsevier route): renders every page of the PDF as a 200 DPI image. There's no separate body text in this case — Gemini reads the page images directly, text and all.

### Step 3 — Supplementary docx
Looks for `data/papers/{doi_slug}_mmc*.docx` and reads all paragraph and table text out of it (via `python-docx`), appending it to what gets sent to Gemini. This is often where extra data tables live that aren't in the main paper body.

### Step 4 — Connect to Gemini
Creates a Vertex AI client (`genai.Client(vertexai=True, project=..., location="europe-west4")`) — this uses your institutional Google Cloud login (`gcloud auth application-default login`, done once per machine with your `dabiakar@ethz.ch` account), never a personal API key.

### Step 5 — Classify every figure (a separate, smaller Gemini call)
Before the real extraction, every figure is sent to Gemini in its own lightweight call and classified into one of: `microstructure`, `DBTT_curve`, `hardness`, `EBSD`, `fracture_surface`, `map`, `other`. This costs a small amount of tokens but tells the pipeline what kind of image each figure is.

### Step 6 — Save only the figures you actually want kept
**This was changed on your request.** Originally, every figure was saved to disk and sorted into type subfolders. You said you didn't want that anymore — you only wanted figures worth manually reviewing kept, and picked EBSD maps specifically. Now: `save_selected_figures()` only writes a figure to `data/figures/{doi_slug}/` if its classified type is in `SAVED_FIGURE_TYPES = {"EBSD"}` (near the top of `extract_data.py` — add types there if you want more kept in the future). **Important: this only controls what's saved to disk.** Every figure — DBTT curves, hardness plots, everything — is still sent to the real extraction call in the next step, so Gemini still reads and extracts data from all of them. You're just not keeping a personal copy of most of the images afterward.

### Step 7 — The real extraction call
This is the one that matters. It sends: the big prompt (built from `schema.yaml`), the paper's body text (or page images, if PDF), the supplementary docx text if any, and every figure image — all in one request. Gemini is forced (via `response_schema`) to return a JSON array where every object has exactly the 68 fields from the schema, using `null` for anything not reported (Gemini is explicitly told never to guess or invent values).

**A setting called `thinking_config=types.ThinkingConfig(thinking_budget=0)` is set on this call — this was a bug fix, explained in the "Bugs Found and Fixed" section below. Short version: without it, Gemini was silently burning most of its token budget on invisible internal reasoning and its actual JSON answer was getting cut off mid-sentence before every extraction had even finished. Disabling it fixed truncation and made each call cheaper, not more expensive.**

### Step 8 — Save the result
Writes `data/outputs/{doi_slug}_extraction.json`, containing: which model was used, the DOI, how many figures/pages were processed, the figure classifications, and the actual `records` array (one JSON object per material/specimen found in the paper). Prints a summary table to your console so you can eyeball it immediately.

**What you get after Stage 2:** one `..._extraction.json` file per paper in `data/outputs/`, each with a list of extracted material records — this is the file you should open and sanity-check before trusting anything downstream. `data/figures/{doi_slug}/` will contain only the EBSD figures (by default).

---

## STAGE 3 — `pipeline.py`: turn extracted records into ML-ready features

This is a completely separate script from extraction. Its job: take the raw extracted JSON (which has things like `dopant_or_alloying_element: "Re"`, `dopant_concentration: 3`, `dopant_concentration_unit: "wt%"`) and turn that into actual computed numeric descriptors a machine-learning model can use — valence electron concentration, atomic size mismatch, enthalpy of mixing, elastic moduli, stacking fault energy, etc.

**What you do:**

```
python scripts/pipeline.py
```

This reads **every** `*_extraction.json` currently in `data/outputs/` — not just the one you just extracted — and produces `data/features_output.csv` (one row per material record, original 68 fields + all the new computed columns) and `data/feature_log.txt` (a full log of what happened for every single record, including anything skipped or flagged, which you should check after every run).

### Step 1 — Build a clean composition for every record
The raw record has scattered fields like `composition_type`, `dopant_or_alloying_element`, `dopant_concentration`. `composition_builder.py` turns this into one clean dict of atomic (mole) fractions, e.g. `{"W": 0.97, "Re": 0.03}`, that every downstream calculation actually uses. Two ways it can do this:

- **Format A — `composition_string`:** if the record has a field like `"W0.982Re0.018"` (the current schema now asks Gemini to compute and fill this in directly), it's parsed with `pymatgen`'s own chemistry-formula parser. This is the preferred, most reliable path.
- **Format B — reconstruction:** if there's no composition string (or it fails to parse), the fields are reconstructed by hand: `pure` → 100% W. `doped`/`alloy` → read the dopant element(s) and their concentration + unit (`ppm`, `wt%`, or `at%`), convert everything to a consistent basis, and compute the remainder as W. **Important subtlety:** `ppm` is treated as *weight* ppm (standard convention for K-doped tungsten literature), and since potassium is much lighter than tungsten, a given weight-ppm value converts to a noticeably *higher* atomic-fraction value — e.g. 80 wt-ppm K works out to about 376 atomic-ppm K, not 80. This is correct chemistry, not a bug, but it's worth knowing if you're eyeballing numbers.

If a record is too sparse to build any composition at all (e.g. a `doped` record where the dopant element is named but its concentration was never reported), the record is marked **SKIPPED** in the log and carries no computed features — nothing is guessed.

### Step 2 — Look up element properties (once per element, ever)
`element_table.py` pulls physical properties for every element that shows up anywhere in your dataset from the `mendeleev` Python package (atomic radius, melting point, electronegativity, etc. — see the exact list below) and caches them to `data/element_properties.csv`. This file is only ever queried once per element across the whole life of the project — if `W` is already cached, it's never re-fetched. One manually-maintained table sits alongside this: `VEC_TABLE`, the "valence electron concentration" convention used in alloy-design literature (this isn't a standard periodic-table property, it's a metallurgy convention, e.g. W=6, Re=7, Ni=10, Cu=11).

### Step 3 — Compute mixture-rule ("Mendeleev") features
For every element-property column pulled in Step 2, `mendeleev_features.py` computes a **composition-weighted average** across the alloy — e.g. if you have W(97%)+Re(3%), the weighted melting point is `0.97 × (W's melting point) + 0.03 × (Re's melting point)`. This produces the `mendeleev_*_weighted` and `mean_*` columns (see column reference below), plus a `frac_{element}` column for every element present.

Two more specific calculations happen here:
- **`delta_atomic_size_mendeleev`** — a composition-weighted measure of how mismatched the atoms' sizes are (bigger mismatch = more lattice strain), using the Takeuchi-Inoue formula.
- **`delta_H_mix_kJ_mol_mendeleev`** — the actual physics-based enthalpy of mixing, computed from a real published table of binary element-pair interaction energies (Takeuchi & Inoue, 2005 — I looked up and transcribed the exact published values for every element pair your dataset actually contains, rather than guess at them; see "Bugs Found and Fixed" below for how carefully that was verified). If your alloy contains an element pair not in that table, this value is `None` rather than silently treated as zero, and the missing pair is listed in `mendeleev_h_mix_missing_pairs`.

### Step 4 — Query Materials Project (MP) for elastic/thermodynamic data
`mp_features.py` queries the Materials Project database (a public DFT-computed materials database) for the alloy's bulk modulus, shear modulus, formation energy, stability, and crystal structure. This needs `MP_API_KEY` in your `.env` — if it's missing, or the `mp-api` package isn't installed, this step is skipped cleanly (every `mp_*` column becomes empty with a `mp_query_skipped` reason, nothing crashes).

Some important, non-obvious behavior here:
- **Trace elements are dropped before querying.** Materials Project doesn't have entries for arbitrary dilute compositions like "tungsten with 80 ppm potassium." Any element making up less than 1% of the atomic composition is dropped from the query — the system is queried as its majority composition only. You'll see this printed as `mp_query_composition: {...}  (dropped as trace, <1%): {...}` for every record.
- **Which specific crystal structure gets picked matters a lot**, and this had a real bug (see below). For a single pure element (like pure W), the code now explicitly picks the entry with `energy_above_hull == 0.0` — the actual, defined ground state — rather than "whichever cubic entry has the lowest energy," which could (and did) accidentally pick a metastable, non-representative crystal form instead.
- **Records for the W-Ni-Fe/Co/Cu "heavy alloy" system are skipped entirely at this stage** (see Step 6 below) rather than queried.

### Step 5 — Estimate unstable stacking fault energy (USFE)
USFE is a physical quantity that correlates with how mobile dislocations are (and therefore with DBTT), but it's essentially never reported directly in these papers. `usfe_features.py` estimates it as a composition-weighted average of pure single-element USFE values, taken from one specific published table (Zhang et al. 2023, Table 1, `{110}<111>` slip system, DOI `10.1016/j.jmrt.2022.12.162`). If an element making up more than 1% of the composition isn't in that table, the whole estimate is left as `None` and the missing element(s) are listed in `usfe_missing_elements` — it never silently averages over only the elements it happens to know about while ignoring the rest.

**Note:** there used to be a second USFE estimate here, based on an empirical formula relating USFE to elastic moduli and lattice constant. It has been **completely removed**. See "Bugs Found and Fixed" below for exactly why.

### Step 6 — Flag out-of-scope records, skip MP for them
Three of your extracted materials (`95W-3.5Ni-1.5Fe`, `93W-4.5Ni-1Fe-1.5Co`, `90W-6Ni-4Cu` — tungsten "heavy alloys," a totally different processing route: liquid-phase sintering, used for things like kinetic penetrators, not DBTT-characterized structural tungsten) don't have DBTT values and aren't really part of what this dataset is for. Any record whose composition contains **Ni, Co, or Cu** gets `dataset_scope_flag = "W_heavy_alloy_no_DBTT"` and its Materials Project query is skipped outright (not attempted and failed — deliberately never queried, since it wasn't judged worth the effort).

### Step 7 — Assemble the row and overwrite the "authoritative" thermodynamic columns
The original schema has four columns — `VEC`, `delta_H_mix_kJ_mol`, `delta_S_mix_J_mol_K`, `delta_atomic_size` — that Gemini used to try to calculate itself during extraction. Gemini is bad at exact arithmetic; this pipeline is not. The schema now tells Gemini to leave these four fields `null`, and this pipeline **overwrites them with its own computed values** (from Step 3) whenever it successfully computed one. If the pipeline couldn't compute a value (e.g. a missing Takeuchi-Inoue pair), whatever was already there is kept rather than being wiped to null. **No separate "_gemini" or "_mendeleev" audit columns are kept in the final CSV** — you were getting confused by having both the raw and overwritten version of the same number sitting side by side, so now there's just one authoritative column per quantity.

### Step 8 — Write the CSV and the log
`data/features_output.csv` — one row per material record. `data/feature_log.txt` — a full plain-text log, one block per record, showing exactly what composition was built, what warnings fired, whether each of the three feature tracks succeeded/was skipped/failed, and why. **Always check this log after a run** — it's the honest record of what actually happened, including every place the pipeline declined to guess.

---

## Full list of columns added by `pipeline.py`

Everything below is *in addition to* the 68 original schema fields (which are also present, minus `_source_file` bookkeeping).

**Composition:**
- `frac_{element}` — atomic fraction of each element present (one column per element that appears anywhere in your dataset)
- `dataset_scope_flag` — `"W_heavy_alloy_no_DBTT"` or empty

**Mixture-rule (element property) features:**
- `mendeleev_atomic_radius_pm_weighted`, `mendeleev_metallic_radius_pm_weighted`, `mendeleev_atomic_weight_weighted`, `mendeleev_melting_point_K_weighted`, `mendeleev_density_g_cm3_weighted`, `mendeleev_electronegativity_weighted` (Pauling scale)
- `mean_boiling_point_K`, `mean_fusion_heat_kJ_mol`, `mean_thermal_conductivity_W_mK`, `mean_en_allen` (Allen-scale electronegativity), `mean_electron_affinity_eV`
- `mendeleev_a_bcc_equiv_angstrom` — composition-weighted lattice constant, converting non-BCC elements to their BCC-equivalent value first

**The four "authoritative" thermodynamic columns (overwritten by the pipeline):**
- `VEC`, `delta_H_mix_kJ_mol`, `delta_S_mix_J_mol_K`, `delta_atomic_size`
- (if the pipeline couldn't compute one, `mendeleev_h_mix_missing_pairs` or `mendeleev_vec_missing_elements` explains which element pair/element was missing from the reference table)

**Materials Project (elastic/thermodynamic DFT data):**
- `mp_material_id`, `mp_formula`, `mp_bulk_modulus_GPa`, `mp_shear_modulus_GPa`, `mp_formation_energy_eV_atom`, `mp_energy_above_hull_eV_atom`, `mp_crystal_system`, `mp_spacegroup_symbol`, `mp_is_stable`
- `mp_bcc_assumption_applied` — `True` if no BCC/cubic phase was found and a different structure's elastic data was used instead (treat with caution when this is set)
- `mp_all_phases_found` — `False` if nothing was found within the energy-above-hull search window
- `mp_query_skipped` — the specific reason nothing was queried (missing API key, package not installed, out-of-scope alloy, trace-only composition, etc.) — `None`/empty when a query actually succeeded

**USFE (stacking fault energy) estimate:**
- `usfe_linear_mixture_mJ_m2` — the composition-weighted estimate; `None` if a significant (>1%) element isn't in the reference table
- `usfe_missing_elements` — which element(s) caused that, if any
- `usfe_approximation_note` — a fixed sentence explaining the method and its limitation, always present

---

## Bugs Found and Fixed (and what they mean for your data)

These happened during development and testing. Documenting them here so you understand exactly what changed and why, in case you ever need to explain or re-verify this pipeline yourself.

### 1. YAML indentation bug in `schema.yaml` broke extraction entirely
After you added the `composition_string` domain rule, two lines in `domain_rules` had landed at column 1 instead of matching the rest of the block's 2-space indent. This isn't a formatting nitpick — in YAML, a "literal block scalar" (the `domain_rules: |` syntax) ends the moment indentation drops below the block's level, so the parser thought the file ended early and then hit a stray `-` it couldn't make sense of. **Effect: `extract_data.py` crashed immediately on startup, before even contacting Gemini.** Fixed by re-indenting those two lines. Always be careful with indentation inside `domain_rules:` — it's whitespace-sensitive Python-style YAML, not free-form text.

### 2. Gemini's responses were silently getting cut off mid-JSON (all records lost)
After expanding the schema from 55 to 68 fields, every single paper in a batch run failed with a JSON parse error, and `data/outputs/*.json` ended up with zero usable records (just an error blob) despite Gemini clearly doing real work. The diagnostic (`finish_reason`, added specifically to catch this) showed `MAX_TOKENS` even though the *visible* output token count was well under the 65,536 limit — the giveaway that Gemini's "thinking" (invisible internal reasoning, on by default for `gemini-2.5-flash`) was eating most of the same token budget the JSON output needed, and the real total (thinking + output) was blowing the cap. **Fixed by disabling thinking entirely** (`thinking_config=types.ThinkingConfig(thinking_budget=0)`) on both Gemini calls — appropriate here because this is a deterministic, schema-forced, `temperature=0` extraction task with no real need for chain-of-thought. This also made each call **cheaper** (you were being billed for tens of thousands of thinking tokens that produced zero usable output before the fix), not more expensive.

### 3. A single `or` in the Materials Project phase-selection logic silently discarded correct answers
`mp_features.py` used the pattern `d.get("energy_above_hull") or 1.0` to provide a default when a value was missing. The bug: in Python, `0.0 or 1.0` evaluates to `1.0`, because `0.0` is treated as "falsy." **The ground state of any element is defined as `energy_above_hull == 0.0` exactly** — so this pattern was silently converting the single correct answer into a wrong sentinel value on every single query. For pure tungsten, this caused the pipeline to select a metastable, non-representative crystal structure (β-W, space group Pm-3n, energy above hull ≈0.127 eV/atom, not actually stable) instead of the real ground state (α-W, space group Im-3m, energy above hull exactly 0.0, the genuinely stable structure) — and consequently pulled the *wrong* bulk modulus and shear modulus values into the dataset. Fixed with a proper helper function using explicit `is not None` checks everywhere in the file, plus an explicit rule: for single-element systems, select the entry with `energy_above_hull == 0.0` directly, rather than "lowest value among near-hull candidates" (that phrasing is meant for choosing between competing multi-element phases, not for choosing among one element's own allotropes).

### 4. The second USFE formula produced numbers ~44–50x too large and was removed
A second stacking-fault-energy estimate (Zhang et al. 2022's empirical formula relating USFE to bulk modulus, shear modulus, and lattice constant) was implemented per an earlier specification, but when tested against pure tungsten's known literature value (~1786 mJ/m²), it produced ~79,000–88,600 instead — consistently 44–50x too high, even after the bug above was fixed and the correct elastic moduli were being used. The unit scaling (or the exact constants/exponents in the formula itself) was never independently verified against the original source, and I was not willing to invent a "correction factor" to force the number to look right — that would be exactly the kind of guessing this project's rules explicitly forbid. **Decision: removed entirely**, rather than left in as an unreliable number someone might trust later. Only the composition-weighted linear-mixture USFE estimate remains, sourced from one specific, cited, verified table.

### 5. Composition builder's `ppm` handling looked "wrong" but wasn't
When testing the 1%-threshold logic for Materials Project queries, an example composition (potassium at 80 ppm in tungsten) was expected to come out as an atomic fraction of `0.00008`. The actual code produces `0.000376` instead. This is not a bug — 80 ppm is a *weight* fraction, and because potassium's atomic weight (~39 g/mol) is much lower than tungsten's (~184 g/mol), a given weight fraction of potassium corresponds to proportionally *more* atoms, hence a higher atomic-fraction number. Documented here so this doesn't get "fixed" into being wrong later.

---

## Quick reference: what to run, and when

| You want to... | Run |
|---|---|
| Fetch a new paper (Elsevier, needs VPN) | Edit `DOIS` in `fetch_papers.py`, then `python scripts/fetch_papers.py` |
| Fetch/register a non-Elsevier paper | Try `fetch_papers.py` first (Unpaywall); if it says "manual download required," save the PDF to `data/papers/{doi_slug}.pdf` yourself and add the DOI to `data/papers/manual_papers.json` |
| Extract everything not yet extracted | `python scripts/extract_data.py --batch` |
| Extract just one paper | `python scripts/extract_data.py <doi>` |
| Re-run feature enrichment on everything currently extracted | `python scripts/pipeline.py` |
| Start completely over | Delete everything under `data/papers/`, `data/outputs/`, `data/figures/`, plus `data/element_properties.csv`, `data/features_output.csv`, `data/feature_log.txt` (PowerShell: `Remove-Item <path> -Recurse -Force`, not `rm -rf`) |
| Change what fields get extracted | Edit `schemas/w_dbtt/schema.yaml` — never hardcode fields in `extract_data.py` |
| Change which figure types get saved to disk | Edit `SAVED_FIGURE_TYPES` near the top of `extract_data.py` |

**Before copying anything from `data/features_output.csv` into your real Excel dataset: read `data/feature_log.txt` for that run first.** It tells you, per record, exactly what was computed, what was skipped, and why — that log is the honest paper trail for every number in the CSV.
