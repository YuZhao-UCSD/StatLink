#!/usr/bin/env python3
"""
Post-2014 Global Dissertation Gap Filler & Pre-2015 ProQuest Noise Recalibrator
(`scripts/fill_post2014_drop.py`).

Closes the 2014 -> 2015 annual cohort step-change from BOTH sides without deleting a single person:
  1. Harvests 2014–2026 Doctoral Dissertations (PhD / ScD / DrPH) in Statistics, Biostatistics &
     Probability from 7 newly verified institutional ETD feeds:
       - University of Washington (`digital.lib.washington.edu` DSpace 7)
       - Iowa State University (`dr.lib.iastate.edu` DSpace 7)
       - University of Minnesota (`conservancy.umn.edu` DSpace 7)
       - Boston University (`open.bu.edu` DSpace 7)
       - Virginia Tech (`vtechworks.lib.vt.edu` DSpace 7)
       - University of Toronto (`tspace.library.utoronto.ca` DSpace 7)
       - Michigan State University (`d.lib.msu.edu/oai` OAI-PMH)
  2. Harvests 2014–2026 Global Statistical, Biostatistical & Probabilistic Doctoral Dissertations
     from OpenAlex (`api.openalex.org/works`, `type:dissertation`, cursor-paginated at 200/page),
     resolving canonical degree-granting universities (US, UK EThOS archive, ETH Zurich, Canada,
     Germany, Netherlands, Scandinavia, Australia, etc.).
  3. Recalibrates pre-2015 `AFT-only` ProQuest non-statistical over-inclusion (e.g. Walden/Capella/
     Phoenix online DrPH/EdD programs & pure Psychology/Music/Wildlife theses) from `is_core_stat=1`
     to `is_core_stat=0, is_broad_stat=1, primary_field='Related Quantitative'`.
"""

import json
import os
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict, deque

import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "scripts"))

from build_database import canonicalize_school
from close_jhu_and_peer_gaps import EXTRA_SCHOOL_FIXES
from harvest_web_and_etds import clean_person_name, first_last_sig, fold_ascii, norm_key

DB_PATH = os.path.join(BASE_DIR, "data", "stat_genealogy.db")
HEADERS = {"User-Agent": "StatLineage/1.0 (Academic Genealogy; mailto:research@statlineage.org)"}

# High-precision Core Statistical / Biostatistical / Probabilistic Dissertation Title Patterns
CORE_STAT_TITLE_RE = re.compile(
    r"\b("
    r"biostatistic\w*|bayesian\s+(?:inference|nonparametric\w*|hierarchical|variable\s+selection|model\w*|regression|method\w*|spatial|survival|network\w*|computation\w*|design\w*)|"
    r"causal\s+(?:inference|mediation|effect\w*|discovery)|propensity\s+score\w*|instrumental\s+variable\w*|"
    r"semiparametric\w*|nonparametric\s+(?:regression|estimation|inference|test\w*|bayes\w*|density|method\w*)|"
    r"high[\-\s]dimensional\s+(?:inference|regression|data|statistic\w*|variable|covariance|time\s+series|model\w*|testing)|"
    r"survival\s+analysis|proportional\s+hazards|competing\s+risks|recurrent\s+event\w*|interval[\-\s]censored|longitudinal\s+data|"
    r"mixed[\-\s]effects\s+model\w*|generalized\s+linear\s+model\w*|quantile\s+regression|functional\s+data\s+analysis|"
    r"conformal\s+prediction|multiple\s+testing|false\s+discovery\s+rate|empirical\s+bayes|empirical\s+likelihood|"
    r"markov\s+chain\s+monte\s+carlo|variational\s+inference|stein'?s\s+method|martingale\w*|stochastic\s+(?:calculus|differential\s+equation\w*|process\w*|analysis)|"
    r"random\s+matri(?:x|ces)|percolation|interacting\s+particle|brownian\s+motion|levy\s+process\w*|large\s+deviations|"
    r"spatial\s+statistics|spatio[\-\s]temporal\s+(?:model\w*|process\w*|data)|time\s+series\s+(?:models?|analysis|econometrics)|"
    r"experimental\s+design|adaptive\s+(?:clinical\s+)?trial\w*|small\s+area\s+estimation|survey\s+sampling|missing\s+data|"
    r"sufficient\s+dimension\s+reduction|graphical\s+model\w*|change[\-\s]point\s+detection|minimax\s+(?:rate\w*|estimation|optimal\w*)"
    r")\b",
    re.I,
)

