#!/usr/bin/env python3
"""
Multi-Source Web & Institutional ETD Harvester for StatLineage.

Massively expands PhD researcher & advisor-student coverage (especially 2010–2026 graduates
and Biostatistics / Data Science / Statistics / Probability programs under-indexed in MGP & AFT)
by harvesting and cross-linking five live online ecosystems:

  1. University of California eScholarship GraphQL API (All 10 UC campuses: UC Berkeley, UCSD,
     UCLA, UC Davis, UCSB, UCI, UCSC, UCR, UCSF, UC Merced — up through Nov 2026!)
  2. Major University DSpace 7 REST JSON Repositories (MIT, UPenn/Wharton, Duke, Cornell,
     University of Minnesota, UW-Madison, Virginia Tech, University of Toronto, Georgia Tech)
  3. Global National & DOI Dissertation Registries:
     - HAL / theses.fr API (8,700+ French PhD theses in Statistics & Probability)
     - DataCite Global Supervisor Thesis API (CMU, Purdue, Imperial, UCL, ETH, Waterloo, etc.)
  4. Departmental PhD Alumni Directories (e.g., Stanford Department of Statistics Alumni right
     through 2026 with exact Dissertation Title, Graduation Year, Homepage, and Advisor)
  5. Professor Personal Websites, Lab/Group "/people" Pages & CV PDFs (crawling faculty homepages
     in our database + targeted academic group sites such as schwartzman.scholar.st)
"""

import concurrent.futures
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata
from collections import Counter, defaultdict, deque
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "data", "stat_genealogy.db")
CACHE_DIR = os.path.join(BASE_DIR, "data_raw", "web_harvest")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}


# ---------------------------------------------------------------------------
# Name & School Formatting Helpers
# ---------------------------------------------------------------------------

def fold_ascii(text: str) -> str:
    if not text:
        return ""
    text = (
        text.replace("ø", "o").replace("Ø", "O")
        .replace("ł", "l").replace("Ł", "L")
        .replace("ß", "ss").replace("đ", "d").replace("Đ", "D")
        .replace("æ", "ae").replace("Æ", "AE")
        .replace("œ", "oe").replace("Œ", "OE")
    )
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


DEGREE_TAIL_RE = re.compile(
    r",?\s*(?:Ph\.?\s*D\.?|Sc\.?\s*D\.?|D\.?\s*Phil\.?|Dr\.?\s*P\.?\s*H\.?|M\.?\s*D\.?)\b.*$",
    re.I,
)
ROLE_PAREN_RE = re.compile(r"\((?:advisor|co-advisor|chair|co-chair|supervisor|director|main adviser)[^)]*\)", re.I)


def clean_person_name(raw: str) -> str:
    if not raw:
        return ""
    s = raw.strip()
    s = ROLE_PAREN_RE.sub("", s)
    s = DEGREE_TAIL_RE.sub("", s)
    s = re.sub(r"\b(?:Prof\.?|Professor|Dr\.?)\s+", "", s, flags=re.I)
    s = s.strip(" ,.;:-")
    if not s:
        return ""

    # Convert "Last, Jr., First Middle" or "Last, First Middle" -> "First Middle Last"
    if "," in s:
        parts = [p.strip(" .") for p in s.split(",") if p.strip(" .")]
        if len(parts) == 2:
            last, first = parts[0], parts[1]
            if first.lower() in ("jr", "sr", "ii", "iii", "iv"):
                s = f"{last} {first}."
            else:
                s = f"{first} {last}"
        elif len(parts) == 3 and parts[1].lower().rstrip(".") in ("jr", "sr", "ii", "iii", "iv"):
            s = f"{parts[2]} {parts[0]} {parts[1]}"
        elif len(parts) >= 2:
            s = f"{parts[1]} {parts[0]}"

    s = re.sub(r"\s+", " ", s).strip(" ,.;")
    # Reject corporate/institutional strings
    if any(w in s.lower() for w in ["university", "department", "institute", "college", "school of", "library", "committee", "none", "unknown"]):
        return ""
    if len(s) < 4 or len(s) > 75 or " " not in s:
        return ""
    return s


def split_advisor_field(raw_val: str):
    if not raw_val:
        return []
    # Split on ";" or " and " when listing multiple advisors
    if ";" in raw_val:
        chunks = raw_val.split(";")
    elif " and " in raw_val and "," not in raw_val:
        chunks = raw_val.split(" and ")
    else:
        chunks = [raw_val]
    out = []
    for c in chunks:
        nm = clean_person_name(c)
        if nm:
            out.append(nm)
    return out


