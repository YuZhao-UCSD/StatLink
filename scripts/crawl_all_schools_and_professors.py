#!/usr/bin/env python3
"""
Global All-Schools & All-Professors Discovery, Audit & Recall-Maximization Pipeline
(`scripts/crawl_all_schools_and_professors.py`).

Executes 6 systematic recall-maximizing passes across all 207 global Statistics, Biostatistics,
Probability, and Quantitative Data Science PhD-granting universities:

  Pass 1: Departmental Faculty Roster Crawler across the 207 master schools — extracts current
          Statistics & Biostatistics professors, verifies whether each professor is in the DB,
          queries Live MGP (`mathgenealogy.org`) to import any missing professors + their PhD
          advisor lineages, and collects faculty personal/profile URLs.
  Pass 2: Parallel Faculty Website & `/people` / `/students` Group Crawler across ~1,500+
          remaining stored & newly discovered faculty websites to extract missing PhD advisees
          and missing upward advisor links.
  Pass 3: Full 28-page Stanford Statistics PhD Alumni Directory crawl (`?page=1..28`).
  Pass 4: Expanded DSpace 7 JSON + DigitalCommons Qualified Dublin Core (`qdc`) University ETD
          harvesters (Baylor, Kansas State, Missouri, Arizona, UConn, LSU, Kentucky, South Carolina,
          SMU, New Mexico, Clemson, Auburn, Nebraska-Lincoln, Utah State, Montana State, Wayne State).
  Pass 5: Full uncapped Wikidata Global Scholarly Graph expansion (`P812` stat/biostat/prob major +
          `P184`/`P185` doctoral advisor/student network + `P108` university employer affiliations).
  Pass 6: Constructs `advisor_schools` table (mapping every professor to the universities where they
          teach and advise PhDs), recomputes exact DAG descendant counts, updates school stats, and
          rebuilds the FTS5 full-text index.
"""

import concurrent.futures
import html
import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict, deque
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

from build_database import canonicalize_school
from crawler_engine import (
    HEADERS,
    clean_extracted_person_name,
    discover_urls_via_ddg,
    fetch_live_mgp_students,
    fold_ascii,
    norm_key,
    parse_html_group_page,
)
from harvest_web_and_etds import (
    MASTERS_REJECT_RE,
    STAT_SUBJ_RE,
    STAT_TITLE_RE,
    clean_person_name,
    first_last_sig,
     harvest_dspace7_repo,
    infer_field,
)

DB_PATH = os.path.join(BASE_DIR, "data", "stat_genealogy.db")
MASTER_SCHOOLS_PATH = os.path.join(BASE_DIR, "data", "stat_departments_master.json")
CACHE_DIR = os.path.join(BASE_DIR, "data_raw", "all_schools_harvest")


# ---------------------------------------------------------------------------
# Pass 1 Helpers: Departmental Faculty Roster Extraction & Live MGP Lookup
# ---------------------------------------------------------------------------

FACULTY_BAD_LINK_TEXT = {
    "read more", "view profile", "full profile", "personal website", "website", "homepage",
    "cv", "curriculum vitae", "email", "contact", "directory", "faculty", "people",
    "emeritus", "adjunct", "staff", "students", "phd students", "alumni", "graduate",
    "undergraduate", "research", "publications", "courses", "news", "events", "home",
    "about", "back to top", "skip to main content", "google scholar", "orcid",
}


def extract_faculty_from_dept_html(html_text: str, base_url: str, school: str, is_biostat: bool):
    """Extracts (faculty_name, profile_or_personal_url, field) from a departmental faculty roster page."""
    soup = BeautifulSoup(html_text, "lxml")
    main = soup.find("main") or soup.body or soup
    extracted = {}

    # Strategy 1: Headings & profile links inside person cards / tables / directory rows
    for el in main.find_all(["h2", "h3", "h4", "a", "strong", "td"]):
        txt = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
        if not txt or len(txt) > 55 or txt.lower() in FACULTY_BAD_LINK_TEXT:
            continue
        # Skip obvious navigation items unless inside a person container
        if el.name == "a":
            href = (el.get("href") or "").strip()
            if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
                continue
            # Profile URLs usually contain /people/, /faculty/, /profile, /directory/, /users/, /staff/, ~
            h_low = href.lower()
            parent_cls = " ".join((el.parent.get("class") or []) if el.parent else []).lower()
            if not any(k in h_low for k in ["/people/", "/faculty/", "/profile", "/directory/", "/users/", "/staff/", "/~", "person", "member"]) and not any(
                k in parent_cls for k in ["person", "faculty", "profile", "member", "views-field", "card", "directory"]
            ):
                continue
            nm = clean_extracted_person_name(txt)
            if nm:
                nk = norm_key(nm)
                full_u = urljoin(base_url, href)
                extracted.setdefault(nk, (nm, full_u, "Biostatistics" if is_biostat else "Statistics"))
        elif el.name in ("h2", "h3", "h4"):
            parent_cls = " ".join((el.parent.get("class") or []) if el.parent else []).lower()
            el_cls = " ".join(el.get("class") or []).lower()
            a_child = el.find("a", href=True)
            if a_child or any(k in (parent_cls + " " + el_cls) for k in ["person", "faculty", "profile", "member", "card", "staff", "directory", "views"]):
                nm = clean_extracted_person_name(txt)
                if nm:
                    nk = norm_key(nm)
                    full_u = urljoin(base_url, a_child["href"]) if a_child else ""
                    extracted.setdefault(nk, (nm, full_u, "Biostatistics" if is_biostat else "Statistics"))

    return list(extracted.values())


def crawl_school_faculty_roster(school_entry: dict):
    school = school_entry["school"]
    depts = school_entry.get("departments") or []
    urls = list(school_entry.get("seed_urls") or [])
    is_biostat_default = any("biostat" in d.lower() for d in depts) and not any(
        "department of statistics" in d.lower() or "school of statistics" in d.lower() for d in depts
    )

    # If no seed URL provided, discover via DuckDuckGo HTML search
    if not urls:
        dept_q = depts[0] if depts else "Department of Statistics"
        try:
            r = requests.get(
                "https://html.duckduckgo.com/html/",
                params={"q": f'"{school}" "{dept_q}" faculty directory'},
                headers=HEADERS,
                timeout=8,
            )
            if r.status_code == 200:
                sp = BeautifulSoup(r.text, "lxml")
                for a in sp.select(".result__a")[:2]:
                    href = a.get("href") or ""
                    if "uddg=" in href:
                        qs = urllib.parse.parse_qs(urlparse(href).query)
                        if "uddg" in qs:
                            href = urllib.parse.unquote(qs["uddg"][0])
                    if href.startswith("http") and not any(b in href for b in ["wikipedia", "linkedin", "usnews", "niche.com"]):
                        urls.append(href)
        except Exception:
            pass

    faculty = []
    sess = requests.Session()
    for u in urls[:3]:
        try:
            resp = sess.get(u, headers=HEADERS, timeout=9, allow_redirects=True)
            if resp.status_code == 200 and "html" in resp.headers.get("content-type", "").lower():
                is_b = "biostat" in u.lower() or is_biostat_default
                fac = extract_faculty_from_dept_html(resp.text, resp.url, school, is_b)
                for nm, pu, fld in fac:
                    faculty.append({"name": nm, "school": school, "profile_url": pu, "field": fld})
        except Exception:
            pass
    return school, faculty


