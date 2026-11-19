#!/usr/bin/env python3
"""
Closes the Post-2013 ProQuest / Cloudflare Recall Gap for Johns Hopkins University (JHU)
and 31 Peer Biostatistics & Statistics Universities (`scripts/close_jhu_and_peer_gaps.py`).

Root Causes Addressed:
  1. School Name Fragmentation & MGP<->AFT Split Faculty Records:
     - Canonicalizes fragmented school strings (`University of Florida, Gainesville`, `Texas A & M University`,
       `University of Texas School of Public Health`, `State University of New York, Buffalo`, `The Johns Hopkins Univers`, etc.).
     - Merges split MGP + AFT professor stubs (`year IS NULL` in AFT) and recovers interdisciplinary AFT `relation=1`
       PhD students (`neuro`, `bme`, `etree`, `computerscience`) advised by verified Stat/Biostat/Prob faculty.
  2. Newly Unlocked School of Public Health DigitalCommons OAI-PMH (`qdc`) Feeds:
     - Harvests UTHealth Houston SPH, VCU Biostatistics, USF Biostatistics, UAB Biostatistics, and MUSC Biostatistics.
  3. External Non-Cloudflare Faculty Lab Sites (JHU & Peer Biostatistics Labs on GitHub.io / custom domains).
  4. Large-Scale ORCID Public API (`pub.orcid.org/v3.0`) Doctoral Harvester + Crossref Polite-Pool Senior-Advisor Linker
     across JHU and 31 peer Biostatistics/Statistics universities blocked by Cloudflare or absent from MGP.
"""

import concurrent.futures
import csv
import gzip
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict, deque

import requests
from bs4 import BeautifulSoup

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

from build_database import SCHOOL_CANONICAL_MAP, canonicalize_school
from crawler_engine import HEADERS, clean_extracted_person_name, fold_ascii, norm_key, parse_html_group_page
from crawl_all_schools_and_professors import harvest_digitalcommons_oai
from harvest_web_and_etds import STAT_TITLE_RE, clean_person_name, first_last_sig, infer_field

DB_PATH = os.path.join(BASE_DIR, "data", "stat_genealogy.db")
RAW_DIR = os.path.join(BASE_DIR, "data_raw")

POLITE_HEADERS = {
    "User-Agent": "StatLineageAcademicExplorer/2.5 (https://statlineage.local; mailto:research@statlineage.org)",
    "Accept": "application/json",
}

# Additional fragmented school names uncovered by our 2004-2013 vs 2015-2025 gap audit
EXTRA_SCHOOL_FIXES = {
    "University of Florida, Gainesville": "University of Florida",
    "Texas A & M University": "Texas A&M University",
    "Texas A & M University, College Station": "Texas A&M University",
    "University of Texas School of Public Health": "University of Texas Health Science Center at Houston",
    "The University of Texas Health Science Center at Houston": "University of Texas Health Science Center at Houston",
    "University of Texas Health Science Center, Houston": "University of Texas Health Science Center at Houston",
    "State University of New York, Buffalo": "University at Buffalo (SUNY)",
    "State University of New York at Buffalo": "University at Buffalo (SUNY)",
    "University of Nebraska - Lincoln": "University of Nebraska-Lincoln",
    "University of Nebraska, Lincoln": "University of Nebraska-Lincoln",
    "The Johns Hopkins Univers": "Johns Hopkins University",
    "The Johns Hopkins University": "Johns Hopkins University",
    "Johns Hopkins University School of Hygiene and Public Health": "Johns Hopkins University",
    "Johns Hopkins University Schoolof Public Health": "Johns Hopkins University",
    "Johns Hopkins University Bloomberg School of Public Health": "Johns Hopkins University",
    "School of Hygiene and Public Health, The Johns Hopkins University": "Johns Hopkins University",
    "Johns Hopkins University; Whiting School of Engineering": "Johns Hopkins University",
    "Washington University, Saint Louis": "Washington University in St. Louis",
    "University of Illinois, Chicago": "University of Illinois at Chicago",
    "University of Colorado, Denver": "University of Colorado Anschutz Medical Campus",
    "University of Maryland, Baltimore": "University of Maryland",
    "Université Pierre-et-Marie-Curie - Paris VI": "Sorbonne Université",
    "Université Pierre et Marie Curie - Paris 6": "Sorbonne Université",
    "Université Paris-Sud XI - Orsay": "Université Paris-Saclay",
}

SPH_DIGITALCOMMONS_REPOS = [
    ("University of Texas Health Science Center at Houston", "United States", "https://digitalcommons.library.tmc.edu/do/oai/", "publication:uthsph_dissertations"),
    ("Virginia Commonwealth University", "United States", "https://scholarscompass.vcu.edu/do/oai/", "publication:etd"),
    ("University of South Florida", "United States", "https://digitalcommons.usf.edu/do/oai/", "publication:etd"),
    ("University of Alabama at Birmingham", "United States", "https://digitalcommons.library.uab.edu/do/oai/", "publication:etd-collection"),
    ("Medical University of South Carolina", "United States", "https://medica-musc.researchcommons.org/do/oai/", "publication:theses"),
]

