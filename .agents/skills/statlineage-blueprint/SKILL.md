---
name: statlineage-blueprint
description: >-
  Complete end-to-end architecture, multi-source ETL pipeline, database schema, backend API,
  and D3.js responsive UI runbook for building or reproducing StatLineage (the interactive
  Academic Family Tree of Statistics, Biostatistics, Probability, and Related Quantitative
  Sciences) from ground zero. Use this skill whenever reproducing, rebuilding, extending,
  refreshing data, or modifying the StatLineage academic genealogy platform.
---

# StatLink (formerly StatLineage) — Ground-Zero Implementation Blueprint

**StatLink** is a high-recall, precision-calibrated, strictly read-only interactive academic genealogy web platform unifying **553,889 PhD scholars** and **593,680 doctoral mentorship edges** (`85,820` Core Statistics, Biostatistics & Probability PhDs and `143,845` Broad Quantitative PhDs) across **207 tracked statistical PhD institutions** and **3,042 global universities**, served via Flask + SQLite FTS5 + D3.js v7 on `http://127.0.0.1:5050`.

For deep-dive specifications, read the companion reference manuals in `./references/`:
- **[Data Sources, Entity Resolution & ETL Pipeline](./references/data_sources_and_etl_pipeline.md)** — Raw dataset schemas (`MGP`, `AcademicTree`, `Wikidata`), entity resolution tiers, TF-IDF + LogisticRegression classifier, pre-2015 `AFT` subtree precision calibration, and all 7 post-2013 ProQuest / Cloudflare gap-closing harvesters (`UC eScholarship`, `DSpace 7`, `DigitalCommons OAI-PMH`, `HAL`, `DataCite`, `OpenAlex`, `UF Commencement PDFs`, `ORCID + Crossref`, `NIH PubMed Methodology`, and `Faculty CVs`).
- **[Backend API, Ancestry Engine & Frontend UI Specification](./references/backend_and_frontend_spec.md)** — Complete SQLite DDL, sub-millisecond `AncestryIndex` (`109` world-famous scientists), read-only REST endpoints, custom D3 layered DAG layout, visual card/star/border rules, Light/Dark CSS tokens, and mobile bottom-sheet UX.

---

## 1. Repository Architecture

```text
StatLineage/
├── app.py                                                # Read-only Flask server & REST API (port 5050, zero POST/write routes)
├── famous_ancestors.py                                   # 109 curated world-famous scientists + <0.1ms in-memory AncestryIndex
├── crawler_engine.py                                     # Offline CLI helper for faculty websites & CV PDFs (pdftotext)
├── start_server.bat                                      # One-click Windows launcher
├── data/
│   ├── stat_genealogy.db                                 # Unified SQLite + FTS5 database (~393 MB, auto-unpacked)
│   ├── db_parts/stat_genealogy.db.gz.part*               # GitHub-safe <=45 MB gzip chunks (zero Git LFS required)
│   └── stat_departments_master.json                      # 207 global Stat/Biostat/Prob PhD universities
├── data_raw/                                             # Raw MGP JSON, AFT Zenodo CSV.gz, Wikidata JSON, ETD/ORCID/PubMed caches
├── scripts/                                              # Ground-zero deterministic build & gap-closing ETL scripts
│   ├── build_master_schools.py
│   ├── build_database.py
│   ├── pack_database_parts.py                            # Splits stat_genealogy.db into <=45 MB gzip chunks for GitHub
│   ├── harvest_web_and_etds.py
│   ├── harvest_pass2_and_dedup.py
│   ├── crawl_all_schools_and_professors.py
│   ├── close_jhu_and_peer_gaps.py
│   ├── harvest_pubmed_methodology_lineages.py
│   ├── clean_pubmed_duplicates_and_postdocs.py
│   ├── recover_common_name_pubmed_phds.py
│   ├── harvest_uf_commencements_and_orcid_retry.py
│   ├── fill_post2014_drop.py
│   ├── harvest_datacite_2014_2025_dissertations.py
│   ├── harvest_openalex_indexed_2015_2020.py
│   ├── complete_2014_2025_gap_and_aft_precision.py
│   ├── enrich_scholar_external_links.py                  # Enriches Personal Website, Wikipedia, Google Scholar, LinkedIn, ORCID & Wikidata
│   ├── enrich_scholar_photos.py                          # Harvests Wikidata P18, Wikipedia pageimages & faculty homepage portraits
│   ├── fix_homonym_descendants_and_deep_enrich.py        # Audits young-PhD homonym edges + crawls 140+ dept directories & SSL-fixed portraits
│   ├── exhaustive_internet_era_websites_and_photos.py    # Internet-era (1980+) DBLP + CRAN + GitHub Pages + Google Sites + Advisor student link sweep
│   ├── audit_and_fix_personal_website_precision.py       # Enforces concrete school/advisor/thesis anchors on github.io & Google Sites (purges homonyms)
│   ├── deduplicate_hyphen_and_stub_scholars.py           # Merges hyphen/space/middle-initial & undated-stub duplicates (e.g. Wenxin / Wen-Xin Zhou)
│   └── refresh_recent_data.py                            # Unified CLI orchestrator (--mode recent | profiles | full, --pack-github)
└── static/
    ├── index.html                                        # 4-view SPA shell (Search, Tree Visualizer, Common Ancestor, Analytics)
    ├── styles.css                                        # Light/Dark CSS tokens, card/star styling, mobile/tablet breakpoints
    └── app.js                                            # Search, D3 layered DAG renderer, Dossier, 1-year growth chart, touch UX
```