def lookup_missing_prof_on_live_mgp(name: str):
    """Queries live mathgenealogy.org for a missing professor to get their PhD school, year, thesis, and advisors."""
    parts = name.split()
    if len(parts) < 2:
        return None
    given, family = parts[0], parts[-1]
    try:
        r = requests.post(
            "https://www.mathgenealogy.org/query-prep.php",
            data={"family_name": family, "given_name": given, "other_names": ""},
            headers=HEADERS,
            timeout=9,
        )
        if r.status_code != 200:
            return None
        soup = BeautifulSoup(r.text, "lxml")
        mgp_id = None
        m_url = re.search(r"id\.php\?id=(\d+)", r.url)
        if m_url:
            mgp_id = int(m_url.group(1))
        else:
            # Check if exactly 1 result or matching full name in table
            table = soup.find("table")
            if table:
                matches = []
                for tr in table.find_all("tr"):
                    a = tr.find("a", href=re.compile(r"id\.php\?id=(\d+)"))
                    if a:
                        mid = int(re.search(r"id=(\d+)", a["href"]).group(1))
                        anm = a.get_text(" ", strip=True)
                        matches.append((mid, anm))
                if len(matches) == 1:
                    mgp_id = matches[0][0]
                else:
                    nk_target = norm_key(name)
                    for mid, anm in matches:
                        if norm_key(clean_person_name(anm)) == nk_target:
                            mgp_id = mid
                            break
            if mgp_id:
                r = requests.get(f"https://www.mathgenealogy.org/id.php?id={mgp_id}", headers=HEADERS, timeout=9)
                soup = BeautifulSoup(r.text, "lxml")

        if not mgp_id:
            return None

        h2 = soup.find("h2")
        disp_name = re.sub(r"\s+", " ", h2.get_text(" ", strip=True)).strip() if h2 else name
        phd_school = ""
        phd_year = None
        span_sch = soup.find("span", style=re.compile(r"#006633"))
        if span_sch:
            phd_school = canonicalize_school(span_sch.get_text(" ", strip=True))
            ptxt = span_sch.parent.get_text(" ", strip=True) if span_sch.parent else ""
            ym = re.search(r"\b(19\d{2}|20[0-2]\d)\b", ptxt)
            if ym:
                phd_year = int(ym.group(1))
        th_div = soup.find("div", id="thesisTitle")
        thesis = re.sub(r"\s+", " ", th_div.get_text(" ", strip=True)).strip() if th_div else ""
        advisor_mgp_ids = []
        for p_tag in soup.find_all("p"):
            if "Advisor" in p_tag.get_text(" ", strip=True):
                for a in p_tag.find_all("a", href=re.compile(r"id\.php\?id=(\d+)")):
                    advisor_mgp_ids.append(int(re.search(r"id=(\d+)", a["href"]).group(1)))
        students = fetch_live_mgp_students(mgp_id, disp_name)
        return {
            "mgp_id": mgp_id,
            "name": disp_name,
            "phd_school": phd_school,
            "phd_year": phd_year,
            "thesis": thesis,
            "advisor_mgp_ids": advisor_mgp_ids,
            "students": students,
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Pass 3 Helper: Full All-Pages Stanford Statistics PhD Alumni Crawler
# ---------------------------------------------------------------------------

def harvest_all_stanford_alumni_pages(stanford_ln_map: dict):
    profile_urls = set()
    sess = requests.Session()

    def fetch_list_page(pg):
        urls = []
        try:
            r = sess.get(f"https://statistics.stanford.edu/people/alumni?page={pg}", headers=HEADERS, timeout=12)
            if r.status_code != 200:
                return urls
            sp = BeautifulSoup(r.text, "lxml")
            main = sp.find("main") or sp
            skip = {
                "/people/faculty", "/people/stein-fellows", "/people/lecturers-research-scientists",
                "/people/postdocs", "/people/phd-students", "/people/masters-students",
                "/people/staff", "/people/alumni",
            }
            for a in main.find_all("a", href=True):
                h = a["href"].strip()
                if h.startswith("/people/") and h not in skip and "?" not in h:
                    urls.append("https://statistics.stanford.edu" + h)
        except Exception:
            pass
        return urls

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for batch in pool.map(fetch_list_page, range(1, 29)):
            profile_urls.update(batch)

    print(f"    -> Discovered {len(profile_urls):,} Stanford Statistics PhD alumni profile URLs across pages 1..28")

    def parse_prof(url):
        try:
            pr = requests.get(url, headers=HEADERS, timeout=9)
            if pr.status_code != 200:
                return None
            sp = BeautifulSoup(pr.text, "lxml")
            h1 = sp.find("h1")
            nm = clean_person_name(h1.get_text(" ", strip=True) if h1 else "")
            if not nm:
                return None
            main = sp.find("main") or sp
            hp = ""
            for a_el in main.find_all("a", href=True):
                if "personal website" in a_el.get_text(" ", strip=True).lower():
                    hp = a_el["href"].strip()
            lines = [ln.strip() for ln in main.get_text("\n", strip=True).split("\n") if ln.strip()]
            yr, th, adv_raw = None, "", ""
            for i, ln in enumerate(lines):
                if ln == "Graduation Year" and i + 1 < len(lines) and lines[i + 1].isdigit():
                    yr = int(lines[i + 1])
                elif ln == "Dissertation Title" and i + 1 < len(lines) and lines[i + 1] not in ("Advisor Name", "Committee Names", "Related News"):
                    th = lines[i + 1]
                elif ln in ("Advisor Name", "Advisor Names", "Advisor") and i + 1 < len(lines) and lines[i + 1] not in ("Committee Names", "Related News", "Awards"):
                    adv_raw = lines[i + 1]
            if not adv_raw:
                return None
            advisors = []
            for ch in re.split(r"[/,;]| and ", adv_raw):
                c = re.sub(r"\([^)]*\)", "", ch).strip()
                if not c:
                    continue
                if " " in c:
                    cn = clean_person_name(c)
                    if cn:
                        advisors.append(cn)
                else:
                    fac = stanford_ln_map.get(fold_ascii(c).lower())
                    if fac:
                        advisors.append(fac)
            if not advisors:
                return None
            return {
                "student": nm,
                "advisors": advisors,
                "school": "Stanford University",
                "country": "United States",
                "year": yr,
                "thesis": th,
                "subjects": "Statistics",
                "homepage": hp,
                "source": "Stanford Dept Directory",
            }
        except Exception:
            return None

    records = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
        for res in pool.map(parse_prof, sorted(profile_urls)):
            if res:
                records.append(res)
    print(f"    -> Extracted {len(records):,} Stanford Statistics PhDs with verified advisors!")
    return records


# ---------------------------------------------------------------------------
# Pass 4 Helper: DigitalCommons Qualified Dublin Core (`qdc`) OAI-PMH Harvester
# ---------------------------------------------------------------------------

DIGITALCOMMONS_OAI_REPOS = [
    ("University of Connecticut", "United States", "https://opencommons.uconn.edu/do/oai/", "publication:dissertations"),
    ("Louisiana State University", "United States", "https://repository.lsu.edu/do/oai/", "publication:gradschool_dissertations"),
    ("University of Kentucky", "United States", "https://uknowledge.uky.edu/do/oai/", "publication:stat_etds"),
    ("University of Kentucky", "United States", "https://uknowledge.uky.edu/do/oai/", "publication:epb_etds"),
    ("University of South Carolina", "United States", "https://scholarcommons.sc.edu/do/oai/", "publication:etd"),
    ("Southern Methodist University", "United States", "https://scholar.smu.edu/do/oai/", "publication:hum_sci_statisticalscience_etds"),
    ("University of New Mexico", "United States", "https://digitalrepository.unm.edu/do/oai/", "publication:math_etds"),
    ("Clemson University", "United States", "https://open.clemson.edu/do/oai/", "publication:all_dissertations"),
    ("University of Nebraska-Lincoln", "United States", "https://digitalcommons.unl.edu/do/oai/", "publication:dissertations"),
    ("Western Michigan University", "United States", "https://scholarworks.wmich.edu/do/oai/", "publication:dissertations"),
    ("Utah State University", "United States", "https://digitalcommons.usu.edu/do/oai/", "publication:etd"),
    ("Montana State University", "United States", "https://scholarworks.montana.edu/oai", ""),
    ("Wayne State University", "United States", "https://digitalcommons.wayne.edu/do/oai/", "publication:oa_dissertations"),
    ("Bowling Green State University", "United States", "https://scholarworks.bgsu.edu/do/oai/", "publication:math_diss"),
]

EXTRA_DSPACE7_REPOS = [
    ("Baylor University", "United States", "https://baylor-ir.tdl.org/server/api/discover/search/objects"),
    ("Kansas State University", "United States", "https://krex.k-state.edu/server/api/discover/search/objects"),
    ("University of Missouri", "United States", "https://mospace.umsystem.edu/server/api/discover/search/objects"),
    ("University of Arizona", "United States", "https://repository.arizona.edu/server/api/discover/search/objects"),
]


def harvest_digitalcommons_oai(school: str, country: str, oai_url: str, set_spec: str):
    records = []
    sess = requests.Session()
    token = None
    pages = 0
    while pages < 18:
        pages += 1
        params = {"verb": "ListRecords"}
        if token:
            params["resumptionToken"] = token
        else:
            params["metadataPrefix"] = "qdc"
            if set_spec:
                params["set"] = set_spec
        try:
            r = sess.get(oai_url, params=params, headers=HEADERS, timeout=18)
            if r.status_code != 200 or "<OAI-PMH" not in r.text:
                break
            root = ET.fromstring(r.text)
            ns_oai = "{http://www.openarchives.org/OAI/2.0/}"
            for rec_el in root.findall(f".//{ns_oai}record"):
                meta_el = rec_el.find(f"{ns_oai}metadata")
                if meta_el is None:
                    continue
                title, creator, discipline, deg_name, date_str = "", "", "", "", ""
                contributors = []
                for child in meta_el.iter():
                    tag = child.tag.split("}")[-1]
                    val = (child.text or "").strip()
                    if not val:
                        continue
                    if tag == "title" and not title:
                        title = re.sub(r"<[^>]+>", "", html.unescape(val)).strip()
                    elif tag == "creator" and not creator:
                        creator = val
                    elif tag == "contributor":
                        contributors.append(val)
                    elif tag == "thesis.degree.discipline":
                        discipline = val
                    elif tag == "thesis.degree.name":
                        deg_name = val
                    elif tag in ("date.created", "date", "date.available") and not date_str:
                        date_str = val

                if not creator or not contributors or not title:
                    continue
                if deg_name and re.search(r"\b(master|m\.s\.|m\.a\.|bachelor)\b", deg_name, re.I) and not re.search(r"\b(doctor|ph\.?d)\b", deg_name, re.I):
                    continue
                if MASTERS_REJECT_RE.search(title):
                    continue
                if not (STAT_SUBJ_RE.search(discipline) or STAT_TITLE_RE.search(title)):
                    continue

                stu = clean_person_name(creator)
                # Strictly take FIRST `<dc:contributor>` = Major PhD Advisor!
                major_adv = clean_person_name(contributors[0])
                if not stu or not major_adv:
                    continue
                ym = re.search(r"\b(19\d{2}|20[0-2]\d)\b", date_str)
                yr = int(ym.group(1)) if ym else None
                records.append({
                    "student": stu,
                    "advisors": [major_adv],
                    "school": school,
                    "country": country,
                    "year": yr,
                    "thesis": re.sub(r"\s+", " ", title),
                    "subjects": discipline or "Statistics",
                    "homepage": "",
                    "source": "DigitalCommons ETD",
                })
            tok_el = root.find(f".//{ns_oai}resumptionToken")
            token = (tok_el.text or "").strip() if tok_el is not None else None
            if not token:
                break
        except Exception:
            break
    if records:
        print(f"    -> DigitalCommons OAI {school} ({set_spec}): +{len(records):,} PhDs")
    return records


# ---------------------------------------------------------------------------
# Pass 5 Helper: Expanded Uncapped Wikidata Doctoral & Affiliation Harvester
# ---------------------------------------------------------------------------

def harvest_expanded_wikidata():
    rows_all = []
    queries = [
        # Query A: Everyone majoring (P812) in Stat/Biostat/Prob/DataScience/Actuarial/Econometrics
        """
        SELECT ?item ?itemLabel ?mgp ?aft ?orcid ?advisor ?advisorLabel ?advisorMgp ?schoolLabel ?empLabel ?year WHERE {
          VALUES ?statField { wd:Q12483 wd:Q214781 wd:Q5862903 wd:Q475298 wd:Q160000 wd:Q216372 wd:Q2374463 }
          ?item p:P69 ?st .
          ?st ps:P69 ?school .
          ?st pq:P812 ?statField .
          OPTIONAL { ?st pq:P582 ?dt . BIND(YEAR(?dt) AS ?year) }
          OPTIONAL { ?item wdt:P549 ?mgp . }
          OPTIONAL { ?item wdt:P2381 ?aft . }
          OPTIONAL { ?item wdt:P496 ?orcid . }
          OPTIONAL { ?item wdt:P184 ?advisor . OPTIONAL { ?advisor wdt:P549 ?advisorMgp . } }
          OPTIONAL { ?item wdt:P108 ?emp . }
          SERVICE wikibase:label { bd:serviceParam wikibase:language "en,fr,de,es". }
        }
        """,
        # Query B: Everyone advised (P184) by a Statistician, Biostatistician, Probabilist, or Econometrician
        """
        SELECT ?item ?itemLabel ?mgp ?aft ?orcid ?advisor ?advisorLabel ?advisorMgp ?schoolLabel ?empLabel ?year WHERE {
          VALUES ?advOcc { wd:Q2732142 wd:Q19501955 wd:Q17167049 wd:Q15995642 }
          ?advisor wdt:P106 ?advOcc .
          ?item wdt:P184 ?advisor .
          OPTIONAL { ?item wdt:P549 ?mgp . }
          OPTIONAL { ?item wdt:P2381 ?aft . }
          OPTIONAL { ?item wdt:P496 ?orcid . }
          OPTIONAL { ?advisor wdt:P549 ?advisorMgp . }
          OPTIONAL { ?item wdt:P108 ?emp . }
          OPTIONAL {
            ?item p:P69 ?st . ?st ps:P69 ?school .
            OPTIONAL { ?st pq:P582 ?dt . BIND(YEAR(?dt) AS ?year) }
          }
          SERVICE wikibase:label { bd:serviceParam wikibase:language "en,fr,de,es". }
        }
        """,
    ]
    for idx, q in enumerate(queries):
        try:
            url = "https://query.wikidata.org/sparql?format=json&query=" + urllib.parse.quote(q)
            req = urllib.request.Request(url, headers={"User-Agent": "StatLineageExplorer/2.0 (Academic Genealogy)"})
            resp = urllib.request.urlopen(req, timeout=55)
            bindings = json.loads(resp.read().decode("utf-8"))["results"]["bindings"]
            rows_all.extend(bindings)
            print(f"    -> Wikidata expansion query {idx+1}: fetched {len(bindings):,} scholarly bindings")
        except Exception as exc:
            print(f"    [WARN] Wikidata query {idx+1}: {exc}")
    return rows_all


# ---------------------------------------------------------------------------
# Master Orchestrator
# ---------------------------------------------------------------------------

def main():
    t0 = time.time()
    os.makedirs(CACHE_DIR, exist_ok=True)

    with open(MASTER_SCHOOLS_PATH, "r", encoding="utf-8") as f:
        master_schools = json.load(f)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    print(f"[1/6] Indexing 545k+ existing scholars & crawling faculty rosters across {len(master_schools)} schools...")
    existing_by_norm = defaultdict(list)
    existing_by_sig = defaultdict(list)
    existing_by_mgp = {}
    stanford_ln_map = {}
    uncrawled_db_homepages = []

    # Load previously crawled URLs from Pass 1 if any
    for r in cur.execute(
        "SELECT id, name, norm_name, school, current_affiliation, year, thesis, primary_field, is_core_stat, is_broad_stat, mgp_id, homepage, sources, advisor_count, student_count FROM people"
    ):
        d = dict(r)
        nk = norm_key(d["name"])
        existing_by_norm[nk].append(d)
        if d["mgp_id"]:
            existing_by_mgp[d["mgp_id"]] = d
        sig = first_last_sig(d["name"])
        if sig[0] and sig[1]:
            existing_by_sig[sig].append(d)
            if (d["school"] == "Stanford University" or d["current_affiliation"] == "Stanford University") and d["student_count"] >= 2 and d["is_core_stat"]:
                prev = stanford_ln_map.get(sig[1])
                if prev is None or d["student_count"] > prev[1]:
                    stanford_ln_map[sig[1]] = (d["name"], d["student_count"])
        if d["is_core_stat"] and d["homepage"] and d["year"] and 1965 <= d["year"] <= 2022:
            uncrawled_db_homepages.append((d["id"], d["name"], d["current_affiliation"] or d["school"], d["homepage"]))

    stanford_ln_clean = {k: v[0] for k, v in stanford_ln_map.items()}
    # Skip the first 450 already crawled in Pass 1 -> crawl the remaining ~1,000+ stored Core Stat faculty homepages!
    remaining_db_homepages = uncrawled_db_homepages[450:]

    # Crawl departmental faculty rosters concurrently across the 207 schools
    dept_faculty_found = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
        for sch_name, fac_list in pool.map(crawl_school_faculty_roster, master_schools):
            dept_faculty_found.extend(fac_list)

    print(f"    -> Extracted {len(dept_faculty_found):,} faculty listings across Statistics & Biostatistics department rosters!")

    # Audit extracted professors against our DB: update affiliations, collect profile URLs, & identify missing professors!
    matched_faculty_count = 0
    missing_faculty_candidates = []
    faculty_sites_to_crawl = []
    explicit_faculty_affiliations = set()  # (person_id, school)

    for f_item in dept_faculty_found:
        fnm = f_item["name"]
        fsch = f_item["school"]
        furl = f_item["profile_url"]
        ffld = f_item["field"]
        nk = norm_key(fnm)
        sig = first_last_sig(fnm)

        cands = list(existing_by_norm.get(nk, ()))
        if not cands and sig[0] and sig[1]:
            cands = [c for c in existing_by_sig.get(sig, ()) if c["is_broad_stat"] or c["school"] == fsch or c["current_affiliation"] == fsch]

        chosen = None
        for c in cands:
            if c["current_affiliation"] == fsch or c["school"] == fsch or c["is_core_stat"]:
                chosen = c
                break
        if not chosen and len(cands) == 1:
            chosen = cands[0]

        if chosen:
            matched_faculty_count += 1
            uid = chosen["id"]
            explicit_faculty_affiliations.add((uid, fsch))
            cur.execute(
                """
                UPDATE people SET
                    current_affiliation = ?,
                    is_core_stat = 1,
                    is_broad_stat = 1,
                    primary_field = CASE WHEN primary_field IN ('Statistics','Biostatistics','Probability') THEN primary_field ELSE ? END,
                    homepage = CASE WHEN homepage = '' THEN ? ELSE homepage END
                WHERE id = ?
                """,
                (fsch, ffld, furl, uid),
            )
            if furl and furl.startswith("http"):
                faculty_sites_to_crawl.append((uid, chosen["name"], fsch, furl))
        else:
            missing_faculty_candidates.append(f_item)

    print(f"    -> Audited departmental faculty: {matched_faculty_count:,} matched & updated in DB; {len(missing_faculty_candidates):,} missing candidates queued for Live MGP & web lookup!")

    # Look up up to 220 missing departmental professors on Live MGP concurrently to import them + their PhD lineages!
    live_mgp_imported = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        futs = {pool.submit(lookup_missing_prof_on_live_mgp, mf["name"]): mf for mf in missing_faculty_candidates[:220]}
        for fut in concurrent.futures.as_completed(futs):
            mf = futs[fut]
            res = fut.result()
            if res:
                res["faculty_school"] = mf["school"]
                res["faculty_url"] = mf["profile_url"]
                res["faculty_field"] = mf["field"]
                live_mgp_imported.append(res)

    print(f"    -> Verified & retrieved {len(live_mgp_imported):,} missing professors + PhD advisor lineages from Live MGP!")

    # -----------------------------------------------------------------------
    # Pass 2: Parallel Crawl of Remaining 1,000+ DB Faculty Homepages + Discovered Dept Profile URLs
    # -----------------------------------------------------------------------
    all_sites_queue = remaining_db_homepages + faculty_sites_to_crawl[:700]
    print(f"\n[2/6] Crawling {len(all_sites_queue):,} faculty websites & profile pages for PhD students & upward advisors...")

    ADVISOR_MENTION_RE = re.compile(
        r"(?:Ph\.?\s*D\.?[^.\n]{0,90}?(?:[Aa]dvis[eo]rs?|[Ss]upervisors?|[Cc]hairs?)\s*[:\-]?\s*|(?:\b[Aa]dvis[eo]r|\b[Ss]upervised\s+by)\s*[:\-]\s*)"
        r"([A-Z][a-zA-Z\u00C0-\u017F'\-]+(?:\s+[A-Z][a-zA-Z\u00C0-\u017F'\-\.]*){1,3})"
    )

    def crawl_faculty_task(task):
        prof_id, prof_name, prof_sch, url = task
        discovered_stus = []
        upward_adv_name = ""
        sess = requests.Session()
        try:
            r = sess.get(url, headers=HEADERS, timeout=6, allow_redirects=True)
            if r.status_code != 200 or "html" not in r.headers.get("content-type", "").lower():
                return prof_id, prof_name, prof_sch, [], ""
            discovered_stus.extend(parse_html_group_page(r.text, prof_name, prof_sch))
            soup = BeautifulSoup(r.text, "lxml")
            plain = soup.get_text(" ", strip=True)
            m_adv = ADVISOR_MENTION_RE.search(plain)
            if m_adv:
                upward_adv_name = clean_extracted_person_name(m_adv.group(1))

            host = urlparse(r.url).netloc
            subs = []
            for a in soup.find_all("a", href=True):
                atxt = a.get_text(" ", strip=True).lower()
                href = a["href"].strip()
                if any(w in atxt or w in href.lower() for w in ["student", "people", "group", "alumni", "team", "lab"]):
                    fu = urljoin(r.url, href)
                    if urlparse(fu).netloc == host and fu != r.url and not fu.lower().endswith(".pdf"):
                        subs.append(fu)
                        if len(subs) >= 2:
                            break
            for su in subs:
                try:
                    sr = sess.get(su, headers=HEADERS, timeout=5)
                    if sr.status_code == 200 and "html" in sr.headers.get("content-type", "").lower():
                        discovered_stus.extend(parse_html_group_page(sr.text, prof_name, prof_sch))
                except Exception:
                    pass
        except Exception:
            pass
        return prof_id, prof_name, prof_sch, discovered_stus, upward_adv_name

    harvested_records = []
    upward_advisor_discoveries = []  # (prof_id, adv_name)

    with concurrent.futures.ThreadPoolExecutor(max_workers=28) as pool:
        for pid, pnm, psch, stus, up_adv in pool.map(crawl_faculty_task, all_sites_queue):
            for s in stus:
                harvested_records.append({
                    "student": s["student"],
                    "advisors": [pnm],
                    "school": s.get("school") or psch,
                    "country": "",
                    "year": s.get("year"),
                    "thesis": s.get("thesis") or "",
                    "subjects": "Statistics",
                    "homepage": "",
                    "source": "Faculty Web Crawl",
                })
            if up_adv:
                upward_advisor_discoveries.append((pid, up_adv))

    print(f"    -> Faculty Web Crawl yielded {len(harvested_records):,} PhD advisee entries and {len(upward_advisor_discoveries):,} upward advisor mentions!")

    # -----------------------------------------------------------------------
    # Pass 3: Complete All 28 Pages of Stanford Statistics PhD Alumni
    # -----------------------------------------------------------------------
    print("\n[3/6] Harvesting all pages (?page=1..28) of Stanford Statistics PhD Alumni Directory...")
    harvested_records.extend(harvest_all_stanford_alumni_pages(stanford_ln_clean))

    # -----------------------------------------------------------------------
    # Pass 4: Additional DSpace 7 & DigitalCommons (`qdc`) University ETDs
    # -----------------------------------------------------------------------
    print("\n[4/6] Harvesting additional US & International DSpace 7 & DigitalCommons OAI-PMH ETDs...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futs_dc = [pool.submit(harvest_digitalcommons_oai, s, c, u, st) for s, c, u, st in DIGITALCOMMONS_OAI_REPOS]
        futs_ds = [pool.submit(harvest_dspace7_repo, s, c, u) for s, c, u in EXTRA_DSPACE7_REPOS]
        for f in concurrent.futures.as_completed(futs_dc + futs_ds):
            harvested_records.extend(f.result())

    # -----------------------------------------------------------------------
    # Pass 5: Expanded Global Wikidata Scholarly Network
    # -----------------------------------------------------------------------
    print("\n[5/6] Running uncapped Wikidata global Stat/Biostat/Prob doctoral & affiliation expansion...")
    wd_bindings = harvest_expanded_wikidata()

    # -----------------------------------------------------------------------
    # Pass 6: Integrate Everything into SQLite, Build `advisor_schools`, & Recompute Metrics
    # -----------------------------------------------------------------------
    print("\n[6/6] Integrating all new professors, PhD graduates, Wikidata links & building `advisor_schools`...")

    max_id = cur.execute("SELECT MAX(id) FROM people").fetchone()[0] or 5_500_000
    next_id = max(6_000_000, max_id + 100)
    existing_edges = set((r[0], r[1]) for r in cur.execute("SELECT advisor_id, student_id FROM edges").fetchall())

    added_people = 0
    added_edges = 0

    # 6A. Insert Live-MGP-verified missing departmental professors + their MGP advisors & students
    for prof in live_mgp_imported:
        mid = prof["mgp_id"]
        if mid in existing_by_mgp:
            uid = existing_by_mgp[mid]["id"]
            cur.execute(
                "UPDATE people SET current_affiliation = ?, is_core_stat = 1, is_broad_stat = 1, homepage = CASE WHEN homepage = '' THEN ? ELSE homepage END WHERE id = ?",
                (prof["faculty_school"], prof["faculty_url"], uid),
            )
            explicit_faculty_affiliations.add((uid, prof["faculty_school"]))
        else:
            uid = mid if not cur.execute("SELECT 1 FROM people WHERE id = ?", (mid,)).fetchone() else next_id
            if uid == next_id:
                next_id += 1
            nk = norm_key(prof["name"])
            cur.execute(
                """
                INSERT OR IGNORE INTO people (
                    id, name, norm_name, search_aliases, school, raw_school, country, year, thesis,
                    mgp_subject, primary_field, is_core_stat, is_broad_stat, field_confidence,
                    classification_reason, current_affiliation, research_areas, mgp_id, aft_pid,
                    wikidata_qid, orcid, homepage, proquest_id, sources, advisor_count, student_count,
                    descendant_count, stat_descendant_count
                ) VALUES (?,?,?,?,?,'','',?,?,NULL,?,1,1,0.95,'Department Faculty + Live MGP',?,'',?,NULL,'','',?,'','MGP (Live),Dept Roster',0,0,0,0)
                """,
                (uid, prof["name"], nk, "", prof["phd_school"], prof["phd_year"], prof["thesis"], prof["faculty_field"], prof["faculty_school"], mid, prof["faculty_url"]),
            )
            new_d = {
                "id": uid, "name": prof["name"], "norm_name": nk, "school": prof["phd_school"],
                "current_affiliation": prof["faculty_school"], "year": prof["phd_year"], "thesis": prof["thesis"],
                "primary_field": prof["faculty_field"], "is_core_stat": 1, "is_broad_stat": 1, "mgp_id": mid,
                "homepage": prof["faculty_url"], "sources": "MGP (Live),Dept Roster", "advisor_count": 0, "student_count": 0,
            }
            existing_by_norm[nk].append(new_d)
            existing_by_mgp[mid] = new_d
            explicit_faculty_affiliations.add((uid, prof["faculty_school"]))
            added_people += 1

        for adv_mid in prof["advisor_mgp_ids"]:
            if adv_mid in existing_by_mgp:
                aid = existing_by_mgp[adv_mid]["id"]
                if aid != uid and (aid, uid) not in existing_edges:
                    cur.execute("INSERT OR IGNORE INTO edges VALUES (?, ?, 'MGP (Live)')", (aid, uid))
                    existing_edges.add((aid, uid))
                    added_edges += 1

        for stu in prof["students"]:
            harvested_records.append({
                "student": stu["student"],
                "advisors": [prof["name"]],
                "school": canonicalize_school(stu["school"]) or prof["faculty_school"],
                "country": "",
                "year": stu["year"],
                "thesis": "",
                "subjects": prof["faculty_field"],
                "homepage": "",
                "source": "MGP (Live)",
            })

    # Also add remaining missing departmental faculty who weren't in MGP (e.g. recent Biostatistics SPH faculty!)
    checked_set = {norm_key(p["name"]) for p in live_mgp_imported}
    for mf in missing_faculty_candidates:
        nk = norm_key(mf["name"])
        if nk in checked_set or nk in existing_by_norm:
            continue
        # Only add clean 2-3 token names from curated seed URLs
        if not mf["profile_url"]:
            continue
        uid = next_id
        next_id += 1
        cur.execute(
            """
            INSERT INTO people (
                id, name, norm_name, search_aliases, school, raw_school, country, year, thesis,
                mgp_subject, primary_field, is_core_stat, is_broad_stat, field_confidence,
                classification_reason, current_affiliation, research_areas, mgp_id, aft_pid,
                wikidata_qid, orcid, homepage, proquest_id, sources, advisor_count, student_count,
                descendant_count, stat_descendant_count
            ) VALUES (?,?,?,'','','','',NULL,'',NULL,?,1,1,0.90,'Departmental Faculty Roster',?,'',NULL,NULL,'','',?,'','Dept Roster',0,0,0,0)
            """,
            (uid, mf["name"], nk, mf["field"], mf["school"], mf["profile_url"]),
        )
        new_d = {
            "id": uid, "name": mf["name"], "norm_name": nk, "school": "",
            "current_affiliation": mf["school"], "year": None, "thesis": "",
            "primary_field": mf["field"], "is_core_stat": 1, "is_broad_stat": 1, "mgp_id": None,
            "homepage": mf["profile_url"], "sources": "Dept Roster", "advisor_count": 0, "student_count": 0,
        }
        existing_by_norm[nk].append(new_d)
        explicit_faculty_affiliations.add((uid, mf["school"]))
        added_people += 1

    # 6B. Link upward advisor mentions discovered on faculty pages (for faculty who had advisor_count == 0)
    for pid, adv_name in upward_advisor_discoveries:
        has_adv = cur.execute("SELECT 1 FROM edges WHERE student_id = ?", (pid,)).fetchone()
        if has_adv:
            continue
        ank = norm_key(adv_name)
        cands = [c for c in existing_by_norm.get(ank, ()) if c["is_broad_stat"] and c["id"] != pid]
        if len(cands) == 1:
            aid = cands[0]["id"]
            if (aid, pid) not in existing_edges and (pid, aid) not in existing_edges:
                cur.execute("INSERT OR IGNORE INTO edges VALUES (?, ?, 'Faculty Bio')", (aid, pid))
                existing_edges.add((aid, pid))
                added_edges += 1

    # 6C. Integrate Wikidata expansion rows (affiliations, missing international scholars, and missing P184 edges)
    for row in wd_bindings:
        item_lbl = row.get("itemLabel", {}).get("value", "")
        if not item_lbl or item_lbl.startswith("Q"):
            continue
        nm = clean_person_name(item_lbl)
        if not nm:
            continue
        qid = row["item"]["value"].rsplit("/", 1)[-1]
        mid = int(row["mgp"]["value"]) if ("mgp" in row and row["mgp"]["value"].isdigit()) else None
        sch = canonicalize_school(row["schoolLabel"]["value"]) if "schoolLabel" in row else ""
        emp = canonicalize_school(row["empLabel"]["value"]) if "empLabel" in row else ""
        yr = int(row["year"]["value"]) if ("year" in row and str(row["year"]["value"]).lstrip("-").isdigit()) else None
        orcid = row.get("orcid", {}).get("value", "")

        stu_rec = existing_by_mgp.get(mid) if mid else None
        if not stu_rec:
            nk = norm_key(nm)
            for c in existing_by_norm.get(nk, ()):
                if (sch and c["school"] == sch) or (yr and c["year"] and abs(yr - c["year"]) <= 2) or len(existing_by_norm[nk]) == 1:
                    stu_rec = c
                    break

        if stu_rec:
            sid = stu_rec["id"]
            if emp and not stu_rec["current_affiliation"]:
                cur.execute("UPDATE people SET current_affiliation = ?, wikidata_qid = CASE WHEN wikidata_qid='' THEN ? ELSE wikidata_qid END WHERE id = ?", (emp, qid, sid))
                stu_rec["current_affiliation"] = emp
        else:
            sid = next_id
            next_id += 1
            nk = norm_key(nm)
            cur.execute(
                """
                INSERT INTO people (
                    id, name, norm_name, search_aliases, school, raw_school, country, year, thesis,
                    mgp_subject, primary_field, is_core_stat, is_broad_stat, field_confidence,
                    classification_reason, current_affiliation, research_areas, mgp_id, aft_pid,
                    wikidata_qid, orcid, homepage, proquest_id, sources, advisor_count, student_count,
                    descendant_count, stat_descendant_count
                ) VALUES (?,?,?,?,?,'','',?,'',NULL,'Statistics',1,1,0.88,'Wikidata Scholarly Expansion',?,'',NULL,NULL,?,?,'','','Wikidata',0,0,0,0)
                """,
                (sid, nm, nk, "", sch, yr, emp, qid, orcid),
            )
            stu_rec = {
                "id": sid, "name": nm, "norm_name": nk, "school": sch, "current_affiliation": emp,
                "year": yr, "thesis": "", "primary_field": "Statistics", "is_core_stat": 1, "is_broad_stat": 1,
                "mgp_id": None, "homepage": "", "sources": "Wikidata", "advisor_count": 0, "student_count": 0,
            }
            existing_by_norm[nk].append(stu_rec)
            added_people += 1

        # Link Wikidata advisor if present
        adv_mid = int(row["advisorMgp"]["value"]) if ("advisorMgp" in row and row["advisorMgp"]["value"].isdigit()) else None
        adv_lbl = clean_person_name(row.get("advisorLabel", {}).get("value", ""))
        adv_rec = existing_by_mgp.get(adv_mid) if adv_mid else None
        if not adv_rec and adv_lbl:
            ank = norm_key(adv_lbl)
            if len(existing_by_norm.get(ank, ())) == 1:
                adv_rec = existing_by_norm[ank][0]
        if adv_rec and adv_rec["id"] != sid:
            aid = adv_rec["id"]
            if (aid, sid) not in existing_edges and (sid, aid) not in existing_edges:
                cur.execute("INSERT OR IGNORE INTO edges VALUES (?, ?, 'Wikidata')", (aid, sid))
                existing_edges.add((aid, sid))
                added_edges += 1

    # 6D. Integrate all harvested ETD, Stanford Alumni, DigitalCommons, DSpace7 & Faculty Web Crawl records
    def resolve_person_strict(name, school, country, year, thesis, field, is_advisor=False, homepage="", source_label="ETD"):
        nonlocal next_id
        nk = norm_key(name)
        sig = first_last_sig(name)
        cands = list(existing_by_norm.get(nk, ()))
        if sig[0] and sig[1]:
            for x in existing_by_sig.get(sig, ()):
                if x not in cands:
                    cands.append(x)

        best, best_sc = None, -999
        for c in cands:
            sc = 45 if norm_key(c["name"]) == nk else (25 if first_last_sig(c["name"]) == sig else 0)
            if not is_advisor:
                if school and c["school"] and c["school"] != school:
                    continue
                if school and c["school"] == school:
                    sc += 55
                if year and c["year"]:
                    if abs(year - c["year"]) <= 2:
                        sc += 45
                    elif abs(year - c["year"]) > 5:
                        continue
                elif year is None or c["year"] is None:
                    sc += 15
            else:
                if school and (c["school"] == school or c["current_affiliation"] == school):
                    sc += 50
                if year and c["year"] and c["year"] <= year - 3:
                    sc += 35
                elif c["is_broad_stat"]:
                    sc += 25
                if len(cands) == 1 and c["is_broad_stat"]:
                    sc += 35
            if sc > best_sc:
                best_sc = sc
                best = c

        if best and best_sc >= (50 if is_advisor else 75):
            uid = best["id"]
            if not is_advisor:
                cur.execute(
                    """
                    UPDATE people SET
                        year = COALESCE(year, ?),
                        school = CASE WHEN school = '' THEN ? ELSE school END,
                        thesis = CASE WHEN thesis = '' THEN ? ELSE thesis END,
                        homepage = CASE WHEN homepage = '' THEN ? ELSE homepage END
                    WHERE id = ?
                    """,
                    (year, school, thesis, homepage, uid),
                )
            return uid, best, False

        uid = next_id
        next_id += 1
        ic = 1 if field in ("Statistics", "Biostatistics", "Probability") else 0
        ib = 1 if (ic or field == "Related Quantitative") else 0
        new_rec = {
            "id": uid, "name": name, "norm_name": nk,
            "school": "" if is_advisor else school,
            "current_affiliation": school if is_advisor else "",
            "year": None if is_advisor else year,
            "thesis": "" if is_advisor else thesis,
            "primary_field": field, "is_core_stat": ic, "is_broad_stat": ib,
            "mgp_id": None, "homepage": homepage, "sources": source_label,
            "advisor_count": 0, "student_count": 0,
        }
        cur.execute(
            """
            INSERT INTO people (
                id, name, norm_name, search_aliases, school, raw_school, country, year, thesis,
                mgp_subject, primary_field, is_core_stat, is_broad_stat, field_confidence,
                classification_reason, current_affiliation, research_areas, mgp_id, aft_pid,
                wikidata_qid, orcid, homepage, proquest_id, sources, advisor_count, student_count,
                descendant_count, stat_descendant_count
            ) VALUES (?,?,?,?,?,'',?,?,?,NULL,?,?,?,0.88,?,?,'',NULL,NULL,'','',?,'',?,0,0,0,0)
            """,
            (
                uid, name, nk, "", "" if is_advisor else school, country,
                None if is_advisor else year, "" if is_advisor else thesis,
                field, ic, ib, f"Verified via {source_label}",
                school if is_advisor else "", homepage, source_label,
            ),
        )
        existing_by_norm[nk].append(new_rec)
        if sig[0] and sig[1]:
            existing_by_sig[sig].append(new_rec)
        return uid, new_rec, True

    for item in harvested_records:
        sch = canonicalize_school(item.get("school") or "")
        adv_ids = []
        adv_pf = ""
        for anm in item["advisors"]:
            aid, arec, is_n = resolve_person_strict(
                anm, sch, item.get("country") or "", item.get("year"), "", "Statistics",
                is_advisor=True, source_label=item["source"]
            )
            if is_n:
                added_people += 1
            adv_ids.append(aid)
            if arec["primary_field"] in ("Biostatistics", "Statistics", "Probability") and not adv_pf:
                adv_pf = arec["primary_field"]

        s_fld = infer_field(item.get("thesis") or "", item.get("subjects") or "", adv_pf)
        sid, srec, is_ns = resolve_person_strict(
            item["student"], sch, item.get("country") or "", item.get("year"),
            item.get("thesis") or "", s_fld, is_advisor=False,
            homepage=item.get("homepage") or "", source_label=item["source"]
        )
        if is_ns:
            added_people += 1
        for aid in adv_ids:
            if aid != sid and (aid, sid) not in existing_edges and (sid, aid) not in existing_edges:
                cur.execute("INSERT OR IGNORE INTO edges VALUES (?, ?, ?)", (aid, sid, item["source"]))
                existing_edges.add((aid, sid))
                added_edges += 1

    print(f"    -> Added +{added_people:,} net-new scholars and +{added_edges:,} new doctoral edges across all schools!")

    # 6E. Build `advisor_schools` table (tracks every university where a scholar teaches OR advises PhDs!)
    print("    -> Building `advisor_schools` faculty-institution mapping & recomputing DAG metrics...")
    cur.executescript("""
        DROP TABLE IF EXISTS advisor_schools;
        CREATE TABLE advisor_schools (
            person_id INTEGER NOT NULL,
            school TEXT NOT NULL,
            advised_at_school INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (person_id, school)
        );
    """)

    # Insert from students' graduation schools (`edges`)
    cur.execute("""
        INSERT OR REPLACE INTO advisor_schools (person_id, school, advised_at_school)
        SELECT e.advisor_id, s.school, COUNT(*)
        FROM edges e
        JOIN people s ON s.id = e.student_id
        WHERE s.school != ''
        GROUP BY e.advisor_id, s.school
    """)
    # Insert from `current_affiliation`
    cur.execute("""
        INSERT OR IGNORE INTO advisor_schools (person_id, school, advised_at_school)
        SELECT id, current_affiliation, 0
        FROM people
        WHERE current_affiliation != ''
    """)
    for pid, fsch in explicit_faculty_affiliations:
        cur.execute("INSERT OR IGNORE INTO advisor_schools VALUES (?, ?, 0)", (pid, fsch))

    cur.execute("CREATE INDEX idx_adv_schools_school ON advisor_schools(school, advised_at_school DESC)")
    cur.execute("CREATE INDEX idx_adv_schools_person ON advisor_schools(person_id)")

    # Recompute exact DAG reachability
    valid_edges = set(
        (r[0], r[1])
        for r in cur.execute(
            "SELECT e.advisor_id, e.student_id FROM edges e JOIN people p1 ON p1.id=e.advisor_id JOIN people p2 ON p2.id=e.student_id"
        ).fetchall()
    )
    advisors_of = defaultdict(list)
    students_of = defaultdict(list)
    for aid, sid in valid_edges:
        advisors_of[sid].append(aid)
        students_of[aid].append(sid)

    core_flags = {r[0]: r[1] for r in cur.execute("SELECT id, is_core_stat FROM people").fetchall()}
    all_uids = list(core_flags.keys())
    in_deg = {u: len(advisors_of.get(u, ())) for u in all_uids}
    q = deque([u for u, d in in_deg.items() if d == 0])
    vis = 0
    while q:
        u = q.popleft()
        vis += 1
        for v in students_of.get(u, ()):
            in_deg[v] -= 1
            if in_deg[v] == 0:
                q.append(v)
    if vis < len(all_uids):
        rem = {u for u, d in in_deg.items() if d > 0}
        st_dfs = {}
        bad = []
        for s0 in rem:
            if st_dfs.get(s0, 0) != 0:
                continue
            stk = [(s0, 0)]
            st_dfs[s0] = 1
            while stk:
                u, idx = stk[-1]
                nbrs = [v for v in students_of.get(u, ()) if v in rem and (u, v) not in bad]
                if idx < len(nbrs):
                    stk[-1] = (u, idx + 1)
                    v = nbrs[idx]
                    if st_dfs.get(v, 0) == 1:
                        bad.append((u, v))
                    elif st_dfs.get(v, 0) == 0:
                        st_dfs[v] = 1
                        stk.append((v, 0))
                else:
                    st_dfs[u] = 2
                    stk.pop()
        for a, s in bad:
            cur.execute("DELETE FROM edges WHERE advisor_id = ? AND student_id = ?", (a, s))
            valid_edges.discard((a, s))
        advisors_of.clear()
        students_of.clear()
        for aid, sid in valid_edges:
            advisors_of[sid].append(aid)
            students_of[aid].append(sid)

    out_rem = {u: len(students_of.get(u, ())) for u in all_uids}
    parent_rem = {u: len(advisors_of.get(u, ())) for u in all_uids}
    leaf_q = deque([u for u, d in out_rem.items() if d == 0])
    desc_sets = {}
    updates = []
    while leaf_q:
        u = leaf_q.popleft()
        stus = students_of.get(u, ())
        if not stus:
            dc, sdc = 0, 0
        else:
            dset = set(stus)
            for s in stus:
                cs = desc_sets.get(s)
                if cs:
                    dset.update(cs)
                parent_rem[s] -= 1
                if parent_rem[s] <= 0:
                    desc_sets.pop(s, None)
            dc = len(dset)
            sdc = sum(1 for x in dset if core_flags.get(x))
            if parent_rem[u] > 0:
                desc_sets[u] = dset
        updates.append((len(advisors_of.get(u, ())), len(stus), dc, sdc, u))
        for p in advisors_of.get(u, ()):
            out_rem[p] -= 1
            if out_rem[p] == 0:
                leaf_q.append(p)

    cur.executemany(
        "UPDATE people SET advisor_count=?, student_count=?, descendant_count=?, stat_descendant_count=? WHERE id=?",
        updates,
    )

    cur.execute("""
        INSERT OR REPLACE INTO schools (school, norm_school, aliases, country, core_stat_count, broad_stat_count, total_phd_count, min_year, max_year)
        SELECT
            p.school, LOWER(p.school),
            COALESCE((SELECT s.aliases FROM schools s WHERE s.school = p.school), ''),
            MAX(p.country), SUM(p.is_core_stat), SUM(p.is_broad_stat), COUNT(*), MIN(p.year), MAX(p.year)
        FROM people p WHERE p.school != '' GROUP BY p.school
    """)

    meta = json.loads(cur.execute("SELECT value_json FROM meta_stats WHERE key='summary'").fetchone()[0])
    field_dist = dict(cur.execute("SELECT primary_field, COUNT(*) FROM people GROUP BY primary_field").fetchall())
    decade_hist = defaultdict(dict)
    for dec, pf, cnt in cur.execute(
        "SELECT (year/10)*10 AS dec, primary_field, COUNT(*) FROM people WHERE year BETWEEN 1900 AND 2027 AND is_core_stat=1 GROUP BY dec, primary_field"
    ):
        decade_hist[str(dec)][pf] = cnt

    meta.update({
        "total_people": cur.execute("SELECT COUNT(*) FROM people").fetchone()[0],
        "total_edges": cur.execute("SELECT COUNT(*) FROM edges").fetchone()[0],
        "core_stat_total": cur.execute("SELECT COUNT(*) FROM people WHERE is_core_stat=1").fetchone()[0],
        "broad_stat_total": cur.execute("SELECT COUNT(*) FROM people WHERE is_broad_stat=1").fetchone()[0],
        "by_field": field_dist,
        "master_stat_schools_tracked": len(master_schools),
        "faculty_roster_matched": matched_faculty_count,
        "total_schools": cur.execute("SELECT COUNT(*) FROM schools").fetchone()[0],
        "core_stat_schools": cur.execute("SELECT COUNT(*) FROM schools WHERE core_stat_count > 0").fetchone()[0],
        "decade_histogram": dict(sorted(decade_hist.items())),
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    cur.execute("INSERT OR REPLACE INTO meta_stats VALUES ('summary', ?)", (json.dumps(meta),))
    cur.execute("INSERT INTO people_fts(people_fts) VALUES('rebuild')")
    conn.commit()
    conn.close()

    print(f"\nALL-SCHOOLS & ALL-PROFESSORS PIPELINE COMPLETE in {time.time() - t0:.1f}s!")
    print("Updated Summary:\n", json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
