# StatLineage Backend API, Ancestry Engine & Frontend UI Specification

This reference defines the complete SQLite database schema, the sub-millisecond `AncestryIndex` engine (`famous_ancestors.py`), the strictly read-only Flask API (`app.py`), the custom D3 v7 layered DAG visualizer, Light/Dark theming tokens, and mobile/tablet responsive architecture (`static/index.html`, `static/styles.css`, `static/app.js`).

---

## 1. SQLite Schema (`data/stat_genealogy.db`)

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE people (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    norm_name TEXT NOT NULL,
    search_aliases TEXT,
    school TEXT,
    raw_school TEXT,
    country TEXT,
    year INTEGER,
    thesis TEXT,
    mgp_subject TEXT,
    primary_field TEXT NOT NULL,          -- 'Statistics' | 'Biostatistics' | 'Probability' | 'Related Quantitative' | 'Mathematics & Other'
    is_core_stat INTEGER NOT NULL,        -- 1 for Statistics / Biostatistics / Probability
    is_broad_stat INTEGER NOT NULL,       -- 1 for Core + Related Quantitative
    field_confidence REAL,
    classification_reason TEXT,
    current_affiliation TEXT,
    research_areas TEXT,
    mgp_id INTEGER UNIQUE,
    aft_pid INTEGER UNIQUE,
    wikidata_qid TEXT,
    orcid TEXT,
    homepage TEXT,
    wikipedia_url TEXT DEFAULT '',
    google_scholar_url TEXT DEFAULT '',
    linkedin_url TEXT DEFAULT '',
    photo_url TEXT DEFAULT '',
    proquest_id TEXT,
    sources TEXT NOT NULL,                -- Comma-joined provenance tags (e.g. 'MGP,AFT,Wikidata')
    advisor_count INTEGER NOT NULL DEFAULT 0,
    student_count INTEGER NOT NULL DEFAULT 0,
    descendant_count INTEGER NOT NULL DEFAULT 0,
    stat_descendant_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE edges (
    advisor_id INTEGER NOT NULL,
    student_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (advisor_id, student_id)
);

CREATE TABLE schools (
    school TEXT PRIMARY KEY,
    norm_school TEXT NOT NULL,
    aliases TEXT,                         -- Comma-separated acronyms (e.g. 'UCB, Berkeley, Cal')
    country TEXT,
    core_stat_count INTEGER NOT NULL,
    broad_stat_count INTEGER NOT NULL,
    total_phd_count INTEGER NOT NULL,
    min_year INTEGER,
    max_year INTEGER
);

CREATE TABLE advisor_schools (
    person_id INTEGER NOT NULL,
    school TEXT NOT NULL,
    advised_at_school INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (person_id, school)
);

CREATE TABLE meta_stats (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL
);

CREATE VIRTUAL TABLE people_fts USING fts5(
    name,
    search_aliases,
    school,
    thesis,
    research_areas,
    content='people',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE INDEX idx_people_norm_name ON people(norm_name);
CREATE INDEX idx_people_school ON people(school);
CREATE INDEX idx_people_year ON people(year);
CREATE INDEX idx_people_core_stat ON people(is_core_stat, stat_descendant_count DESC, student_count DESC);
CREATE INDEX idx_people_broad_stat ON people(is_broad_stat, stat_descendant_count DESC);
CREATE INDEX idx_people_field ON people(primary_field, stat_descendant_count DESC);
CREATE INDEX idx_edges_advisor ON edges(advisor_id);
CREATE INDEX idx_edges_student ON edges(student_id);
CREATE INDEX idx_schools_core ON schools(core_stat_count DESC);
CREATE INDEX idx_adv_schools_school ON advisor_schools(school);
CREATE INDEX idx_adv_schools_person ON advisor_schools(person_id);
```

---

## 2. World-Famous Scientist Ancestor Engine (`famous_ancestors.py`)

### 2.1 Curated Catalog (`FAMOUS_SCIENTISTS_LIST` / `FAMOUS_BY_ID`)
Curates **109 verified historical & modern pioneers** mapped to exact `people.id` keys across three categories:
- **`Physics & Science`**: Copernicus (`126177`), Kepler (`127098`), Galileo (`134975`), Huygens (`125561`), Newton (`74313`), Maxwell (`105806`), Helmholtz (`49057`), Gibbs (`132578`), Boltzmann (`13105`), Planck (`20750`), Rutherford (`50699`), Einstein (`53269`), Bohr (`105783`), Born (`18245`), Schrödinger (`53005`), Heisenberg (`51469`), Fermi (`14167`), Dirac (`18524`), Feynman (`91222`), von Neumann (`53213`), Church (`8011`), Turing (`8014`), Shannon (`42920`), Wiener (`25222`), Nash (`18590`), Judea Pearl (`70481`), Arrow (`51175`), Granger (`42695`), Heckman (`198375`), etc.
- **`Mathematics`**: Leibniz (`60985`), Jacob/Johann/Daniel Bernoulli (`54440`, `53410`, `108998`), Euler (`38586`), Lagrange (`17864`), Laplace (`108295`), Fourier (`17981`), Poisson (`17865`), Gauss (`18231`), Cauchy (`55177`), Dirichlet (`17946`), Weierstrass (`7486`), Riemann (`18232`), Klein (`7401`), Poincaré (`34227`), Hilbert (`7298`), Lebesgue (`86693`), Hardy (`17806`), Emmy Noether (`6967`), Weyl (`7373`), Mandelbrot (`60791`), etc.
- **`Statistics & Probability`**: C. S. Peirce (`24099`), Galton (`30175`), Chebyshev (`12542`), Markov (`12543`), Lyapunov (`31015`), Karl Pearson (`30176`), Borel (`39071`), Lévy (`33053`), R. A. Fisher (`30187`), Neyman (`12694`), Cramér, Kolmogorov, Hotelling, Wald, Doob, Feller, Tukey, Rao, Blackwell (`4209`), Cox, Kiefer, Le Cam, Bahadur, Dempster, Box, Huber, Wahba, Efron (`14941`), Bickel, Brillinger, Varadhan, Donoho, Laird, Tibshirani, Speed, Rubin, Jordan, Hall, etc.

### 2.2 Performance Architecture (`AncestryIndex`)
- **NEVER use SQLite `WITH RECURSIVE ... UNION ALL`** on ancestral DAGs: 18th- and 19th-century European lineages reconverge heavily, causing exponential path blowup (`13+ seconds` per query). Also avoid running `SELECT COUNT(*) FROM edges` on every request (`~8ms` overhead per card).
- Instead, `AncestryIndex.ensure_loaded(db)` loads `edges` once into in-memory Python `dict[int, tuple[int, ...]]` maps (`_parents` upward and `_children` downward) guarded by a thread lock and boolean `_loaded = True` (~`0.35s` cold start once; **`0.07ms` per upward BFS** and **`0.01–2ms` per downward descendant BFS** thereafter).
- **`get_descendants_map(db, ancestor_id, direct_only=False)`**: Visited-set downward BFS over `_children` returning `{descendant_id: min_generations_down}` (excluding `ancestor_id`), handling even `70,000+`-descendant pioneers (Gauss, Euler, Neyman) in `< 2ms` for `/api/search`.
- **`_walk_upward(person_id, max_hops=60)`**: Standard visited-set BFS returning `(dist_by_ancestor, next_step_down_toward_person)`.
- **Category-Diverse Highlighting (`_select_highlights`)**: Selects up to `6` `highlighted` ancestors balancing closest `Statistics & Probability` pioneers, `Mathematics` titans, and `Physics & Science` icons (sorted by `fame_score - 0.65 * generations`), placing remaining matches into `more`.
- **`build_lineage_subgraph(db, student_id, ancestor_id)`**: Takes the upward-reachable set from `student_id`, restricts downward edges to that set, and runs a forward BFS from `ancestor_id` to recover all directed paths `ancestor_id ->* student_id` (falling back to the shortest chain if `> 42` braided bridge nodes exist).

---

## 3. Strictly Read-Only Backend REST API (`app.py`, Port `5050`)

**Security, Cleanliness, Cache & Portable DB Rule**: Expose **zero** `POST`, `PUT`, `DELETE`, live-scrape, or database-write endpoints. Set `SEND_FILE_MAX_AGE_DEFAULT = 0` and apply `@app.after_request` `Cache-Control: no-store, no-cache, must-revalidate, max-age=0` on `/` and `/static/*` so Flask 2.x never caches stale `index.html` for 12 hours (`max-age=43200`). Before opening `data/stat_genealogy.db`, `ensure_database_ready()` checks if the unpacked SQLite file is absent (e.g. right after `git clone`) and stream-decompresses `data/db_parts/stat_genealogy.db.gz.part*` (`<= 45 MB` chunks created by `scripts/pack_database_parts.py`) via `zlib.decompressobj(16 + zlib.MAX_WBITS)`. All routes are `GET`:

1. **`GET /`** -> Serves `static/index.html`.
2. **`GET /api/stats`** -> Returns:
   - `summary`: Global counts (`core_stat_total`, `broad_stat_total`, `total_people`, `total_edges`, `core_stat_schools`, `total_schools`, `by_field`) plus `year_histogram` (`1920..2026` annual counts grouped by `year` and `primary_field` for `is_core_stat = 1`).
   - `featured`: **5 representative scholars across 5 distinct historical eras** for the `Notable Trees` bar:
     1. `18231` — **Carl Friedrich Gauss (`1799`)** (Classical Foundations)
     2. `12694` — **Jerzy Neyman (`1924`)** (Early 20th-Century Mathematical Statistics)
     3. `4209` — **David Blackwell (`1941`)** (Mid-Century Probability & Bayesian Decision Theory)
     4. `14941` — **Bradley Efron (`1964`)** (Late 20th-Century Computational Statistics)
     5. `47762` — **Xihong Lin (`1994`)** (Modern Biostatistics & Statistical Genomics)
   - `top_schools`: Top 25 universities ordered by `core_stat_count DESC`.
3. **`GET /api/random_person`** -> Uniformly samples from an in-memory cached list (`_LUCKY_IDS_CACHE`) of connected Core Stat/Biostat/Prob PhDs (`is_core_stat = 1 AND (advisor_count > 0 OR student_count > 0)`) in `< 0.1ms` to power the **`I'm Feeling Lucky`** button.
4. **`GET /api/schools?q=&scope=&limit=25`** -> Autocomplete over `schools` prioritizing exact acronym matches (`aliases`), prefix matches, and Damerau-Levenshtein fuzzy fallback (`FuzzySearchIndex.fuzzy_match_schools`) for typo queries (e.g. `"stanfrod"`, `"berkely"`).
5. **`GET /api/master_schools`** -> Enriches the `207` institutions in `data/stat_departments_master.json` with live `core_phd_graduates` and `core_faculty_advisors` (from `advisor_schools`).
6. **`GET /api/search?q=&school=&school_role=both|graduates|faculty&ancestor_id=&ancestor_mode=all|direct&year_min=&year_max=&scope=core|Statistics|Biostatistics|Probability|broad|all&sort=&page=1&per_page=36`**:
   - Uses `people_fts` with `bm25(people_fts, 10.0, 6.0, 2.0, 1.5, 1.5)`, first-token nickname OR-expansion, compound/hyphen expansion (`wen-xin` / `wen xin` <-> `wenxin`), and prefix wildcards.
   - **Academic Descendants Filter (`ancestor_id` + `ancestor_mode=all|direct`)**: Resolves `{descendant_id: generations_down}` in `< 2ms` via `ANCESTRY_INDEX.get_descendants_map(db, ancestor_id, direct_only=(ancestor_mode == "direct"))`, filters `people` via a pure read-only `p.id IN (...)` integer-list clause (zero bind-parameter limit, zero temp-table writes), composes freely with `q`, `school`, `year_min`/`year_max`, `scope`, and `sort`, returns `"ancestor": {id, name, school, year}`, and attaches `"ancestor_generations"` (`N gens ↓`) to every returned card.
   - **Google-Style Fuzzy Search (`FuzzySearchIndex` in `app.py`)**: Maintains an in-memory character-3-gram vocabulary + real-scholar co-occurrence index (`token_to_pids`) and bounded Damerau-Levenshtein (`<= 2` edits, including transpositions, umlaut folding `ue/oe/ae`, and no-space concatenation splitting such as `"bradleyefron"` or `"wenxinzhou"`). Automatically triggers when exact FTS hits `< 5` or `strong_name_hits == 0`, returning `"fuzzy_query"` for SERP display.
   - **High-Recall Cross-Scope Rescue**: When `q` is non-empty (or `ancestor_id` has 0 hits in restricted scope) and `scope != "all"`, seamlessly rescues broader-scope matches and sets `"expanded_from_all": true`.
   - Attaches `famous_ancestors_preview` (top 3 famous ancestors) and `famous_self` to every result card.
6. **`GET /api/person/<int:person_id>`** -> Full scholar dossier: metadata, `photo_url` (also included on `row_to_person_brief` across search/advisor/student payloads), verified external profile links (`homepage`, `wikipedia_url`, `google_scholar_url`, `linkedin_url`, `orcid`, `wikidata_qid`, `mgp_id`, `aft_pid`), `advisors`, `students`, up to 40 `siblings` (co-students sorted by closest graduation year), `famous_self`, and `famous_ancestors` (`highlighted`, `more`, and step-by-step `chain` arrays).
7. **`GET /api/tree/<int:person_id>?up_depth=6&down_depth=2&max_children=45`** -> Layered ego-genealogy DAG around `person_id`:
   - Walks upward `up_depth` (`<= 45`) generations; if an ancestor is reached via multiple paths, pushes `gen_offset` to the **most negative step** (`-step < gen_offset[aid]`) so arrows always point strictly downward.
   - Walks downward `down_depth` (`<= 5`) generations, capping children per node (`max(max_children, 90)` for focal person, `max_children` for descendants, prioritized by `is_core_stat DESC, descendant_count DESC`) and recording `hidden_student_count`.
8. **`GET /api/lineage_path?student_id=&ancestor_id=`** -> D3 subgraph tracing all directed paths from `ancestor_id` down to `student_id`.
9. **`GET /api/connection?id1=&id2=`** -> Upward BFS from both scholars up to 45 generations to find direct ancestry or the Most Recent Common Academic Ancestor (`MRCA`) minimizing `dist1[a] + dist2[a]`.
10. **`GET /api/topic_map`** -> Returns the precomputed 2D Statistics-fine-tuned, language-invariant LLM-embedded dissertation topic landscape (`meta_stats['dissertation_topic_map']` / `data/dissertation_topic_map.json`, generated by `scripts/build_dissertation_topic_map.py` by fine-tuning the top 6 transformer layers of `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`—freezing the bottom 6 layers to preserve 50+-language alignment—with two-level hierarchical ArcFace/CosFace additive-margin cosine loss ($m=0.12$, $\mathcal{L}_{\text{fine (35)}} + 0.70 \cdot \mathcal{L}_{\text{macro (5 domains)}}$, label smoothing `0.03`, stratified 90/10 train/val split up to `1,400`/class, up to `25` epochs with cosine LR annealing and early stopping on `val_loss`), unifying same-subfield sub-clusters (e.g., `treatment effects` + `observational studies` → `causal inference`), and compact cosine t-SNE (`perplexity=32`, `early_exaggeration=18`, with statistical cousin affinity priors such as `causal inference ↔ experimental design` and `survival analysis ↔ clinical trials`) PCA-aligned horizontally into a `1280×560` widescreen viewBox with `0.52×` inter-cluster pull / `0.84×` intra-cluster scale so adjacent domains naturally overlap into one unified continent).

---

## 4. Frontend UI, Visual Semantics & Responsive Architecture (`static/`)

### 4.1 Clean Minimalist Layout Rules (Strict User Preferences)
- **Website Name**: **`StatLink`** (`<title>StatLink — Academic Family Tree of Statistics, Biostatistics & Probability</title>`).
- **Top Header (`index.html`)**: Google-style bannerless bar (`background: transparent; border: none; box-shadow: none;`) displaying **1-word** navigation links (**`Search`**, **`Tree`**, **`Kinship`**, **`Analytics`**), an **`About`** button (`#aboutLinkBtn`, no hover tooltip; clicking opens the minimalist `#aboutModal` dialog showing `"Built by a statistician who knows a bit about search."` and a **`Visit Homepage ↗`** button linking to `https://yuzhao-ucsd.github.io/`), and the circular Light/Dark theme toggle button on the right (**no** banner border/box, **no** multi-word tab badges). On the `Search` tab (`body[data-active-view="explore"]`), `.topbar` has no top-left logo; on **every subtab (`Tree`, `Kinship`, `Analytics`)**, `.topbar` displays a clickable **`StatLink`** logo (`#topbarLogoBtn`) on the top-left that returns immediately to the centered `Search` front page. Clicking `Search` while already viewing search results also resets back to the centered `StatLink` homepage.
- **View 1 (`Search` — Google-Homepage-Style Minimalist Design & Pinned SERP)**:
  - **Two-Mode Responsive Layout (`#exploreLayout`)** controlled by `isExploreHomeState()` in `static/app.js`:
    1. **Front-Page Mode (`.explore-layout.is-home`)** — Active by default when `query`, `school`, `ancestorId`, and `yearMin`/`yearMax` are empty, `scope === "core"`, and `browseMode === false`:
       - **Google-Homepage Optical Centering**: Uses animatable flex spacers (`.explore-layout.is-home::before { flex: 0.86 1 0px; }` and `::after { flex: 1.14 1 0px; }`) so the search module sits at the exact Google homepage optical vertical & horizontal center across every monitor resolution, smoothly gliding to `0.24 : 1.76` when `.filters-open` unfolds.
       - Centered two-tone wordmark **`StatLink`** (`.google-wordmark`) + one-line subtitle `Academic Family Tree of Statistics, Biostatistics & Probability`.
       - **Pill Search Box with Google Fuzzy Autocomplete Dropdown (`#mainSearchBoxWrap` + `#mainSearchDropdown`)**: Typing in `#mainSearchInput` fetches top 8 exact/fuzzy suggestions (`140ms` debounce) and merges `#mainSearchDropdown` seamlessly into the search pill (`border-radius: 24px 24px 0 0` + `0 0 24px 24px` unified floating card, supporting `ArrowUp`/`ArrowDown` preview) **without leaving the front page**. Only selecting a dropdown item (`mousedown`) or pressing `Enter` invokes `submitMainSearch()` to display the SERP results page.
       - Two understated Google-style action buttons beneath the search bar (`.google-actions-row`): **`Filters & Universities ▾`** (`#openFiltersHeroBtn`, toggles the inline collapsible filter panel directly below the buttons) and **`I'm Feeling Random`** (`#feelingLuckyBtn`, calls `GET /api/random_person` and jumps directly into a random connected statistical researcher's family tree).
       - Centered **`Notable Trees:`** quick-launch row displaying the 5 era-spanning pioneers formatted as `Name (Year)`: `Carl Friedrich Gauss (1799)`, `Jerzy Neyman (1924)`, `David Blackwell (1941)`, `Bradley Efron (1964)`, `Xihong Lin (1994)`.
    2. **Google-Pinned SERP Mode (`.explore-layout.has-results`)** — Activated when the user selects a dropdown suggestion, presses `Enter` in `#mainSearchInput`, clicks `Show Results`, or applies any school/year/scope/sort filter:
       - Hides `.google-hero-brand`, `#feelingLuckyBtn`, **and `#featuredStrip` (`Notable Trees`)**.
       - **Sticky Pinned Search Header (`#searchHeroSticky`)**: Pins `position: sticky; top: 0; z-index: 125;` full-bleed across the top of the viewport (gaining elevation shadow `.is-scrolled` when scrolling down), placing the clickable **`StatLink`** logo (`#serpHomeLogoBtn`, which resets back to the Search front page on click) **directly to the left of `#mainSearchBoxWrap`**, followed by **`Filters & Universities ▾`** and the inline `#inlineFiltersCollapse` accordion.
       - Compact `.results-toolbar` showing result count, optional `"Showing fuzzy matches for …"` hint, and active filter tags (`School: ... ×`, `Descendant of: ... ×` / `Student of: ... ×`, `Years: ... ×`, `Scope: ... ×`). Each `.scholar-card` also displays `N gens ↓` when `ancestorId` is active and makes `🌳 X desc.` (`.desc-filter-inline`) clickable to filter by that scholar's descendants.
  - **Google-SERP-Style Browser History (`pushState` / `replaceState` / `popstate` in `static/app.js`)**:
    - `commitHistoryState()` records snapshots across Search Home (`/`), Search SERP (`/#search?q=...&anc=...`), Family Tree Visualizer (`/#person=<id>`), Kinship (`/#kinship`), and Sources (`/#sources`).
    - Searching for a scholar and opening their tree pushes distinct history states so clicking the **Browser Back button** from `Family Tree Visualizer` returns directly to the exact Search SERP (preserving query, filters, and pagination), and pressing Back again returns to the centered `StatLink` front page (while depth-stepper tweaks on the same focal scholar use `replaceState` to avoid history spam).
  - **Smooth 60fps Inline Accordion (`#inlineFiltersCollapse` -> `#inlineFiltersPanel`, Desktop & Mobile)**:
    - Never uses `display: none` or discrete `justify-content` changes. Animates `flex-grow` spacers alongside `grid-template-rows: 0fr <-> 1fr`, `opacity`, `margin-top`, and subtle inner `transform` (`0.38s cubic-bezier(0.22, 1, 0.36, 1)`) **inline directly below the action buttons and above `Notable Trees:`** (`overflow-ready` enables `overflow: visible` after expand so `#schoolDropdown` and `#ancestorDropdown` float cleanly without clipping).
    - Contains `Disciplinary Scope` pills (`Core Stat · Biostat · Prob`, `Statistics`, `Biostatistics`, `Probability`, `+ Related Quantitative`, `All` — no hint paragraph), a **compact 3-column grid** (`minmax(0, 1.35fr) 150px minmax(0, 1.35fr)` on desktop; `1fr` on `<= 768px`) with:
      1. `University (PhD or Faculty)` autocomplete + role chips (`Grads + Faculty`, `PhD Alumni`, `Faculty / Advisors`),
      2. Compact **`PhD Year`** (`150px` wide, spinbutton-free `From`–`To` `.year-compact-input` boxes with no bulky era-chip row), and
      3. **`Descendant Of`** (directly right of `PhD Year`: `#ancestorFilterInput` scholar autocomplete + `#ancestorModeGroup` chips `All Descendants` | `Direct Students`),
      followed by a footer row with `Sort By` dropdown, `Reset All`, and `Show Results` (**no** `Top PhD Institutions` mini-list and **no** `Search Live MGP` box).
- **View 2 (`Tree` — Full-Bleed Google-Maps-Style Minimalist Canvas)**:
  - **Zero top toolbar bar & no `Export SVG` button**: Full-bleed `#treeCanvasContainer` overlaid with sleek Google-Maps-style floating controls:
    1. **Top-left floating pill search (`.gmap-search-pill`)**: Compact rounded search input (`Search in tree…`) + optional floating `← Full Tree` pill (`#exitTraceModeBtn`) when tracing a famous ancestor path.
    2. **Bottom-right floating camera & depth dock (`.gmap-bottom-right`)**:
       - Compact **Generations pill (`#treeDepthToggleBtn`, `6↑ · 2↓`)** that unfolds a clean floating popover card (`#treeDepthPopover`) on click for adjusting `Ancestors ↑` (`−`, directly editable `<input id="ancDepthVal">` accepting `1–45` or `All`, `+`, `All`) and `Descendants ↓` (`−`, editable `<input id="descDepthVal">` `0–4`, `+`). Switching to a different search result (`openPersonInTree`) automatically resets generation depths back to defaults (`6↑ · 2↓`).
       - **3-icon vertical camera card (`.gmap-icon-stack`)**: `38×38px` geometric icon buttons for **Center on focal scholar** (`#centerFocalBtn`, crosshair icon), **Fit entire tree** (`#fitTreeBtn`, frame icon), and **Switch vertical / horizontal orientation** (`#toggleLayoutDirBtn`, hierarchy icon rotating `-90deg` in horizontal mode).
  - Bottom-left legend: `Statistics` (blue `#3b5bdb`), `Biostatistics` (emerald `#0ca678`), `Probability` (violet `#7950f2`), `Related Quantitative` (amber `#d97706`), `Historical / Math Ancestor` (slate `#64748b`).
  - Right-hand Dossier panel (`#dossierPanel`):
    - Header: Subfield pill, classification provenance, **`64×64px` circular portrait avatar** (`renderAvatarHtml` in `static/app.js` — renders `p.photo_url` with `referrerpolicy="no-referrer"` and automatic `onerror="this.remove()"` fallback to a subfield-tinted 2-letter initials badge via `getInitials`, e.g., `"Lin Zheng"` -> `LZ`; `40×40px` on Search cards and `30×30px` on compact advisor/student/sibling rows), scholar name (prefixed with gold `★` if `famous_self`), PhD school/year, affiliation, optional `★ World-Famous Scientist` badge, and clean read-only actions (`Center Tree on ...` when viewing a non-focal node + `Compare Kinship ↔`; **no** `Crawl Web for Students` button).
    - KPIs (`PhD Advisors`, clickable `PhD Students` (`#dossierStuKpi`), clickable `Total Descendants` (`#dossierDescKpi`) jumping to the `Descendant Of` search filter), `Doctoral Dissertation`, **`Verified Source`** (plain title with **no** parenthetical provenance list; renders verified external pills for `Personal Website ↗`, `Wikipedia ↗`, `Google Scholar ↗`, `LinkedIn ↗`, `ORCID ↗`, `Wikidata ↗`, `MGP ↗`, and `AcademicTree ↗`), `Doctoral Advisors`, **`★ World-Famous Scientist Ancestors`** (placed directly **below** `Doctoral Advisors`, with `Trace Path on Canvas`, `Lineage Steps ▾`, and `View Tree →`), `Direct PhD Students`, and `Academic Siblings`.
- **View 3 (`Common Ancestor`)**: Side-by-side person pickers, quick-example pairs (`Bradley Efron ↔ Trevor Hastie`, `Nan Laird ↔ Xihong Lin`, `David Blackwell ↔ Peter Bickel`, `Karl Pearson ↔ Ronald Fisher`), relationship banner, and interactive kinship DAG canvas.
- **View 4 (`Analytics` — `Data Sources, Methodology & Analytics`)**:
  - **5 KPI Cards** (`Core Stat · Biostat · Prob PhDs`, `Broad Quantitative PhDs`, `Total Connected Scholars`, `Verified PhD Advisor Edges` with concise description `"Verified doctoral advisor–student mentorship links"`, `PhD-Granting Universities` — **no** `AcademicTree / ProQuest Enriched` card).
  - **PhDs by Graduation Year (`1920–2026`, 1-Year Resolution)**: Stacked D3 bar chart where every single bar represents **1 graduation year**, with concise subtitle `Annual doctoral graduates across core statistical sciences (1920–2026).` (**no** `"Hover any year bar for exact counts."` sentence), top-right color legend (`Statistics`, `Biostatistics`, `Probability`), 5/10-year tick labels, and hover breakdown tooltips.
  - **Top PhD-Granting Universities** leaderboard + **Dissertation Topic Landscape** (`#topicMapCard` at the bottom of the Analytics tab: clean header with title `Dissertation Topic Landscape` and 2-word spatially-contiguous 2D-manifold cluster legend pills `#topicClusterLegend` (`ML & Causal`, `Time & Finance`, `Clinical Biostatistics`, `Bayesian & Stochastic`, `High-Dimensional Stats`), no subtitle or search bar; interactive D3 v7 pan/zoom canvas `#topicMapSvg` with wheel-scroll page lock (`wheel` `preventDefault`, `overscroll-behavior: contain`) and floating in-canvas `Reset` / `+` / `−` dock, subtle dot-grid backdrop, soft Gaussian cluster auras (`#topicAuraBlur`), ~282 deduplicated 100% English flat glassmorphic translucent nodes (`fill-opacity: 0.34`, `1.05/k` hairline rings, luminous hover glow, `4.2px–26px` radius) at raw cosine t-SNE coordinates, strongly volume-scaled labels (`8.2px–22.5px`) with zoom-compensated font/halo counter-scaling (`fontSize / k^0.78`, `strokeWidth / k`), rich hover tooltips, and a click-to-explore `#topicDetailDrawer` displaying representative dissertations whose cards open the scholar's Family Tree). **No** `Global Directory of 207 Institutions` card and **no** `Evaluation of Online Academic Genealogy Resources` prose box.

### 4.2 Card Border, Subfield Stripe & Gold Star (`★`) Semantics
- **Card Outer Border (`.node-rect`)**:
  - **Blue (`2.8px` `var(--accent)`)**: Focal scholar (`role === "focal"` / `"mrca"`) or currently selected/hovered node.
  - **Grey (`1.4px` `var(--node-card-stroke)`)**: All other ancestor and descendant nodes (**never** apply gold box borders to famous nodes).
- **Left Vertical Color Stripe (`5.5px` wide)**: Encodes `primary_field` (`Blue` = Statistics, `Green` = Biostatistics, `Purple` = Probability, `Amber` = Related Quantitative, `Grey` = Mathematics & Other).
- **Gold Star (`★`, `#f59f00` / `var(--gold-main)`)**: Rendered cleanly to the left of the scholar's name wherever `p.famous_self` is truthy:
  1. Search & Directory cards (`.scholar-name`)
  2. Family Tree Visualizer SVG node cards (`★ ` prefix on line 1 of card text)
  3. Right-hand Dossier title (`.dossier-title`) and compact advisor/student/sibling rows.

### 4.3 Custom D3 v7 Layered DAG Layout & Dark-Mode Zoom Invariant (`static/app.js`)
- **`computeLayeredDagLayout(nodes, links, isHorizontal)`**:
  - Card dimensions: `CARD_W = 204`, `CARD_H = 68`, `GAP_MAIN = 68` (`86` horizontal), `GAP_CROSS = 22` (`18` horizontal).
  - Groups nodes by integer `gen_offset` and runs multi-pass barycentric sweeps (downward parent-median then upward child-median) to minimize edge crossings without external `dagre` dependencies.
  - Renders smooth cubic Bézier `<path class="tree-link">` curves between card edges.
- **Dark Mode Pan/Zoom Preservation**:
  - `setTheme(theme)` toggles `document.documentElement.setAttribute("data-theme", theme)` so SVG cards and strokes recolor instantaneously via CSS custom properties **without tearing down `#treeSvg`**.
  - Inside `renderGenealogyGraph(svgSelector, graphData, recenterOnFocal)`, always capture `prevTransform = d3.zoomTransform(svgEl)` before clearing `<svg>` and reapply it whenever `recenterOnFocal === false`.

### 4.4 Mobile & Tablet Responsiveness (`static/styles.css`)
- **All Viewports (Desktop, Tablet & Mobile)**:
  - `#inlineFiltersPanel.inline-filters-panel` unfolds inline directly below `.search-hero-bar` and above `#featuredStrip` (`Notable Trees`) when `#openFiltersHeroBtn` (`Filters & Universities ▾`) is toggled, switching `.inline-filters-grid` from 3 columns (`minmax(0, 1.35fr) 150px minmax(0, 1.35fr)`) on desktop to 1 column at `<= 768px` (with `.inline-year-col .year-range-row { max-width: 220px; }`).
- **`<= 1024px` (Tablets & Phones)**:
  - Header switches to `height: auto`, wrapping tabs into a horizontally scrollable second row (`overflow-x: auto; scrollbar-width: none`).
- **`<= 768px` (Smartphones)**:
  - `16px` font-size on `<input>`/`<select>` elements prevents iOS Safari auto-zoom on focus.
  - `.tree-workspace` stacks vertically with `height: calc(100dvh - 96px)`.
  - `#treeSvg` sets `touch-action: none` for smooth single-finger pan and two-finger pinch-zoom (`d3.zoom`).
  - `#dossierPanel` transforms into a **3-state collapsible bottom sheet**:
    - Default **Split (`42dvh`)**: Shows both tree canvas above and scrollable dossier below.
    - **Expanded (`82dvh`, `.mobile-expanded`)**: Full profile reading mode.
    - **Minimized (`44px`, `.mobile-minimized`)**: Collapses to the grab-bar header (`#mobileDossierBar`) for full-screen tree exploration; tapping any tree node automatically un-minimizes the sheet.