PROB_TITLE_RE = re.compile(
    r"\b(stochastic\s+(?:calculus|differential|process\w*|analysis)|martingale\w*|random\s+matri(?:x|ces)|"
    r"percolation|brownian\s+motion|levy\s+process\w*|large\s+deviations|interacting\s+particle|markov\s+process\w*)\b",
    re.I,
)

BIOSTAT_TITLE_RE = re.compile(
    r"\b(biostatistic\w*|clinical\s+trial\w*|survival\s+analysis|proportional\s+hazards|competing\s+risks|"
    r"recurrent\s+event\w*|longitudinal\s+data|epidemiolog\w*|mendelian\s+randomization|single[\-\s]cell|genomic\w*|neuroimaging|fmri)\b",
    re.I,
)

NON_PHD_REJECT_RE = re.compile(
    r"\b(master'?s|master\s+of|bachelor|undergraduate|honors\s+thesis|b\.?\s*s\.?\b|m\.?\s*s\.?\b|m\.?\s*a\.?\b|"
    r"m\.?\s*sc|diplomarbeit|licentiate|capstone|course\s+notes|lecture\s+notes|habilitationsschrift)\b",
    re.I,
)

NON_STAT_DOMAIN_REJECT_RE = re.compile(
    r"\b(deer|wildlife|fisheries|poultry|dairy\s+cattle|beef\s+cattle|swine|soybean|maize|turfgrass|"
    r"tonality|music\s+theory|literary|poetry|archeolog\w*|archaeolog\w*|theolog\w*|nursing\s+practice)\b",
    re.I,
)


def canon_uni(raw_u: str) -> str:
    if not raw_u:
        return ""
    s = raw_u.strip()
    # Extract university inside parentheses if repository name like "ScholarlyCommons (University of Pennsylvania)"
    m = re.search(r"\(([^)]*(?:University|Institut|College|École|ETH|UCL|KU\s+Leuven)[^)]*)\)", s, re.I)
    if m:
        s = m.group(1).strip()
    s = re.sub(r"\s+System$", "", s, flags=re.I).strip()
    c = canonicalize_school(s)
    return EXTRA_SCHOOL_FIXES.get(c, c)