# JHU & Peer Universities targeted for ORCID PhD Graduation + Crossref Doctoral Advisor Resolution
ORCID_TARGET_UNIVERSITIES = [
    ("Johns Hopkins University", '"Johns Hopkins"', 600),
    ("Harvard University", '"Harvard"', 500),
    ("University of Washington", '"University of Washington"', 450),
    ("University of North Carolina at Chapel Hill", '"North Carolina at Chapel Hill" OR "UNC Chapel Hill"', 450),
    ("University of Michigan", '"University of Michigan"', 450),
    ("Columbia University", '"Columbia University"', 400),
    ("Emory University", '"Emory University"', 300),
    ("University of Pittsburgh", '"University of Pittsburgh"', 300),
    ("Boston University", '"Boston University"', 300),
    ("Yale University", '"Yale University"', 250),
    ("Brown University", '"Brown University"', 250),
    ("North Carolina State University", '"North Carolina State University"', 350),
    ("Texas A&M University", '"Texas A&M"', 300),
    ("University of Florida", '"University of Florida"', 300),
    ("Rutgers University", '"Rutgers"', 250),
    ("University of Maryland", '"University of Maryland"', 250),
    ("University of Southern California", '"University of Southern California"', 250),
    ("University of Chicago", '"University of Chicago"', 250),
    ("Carnegie Mellon University", '"Carnegie Mellon"', 250),
    ("Ohio State University", '"Ohio State University"', 250),
    ("Purdue University", '"Purdue University"', 250),
    ("Pennsylvania State University", '"Pennsylvania State University" OR "Penn State"', 250),
    ("Iowa State University", '"Iowa State University"', 250),
    ("University of Illinois Urbana-Champaign", '"Illinois at Urbana-Champaign" OR "Illinois Urbana-Champaign"', 250),
    ("University of Minnesota", '"University of Minnesota"', 300),
    ("Vanderbilt University", '"Vanderbilt University"', 250),
    ("University of Rochester", '"University of Rochester"', 200),
    ("University at Buffalo (SUNY)", '"University at Buffalo"', 200),
    ("University of Iowa", '"University of Iowa"', 200),
    ("New York University", '"New York University"', 250),
    ("Northwestern University", '"Northwestern University"', 200),
    ("London School of Hygiene & Tropical Medicine", '"London School of Hygiene"', 200),
]

PHD_ROLE_RE = re.compile(
    r"\b(ph\.?\s*d\.?|doctor\s+of\s+philosophy|sc\.?\s*d\.?|doctor\s+of\s+science|dr\.?\s*p\.?\s*h\.?|d\.?\s*phil\.?|doctoral)\b",
    re.I,
)
MS_ROLE_REJECT_RE = re.compile(
    r"\b(master|m\.?\s*s\.?|m\.?\s*a\.?|sc\.?\s*m\.?|m\.?\s*p\.?\s*h\.?|m\.?\s*h\.?\s*s\.?|bachelor|b\.?\s*s\.?|b\.?\s*a\.?|postdoc\w*|fellow|professor|visiting|intern)\b",
    re.I,
)
STAT_DEPT_OR_ROLE_RE = re.compile(
    r"\b(biostat\w*|statist\w*|probab\w*|applied\s+mathematics\s+and\s+statistics|data\s+science|"
    r"quantitative\s+health|stochastic\w*|actuar\w*|operations\s+research|econometr\w*|epidemiolog\w*)\b",
    re.I,
)


def school_matches_org(canon_school: str, org_name: str) -> bool:
    if not org_name:
        return False
    ol = fold_ascii(org_name).lower()
    cl = fold_ascii(canon_school).lower()
    if "hopkins" in cl:
        return "hopkins" in ol
    if "harvard" in cl:
        return "harvard" in ol
    if "north carolina at chapel hill" in cl:
        return ("chapel hill" in ol or "gillings" in ol or org_name.strip() == "UNC")
    if "north carolina state" in cl:
        return ("north carolina state" in ol or "nc state" in ol)
    if "texas a&m" in cl:
        return "texas a" in ol and "m" in ol
    if "buffalo" in cl:
        return "buffalo" in ol
    if "illinois urbana" in cl:
        return "urbana" in ol
    if "pennsylvania state" in cl:
        return ("pennsylvania state" in ol or "penn state" in ol)
    if "hygiene" in cl:
        return "hygiene" in ol
    # General check: main distinctive tokens match
    stop = {"university", "of", "the", "at", "in", "and", "college", "school", "institute"}
    toks = [t for t in re.findall(r"[a-z]+", cl) if t not in stop]
    return bool(toks) and all(t in ol for t in toks)


