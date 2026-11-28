---
name: statlink-data-refresh
description: >-
  Incremental and full data-refresh runbook for updating StatLink (formerly StatLineage) with the
  most recent PhD dissertations, mentorship lineages, faculty affiliations, Verified Source links
  (Personal Website, Wikipedia, Google Scholar, LinkedIn, ORCID, Wikidata), and scholar portrait
  photos across all 15 data sources and crawlers. Activate this skill whenever updating StatLink
  with new academic years, refreshing crawlers, re-running profile/photo enrichment, or adding a
  new data source.
---

# StatLink — Data Refresh & Multi-Source Crawler Runbook

Use this skill whenever updating **StatLink** (`d:\Jetski\Family Tree`) with the newest PhD graduates, advisor-student edges, faculty directory moves, personal websites, external profile links, or portrait photos—without losing precision or breaking read-only web invariants.

For per-source API endpoints, cache files, rate limits, and anti-bot/SSL workarounds across all **15 upstream sources**, read:
- **[Complete Source Catalog, Cache Files & Crawler Rules](./references/source_catalog_and_crawlers.md)**

---

## 1. Environment Prerequisite (Windows Anaconda + MiKTeX + R)

Always prepend Anaconda (`python`, OpenSSL DLLs), MiKTeX (`pdftotext` for faculty CV PDFs and UF commencement booklets), and `Rscript` (`tools::CRAN_package_db()` for CRAN R-package author URLs):

```powershell
$env:PATH = "D:\Software\Anaconda;D:\Software\Anaconda\Library\bin;D:\Software\Anaconda\Scripts;C:\Program Files\MiKTeX\miktex\bin\x64;C:\Program Files\R\R-4.1.2\bin\x64;" + $env:PATH
$env:PYTHONIOENCODING = "utf-8"
```

---

## 2. One-Command Orchestrator (`scripts/refresh_recent_data.py`)

Choose the refresh mode matching the scope of your update:

1. **Incremental Lineage + Profile Refresh (Recommended Periodic Update)**:
   Re-runs all post-2013 ETD, commencement PDF, ORCID+Crossref, NIH PubMed, and faculty web harvesters, followed by external link, portrait photo, homonym precision, and internet-era personal website crawlers, then recomputes topological DAG counts and `people_fts`:
   ```powershell
   python scripts/refresh_recent_data.py --mode recent --clear-recent-caches
   ```
2. **Profile Links, Websites, Portraits & Homonym Audit Only (Fastest)**:
   Refreshes `Verified Source` pills (`Personal Website`, `Wikipedia`, `Google Scholar`, `LinkedIn`, `ORCID`, `Wikidata`), portrait photos, 140+ departmental faculty directories, DBLP, CRAN, `*.github.io`, `sites.google.com/view/*`, and advisor-linked student websites:
   ```powershell
   python scripts/refresh_recent_data.py --mode profiles --clear-recent-caches
   ```
3. **Full Ground-Zero Rebuild + Recent Refresh + GitHub Archive Chunking**:
   Use after replacing foundational dumps (`data_raw/mgp_data_new.json`, `data_raw/aft_*.csv.gz`, `data_raw/wikidata_*.json`):
   ```powershell
   python scripts/refresh_recent_data.py --mode full --clear-recent-caches --pack-github
   ```

---

## 3. Advancing the Maximum Calendar Year (e.g. `2026` -> `2027+`)

Whenever refreshing into a new calendar year $Y > 2026$, update the upper year bound in **four** places before running `--mode recent`:
1. **Harvesters**: Update upper year range in `scripts/harvest_datacite_2014_2025_dissertations.py`, `scripts/complete_2014_2025_gap_and_aft_precision.py`, `scripts/harvest_pubmed_methodology_lineages.py`, and add new semester commencement PDF URLs in `scripts/harvest_uf_commencements_and_orcid_retry.py`.
2. **Backend Histogram (`app.py`)**: Update `year BETWEEN 1920 AND <Y>` in `/api/stats`.
3. **Frontend 1-Year Growth Chart (`static/app.js` & `static/index.html`)**: Update `const maxYear = <Y>;` in `renderDecadeChart()` (`static/app.js`), `<input max="<Y+1>">` and `(1920–<Y>)` subtitle in `static/index.html` (under `Data Sources, Methodology & Analytics`), and bump `?v=` query strings on `styles.css` (currently `?v=24`) / `app.js` (currently `?v=34`).

---

## 4. Complete 15-Source Execution Order & What Each Stage Updates

If running stages individually for targeted debugging rather than via `scripts/refresh_recent_data.py`, **always preserve this order**:

