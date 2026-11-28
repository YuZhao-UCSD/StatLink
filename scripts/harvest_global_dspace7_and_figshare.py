#!/usr/bin/env python3
"""
Wave 1A Harvester: Global DSpace 7 University Repositories (34 universities),
Figshare Institutional Doctoral Dissertations (CMU KiltHub, Purdue Hammer, Monash, Global),
Brown Digital Repository JSON API, and UC eScholarship 2024–2026 Doctoral Refresh.

Dynamically handles every university's advisor and discipline metadata schema
(including Cornell's `dc.contributor.chair` + `thesis.degree.discipline`,
Georgia Tech's `relation.isAdvisorOfPublication`, Boston U's `etd.degree.*`,
UPenn's `upenn.schoolDepartmentCenter`, NTU's `dc.contributor.supervisor2`, etc.).
"""

import concurrent.futures
import json
import os
import re
import socket
import sys
import time
import urllib3

import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
socket.setdefaulttimeout(15.0)
sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "scripts"))

from etd_ingest_common import (
    CACHE_DIR,
    HEADERS,
    StatLinkIngestor,
    clean_person_name,
    clean_title,
    classify_stat_field,
    is_doctoral_degree,
    split_advisor_field,
)

CACHE_FILE = os.path.join(CACHE_DIR, "wave1a_dspace7_figshare_records.json")

DSPACE7_REPOS = [
    ("Cornell University", "United States", "https://ecommons.cornell.edu/server/api/discover/search/objects"),
    ("Massachusetts Institute of Technology", "United States", "https://dspace.mit.edu/server/api/discover/search/objects"),
    ("University of Pennsylvania", "United States", "https://repository.upenn.edu/server/api/discover/search/objects"),
    ("Duke University", "United States", "https://dukespace.lib.duke.edu/server/api/discover/search/objects"),
    ("University of Minnesota", "United States", "https://conservancy.umn.edu/server/api/discover/search/objects"),
    ("University of Wisconsin-Madison", "United States", "https://minds.wisconsin.edu/server/api/discover/search/objects"),
    ("Virginia Tech", "United States", "https://vtechworks.lib.vt.edu/server/api/discover/search/objects"),
    ("Georgia Institute of Technology", "United States", "https://repository.gatech.edu/server/api/discover/search/objects"),
    ("University of Washington", "United States", "https://digital.lib.washington.edu/server/api/discover/search/objects"),
    ("Iowa State University", "United States", "https://dr.lib.iastate.edu/server/api/discover/search/objects"),
    ("Boston University", "United States", "https://open.bu.edu/server/api/discover/search/objects"),
    ("Ohio State University", "United States", "https://kb.osu.edu/server/api/discover/search/objects"),
    ("University of Arizona", "United States", "https://repository.arizona.edu/server/api/discover/search/objects"),
    ("University of Delaware", "United States", "https://udspace.udel.edu/server/api/discover/search/objects"),
    ("University of Oklahoma", "United States", "https://shareok.org/server/api/discover/search/objects"),
    ("University of Kansas", "United States", "https://kuscholarworks.ku.edu/server/api/discover/search/objects"),
    ("University of Toronto", "Canada", "https://tspace.library.utoronto.ca/server/api/discover/search/objects"),
    ("McMaster University", "Canada", "https://macsphere.mcmaster.ca/server/api/discover/search/objects"),
    ("Queen's University", "Canada", "https://qspace.library.queensu.ca/server/api/discover/search/objects"),
    ("York University", "Canada", "https://yorkspace.library.yorku.ca/server/api/discover/search/objects"),
    ("Dalhousie University", "Canada", "https://dalspace.library.dal.ca/server/api/discover/search/objects"),
    ("University of Calgary", "Canada", "https://prism.ucalgary.ca/server/api/discover/search/objects"),
    ("University of Ottawa", "Canada", "https://ruor.uottawa.ca/server/api/discover/search/objects"),
    ("University of Manitoba", "Canada", "https://mspace.lib.umanitoba.ca/server/api/discover/search/objects"),
    ("University of Victoria", "Canada", "https://dspace.library.uvic.ca/server/api/discover/search/objects"),
    ("Imperial College London", "United Kingdom", "https://spiral.imperial.ac.uk/server/api/discover/search/objects"),
    ("University of Edinburgh", "United Kingdom", "https://era.ed.ac.uk/server/api/discover/search/objects"),
    ("École Polytechnique Fédérale de Lausanne", "Switzerland", "https://infoscience.epfl.ch/server/api/discover/search/objects"),
    ("University of Helsinki", "Finland", "https://helda.helsinki.fi/server/api/discover/search/objects"),
    ("Aalto University", "Finland", "https://aaltodoc.aalto.fi/server/api/discover/search/objects"),
    ("Utrecht University", "Netherlands", "https://studenttheses.uu.nl/server/api/discover/search/objects"),
    ("Nanyang Technological University", "Singapore", "https://dr.ntu.edu.sg/server/api/discover/search/objects"),
    ("Australian National University", "Australia", "https://openresearch-repository.anu.edu.au/server/api/discover/search/objects"),
    ("University of Melbourne", "Australia", "https://minerva-access.unimelb.edu.au/server/api/discover/search/objects"),
]