def harvest_orcid_university(school_tuple, school_faculty_sigs: dict):
    """
    Queries ORCID Expanded Search for `school_tuple`, fetches `/activities` for matching scholars,
    verifies explicit PhD education at `canon_school` in a statistical/biostatistical department,
    and resolves the doctoral advisor via Crossref Polite Pool co-authorship during the PhD window!
    """
    canon_school, org_q, max_rows = school_tuple
    sess = requests.Session()
    orcid_candidates = []

    for start in range(0, max_rows, 200):
        try:
            r = sess.get(
                "https://pub.orcid.org/v3.0/expanded-search/",
                params={
                    "q": f'affiliation-org-name:({org_q}) AND (biostatistics OR statistics OR "applied mathematics and statistics" OR "statistical science")',
                    "start": start,
                    "rows": min(200, max_rows - start),
                },
                headers={"Accept": "application/json"},
                timeout=15,
            )
            if r.status_code != 200:
                break
            batch = r.json().get("expanded-result") or []
            if not batch:
                break
            for item in batch:
                oid = item.get("orcid-id")
                gn = (item.get("given-names") or "").strip()
                fn = (item.get("family-names") or "").strip()
                nm = clean_person_name(f"{gn} {fn}")
                if oid and nm:
                    orcid_candidates.append((oid, nm))
            if len(batch) < 200:
                break
        except Exception:
            break

    if not orcid_candidates:
        return []

    fac_map = school_faculty_sigs.get(canon_school, {})  # (first_init, last_tok) -> [(prof_id, prof_name, full_first)]

    def inspect_orcid_scholar(cand):
        oid, nm = cand
        try:
            ra = requests.get(
                f"https://pub.orcid.org/v3.0/{oid}/activities",
                headers={"Accept": "application/json"},
                timeout=10,
            )
            if ra.status_code != 200:
                return None
            act = ra.json()
            ed_groups = ((act.get("educations") or {}).get("affiliation-group") or []) + (
                (act.get("qualifications") or {}).get("affiliation-group") or []
            )
            matched_phd = None
            for grp in ed_groups:
                for s in grp.get("summaries") or []:
                    es = s.get("education-summary") or s.get("qualification-summary") or {}
                    org = ((es.get("organization") or {}).get("name") or "").strip()
                    if not school_matches_org(canon_school, org):
                        continue
                    role = (es.get("role-title") or "").strip()
                    dept = (es.get("department-name") or "").strip()
                    if not PHD_ROLE_RE.search(role):
                        continue
                    if MS_ROLE_REJECT_RE.search(role) and not re.search(r"\bph\.?\s*d\b", role, re.I):
                        continue
                    combo = f"{role} {dept}"
                    if not STAT_DEPT_OR_ROLE_RE.search(combo):
                        continue
                    sy_s = ((es.get("start-date") or {}).get("year") or {}).get("value")
                    ey_s = ((es.get("end-date") or {}).get("year") or {}).get("value")
                    sy = int(sy_s) if sy_s and str(sy_s).isdigit() else None
                    ey = int(ey_s) if ey_s and str(ey_s).isdigit() else None
                    # Skip future expected graduation years > 2026 so we keep completed PhDs clean (or allow <=2026)
                    if ey and ey > 2026:
                        ey = None
                    field = "Biostatistics" if re.search(r"\bbiostat\w*|epidemiolog\w*|public\s+health", combo, re.I) else (
                        "Probability" if re.search(r"\bprobab\w*|stochastic", combo, re.I) else "Statistics"
                    )
                    matched_phd = (sy, ey, dept, field)
                    break
                if matched_phd:
                    break

            if not matched_phd:
                return None

            sy, ey, dept, field = matched_phd

            # Try to resolve doctoral advisor at `canon_school` via Crossref ORCID works during PhD window!
            inferred_advisor_name = ""
            if fac_map:
                try:
                    rc = requests.get(
                        f"https://api.crossref.org/works?filter=orcid:{oid}&rows=18",
                        headers=POLITE_HEADERS,
                        timeout=9,
                    )
                    if rc.status_code == 200:
                        works = (rc.json().get("message") or {}).get("items") or []
                        mentor_votes = Counter()
                        stu_nk = norm_key(nm)
                        min_w_yr = (sy - 1) if sy else ((ey - 5) if ey else 2012)
                        max_w_yr = (ey + 2) if ey else 2027

                        for w in works:
                            w_yr = ((w.get("issued") or {}).get("date-parts") or [[None]])[0][0]
                            if w_yr and not (min_w_yr <= w_yr <= max_w_yr):
                                continue
                            auths = w.get("author") or []
                            if not (2 <= len(auths) <= 12):
                                continue
                            for idx_a, a_obj in enumerate(auths):
                                ag = (a_obj.get("given") or "").strip()
                                af = (a_obj.get("family") or "").strip()
                                if not ag or not af:
                                    continue
                                ank = norm_key(f"{ag} {af}")
                                if ank == stu_nk:
                                    continue
                                sig = (fold_ascii(ag[0]).lower(), fold_ascii(af).lower().split()[-1])
                                if sig in fac_map:
                                    for _, prof_nm, prof_first in fac_map[sig]:
                                        ag_first = fold_ascii(ag.split()[0]).lower().rstrip(".")
                                        if len(ag_first) == 1 or prof_first.startswith(ag_first) or ag_first.startswith(prof_first):
                                            # Weight last/senior author and 2-3 author dissertation papers highest!
                                            wt = 3 if idx_a == len(auths) - 1 else 1
                                            if len(auths) <= 3:
                                                wt += 2
                                            mentor_votes[prof_nm] += wt

                        if mentor_votes:
                            top_prof, top_wt = mentor_votes.most_common(1)[0]
                            if top_wt >= 4:  # Requires either a 2-3 author senior paper or >=2 co-authored PhD papers!
                                inferred_advisor_name = top_prof
                except Exception:
                    pass

            return {
                "student": nm,
                "advisors": [inferred_advisor_name] if inferred_advisor_name else [],
                "school": canon_school,
                "country": "United Kingdom" if "London" in canon_school else "United States",
                "year": ey,
                "thesis": "",
                "subjects": dept or field,
                "field": field,
                "orcid": oid,
                "source": "ORCID + Crossref" if inferred_advisor_name else "ORCID Verified PhD",
            }
        except Exception:
            return None

    verified_phds = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=14) as pool:
        for res in pool.map(inspect_orcid_scholar, orcid_candidates):
            if res:
                verified_phds.append(res)

    linked_cnt = sum(1 for x in verified_phds if x["advisors"])
    print(f"    -> ORCID {canon_school}: verified {len(verified_phds):,} Stat/Biostat PhDs ({linked_cnt} advisor-linked!) out of {len(orcid_candidates)} profiles")
    return verified_phds