---

## 2. Environment & Dependencies

- **Python 3.9+** with `flask`, `numpy`, `scikit-learn`, `requests`, `beautifulsoup4`, `lxml`.
- **`pdftotext` CLI** (Poppler or MiKTeX) on `PATH` for offline parsing of faculty CV PDFs and University of Florida Doctoral Commencement booklets.
- **Windows Anaconda + MiKTeX `PATH` setup** (prevents Windows Store stub & missing OpenSSL DLL issues):
  ```powershell
  $env:PATH = "D:\Software\Anaconda;D:\Software\Anaconda\Library\bin;D:\Software\Anaconda\Scripts;C:\Program Files\MiKTeX\miktex\bin\x64;" + $env:PATH
  $env:PYTHONIOENCODING = "utf-8"
  ```

---

## 3. Step-by-Step Reproduction from Ground Zero

### Phase A: Acquire Raw Foundational Exports (`data_raw/`)
1. Place the cleaned **Mathematics Genealogy Project** dump at `data_raw/mgp_data_new.json` (`338,532` PhDs).
2. Download the official **Academic Family Tree** Zenodo export into `data_raw/aft_people.csv.gz`, `data_raw/aft_connect.csv.gz`, and `data_raw/aft_locations.csv.gz`.
3. Query `https://query.wikidata.org/sparql` (using modulo partitioning in `scripts/finish_wikidata_split.py` if needed) to save:
   - `data_raw/wikidata_mgp_aft_bridges.json` (`P549` ↔ `P2381` gold-standard identity bridges).
   - `data_raw/wikidata_stats.json` (`P184` doctoral advisor edges, `P69`, `P108`, `P496` ORCID, `P856` homepage).

### Phase B: Build Base Unified Graph & Master University Directory
4. Generate the `207`-institution global directory (`data/stat_departments_master.json`):
   ```powershell
   python scripts/build_master_schools.py
   ```
5. Run the core 5-stage graph & ML classification builder (`~3–5 min`):
   ```powershell
   python scripts/build_database.py
   ```
   - Normalizes `3,042` schools and `70+` nickname groups; merges `MGP`, `AcademicTree` (**strictly `relation == 1` doctoral edges**), and `Wikidata`; sanitizes cycles into a strict DAG; trains a `TfidfVectorizer + LogisticRegression` classifier on `202,492` MSC-labeled theses to classify the `40%` missing-MSC MGP records; computes exact reverse-topological DAG descendant sets; and builds `data/stat_genealogy.db` + `people_fts`.

### Phase C: Close the Post-2013 ProQuest & Cloudflare Recall Gap (`2014–2026`)
Execute the supplemental harvesters in order (each script updates `people`, `edges`, `advisor_schools`, topological `descendant_count`, `meta_stats`, and rebuilds `people_fts`; see [data_sources_and_etl_pipeline.md](./references/data_sources_and_etl_pipeline.md) for full details):
6. **UC eScholarship (all 10 campuses), DSpace 7, HAL (`theses.fr`), DataCite, Stanford Alumni & Faculty Websites**:
   ```powershell
   python scripts/harvest_web_and_etds.py
   python scripts/harvest_pass2_and_dedup.py
   python scripts/crawl_all_schools_and_professors.py
   ```
7. **School of Public Health DigitalCommons OAI-PMH (`qdc`), Split MGP↔AFT Stub Unification & 32-School ORCID + Crossref**:
   ```powershell
   python scripts/close_jhu_and_peer_gaps.py
   python scripts/harvest_uf_commencements_and_orcid_retry.py
   ```
8. **37-School NIH PubMed Statistical Methodology Lineages + Postdoc/Homonym Calibration**:
   ```powershell
   python scripts/harvest_pubmed_methodology_lineages.py
   python scripts/clean_pubmed_duplicates_and_postdocs.py
   python scripts/recover_common_name_pubmed_phds.py
   ```
