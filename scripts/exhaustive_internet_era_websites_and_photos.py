#!/usr/bin/env python3
"""
Exhaustive Internet-Era Personal Website & Portrait Harvester
(`scripts/exhaustive_internet_era_websites_and_photos.py`).

Maximizes recall of personal websites (`homepage`), portrait photos (`photo_url`), Google Scholar
(`google_scholar_url`), and LinkedIn (`linkedin_url`) for Internet-Era scholars (`year >= 1980`, or
`year IS NULL` connected to an internet-era advisor/advisee), ignoring pre-internet historical rows:

  - Stage 1: Complete DBLP RDF `dblp:webpage` Graph (`https://sparql.dblp.org/sparql`, ~140k triples)
             — exact ORCID/Wikidata matches, affiliation matches, homonym HTML bio verification, and
             extraction of researcher GitHub handles (`github.com/<handle>`).
  - Stage 2: Complete CRAN (`21,500+` R Packages via `Rscript` `tools::CRAN_package_db()`) Author &
             Maintainer Personal URLs and GitHub Handles (`<handle>.github.io`).
  - Stage 3: Targeted GitHub Statistical & `.github.io` Ecosystem Sweep (`jemdoc`, `quarto`,
             `academicpages`, `Biometrika`, `Annals of Statistics`, `JASA`, `Biometrics`,
             `Biostatistics`, and statistical lineage repositories -> custom handles like
             `nozoeli.github.io` for Yuchao Liu, UCSD PhD 2018 under Ery Arias-Castro).
  - Stage 4: Advisor Personal Webpage -> Hyperlinked PhD Student Websites (`5,280+` Core Stat
             advisor homepages + `/students`, `/group`, `/people`, `/alumni` subpages) + Junior
             Faculty / Postdoc / Alumni Directory Sweep (`student_count == 0`, `year >= 1985`).
  - Stage 5: High-Throughput Fastly CDN Keep-Alive `HEAD` + Bio-Verified `GET` Sweep across
             `*.github.io` (`<first><last>`, `<first>-<last>`, `<last><first>`, `<first><last>-<school>`,
             `<first><last>stat`) and `sites.google.com/view/*` (`302` vs `200`) for Internet-Era
             Core Stat PhDs (`year >= 1990`).
  - Stage 6: Final Portrait Photo (`jemdoc` `img-container`, Drupal-safe, SSL-fallback) & Social Link
             Extraction across all newly discovered websites.
"""

import concurrent.futures
import html as html_lib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import unicodedata
from collections import Counter, defaultdict
from urllib.parse import unquote, urljoin, urlparse

import requests
import urllib3
from bs4 import BeautifulSoup

socket.setdefaulttimeout(8.0)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "scripts"))
from build_database import canonicalize_school
from fix_homonym_descendants_and_deep_enrich import (
    BROWSER_HEADERS,
    GSCHOLAR_RE,
    LINKEDIN_RE,
    SKIP_SEARCH_DOMAINS,
    extract_assets_from_profile_html,
    fold_norm_name,
    is_clean_portrait_url,
    run_bounded_batch,
    safe_fetch_html,
)

DB_PATH = os.path.join(BASE_DIR, "data", "stat_genealogy.db")
MASTER_JSON = os.path.join(BASE_DIR, "data", "stat_departments_master.json")
CACHE_DIR = os.path.join(BASE_DIR, "data_raw")
os.makedirs(CACHE_DIR, exist_ok=True)

DBLP_CACHE_PATH = os.path.join(CACHE_DIR, "dblp_webpages_cache.json")
CRAN_CACHE_PATH = os.path.join(CACHE_DIR, "cran_packages_urls_cache.json")
GH_HANDLES_CACHE_PATH = os.path.join(CACHE_DIR, "github_stat_handles_cache.json")

ORCID_RE = re.compile(r"\b(\d{4}-\d{4}-\d{4}-\d{3}[\dX])\b", re.I)
QID_RE = re.compile(r"\b(Q\d+)\b")
GH_USER_RE = re.compile(r"https?://(?:www\.)?github\.com/([A-Za-z0-9](?:[A-Za-z0-9\-]{0,37}[A-Za-z0-9])?)(?:[/?#]|$)", re.I)
GH_IO_RE = re.compile(r"https?://([A-Za-z0-9][A-Za-z0-9\-]{0,38})\.github\.io(?:[/?#]|$)", re.I)

NON_PERSONAL_GITHUB_LOGINS = {
    "orgs", "topics", "features", "marketplace", "explore", "settings", "login", "join",
    "pricing", "about", "site", "security", "sponsors", "collections", "events", "trending",
    "cran", "bioconductor", "ropensci", "tidyverse", "rstudio", "posit-dev", "r-lib",
    "scikit-learn", "pytorch", "tensorflow", "apache", "google", "microsoft", "facebookresearch",
    "meta-llama", "openai", "stanfordnlp", "berkeley-abc",
}

