#!/usr/bin/env python3
"""
Fast DataCite Global Dissertation Harvester (2014–2025)
(`scripts/harvest_datacite_2014_2025_dissertations.py`).

Paginates DataCite's open DOI registry (`types.resourceTypeGeneral:Dissertation` across Columbia
AcademicCommons, UNC CDR, UMich Deep Blue, Purdue Hammer, CMU KiltHub, EPFL, ETH Zurich, Imperial,
UCL, Waterloo, Monash, etc.) at 500 records/page to fill remaining 2014–2025 doctoral theses and
`Supervisor` / `ThesisAdvisor` links.
"""

import json
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict

import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "scripts"))

from build_database import canonicalize_school
from close_jhu_and_peer_gaps import EXTRA_SCHOOL_FIXES
from complete_2014_2025_gap_and_aft_precision import (
    GENUINE_STAT_THESIS_BROAD_RE,
    NON_STAT_THESIS_NOISE_RE,
    REPO_SUBSTRING_MAP,
)
from fill_post2014_drop import (
    BIOSTAT_TITLE_RE,
    CORE_STAT_TITLE_RE,
    NON_PHD_REJECT_RE,
    NON_STAT_DOMAIN_REJECT_RE,
    PROB_TITLE_RE,
)
from harvest_web_and_etds import clean_person_name, first_last_sig, norm_key

DB_PATH = os.path.join(BASE_DIR, "data", "stat_genealogy.db")


def resolve_dc_publisher(pub: str, known_schools: set) -> str:
    if not pub:
        return ""
    pl = pub.lower().strip()
    if "epfl" in pl:
        return "École Polytechnique Fédérale de Lausanne"
    for sub, mapped in REPO_SUBSTRING_MAP:
        if sub in pl:
            return mapped
    c = canonicalize_school(pub.strip())
    c = EXTRA_SCHOOL_FIXES.get(c, c)
    if c in known_schools or re.search(r"\b(University|Universität|Université|Universidad|Universiteit|Universidade|ETH|Institut\w*|College\s+London)\b", c, re.I):
        if not any(b in c.lower() for b in ["zenodo", "figshare", "researchgate", "arxiv", "proquest", "press", "springer", "elsevier"]):
            return c
    return ""