def main():
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # -----------------------------------------------------------------------
    # Stage 1A: Canonicalize Fragmented School Spellings in SQLite
    # -----------------------------------------------------------------------
    print("[1/5] Fixing fragmented school names & merging MGP<->AFT split faculty records...")
    fixed_sch_rows = 0
    for bad_sch, good_sch in EXTRA_SCHOOL_FIXES.items():
        cur.execute("UPDATE people SET school = ? WHERE school = ?", (good_sch, bad_sch))
        fixed_sch_rows += cur.rowcount
        cur.execute("UPDATE people SET current_affiliation = ? WHERE current_affiliation = ?", (good_sch, bad_sch))
        cur.execute("UPDATE OR IGNORE advisor_schools SET school = ? WHERE school = ?", (good_sch, bad_sch))
        cur.execute("DELETE FROM advisor_schools WHERE school = ?", (bad_sch,))
        cur.execute("DELETE FROM schools WHERE school = ?", (bad_sch,))
    print(f"      Unified {fixed_sch_rows:,} fragmented university records (Florida, Texas A&M, UTHealth, Buffalo, JHU, etc.).")

    # Stage 1B: Merge exact-name MGP <-> AFT faculty stubs (where AFT has year IS NULL and MGP has PhD record!)
    split_pairs = cur.execute("""
        SELECT p1.id AS mgp_uid, p1.aft_pid AS p1_aft, p2.id AS aft_uid, p2.aft_pid AS p2_aft,
               p2.school AS aft_loc, p2.current_affiliation AS aft_affil, p2.research_areas, p2.orcid, p2.homepage,
               p1.sources AS s1, p2.sources AS s2
        FROM people p1
        JOIN people p2 ON p1.norm_name = p2.norm_name AND p1.id != p2.id
        WHERE p1.mgp_id IS NOT NULL
          AND p2.mgp_id IS NULL AND p2.aft_pid IS NOT NULL AND p2.year IS NULL
          AND (p1.is_broad_stat = 1 OR p2.is_broad_stat = 1)
    """).fetchall()

    seen_mgp_u = set()
    seen_aft_u = set()
    merged_splits = 0
    for sp in split_pairs:
        kid, did = sp["mgp_uid"], sp["aft_uid"]
        if kid in seen_mgp_u or did in seen_aft_u:
            continue
        # Only merge if single unambiguous pair
        seen_mgp_u.add(kid)
        seen_aft_u.add(did)
        affil = sp["aft_affil"] or sp["aft_loc"] or ""
        srcs = ",".join(sorted(set((sp["s1"] + "," + sp["s2"]).split(","))))
        p2_aft = sp["p2_aft"] if sp["p1_aft"] is None else None

        cur.execute("UPDATE OR IGNORE edges SET advisor_id = ? WHERE advisor_id = ? AND student_id != ?", (kid, did, kid))
        cur.execute("UPDATE OR IGNORE edges SET student_id = ? WHERE student_id = ? AND advisor_id != ?", (kid, did, kid))
        cur.execute("DELETE FROM edges WHERE advisor_id = ? OR student_id = ?", (did, did))
        cur.execute("DELETE FROM advisor_schools WHERE person_id = ?", (did,))
        cur.execute("DELETE FROM people WHERE id = ?", (did,))
        cur.execute(
            """
            UPDATE people SET
                aft_pid = COALESCE(aft_pid, ?),
                current_affiliation = CASE WHEN current_affiliation = '' THEN ? ELSE current_affiliation END,
                research_areas = CASE WHEN research_areas = '' THEN ? ELSE research_areas END,
                orcid = CASE WHEN orcid = '' THEN ? ELSE orcid END,
                homepage = CASE WHEN homepage = '' THEN ? ELSE homepage END,
                is_core_stat = CASE WHEN is_core_stat = 1 THEN 1 ELSE (SELECT CASE WHEN ? LIKE '%Biostat%' THEN 1 ELSE is_core_stat END) END,
                sources = ?
            WHERE id = ?
            """,
            (p2_aft, affil, sp["research_areas"] or "", sp["orcid"] or "", sp["homepage"] or "", sp["research_areas"] or "", srcs, kid),
        )
        if affil:
            cur.execute("INSERT OR IGNORE INTO advisor_schools VALUES (?, ?, 0)", (kid, affil))
        merged_splits += 1

    print(f"      Merged {merged_splits:,} split MGP <-> AFT professor stubs (e.g. Mei-Cheng Wang, Hongkai Ji, Laurent Younes)!")

    # Stage 1C: Reclaim AFT `relation=1` PhDs (including `neuro`, `bme`, `etree`, `computerscience`) whose mentor is a verified Core Stat/Biostat/Prob professor!
    aft_pid_to_uid = {
        r[0]: (r[1], r[2])
        for r in cur.execute("SELECT aft_pid, id, primary_field FROM people WHERE aft_pid IS NOT NULL AND is_core_stat = 1").fetchall()
    }
    aft_all_pid_to_uid = {
        r[0]: r[1] for r in cur.execute("SELECT aft_pid, id FROM people WHERE aft_pid IS NOT NULL").fetchall()
    }

    aft_people_lookup = {}
    with gzip.open(os.path.join(RAW_DIR, "aft_people.csv.gz"), "rt", encoding="utf-8", errors="replace") as gz:
        for row in csv.DictReader(gz):
            if row["pid"].isdigit():
                pid = int(row["pid"])
                fn = (row.get("firstname") or "").strip(" ,")
                mn = (row.get("middlename") or "").strip(" ,")
                ln = (row.get("lastname") or "").strip(" ,")
                nm = " ".join(x for x in [fn, mn, ln] if x and x != "NULL")
                aft_people_lookup[pid] = (nm, row.get("orcid_id") or "", row.get("homepage") or "", row.get("area") or "")

    reclaimed_aft_students = 0
    reclaimed_aft_edges = 0
    next_id = (cur.execute("SELECT MAX(id) FROM people").fetchone()[0] or 7_000_000) + 100
    existing_edges = set((r[0], r[1]) for r in cur.execute("SELECT advisor_id, student_id FROM edges").fetchall())

    with gzip.open(os.path.join(RAW_DIR, "aft_connect.csv.gz"), "rt", encoding="utf-8", errors="replace") as gz:
        for row in csv.DictReader(gz):
            if row.get("relation") != "1":
                continue
            if not (row["pid1"].isdigit() and row["pid2"].isdigit()):
                continue
            p1, p2 = int(row["pid1"]), int(row["pid2"])
            if p2 not in aft_pid_to_uid or p1 not in aft_people_lookup:
                continue
            adv_uid, adv_field = aft_pid_to_uid[p2]
            loc = canonicalize_school(row.get("location") or "")
            if loc in EXTRA_SCHOOL_FIXES:
                loc = EXTRA_SCHOOL_FIXES[loc]
            sd = (row.get("stopdate") or "")[:4]
            yr = int(sd) if sd.isdigit() and 1850 <= int(sd) <= 2027 else None
            dt = (row.get("dissertation_title") or "").strip()
            if dt == "NULL":
                dt = ""
            pqid = (row.get("pqid") or "").strip()
            if pqid in ("NULL", "0"):
                pqid = ""

            if p1 in aft_all_pid_to_uid:
                stu_uid = aft_all_pid_to_uid[p1]
            else:
                nm, orc, hp, area = aft_people_lookup[p1]
                nm_clean = clean_person_name(nm)
                if not nm_clean:
                    continue
                stu_uid = next_id
                next_id += 1
                nk = norm_key(nm_clean)
                cur.execute(
                    """INSERT INTO people (id, name, norm_name, search_aliases, school, raw_school, country, year, thesis,
                       mgp_subject, primary_field, is_core_stat, is_broad_stat, field_confidence, classification_reason,
                       current_affiliation, research_areas, mgp_id, aft_pid, wikidata_qid, orcid, homepage, proquest_id,
                       sources, advisor_count, student_count, descendant_count, stat_descendant_count)
                       VALUES (?,?,?,?,?,'','',?,?,NULL,?,1,1,0.86,'Advised by Core Stat/Biostat Faculty','',?,NULL,?,'',?,?,?,'AFT',0,0,0,0)""",
                    (stu_uid, nm_clean, nk, "", loc, yr, dt, adv_field, area if area != "NULL" else "", p1, orc if orc != "NULL" else "", hp if hp != "NULL" else "", pqid),
                )
                aft_all_pid_to_uid[p1] = stu_uid
                reclaimed_aft_students += 1

            if adv_uid != stu_uid and (adv_uid, stu_uid) not in existing_edges and (stu_uid, adv_uid) not in existing_edges:
                cur.execute("INSERT OR IGNORE INTO edges VALUES (?, ?, 'AFT')", (adv_uid, stu_uid))
                existing_edges.add((adv_uid, stu_uid))
                reclaimed_aft_edges += 1

    aft_people_lookup.clear()
    print(f"      Reclaimed +{reclaimed_aft_students:,} interdisciplinary AFT PhD advisees and +{reclaimed_aft_edges:,} edges under Core Stat/Biostat mentors!")

    # Stage 1D: Promote non-core PhDs advised by Core Stat/Biostat/Prob faculty whose dissertation clearly uses statistical/probabilistic/ML methods
    cur.execute("""
        UPDATE people
        SET primary_field = COALESCE(
                (SELECT a.primary_field FROM edges e JOIN people a ON a.id = e.advisor_id WHERE e.student_id = people.id AND a.is_core_stat = 1 LIMIT 1),
                'Statistics'
            ),
            is_core_stat = 1,
            is_broad_stat = 1,
            classification_reason = 'Core Stat/Biostat Advisor + Quantitative Thesis'
        WHERE is_core_stat = 0
          AND EXISTS (SELECT 1 FROM edges e JOIN people a ON a.id = e.advisor_id WHERE e.student_id = people.id AND a.is_core_stat = 1)
          AND (
              LOWER(thesis) LIKE '%statist%' OR LOWER(thesis) LIKE '%probab%' OR LOWER(thesis) LIKE '%stochastic%'
              OR LOWER(thesis) LIKE '%nonparametric%' OR LOWER(thesis) LIKE '%bayesian%' OR LOWER(thesis) LIKE '%regression%'
              OR LOWER(thesis) LIKE '%inference%' OR LOWER(thesis) LIKE '%markov%' OR LOWER(thesis) LIKE '%random%'
              OR LOWER(thesis) LIKE '%learning%' OR LOWER(thesis) LIKE '%estimat%' OR LOWER(thesis) LIKE '%causal%'
              OR LOWER(thesis) LIKE '%longitudinal%' OR LOWER(thesis) LIKE '%biostat%' OR LOWER(thesis) LIKE '%genom%'
          )
    """)
    print(f"      Promoted {cur.rowcount:,} quantitative PhDs advised by Core Stat/Biostat/Prob professors (e.g. JHU AMS students of Fei Lu, Carey Priebe)!")

    # Fix known JHU Biostatistics / AMS faculty affiliations & core status so ORCID Crossref matching has complete JHU faculty roster!
    jhu_core_faculty_fixes = [
        "Jeffrey T. Leek", "Ciprian Mugur Crainiceanu", "Martin Lindquist", "Elizabeth Anne Stuart",
        "Michael Aaron Rosenblum", "Abhirup Datta", "Stephanie C. Hicks", "Kasper Daniel Hansen",
        "Mei-Cheng Wang", "Donald Jay Geman", "Laurent Younes", "Mauro Maggioni", "Jeremias Sulam",
        "Joshua T. Vogelstein", "Rene Vidal", "Fei Lu", "Daniel Quitt Naiman", "Carey E. Priebe",
        "Brian Scott Caffo", "Karen Jean Bandeen-Roche", "Ingo Ruczinski", "Hongkai Ji",
        "Vadim Victorovich Zipunnikov", "Nilanjan Chatterjee", "Scott L. Zeger", "Kung-Yee Liang",
    ]
    for jnm in jhu_core_faculty_fixes:
        nk = norm_key(jnm)
        cur.execute(
            """UPDATE people SET
                current_affiliation = 'Johns Hopkins University',
                is_core_stat = 1,
                is_broad_stat = 1,
                primary_field = CASE WHEN primary_field IN ('Statistics','Biostatistics','Probability') THEN primary_field ELSE 'Biostatistics' END
               WHERE norm_name = ?""",
            (nk,),
        )
        for row_id in cur.execute("SELECT id FROM people WHERE norm_name = ?", (nk,)).fetchall():
            cur.execute("INSERT OR IGNORE INTO advisor_schools VALUES (?, 'Johns Hopkins University', 0)", (row_id[0],))
    conn.commit()

    # -----------------------------------------------------------------------
    # Stage 2: Harvest Newly Unlocked School of Public Health DigitalCommons OAI Feeds
    # -----------------------------------------------------------------------
    print("\n[2/5] Harvesting 5 School of Public Health DigitalCommons OAI feeds (UTHealth, VCU, USF, UAB, MUSC)...")
    harvested_new = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        for res in pool.map(lambda x: harvest_digitalcommons_oai(*x), SPH_DIGITALCOMMONS_REPOS):
            harvested_new.extend(res)

    # -----------------------------------------------------------------------
    # Stage 3: Crawl External Lab Websites of JHU & Peer Biostatistics Labs
    # -----------------------------------------------------------------------
    print("\n[3/5] Crawling external Biostatistics lab websites (Jeff Leek, Stephanie Hicks, Kasper Hansen, NeuroData, RafaLab, Sulam)...")
    ext_labs = [
        ("Jeffrey T. Leek", "Johns Hopkins University", "https://jtleek.com/"),
        ("Stephanie C. Hicks", "Johns Hopkins University", "https://www.stephaniehicks.com/"),
        ("Kasper Daniel Hansen", "Johns Hopkins University", "https://www.hansenlab.org/"),
        ("Joshua T. Vogelstein", "Johns Hopkins University", "https://neurodata.io/about/"),
        ("Jeremias Sulam", "Johns Hopkins University", "https://jsulam.github.io/"),
        ("Rafael A. Irizarry", "Harvard University", "https://rafalab.dfci.harvard.edu/"),
    ]
    for pnm, psch, purl in ext_labs:
        try:
            r = requests.get(purl, headers=HEADERS, timeout=8)
            if r.status_code == 200:
                stus = parse_html_group_page(r.text, pnm, psch)
                for s in stus:
                    harvested_new.append({
                        "student": s["student"],
                        "advisors": [pnm],
                        "school": psch,
                        "country": "United States",
                        "year": s.get("year"),
                        "thesis": s.get("thesis") or "",
                        "subjects": "Biostatistics",
                        "field": "Biostatistics",
                        "orcid": "",
                        "source": "Faculty Lab Website",
                    })
                soup = BeautifulSoup(r.text, "lxml")
                for a in soup.find_all("a", href=True):
                    if any(w in (a.get_text() + a["href"]).lower() for w in ["people", "team", "member", "alumni", "student"]):
                        fu = requests.compat.urljoin(r.url, a["href"])
                        if purl.split("/")[2] in fu and fu != r.url:
                            sr = requests.get(fu, headers=HEADERS, timeout=7)
                            if sr.status_code == 200:
                                for s in parse_html_group_page(sr.text, pnm, psch):
                                    harvested_new.append({
                                        "student": s["student"],
                                        "advisors": [pnm],
                                        "school": psch,
                                        "country": "United States",
                                        "year": s.get("year"),
                                        "thesis": s.get("thesis") or "",
                                        "subjects": "Biostatistics",
                                        "field": "Biostatistics",
                                        "orcid": "",
                                        "source": "Faculty Lab Website",
                                    })
                            break
        except Exception:
            pass

    # -----------------------------------------------------------------------
    # Stage 4: Large-Scale ORCID Public API Harvester + Crossref Advisor Linker
    #          across JHU and 31 Peer Post-2013 Gap Universities!
    # -----------------------------------------------------------------------
    print(f"\n[4/5] Harvesting ORCID verified PhDs + Crossref doctoral advisors across {len(ORCID_TARGET_UNIVERSITIES)} universities...")

    # Build per-university faculty signature index `(first_init, last_tok) -> [(id, name, first_tok)]`
    school_faculty_sigs = defaultdict(lambda: defaultdict(list))
    for r in cur.execute("""
        SELECT DISTINCT p.id, p.name, a.school
        FROM advisor_schools a
        JOIN people p ON p.id = a.person_id
        WHERE p.is_broad_stat = 1
    """):
        ft, lt = first_last_sig(r["name"])
        if ft and lt:
            school_faculty_sigs[r["school"]][(ft[0], lt)].append((r["id"], r["name"], ft))

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for batch in pool.map(lambda st: harvest_orcid_university(st, school_faculty_sigs), ORCID_TARGET_UNIVERSITIES):
            harvested_new.extend(batch)

    print(f"\nTotal new records collected across SPH OAI, Lab Sites & 32-School ORCID/Crossref Harvester: {len(harvested_new):,}")

    # -----------------------------------------------------------------------
    # Stage 5: Strict Same-School Upsert, Rebuild `advisor_schools`, Schools & FTS5
    # -----------------------------------------------------------------------
    print("[5/5] Integrating all newly discovered PhDs & advisor edges into SQLite + FTS5...")

    existing_by_norm = defaultdict(list)
    existing_by_orcid = {}
    for r in cur.execute("SELECT id, name, norm_name, school, current_affiliation, year, thesis, primary_field, is_core_stat, is_broad_stat, orcid, sources FROM people"):
        d = dict(r)
        existing_by_norm[d["norm_name"]].append(d)
        if d["orcid"]:
            existing_by_orcid[d["orcid"]] = d

    added_p = 0
    added_e = 0
    enriched_p = 0

    for item in harvested_new:
        stu_nm = item["student"]
        sch = canonicalize_school(item["school"])
        if sch in EXTRA_SCHOOL_FIXES:
            sch = EXTRA_SCHOOL_FIXES[sch]
        yr = item.get("year")
        th = item.get("thesis") or ""
        oid = item.get("orcid") or ""
        fld = item.get("field") or infer_field(th, item.get("subjects") or "", "Biostatistics" if "biostat" in (item.get("subjects") or "").lower() else "Statistics")
        src = item.get("source") or "ORCID Verified PhD"

        # Match existing student by ORCID first, or strict same-school name match
        matched = existing_by_orcid.get(oid) if oid else None
        if not matched:
            snk = norm_key(stu_nm)
            for c in existing_by_norm.get(snk, ()):
                if c["school"] == sch or (not c["school"] and c["current_affiliation"] == sch):
                    if not (yr and c["year"] and abs(yr - c["year"]) > 4):
                        matched = c
                        break

        if matched:
            sid = matched["id"]
            cur.execute(
                """UPDATE people SET
                    school = CASE WHEN school = '' THEN ? ELSE school END,
                    year = COALESCE(year, ?),
                    thesis = CASE WHEN thesis = '' THEN ? ELSE thesis END,
                    orcid = CASE WHEN orcid = '' THEN ? ELSE orcid END,
                    is_core_stat = 1,
                    is_broad_stat = 1,
                    primary_field = CASE WHEN primary_field IN ('Statistics','Biostatistics','Probability') THEN primary_field ELSE ? END
                   WHERE id = ?""",
                (sch, yr, th, oid, fld, sid),
            )
            enriched_p += 1
        else:
            sid = next_id
            next_id += 1
            snk = norm_key(stu_nm)
            ic = 1 if fld in ("Statistics", "Biostatistics", "Probability") else 0
            cur.execute(
                """INSERT INTO people (
                    id, name, norm_name, search_aliases, school, raw_school, country, year, thesis,
                    mgp_subject, primary_field, is_core_stat, is_broad_stat, field_confidence,
                    classification_reason, current_affiliation, research_areas, mgp_id, aft_pid,
                    wikidata_qid, orcid, homepage, proquest_id, sources, advisor_count, student_count,
                    descendant_count, stat_descendant_count
                ) VALUES (?,?,?,?,?,'',?,?,?,NULL,?,?,1,0.92,?,'',?,NULL,NULL,'',?,'','',?,0,0,0,0)""",
                (sid, stu_nm, snk, "", sch, item.get("country") or "United States", yr, th, fld, ic, f"Verified via {src}", item.get("subjects") or "", oid, src),
            )
            new_d = {
                "id": sid, "name": stu_nm, "norm_name": snk, "school": sch, "current_affiliation": "",
                "year": yr, "thesis": th, "primary_field": fld, "is_core_stat": ic, "is_broad_stat": 1,
                "orcid": oid, "sources": src,
            }
            existing_by_norm[snk].append(new_d)
            if oid:
                existing_by_orcid[oid] = new_d
            added_p += 1

        # Link resolved advisor(s)
        for anm in (item.get("advisors") or []):
            if not anm:
                continue
            ank = norm_key(anm)
            adv_cands = [c for c in existing_by_norm.get(ank, ()) if c["id"] != sid]
            adv_chosen = None
            for ac in adv_cands:
                if ac["current_affiliation"] == sch or ac["school"] == sch or ac["is_core_stat"]:
                    adv_chosen = ac
                    break
            if not adv_chosen and adv_cands:
                adv_chosen = adv_cands[0]
            if not adv_chosen:
                aid = next_id
                next_id += 1
                cur.execute(
                    """INSERT INTO people (
                        id, name, norm_name, search_aliases, school, raw_school, country, year, thesis,
                        mgp_subject, primary_field, is_core_stat, is_broad_stat, field_confidence,
                        classification_reason, current_affiliation, research_areas, mgp_id, aft_pid,
                        wikidata_qid, orcid, homepage, proquest_id, sources, advisor_count, student_count,
                        descendant_count, stat_descendant_count
                    ) VALUES (?,?,?,'','','','',NULL,'',NULL,?,1,1,0.9,'Advisor via ETD/ORCID',?,'',NULL,NULL,'','','','',?,0,0,0,0)""",
                    (aid, anm, ank, fld, sch, src),
                )
                adv_chosen = {"id": aid, "name": anm, "norm_name": ank, "school": "", "current_affiliation": sch, "year": None, "thesis": "", "primary_field": fld, "is_core_stat": 1, "is_broad_stat": 1, "orcid": "", "sources": src}
                existing_by_norm[ank].append(adv_chosen)
                added_p += 1

            aid = adv_chosen["id"]
            if aid != sid and (aid, sid) not in existing_edges and (sid, aid) not in existing_edges:
                cur.execute("INSERT OR IGNORE INTO edges VALUES (?, ?, ?)", (aid, sid, src))
                existing_edges.add((aid, sid))
                added_e += 1

    print(f"      Added +{added_p:,} net-new PhDs, +{added_e:,} advisor-student edges, and enriched {enriched_p:,} existing records!")

    # Rebuild advisor_schools, DAG metrics, schools table, meta_stats & FTS5
    cur.execute("""
        INSERT OR REPLACE INTO advisor_schools (person_id, school, advised_at_school)
        SELECT e.advisor_id, s.school, COUNT(*)
        FROM edges e JOIN people s ON s.id = e.student_id
        WHERE s.school != '' GROUP BY e.advisor_id, s.school
    """)
    cur.execute("""
        INSERT OR IGNORE INTO advisor_schools (person_id, school, advised_at_school)
        SELECT id, current_affiliation, 0 FROM people WHERE current_affiliation != ''
    """)

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
        st_dfs, bad = {}, []
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
        "total_schools": cur.execute("SELECT COUNT(*) FROM schools").fetchone()[0],
        "core_stat_schools": cur.execute("SELECT COUNT(*) FROM schools WHERE core_stat_count > 0").fetchone()[0],
        "decade_histogram": dict(sorted(decade_hist.items())),
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    cur.execute("INSERT OR REPLACE INTO meta_stats VALUES ('summary', ?)", (json.dumps(meta),))
    cur.execute("INSERT INTO people_fts(people_fts) VALUES('rebuild')")
    conn.commit()

    jhu_row = cur.execute("SELECT * FROM schools WHERE school = 'Johns Hopkins University'").fetchone()
    jhu_post14 = cur.execute("SELECT COUNT(*) FROM people WHERE school = 'Johns Hopkins University' AND is_core_stat = 1 AND year >= 2014").fetchone()[0]
    jhu_no_yr = cur.execute("SELECT COUNT(*) FROM people WHERE school = 'Johns Hopkins University' AND is_core_stat = 1 AND year IS NULL").fetchone()[0]
    print(f"\nJHU Updated Stats: {dict(jhu_row)} | Core >=2014: {jhu_post14} | Core (year unlisted): {jhu_no_yr}")
    conn.close()
    print(f"GAP-CLOSING PIPELINE DONE in {time.time() - t0:.1f}s!\nSummary: {json.dumps(meta, indent=2)}")


if __name__ == "__main__":
    main()
