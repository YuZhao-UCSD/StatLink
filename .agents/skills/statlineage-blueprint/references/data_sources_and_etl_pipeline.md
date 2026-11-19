# StatLineage Data Sources, Entity Resolution & ETL Pipeline Reference

This reference documents every raw data source, SPARQL/GraphQL/REST query, normalization rule, entity-resolution algorithm, discipline classifier, and post-2013 gap-closing harvester required to rebuild `data/stat_genealogy.db` (`553,885` PhD scholars, `593,676` strictly doctoral advisor–student edges, `85,820` Core Stat/Biostat/Prob PhDs, `143,845` Broad Quantitative PhDs across `207` tracked statistical institutions and `3,042` universities) from ground zero.

---

## 1. Foundational Bulk Datasets (`data_raw/`)

### 1.1 Mathematics Genealogy Project (MGP)
- **Target File**: `data_raw/mgp_data_new.json` (`338,532` PhD records).
- **Schema per Record**: `mgp_id` (int), `name`, `school`, `country`, `year` (int), `thesis`, `msc` (e.g., `62—Statistics`, `60—Probability theory and stochastic processes`), `advisors` (list of `mgp_id`), `students` (list of `mgp_id`).
- **Strengths & Recall Blind Spots**:
  - Gold-standard historical lineages reaching back to Gauss (`18231`), Laplace (`108295`), Poisson (`17865`), Chebyshev (`12542`), Markov (`12543`), Kolmogorov, Fisher, and Neyman (`12694`).
  - **Blind Spot 1**: ~40% of MGP records (`136,040` scholars, including Bradley Efron, John Tukey, Peter Bickel, Nan Laird, Peter Bühlmann, Srinivasa Varadhan) have **missing MSC subject codes**.
  - **Blind Spot 2**: Schools of Public Health (Johns Hopkins Bloomberg SPH, Harvard T.H. Chan SPH, UW Biostatistics, UNC Gillings SPH, UF Biostatistics, UMich SPH, etc.) rarely submit Biostatistics PhDs to MGP.

### 1.2 Academic Family Tree (`academictree.org` Zenodo Export)
- **Target Files** (downloadable from the official AcademicTree Zenodo repository):
  - `data_raw/aft_people.csv.gz` (`896,345` scholars: `pid`, `firstname`, `middlename`, `lastname`, `degrees`, `location`, `locid`, `majorarea`, `orcid`, `homepage`).
  - `data_raw/aft_connect.csv.gz` (`cid`, `pid1` [advisor], `pid2` [student], `relation`, `locid`, `startdate`, `stopdate`).
  - `data_raw/aft_locations.csv.gz` (`locid`, `institution`, `department`, `city`, `state`, `country`).