# Dynamic advisor & degree metadata key matchers (never include committeeMember/reader/examiner!)
ADVISOR_KEY_RE = re.compile(
    r"(?:advisor|adviser|supervisor|chair|cochair|co-chair|promoter|director|mentor)",
    re.I,
)
ADVISOR_KEY_EXCLUDE_RE = re.compile(
    r"(?:email|orcid|affiliation|committeemember|committee_member|reader|examiner|opponent|reviewer|referee|latestfordiscovery)",
    re.I,
)
DEGREE_KEY_RE = re.compile(r"(?:degree|level|type|genre)", re.I)
DEPT_SUBJ_KEY_RE = re.compile(r"(?:subject|discipline|department|school|program|major|faculty|unit|center)", re.I)

DSPACE_QUERIES = [
    "Statistics",
    "Biostatistics",
    "Statistical Science",
    "Probability stochastic",
    "Bayesian causal inference",
    "Survival analysis longitudinal",
    "High-dimensional regression",
    "Time series econometrics",
]


def harvest_one_dspace7(item):
    school, country, api_url = item
    records = []
    seen_uuids = set()
    sess = requests.Session()
    sess.headers.update(HEADERS)
    sess.headers["Accept"] = "application/json"
    t_repo = time.time()
    page_sz = 50 if "iastate.edu" in api_url else 100

    for q in DSPACE_QUERIES:
        if (time.time() - t_repo) > 95:
            break
        max_pages = 15 if q in ("Statistics", "Biostatistics", "Statistical Science") else 6
        for page in range(0, max_pages):
            if (time.time() - t_repo) > 95:
                break
            try:
                resp = sess.get(
                    api_url,
                    params={"query": q, "dsoType": "item", "size": page_sz, "page": page},
                    timeout=(8, 12),
                    verify=False,
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

                    # 1. Extract explicit advisors (dynamically across all schema dialects)
                    advisors = []
                    for k, vals in meta.items():
                        if ADVISOR_KEY_RE.search(k) and not ADVISOR_KEY_EXCLUDE_RE.search(k):
                            for v in vals or []:
                                val = (v.get("value") or "").strip()
                                if val and not re.match(r"^[0-9a-f\-]{20,}$", val, re.I):
                                    advisors.extend(split_advisor_field(val))
                    if not advisors:
                        continue

                    # 2. Extract author (dissertations have 1 single author!)
                    auth_raw = meta.get("dc.contributor.author") or meta.get("dc.creator") or []
                    if not auth_raw or len(auth_raw) > 1:
                        continue
                    student = clean_person_name(auth_raw[0].get("value") or "")
                    if not student:
                        continue

                    # 3. Degree & thesis check (strictly doctoral!)
                    deg_vals = []
                    for k, vals in meta.items():
                        if DEGREE_KEY_RE.search(k):
                            for v in vals or []:
                                if v.get("value"):
                                    deg_vals.append(str(v["value"]))
                    deg_str = " | ".join(deg_vals)
                    # Must look like a thesis/dissertation if dc.type exists (reject journal articles/reports/datasets)
                    type_str = " ".join(str(v.get("value") or "") for v in (meta.get("dc.type") or []))
                    if type_str and not re.search(r"\b(thesis|dissertation|doctoral|ph\.?d|etd|book|text)\b", type_str + " " + deg_str, re.I):
                        if re.search(r"\b(article|dataset|poster|presentation|conference|working\s+paper|technical\s+report|image|video|audio|software|periodical)\b", type_str, re.I):
                            continue
                    title_raw = meta.get("dc.title") or []
                    title = clean_title(title_raw[0].get("value") if title_raw else "")
                    if not title:
                        continue
                    if not is_doctoral_degree(deg_str, title, require_positive=False):
                        continue

                    # 4. Graduation year
                    yr = None
                    for dk in ("dc.date.issued", "dc.date.created", "dc.date.submitted", "dc.date.awarded", "dc.date"):
                        dvals = meta.get(dk) or []
                        if dvals:
                            m = re.search(r"\b(19\d{2}|20[0-2]\d)\b", str(dvals[0].get("value") or ""))
                            if m:
                                yr = int(m.group(1))
                                break

                    # 5. Department / discipline / subjects
                    subj_parts = []
                    for k, vals in meta.items():
                        if DEPT_SUBJ_KEY_RE.search(k):
                            for v in vals or []:
                                if v.get("value"):
                                    subj_parts.append(str(v["value"]))
                    subjs = ", ".join(subj_parts)
                    if not classify_stat_field(subjs, title, advisor_is_core=False):
                        continue

                    # Check ORCID if present
                    orcid = ""
                    for k, vals in meta.items():
                        if "orcid" in k.lower():
                            for v in vals or []:
                                om = re.search(r"\b(\d{4}-\d{4}-\d{4}-\d{3}[\dX])\b", str(v.get("value") or ""))
                                if om:
                                    orcid = om.group(1)
                                    break

                    records.append({
                        "student": student,
                        "advisors": advisors,
                        "school": school,
                        "country": country,
                        "year": yr,
                        "thesis": title,
                        "subjects": subjs,
                        "degree": deg_str,
                        "orcid": orcid,
                        "source": "University ETD",
                    })

                tot_pages = (sr.get("page") or {}).get("totalPages") or 1
                if page + 1 >= tot_pages:
                    break
            except Exception:
                break

    print(f"  [DSpace7] {school:42s}: {len(records):4d} doctoral stat/biostat/prob ETDs")
    return records


# ---------------------------------------------------------------------------
# Figshare Institutional Doctoral Dissertations (CMU KiltHub, Purdue Hammer, Monash, Global)
# ---------------------------------------------------------------------------

FIGSHARE_INSTITUTIONS = [
    ("Carnegie Mellon University", "United States", 18614, ["Statistics and Data Science", "Statistics", "Biostatistics", "Probability", "Machine Learning", "Bayesian", "Causal Inference", "High-Dimensional"]),
    ("Purdue University", "United States", 19657, ["Statistics", "Biostatistics", "Probability", "Statistical", "Bayesian", "Causal", "Survival", "Regression"]),
]

FIGSHARE_GLOBAL_QUERIES = [
    "Statistics PhD dissertation",
    "Biostatistics PhD dissertation",
    "Statistical Science dissertation",
    "Probability stochastic dissertation",
    "Bayesian inference PhD thesis",
    "Causal inference PhD dissertation",
    "Survival analysis PhD thesis",
    "High-dimensional statistics dissertation",
]

FIGSHARE_INST_MAP = {
    18614: ("Carnegie Mellon University", "United States"),
    19657: ("Purdue University", "United States"),
}


def harvest_figshare_theses():
    records = []
    sess = requests.Session()
    sess.headers.update(HEADERS)
    candidate_ids = {}  # article_id -> (school_hint, country_hint)

    # 1. Targeted CMU KiltHub & Purdue Hammer sweeps
    for school, country, group_id, queries in FIGSHARE_INSTITUTIONS:
        for q in queries:
            for page in range(1, 12):
                try:
                    r = sess.post(
                        "https://api.figshare.com/v2/articles/search",
                        json={"search_for": q, "item_type": 8, "group": group_id, "page": page, "page_size": 100},
                        timeout=15,
                    )
                    if r.status_code != 200:
                        break
                    arr = r.json()
                    if not isinstance(arr, list) or not arr:
                        break
                    for it in arr:
                        aid = it.get("id")
                        if aid and aid not in candidate_ids:
                            candidate_ids[aid] = (school, country)
                    if len(arr) < 100:
                        break
                except Exception:
                    break

    # 2. Global Figshare item_type=8 thesis sweep (catches Monash, Loughborough, Melbourne, Auckland, UIC, GMU, etc.)
    for q in FIGSHARE_GLOBAL_QUERIES:
        for page in range(1, 8):
            try:
                r = sess.post(
                    "https://api.figshare.com/v2/articles/search",
                    json={"search_for": q, "item_type": 8, "page": page, "page_size": 100},
                    timeout=15,
                )
                if r.status_code != 200:
                    break
                arr = r.json()
                if not isinstance(arr, list) or not arr:
                    break
                for it in arr:
                    aid = it.get("id")
                    if aid and aid not in candidate_ids:
                        candidate_ids[aid] = ("", "")
                if len(arr) < 100:
                    break
            except Exception:
                break

    print(f"  [Figshare] Fetching detailed metadata for {len(candidate_ids):,} candidate theses...")

    def fetch_article(pair):
        aid, (sch_hint, cntry_hint) = pair
        try:
            r = sess.get(f"https://api.figshare.com/v2/articles/{aid}", timeout=12)
            if r.status_code != 200:
                return None
            d = r.json()
            title = clean_title(d.get("title") or "")
            if not title:
                return None
            authors = d.get("authors") or []
            if not authors:
                return None
            student = clean_person_name(authors[0].get("full_name") or "")
            if not student:
                return None
            orcid = (authors[0].get("orcid_id") or "").strip()

            cfields = {}
            for cf in d.get("custom_fields") or []:
                cname = (cf.get("name") or "").strip()
                cval = cf.get("value")
                if isinstance(cval, list):
                    cval = ", ".join(str(x) for x in cval if x)
                else:
                    cval = str(cval or "").strip()
                if cname and cval:
                    cfields[cname.lower()] = cval

            # Extract advisors strictly from advisor/supervisor/chair custom fields (NEVER committee members!)
            advisors = []
            for k, v in cfields.items():
                if any(w in k for w in ("advisor", "adviser", "supervisor", "chair")) and "committee member" not in k and "additional" not in k:
                    advisors.extend(split_advisor_field(v))
            if not advisors:
                return None

            # Degree check
            deg_str = " | ".join(
                v for k, v in cfields.items() if any(w in k for w in ("degree", "qualification", "award"))
            )
            if not is_doctoral_degree(deg_str, title, require_positive=False):
                return None

            # School resolution
            school = sch_hint
            country = cntry_hint
            if not school:
                gid = d.get("group_id")
                if gid in FIGSHARE_INST_MAP:
                    school, country = FIGSHARE_INST_MAP[gid]
                else:
                    for k, v in cfields.items():
                        if any(w in k for w in ("institution", "university", "grantor", "publisher")):
                            if len(v) < 120:
                                school = v
                                break
                    if not school:
                        url_pub = (d.get("url_public_html") or "").lower()
                        if "kilthub.cmu.edu" in url_pub:
                            school, country = "Carnegie Mellon University", "United States"
                        elif "hammer.purdue.edu" in url_pub:
                            school, country = "Purdue University", "United States"
                        elif "bridges.monash.edu" in url_pub:
                            school, country = "Monash University", "Australia"
                        elif "melbourne.figshare.com" in url_pub:
                            school, country = "University of Melbourne", "Australia"
                        elif "auckland.figshare.com" in url_pub:
                            school, country = "University of Auckland", "New Zealand"
                        elif "sheffield.figshare.com" in url_pub:
                            school, country = "University of Sheffield", "United Kingdom"
                        elif "lboro.ac.uk" in url_pub:
                            school, country = "Loughborough University", "United Kingdom"
                        elif "indigo.uic.edu" in url_pub:
                            school, country = "University of Illinois Chicago", "United States"
            if not school:
                return None

            # Year
            yr = None
            for cand_dt in (cfields.get("date", ""), d.get("published_date") or "", (d.get("timeline") or {}).get("firstOnline") or ""):
                m = re.search(r"\b(19\d{2}|20[0-2]\d)\b", str(cand_dt))
                if m:
                    yr = int(m.group(1))
                    break

            # Department / subjects
            dept_parts = [v for k, v in cfields.items() if any(w in k for w in ("department", "school", "faculty", "discipline", "program", "subject"))]
            cats = [c.get("title") or "" for c in (d.get("categories") or [])]
            subjs = ", ".join(dept_parts + cats)
            if not classify_stat_field(subjs, title, advisor_is_core=False):
                return None

            return {
                "student": student,
                "advisors": advisors,
                "school": school,
                "country": country,
                "year": yr,
                "thesis": title,
                "subjects": subjs,
                "degree": deg_str,
                "orcid": orcid,
                "source": "Figshare ETD",
            }
        except Exception:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        for res in pool.map(fetch_article, candidate_ids.items()):
            if res:
                records.append(res)

    print(f"  [Figshare] Harvested {len(records):,} verified doctoral stat/biostat/prob ETDs")
    return records


# ---------------------------------------------------------------------------
# Brown Digital Repository JSON API
# ---------------------------------------------------------------------------

def harvest_brown_bdr():
    records = []
    sess = requests.Session()
    sess.headers.update(HEADERS)
    seen_pids = set()
    for q in ("Biostatistics", "Statistics", "Probability", "Applied Mathematics"):
        for start in range(0, 400, 100):
            try:
                r = sess.get(
                    "https://repository.library.brown.edu/api/search/",
                    params={"q": f'"{q}" AND object_type:thesis', "rows": 100, "start": start},
                    timeout=15,
                )
                if r.status_code != 200:
                    break
                docs = (r.json().get("response") or {}).get("docs") or []
                if not docs:
                    break
                for doc in docs:
                    pid = doc.get("pid")
                    if not pid or pid in seen_pids:
                        continue
                    seen_pids.add(pid)
                    title = clean_title((doc.get("primary_title") or (doc.get("title") or [""])[0]))
                    authors = doc.get("name_author") or doc.get("contributor_author") or doc.get("creator") or []
                    if not authors or len(authors) > 1:
                        continue
                    student = clean_person_name(authors[0])
                    if not student:
                        continue
                    adv_raw = doc.get("name_advisor") or doc.get("contributor_advisor") or []
                    advisors = []
                    for a in adv_raw:
                        advisors.extend(split_advisor_field(a))
                    if not advisors:
                        continue
                    deg = " ".join(doc.get("mods_genre_aat_ssim") or doc.get("genre") or [])
                    if not is_doctoral_degree(deg, title, require_positive=False):
                        continue
                    yr = None
                    dt = doc.get("dateIssued_year_ssim") or doc.get("dateIssued") or ""
                    m = re.search(r"\b(19\d{2}|20[0-2]\d)\b", str(dt))
                    if m:
                        yr = int(m.group(1))
                    subjs = ", ".join((doc.get("department") or []) + (doc.get("subject") or []) + [q])
                    if not classify_stat_field(subjs, title, advisor_is_core=False):
                        continue
                    records.append({
                        "student": student,
                        "advisors": advisors,
                        "school": "Brown University",
                        "country": "United States",
                        "year": yr,
                        "thesis": title,
                        "subjects": subjs,
                        "degree": deg,
                        "source": "University ETD",
                    })
                if len(docs) < 100:
                    break
            except Exception:
                break
    print(f"  [Brown BDR] Harvested {len(records):,} verified doctoral stat/biostat/prob ETDs")
    return records


# ---------------------------------------------------------------------------
# UC eScholarship 2024–2026 Refresh (All 10 UC campuses, strict _XXXXD_ doctoral filter)
# ---------------------------------------------------------------------------

UC_CAMPUSES = {
    "ucb": "University of California, Berkeley",
    "ucla": "University of California, Los Angeles",
    "ucsd": "University of California, San Diego",
    "ucd": "University of California, Davis",
    "ucsb": "University of California, Santa Barbara",
    "uci": "University of California, Irvine",
    "ucsc": "University of California, Santa Cruz",
    "ucr": "University of California, Riverside",
    "ucsf": "University of California, San Francisco",
    "ucm": "University of California, Merced",
}


def harvest_uc_escholarship_recent():
    records = []
    sess = requests.Session()
    sess.headers.update(HEADERS)
    seen = set()
    gql = """
    query($unit: ID!, $after: String) {
      unit(id: $unit) {
        items(first: 100, after: $after, include: [ETD]) {
          pageInfo { hasNextPage endCursor }
          nodes {
            id title published subjects disciplines nativeFileName
            authors { nodes { name orcid } }
            advisors { nodes { name } }
          }
        }
      }
    }
    """
    for code, school in UC_CAMPUSES.items():
        after = None
        campus_cnt = 0
        for _ in range(15):  # Most recent ~1,500 ETDs per campus (covers 2024-2026)
            try:
                r = sess.post(
                    "https://escholarship.org/graphql",
                    json={"query": gql, "variables": {"unit": f"{code}_etd", "after": after}},
                    timeout=20,
                )
                if r.status_code != 200:
                    break
                items = (((r.json().get("data") or {}).get("unit") or {}).get("items")) or {}
                nodes = items.get("nodes") or []
                if not nodes:
                    break
                stop_early = False
                for nd in nodes:
                    nid = nd.get("id")
                    if not nid or nid in seen:
                        continue
                    seen.add(nid)
                    pub = nd.get("published") or ""
                    ym = re.search(r"\b(20\d{2})\b", pub)
                    yr = int(ym.group(1)) if ym else None
                    if yr and yr < 2023:
                        stop_early = True
                        continue
                    # STRICT UC ProQuest doctoral degree code check (_XXXXD_ vs _XXXXN_/_XXXXM_ Master's!)
                    nfn = (nd.get("nativeFileName") or "")
                    if nfn and re.search(r"_\d{4}[NM]_", nfn):
                        continue
                    auths = ((nd.get("authors") or {}).get("nodes")) or []
                    advs = ((nd.get("advisors") or {}).get("nodes")) or []
                    if len(auths) != 1 or not advs:
                        continue
                    student = clean_person_name(auths[0].get("name") or "")
                    if not student:
                        continue
                    advisors = []
                    for a in advs:
                        advisors.extend(split_advisor_field(a.get("name") or ""))
                    if not advisors:
                        continue
                    title = clean_title(nd.get("title") or "")
                    subjs = ", ".join((nd.get("disciplines") or []) + (nd.get("subjects") or []))
                    if not classify_stat_field(subjs, title, advisor_is_core=False):
                        continue
                    records.append({
                        "student": student,
                        "advisors": advisors,
                        "school": school,
                        "country": "United States",
                        "year": yr,
                        "thesis": title,
                        "subjects": subjs,
                        "degree": "Ph.D.",
                        "orcid": (auths[0].get("orcid") or "").strip(),
                        "source": "eScholarship",
                    })
                    campus_cnt += 1
                pi = items.get("pageInfo") or {}
                if stop_early or not pi.get("hasNextPage") or not pi.get("endCursor"):
                    break
                after = pi["endCursor"]
            except Exception:
                break
        if campus_cnt:
            print(f"  [eScholarship] {school:42s}: {campus_cnt:4d} recent (2023-2026) doctoral ETDs")
    return records


def main():
    t0 = time.time()
    print("=" * 72)
    print("WAVE 1A: 34 DSPACE 7 REPOS + FIGSHARE (CMU/PURDUE/GLOBAL) + BROWN + UC")
    print("=" * 72)

    all_records = []

    print("\n[1/4] Crawling 34 live DSpace 7 university repositories concurrently...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        for res in pool.map(harvest_one_dspace7, DSPACE7_REPOS):
            all_records.extend(res)

    print("\n[2/4] Crawling Figshare Institutional Theses (CMU KiltHub, Purdue Hammer, Global)...")
    all_records.extend(harvest_figshare_theses())

    print("\n[3/4] Crawling Brown Digital Repository JSON API...")
    all_records.extend(harvest_brown_bdr())

    print("\n[4/4] Refreshing UC eScholarship 2023-2026 doctoral dissertations (all 10 campuses)...")
    all_records.extend(harvest_uc_escholarship_recent())

    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(all_records, f, ensure_ascii=False)
    print(f"\nSaved {len(all_records):,} raw doctoral records -> {CACHE_FILE}")

    print("\nIngesting Wave 1A records into data/stat_genealogy.db...")
    ing = StatLinkIngestor()
    for rec in all_records:
        ing.ingest_record(rec, allow_without_advisor=False)
    ing.commit_and_close()

    print(f"\nWave 1A DONE in {time.time() - t0:.1f}s:")
    for k, v in ing.stats.items():
        print(f"  {k:20s}: {v:,}")


if __name__ == "__main__":
    main()
