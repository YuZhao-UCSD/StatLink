# StatLink — Complete Source Catalog, Cache Files & Crawler Rules

This companion reference documents all **15 upstream data sources and crawlers** powering StatLink (`d:\Jetski\Family Tree`), their `data_raw/` caches, rate limits, anti-bot/SSL pitfalls, and post-refresh benchmark checks.

---

## 1. Cache Files in `data_raw/` (What to Keep vs. Clear on Refresh)

| Cache File (`data_raw/`) | Source | Refresh Guidance |
| :--- | :--- | :--- |
| `mgp_data_new.json` | Mathematics Genealogy Project | Keep unless replacing with a newer MGP snapshot (`--mode full`). |
| `aft_people.csv.gz`, `aft_connect.csv.gz`, `aft_locations.csv.gz` | Academic Family Tree (Zenodo) | Keep unless Zenodo publishes a new AFT dump (`--mode full`). |
| `wikidata_mgp_aft_bridges.json`, `wikidata_stats.json` | Wikidata SPARQL (`P549`, `P2381`, `P184`) | Re-run modulo-20 SPARQL slices (`scripts/finish_wikidata_split.py`) when doing a major foundational refresh. |
| `wikidata_external_links_cache.json` | Wikidata (`P856`, `P1960`, `P6634`, sitelinks) | Delete before running `enrich_scholar_external_links.py` if refreshing newly added Wikidata links. |
| `orcid_researcher_urls_cache.json` | ORCID v3.0 `/person` `researcher-urls` | Incremental dict keyed by ORCID; keeps prior lookups and automatically fetches only newly added ORCIDs. |
| `scholar_photos_cache.json` | Wikidata `P18` + Wikipedia `pageimages` | Delete if re-harvesting updated Commons / Wikipedia thumbnails. |
| `datacite_dissertations_2014_2025.json` | DataCite Works API (`dissertation`) | Cleared by `--clear-recent-caches` so new dissertations are fetched. |
| `openalex_indexed_2015_2020.json` | OpenAlex Polite Pool (`type:dissertation`) | Cleared by `--clear-recent-caches` (mind the `\$0.10`/day anonymous IP quota!). |
| `pubmed_methodology_lineages.json` | NIH PubMed E-Utilities (`40+` journals) | Cleared by `--clear-recent-caches` to harvest newly published papers. |
| `dblp_webpages_cache.json` | DBLP SPARQL (`dblp:webpage`, `152k+` rows) | Cleared by `--clear-recent-caches` (~12s to re-download in `20,000`-row slices). |
| `cran_packages_urls_cache.json` | CRAN `tools::CRAN_package_db()` via `Rscript` | Cleared by `--clear-recent-caches` (~10s to re-extract `21,500+` R packages). |
| `github_stat_handles_cache.json` | GitHub Repo Search (`*.github.io`, `jemdoc`, journals) | Cleared by `--clear-recent-caches` (~2.5 min with polite `6.2s` pacing). |

---

## 2. Per-Source Endpoints, Rate Limits & Hard-Won Pitfalls

### Source 1–3: Foundational Genealogy (`MGP`, `AcademicTree`, `Wikidata`)
- **AcademicTree (`aft_connect.csv.gz`)**: **Strictly filter `relation == 1` (PhD Advisor -> Student)**. Never import `relation == 2` (Postdoc), `3` (Research collaborator), or `0`/`4` (Undergrad/High-school).
- **Wikidata SPARQL (`https://query.wikidata.org/sparql`)**: Full queries over `262k+` `wdt:P549` items hit the `60s` WDQS timeout unless partitioned into 20 slices by final digits (`REGEX(STR(?mgp), "[05]$")`, etc.) with descriptive `User-Agent` and `Accept: application/sparql-results+json`.

### Source 4: UC eScholarship (`https://escholarship.org/graphql` / OAI-PMH)
- Covers all **10 UC campuses** (`UC Berkeley`, `UC Davis`, `UC Irvine`, `UCLA`, `UC Merced`, `UC Riverside`, `UC San Diego`, `UCSF`, `UC Santa Barbara`, `UC Santa Cruz`), bypassing ProQuest's post-2013 embargo with exact `advisors` vs. `committee_members` separation.

### Source 5: Institutional `DSpace 7` REST APIs (`/server/api/discover/search/objects`)
- Unblocks university repositories where ProQuest is restricted (`OhioLINK` for OSU/Case Western/Cincinnati, `UW`, `UMN`, `TAMU`, `Rice`, `Duke`, `Penn`, `MIT`, `Cornell`, `UMD`, `NCSU`, `Virginia Tech`, `Georgia Tech`). Filter `dc.type = Thesis/Dissertation` and `dc.contributor.advisor` (ignoring `dc.contributor.committeeMember`).