9. **Global 2014–2026 ETD Sweep (`DataCite`, `OpenAlex` Indexed Concepts) + Pre-2015 `AFT` Non-Stat Subtree Precision Calibration**:
   ```powershell
   python scripts/fill_post2014_drop.py
   python scripts/harvest_datacite_2014_2025_dissertations.py
   python scripts/harvest_openalex_indexed_2015_2020.py
   python scripts/complete_2014_2025_gap_and_aft_precision.py
   python scripts/enrich_scholar_external_links.py
   python scripts/enrich_scholar_photos.py
   python scripts/fix_homonym_descendants_and_deep_enrich.py
   python scripts/exhaustive_internet_era_websites_and_photos.py
   python scripts/audit_and_fix_personal_website_precision.py
   python scripts/deduplicate_hyphen_and_stub_scholars.py
   ```

### Phase D: Pack Portable GitHub Archive & Launch Web Server
10. Pack `data/stat_genealogy.db` into `<= 45 MB` gzip chunks (`data/db_parts/stat_genealogy.db.gz.part*`) for GitHub distribution without Git LFS:
    ```powershell
    python scripts/pack_database_parts.py
    ```
11. Start the Flask application on `http://127.0.0.1:5050` (`ensure_database_ready()` in `app.py` automatically unpacks `data/db_parts/` on fresh clones):
    ```powershell
    python app.py
    ```

---

## 4. Critical Design Decisions & Hard-Won Pitfalls

1. **Strictly Read-Only Web Application & Zero Static-HTML Caching Trap**:
   - `app.py` exposes **only `GET` endpoints** (`/api/stats`, `/api/random_person`, `/api/schools`, `/api/master_schools`, `/api/search`, `/api/person/<id>`, `/api/tree/<id>`, `/api/lineage_path`, `/api/connection`). Never expose browser-triggered web-crawl or live-import `POST` endpoints in the UI or server.
   - Set `SEND_FILE_MAX_AGE_DEFAULT = 0` and emit `Cache-Control: no-store, no-cache, must-revalidate, max-age=0` on `/` and `/static/*` (Flask 2.0 defaults to `max-age=43200`, which otherwise causes browsers to serve stale `index.html` layouts for 12 hours).
2. **Sub-Millisecond Ancestry & Descendant Lookup (`AncestryIndex` vs. SQLite CTE)**:
   - Never run `WITH RECURSIVE ... UNION ALL` in SQLite on academic genealogy DAGs—reconverging 18th-century European lineages cause exponential blowup (`13.7s` per request). Cache upward `_parents` and downward `_children` tuples in memory (`famous_ancestors.py`) for **`0.07ms` upward BFS** and **`0.01–2ms` downward descendant BFS** (`get_descendants_map(db, ancestor_id, direct_only)` powering `/api/search?ancestor_id=`).
3. **Why `2014 -> 2015` Drops in Raw Data & How It Was Solved**:
   - AcademicTree stopped bulk-importing ProQuest in late 2014, while pre-2015 `AFT-only` rows contained `~11,750` non-statistical ecology/psychology/humanities theses pulled into `Core` by multi-hop lineage propagation. Recalibrating non-statistical `AFT-only` rows to `Related Quantitative` (`0` deleted) + harvesting post-2013 ETDs/ORCID/DataCite/OpenAlex/PubMed produces a smooth, truthful annual curve (`1920–2026`).