def main():
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    known_schools = {r[0] for r in cur.execute("SELECT school FROM schools WHERE total_phd_count >= 3").fetchall()}
    sess = requests.Session()
    sess.headers.update({"User-Agent": "StatLineage/1.0"})

    q = (
        'types.resourceTypeGeneral:Dissertation AND publicationYear:[2014 TO 2025] AND ('
        'titles.title:biostatistics OR titles.title:statistics OR titles.title:statistical OR '
        'titles.title:bayesian OR titles.title:semiparametric OR titles.title:nonparametric OR '
        'titles.title:"survival analysis" OR titles.title:"causal inference" OR '
        'titles.title:"quantile regression" OR titles.title:"high-dimensional" OR '
        'titles.title:"time series" OR titles.title:martingale OR titles.title:"stochastic differential" OR '
        'titles.title:"markov chain" OR titles.title:"random matrices" OR titles.title:"empirical bayes"'
        ')'
    )

    next_url = "https://api.datacite.org/dois"
    params = {"query": q, "page[size]": 500, "page[cursor]": "1"}
    pages = 0
    out = []
    seen_dois = set()

    print("[1/2] Paginating DataCite 2014–2025 Global Dissertations at 500/page...", flush=True)
    while next_url and pages < 20:
        pages += 1
        try:
            r = sess.get(next_url, params=params, timeout=20)
            params = None  # Subsequent pages use `links.next` directly
            if r.status_code != 200:
                break
            body = r.json()
            data = body.get("data") or []
            if not data:
                break
            next_url = (body.get("links") or {}).get("next")

            for item in data:
                doi = item.get("id")
                if doi in seen_dois:
                    continue
                seen_dois.add(doi)
                attr = item.get("attributes") or {}
                yr = attr.get("publicationYear")
                if not (isinstance(yr, int) and 2014 <= yr <= 2025):
                    continue
                titles = attr.get("titles") or []
                title = (titles[0].get("title") if titles else "") or ""
                title = re.sub(r"\s+", " ", title).strip()
                if len(title) < 16:
                    continue
                if NON_PHD_REJECT_RE.search(title) or NON_STAT_DOMAIN_REJECT_RE.search(title) or NON_STAT_THESIS_NOISE_RE.search(title):
                    continue

                # Check resourceType text to reject Master's / Bachelor's theses
                rt_sub = ((attr.get("types") or {}).get("resourceType") or "").lower()
                if any(b in rt_sub for b in ["master", "bachelor", "undergrad", "honors", "b.s", "m.s"]):
                    continue

                if not CORE_STAT_TITLE_RE.search(title):
                    continue

                creators = attr.get("creators") or []
                if len(creators) != 1:
                    continue
                stu_nm = clean_person_name(creators[0].get("name") or "")
                if not stu_nm:
                    continue

                sch = resolve_dc_publisher(attr.get("publisher") or "", known_schools)
                if not sch:
                    # Try creator affiliation
                    affs = creators[0].get("affiliation") or []
                    if affs:
                        aff_str = affs[0].get("name") if isinstance(affs[0], dict) else str(affs[0])
                        sch = resolve_dc_publisher(aff_str, known_schools)
                if not sch:
                    continue

                advisors = []
                for c_obj in (attr.get("contributors") or []):
                    if c_obj.get("contributorType") in ("Supervisor", "Researcher", "ProjectLeader"):
                        anm = clean_person_name(c_obj.get("name") or "")
                        if anm and norm_key(anm) != norm_key(stu_nm):
                            advisors.append(anm)

                fld = "Biostatistics" if BIOSTAT_TITLE_RE.search(title) else (
                    "Probability" if PROB_TITLE_RE.search(title) else "Statistics"
                )
                out.append({
                    "student": stu_nm,
                    "school": sch,
                    "year": yr,
                    "thesis": title,
                    "advisors": advisors[:2],
                    "field": fld,
                    "source": "DataCite ETD",
                })
        except Exception:
            break

    print(f"  -> Extracted {len(out):,} verified 2014–2025 DataCite Stat/Biostat/Prob dissertations across {pages} pages!", flush=True)

    print("[2/2] Integrating into stat_genealogy.db & rebuilding FTS5...", flush=True)
    by_norm_school = defaultdict(list)
    by_fl_school = defaultdict(list)
    fac_by_school_fl = defaultdict(list)

    for r in cur.execute("SELECT id, name, norm_name, school, year, thesis FROM people WHERE school != ''"):
        d = dict(r)
        by_norm_school[(d["norm_name"], d["school"])].append(d)
        f0, l0 = first_last_sig(d["name"])
        if f0 and l0:
            by_fl_school[(f0, l0, d["school"])].append(d)

    for r in cur.execute("""
        SELECT DISTINCT p.id, p.name, p.year, a.school
        FROM advisor_schools a JOIN people p ON p.id = a.person_id
        WHERE p.is_broad_stat = 1
    """):
        f0, l0 = first_last_sig(r["name"])
        if f0 and l0:
            fac_by_school_fl[(r["school"], f0[0], l0)].append((r["id"], r["name"], f0, r["year"]))

    school_country = {r[0]: r[1] for r in cur.execute("SELECT school, country FROM schools WHERE country != ''").fetchall()}
    next_id = (cur.execute("SELECT MAX(id) FROM people").fetchone()[0] or 7_900_000) + 100
    existing_edges = set((r[0], r[1]) for r in cur.execute("SELECT advisor_id, student_id FROM edges").fetchall())

    added_p = 0
    enriched_p = 0
    added_e = 0

    for item in out:
        stu_nm = item["student"]
        snk = norm_key(stu_nm)
        f1, l1 = first_last_sig(stu_nm)
        if not snk or not f1 or len(f1) < 2:
            continue
        sch = item["school"]
        yr = item["year"]
        th = item["thesis"]
        fld = item["field"]
        src = item["source"]

        matched = None
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
                "UPDATE people SET year = COALESCE(year, ?), thesis = CASE WHEN thesis = '' THEN ? ELSE thesis END, is_core_stat = 1, is_broad_stat = 1 WHERE id = ?",
                (yr, th, sid),
            )
            enriched_p += 1
        else:
            sid = next_id
            next_id += 1
            ctry = school_country.get(sch, "")
            cur.execute(
                """INSERT INTO people (
                    id, name, norm_name, search_aliases, school, raw_school, country, year, thesis,
                    mgp_subject, primary_field, is_core_stat, is_broad_stat, field_confidence,
                    classification_reason, current_affiliation, research_areas, mgp_id, aft_pid,
                    wikidata_qid, orcid, homepage, proquest_id, sources, advisor_count, student_count,
                    descendant_count, stat_descendant_count
                ) VALUES (?,?,?,?,?,'',?,?,?,NULL,?,1,1,0.90,?,'','',NULL,NULL,'','','','',?,0,0,0,0)""",
                (sid, stu_nm, snk, "", sch, ctry, yr, th, fld, f"Verified via {src}", src),
            )
            new_d = {"id": sid, "name": stu_nm, "norm_name": snk, "school": sch, "year": yr, "thesis": th}
            by_norm_school[(snk, sch)].append(new_d)
            by_fl_school[(f1, l1, sch)].append(new_d)
            added_p += 1

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
            if aid and aid != sid and (aid, sid) not in existing_edges and (sid, aid) not in existing_edges:
                cur.execute("INSERT OR IGNORE INTO edges VALUES (?, ?, ?)", (aid, sid, src))
                existing_edges.add((aid, sid))
                added_e += 1
                cur.execute("UPDATE people SET student_count = student_count + 1, descendant_count = descendant_count + 1, stat_descendant_count = stat_descendant_count + 1 WHERE id = ?", (aid,))
                cur.execute("UPDATE people SET advisor_count = advisor_count + 1 WHERE id = ?", (sid,))

    print(f"  DataCite Integration: +{added_p:,} net-new PhDs, +{added_e:,} advisor edges, and {enriched_p:,} enriched records!", flush=True)

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

    print("\n=== FINAL ANNUAL CORE COUNTS (2006–2025) ===", flush=True)
    for yr in range(2006, 2026):
        c_yr = cur.execute("SELECT COUNT(*) FROM people WHERE is_core_stat=1 AND year=?", (yr,)).fetchone()[0]
        print(f"  {yr}: {c_yr:,}", flush=True)
    conn.close()
    print(f"DONE in {time.time() - t0:.1f}s!", flush=True)


if __name__ == "__main__":
    main()