### Source 6: School of Public Health `bepress DigitalCommons` OAI-PMH
- Biostatistics PhDs frequently live in School of Public Health repositories (`JHU`, `Harvard`, `UW`, `UMich`, `UNC`, `Columbia`, `UMN`, `Iowa`, `Pitt`, `BU`, `Emory`, `UAB`, `UTHealth`).
- **CRITICAL**: Always request `metadataPrefix=qdc` (Qualified Dublin Core), **not** `oai_dc`—`oai_dc` collapses advisors and readers, whereas `qdc` exposes `<dc:contributor.advisor>`.

### Source 7: French National Theses (`HAL / theses.fr`)
- Endpoint: `https://api.archives-ouvertes.fr/search/` with `docType_s:THESE` and `domain_s:math.math-st` (Statistics) or `domain_s:math.math-pr` (Probability). Extracts `authFullName_s`, `director_s` (PhD advisor), `instStructName_s`, `producedDateY_i`, and `title_s`.

### Source 8: `DataCite` Global Dissertation Registry (`https://api.datacite.org/works`)
- **Zero daily quota**, `page[size]=500`, `resource-type-id=dissertation`.
- Strictly require `contributorType == "Supervisor"` (never `"CommitteeMember"`).
- **Homonym Guard**: Never allow `DataCite ETD` to fall back to a global cross-school advisor match when multiple scholars share that name or when `student.year - advisor.year <= 3`.

### Source 9: `OpenAlex` Polite Pool (`https://api.openalex.org/works`)
- **CRITICAL QUOTA RULE**: Anonymous IP quota is `\$0.10`/day. `title.search:` full-text queries cost `\$0.01`/call (`10` calls exhausts the entire IP!). Indexed `concepts.id` (`C105795698` Statistics, `C62649853` Probability, `C149782125` Econometrics/Biostat) + `type:dissertation` + `publication_year` filters cost `\$0.0001`/call (`1,000` calls/day). Always pass `mailto=` in headers/params.

### Source 10: Departmental Alumni Pages & `UF` Doctoral Commencement Booklets (`pdftotext`)
- Parses `statistics.stanford.edu/people/alumni` and downloads semester Doctoral Commencement PDFs from `commencement.ufl.edu` (since `ufdc.ufl.edu` uses Cloudflare JS challenges), extracting `DOCTOR OF PHILOSOPHY` `Statistics`/`Biostatistics` blocks via `pdftotext -layout`.

### Source 11: 32-University `ORCID v3.0` + `Crossref` Co-Author Bridge
- Queries `https://pub.orcid.org/v3.0/expanded-search/` (**strictly `max_workers <= 8`** with exponential backoff on `HTTP 429`), verifies a true doctoral role (`PhD` / `D.Phil.` / `Sc.D.`) in `/v3.0/{orcid}/educations`, and matches `api.crossref.org/works` co-authors in `[phd_year - 3, phd_year + 2]` against verified faculty at that exact university.

### Source 12: 37-School `NIH PubMed E-Utilities` Statistical Methodology Harvester
- Queries `esearch.fcgi` / `efetch.fcgi` restricted to `40+` core statistical/biostatistical methodology journals (`Biostatistics`, `Biometrics`, `JASA`, `Biometrika`, `Ann Stat`, `Stat Med`, `JCGS`, `Bioinformatics`, etc.) on `2–8` author papers where first author is in `Dept. of Statistics/Biostatistics` at University $U$ and senior (last) author is a verified advisor at $U$ with PhD $\ge 5$ years earlier. Always run `clean_pubmed_duplicates_and_postdocs.py` and `recover_common_name_pubmed_phds.py` afterward.

### Source 13: Faculty Websites, CV PDFs & 140+ Departmental `/faculty` Directories
- Uses `crawler_engine.py` + `fix_homonym_descendants_and_deep_enrich.py`.
- **SSL Intermediate-Certificate Rule**: Many `.edu` servers (`math.ucsd.edu`, `pages.ucsd.edu`, `cseweb.ucsd.edu`, `stat.ufl.edu`) omit intermediate certs in Python's OpenSSL bundle (`SSLCertVerificationError`). Always retry `requests.Session.get()` with `verify=False` upon `SSLError`.
- **Concurrency Rule**: Use a **single shared `ThreadPoolExecutor` with a `FIRST_COMPLETED` sliding window** (`run_bounded_batch`) and `socket.setdefaulttimeout(8.0)`—never instantiate `ThreadPoolExecutor` inside a chunk loop with `shutdown(wait=False)` (which leaks OS threads/sockets).

### Source 14: Wikipedia `pageimages` (`pilicense=any`) & Portrait Scoring Rules
- **Wikipedia Fair-Use Rule**: Always pass `pilicense=any` to `https://{lang}.wikipedia.org/w/api.php?action=query&prop=pageimages&pithumbsize=240` (`en`/`de`/`fr`/`ru`). MediaWiki's default `pilicense=free` silently excludes locally uploaded historic fair-use portraits (*John Tukey*, *George Box*, *Abraham Wald*, *Harold Hotelling*, *William Feller*, *Joseph Doob*).
- **Drupal `/sites/default/files/` Rule**: **Never** put bare `"default"` in URL substring blocklists (`BAD_IMG_WORDS`)—Drupal `.edu` sites store all faculty portraits under `/sites/default/files/...`! Block specific placeholders (`"default-avatar"`, `"default_profile"`, `"default.jpg"`, etc.) and block `"gravatar.com"` / `"wp-includes"` mystery-man silhouettes.
- **Google Scholar Photo Rule**: Never hotlink `scholar.googleusercontent.com/citations?view_op=medium_photo&user=...`—Google rate-limits direct requests with `HTTP 429` (`text/html`).