def harvest_6_dspace7_servers():
    """Harvests 2013–2026 doctoral Statistics & Biostatistics theses from 6 DSpace 7 servers."""
    configs = [
        ("University of Washington", "United States", "https://digital.lib.washington.edu/server/api/discover/search/objects", ["Theses--Biostatistics", "Theses--Statistics"]),
        ("Iowa State University", "United States", "https://dr.lib.iastate.edu/server/api/discover/search/objects", ["department:Statistics dissert*"]),
        ("University of Minnesota", "United States", "https://conservancy.umn.edu/server/api/discover/search/objects", ["Biostatistics dissertation", "School of Statistics dissertation"]),
        ("Boston University", "United States", "https://open.bu.edu/server/api/discover/search/objects", ["Biostatistics", "Statistics"]),
        ("Virginia Tech", "United States", "https://vtechworks.lib.vt.edu/server/api/discover/search/objects", ["Statistics"]),
        ("University of Toronto", "Canada", "https://tspace.library.utoronto.ca/server/api/discover/search/objects", ["Statistical Sciences", "Biostatistics"]),
    ]

    sess = requests.Session()
    sess.headers.update(HEADERS)
    out = []

    for canon_school, country, api_url, queries in configs:
        seen_titles = set()
        sch_cnt = 0
        for q in queries:
            for page in range(0, 12):
                try:
                    params = {"query": q, "size": 50, "page": page, "sort": "dc.date.issued,DESC"}
                    if canon_school == "Virginia Tech":
                        params["f.department"] = "Statistics,equals"
                    r = sess.get(api_url, params=params, timeout=14)
                    if r.status_code != 200:
                        break
                    sr = ((r.json().get("_embedded") or {}).get("searchResult")) or {}
                    objs = ((sr.get("_embedded") or {}).get("objects")) or []
                    if not objs:
                        break
                    stop_early = False
                    for o in objs:
                        md = (((o.get("_embedded") or {}).get("indexableObject")) or {}).get("metadata") or {}
                        vals = lambda k: [x.get("value", "").strip() for x in (md.get(k) or []) if x.get("value")]

                        # Year check (focus on >= 2013!)
                        dt_list = vals("dc.date.issued")
                        ym = re.search(r"\b(19\d{2}|20[0-2]\d)\b", dt_list[0] if dt_list else "")
                        yr = int(ym.group(1)) if ym else None
                        if yr and yr < 2012:
                            stop_early = True
                            continue
                        if yr and yr > 2026:
                            yr = None

                        # Degree level check: STRICTLY doctoral!
                        deg_lvl = " ".join(vals("thesis.degree.level") + vals("etd.degree.level") + vals("dc.description.degree") + vals("thesis.degree.name") + vals("etd.degree.name") + vals("dc.type") + vals("dc.type.genre")).lower()
                        if any(bad in deg_lvl for bad in ["master", "mast", "bachelor", "undergrad", "m.s.", "m.a.", "msc", "mph", "report", "curricula"]):
                            if not any(ok in deg_lvl for ok in ["ph.d", "phd", "doctor", "dissertation", "sc.d"]):
                                continue
                        if canon_school in ("Boston University", "Virginia Tech", "University of Toronto", "Iowa State University"):
                            if not any(ok in deg_lvl for ok in ["doctoral", "dissertation", "phd", "ph.d", "doctor"]):
                                continue

                        # Department / Subject check: STRICTLY Statistics / Biostatistics / Probability!
                        dept_txt = " ".join(vals("dc.contributor.department") + vals("thesis.degree.discipline") + vals("etd.degree.discipline") + vals("dc.subject.other") + vals("dc.subject.disciplines"))
                        titles = vals("dc.title")
                        title = titles[0] if titles else ""
                        if not title or title.lower() in seen_titles:
                            continue

                        if dept_txt:
                            if not re.search(r"\b(biostat\w*|statist\w*|probab\w*)\b", dept_txt, re.I):
                                continue
                        else:
                            if not CORE_STAT_TITLE_RE.search(title):
                                continue
                        if NON_STAT_DOMAIN_REJECT_RE.search(title):
                            continue

                        auths = vals("dc.contributor.author") + vals("dc.creator")
                        if not auths:
                            continue
                        stu_nm = clean_person_name(auths[0])
                        if not stu_nm:
                            continue

                        adv_raw = vals("dc.contributor.committeechair") + vals("dc.contributor.advisor") + vals("dc.contributor.supervisor")
                        advisors = [clean_person_name(a) for a in adv_raw[:2] if clean_person_name(a)]

                        fld = "Biostatistics" if ("biostat" in (dept_txt + " " + title).lower()) else (
                            "Probability" if PROB_TITLE_RE.search(title) else "Statistics"
                        )
                        seen_titles.add(title.lower())
                        out.append({
                            "student": stu_nm,
                            "school": canon_school,
                            "country": country,
                            "year": yr,
                            "thesis": title,
                            "advisors": advisors,
                            "orcid": "",
                            "field": fld,
                            "source": "University ETD",
                        })
                        sch_cnt += 1
                    if stop_early or len(objs) < 50:
                        break
                except Exception:
                    break
        print(f"    -> DSpace7 {canon_school:34s}: +{sch_cnt:3d} post-2012 doctoral theses", flush=True)
    return out