SCHOOL_SHORT_TAGS = {
    "University of California, San Diego": ("ucsd", "san diego"),
    "University of California, Berkeley": ("berkeley", "ucb"),
    "University of California, Los Angeles": ("ucla", "los angeles"),
    "University of California, Davis": ("ucdavis", "uc davis"),
    "University of California, Santa Barbara": ("ucsb", "santa barbara"),
    "University of California, Irvine": ("uci", "uc irvine"),
    "University of California, Santa Cruz": ("ucsc", "santa cruz"),
    "University of California, San Francisco": ("ucsf", "san francisco"),
    "University of California, Riverside": ("ucr", "uc riverside"),
    "Stanford University": ("stanford",),
    "Harvard University": ("harvard", "hsph"),
    "Massachusetts Institute of Technology": ("mit", "massachusetts institute"),
    "Carnegie Mellon University": ("cmu", "carnegie mellon"),
    "University of Pennsylvania": ("upenn", "wharton", "pennsylvania"),
    "University of Chicago": ("uchicago", "university of chicago"),
    "Columbia University": ("columbia",),
    "Princeton University": ("princeton",),
    "Yale University": ("yale",),
    "Cornell University": ("cornell",),
    "Johns Hopkins University": ("jhu", "johns hopkins"),
    "University of Washington": ("uw", "university of washington"),
    "University of Michigan": ("umich", "michigan"),
    "University of North Carolina at Chapel Hill": ("unc", "chapel hill"),
    "North Carolina State University": ("ncsu", "nc state", "north carolina state"),
    "Duke University": ("duke",),
    "University of Wisconsin-Madison": ("wisc", "wisconsin", "uw madison"),
    "University of Minnesota": ("umn", "minnesota"),
    "Purdue University": ("purdue",),
    "Ohio State University": ("osu", "ohio state"),
    "Pennsylvania State University": ("psu", "penn state", "pennsylvania state"),
    "Texas A&M University": ("tamu", "texas a m", "texas a&m"),
    "University of Illinois Urbana-Champaign": ("uiuc", "urbana", "illinois"),
    "Iowa State University": ("iastate", "iowa state"),
    "University of Iowa": ("uiowa", "university of iowa"),
    "University of Florida": ("ufl", "university of florida"),
    "Northwestern University": ("northwestern",),
    "New York University": ("nyu", "new york university"),
    "Rutgers University": ("rutgers",),
    "Rice University": ("rice",),
    "Brown University": ("brown",),
    "Emory University": ("emory",),
    "University of Pittsburgh": ("pitt", "pittsburgh"),
    "University of Texas at Austin": ("utexas", "ut austin", "texas at austin"),
    "Georgia Institute of Technology": ("gatech", "georgia tech", "georgia institute"),
    "Virginia Tech": ("vt", "virginia tech", "virginia polytechnic"),
    "University of Connecticut": ("uconn", "connecticut"),
    "Boston University": ("bu", "boston university"),
    "University of Southern California": ("usc", "southern california"),
    "California Institute of Technology": ("caltech", "california institute"),
    "ETH Zürich": ("ethz", "eth zurich"),
    "EPFL": ("epfl", "lausanne"),
    "University of Oxford": ("oxford",),
    "University of Cambridge": ("cambridge",),
    "Imperial College London": ("imperial",),
    "University College London": ("ucl", "university college london"),
    "London School of Economics": ("lse", "london school of economics"),
    "University of Warwick": ("warwick",),
    "University of Toronto": ("utoronto", "toronto"),
    "University of British Columbia": ("ubc", "british columbia"),
    "University of Waterloo": ("uwaterloo", "waterloo"),
    "McGill University": ("mcgill",),
    "National University of Singapore": ("nus", "national university of singapore"),
    "University of Hong Kong": ("hku", "university of hong kong"),
    "Chinese University of Hong Kong": ("cuhk", "chinese university of hong kong"),
    "Hong Kong University of Science and Technology": ("hkust",),
    "Peking University": ("pku", "peking university"),
    "Tsinghua University": ("tsinghua", "thu"),
}

ACADEMIC_QUANT_MARKERS = (
    "ph d", "phd", "d phil", "doctoral", "dissertation", "thesis",
    "professor", "postdoc", "postdoctoral", "faculty", "lecturer", "researcher",
    "statistics", "biostatistics", "statistical", "probability", "econometrics",
    "data scientist", "data science", "machine learning", "quantitative", "biometrics",
    "mathematics", "operations research", "bioinformatics",
)

STOPWORDS_THESIS = {
    "analysis", "models", "modeling", "methods", "statistical", "statistics", "inference",
    "estimation", "applications", "theory", "problems", "data", "using", "based", "approach",
    "approaches", "study", "studies", "some", "topics", "high", "dimensional", "generalized",
    "linear", "regression", "random", "processes", "learning", "stochastic", "bayesian",
}

_FAST_TLS = threading.local()


def get_fast_session() -> requests.Session:
    s = getattr(_FAST_TLS, "sess", None)
    if s is None:
        s = requests.Session()
        ad = requests.adapters.HTTPAdapter(pool_connections=64, pool_maxsize=64, max_retries=0)
        s.mount("http://", ad)
        s.mount("https://", ad)
        _FAST_TLS.sess = s
    return s


def school_match_phrases(school_name: str) -> set:
    if not school_name:
        return set()
    phrases = set()
    if school_name in SCHOOL_SHORT_TAGS:
        phrases.update(SCHOOL_SHORT_TAGS[school_name])
    ns = fold_norm_name(school_name)
    if ns:
        phrases.add(ns)
    toks = [w for w in ns.split() if w not in ("university", "of", "the", "at", "institute", "technology", "college", "state", "and", "in")]
    if len(toks) >= 2:
        phrases.add(" ".join(toks[:2]))
    elif len(toks) == 1 and len(toks[0]) >= 5:
        phrases.add(toks[0])
    return {p for p in phrases if len(p) >= 3}