4. **OpenAlex Quota Discipline, ORCID Rate Limiting & Automated ETD Homonym Guard**:
   - OpenAlex `title.search:` costs `\$0.01`/call (`100x` indexed filters at `\$0.0001`/call) against a `\$0.10`/day anonymous IP quota—always use indexed `concepts.id` filters or `DataCite` (zero quota).
   - Against `pub.orcid.org`, use at most `8` concurrent workers with exponential backoff on `HTTP 429`.
   - **Never link an automated ETD / PubMed advisee to an advisor whose own PhD is $\le 3$ years prior** (`student.year - advisor.year <= 3`) or via an ambiguous cross-university `DataCite ETD` global name fallback—this causes recent PhD graduates (e.g., *Yu Zhao*, UCSD PhD 2023) to absorb engineering advisees of older homonyms at other universities. Always enforce `fix_false_homonym_descendants()`, retry `.edu` HTML fetches with `verify=False` on `SSLError` (`math.ucsd.edu`, `pages.ucsd.edu`, `cseweb.ucsd.edu`, `stat.ufl.edu`), never block `"default"` as a raw URL substring (which matches Drupal's `/sites/default/files/` across US `.edu` sites), and use a single shared sliding-window `ThreadPoolExecutor` (`run_bounded_batch`) so neither hung sockets nor leaked per-chunk thread pools can stall or drop URLs.
5. **UI Cleanliness & Visual Invariants** (see [backend_and_frontend_spec.md](./references/backend_and_frontend_spec.md)):
   - **Google-Style Top Bar, `Search` Front Page, Pinned SERP & Fuzzy Search (`StatLink`)**:
     - **Bannerless Top Bar & Subtab Home Logo**: Bannerless (`background: transparent; border: none; box-shadow: none;`), displaying **1-word** navigation links (**`Search`**, **`Tree`**, **`Kinship`**, **`Analytics`**) + circular theme icon on the right, and automatically revealing a clickable **`StatLink`** logo (`#topbarLogoBtn`) on the top-left of every subtab (`Tree`, `Kinship`, `Analytics`) to return to the `Search` tab (`body:not([data-active-view="explore"])`), while keeping the Search front page top-left empty.
     - **Front Page (`.explore-layout.is-home`)**: Optically centered (`::before` `flex: 0.86`, `::after` `flex: 1.14`) **`StatLink`** wordmark, floating pill search box with live fuzzy autocomplete dropdown (`#mainSearchDropdown`), two action buttons (`Filters & Universities ▾` and `I'm Feeling Random`), smooth `0fr <-> 1fr` inline filter accordion, and the **5 era-spanning `Notable Trees`** (`Gauss`, `Neyman`, `Blackwell`, `Efron`, `Xihong Lin`).
     - **Google-Pinned SERP, 3-Column Filter Panel & Floating `Tree` Controls**: Hides `Notable Trees` on SERP; adds a clickable **`StatLink`** logo (`#serpHomeLogoBtn`) to the **left of the search box** to return home; pins `.search-hero` (`position: sticky; top: 0;`) with scroll shadow (`.is-scrolled`) as the user scrolls down; structures `#inlineFiltersPanel` as a compact **3-column grid** (`University (PhD or Faculty)` | compact `150px` **`PhD Year`** `From`–`To` without wrapped era chips | **`Descendant Of`** scholar autocomplete with `All Descendants` / `Direct Students` chips right of `PhD Year`); uses Google-Maps-style floating `Tree` controls (top-left search pill + bottom-right 3-icon camera stack + `6↑ · 2↓` popover with directly editable middle generation inputs that auto-reset to default `6↑ · 2↓` when opening a different search result); and supports Google-like fuzzy search (`FuzzySearchIndex` in `app.py`) plus HTML5 Browser Back/Forward (`pushState`/`popstate`).
   - **No clutter**: No permanent left sidebar; no top-right scholar count pill; no `Top PhD Institutions` list or scope hint text on Tab 1; `"All"` scope pill label; no `Trail` bar; no `AcademicTree / ProQuest Enriched` KPI card; no `"Hover any year bar for exact counts."` sentence or `Evaluation of Online Academic Genealogy Resources` section on Tab 4.
   - **Tree Node Visuals**: Blue border = focal/selected/hovered scholar; grey border = other scholars; `5.5px` left stripe = subfield color; gold `★` next to world-famous scientists' names on Search cards, Tree SVG nodes, and Dossier titles; `★ World-Famous Scientist Ancestors` placed below `Doctoral Advisors` in the right-hand Dossier.
   - **Dark Mode Zoom Stability**: Toggle `data-theme` without tearing down `#treeSvg`, and preserve `d3.zoomTransform(svgEl)` across non-recentering renders.
6. **Living Blueprint & Data-Refresh Skill Maintenance Rule**:
   - **Whenever any UI, backend API, database schema, crawler, or ETL pipeline change is made to StatLink, always update `SKILL.md` + `./references/` here AND `.agents/skills/statlink-data-refresh/` in the same turn** so ground-zero reproducibility and incremental data refreshes never drift from the live codebase.

---

## 5. Verification Checklist

After building or modifying StatLineage, verify all of the following against `http://127.0.0.1:5050`:
1. `GET /api/stats` returns `core_stat_total > 80000`, `total_people > 550000`, `year_histogram` populated for every single year `1920..2026`, and `len(featured) == 5` (`Gauss`, `Neyman`, `Blackwell`, `Efron`, `Xihong Lin`).
2. `GET /api/search?q=Lei+Huang&school=Johns+Hopkins+University` and `GET /api/search?q=Jing+Liu&school=University+of+Florida` return verified Biostatistics PhDs (`2015` under *Ciprian Crainiceanu* and `2017` under *Samuel S. Wu*).
3. `GET /api/person/14941` (Bradley Efron) returns `< 5ms` response time with `famous_self` populated and `famous_ancestors.highlighted` tracing back to Laplace, Lagrange, Euler, Leibniz, Poisson, and Chebyshev.
4. Confirm zero `POST` routes exist (`POST /api/crawl_professor/14941` returns `404`), Dark Mode toggle preserves tree zoom/pan coordinates, and mobile viewport (`<= 768px`) activates the slide-over filter drawer and 3-state collapsible bottom Dossier sheet.