def harvest_msu_oai():
    """Harvests Michigan State University OAI-PMH dissertations in Statistics & Probability."""
    sess = requests.Session()
    sess.headers.update(HEADERS)
    out = []
    token = None
    pages = 0
    ns = {"oai": "http://www.openarchives.org/OAI/2.0/", "dc": "http://purl.org/dc/elements/1.1/"}
    while pages < 30:
        pages += 1
        params = {"verb": "ListRecords", "resumptionToken": token} if token else {"verb": "ListRecords", "metadataPrefix": "oai_dc", "from": "2013-01-01"}
        try:
            r = sess.get("https://d.lib.msu.edu/oai", params=params, timeout=15)
            if r.status_code != 200:
                break
            root = ET.fromstring(r.text)
            for rec in root.findall(".//oai:record", ns):
                md = rec.find("oai:metadata", ns)
                if md is None:
                    continue
                titles = [x.text.strip() for x in md.findall(".//dc:title", ns) if x.text]
                creators = [x.text.strip() for x in md.findall(".//dc:creator", ns) if x.text]
                contribs = [x.text.strip() for x in md.findall(".//dc:contributor", ns) if x.text]
                subjs = " ".join(x.text.strip() for x in md.findall(".//dc:subject", ns) if x.text)
                descs = " ".join(x.text.strip() for x in md.findall(".//dc:description", ns) if x.text)
                dates = [x.text.strip() for x in md.findall(".//dc:date", ns) if x.text]
                if not titles or not creators:
                    continue
                if "master" in descs.lower() and "doctor" not in descs.lower() and "ph.d" not in descs.lower():
                    continue
                if not (re.search(r"\b(statistics|biostatistics|probability)\b", subjs, re.I) or CORE_STAT_TITLE_RE.search(titles[0])):
                    continue
                if NON_STAT_DOMAIN_REJECT_RE.search(titles[0]):
                    continue
                stu = clean_person_name(creators[0])
                if not stu:
                    continue
                ym = re.search(r"\b(201[3-9]|202[0-6])\b", " ".join(dates))
                yr = int(ym.group(1)) if ym else None
                advs = [clean_person_name(c) for c in contribs[:1] if clean_person_name(c)]
                fld = "Biostatistics" if "biostat" in (subjs + " " + titles[0]).lower() else ("Probability" if PROB_TITLE_RE.search(titles[0]) else "Statistics")
                out.append({
                    "student": stu,
                    "school": "Michigan State University",
                    "country": "United States",
                    "year": yr,
                    "thesis": titles[0],
                    "advisors": advs,
                    "orcid": "",
                    "field": fld,
                    "source": "University ETD",
                })
            rt = root.find(".//oai:resumptionToken", ns)
            token = rt.text.strip() if (rt is not None and rt.text and rt.text.strip()) else None
            if not token:
                break
        except Exception:
            break
    print(f"    -> OAI Michigan State University         : +{len(out):3d} post-2013 doctoral theses", flush=True)
    return out