def verify_page_matches_scholar(html: str, final_url: str, p: dict, adv_phrases: set, unique_core: bool, fetch_subpage: bool = True):
    """
    Strictly verifies that `html` at `final_url` belongs to scholar `p` (preventing homonym collisions)
    and returns `(True, assets_dict)` or `(False, None)`.
    """
    if not html or len(html) < 120:
        return False, None
    low_raw = html[:15000].lower()
    if "there isn't a github pages site here" in low_raw or "404 not found" in low_raw[:600]:
        return False, None

    soup = BeautifulSoup(html, "lxml")
    title_str = soup.title.get_text(" ", strip=True) if soup.title else ""
    heads_str = " ".join(h.get_text(" ", strip=True) for h in soup.find_all(["h1", "h2", "h3"])[:6])
    vis_text = soup.get_text(" ", strip=True)

    norm_header = fold_norm_name(title_str + " " + heads_str + " " + vis_text[:900])
    norm_full = fold_norm_name(vis_text)

    toks = p["norm"].split()
    if len(toks) < 2:
        return False, None
    first_t, last_t = toks[0], toks[-1]

    header_words = set(norm_header.split())
    if last_t not in header_words:
        return False, None
    if first_t not in header_words and not any(w.startswith(first_t) or first_t.startswith(w) for w in header_words if len(w) >= 3):
        return False, None

    padded_full = f" {norm_full} "

    # Active-Student Homonym Guard: if scholar graduated >=3 yrs ago (`year <= 2023`), reject pages
    # introducing the owner as a current PhD/MS/undergrad student at a DIFFERENT university!
    intro_norm = norm_full[:700]
    if p.get("year") is not None and p["year"] <= 2023:
        is_current_student_bio = any(
            st_m in intro_norm
            for st_m in (
                "ph d candidate", "phd candidate", "i am a ph d student", "i am a phd student",
                "year ph d student", "year phd student", "master s student", "ms student",
                "undergraduate student",
            )
        )
        if is_current_student_bio:
            phd_sch_phr = school_match_phrases(p.get("school") or "")
            if not any(f" {sp} " in padded_full or (len(sp) >= 5 and sp in norm_full) for sp in phd_sch_phr):
                return False, None

    def check_anchors(txt_padded: str, txt_norm: str) -> bool:
        # Anchor A1: PhD school, known affiliation, or advising university keyword
        for sp in p["school_phrases"]:
            if f" {sp} " in txt_padded or (len(sp) >= 5 and sp in txt_norm):
                return True
        # Anchor A2: Doctoral advisor surname / full name or doctoral advisee full name
        for ap in adv_phrases:
            if f" {ap} " in txt_padded:
                return True
        # Anchor B: >=2 distinctive (>=6-char) dissertation words
        if len(p["thesis_words"]) >= 2:
            hits = sum(1 for tw in p["thesis_words"] if f" {tw} " in txt_padded)
            if hits >= 2:
                return True
        return False

    matched = check_anchors(padded_full, norm_full)

    # Many jemdoc / academicpages sites put PhD school on `bio.html` or `about.html`!
    if not matched and fetch_subpage:
        for a in soup.find_all("a", href=True)[:25]:
            atxt = a.get_text(" ", strip=True).lower()
            ahref = a["href"].strip().lower()
            if any(k in atxt or k in ahref for k in ("bio", "about", "cv", "vita", "background")):
                if ahref.endswith(".pdf") or ahref.startswith("mailto:"):
                    continue
                sub_u = urljoin(final_url, a["href"].strip())
                if urlparse(sub_u).netloc == urlparse(final_url).netloc and sub_u != final_url:
                    sub_html, _ = safe_fetch_html(sub_u, timeout=3.5, max_bytes=120_000)
                    if sub_html:
                        sub_norm = fold_norm_name(BeautifulSoup(sub_html, "lxml").get_text(" ", strip=True))
                        comb_norm = norm_full + " " + sub_norm
                        if check_anchors(f" {comb_norm} ", comb_norm):
                            matched = True
                            break

    if not matched:
        return False, None

    assets = extract_assets_from_profile_html(html, final_url, p["name"])
    return True, assets