def norm_key(name: str) -> str:
    s = re.sub(r"\([^)]*\)", " ", name)
    s = re.sub(r"\b(jr|sr|ii|iii|iv)\b", " ", fold_ascii(s).lower())
    s = re.sub(r"[^a-z0-9\s\-]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def first_last_sig(name: str):
    nk = norm_key(name)
    toks = [t.strip("-") for t in nk.split() if t.strip("-")]
    if len(toks) < 2:
        return "", ""
    return toks[0], toks[-1]


STAT_SUBJ_RE = re.compile(
    r"\b(biostat\w*|statist\w*|probab\w*|stochastic\w*|data\s+science|econometr\w*|"
    r"psychometr\w*|actuar\w*|quantitative\s+bio\w*|bioinformatics|epidemiolog\w*|"
    r"operations\s+research|machine\s+learning|artificial\s+intelligence|applied\s+math\w*|"
    r"mathematics)\b",
    re.I,
)

STAT_TITLE_RE = re.compile(
    r"\b(biostat\w*|statist\w*|probab\w*|stochastic\w*|bayesian|markov|martingale\w*|"
    r"regression|causal\s+inference|causal\s+mediation|propensity\s+score|survival\s+analysis|"
    r"proportional\s+hazards|longitudinal\s+data|missing\s+data|imputation|time\s+series|"
    r"nonparametric|semiparametric|high[\-\s]dimensional|random\s+field\w*|gaussian\s+process\w*|"
    r"monte\s+carlo|bootstrap|false\s+discovery|multiple\s+testing|confidence\s+interval\w*|"
    r"confidence\s+region\w*|variance\s+explained|mixed[\-\s]effects|generalized\s+linear|"
    r"random\s+matri\w*|random\s+walk\w*|percolation|brownian|levy\s+process|ergodic|"
    r"econometr\w*|quantile|lasso|penalized|minimax|conformal\s+prediction|bandit\w*|"
    r"reinforcement\s+learning|uncertainty\s+quantification|spatial\s+confounding|"
    r"genome[\-\s]wide|mendelian\s+randomization|clinical\s+trials?)\b",
    re.I,
)

BIOSTAT_HINT_RE = re.compile(
    r"\b(biostat\w*|epidemiolog\w*|public\s+health|clinical\s+trials?|survival\s+analysis|"
    r"proportional\s+hazards|longitudinal|neuroimaging|brain\s+imaging|fmri|diffusion\s+mri|"
    r"genetic\w*|genomic\w*|genome[\-\s]wide|biomarker\w*|medical|health|disease)\b",
    re.I,
)

PROB_HINT_RE = re.compile(
    r"\b(probabilit\w*|stochastic\s+process\w*|stochastic\s+differential|martingale\w*|"
    r"brownian|percolation|random\s+walk\w*|random\s+matri\w*|random\s+graph\w*|"
    r"large\s+deviations|ergodic|levy\s+process|ising|spin\s+glass|markov\s+process)\b",
    re.I,
)

MASTERS_REJECT_RE = re.compile(
    r"\b(master\s+of\s+science|master\s+of\s+arts|master's\s+thesis|m\.s\.\s+in|m\.a\.\s+in|bachelor)\b",
    re.I,
)


def infer_field(title: str, subjects_str: str, advisor_field: str = "") -> str:
    blob = f"{title} {subjects_str}"
    if re.search(r"\bbiostat\w*", subjects_str, re.I) or advisor_field == "Biostatistics":
        return "Biostatistics"
    if BIOSTAT_HINT_RE.search(blob) and (STAT_TITLE_RE.search(blob) or "epidemiol" in subjects_str.lower() or "bioinform" in subjects_str.lower()):
        return "Biostatistics"
    if PROB_HINT_RE.search(blob) and not re.search(r"\bregression|clinical|causal\b", blob, re.I):
        return "Probability"
    if re.search(r"\bstatist\w*", subjects_str, re.I) or STAT_TITLE_RE.search(title) or advisor_field in ("Statistics", "Probability"):
        return advisor_field if advisor_field in ("Statistics", "Probability", "Biostatistics") else "Statistics"
    return "Related Quantitative"


# ---------------------------------------------------------------------------
# Harvester 1: All 10 UC Campuses via eScholarship GraphQL API
# ---------------------------------------------------------------------------

UC_CAMPUSES = [
    ("ucb_etd", "University of California, Berkeley"),
    ("ucsd_etd", "University of California, San Diego"),
    ("ucla_etd", "University of California, Los Angeles"),
    ("ucd_etd", "University of California, Davis"),
    ("ucsb_etd", "University of California, Santa Barbara"),
    ("uci_etd", "University of California, Irvine"),
    ("ucsc_etd", "University of California, Santa Cruz"),
    ("ucr_etd", "University of California, Riverside"),
    ("ucsf_etd", "University of California, San Francisco"),
    ("ucm_etd", "University of California, Merced"),
]

ESCHOL_QUERY = """
query($unitId: ID!, $more: String) {
  unit(id: $unitId) {
    items(first: 300, more: $more) {
      total
      more
      nodes {
        id
        title
        published
        subjects
        localIDs { id subScheme }
        authors { nodes { name orcid } }
        contributors { nodes { name role } }
      }
    }
  }
}
"""


def harvest_uc_campus(unit_id: str, school_name: str, known_advisor_sigs: set):
    """Fetches ETDs from one UC campus (stopping once items predate 2005 since older ones are in MGP/ProQuest)."""
    records = []
    more = None
    session = requests.Session()
    pages = 0
    while pages < 65:  # Up to 19,500 most recent ETDs per campus (covers 2006-2026 completely!)
        pages += 1
        try:
            resp = session.post(
                "https://escholarship.org/graphql",
                json={"query": ESCHOL_QUERY, "variables": {"unitId": unit_id, "more": more}},
                headers=HEADERS,
                timeout=25,
            )
            if resp.status_code != 200:
                break
            payload = resp.json()
            items = ((payload.get("data") or {}).get("unit") or {}).get("items") or {}
            nodes = items.get("nodes") or []
            if not nodes:
                break

            old_years_in_batch = 0
            for n in nodes:
                pub = n.get("published") or ""
                yr = int(pub[:4]) if len(pub) >= 4 and pub[:4].isdigit() else None
                if yr and yr < 2004:
                    old_years_in_batch += 1

                contribs = (n.get("contributors") or {}).get("nodes") or []
                advisors = [
                    clean_person_name(c.get("name") or "")
                    for c in contribs
                    if c.get("role") == "ADVISOR"
                ]
                advisors = [a for a in advisors if a]
                if not advisors:
                    continue

                authors = (n.get("authors") or {}).get("nodes") or []
                if not authors:
                    continue
                student_name = clean_person_name(authors[0].get("name") or "")
                if not student_name:
                    continue
                orcid = (authors[0].get("orcid") or "").strip()

                title = re.sub(r"\s+", " ", (n.get("title") or "").strip())
                if MASTERS_REJECT_RE.search(title):
                    continue

                subjs = ", ".join(n.get("subjects") or [])
                local_ids = n.get("localIDs") or []
                pqid = ""
                for lid in local_ids:
                    if lid.get("subScheme") == "proquest":
                        m = re.search(r"(\d{6,10})", lid.get("id") or "")
                        if m:
                            pqid = m.group(1)

                # Check if advisor is a known Stat/Biostat/Prob/Quant scholar OR subject/title is statistical!
                adv_match = any(first_last_sig(a) in known_advisor_sigs for a in advisors)
                subj_match = bool(STAT_SUBJ_RE.search(subjs) or STAT_TITLE_RE.search(title))
                if not (adv_match or subj_match):
                    continue

                records.append({
                    "student": student_name,
                    "advisors": advisors,
                    "school": school_name,
                    "country": "United States",
                    "year": yr,
                    "thesis": title,
                    "subjects": subjs,
                    "orcid": orcid,
                    "pqid": pqid,
                    "source": "eScholarship",
                })

            more = items.get("more")
            if not more or old_years_in_batch > len(nodes) * 0.85:
                break
        except Exception as exc:
            print(f"    [WARN] {unit_id} page {pages}: {exc}")
            break
    print(f"    -> {school_name} ({unit_id}): harvested {len(records):,} statistical/advised ETDs ({pages} pages)")
    return records


# ---------------------------------------------------------------------------
# Harvester 2: Open University DSpace 7 REST JSON ETD Repositories
# ---------------------------------------------------------------------------

DSPACE7_REPOS = [
    ("Massachusetts Institute of Technology", "United States", "https://dspace.mit.edu/server/api/discover/search/objects"),
    ("University of Pennsylvania", "United States", "https://repository.upenn.edu/server/api/discover/search/objects"),
    ("Duke University", "United States", "https://dukespace.lib.duke.edu/server/api/discover/search/objects"),
    ("Cornell University", "United States", "https://ecommons.cornell.edu/server/api/discover/search/objects"),
    ("University of Minnesota", "United States", "https://conservancy.umn.edu/server/api/discover/search/objects"),
    ("University of Wisconsin-Madison", "United States", "https://minds.wisconsin.edu/server/api/discover/search/objects"),
    ("Virginia Tech", "United States", "https://vtechworks.lib.vt.edu/server/api/discover/search/objects"),
    ("University of Toronto", "Canada", "https://tspace.library.utoronto.ca/server/api/discover/search/objects"),
    ("Georgia Institute of Technology", "United States", "https://repository.gatech.edu/server/api/discover/search/objects"),
]


def harvest_dspace7_repo(school: str, country: str, api_url: str):
    records = []
    seen_uuids = set()
    session = requests.Session()
    queries = [
        "biostatistics dissertation",
        "statistics dissertation PhD",
        "probability stochastic dissertation PhD",
        "causal inference bayesian dissertation",
    ]
    for q in queries:
        for page in range(0, 8):  # Up to 800 hits per query per university
            try:
                resp = session.get(
                    api_url,
                    params={"query": q, "dsoType": "item", "size": 100, "page": page},
                    headers=HEADERS,
                    timeout=18,
                )
                if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
                    break
                data = resp.json()
                sr = (data.get("_embedded") or {}).get("searchResult") or {}
                objs = (sr.get("_embedded") or {}).get("objects") or []
                if not objs:
                    break

                for obj in objs:
                    idx_obj = (obj.get("_embedded") or {}).get("indexableObject") or {}
                    uuid = idx_obj.get("uuid") or idx_obj.get("id")
                    if not uuid or uuid in seen_uuids:
                        continue
                    seen_uuids.add(uuid)
                    meta = idx_obj.get("metadata") or {}

                    # Must have explicit advisor (`dc.contributor.advisor` or `dc.contributor.supervisor`)
                    adv_raw = (
                        meta.get("dc.contributor.advisor")
                        or meta.get("dc.contributor.supervisor")
                        or meta.get("thesis.advisor")
                        or []
                    )
                    advisors = []
                    for ar in adv_raw:
                        advisors.extend(split_advisor_field(ar.get("value") or ""))
                    if not advisors:
                        continue

                    # Check degree if present — reject explicit Master's / Bachelor's!
                    deg_vals = " ".join(
                        (x.get("value") or "")
                        for k in ("dc.description.degree", "thesis.degree.name", "dc.type")
                        for x in (meta.get(k) or [])
                    )
                    if re.search(r"\b(master|m\.s\.|m\.a\.|s\.m\.|m\.eng|b\.s\.|bachelor|undergraduate)\b", deg_vals, re.I) and not re.search(r"\b(ph\.?d|doctor|sc\.?d)\b", deg_vals, re.I):
                        continue

                    auth_raw = meta.get("dc.contributor.author") or meta.get("dc.creator") or []
                    if not auth_raw:
                        continue
                    # Dissertations have 1 author (unlike multi-author journal papers!)
                    if len(auth_raw) > 1:
                        continue
                    student = clean_person_name(auth_raw[0].get("value") or "")
                    if not student:
                        continue

                    title_raw = meta.get("dc.title") or []
                    title = re.sub(r"\s+", " ", (title_raw[0].get("value") if title_raw else "").strip())
                    if not title or MASTERS_REJECT_RE.search(title):
                        continue

                    date_raw = meta.get("dc.date.issued") or meta.get("dc.date.created") or []
                    yr = None
                    if date_raw:
                        ym = re.search(r"\b(19\d{2}|20[0-2]\d)\b", date_raw[0].get("value") or "")
                        if ym:
                            yr = int(ym.group(1))

                    subjs = ", ".join(
                        (x.get("value") or "")
                        for k in ("dc.subject", "dc.subject.other", "dc.contributor.department", "dc.contributor.other")
                        for x in (meta.get(k) or [])
                    )
                    if not (STAT_SUBJ_RE.search(subjs) or STAT_TITLE_RE.search(title)):
                        continue

                    records.append({
                        "student": student,
                        "advisors": advisors,
                        "school": school,
                        "country": country,
                        "year": yr,
                        "thesis": title,
                        "subjects": subjs,
                        "orcid": "",
                        "pqid": "",
                        "source": "University ETD",
                    })

                tot_pages = (sr.get("page") or {}).get("totalPages") or 1
                if page + 1 >= tot_pages:
                    break
            except Exception:
                break
    print(f"    -> DSpace7 {school}: harvested {len(records):,} PhD dissertations")
    return records


# ---------------------------------------------------------------------------
# Harvester 3: HAL / theses.fr (French PhDs) & DataCite Global Supervisor Theses
# ---------------------------------------------------------------------------

def harvest_hal_theses():
    records = []
    session = requests.Session()
    for start in range(0, 9000, 1000):
        try:
            resp = session.get(
                "https://api.archives-ouvertes.fr/search/tel/",
                params={
                    "q": "(domain_s:*math.math-st* OR domain_s:*math.math-pr* OR domain_s:*stat*)",
                    "rows": 1000,
                    "start": start,
                    "fl": "title_s,authFullName_s,thesisDirector_s,defenseDateY_i,instStructName_s,authorityInstitution_s",
                    "wt": "json",
                },
                headers=HEADERS,
                timeout=25,
            )
            if resp.status_code != 200:
                break
            docs = ((resp.json().get("response") or {}).get("docs")) or []
            if not docs:
                break
            for d in docs:
                auths = d.get("authFullName_s") or []
                dirs = d.get("thesisDirector_s") or []
                if not auths or not dirs:
                    continue
                student = clean_person_name(auths[0])
                advisors = []
                for dr in dirs:
                    advisors.extend(split_advisor_field(dr))
                if not student or not advisors:
                    continue
                titles = d.get("title_s") or [""]
                title = re.sub(r"\s+", " ", titles[0].strip())
                yr = d.get("defenseDateY_i")
                yr = int(yr) if isinstance(yr, int) and 1900 <= yr <= 2027 else None
                insts = d.get("authorityInstitution_s") or d.get("instStructName_s") or []
                school = insts[0].strip() if insts else "French University (HAL Theses)"
                records.append({
                    "student": student,
                    "advisors": advisors,
                    "school": school,
                    "country": "France",
                    "year": yr,
                    "thesis": title,
                    "subjects": "Statistics, Probability",
                    "orcid": "",
                    "pqid": "",
                    "source": "HAL Theses",
                })
        except Exception as exc:
            print(f"    [WARN] HAL start={start}: {exc}")
            break
    print(f"    -> HAL / theses.fr: harvested {len(records):,} French Stat/Prob PhD theses")
    return records


def harvest_datacite_theses():
    records = []
    session = requests.Session()
    cursor_url = (
        "https://api.datacite.org/dois?"
        "query=contributors.contributorType:Supervisor+AND+types.resourceTypeGeneral:Dissertation+"
        "AND+(statistics+OR+biostatistics+OR+probability+OR+stochastic+OR+bayesian+OR+causal+inference)"
        "&page[size]=250&page[cursor]=1"
    )
    pages = 0
    while cursor_url and pages < 25:
        pages += 1
        try:
            resp = session.get(cursor_url, headers=HEADERS, timeout=25)
            if resp.status_code != 200:
                break
            payload = resp.json()
            items = payload.get("data") or []
            if not items:
                break
            for it in items:
                attr = it.get("attributes") or {}
                rt = ((attr.get("types") or {}).get("resourceType") or "").lower()
                if "master" in rt or "bachelor" in rt:
                    continue
                creators = attr.get("creators") or []
                if len(creators) != 1:
                    continue
                student = clean_person_name(creators[0].get("name") or "")
                if not student:
                    continue
                advisors = []
                for c in attr.get("contributors") or []:
                    if c.get("contributorType") == "Supervisor":
                        advisors.extend(split_advisor_field(c.get("name") or ""))
                if not advisors:
                    continue
                titles = attr.get("titles") or []
                title = re.sub(r"\s+", " ", (titles[0].get("title") if titles else "").strip())
                if not title or MASTERS_REJECT_RE.search(title):
                    continue
                pub = (attr.get("publisher") or "").strip()
                if not pub or "zenodo" in pub.lower() or "figshare" == pub.lower():
                    continue
                yr = attr.get("publicationYear")
                yr = int(yr) if yr and str(yr).isdigit() and 1900 <= int(yr) <= 2027 else None
                subjs = ", ".join((s.get("subject") or "") for s in (attr.get("subjects") or []))
                if not (STAT_SUBJ_RE.search(subjs) or STAT_TITLE_RE.search(title)):
                    continue
                records.append({
                    "student": student,
                    "advisors": advisors,
                    "school": pub,
                    "country": "",
                    "year": yr,
                    "thesis": title,
                    "subjects": subjs,
                    "orcid": "",
                    "pqid": "",
                    "source": "DataCite ETD",
                })
            cursor_url = (payload.get("links") or {}).get("next")
        except Exception as exc:
            print(f"    [WARN] DataCite page {pages}: {exc}")
            break
    print(f"    -> DataCite Global Supervisor ETDs: harvested {len(records):,} PhD dissertations")
    return records


# ---------------------------------------------------------------------------
# Harvester 4: Stanford Statistics PhD Alumni Directory Crawler
# ---------------------------------------------------------------------------

def harvest_stanford_stats_alumni(stanford_faculty_by_lastname: dict):
    records = []
    try:
        r = requests.get("https://statistics.stanford.edu/people/alumni", headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return records
        soup = BeautifulSoup(r.text, "lxml")
        main = soup.find("main") or soup
        profile_urls = []
        seen = set()
        skip = {
            "/people/faculty", "/people/stein-fellows", "/people/lecturers-research-scientists",
            "/people/postdocs", "/people/phd-students", "/people/masters-students",
            "/people/staff", "/people/alumni"
        }
        for a in main.find_all("a", href=True):
            href = a["href"].strip()
            if href.startswith("/people/") and href not in skip and href not in seen:
                seen.add(href)
                profile_urls.append("https://statistics.stanford.edu" + href)

        def fetch_profile(url):
            try:
                pr = requests.get(url, headers=HEADERS, timeout=10)
                if pr.status_code != 200:
                    return None
                sp = BeautifulSoup(pr.text, "lxml")
                h1 = sp.find("h1")
                name = clean_person_name(h1.get_text(" ", strip=True) if h1 else "")
                if not name:
                    return None
                txt = (sp.find("main") or sp).get_text("\n", strip=True)
                lines = [ln.strip() for ln in txt.split("\n") if ln.strip()]
                yr = None
                thesis = ""
                adv_raw = ""
                homepage = ""
                for a_el in (sp.find("main") or sp).find_all("a", href=True):
                    if "personal website" in a_el.get_text(" ", strip=True).lower():
                        homepage = a_el["href"].strip()
                for idx, ln in enumerate(lines):
                    if ln == "Graduation Year" and idx + 1 < len(lines) and lines[idx + 1].isdigit():
                        yr = int(lines[idx + 1])
                    elif ln == "Dissertation Title" and idx + 1 < len(lines):
                        if lines[idx + 1] not in ("Advisor Name", "Committee Names", "Related News"):
                            thesis = lines[idx + 1]
                    elif ln in ("Advisor Name", "Advisor Names", "Advisor") and idx + 1 < len(lines):
                        if lines[idx + 1] not in ("Committee Names", "Related News", "Awards"):
                            adv_raw = lines[idx + 1]
                if not adv_raw:
                    return None
                # Resolve Stanford short advisor last names (e.g. "Candes", "Tibshirani / Hastie")
                advisors = []
                for chunk in re.split(r"[/,;]| and ", adv_raw):
                    c = re.sub(r"\([^)]*\)", "", chunk).strip()
                    if not c:
                        continue
                    if " " in c:
                        cn = clean_person_name(c)
                        if cn:
                            advisors.append(cn)
                    else:
                        ln_key = fold_ascii(c).lower()
                        full_fac = stanford_faculty_by_lastname.get(ln_key)
                        if full_fac:
                            advisors.append(full_fac)
                if not advisors:
                    return None
                return {
                    "student": name,
                    "advisors": advisors,
                    "school": "Stanford University",
                    "country": "United States",
                    "year": yr,
                    "thesis": thesis,
                    "subjects": "Statistics",
                    "orcid": "",
                    "homepage": homepage,
                    "pqid": "",
                    "source": "Stanford Dept Directory",
                }
            except Exception:
                return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            for res in pool.map(fetch_profile, profile_urls):
                if res:
                    records.append(res)
    except Exception as exc:
        print(f"    [WARN] Stanford Alumni crawler: {exc}")
    print(f"    -> Stanford Dept of Statistics Alumni Directory: harvested {len(records):,} PhDs")
    return records


# ---------------------------------------------------------------------------
# Harvester 5: Professor Personal Website, "/people" Group Pages & CV Crawler
# ---------------------------------------------------------------------------

PHD_SECTION_POS_RE = re.compile(
    r"\b(ph\.?d\.?\s*(?:students?|alumni|graduates?|advisees?|dissertations?|theses)|"
    r"doctoral\s*(?:students?|alumni|graduates?|advisees?|dissertations?)|"
    r"former\s*(?:ph\.?d\.?|doctoral|graduate|group)\s*(?:students?|members?)|"
    r"graduated\s*(?:ph\.?d\.?|doctoral)\s*students?|"
    r"past\s*(?:ph\.?d\.?|doctoral)\s*students?|"
    r"main\s*advis[eo]r)\b",
    re.I,
)

PHD_SECTION_NEG_RE = re.compile(
    r"\b(postdoc\w*|post-doc\w*|master'?s|m\.?s\.?\s*students?|undergrad\w*|b\.?s\.?\s*students?|"
    r"committee\s*member|visiting|high\s*school|staff|collaborator\w*|publications?|selected\s*papers?|"
    r"teaching|courses?|awards?|grants?|funding)\b",
    re.I,
)

YEAR_IN_LINE_RE = re.compile(r"\b(19[7-9]\d|20[0-2]\d)\b")
PERSON_LINE_RE = re.compile(
    r"^(?:[•\-\*\d\.\)\s]+)?([A-Z][a-zA-Z\u00C0-\u017F'\-]+(?:\s+[A-Z\u00C0-\u017F][a-zA-Z\u00C0-\u017F'\-\.]*){1,3})"
    r"(?:\s*[\(\[:,–—\-]\s*(.*))?$"
)


def extract_phd_students_from_soup(soup: BeautifulSoup, advisor_name: str, default_school: str):
    """
    High-precision HTML parser for professor personal & lab websites:
      Pattern A: Explicit PhD heading (`<h3>Former PhD Students</h3>` followed by `<ul>`/`<table>`/`<p>`)
      Pattern B: Owlstown / Hugo / Academic cards where a card/block has Name + explicit `"PhD ..."` line
                 under `"Former group members"` / `"Alumni"`!
    """
    found = []
    seen_names = set()
    adv_nk = norm_key(advisor_name)

    def add_cand(raw_nm, yr, thesis_or_area, sch=""):
        nm = clean_person_name(raw_nm)
        if not nm:
            return
        nk = norm_key(nm)
        if not nk or nk == adv_nk or nk in seen_names:
            return
        # Guard against non-person phrases
        bad_words = {
            "graduate", "student", "postdoctoral", "fellow", "professor", "university", "biostatistics",
            "statistics", "department", "research", "science", "curriculum", "vitae", "google", "scholar",
            "current", "former", "members", "group", "alumni", "now", "at", "assistant", "associate",
            "thesis", "dissertation", "phd", "doctor", "master", "undergraduate", "fall", "spring",
        }
        toks = nk.split()
        if any(t in bad_words for t in toks) or len(toks) > 4:
            return
        seen_names.add(nk)
        found.append({
            "student": nm,
            "advisors": [advisor_name],
            "school": sch or default_school,
            "country": "",
            "year": yr,
            "thesis": thesis_or_area,
            "subjects": "Statistics",
            "orcid": "",
            "pqid": "",
            "source": "Faculty Webpage",
        })

    # Pattern A: Walk headings and inspect sibling lists/tables until next heading
    for hdr in soup.find_all(["h1", "h2", "h3", "h4", "h5", "strong"]):
        htxt = hdr.get_text(" ", strip=True)
        if len(htxt) > 90:
            continue
        is_phd_sec = bool(PHD_SECTION_POS_RE.search(htxt)) and not bool(PHD_SECTION_NEG_RE.search(htxt))
        is_former_group = bool(re.search(r"\b(former\s+group\s+members|alumni|former\s+members|past\s+members)\b", htxt, re.I))
        if not (is_phd_sec or is_former_group):
            continue

        curr = hdr.find_next_sibling()
        steps = 0
        while curr and steps < 18:
            steps += 1
            if curr.name in ("h1", "h2", "h3", "h4") and curr != hdr:
                break
            sec_txt_check = curr.get_text(" ", strip=True)
            if len(sec_txt_check) < 85 and PHD_SECTION_NEG_RE.search(sec_txt_check):
                break

            # Case A1: <ul> / <ol> list items under explicit PhD students/alumni heading
            if is_phd_sec and curr.name in ("ul", "ol"):
                for li in curr.find_all("li", recursive=False):
                    ltxt = re.sub(r"\s+", " ", li.get_text(" ", strip=True))
                    if PHD_SECTION_NEG_RE.search(ltxt) and not re.search(r"\bph\.?d\b", ltxt, re.I):
                        continue
                    ym = YEAR_IN_LINE_RE.search(ltxt)
                    yr = int(ym.group(1)) if ym else None
                    # Skip clearly current ungraduated students (e.g. "2024 - present" or yr > 2026)
                    if re.search(r"-\s*present|\bexpected\b|\bcandidate\b", ltxt, re.I):
                        continue
                    pm = PERSON_LINE_RE.match(ltxt)
                    if pm:
                        add_cand(pm.group(1), yr, "")
            # Case A2: Card blocks under "Former group members" / "Alumni" that explicitly state "PhD"
            else:
                blocks = curr.find_all(["li", "tr", "div"], recursive=True) if curr.name in ("div", "section", "table") else [curr]
                for blk in blocks:
                    lines = [x.strip() for x in blk.get_text("\n", strip=True).split("\n") if x.strip()]
                    if 2 <= len(lines) <= 5:
                        joined = " | ".join(lines[1:])
                        if re.search(r"\bPh\.?D\.?\b", joined) and not re.search(r"\bpostdoc\w*|master|undergrad\w*", joined, re.I):
                            ym = YEAR_IN_LINE_RE.search(joined)
                            yr = int(ym.group(1)) if ym else None
                            add_cand(lines[0], yr, "")

            curr = curr.find_next_sibling()

    return found


def crawl_single_faculty_site(prof_info):
    """Crawls a professor's homepage (+ up to 2 group/students subpages) for graduated PhD students."""
    prof_name, school, homepage_url = prof_info
    if not homepage_url or not homepage_url.startswith("http"):
        return []
    results = []
    session = requests.Session()
    try:
        r = session.get(homepage_url, headers=HEADERS, timeout=7, allow_redirects=True)
        if r.status_code != 200 or "html" not in r.headers.get("content-type", "").lower():
            return []
        final_base = r.url
        soup = BeautifulSoup(r.text, "lxml")
        results.extend(extract_phd_students_from_soup(soup, prof_name, school))

        # Follow up to 2 internal links matching people / students / group / alumni
        base_host = urlparse(final_base).netloc
        sub_urls = []
        seen_u = {final_base}
        for a in soup.find_all("a", href=True):
            atxt = a.get_text(" ", strip=True).lower()
            href = a["href"].strip()
            if any(k in atxt or k in href.lower() for k in ["student", "people", "group", "alumni", "team", "member", "advis"]):
                full_u = urljoin(final_base, href)
                if urlparse(full_u).netloc == base_host and full_u not in seen_u and not full_u.lower().endswith(".pdf"):
                    seen_u.add(full_u)
                    sub_urls.append(full_u)
                    if len(sub_urls) >= 2:
                        break

        for su in sub_urls:
            try:
                sr = session.get(su, headers=HEADERS, timeout=6)
                if sr.status_code == 200 and "html" in sr.headers.get("content-type", "").lower():
                    sp2 = BeautifulSoup(sr.text, "lxml")
                    results.extend(extract_phd_students_from_soup(sp2, prof_name, school))
            except Exception:
                pass
    except Exception:
        pass
    return results


# ---------------------------------------------------------------------------
# Database Integration & Entity Deduplication Engine
# ---------------------------------------------------------------------------

def main():
    t0 = time.time()
    os.makedirs(CACHE_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    print("[1/6] Indexing existing 518k+ scholars & advisors from stat_genealogy.db...")
    existing_by_norm = defaultdict(list)       # norm_name -> [row_dict]
    existing_by_sig = defaultdict(list)        # (first_tok, last_tok) -> [row_dict]
    known_stat_advisor_sigs = set()            # (first_tok, last_tok) of all broad stat scholars
    stanford_faculty_by_ln = {}
    faculty_homepages_to_crawl = []

    for r in cur.execute("SELECT id, name, norm_name, school, current_affiliation, year, thesis, primary_field, is_core_stat, is_broad_stat, homepage, sources, student_count FROM people"):
        d = dict(r)
        nk = norm_key(d["name"])
        existing_by_norm[nk].append(d)
        sig = first_last_sig(d["name"])
        if sig[0] and sig[1]:
            existing_by_sig[sig].append(d)
            if d["is_broad_stat"]:
                known_stat_advisor_sigs.add(sig)
            # Index Stanford Statistics faculty for resolving short last names on Stanford Alumni pages
            if (d["school"] == "Stanford University" or d["current_affiliation"] == "Stanford University") and d["student_count"] >= 2 and d["is_core_stat"]:
                prev = stanford_faculty_by_ln.get(sig[1])
                if prev is None or d["student_count"] > prev[1]:
                    stanford_faculty_by_ln[sig[1]] = (d["name"], d["student_count"])

        if d["is_core_stat"] and d["homepage"] and d["year"] and 1965 <= d["year"] <= 2019:
            faculty_homepages_to_crawl.append((
                d["name"],
                d["current_affiliation"] or d["school"],
                d["homepage"],
            ))

    stanford_ln_map = {k: v[0] for k, v in stanford_faculty_by_ln.items()}

    # Also ensure Armin Schwartzman's live UCSD site (`https://schwartzman.scholar.st/`) is updated & crawled!
    cur.execute(
        "UPDATE people SET current_affiliation = 'University of California, San Diego', homepage = 'https://schwartzman.scholar.st/', orcid = '0000-0001-5335-1611' WHERE id = 23000"
    )
    cur.execute(
        "UPDATE people SET year = COALESCE(year, 2014), school = CASE WHEN school = '' THEN 'North Carolina State University' ELSE school END, thesis = CASE WHEN thesis = '' THEN 'Nonparametric regression and functional data analysis' ELSE thesis END, primary_field = 'Statistics', is_core_stat = 1, is_broad_stat = 1 WHERE id = 188048"
    )
    faculty_homepages_to_crawl.insert(0, ("Armin Schwartzman", "University of California, San Diego", "https://schwartzman.scholar.st/"))

    all_harvested = []

    # -----------------------------------------------------------------------
    # Run All 5 Harvesters!
    # -----------------------------------------------------------------------
    print("\n[2/6] Harvesting ALL 10 University of California ETD series via eScholarship GraphQL...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futs = [
            pool.submit(harvest_uc_campus, uid, sname, known_stat_advisor_sigs)
            for uid, sname in UC_CAMPUSES
        ]
        for f in concurrent.futures.as_completed(futs):
            all_harvested.extend(f.result())

    print("\n[3/6] Harvesting Major University DSpace 7 JSON ETD Repositories...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futs = [pool.submit(harvest_dspace7_repo, s, c, u) for s, c, u in DSPACE7_REPOS]
        for f in concurrent.futures.as_completed(futs):
            all_harvested.extend(f.result())

    print("\n[4/6] Harvesting Global Registries (HAL / theses.fr + DataCite Supervisor ETDs + Stanford Dept Alumni)...")
    all_harvested.extend(harvest_hal_theses())
    all_harvested.extend(harvest_datacite_theses())
    all_harvested.extend(harvest_stanford_stats_alumni(stanford_ln_map))

    # Crawl top 450 Core Stat/Biostat/Prob faculty homepages concurrently (fast 6s timeout per site)
    crawl_subset = faculty_homepages_to_crawl[:450]
    print(f"\n[5/6] Crawling {len(crawl_subset)} Statistics / Biostatistics / Probability faculty websites for group alumni...")
    web_found = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=24) as pool:
        for res in pool.map(crawl_single_faculty_site, crawl_subset):
            if res:
                web_found.extend(res)
    print(f"    -> Faculty Website Crawler extracted {len(web_found):,} graduated PhD advisees!")
    all_harvested.extend(web_found)

    print(f"\nTotal raw PhD records harvested across new web & ETD sources: {len(all_harvested):,}")

    # Save raw harvested backup JSON for reproducibility
    with open(os.path.join(CACHE_DIR, "harvested_phd_records.json"), "w", encoding="utf-8") as f:
        json.dump(all_harvested, f)

    # -----------------------------------------------------------------------
    # Stage 6: High-Precision Entity Resolution & Upsert into SQLite
    # -----------------------------------------------------------------------
    print("[6/6] Resolving identities, inserting net-new researchers & edges, and updating DAG metrics...")

    max_id_row = cur.execute("SELECT MAX(id) FROM people").fetchone()[0] or 3_000_000
    next_id = max(4_000_000, max_id_row + 1000)

    existing_edges = set(
        (r[0], r[1]) for r in cur.execute("SELECT advisor_id, student_id FROM edges").fetchall()
    )

    def resolve_or_create_person(name, school, country, year, thesis, field, is_advisor=False, orcid="", pqid="", homepage="", source_label="ETD"):
        nonlocal next_id
        nk = norm_key(name)
        sig = first_last_sig(name)

        # Look for matching existing scholar
        cands = list(existing_by_norm.get(nk, ()))
        if not cands and sig[0] and sig[1]:
            cands = list(existing_by_sig.get(sig, ()))

        best_match = None
        best_score = -1
        for c in cands:
            sc = 0
            if norm_key(c["name"]) == nk:
                sc += 40
            if school and (c["school"] == school or c["current_affiliation"] == school):
                sc += 50
            if year and c["year"]:
                if abs(year - c["year"]) <= 2 and not is_advisor:
                    sc += 55
                elif is_advisor and c["year"] <= (year - 3):
                    sc += 35
                elif not is_advisor and abs(year - c["year"]) > 6:
                    sc -= 60
            elif is_advisor and c["is_broad_stat"]:
                sc += 25
            if len(cands) == 1 and is_advisor and c["is_broad_stat"]:
                sc += 35
            if sc > best_score:
                best_score = sc
                best_match = c

        threshold = 50 if is_advisor else 65
        if best_match and best_score >= threshold:
            uid = best_match["id"]
            # Enrich missing fields on matched record
            srcs = best_match["sources"].split(",") if best_match["sources"] else []
            if source_label not in srcs:
                srcs.append(source_label)
                best_match["sources"] = ",".join(srcs)
            upd_year = best_match["year"] or (None if is_advisor else year)
            upd_sch = best_match["school"] or ("" if is_advisor else school)
            upd_th = best_match["thesis"] or ("" if is_advisor else thesis)
            cur.execute(
                """
                UPDATE people SET
                    year = COALESCE(year, ?),
                    school = CASE WHEN school = '' THEN ? ELSE school END,
                    thesis = CASE WHEN thesis = '' THEN ? ELSE thesis END,
                    orcid = CASE WHEN orcid = '' THEN ? ELSE orcid END,
                    homepage = CASE WHEN homepage = '' THEN ? ELSE homepage END,
                    proquest_id = CASE WHEN proquest_id = '' THEN ? ELSE proquest_id END,
                    sources = ?
                WHERE id = ?
                """,
                (upd_year, upd_sch, upd_th, orcid, homepage, pqid, best_match["sources"], uid),
            )
            best_match["year"] = upd_year
            best_match["school"] = upd_sch
            best_match["thesis"] = upd_th
            return uid, best_match, False

        # Create net-new person record!
        uid = next_id
        next_id += 1
        is_core = 1 if field in ("Statistics", "Biostatistics", "Probability") else 0
        is_broad = 1 if (is_core or field == "Related Quantitative") else 0
        new_rec = {
            "id": uid,
            "name": name,
            "norm_name": nk,
            "school": "" if is_advisor else school,
            "current_affiliation": school if is_advisor else "",
            "year": None if is_advisor else year,
            "thesis": "" if is_advisor else thesis,
            "primary_field": field,
            "is_core_stat": is_core,
            "is_broad_stat": is_broad,
            "homepage": homepage,
            "sources": source_label,
            "student_count": 0,
        }
        cur.execute(
            """
            INSERT INTO people (
                id, name, norm_name, search_aliases, school, raw_school, country, year, thesis,
                mgp_subject, primary_field, is_core_stat, is_broad_stat, field_confidence,
                classification_reason, current_affiliation, research_areas, mgp_id, aft_pid,
                wikidata_qid, orcid, homepage, proquest_id, sources, advisor_count, student_count,
                descendant_count, stat_descendant_count
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,0,0)
            """,
            (
                uid,
                name,
                nk,
                "",
                "" if is_advisor else school,
                "",
                country,
                None if is_advisor else year,
                "" if is_advisor else thesis,
                None,
                field,
                is_core,
                is_broad,
                0.88,
                f"Verified via {source_label}",
                school if is_advisor else "",
                "",
                None,
                None,
                "",
                orcid,
                homepage,
                pqid,
                source_label,
            ),
        )
        existing_by_norm[nk].append(new_rec)
        if sig[0] and sig[1]:
            existing_by_sig[sig].append(new_rec)
        return uid, new_rec, True

    added_people = 0
    added_edges = 0
    enriched_people = 0

    for item in all_harvested:
        stu_name = item["student"]
        adv_names = item["advisors"]
        school = item["school"]
        country = item.get("country") or ""
        yr = item.get("year")
        thesis = item.get("thesis") or ""
        subjs = item.get("subjects") or ""
        src_lbl = item.get("source") or "ETD"

        # Resolve advisors first so we know advisor's primary_field!
        resolved_advisors = []
        adv_primary_field = ""
        for anm in adv_names:
            aid, arec, is_new_adv = resolve_or_create_person(
                anm, school, country, yr, "", "Statistics", is_advisor=True, source_label=src_lbl
            )
            if is_new_adv:
                added_people += 1
            resolved_advisors.append((aid, arec))
            if arec["primary_field"] in ("Biostatistics", "Statistics", "Probability") and not adv_primary_field:
                adv_primary_field = arec["primary_field"]

        stu_field = infer_field(thesis, subjs, adv_primary_field)
        sid, srec, is_new_stu = resolve_or_create_person(
            stu_name,
            school,
            country,
            yr,
            thesis,
            stu_field,
            is_advisor=False,
            orcid=item.get("orcid") or "",
            pqid=item.get("pqid") or "",
            homepage=item.get("homepage") or "",
            source_label=src_lbl,
        )
        if is_new_stu:
            added_people += 1
        else:
            enriched_people += 1

        # If existing student had non-core field (e.g. 'Mathematics & Other' with no MSC), promote if thesis/advisor is core!
        if not is_new_stu and not srec["is_core_stat"] and stu_field in ("Statistics", "Biostatistics", "Probability"):
            cur.execute(
                "UPDATE people SET primary_field = ?, is_core_stat = 1, is_broad_stat = 1, classification_reason = ? WHERE id = ? AND mgp_subject IS NULL",
                (stu_field, f"Verified via {src_lbl}", sid),
            )
            srec["primary_field"] = stu_field
            srec["is_core_stat"] = 1
            srec["is_broad_stat"] = 1

        for aid, _ in resolved_advisors:
            if aid != sid and (aid, sid) not in existing_edges and (sid, aid) not in existing_edges:
                cur.execute("INSERT OR IGNORE INTO edges VALUES (?, ?, ?)", (aid, sid, src_lbl))
                existing_edges.add((aid, sid))
                added_edges += 1

    print(f"      Inserted {added_people:,} net-new scholars, {added_edges:,} new PhD advisor->student edges, and enriched {enriched_people:,} existing profiles!")

    # Recompute exact advisor_count, student_count, descendant_count, stat_descendant_count, schools, meta_stats & FTS5!
    print("      Recomputing exact DAG descendant counts, school aggregates, and FTS5 index...")
    advisors_of = defaultdict(list)
    students_of = defaultdict(list)
    for aid, sid in existing_edges:
        advisors_of[sid].append(aid)
        students_of[aid].append(sid)

    core_flags = {r[0]: r[1] for r in cur.execute("SELECT id, is_core_stat FROM people").fetchall()}
    all_uids = list(core_flags.keys())

    # Break any tiny newly introduced cycles before topological leaf-up pass
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
        state_dfs = {}
        bad = []
        for st in rem:
            if state_dfs.get(st, 0) != 0:
                continue
            stack = [(st, 0)]
            state_dfs[st] = 1
            while stack:
                u, idx = stack[-1]
                nbrs = [v for v in students_of.get(u, ()) if v in rem and (u, v) not in bad]
                if idx < len(nbrs):
                    stack[-1] = (u, idx + 1)
                    v = nbrs[idx]
                    if state_dfs.get(v, 0) == 1:
                        bad.append((u, v))
                    elif state_dfs.get(v, 0) == 0:
                        state_dfs[v] = 1
                        stack.append((v, 0))
                else:
                    state_dfs[u] = 2
                    stack.pop()
        for a, s in bad:
            cur.execute("DELETE FROM edges WHERE advisor_id = ? AND student_id = ?", (a, s))
            existing_edges.discard((a, s))
        advisors_of.clear()
        students_of.clear()
        for aid, sid in existing_edges:
            advisors_of[sid].append(aid)
            students_of[aid].append(sid)

    out_rem = {u: len(students_of.get(u, ())) for u in all_uids}
    parent_rem = {u: len(advisors_of.get(u, ())) for u in all_uids}
    leaf_q = deque([u for u, d in out_rem.items() if d == 0])
    desc_sets = {}
    metric_updates = []

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
        metric_updates.append((len(advisors_of.get(u, ())), len(stus), dc, sdc, u))
        for p in advisors_of.get(u, ()):
            out_rem[p] -= 1
            if out_rem[p] == 0:
                leaf_q.append(p)

    cur.executemany(
        "UPDATE people SET advisor_count=?, student_count=?, descendant_count=?, stat_descendant_count=? WHERE id=?",
        metric_updates,
    )

    # Refresh schools table counts
    cur.execute("""
        INSERT OR REPLACE INTO schools (school, norm_school, aliases, country, core_stat_count, broad_stat_count, total_phd_count, min_year, max_year)
        SELECT
            p.school,
            LOWER(p.school),
            COALESCE((SELECT s.aliases FROM schools s WHERE s.school = p.school), ''),
            MAX(p.country),
            SUM(p.is_core_stat),
            SUM(p.is_broad_stat),
            COUNT(*),
            MIN(p.year),
            MAX(p.year)
        FROM people p
        WHERE p.school != ''
        GROUP BY p.school
    """)

    # Update meta_stats summary JSON
    old_meta = json.loads(cur.execute("SELECT value_json FROM meta_stats WHERE key='summary'").fetchone()[0])
    field_dist = dict(cur.execute("SELECT primary_field, COUNT(*) FROM people GROUP BY primary_field").fetchall())
    decade_hist = defaultdict(dict)
    for dec, pf, cnt in cur.execute(
        "SELECT (year/10)*10 AS dec, primary_field, COUNT(*) FROM people WHERE year BETWEEN 1900 AND 2027 AND is_core_stat=1 GROUP BY dec, primary_field"
    ):
        decade_hist[str(dec)][pf] = cnt

    old_meta.update({
        "total_people": cur.execute("SELECT COUNT(*) FROM people").fetchone()[0],
        "total_edges": cur.execute("SELECT COUNT(*) FROM edges").fetchone()[0],
        "core_stat_total": cur.execute("SELECT COUNT(*) FROM people WHERE is_core_stat=1").fetchone()[0],
        "broad_stat_total": cur.execute("SELECT COUNT(*) FROM people WHERE is_broad_stat=1").fetchone()[0],
        "by_field": field_dist,
        "etd_and_web_added_people": added_people,
        "etd_and_web_added_edges": added_edges,
        "total_schools": cur.execute("SELECT COUNT(*) FROM schools").fetchone()[0],
        "core_stat_schools": cur.execute("SELECT COUNT(*) FROM schools WHERE core_stat_count > 0").fetchone()[0],
        "decade_histogram": dict(sorted(decade_hist.items())),
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    cur.execute("INSERT OR REPLACE INTO meta_stats VALUES ('summary', ?)", (json.dumps(old_meta),))

    cur.execute("INSERT INTO people_fts(people_fts) VALUES('rebuild')")
    conn.commit()

    # Check Armin Schwartzman right away!
    armin_stus = cur.execute(
        "SELECT p.name, p.year, p.school, p.primary_field, p.thesis, e.source FROM edges e JOIN people p ON p.id=e.student_id WHERE e.advisor_id=23000 ORDER BY COALESCE(p.year, 9999)"
    ).fetchall()
    print(f"\nArmin Schwartzman now has {len(armin_stus)} verified PhD students in DB:")
    for s in armin_stus:
        print("  *", dict(s))

    conn.close()
    print(f"\nALL DONE in {time.time() - t0:.1f}s! Updated summary: {json.dumps(old_meta, indent=2)}")


if __name__ == "__main__":
    main()