def harvest_openalex_global_dissertations(known_schools: set):
    """Cursor-paginates OpenAlex 2014–2026 `type:dissertation` works matching Core Stat/Biostat/Prob."""
    sess = requests.Session()
    sess.headers.update(HEADERS)
    cursor = "*"
    out = []
    pages = 0

    filter_str = (
        "type:dissertation,publication_year:2014-2026,"
        "title.search:biostatistics|bayesian|semiparametric|nonparametric|causal inference|"
        "high-dimensional|survival analysis|proportional hazards|competing risks|quantile regression|"
        "functional data analysis|conformal prediction|false discovery rate|empirical bayes|"
        "markov chain monte carlo|variational inference|martingale|stochastic differential|"
        "random matrices|percolation|spatio-temporal|small area estimation|graphical models|minimax"
    )

    while cursor and pages < 75:  # Up to 15,000 dissertations scanned at 200/page (~40s)
        pages += 1
        try:
            r = sess.get(
                "https://api.openalex.org/works",
                params={"filter": filter_str, "per-page": 200, "cursor": cursor},
                timeout=16,
            )
            if r.status_code != 200:
                break
            data = r.json()
            results = data.get("results") or []
            if not results:
                break
            cursor = (data.get("meta") or {}).get("next_cursor")

            for w in results:
                yr = w.get("publication_year")
                if not (isinstance(yr, int) and 2014 <= yr <= 2026):
                    continue
                title = (w.get("title") or "").strip()
                if not title or len(title) < 15:
                    continue
                if NON_PHD_REJECT_RE.search(title) or NON_STAT_DOMAIN_REJECT_RE.search(title):
                    continue
                if not CORE_STAT_TITLE_RE.search(title):
                    continue

                auths = w.get("authorships") or []
                if not (1 <= len(auths) <= 3):
                    continue
                a0 = auths[0]
                au_obj = a0.get("author") or {}
                stu_nm = clean_person_name(au_obj.get("display_name") or "")
                if not stu_nm:
                    continue
                oid_raw = au_obj.get("orcid") or ""
                oid = oid_raw.split("/")[-1] if "orcid.org/" in oid_raw else ""

                # Resolve degree-granting university from authorship institution or repository source
                raw_sch = ""
                insts = a0.get("institutions") or []
                if insts and insts[0].get("display_name"):
                    raw_sch = insts[0]["display_name"]
                if not raw_sch:
                    src_obj = ((w.get("primary_location") or {}).get("source")) or {}
                    disp_s = src_obj.get("display_name") or ""
                    host_s = src_obj.get("host_organization_name") or ""
                    if any(k in disp_s.lower() for k in ["university", "institut", "college", "eth ", "ucl"]):
                        raw_sch = disp_s
                    elif any(k in host_s.lower() for k in ["university", "institut", "college", "eth "]):
                        raw_sch = host_s
                if not raw_sch:
                    raw_affs = a0.get("raw_affiliation_strings") or []
                    if raw_affs and len(raw_affs[0]) < 95 and any(k in raw_affs[0].lower() for k in ["university", "institut", "college"]):
                        raw_sch = raw_affs[0]

                sch = canon_uni(raw_sch)
                # Keep if recognized university or explicitly contains University/Institut/ETH/UCL
                if not sch or (sch not in known_schools and not re.search(r"\b(University|Universität|Université|Universidad|Universiteit|Universidade|Institut\w*|ETH\s+Z|College\s+London)\b", sch, re.I)):
                    continue
                if any(bad_s in sch.lower() for bad_s in ["press", "california digital library", "zenodo", "arxiv", "proquest", "springer", "elsevier", "wiley"]):
                    continue

                # If 2 authors on a dissertation record, 2nd author in OpenAlex ETDs is frequently the supervisor!
                advisors = []
                if len(auths) == 2:
                    adv_cand = clean_person_name((auths[1].get("author") or {}).get("display_name") or "")
                    if adv_cand and norm_key(adv_cand) != norm_key(stu_nm):
                        advisors.append(adv_cand)

                fld = "Biostatistics" if BIOSTAT_TITLE_RE.search(title) else (
                    "Probability" if PROB_TITLE_RE.search(title) else "Statistics"
                )
                out.append({
                    "student": stu_nm,
                    "school": sch,
                    "country": "",
                    "year": yr if yr <= 2025 else None,
                    "thesis": title,
                    "advisors": advisors,
                    "orcid": oid,
                    "field": fld,
                    "source": "OpenAlex Global ETD",
                })
        except Exception:
            break

    print(f"    -> OpenAlex Global ETDs (2014–2026)      : +{len(out):,} verified Stat/Biostat/Prob dissertations", flush=True)
    return out