def fetch_dblp_all_webpages() -> list:
    if os.path.exists(DBLP_CACHE_PATH) and os.path.getsize(DBLP_CACHE_PATH) > 100_000:
        with open(DBLP_CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        print(f"[Stage 1] Loaded {len(data):,} cached DBLP researcher webpage records.")
        return data

    print("\n=== STAGE 1: Downloading Complete DBLP Researcher Webpage RDF Graph (`sparql.dblp.org`) ===")
    all_rows = []
    sess = requests.Session()
    headers = {
        "Accept": "application/sparql-results+json",
        "User-Agent": "StatLinkAcademicGenealogy/2.0 (https://github.com/YuZhao-UCSD/StatLineage)",
    }
    page_size = 20000
    for offset in range(0, 160000, page_size):
        q = f"""
        PREFIX dblp: <https://dblp.org/rdf/schema#>
        SELECT ?p ?name ?web ?aff ?orcid ?wd WHERE {{
          ?p dblp:webpage ?web ;
             dblp:primaryCreatorName ?name .
          OPTIONAL {{ ?p dblp:primaryAffiliation ?aff }}
          OPTIONAL {{ ?p dblp:orcid ?orcid }}
          OPTIONAL {{ ?p dblp:wikidata ?wd }}
        }}
        LIMIT {page_size} OFFSET {offset}
        """
        ok = False
        for attempt in range(3):
            try:
                r = sess.get("https://sparql.dblp.org/sparql", params={"query": q}, headers=headers, timeout=30)
                if r.status_code == 200:
                    bindings = r.json().get("results", {}).get("bindings", [])
                    for b in bindings:
                        raw_nm = re.sub(r"\s+\d{4}$", "", b.get("name", {}).get("value", "")).strip()
                        web = b.get("web", {}).get("value", "").strip()
                        aff = b.get("aff", {}).get("value", "").strip()
                        orc_raw = b.get("orcid", {}).get("value", "")
                        wd_raw = b.get("wikidata", {}).get("value", "") or b.get("wd", {}).get("value", "")
                        m_o = ORCID_RE.search(orc_raw)
                        m_q = QID_RE.search(wd_raw)
                        all_rows.append({
                            "name": raw_nm,
                            "web": web,
                            "aff": aff,
                            "orcid": m_o.group(1).upper() if m_o else "",
                            "qid": m_q.group(1) if m_q else "",
                        })
                    print(f"  DBLP slice offset={offset:,}: +{len(bindings):,} rows (total={len(all_rows):,})")
                    ok = True
                    if len(bindings) < page_size:
                        offset = 999999
                    break
            except Exception as e:
                print(f"  DBLP slice offset={offset} retry {attempt+1}: {e}")
                time.sleep(1.5)
        if not ok or offset >= 999999:
            break

    if all_rows:
        with open(DBLP_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(all_rows, f)
    return all_rows


def fetch_cran_author_urls() -> list:
    if os.path.exists(CRAN_CACHE_PATH) and os.path.getsize(CRAN_CACHE_PATH) > 10_000:
        with open(CRAN_CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        print(f"[Stage 2] Loaded {len(data):,} cached CRAN package metadata rows.")
        return data

    print("\n=== STAGE 2: Extracting Complete CRAN (`21,500+` R Packages) Author URLs & GitHub Handles ===")
    rscript = shutil.which("Rscript") or r"C:\Program Files\R\R-4.1.2\bin\x64\Rscript.EXE"
    tsv_tmp = os.path.join(CACHE_DIR, "cran_pkgs_tmp.tsv")
    if os.path.exists(rscript):
        r_code = (
            "options(repos=c(CRAN='https://cloud.r-project.org')); "
            "db <- tools::CRAN_package_db(); "
            "clean <- function(x) gsub('[\\r\\n\\t]+', ' ', ifelse(is.na(x), '', x)); "
            "df <- data.frame("
            "pkg=clean(db$Package), "
            "auth=clean(db$Author), "
            "maint=clean(db$Maintainer), "
            "url=clean(db$URL), "
            "bug=clean(db$BugReports), "
            "stringsAsFactors=FALSE); "
            f"write.table(df, file={json.dumps(tsv_tmp)}, sep='\\t', row.names=FALSE, quote=FALSE, fileEncoding='UTF-8')"
        )
        try:
            subprocess.run([rscript, "-e", r_code], timeout=60, check=True)
        except Exception as e:
            print(f"  Rscript CRAN fetch warning: {e}")

    rows = []
    if os.path.exists(tsv_tmp):
        with open(tsv_tmp, "r", encoding="utf-8", errors="replace") as f:
            header = f.readline()
            for line in f:
                parts = line.rstrip("\r\n").split("\t")
                if len(parts) >= 5:
                    pkg, auth, maint, url, bug = parts[:5]
                    if url or bug or "orcid" in auth.lower():
                        rows.append({"pkg": pkg, "auth": auth, "maint": maint, "url": url, "bug": bug})
        try:
            os.remove(tsv_tmp)
        except OSError:
            pass
        with open(CRAN_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(rows, f)
    print(f"  Extracted {len(rows):,} CRAN R packages with URLs / GitHub links / ORCIDs.")
    return rows


def harvest_github_statistical_handles() -> set:
    if os.path.exists(GH_HANDLES_CACHE_PATH) and os.path.getsize(GH_HANDLES_CACHE_PATH) > 1000:
        with open(GH_HANDLES_CACHE_PATH, "r", encoding="utf-8") as f:
            cached = set(json.load(f))
    else:
        cached = set()

    queries = [
        '"github.io" "Statistics" PhD in:readme,description',
        '"github.io" "Biostatistics" PhD in:readme,description',
        '"github.io" "Statistical Science" in:readme,description',
        '"github.io" "Probability" PhD in:readme,description',
        '"github.io" "Econometrics" PhD in:readme,description',
        '"jemdoc" "Statistics" PhD in:readme,description',
        '"jemdoc" "Biostatistics" in:readme,description',
        'github.io in:name "Department of Statistics"',
        'github.io in:name "Biostatistics"',
        'github.io in:name "PhD student" statistics',
        'github.io in:name "Assistant Professor" statistics',
        '"Biometrika" language:R',
        '"Annals of Statistics" language:R',
        '"Journal of the American Statistical Association" language:R',
        '"Biometrics" biostatistics language:R',
        '"Annals of Applied Statistics" language:R',
        '"Statistica Sinica" language:R',
        '"Arias-Castro"',
        '"Wasserman" statistics language:R',
        '"Tibshirani" language:R',
        '"Buhlmann" statistics language:R',
        '"Xihong Lin" language:R',
        '"Jianqing Fan" language:R',
        '"Chernozhukov" language:R',
    ]

    if len(cached) >= 1200:
        print(f"[Stage 3] Loaded {len(cached):,} cached GitHub statistical handles.")
        return cached

    print(f"\n=== STAGE 3: Harvesting Custom GitHub Handles across {len(queries)} Statistical Queries ===")
    sess = requests.Session()
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "StatLinkAcademicGenealogy/2.0"}

    for idx, q in enumerate(queries, 1):
        try:
            r = sess.get(
                "https://api.github.com/search/repositories",
                params={"q": q, "per_page": 100, "sort": "updated"},
                headers=headers,
                timeout=10,
            )
            if r.status_code == 200:
                items = r.json().get("items") or []
                for it in items:
                    login = ((it.get("owner") or {}).get("login") or "").strip()
                    if login and login.lower() not in NON_PERSONAL_GITHUB_LOGINS:
                        cached.add(login.lower())
                    hp = (it.get("homepage") or "").strip()
                    m_io = GH_IO_RE.search(hp)
                    if m_io:
                        cached.add(m_io.group(1).lower())
                print(f"  [{idx:2d}/{len(queries)}] GitHub query `{q[:48]}` -> +{len(items)} repos (unique handles={len(cached):,})")
            elif r.status_code in (403, 429):
                print("  GitHub search rate-limit reached; pausing 12s...")
                time.sleep(12.0)
        except Exception:
            pass
        time.sleep(6.2)

    with open(GH_HANDLES_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(cached), f)
    return cached


def main():
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row

    print("Loading Internet-Era scholar graph (`year >= 1980` or modern advisor/advisee)...")
    all_people_rows = conn.execute("""
        SELECT id, name, norm_name, school, current_affiliation, year, thesis,
               is_core_stat, is_broad_stat, student_count,
               COALESCE(orcid, '') AS orcid,
               COALESCE(wikidata_qid, '') AS qid,
               COALESCE(homepage, '') AS homepage,
               COALESCE(photo_url, '') AS photo_url,
               COALESCE(google_scholar_url, '') AS gscholar,
               COALESCE(linkedin_url, '') AS linkedin
        FROM people
    """).fetchall()

    total_name_counts = Counter()
    core_internet_name_counts = Counter()
    year_by_id = {}
    name_by_id = {}
    for r in all_people_rows:
        uid = r["id"]
        nn = r["norm_name"] or fold_norm_name(r["name"])
        total_name_counts[nn] += 1
        year_by_id[uid] = r["year"]
        name_by_id[uid] = r["name"]

    edge_rows = conn.execute("SELECT advisor_id, student_id FROM edges").fetchall()
    advisors_of = defaultdict(list)
    students_of = defaultdict(list)
    for aid, sid in edge_rows:
        advisors_of[sid].append(aid)
        students_of[aid].append(sid)

    def is_internet_era(r) -> bool:
        yr = r["year"]
        if yr is not None:
            return yr >= 1980
        uid = r["id"]
        for aid in advisors_of.get(uid, ()):
            ay = year_by_id.get(aid)
            if ay is not None and ay >= 1965:
                return True
        for sid in students_of.get(uid, ()):
            sy = year_by_id.get(sid)
            if sy is not None and sy >= 1990:
                return True
        return False

    adv_schools_map = defaultdict(set)
    for pid, sch in conn.execute("SELECT person_id, school FROM advisor_schools"):
        if sch:
            adv_schools_map[pid].add(sch)

    people = {}
    orcid_to_uid = {}
    qid_to_uid = {}
    norm_to_uids = defaultdict(list)
    adv_phrases_by_uid = {}

    for r in all_people_rows:
        if not is_internet_era(r):
            continue
        uid = r["id"]
        nn = r["norm_name"] or fold_norm_name(r["name"])
        if len(nn.split()) < 2:
            continue
        if r["is_core_stat"] == 1:
            core_internet_name_counts[nn] += 1

        th_toks = [
            w for w in re.findall(r"[a-z]{6,}", fold_norm_name(r["thesis"] or ""))
            if w not in STOPWORDS_THESIS
        ][:8]
        sch_phr = school_match_phrases(r["school"] or "") | school_match_phrases(r["current_affiliation"] or "")
        for asch in adv_schools_map.get(uid, ()):
            sch_phr |= school_match_phrases(asch)

        rec = {
            "id": uid,
            "name": r["name"],
            "norm": nn,
            "school": r["school"] or "",
            "affil": r["current_affiliation"] or "",
            "year": r["year"],
            "is_core": r["is_core_stat"],
            "is_broad": r["is_broad_stat"],
            "stu_cnt": r["student_count"] or 0,
            "orcid": r["orcid"],
            "qid": r["qid"],
            "homepage": r["homepage"],
            "photo_url": r["photo_url"],
            "gscholar": r["gscholar"],
            "linkedin": r["linkedin"],
            "school_phrases": sch_phr,
            "thesis_words": set(th_toks),
            "dirty": False,
        }
        people[uid] = rec
        norm_to_uids[nn].append(uid)
        if rec["orcid"]:
            orcid_to_uid[rec["orcid"].upper()] = uid
        if rec["qid"]:
            qid_to_uid[rec["qid"]] = uid

        aph = set()
        for aid in advisors_of.get(uid, ()):
            anm = fold_norm_name(name_by_id.get(aid, ""))
            atoks = anm.split()
            if atoks:
                if len(atoks[-1]) >= 5:
                    aph.add(atoks[-1])
                if len(atoks) >= 2:
                    aph.add(f"{atoks[0]} {atoks[-1]}")
        for sid in students_of.get(uid, ())[:25]:
            snm = fold_norm_name(name_by_id.get(sid, ""))
            stoks = snm.split()
            if len(stoks) >= 2:
                aph.add(f"{stoks[0]} {stoks[-1]}")
        adv_phrases_by_uid[uid] = aph

    print(f"Indexed {len(people):,} Internet-Era scholars ({sum(1 for p in people.values() if p['is_core']==1):,} Core Stat).")

    def is_unique_core(p: dict) -> bool:
        nn = p["norm"]
        return p["is_core"] == 1 and core_internet_name_counts[nn] == 1 and total_name_counts[nn] <= 2

    def apply_verified_assets(uid: int, web_url: str, assets: dict) -> bool:
        p = people[uid]
        changed = False
        if web_url and not p["homepage"]:
            p["homepage"] = assets.get("personal_url") or web_url
            changed = True
        if assets.get("photo_url") and not p["photo_url"]:
            p["photo_url"] = assets["photo_url"]
            changed = True
        if assets.get("gscholar") and not p["gscholar"]:
            p["gscholar"] = assets["gscholar"]
            changed = True
        if assets.get("linkedin") and not p["linkedin"]:
            p["linkedin"] = assets["linkedin"]
            changed = True
        if changed:
            p["dirty"] = True
        return changed

    def flush_commits(stage_label: str):
        dirty = [
            (p["homepage"], p["photo_url"], p["gscholar"], p["linkedin"], p["orcid"], p["qid"], p["id"])
            for p in people.values()
            if p["dirty"]
        ]
        if dirty:
            conn.executemany(
                """
                UPDATE people
                SET homepage = ?,
                    photo_url = ?,
                    google_scholar_url = ?,
                    linkedin_url = ?,
                    orcid = CASE WHEN (orcid = '' OR orcid IS NULL) THEN ? ELSE orcid END,
                    wikidata_qid = CASE WHEN (wikidata_qid = '' OR wikidata_qid IS NULL) THEN ? ELSE wikidata_qid END
                WHERE id = ?
                """,
                dirty,
            )
            conn.commit()
            for p in people.values():
                p["dirty"] = False
        print(f"  -> Committed {len(dirty):,} scholar updates ({stage_label}).")

    # =========================================================================
    # STAGE 1: DBLP Complete Researcher Webpage Graph
    # =========================================================================
    dblp_rows = fetch_dblp_all_webpages()
    gh_handles = set()
    dblp_verify_tasks = []
    dblp_direct = 0

    for row in dblp_rows:
        web = row["web"]
        if not web:
            continue
        m_gh = GH_USER_RE.match(web)
        if m_gh:
            lg = m_gh.group(1).lower()
            if lg not in NON_PERSONAL_GITHUB_LOGINS:
                gh_handles.add(lg)
        m_io = GH_IO_RE.match(web)
        if m_io:
            gh_handles.add(m_io.group(1).lower())

        uid = None
        if row["orcid"] and row["orcid"] in orcid_to_uid:
            uid = orcid_to_uid[row["orcid"]]
        elif row["qid"] and row["qid"] in qid_to_uid:
            uid = qid_to_uid[row["qid"]]

        nn = fold_norm_name(row["name"])
        cands = norm_to_uids.get(nn, [])

        if uid is None and cands and row["aff"]:
            aff_canon = canonicalize_school(row["aff"])
            aff_norm = fold_norm_name(aff_canon or row["aff"])
            for cid in cands:
                cp = people[cid]
                if any(sp in aff_norm for sp in cp["school_phrases"] if len(sp) >= 4):
                    uid = cid
                    break

        m_gs = GSCHOLAR_RE.search(web)
        m_li = LINKEDIN_RE.search(web)
        is_personal_cand = (
            web.startswith(("http://", "https://"))
            and not m_gs
            and not m_li
            and not any(sd in web.lower() for sd in SKIP_SEARCH_DOMAINS)
            and "github.com/" not in web.lower()
            and "dl.acm.org" not in web.lower()
        )

        if uid is not None and uid in people:
            p = people[uid]
            if m_gs and not p["gscholar"]:
                p["gscholar"] = f"https://scholar.google.com/citations?user={m_gs.group(1)}"
                p["dirty"] = True
                dblp_direct += 1
            elif m_li and not p["linkedin"]:
                p["linkedin"] = f"https://www.linkedin.com/in/{m_li.group(1).strip('/')}"
                p["dirty"] = True
                dblp_direct += 1
            elif is_personal_cand and not p["homepage"]:
                p["homepage"] = web
                p["dirty"] = True
                dblp_direct += 1
            if row["orcid"] and not p["orcid"]:
                p["orcid"] = row["orcid"]
                p["dirty"] = True
            if row["qid"] and not p["qid"]:
                p["qid"] = row["qid"]
                p["dirty"] = True
        elif is_personal_cand and cands:
            core_cands = [cid for cid in cands if (people[cid]["is_core"] == 1 or people[cid]["is_broad"] == 1) and not people[cid]["homepage"]]
            if 1 <= len(core_cands) <= 6:
                dblp_verify_tasks.append((web, core_cands))

    # Deduplicate DBLP live bio verification URLs
    seen_v_urls = set()
    dedup_dblp_tasks = []
    for web, core_cands in dblp_verify_tasks:
        if web not in seen_v_urls:
            seen_v_urls.add(web)
            dedup_dblp_tasks.append((web, core_cands))

    print(f"  DBLP direct ORCID/QID/Affil matches: {dblp_direct:,} | Queued {len(dedup_dblp_tasks):,} candidate URLs for live bio verification...")

    def verify_url_against_cands(task):
        url, cand_ids = task
        html, final_u = safe_fetch_html(url, timeout=4.2)
        if not html:
            return None
        for cid in cand_ids:
            p = people[cid]
            ok, assets = verify_page_matches_scholar(html, final_u, p, adv_phrases_by_uid[cid], is_unique_core(p), fetch_subpage=False)
            if ok:
                return cid, final_u, assets
        return None

    dblp_verified_cnt = 0
    for res in run_bounded_batch(verify_url_against_cands, dedup_dblp_tasks, max_workers=32, batch_timeout=10.0):
        if res:
            cid, final_u, assets = res
            if apply_verified_assets(cid, final_u, assets):
                dblp_verified_cnt += 1
    print(f"  DBLP live bio-verified websites: +{dblp_verified_cnt:,}")
    flush_commits("Stage 1: DBLP")

    # =========================================================================
    # STAGE 2: CRAN R-Package Author/Maintainer URLs & GitHub Handles
    # =========================================================================
    cran_rows = fetch_cran_author_urls()
    cran_cand_tasks = []
    for cr in cran_rows:
        combined_urls = cr.get("url", "") + " " + cr.get("bug", "")
        for m_gh in GH_USER_RE.finditer(combined_urls):
            lg = m_gh.group(1).lower()
            if lg not in NON_PERSONAL_GITHUB_LOGINS:
                gh_handles.add(lg)
        for m_io in GH_IO_RE.finditer(combined_urls):
            gh_handles.add(m_io.group(1).lower())

        # Maintainer name before `<email>`
        maint_raw = re.split(r"<", cr.get("maint", ""))[0].strip().strip("'\"")
        mn = fold_norm_name(maint_raw)
        cands = [cid for cid in norm_to_uids.get(mn, []) if people[cid]["is_core"] == 1 and not people[cid]["homepage"]]
        if 1 <= len(cands) <= 5:
            for u_tok in re.findall(r"https?://[^\s,;<>\"')]+", cr.get("url", "")):
                ul = u_tok.lower()
                if not any(sd in ul for sd in SKIP_SEARCH_DOMAINS) and "github.com/" not in ul and "r-project.org" not in ul and "cran." not in ul and "doi.org" not in ul:
                    if u_tok not in seen_v_urls:
                        seen_v_urls.add(u_tok)
                        cran_cand_tasks.append((u_tok, cands))

    cran_verified = 0
    for res in run_bounded_batch(verify_url_against_cands, cran_cand_tasks, max_workers=28, batch_timeout=10.0):
        if res:
            cid, final_u, assets = res
            if apply_verified_assets(cid, final_u, assets):
                cran_verified += 1
    print(f"  CRAN maintainer websites verified: +{cran_verified:,} (accumulated GitHub handles: {len(gh_handles):,})")
    flush_commits("Stage 2: CRAN")

    # =========================================================================
    # STAGE 3: Custom GitHub Statistical Handles -> `https://<handle>.github.io/`
    # =========================================================================
    gh_handles |= harvest_github_statistical_handles()
    # Also extract any github.io handles already present in DB to skip redundant checks
    existing_gh_io = set()
    for p in people.values():
        m_e = GH_IO_RE.search(p["homepage"] or "")
        if m_e:
            existing_gh_io.add(m_e.group(1).lower())

    custom_handles_list = sorted(gh_handles - existing_gh_io)
    print(f"\n=== STAGE 3B: Probing {len(custom_handles_list):,} Discovered Custom GitHub Handles (`<handle>.github.io`) on Fastly CDN ===")

    def probe_custom_gh_io(handle: str):
        url = f"https://{handle}.github.io/"
        sess = get_fast_session()
        try:
            rh = sess.head(url, headers=BROWSER_HEADERS, timeout=(2.5, 3.5), allow_redirects=True)
            if rh.status_code != 200:
                return None
        except Exception:
            return None
        html, final_u = safe_fetch_html(url, timeout=4.5)
        if not html:
            return None
        soup = BeautifulSoup(html[:40000], "lxml")
        # Extract candidate person name from <title> or <h1>
        cands_names = []
        if soup.title and soup.title.string:
            t_clean = re.split(r"[|\-–—•:@]", soup.title.get_text(" ", strip=True))[0].strip()
            cands_names.append(fold_norm_name(t_clean))
        for h in soup.find_all(["h1", "h2"])[:3]:
            cands_names.append(fold_norm_name(h.get_text(" ", strip=True)))

        checked_uids = set()
        for cn in cands_names:
            if not cn or len(cn.split()) < 2 or len(cn.split()) > 4:
                continue
            for cid in norm_to_uids.get(cn, ()):
                if cid in checked_uids:
                    continue
                checked_uids.add(cid)
                p = people[cid]
                if p["homepage"] and p["photo_url"]:
                    continue
                ok, assets = verify_page_matches_scholar(html, final_u, p, adv_phrases_by_uid[cid], is_unique_core(p), fetch_subpage=True)
                if ok:
                    return cid, final_u, assets
        return None

    custom_gh_hits = 0
    for res in run_bounded_batch(probe_custom_gh_io, custom_handles_list, max_workers=48, batch_timeout=8.0):
        if res:
            cid, final_u, assets = res
            if apply_verified_assets(cid, final_u, assets):
                custom_gh_hits += 1
    print(f"  Custom `<handle>.github.io` personal websites verified: +{custom_gh_hits:,}")
    flush_commits("Stage 3: Custom GitHub Pages")

    # =========================================================================
    # STAGE 4: Advisor Personal Webpages -> Hyperlinked Advisee Websites!
    # =========================================================================
    advisor_pages = [
        (p["id"], p["homepage"])
        for p in people.values()
        if p["homepage"] and p["is_core"] == 1 and p["stu_cnt"] >= 1
    ]
    print(f"\n=== STAGE 4: Scanning {len(advisor_pages):,} Core Stat Advisor Websites for Hyperlinked PhD Student Personal Sites ===")

    def extract_student_links_from_advisor(item):
        adv_id, adv_hp = item
        stu_ids = students_of.get(adv_id, [])
        if not stu_ids:
            return []
        stu_by_norm = {}
        for sid in stu_ids:
            if sid in people and not people[sid]["homepage"]:
                stu_by_norm[people[sid]["norm"]] = sid
        if not stu_by_norm:
            return []

        html, final_u = safe_fetch_html(adv_hp, timeout=4.2)
        if not html:
            return []
        pages_html = [(html, final_u)]
        soup0 = BeautifulSoup(html, "lxml")
        sub_fetched = 0
        for a in soup0.find_all("a", href=True):
            atxt = a.get_text(" ", strip=True).lower()
            ahref = a["href"].strip().lower()
            if any(k in atxt or k in ahref for k in ("student", "group", "people", "member", "lab", "alumni", "advisee", "team")):
                if ahref.endswith(".pdf") or ahref.startswith("mailto:"):
                    continue
                su = urljoin(final_u, a["href"].strip())
                if urlparse(su).netloc == urlparse(final_u).netloc and su != final_u:
                    sh, sfu = safe_fetch_html(su, timeout=3.8)
                    if sh:
                        pages_html.append((sh, sfu))
                        sub_fetched += 1
                        if sub_fetched >= 2:
                            break

        discovered = []
        adv_domain_path = urlparse(final_u).netloc + urlparse(final_u).path.rstrip("/")
        for h_str, base_u in pages_html:
            sp = BeautifulSoup(h_str, "lxml")
            for a in sp.find_all("a", href=True):
                raw_t = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
                if not (4 <= len(raw_t) <= 45):
                    continue
                nn = fold_norm_name(raw_t)
                if nn in stu_by_norm:
                    href = urljoin(base_u, a["href"].strip())
                    if not href.startswith(("http://", "https://")) or href.lower().endswith(".pdf"):
                        continue
                    if any(sd in href.lower() for sd in SKIP_SEARCH_DOMAINS):
                        continue
                    cand_dp = urlparse(href).netloc + urlparse(href).path.rstrip("/")
                    if cand_dp == adv_domain_path:
                        continue
                    discovered.append((stu_by_norm[nn], href))
        return discovered

    adv_stu_pairs = []
    for pairs in run_bounded_batch(extract_student_links_from_advisor, advisor_pages, max_workers=36, batch_timeout=10.0):
        if pairs:
            adv_stu_pairs.extend(pairs)

    # Fetch each discovered student URL to grab their portrait + Scholar + LinkedIn!
    seen_stu = set()
    uniq_stu_pairs = []
    for sid, s_url in adv_stu_pairs:
        if sid not in seen_stu:
            seen_stu.add(sid)
            uniq_stu_pairs.append((sid, s_url))

    def enrich_adv_linked_student(pair):
        sid, s_url = pair
        html, final_u = safe_fetch_html(s_url, timeout=4.2)
        if not html:
            return sid, s_url, {}
        assets = extract_assets_from_profile_html(html, final_u, people[sid]["name"])
        return sid, final_u or s_url, assets

    adv_stu_committed = 0
    for res in run_bounded_batch(enrich_adv_linked_student, uniq_stu_pairs, max_workers=32, batch_timeout=9.0):
        if res:
            sid, final_u, assets = res
            if apply_verified_assets(sid, final_u, assets):
                adv_stu_committed += 1
    print(f"  Advisor-hyperlinked PhD student personal websites verified: +{adv_stu_committed:,}")
    flush_commits("Stage 4: Advisor Student Links")

    # =========================================================================
    # STAGE 5: Exhaustive Fastly CDN `*.github.io` & `sites.google.com/view/*`
    #          Slug Probing across Internet-Era Core Stat PhDs (`year >= 1990`)
    # =========================================================================
    slug_to_cands = defaultdict(list)
    gsite_to_cands = defaultdict(list)

    for uid, p in people.items():
        if p["homepage"] or p["is_core"] != 1:
            continue
        if p["year"] is not None and p["year"] < 1990:
            continue
        toks = [re.sub(r"[^a-z0-9]", "", t) for t in p["norm"].split()]
        toks = [t for t in toks if t]
        if len(toks) < 2:
            continue
        f_tok, l_tok = toks[0], toks[-1]
        if len(f_tok) < 2 or len(l_tok) < 2:
            continue

        cand_slugs = {
            f"{f_tok}{l_tok}",
            f"{f_tok}-{l_tok}",
            f"{l_tok}{f_tok}",
        }
        # School-tag suffix (like `yuzhao-ucsd.github.io` or `...-stat.github.io`)
        if p["school"] in SCHOOL_SHORT_TAGS:
            stag = SCHOOL_SHORT_TAGS[p["school"]][0].replace(" ", "")
            if stag.isalnum():
                cand_slugs.add(f"{f_tok}{l_tok}-{stag}")
                cand_slugs.add(f"{f_tok}-{l_tok}-{stag}")
        cand_slugs.add(f"{f_tok}{l_tok}-stat")
        cand_slugs.add(f"{f_tok}{l_tok}stat")

        for sl in cand_slugs:
            if 4 <= len(sl) <= 38 and sl not in existing_gh_io:
                if len(slug_to_cands[sl]) < 6:
                    slug_to_cands[sl].append(uid)

        if p["year"] is None or p["year"] >= 2005:
            gs_sl = f"{f_tok}{l_tok}"
            if 5 <= len(gs_sl) <= 36 and len(gsite_to_cands[gs_sl]) < 5:
                gsite_to_cands[gs_sl].append(uid)

    slug_items = list(slug_to_cands.items())
    print(f"\n=== STAGE 5A: Fastly Keep-Alive Probing {len(slug_items):,} Candidate `*.github.io` Slugs ===")

    def probe_slug_item(item):
        sl, cand_ids = item
        url = f"https://{sl}.github.io/"
        sess = get_fast_session()
        try:
            rh = sess.head(url, headers=BROWSER_HEADERS, timeout=(2.2, 3.2), allow_redirects=True)
            if rh.status_code != 200:
                return None
        except Exception:
            return None
        html, final_u = safe_fetch_html(url, timeout=4.2)
        if not html:
            return None
        for cid in cand_ids:
            p = people[cid]
            if p["homepage"]:
                continue
            ok, assets = verify_page_matches_scholar(html, final_u, p, adv_phrases_by_uid[cid], is_unique_core(p), fetch_subpage=True)
            if ok:
                return cid, final_u, assets
        return None

    gh_slug_hits = 0
    BATCH_SLUGS = 15000
    for start in range(0, len(slug_items), BATCH_SLUGS):
        sub = slug_items[start : start + BATCH_SLUGS]
        for res in run_bounded_batch(probe_slug_item, sub, max_workers=64, batch_timeout=7.5):
            if res:
                cid, final_u, assets = res
                if apply_verified_assets(cid, final_u, assets):
                    gh_slug_hits += 1
        flush_commits(f"Stage 5A github.io {min(start + BATCH_SLUGS, len(slug_items)):,}/{len(slug_items):,} (total hits={gh_slug_hits:,})")

    gsite_items = [(sl, [u for u in uids if not people[u]["homepage"]]) for sl, uids in gsite_to_cands.items()]
    gsite_items = [(sl, uids) for sl, uids in gsite_items if uids]
    print(f"\n=== STAGE 5B: Probing {len(gsite_items):,} Candidate `sites.google.com/view/*` Academic Sites ===")

    def probe_gsite_item(item):
        sl, cand_ids = item
        url = f"https://sites.google.com/view/{sl}"
        sess = get_fast_session()
        try:
            # Non-existent Google Sites immediately return 302 to accounts.google.com!
            rh = sess.get(url, headers=BROWSER_HEADERS, timeout=(2.5, 3.8), allow_redirects=False, stream=True)
            if rh.status_code != 200:
                rh.close()
                return None
            chunks = []
            tot = 0
            for ch in rh.iter_content(chunk_size=16384):
                if not ch:
                    break
                chunks.append(ch)
                tot += len(ch)
                if tot >= 180_000:
                    break
            enc = rh.encoding or "utf-8"
            rh.close()
            html = b"".join(chunks).decode(enc, errors="replace")
        except Exception:
            return None

        for cid in cand_ids:
            p = people[cid]
            if p["homepage"]:
                continue
            ok, assets = verify_page_matches_scholar(html, url, p, adv_phrases_by_uid[cid], is_unique_core(p), fetch_subpage=False)
            if ok:
                return cid, url, assets
        return None

    gs_hits = 0
    for start in range(0, len(gsite_items), 15000):
        sub = gsite_items[start : start + 15000]
        for res in run_bounded_batch(probe_gsite_item, sub, max_workers=48, batch_timeout=7.5):
            if res:
                cid, final_u, assets = res
                if apply_verified_assets(cid, final_u, assets):
                    gs_hits += 1
        flush_commits(f"Stage 5B Google Sites {min(start + 15000, len(gsite_items)):,}/{len(gsite_items):,} (total hits={gs_hits:,})")

    # =========================================================================
    # STAGE 6: Portrait Extraction on Newly Added Websites Missing `photo_url`
    # =========================================================================
    need_photos = [
        (p["id"], p["name"], p["homepage"])
        for p in people.values()
        if p["homepage"] and not p["photo_url"] and p["is_core"] == 1
    ]
    print(f"\n=== STAGE 6: Extracting Portraits from {len(need_photos):,} Core Stat Websites Missing Photos ===")

    def fetch_missing_photo(item):
        uid, nm, hp = item
        html, final_u = safe_fetch_html(hp, timeout=4.2)
        if not html:
            return None
        assets = extract_assets_from_profile_html(html, final_u, nm)
        if assets.get("photo_url") or assets.get("gscholar") or assets.get("linkedin"):
            return uid, assets
        return None

    ph_added = 0
    for res in run_bounded_batch(fetch_missing_photo, need_photos, max_workers=40, batch_timeout=8.5):
        if res:
            uid, assets = res
            p = people[uid]
            if assets.get("photo_url") and not p["photo_url"]:
                p["photo_url"] = assets["photo_url"]
                p["dirty"] = True
                ph_added += 1
            if assets.get("gscholar") and not p["gscholar"]:
                p["gscholar"] = assets["gscholar"]
                p["dirty"] = True
            if assets.get("linkedin") and not p["linkedin"]:
                p["linkedin"] = assets["linkedin"]
                p["dirty"] = True
    flush_commits(f"Stage 6: +{ph_added:,} additional portraits")

    print("\n=== FINAL VERIFICATION (`Yuchao Liu` & Global Totals) ===")
    yc = conn.execute(
        "SELECT id, name, school, year, homepage, photo_url FROM people WHERE id = 234311"
    ).fetchone()
    if yc:
        print("  Yuchao Liu (UCSD 2018):", dict(yc))

    for col, lbl in [
        ("homepage != '' AND homepage IS NOT NULL", "Personal / Faculty Website"),
        ("photo_url != '' AND photo_url IS NOT NULL", "Verified Portrait Photo"),
        ("wikipedia_url != '' AND wikipedia_url IS NOT NULL", "Wikipedia"),
        ("google_scholar_url != '' AND google_scholar_url IS NOT NULL", "Google Scholar"),
        ("linkedin_url != '' AND linkedin_url IS NOT NULL", "LinkedIn"),
        ("orcid != '' AND orcid IS NOT NULL", "ORCID"),
        ("wikidata_qid != '' AND wikidata_qid IS NOT NULL", "Wikidata"),
    ]:
        tot = conn.execute(f"SELECT COUNT(*) FROM people WHERE {col}").fetchone()[0]
        core = conn.execute(f"SELECT COUNT(*) FROM people WHERE is_core_stat = 1 AND {col}").fetchone()[0]
        print(f"  GLOBAL {lbl:28s}: {tot:7,d} total  |  {core:6,d} Core Stat/Biostat/Prob")

    conn.close()
    print(f"\nExhaustive Internet-Era harvest completed cleanly in {time.time() - t0:.1f}s!")


if __name__ == "__main__":
    main()
