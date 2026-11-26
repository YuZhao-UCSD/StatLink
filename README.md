# StatLink — Academic Family Tree of Statistics, Biostatistics & Probability

An interactive, mobile-responsive academic genealogy explorer unifying **550,843 deduplicated PhD scholars** and **590,063 doctoral advisor–student links** (`84,424` Core Statistics, Biostatistics & Probability PhDs, `142,800+` Broad Quantitative PhDs, `43,000+` verified personal websites, and `21,000+` scholar portraits) across **207 tracked Statistics/Biostatistics PhD-granting institutions** and **3,042 global universities**.

## GitHub Pages (Serverless Static Deployment)

The repository includes a pre-built, 100% serverless static site in **`docs/`** (built by `scripts/build_gh_pages.py` + `static/static_api.js` using compact gzipped indexes, a binary `uint32` graph, and 256 on-demand JSON detail shards). To publish live on GitHub Pages:
1. Open **Settings → Pages** in this repository.
2. Under **Build and deployment**, set **Source** to **Deploy from a branch**, **Branch** to `main`, and **Folder** to **`/docs`**, then click **Save**.

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

- **Minimalist Google-Style Search, Fuzzy Matching & Lineage Filters**: Centered Google-style homepage with live fuzzy autocomplete suggestions (`FuzzySearchIndex`), sticky Google-pinned SERP header, **`I'm Feeling Random`** lineage jump, smooth inline **`Filters & Universities`** accordion (`University`, compact `PhD Year`, and **`Descendant Of`** across all generations or direct PhD students), and 5 era-spanning **`Notable Trees`** (`Carl Friedrich Gauss (1799)`, `Jerzy Neyman (1924)`, `David Blackwell (1941)`, `Bradley Efron (1964)`, `Xihong Lin (1994)`).
- **Interactive D3.js Family Tree Visualizer & Scholar Dossier**: Multi-generation ancestor/descendant DAG layout (`Top → Down` and `Left → Right`), Google-Maps-style floating camera & editable generation controls (`6↑ · 2↓`), scholar portrait avatars & verified external profile links (`Personal Website`, `Wikipedia`, `Google Scholar`, `LinkedIn`, `ORCID`, `Wikidata`), Light/Dark themes, gold `★` highlights for **109 curated World-Famous Scientist Ancestors**, step-by-step lineage chain tracing on canvas, and a 3-state collapsible mobile bottom Dossier sheet.
- **Common Academic Ancestor (MRCA) Finder**: Computes and renders the shortest shared doctoral lineage connecting any two scholars up to 45 generations.
- **Field Analytics & Global University Directory**: 1-year-resolution (`1920–2026`) stacked PhD graduation chart and searchable directory of `207` global Statistics, Biostatistics & Probability PhD institutions.

## Multi-Source Data Pipeline & Ground-Zero Reproduction

1. **Mathematics Genealogy Project (MGP)** (`338,532` cleaned PhD records through Summer 2026).
2. **Academic Family Tree (`academictree.org`)** (April 2026 Zenodo export filtered strictly to `relation = 1` doctoral mentorship + ProQuest dissertation metadata).
3. **Wikidata Scholarly Graph** (`9,547` exact `MGP ID ↔ AcademicTree PID` bridges + `23,882` curated `P184` doctoral advisor links).
4. **Institutional ETDs, Registries & Post-2013 Gap Closure (`scripts/*.py`)**:
   - All 10 **University of California** campuses (`escholarship.org/graphql`), `15+` **DSpace 7** & **DigitalCommons OAI-PMH** repositories, **HAL / `theses.fr`**, **DataCite Supervisor DOIs**, **OpenAlex** indexed dissertations, **Stanford Statistics Alumni**, and **University of Florida Doctoral Commencement PDFs (`2013–2026`)**.
   - **32-School ORCID v3.0 + Crossref** advisor resolution, **37-School NIH PubMed** statistical methodology lineages, and **1,600+ Faculty Websites & CV PDFs**.

Full ground-zero reproduction instructions, SQLite DDL, API contracts, UI specifications, and incremental data-refresh runbooks are maintained in [`.agents/skills/statlineage-blueprint/SKILL.md`](.agents/skills/statlineage-blueprint/SKILL.md) and [`.agents/skills/statlink-data-refresh/SKILL.md`](.agents/skills/statlink-data-refresh/SKILL.md).