- **CRITICAL Doctoral Filter (`relation == 1`)**:
  - AcademicTree encodes relationship types in `relation`: `0` = research assistant/undergrad, `1` = **graduate/PhD student**, `2` = postdoc, `3` = collaborator, `4` = research scientist.
  - **Strictly filter to `relation == 1`** (and exclude Master's-only degrees when explicit) so postdocs and undergraduates never pollute PhD trees.
- **Historical ProQuest Cutoff**:
  - AcademicTree's bulk import of ProQuest Dissertations & Theses ended in **late 2014**, creating a sharp drop from `2014 -> 2015` unless supplemented by institutional ETDs, DataCite, OpenAlex, ORCID, Commencement PDFs, PubMed methodology lineages, and faculty CV crawls (detailed in Section 5).

### 1.3 Wikidata Scholarly Graph (`query.wikidata.org/sparql`)
- **Target Files**:
  - `data_raw/wikidata_mgp_aft_bridges.json`: `9,547` exact gold-standard identity bridges querying items that simultaneously possess `wdt:P549` (MGP ID) and `wdt:P2381` (AcademicTree ID).
  - `data_raw/wikidata_stats.json`: `10,714` statistical/mathematical scholars and `23,882` curated `wdt:P184` (doctoral advisor) / `wdt:P185` (doctoral student) edges, plus `P69` (educated at), `P108` (employer), `P496` (ORCID), and `P856` (official website).
- **Pagination Tip**: If the public Wikidata SPARQL endpoint times out on large joins, partition queries by numeric ID suffix or modulo ranges (`scripts/finish_wikidata_split.py`).

---

## 2. Canonical Normalization (`scripts/build_database.py`, `scripts/build_master_schools.py`)

1. **Unicode & Diacritic Folding (`fold_ascii`)**:
   - Explicitly map non-NFKD letters (`ø -> o`, `ł -> l`, `ß -> ss`, `đ -> d`, `æ -> ae`, `œ -> oe`), apply `unicodedata.normalize("NFKD", text)`, strip combining marks, lowercase, and collapse whitespace.
2. **Nickname Equivalence Groups (`NICKNAME_GROUPS`)**:
   - 70+ bidirectional given-name equivalence sets (`robert/rob/bob`, `richard/rick/dick`, `william/bill/will`, `james/jim`, `alexander/alex/sasha/aleksei`, `nicholas/nick/nikolai`, `elizabeth/liz/beth`, `katherine/catherine/kathryn/kate`, `kiyosi/kiyoshi`, `vasily/wassily`, etc.) used in both Entity Resolution and search query expansion.
3. **University Canonicalization (`SCHOOL_CANONICAL_MAP` + `EXTRA_SCHOOL_FIXES`)**:
   - Normalizes 350+ multilingual, historical, and campus-fragmented strings into canonical institution names (e.g., `"University of California, Berkeley"`, `"UC Berkeley"`, `"U.C. Berkeley"` -> `"University of California, Berkeley"`; `"The Johns Hopkins Univers"`, `"Johns Hopkins Bloomberg School of Public Health"` -> `"Johns Hopkins University"`; `"University of Florida, Gainesville"` -> `"University of Florida"`; `"Texas A & M University, College Station"` -> `"Texas A&M University"`).
   - Attaches `SCHOOL_ACRONYMS` (`MIT`, `UCB`, `UCSD`, `UCLA`, `JHU`, `UNC`, `UW`, `UMich`, `CMU`, `ETH`, `LSE`, `HSPH`, `NCSU`, `TAMU`, etc.) to `schools.aliases` and `people.search_aliases`.
4. **Master Directory of 207 Statistical Universities (`data/stat_departments_master.json`)**:
   - Generated by `scripts/build_master_schools.py`, covering US ASA/IPEDS Statistics & Biostatistics PhD programs plus flagship Canadian, UK, European, Asian, Israeli, Australian, and Latin American departments.
   - Populates `advisor_schools(person_id, school, advised_at_school)` so faculty who earned their PhD at University A (e.g., Harvard) and advise PhD students at University B (e.g., Johns Hopkins or Stanford) appear under University B's `Faculty / Advisors` filter.

---

## 3. Multi-Evidence Entity Resolution & Strict DAG Construction

### 3.1 Merging `MGP`, `AcademicTree`, and `Wikidata` Without Homonym Conflation
Assign MGP IDs (`1 .. ~350,000`) as primary canonical `people.id` values; assign unmatched AcademicTree records `1,000,000 + aft_pid`, Wikidata-only records `4,000,000+`, and web/ETD/ORCID/PubMed records `5,000,000+` / `6,000,000+`.

An `AFT` record merges into an `MGP` record if **any** of the following hold:
1. **Tier 1 (Exact External ID Bridge)**: Exact `(mgp_id, aft_pid)` pair in `wikidata_mgp_aft_bridges.json` or matching ORCID.
2. **Tier 2 (Dissertation Title Similarity)**: Compatible normalized last name + compatible first initial/nickname + token Jaccard similarity $\ge 0.62$ on non-stopword dissertation titles (length $\ge 4$ tokens).
3. **Tier 3 (Canonical University + Graduation Year)**: Exact normalized full name (or nickname equivalent) + identical canonical PhD `school` + `abs(year_mgp - year_aft) <= 1`.
4. **Tier 4 (Kinship Intersection + Faculty Stub Unification)**:
   - Many professors in AcademicTree were auto-created from ProQuest advisor fields with `year IS NULL` and `school = current_affiliation` while their own PhD exists in MGP.
   - Merge an AFT stub (`year IS NULL`) into an MGP scholar when full/nickname names match **and** either (a) they share at least one already-resolved advisor or student, or (b) the AFT stub's institution matches the MGP scholar's `advisor_schools` / `current_affiliation` and there is no competing homonym in the field (`scripts/close_jhu_and_peer_gaps.py`).

### 3.2 Cycle Sanitization & Exact Topological Descendant Counting
- **Strict DAG Enforcement**: Historical mentorship noise or mutual co-mentorship entries can introduce directed cycles. Run DFS cycle detection; break back-edges where `advisor.year >= student.year` (preferring MGP edges over secondary sources).
- **Exact Descendant Counts (`descendant_count` & `stat_descendant_count`)**:
  - Do **not** sum `student.descendant_count + 1` across children—genealogical DAGs reconverge heavily (co-advisors and shared academic grandchildren), which causes massive double-counting.
  - Instead, sort the DAG in reverse topological order (from leaves upward) and union Python `set`/`int` bitsets of reachable descendants per node (`desc_sets[p] |= desc_sets[child] | {child}`), freeing child sets once all parents are processed to keep peak RAM under `~2.5 GB`.

---

## 4. High-Recall, Precision-Calibrated Discipline Classifier

Every scholar receives `primary_field` $\in$ `{"Statistics", "Biostatistics", "Probability", "Related Quantitative", "Mathematics & Other"}` plus boolean flags `is_core_stat` (`Statistics | Biostatistics | Probability`) and `is_broad_stat` (`is_core_stat` or `Related Quantitative`).

### 4.1 Positive Classification Signals (`scripts/build_database.py`)
1. **Explicit MSC Codes**:
   - `62—Statistics` -> `Statistics` (`is_core_stat = 1`, confidence `0.99`). If dissertation/department matches biostatistical/clinical/epidemiological/genomic markers, refine `primary_field` to `Biostatistics`.
   - `60—Probability theory and stochastic processes` -> `Probability` (`is_core_stat = 1`, confidence `0.99`).
   - `91B84` (Econometrics), `68T05`/`68Q32` (Statistical Learning), `90B`/`90C` (Stochastic OR), `92D30`/`92C50` (Quantitative Epi/Biomathematics) -> `Related Quantitative` (`is_broad_stat = 1`).
2. **Departmental & Source Metadata**:
   - Explicit PhD or dissertation from a `Department of Statistics`, `Department of Biostatistics`, `Statistical Science`, or `Probability` -> `is_core_stat = 1`.
3. **Machine-Learned Dissertation Classifier (Solving the 40% Missing-MSC Gap)**:
   - Train a calibrated `TfidfVectorizer(ngram_range=(1, 2), min_df=3, max_features=120000, sublinear_tf=True)` + `LogisticRegression` model on all `202,492` MSC-labeled MGP dissertations (`MSC 60`, `MSC 62`, quantitative neighbor MSCs, and pure-math negatives).
   - Combine ML probability (`>= 0.72` standalone, or `>= 0.52` with advisor/student lineage support) with curated domain regexes (`CORE_STAT_TITLE_RE`, `BIOSTAT_TITLE_RE`, `PROB_TITLE_RE`) to recover unlabeled statistical pioneers and graduates.
4. **Two-Pass Lineage Consensus Propagation**:
   - If a scholar lacks an explicit non-stat MSC code (e.g., `14—Algebraic Geometry` or `55—Algebraic Topology`) and `>= 60%` (at least 3) of their PhD students are `is_core_stat = 1`, or their advisor is `is_core_stat = 1` at a core department without contradicting thesis keywords, propagate statistical membership.

### 4.2 Crucial Precision Calibration on `AFT`-Only Subtrees (`scripts/complete_2014_2025_gap_and_aft_precision.py`)
- **The Pre-2015 Inflation Trap**: Multi-hop lineage propagation from early biometricians (e.g., Galton/Pearson/Wright) into AcademicTree's `896k` ProQuest graph can pull non-statistical biology, psychology, education (`Walden University`), ecology (salamander mimicry, songbirds), and humanities dissertations into `is_core_stat = 1`, creating an artificial pre-2015 bulge (`~11,750` rows).
- **Calibration Rule**: For rows with `sources = 'AFT'` (no MGP/ETD/ORCID statistical source), require **either** an explicit statistical/biostatistical area/department **or** a positive match on statistical/probabilistic/biostatistical dissertation terminology (`CORE_STAT_TITLE_RE | BIOSTAT_TITLE_RE | PROB_TITLE_RE`) without matching `NON_STAT_DOMAIN_REJECT_RE`. Reclassify non-matching propagated `AFT` rows to `primary_field = 'Related Quantitative', is_core_stat = 0, is_broad_stat = 1` (**delete 0 records** so all lineages remain unbroken).

---

## 5. Closing the Post-2013 ProQuest & Cloudflare Recall Gap (`2014–2026`)

Because `academictree.org` stopped bulk-ingesting ProQuest in late 2014 and several university repositories (`jscholarship.library.jhu.edu`, `ufdc.ufl.edu`, etc.) sit behind Cloudflare bot challenges, execute these six targeted harvesters in order:

### 5.1 UC eScholarship GraphQL (`scripts/harvest_web_and_etds.py`)
- **Endpoint**: `POST https://escholarship.org/graphql`
- **Units Harvested**: All 10 UC campuses (`ucb_etd`, `ucsd_etd`, `ucla_etd`, `ucd_etd`, `ucsb_etd`, `uci_etd`, `ucsc_etd`, `ucr_etd`, `ucsf_etd`, `ucm_etd`).
- **Fields Extracted**: Author, dissertation title, publication date (`2010–2026`), department (`Statistics`, `Biostatistics`, `Mathematics`, `Data Science`), and `advisors` array.

### 5.2 University DSpace 7 REST & DigitalCommons OAI-PMH (`qdc`) Feeds
- **Scripts**: `scripts/harvest_web_and_etds.py`, `scripts/crawl_all_schools_and_professors.py`, `scripts/close_jhu_and_peer_gaps.py`, `scripts/fill_post2014_drop.py`.
- **DSpace 7 `/server/api/discover/search/objects` Repositories**: MIT (`dspace.mit.edu`), UPenn/Wharton, Duke (`dukespace.lib.unc.edu` / Duke), Cornell (`ecommons.cornell.edu`), UMN (`conservancy.umn.edu`), UW-Madison, Virginia Tech, Toronto (`tspace.library.utoronto.ca`), Georgia Tech (`repository.gatech.edu`), Rice, Kansas State, Ohio State (`OhioLINK`).
- **DigitalCommons OAI-PMH (`?verb=ListRecords&metadataPrefix=qdc`) Repositories**: SMU, University of South Carolina, Clemson, Missouri, Baylor, UTHealth Houston SPH, VCU Biostatistics, USF Biostatistics, UAB Biostatistics, MUSC Biostatistics, UConn, Western Ontario, Nebraska-Lincoln, Iowa.
- Extract `dc.contributor.advisor` (excluding `dc.contributor.committeeMember`) and `dc.subject` / department tags.

### 5.3 Global Dissertation Registries: `HAL / theses.fr`, `DataCite`, and `OpenAlex`
1. **HAL / `theses.fr` (`api.archives-ouvertes.fr/search/`)**:
   - Query `docType_s:THESE` with `domain_s:math.math-st` (Statistics) and `domain_s:math.math-pr` (Probability). Extracts `authFullName_s`, `director_s` (PhD advisor), `instStructName_s`, `producedDateY_i`, and `title_s` (`8,900+` French PhDs).
2. **DataCite REST API (`api.datacite.org/works`, `scripts/harvest_datacite_2014_2025_dissertations.py`)**:
   - **Zero daily quota**, `page[size]=500`, filter `resource-type-id=dissertation` and `publication-year=2014..2026`.
   - Inspect `contributors[]` where `contributorType == "Supervisor"` (strictly ignoring `"CommitteeMember"`). Covers CMU (`KiltHub`), Purdue, Imperial College London, UCL, ETH Zürich, Waterloo, Virginia Tech, Caltech, etc.
3. **OpenAlex Polite Pool (`api.openalex.org/works`, `scripts/harvest_openalex_indexed_2015_2020.py`)**:
   - **CRITICAL QUOTA RULE**: OpenAlex anonymous daily IP quota is `\$0.10`/day. `title.search:` full-text queries cost `\$0.01`/call (`10` calls exhausts the entire IP!), whereas indexed `concepts.id` / `topics.id` + `type:dissertation` + `publication_year` filters cost `\$0.0001`/call (`1,000` calls/day). Always pass `mailto=` in headers/params, use indexed concept filters (`C105795698` Statistics, `C62649853` Probability, `C149782125` Econometrics/Biostat), and resolve repository hostnames (`Harvard DASH`, `UMich Deep Blue`, `Texas A&M OakTrust`, `Oxford ORA`, `Cambridge Apollo`, etc.) via `REPO_SUBSTRING_MAP`.

### 5.4 Departmental Alumni Pages & Commencement PDFs (`scripts/harvest_uf_commencements_and_orcid_retry.py`)
- **Stanford Statistics PhD Alumni**: Parse `statistics.stanford.edu/people/alumni` (`454` PhDs through 2026 with exact thesis, year, and advisor).
- **University of Florida Official Doctoral Commencement PDFs (`2013–2026`)**:
  - Because `ufdc.ufl.edu` is behind Cloudflare, download all `36` semester commencement booklet PDFs from `commencement.ufl.edu`, run `pdftotext -layout` (MiKTeX / Poppler), parse `DOCTOR OF PHILOSOPHY` candidate blocks with major `Statistics` or `Biostatistics`, and resolve initialed chairs (e.g., `S. S. Wu` -> `Samuel S. Wu`, recovering graduates like **Jing Liu**, UF Biostatistics PhD 2017).

### 5.5 Rate-Limit-Safe 32-University `ORCID v3.0` + `Crossref` Harvester
- **Scripts**: `scripts/close_jhu_and_peer_gaps.py` and `scripts/harvest_uf_commencements_and_orcid_retry.py`.
- Query `https://pub.orcid.org/v3.0/expanded-search/` for each of 32 Cloudflare-gated universities combined with `"Statistics"` / `"Biostatistics"` + `"PhD"`.
- **Concurrency Rule**: Never nest thread pools against `pub.orcid.org` (`>16` concurrent requests trigger silent `HTTP 429` drops). Use a single `ThreadPoolExecutor(max_workers=8)` with exponential retry backoff on `429`/`5xx`.
- Fetch `/v3.0/{orcid}/educations` to verify a true `PhD` / `D.Phil.` / `Sc.D.` role in `Statistics` or `Biostatistics` (rejecting MS/MPH/BS/Postdoc entries). When ORCID lacks an explicit advisor field, query `api.crossref.org/works` for papers published by the student within `[phd_year - 3, phd_year + 2]` and match co-authors against verified faculty advisors at that exact university.

### 5.6 37-School `NIH PubMed E-Utilities` Statistical Methodology Harvester
- **Scripts**: `scripts/harvest_pubmed_methodology_lineages.py`, `scripts/clean_pubmed_duplicates_and_postdocs.py`, `scripts/recover_common_name_pubmed_phds.py`.
- Queries `esearch.fcgi` / `efetch.fcgi` (`2012–2026`) restricted to `40+` core statistical, biostatistical, bioinformatics, statistical genetics, and neuroimaging methodology journals (`Biostatistics`, `Biometrics`, `JASA`, `Stat Med`, `Biometrika`, `Ann Stat`, `Ann Appl Stat`, `JCGS`, `Bioinformatics`, `Genet Epidemiol`, `NeuroImage`, etc.).
- **High-Precision Linkage Criteria**:
  1. Small-team paper (`2–8` authors).
  2. **First author** has an explicit `Department of Biostatistics` / `Statistics` / `Statistical Science` affiliation at University $U$.
  3. **Last (senior) author** matches a verified faculty advisor at University $U$ whose own PhD is $\ge 5$ years prior to paper publication.
  4. **Anti-Postdoc / Anti-Colleague Guard**: Reject if the first author already holds an earlier PhD in the database at the same university ($\ge 3$ years earlier) or already advises $\ge 3$ students.
  5. **Common-Name Homonym Recovery (`recover_common_name_pubmed_phds.py`)**: Do **not** blindly skip a first author just because an older PhD at *another* university shares the same common name (e.g., **Lei Huang**, JHU Biostatistics PhD 2015 under *Ciprian Crainiceanu*, collided with a 2002 USC PhD named *Lei Huang*). Only skip external-school matches if external evidence (ORCID/homepage) confirms the person moved there as a postdoc; otherwise create a distinct university-scoped PhD node.

### 5.7 Faculty Personal Websites & CV PDF Crawler (`crawler_engine.py`)
- Batch-crawls `1,600+` faculty homepages (`/people`, `/students`, `/group`, `/alumni`) and CV PDFs (`pdftotext -layout`).
- **SSL Intermediate-Certificate Fallback**: University departmental servers (e.g., `stat.ufl.edu`) frequently omit intermediate SSL certificates on Windows. Wrap `requests.Session.get()` in `safe_get()` that retries with `verify=False` exclusively upon `requests.exceptions.SSLError`.
- **Strict Section Parser**: In HTML and CV PDFs, only extract names inside explicit `PhD Students` / `Doctoral Advisees` / `Dissertation Chair` sections, immediately halting extraction when encountering `Postdoctoral`, `Master's / MS`, `Undergraduate`, or `Dissertation Committee Member` headings.

---

## 6. Post-Update Database Finalization Invariants

Whenever any supplemental harvest script modifies `people` or `edges`, **always** run the four finalization steps before committing:
1. Recompute `advisor_count` and `student_count` on `people` from `edges`.
2. Re-run exact reverse-topological DAG set propagation to update `descendant_count` and `stat_descendant_count`.
3. Rebuild `schools`, `advisor_schools`, and `meta_stats['summary']` (`total_people`, `total_edges`, `core_stat_total`, `broad_stat_total`, `by_field`, `core_stat_schools`, `total_schools`).
4. Rebuild the FTS5 full-text index:
   ```sql
   INSERT INTO people_fts(people_fts) VALUES('rebuild');
   ```
