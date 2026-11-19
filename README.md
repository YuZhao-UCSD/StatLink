# StatLineage — Academic Family Tree of Statistics, Biostatistics & Probability

An interactive, mobile-responsive academic genealogy explorer unifying **553,885 PhD scholars** and **593,676 doctoral advisor–student links** (`85,820` Core Statistics, Biostatistics & Probability PhDs and `143,845` Broad Quantitative PhDs) across **207 tracked Statistics/Biostatistics PhD-granting institutions** and **3,042 global universities**.

## Quick Start

```bash
git clone https://github.com/YuZhao-UCSD/StatLineage.git
cd StatLineage
pip install -r requirements.txt
python app.py
```

Then open **http://127.0.0.1:5050** in your browser.
- On first startup after `git clone`, `app.py` automatically unpacks the bundled `data/db_parts/stat_genealogy.db.gz.part*` archive into `data/stat_genealogy.db` in ~3 seconds (no Git LFS required).
- On Windows with Anaconda, you can also double-click **`start_server.bat`**.

## Key Features

- **Minimalist Google-Style Search & Directory**: Instant full-text FTS5 search with nickname expansion, high-recall cross-field rescue, **`I'm Feeling Lucky`** random lineage jump, smooth inline **`Filters & Universities`** accordion, and 5 era-spanning **`Notable Trees`** (`Carl Friedrich Gauss (1799)`, `Jerzy Neyman (1924)`, `David Blackwell (1941)`, `Bradley Efron (1964)`, `Xihong Lin (1994)`).
- **Interactive D3.js Family Tree Visualizer & Scholar Dossier**: Multi-generation ancestor/descendant DAG layout (`Top → Down` and `Left → Right`), SVG export, Light/Dark themes, gold `★` highlights for **109 curated World-Famous Scientist Ancestors**, step-by-step lineage chain tracing on canvas, and a 3-state collapsible mobile bottom Dossier sheet.
- **Common Academic Ancestor (MRCA) Finder**: Computes and renders the shortest shared doctoral lineage connecting any two scholars up to 45 generations.
- **Field Analytics & Global University Directory**: 1-year-resolution (`1920–2026`) stacked PhD graduation chart and searchable directory of `207` global Statistics, Biostatistics & Probability PhD institutions.

## Multi-Source Data Pipeline & Ground-Zero Reproduction

1. **Mathematics Genealogy Project (MGP)** (`338,532` cleaned PhD records through Summer 2026).
2. **Academic Family Tree (`academictree.org`)** (April 2026 Zenodo export filtered strictly to `relation = 1` doctoral mentorship + ProQuest dissertation metadata).
3. **Wikidata Scholarly Graph** (`9,547` exact `MGP ID ↔ AcademicTree PID` bridges + `23,882` curated `P184` doctoral advisor links).
4. **Institutional ETDs, Registries & Post-2013 Gap Closure (`scripts/*.py`)**:
   - All 10 **University of California** campuses (`escholarship.org/graphql`), `15+` **DSpace 7** & **DigitalCommons OAI-PMH** repositories, **HAL / `theses.fr`**, **DataCite Supervisor DOIs**, **OpenAlex** indexed dissertations, **Stanford Statistics Alumni**, and **University of Florida Doctoral Commencement PDFs (`2013–2026`)**.
   - **32-School ORCID v3.0 + Crossref** advisor resolution, **37-School NIH PubMed** statistical methodology lineages, and **1,600+ Faculty Websites & CV PDFs**.

Full ground-zero reproduction instructions, SQLite DDL, API contracts, and UI specifications are maintained in [`.agents/skills/statlineage-blueprint/SKILL.md`](.agents/skills/statlineage-blueprint/SKILL.md).