### Stage A — Foundational Graph Dumps (Only when `mgp` / `aft` / `wikidata` dumps are updated)
1. `python scripts/build_master_schools.py` -> Rebuilds `data/stat_departments_master.json` (`207` global Stat/Biostat/Prob PhD institutions).
2. `python scripts/build_database.py` -> Merges **MGP**, **AcademicTree** (**strictly `relation == 1` doctoral edges**), and **Wikidata** (`P549`↔`P2381` bridges + `P184`), breaks cycles into a strict DAG, classifies missing-MSC theses via `TfidfVectorizer + LogisticRegression`, and populates `people`, `edges`, `schools`, `meta_stats`, and `people_fts`.

### Stage B — Recent PhD Dissertation & Mentorship Harvesters (`2014–Present`)
3. `python scripts/harvest_web_and_etds.py` & `python scripts/harvest_pass2_and_dedup.py` -> **UC eScholarship** (all 10 UC campuses), **DSpace 7** institutional ETDs, **HAL / `theses.fr`** (`math.math-st`, `math.math-pr`), initial **DataCite** & **Stanford Alumni**.
4. `python scripts/crawl_all_schools_and_professors.py` -> **Faculty Personal Websites & CV PDFs** (`crawler_engine.py` + `pdftotext -layout`, halting before MS/Postdoc/Committee sections).
5. `python scripts/close_jhu_and_peer_gaps.py` -> **School of Public Health DigitalCommons OAI-PMH** (`metadataPrefix=qdc` across JHU, Harvard, UW, UMich, UNC, Columbia, etc.), split MGP↔AFT stub unification, and **32-School ORCID v3.0 + Crossref**.
6. `python scripts/harvest_uf_commencements_and_orcid_retry.py` -> **University of Florida Doctoral Commencement PDFs** (bypassing `ufdc.ufl.edu` Cloudflare) + rate-limit-safe (`max_workers=8`) **ORCID + Crossref** retry.
7. `python scripts/harvest_pubmed_methodology_lineages.py`, `python scripts/clean_pubmed_duplicates_and_postdocs.py`, `python scripts/recover_common_name_pubmed_phds.py` -> **37-School NIH PubMed E-Utilities** statistical methodology lineages (`40+` core stat/biostat journals) + anti-postdoc & common-name homonym calibration.
8. `python scripts/fill_post2014_drop.py`, `python scripts/harvest_datacite_2014_2025_dissertations.py`, `python scripts/harvest_openalex_indexed_2015_2020.py`, `python scripts/complete_2014_2025_gap_and_aft_precision.py` -> **Global DataCite Dissertations** (`contributorType == "Supervisor"`), quota-safe **OpenAlex Indexed Concepts** (`C105795698 | C62649853 | C149782125`, `\$0.0001`/call), and pre-2015 `AFT`-only non-stat precision calibration.