### Source 15: Exhaustive Internet-Era (`year >= 1980`) Personal Website & Portrait Sweep
- Implemented in `scripts/exhaustive_internet_era_websites_and_photos.py`:
  1. **DBLP SPARQL (`https://sparql.dblp.org/sparql`)**: Always anchor bulk queries on `?p dblp:webpage ?web ; dblp:primaryCreatorName ?name` (`20,000` rows/slice, `~1.3s`/slice; never run unindexed `FILTER(STR(?name) = ...)` over all 4M creators).
  2. **CRAN (`Rscript` `tools::CRAN_package_db()`)**: Extracts personal URLs and `github.com/<handle>` usernames across `21,500+` R packages.
  3. **Custom GitHub Handles (`<handle>.github.io`)**: Pace `api.github.com/search/repositories` at `>= 6.2s`/call (`10 req/min` unauthenticated limit), then probe `https://<handle>.github.io/` directly on **Fastly CDN** (zero GitHub API quota).
  4. **Fastly CDN `*.github.io` & `sites.google.com/view/*` Probing (`audit_and_fix_personal_website_precision.py`)**: Use HTTP `HEAD` over keep-alive `requests.Session` (`404` in `~18ms`) for `*.github.io`, and `allow_redirects=False` (`302 -> accounts.google.com` = non-existent, `200` = live public site) for `sites.google.com/view/*`. Gate every `200 OK` page through `verify_page_matches_scholar()` requiring a concrete match on `school`, `current_affiliation`, `advisor_schools`, advisor surname, advisee full name, or $\ge 2$ rare `thesis` keywords (**never** accept generic `"PhD/Statistics"` text on name alone, and reject `"PhD candidate"` bios at non-alma-mater universities when `year <= 2023`, preventing collisions like CMU student `zeyu-zheng.github.io` vs. UC Berkeley Professor *Zeyu Zheng* `https://zheng80.github.io/`).

---

## 3. Post-Refresh Benchmark Checks (Against `http://127.0.0.1:5050`)

After restarting `python app.py`, verify these regression anchors:
1. **`GET /api/stats`**: `total_people > 550,000`, `core_stat_total > 85,000`, `year_histogram` populated continuously for every year `1920..2026`, `len(featured) == 5`.
2. **`Yu Zhao` (`GET /api/person/4010890`)**: `student_count == 0`, `descendant_count == 0` (confirms homonym guard ran), `homepage == "https://yuzhao-ucsd.github.io/"`, `photo_url` non-empty.
3. **`John Wilder Tukey` (`GET /api/person/15860`)**: `wikipedia_url` and `photo_url` (`pilicense=any`) both non-empty.
4. **`Han Liu` (`GET /api/person/158757`)**: Northwestern `homepage` and `photo_url` non-empty.
5. **`Ery Arias-Castro` (`GET /api/person/24284`)**: UCSD `homepage`, `google_scholar_url`, and `photo_url` non-empty.
6. **`Yuchao Liu` (`GET /api/person/234311`)**: `homepage == "https://nozoeli.github.io/"` and `photo_url == "https://nozoeli.github.io/image1.JPG"`.
7. **`Wenxin Zhou` (`GET /api/person/176298`) & Fuzzy Search**: Unified single record (`Wen-Xin Zhou` UCSD advisees merged onto `176298`), and `GET /api/search?q=wen-xin+zhou`, `GET /api/search?q=wenxinzhou`, and `GET /api/search?q=bradly+effron` return verified top hits via `FuzzySearchIndex`.
8. **`Zeyu Zheng` (`GET /api/person/251465`) & `Descendant Of` Search (`GET /api/search?ancestor_id=14941`)**: `homepage == "https://zheng80.github.io/"` (UC Berkeley IEOR), and `ancestor_id=14941` returns Bradley Efron's descendants with `ancestor_generations` populated in `< 20ms` (`ancestor_mode=direct` returns direct PhD students).
9. **Post-Audit Hygiene (`scripts/audit_and_repair_database.py` + `scripts/harvest_web_and_etds.py`)**: Zero `> 75`-year chronological edge gaps, zero non-MGP `student.year < advisor.year` edges, zero `no-portrait`/shared banner `photo_url`s, zero broken `linkedin.com/in/https:` links, and UC `eScholarship` harvests strictly filter on ProQuest doctoral degree code `_XXXXD_` in `nativeFileName`.