def main():
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    known_schools = {r[0] for r in cur.execute("SELECT school FROM schools WHERE total_phd_count >= 3").fetchall()}

    print("[1/4] Harvesting 7 newly verified Institutional ETD feeds (UW, Iowa State, UMN, BU, VT, Toronto, MSU)...", flush=True)
    dspace_recs = harvest_6_dspace7_servers()
    msu_recs = harvest_msu_oai()

    print("\n[2/4] Harvesting OpenAlex Global 2014–2026 Statistical/Biostatistical/Probabilistic Dissertations...", flush=True)
    oa_recs = harvest_openalex_global_dissertations(known_schools)

    print("\n[3/4] Recalibrating pre-2015 non-statistical ProQuest over-inclusion (`is_core_stat=1 -> Related Quantitative`)...", flush=True)
    # 3A: Online EdD/DrPH/DBA institutions without PhD Statistics departments (e.g. Walden, Capella, Phoenix, Argosy, Northcentral)
    cur.execute("""
        UPDATE people
        SET is_core_stat = 0, is_broad_stat = 1, primary_field = 'Related Quantitative'
        WHERE is_core_stat = 1 AND sources = 'AFT'
          AND (
              school IN ('Walden University', 'Capella University', 'University of Phoenix', 'Northcentral University', 'Argosy University', 'Nova Southeastern University', 'Grand Canyon University', 'Alliant International University', ' Fielding Graduate University')
              OR LOWER(school) LIKE '%walden%' OR LOWER(school) LIKE '%capella%'
          )
    """)
    demoted_online = cur.rowcount

    # 3B: Pure Psychology/Cognitive/Music/Wildlife/Literature `AFT-only` theses without Stat/Biostat/Prob/Epi/Econ research areas
    cur.execute("""
        UPDATE people
        SET is_core_stat = 0, is_broad_stat = 1, primary_field = 'Related Quantitative'
        WHERE is_core_stat = 1 AND sources = 'AFT'
          AND classification_reason IN ('Dissertation ML + Domain Terms', 'Lineage Consensus (Stat/Biostat/Prob Advisor)', 'General Mathematical / Ancestral Lineage')
          AND LOWER(research_areas) NOT LIKE '%stat%'
          AND LOWER(research_areas) NOT LIKE '%probab%'
          AND LOWER(research_areas) NOT LIKE '%epidemiol%'
          AND LOWER(research_areas) NOT LIKE '%economet%'
          AND LOWER(research_areas) NOT LIKE '%biomet%'
          AND (
              (research_areas != '' AND (
                  LOWER(research_areas) LIKE '%memory%' OR LOWER(research_areas) LIKE '%emotion%' OR LOWER(research_areas) LIKE '%auditory%'
                  OR LOWER(research_areas) LIKE '%visual cortex%' OR LOWER(research_areas) LIKE '%speech%' OR LOWER(research_areas) LIKE '%music%'
                  OR LOWER(research_areas) LIKE '%ecology%' OR LOWER(research_areas) LIKE '%mammol%' OR LOWER(research_areas) LIKE '%wildlife%'
                  OR LOWER(research_areas) LIKE '%psychol%' OR LOWER(research_areas) LIKE '%linguist%' OR LOWER(research_areas) LIKE '%sociol%'
              ))
              OR LOWER(thesis) LIKE '%mule deer%' OR LOWER(thesis) LIKE '%tonality%' OR LOWER(thesis) LIKE '%ipomoea%'
              OR LOWER(thesis) LIKE '%human memory%' OR LOWER(thesis) LIKE '%eye movement%' OR LOWER(thesis) LIKE '%auditory cortex%'
          )
    """)
    demoted_domain = cur.rowcount
    print(f"      Recalibrated {demoted_online + demoted_domain:,} non-stat ProQuest theses ({demoted_online} online DrPH/EdD + {demoted_domain} non-stat domain) to Related Quantitative (0 deleted!).", flush=True)

    # -----------------------------------------------------------------------
    # Stage 4: O(1) Hash-Indexed Integration + DAG & FTS5 Rebuild
    # -----------------------------------------------------------------------
    all_new = dspace_recs + msu_recs + oa_recs
    print(f"\n[4/4] O(1) Hash-Indexed Integration of {len(all_new):,} harvested dissertations...", flush=True)

    by_orcid = {}
    by_norm_school = defaultdict(list)
    by_fl_school = defaultdict(list)
    fac_by_school_fl = defaultdict(list)

    for r in cur.execute("SELECT id, name, norm_name, school, current_affiliation, year, thesis, primary_field, is_core_stat, student_count, orcid FROM people"):
        d = dict(r)
        if d["orcid"]:
            by_orcid[d["orcid"]] = d
        if d["school"]:
            by_norm_school[(d["norm_name"], d["school"])].append(d)
            f0, l0 = first_last_sig(d["name"])
            if f0 and l0:
                by_fl_school[(f0, l0, d["school"])].append(d)
        if d["current_affiliation"]:
            by_norm_school[(d["norm_name"], d["current_affiliation"])].append(d)

    for r in cur.execute("""
        SELECT DISTINCT p.id, p.name, p.year, a.school
        FROM advisor_schools a JOIN people p ON p.id = a.person_id
        WHERE p.is_broad_stat = 1
    """):
        f0, l0 = first_last_sig(r["name"])
        if f0 and l0:
            fac_by_school_fl[(r["school"], f0[0], l0)].append((r["id"], r["name"], f0, r["year"]))

    school_country = {r[0]: r[1] for r in cur.execute("SELECT school, country FROM schools WHERE country != ''").fetchall()}
    next_id = (cur.execute("SELECT MAX(id) FROM people").fetchone()[0] or 7_600_000) + 100
    existing_edges = set((r[0], r[1]) for r in cur.execute("SELECT advisor_id, student_id FROM edges").fetchall())

    added_p = 0
    enriched_p = 0
    added_e = 0

    for item in all_new:
        stu_nm = item["student"]
        snk = norm_key(stu_nm)
        f1, l1 = first_last_sig(stu_nm)
        if not snk or not f1 or len(f1) < 2:
            continue
        sch = item["school"]
        yr = item["year"]
        th = item["thesis"]
        oid = item["orcid"]
        fld = item["field"]
        src = item["source"]

        matched = by_orcid.get(oid) if oid else None
        if not matched:
            for c in by_norm_school.get((snk, sch), ()):
                if not (yr and c["year"] and abs(yr - c["year"]) > 4):
                    matched = c
                    break
        if not matched:
            for c in by_fl_school.get((f1, l1, sch), ()):
                if not (yr and c["year"] and abs(yr - c["year"]) > 3):
                    matched = c
                    break

        if matched:
            sid = matched["id"]
            cur.execute(
                """UPDATE people SET
                    year = COALESCE(year, ?),
                    thesis = CASE WHEN thesis = '' THEN ? ELSE thesis END,
                    orcid = CASE WHEN orcid = '' THEN ? ELSE orcid END,
                    is_core_stat = 1,
                    is_broad_stat = 1
                   WHERE id = ?""",
                (yr, th, oid, sid),
            )
            enriched_p += 1
        else:
            sid = next_id
            next_id += 1
            ctry = item["country"] or school_country.get(sch, "")
            cur.execute(
                """INSERT INTO people (
                    id, name, norm_name, search_aliases, school, raw_school, country, year, thesis,
                    mgp_subject, primary_field, is_core_stat, is_broad_stat, field_confidence,
                    classification_reason, current_affiliation, research_areas, mgp_id, aft_pid,
                    wikidata_qid, orcid, homepage, proquest_id, sources, advisor_count, student_count,
                    descendant_count, stat_descendant_count
                ) VALUES (?,?,?,?,?,'',?,?,?,NULL,?,1,1,0.90,?,'','',NULL,NULL,'',?,'','',?,0,0,0,0)""",
                (sid, stu_nm, snk, "", sch, ctry, yr, th, fld, f"Verified via {src}", oid, src),
            )
            new_d = {"id": sid, "name": stu_nm, "norm_name": snk, "school": sch, "current_affiliation": "", "year": yr, "thesis": th, "primary_field": fld, "is_core_stat": 1, "student_count": 0, "orcid": oid}
            by_norm_school[(snk, sch)].append(new_d)
            by_fl_school[(f1, l1, sch)].append(new_d)
            if oid:
                by_orcid[oid] = new_d
            added_p += 1

        # Link verified advisor(s)
        for anm in item["advisors"]:
            af, al = first_last_sig(anm)
            if not af or not al:
                continue
            aid = None
            for pid, pnm, pf_tok, pyr in fac_by_school_fl.get((sch, af[0], al), ()):
                if len(af) == 1 or pf_tok.startswith(af) or af.startswith(pf_tok):
                    if not (pyr and yr and pyr > yr - 3):
                        aid = pid
                        break
            if not aid and src == "University ETD" and len(af) >= 2:
                # For authoritative DSpace7 committeechair/advisor, match or create faculty advisor at `sch`
                ank = norm_key(anm)
                cands = by_norm_school.get((ank, sch), ())
                if cands:
                    aid = cands[0]["id"]
                else:
                    row_glob = cur.execute(
                        "SELECT id FROM people WHERE norm_name = ? AND is_broad_stat = 1 AND (year IS NULL OR year <= ?)",
                        (ank, (yr or 2025) - 3),
                    ).fetchall()
                    if len(row_glob) == 1:
                        aid = row_glob[0][0]
            if aid and aid != sid and (aid, sid) not in existing_edges and (sid, aid) not in existing_edges:
                cur.execute("INSERT OR IGNORE INTO edges VALUES (?, ?, ?)", (aid, sid, src))
                existing_edges.add((aid, sid))
                added_e += 1

    print(f"      Added +{added_p:,} net-new PhDs, +{added_e:,} advisor-student edges, and enriched {enriched_p:,} existing records!", flush=True)

    # Refresh advisor_schools, DAG counts, schools, meta_stats & FTS5
    cur.execute("""
        INSERT OR REPLACE INTO advisor_schools (person_id, school, advised_at_school)
        SELECT e.advisor_id, s.school, COUNT(*)
        FROM edges e JOIN people s ON s.id = e.student_id
        WHERE s.school != '' GROUP BY e.advisor_id, s.school
    """)

    valid_edges = set(
        (r[0], r[1])
        for r in cur.execute(
            "SELECT e.advisor_id, e.student_id FROM edges e JOIN people p1 ON p1.id=e.advisor_id JOIN people p2 ON p2.id=e.student_id"
        ).fetchall()
    )
    advisors_of = defaultdict(list)
    students_of = defaultdict(list)
    for a, s in valid_edges:
        advisors_of[s].append(a)
        students_of[a].append(s)

    core_flags = {r[0]: r[1] for r in cur.execute("SELECT id, is_core_stat FROM people").fetchall()}
    all_uids = list(core_flags.keys())
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
    meta.update({
        "total_people": cur.execute("SELECT COUNT(*) FROM people").fetchone()[0],
        "total_edges": cur.execute("SELECT COUNT(*) FROM edges").fetchone()[0],
        "core_stat_total": cur.execute("SELECT COUNT(*) FROM people WHERE is_core_stat=1").fetchone()[0],
        "broad_stat_total": cur.execute("SELECT COUNT(*) FROM people WHERE is_broad_stat=1").fetchone()[0],
        "by_field": field_dist,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    cur.execute("INSERT OR REPLACE INTO meta_stats VALUES ('summary', ?)", (json.dumps(meta),))
    cur.execute("INSERT INTO people_fts(people_fts) VALUES('rebuild')")
    conn.commit()

    print("\n=== UPDATED ANNUAL CORE COUNTS (2010–2025) ===", flush=True)
    for yr in range(2010, 2026):
        c_yr = cur.execute("SELECT COUNT(*) FROM people WHERE is_core_stat=1 AND year=?", (yr,)).fetchone()[0]
        print(f"  {yr}: {c_yr:,}", flush=True)
    conn.close()
    print(f"DONE in {time.time() - t0:.1f}s! Summary: {json.dumps(meta)}", flush=True)


if __name__ == "__main__":
    main()