9. **Global Multi-Wave Institutional ETD, National Thesis, Wikipedia & Faculty Website Expansion (`scripts/etd_ingest_common.py`)**:
   - `python scripts/harvest_global_dspace7_and_figshare.py` -> **34 live DSpace 7 repositories** (dynamically matching `dc.contributor.chair`, `dc.contributor.advisor`, `dc.contributor.supervisor`, `relation.isAdvisorOfPublication` & `thesis.degree.discipline` — unlocking Cornell `419`, UW `772`, Virginia Tech `337`, Duke `318`, BU `306`, Georgia Tech `286`, Queen's `280`, Arizona `253`, Edinburgh `253`, McMaster `224`, MIT `208`, Toronto `173`, York `170`, Calgary `166`, NTU `134`, etc.) + **Figshare Institutional Theses (`item_type=8`)** for CMU KiltHub (`group=18614`), Purdue Hammer (`group=19657`), Monash & Global.
   - `python scripts/harvest_oai_pmh_and_national_theses.py` -> **French `theses.fr` Official Lucene API** (`status:soutenue`, `2,845` defended theses with exact `directeurs`), **German National Library (`DNB` SRU MARC21-XML `700 $4 dgs`)**, UK EPrints (`White Rose` Sheffield/Leeds/York, `Southampton`), and **Expanded Global DataCite Dissertations (`2000–2026`, `2,563`)**.
   - `python scripts/harvest_curl_dspace7_and_fixed_oai.py` -> Uses Windows native `curl.exe --ssl-no-revoke` to bypass Python `urllib3` TLS/WAF blocks on **UT Austin (`495`)**, **Johns Hopkins (`421`)**, **Texas Tech (`198`)**, **Baylor (`180`)**, **U Missouri (`163`)**, **U Adelaide (`71`)**, **U Houston (`40`)**, plus fixed multi-dialect OAI-PMH (`SMU`, `U Montreal`, `Concordia`, `Texas A&M`) and **UC eScholarship `2023–2026` doctoral refresh** (`1,496`, strict `_XXXXD_`).
   - `python scripts/harvest_wave1d_digitalcommons_and_cambridge.py` -> Polite `max_workers=2` `curl.exe` DigitalCommons `qdc` sweep (preventing shared Cloudflare 403s across **Purdue e-Pubs**, **Clemson**, **Wayne State**, **WashU**, **USF**, **South Carolina**, **Marquette**, **Denver**, **Memphis**, **Arkansas**, **VCU**, **Nebraska**) + Cambridge Apollo OAI + OpenAlex `2020–2026`.
   - `python scripts/harvest_wave1e_cambridge_umd_scholaris.py` -> Newly discovered `api.` DSpace 7 subdomains & Canadian Scholaris network: **University of Maryland (`api.drum.lib.umd.edu`, `654`)**, **Université de Montréal (`umontreal.scholaris.ca`, `639`)**, **McGill (`mcgill.scholaris.ca`, `463`)**, **Western (`uwo.scholaris.ca`, `332`)**, **University of Cambridge (`api.repository.cam.ac.uk`, `98`)**, **Windsor (`94`)**, **Sherbrooke (`47`)**, **Regina (`44`)**, **Brock (`9`)**.
   - `python scripts/harvest_expanded_orcid_pubmed_wikidata.py` -> Fresh **Wikidata SPARQL `wdt:P184`** (`10,024` links), **65-university ORCID v3.0 + Crossref**, and **65-university NIH PubMed methodology** (`326` multi-paper lineages).
   - `python scripts/harvest_wikipedia_infobox_and_crossref_dissertations.py` -> Batch-mines **6,515 English Wikipedia biographies** (`action=query&prop=revisions&rvprop=content`) for `doctoral_advisor`/`doctoral_students` (`6,211` extracted) + **Crossref `type:dissertation`** (`chair`).
   - `python scripts/harvest_department_alumni_and_faculty_websites.py` -> Crawls departmental PhD alumni pages + **ALL `6,405` verified Core Stat faculty personal websites, `/students` subpages, and CV PDFs (`pdftotext -layout`)** (`1,916` graduated PhD advisees).
   - `python scripts/audit_post_global_crawl.py` -> Scrubs noisy scraped/CV/HTML artifacts, cleans parenthetical school suffixes (`(adoptive)`, `(UK)`, `(Montreal Neurological Institute)`), merges duplicate newly-created stubs onto canonical scholars, and purges edge-less orphans before running `deduplicate_hyphen_and_stub_scholars.py` and `audit_and_repair_database.py` (whose Pass 5 uses single-pass `GROUP BY school` for `~11s` `schools` + `meta_stats` + `people_fts` rebuild).

### Stage C — Precision Homonym Guard, `Verified Source` Links, Websites & Portraits
10. `python scripts/enrich_scholar_external_links.py` -> Partitioned **Wikidata SPARQL** (`P549`, `P2381`, `P496` ORCID, `P1960` Google Scholar, `P6634` LinkedIn, `P856` website, `en`/`de`/`fr`/`ru` Wikipedia) + **ORCID `/v3.0/{orcid}/person` `researcher-urls`**.
10. `python scripts/enrich_scholar_photos.py` -> **Wikidata `wdt:P18`** (`240px` Commons thumbnails) + **Wikipedia `pageimages` (`pilicense=any`)** + initial Core Stat homepage portraits.
11. `python scripts/fix_homonym_descendants_and_deep_enrich.py` ->
    - **CRITICAL PRECISION GUARD**: Purges automated ETD/PubMed young-PhD homonym edges (`student.year - advisor.year <= 3`) and ambiguous cross-school `DataCite ETD` fallbacks (preventing recent PhDs like *Yu Zhao*, UCSD 2023, from absorbing engineering advisees of older homonyms).
    - Crawls **140+ Departmental `/people` & `/faculty` Directories** with PhD-school bio verification (catching cross-university moves like *Han Liu*, CMU -> Northwestern) and scans `.edu` portraits with `verify=False` SSL retry and Drupal `/sites/default/files/` compatibility.
12. `python scripts/exhaustive_internet_era_websites_and_photos.py` -> Exhaustive internet-era (`year >= 1980`) personal website & portrait sweep across:
    - **DBLP RDF Graph** (`sparql.dblp.org`, `152k+` `dblp:webpage` triples)
    - **CRAN R-Package Database** (`21,500+` packages via `Rscript`)
    - **Custom GitHub Statistical Handles** (`<handle>.github.io`, e.g. `nozoeli.github.io` for *Yuchao Liu*)
    - **Advisor Webpage -> Hyperlinked Advisee Websites** (`5,280+` Core Stat advisors)
    - **Fastly CDN Keep-Alive `*.github.io` & `sites.google.com/view/*` Slug Probing** with strict concrete `PhD school / affiliation / advisor_schools / advisor / advisee / thesis` bio verification (never accepting name + generic academic words alone).
12b. `python scripts/audit_and_fix_personal_website_precision.py` -> Audits all `*.github.io` & `sites.google.com/view/*` links against concrete biographical anchors + active-student homonym guards and crawls interdisciplinary faculty directories (`BAIR Berkeley`, etc., e.g. *Zeyu Zheng* -> `https://zheng80.github.io/`).
13. `python scripts/deduplicate_hyphen_and_stub_scholars.py` -> Unifies hyphenated/spaced/joined compound-name variants (`Wenxin Zhou` <-> `Wen-Xin Zhou`, `Run-Ze Li` <-> `Runze Li`, `Xu-Ming He` <-> `Xuming He`) and undated (`year IS NULL`) exact-name / middle-initial stubs sharing an institution, advisor, or student, populating all spelling variants into `search_aliases` and rebuilding DAG `descendant_count` + `people_fts`.
14. `python scripts/audit_and_repair_database.py` -> Full-database 5-pass precision audit & repair: (1) purges UC `eScholarship` Master's (`MS`/`MA`) theses via ProQuest degree codes (`_XXXXN_`/`_XXXXM_` vs. doctoral `_XXXXD_`, also enforced directly in `scripts/harvest_web_and_etds.py`), (2) deletes `> 75`-year impossible edges, non-MGP `student.year < advisor.year` homonym collisions, and extreme non-honorary MGP inversions, (3) merges duplicate direct students under the same advisor + duplicate scholars sharing `wikidata_qid`/`orcid`/`wikipedia_url`/`google_scholar_url`, cleans placeholder/shared photos, broken/department LinkedIn URLs, shared/translate homepages, and HTML entities/tags, (4) live-verifies `homepage` & `photo_url` HTTP health on priority Core Stat advisors, and (5) recomputes topological DAG counts, `schools`, `meta_stats`, and `people_fts`.
15. `python scripts/build_dissertation_topic_map.py` -> Fine-tunes the top 6 layers of language-invariant `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (bottom 6 frozen; supports `--reuse-finetuned`) across **162 fine-grained official AMS MSC 2020 (Sections 60 & 62) + modern ML/Biostatistics/Finance classifications** using multi-label soft-target additive-margin cosine loss + empirical **collaborative signal** (within-dissertation multi-concept `PPMI` co-occurrence + shared-advisor/lineage co-occurrence from `edges`), blends `68%` semantic + `32%` collaborative distance into cosine `t-SNE` (`width: 1280`, `height: 560`, 5 ambient Gaussian cluster auras `cx, cy, r`, zero non-statistical bubble labels), and saves `data/dissertation_topic_map.json` + `meta_stats['dissertation_topic_map']` powering `GET /api/topic_map`.

### Stage D — Post-Refresh Finalization, Server Restart & Optional GitHub Push
13. If not using `refresh_recent_data.py`, always recompute `advisor_count`, `student_count`, topological `descendant_count`, `meta_stats['summary']`, and `INSERT INTO people_fts(people_fts) VALUES('rebuild')`.
14. **Restart `python app.py` on port `5050`**: `app.py` caches `ANCESTRY_INDEX` (`famous_ancestors.py` — both upward `_parents` and downward `_children` / `get_descendants_map` powering the `Descendant Of` search filter), `_FUZZY_INDEX`, and `_LUCKY_IDS_CACHE` in memory at startup—restarting is **mandatory** after any `edges` or `people` addition/deletion.
15. **Before `git push` to GitHub**: Run `python scripts/pack_database_parts.py` (regenerating `<= 45 MB` chunks under `data/db_parts/`) and `python scripts/build_gh_pages.py` (regenerating the 100% serverless GitHub Pages site in `docs/` with `static/static_api.js`, `docs/data/people_index.json.gz`, `docs/data/graph.bin.gz`, `docs/data/topic_map.json`, 256 detail shards, 150 static SEO scholar landing pages in `docs/scholars/` instrumented with Google Analytics 4 `G-FE8JRRVYEP`, `robots.txt`, and `sitemap.xml`).

---

## 5. Living Skill Synchronization Rule

Whenever you add a new data source, modify an existing crawler/enricher in `scripts/*.py`, or change database columns / API endpoints, **update both this skill (`.agents/skills/statlink-data-refresh/`) and `.agents/skills/statlineage-blueprint/` in the same turn**.
