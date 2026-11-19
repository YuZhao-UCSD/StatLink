#!/usr/bin/env python3
"""
Homonym-Safe PubMed Doctoral Lineage Recovery (`scripts/recover_common_name_pubmed_phds.py`).

Fixes the two filtering rules that previously excluded common-named Biostatistics/Statistics PhDs
such as Lei Huang (JHU Biostatistics PhD 2015, advised by Ciprian M. Crainiceanu):
  1. Common-Name Homonym Collision: Previously, if ANY person in the 551k database shared the same
     `norm_name` (e.g. 'Lei Huang' at USC 2002 in Wireless Networks, or 'Ivan Diaz' at another school),
     every student with that name was skipped. Now, external prior PhDs only block rare/unambiguous
     names (`len(fl_matches) <= 2` within 1..20 yrs), while preserving strict same-school faculty &
     same-school prior-PhD guards.
  2. Collaborative Methodology Team Size (`<= 5` authors, or `6` authors with multi-paper departmental
     presence): Captures neuroimaging/genomics methodology theses that include 1-2 clinical collaborators.
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

from harvest_web_and_etds import clean_person_name, first_last_sig, fold_ascii, norm_key
from harvest_pubmed_methodology_lineages import DEPT_AFF_TERMS, DEPT_CHECK_RE, JOURNAL_FILTER, TARGET_SCHOOLS
from clean_pubmed_duplicates_and_postdocs import middle_compatible

DB_PATH = os.path.join(BASE_DIR, "data", "stat_genealogy.db")

# Exact verified dissertation overrides where full thesis metadata was confirmed
VERIFIED_OVERRIDES = {
    ("lei huang", "Johns Hopkins University"): {
        "year": 2015,
        "thesis": "Statistical Analysis in High Dimensional Structured Data",
        "field": "Biostatistics",
    }
}


def main():
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    fac_by_school = defaultdict(dict)
    fac_ids_by_school = defaultdict(set)
    for r in cur.execute("""
        SELECT DISTINCT p.id, p.name, p.year, p.primary_field, a.school
        FROM advisor_schools a JOIN people p ON p.id = a.person_id
        WHERE p.is_broad_stat = 1
          AND (p.student_count >= 1 OR p.sources LIKE '%Roster%' OR (p.year IS NOT NULL AND p.year <= 2020))
        UNION
        SELECT DISTINCT p.id, p.name, p.year, p.primary_field, p.current_affiliation AS school
        FROM people p
        WHERE p.current_affiliation != '' AND p.is_broad_stat = 1
          AND (p.student_count >= 1 OR p.sources LIKE '%Roster%' OR p.year IS NULL OR p.year <= 2020)
    """):
        ft, lt = first_last_sig(r["name"])
        if ft and lt:
            fac_by_school[r["school"]].setdefault((ft[0], lt), []).append(
                (r["id"], r["name"], r["year"], r["primary_field"])
            )
            fac_ids_by_school[r["school"]].add(r["id"])

    by_fl = defaultdict(list)
    for r in cur.execute(
        "SELECT id, name, norm_name, school, current_affiliation, year, is_core_stat, is_broad_stat, student_count FROM people"
    ):
        ft, lt = first_last_sig(r["name"])
        if ft and lt:
            by_fl[(ft, lt)].append(dict(r))

    sess = requests.Session()
    discovered = []

    print(f"[1/2] Running homonym-safe PubMed recovery across {len(TARGET_SCHOOLS)} universities...", flush=True)
    for canon_school, pubmed_org_q, org_substrings in TARGET_SCHOOLS:
        fac_map = fac_by_school.get(canon_school, {})
        fac_ids = fac_ids_by_school.get(canon_school, set())
        if not fac_map:
            continue

        term = f"{DEPT_AFF_TERMS} AND {pubmed_org_q} AND {JOURNAL_FILTER} AND 2011:2026[dp]"
        try:
            es = sess.get(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                params={"db": "pubmed", "term": term, "retmax": 2500, "retmode": "json"},
                timeout=15,
            ).json()
            pmids = (es.get("esearchresult") or {}).get("idlist") or []
            time.sleep(0.34)
        except Exception:
            continue
        if not pmids:
            continue

        first_papers = defaultdict(list)
        dept_coauth_years = defaultdict(list)

        for idx in range(0, len(pmids), 250):
            batch = pmids[idx : idx + 250]
            try:
                ef = sess.get(
                    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
                    params={"db": "pubmed", "id": ",".join(batch), "retmode": "xml"},
                    timeout=22,
                )
                time.sleep(0.34)
                if ef.status_code != 200:
                    continue
                root = ET.fromstring(ef.text)
            except Exception:
                continue

            for art in root.findall(".//PubmedArticle"):
                aus = art.findall(".//Author")
                yr_s = art.findtext(".//PubDate/Year") or art.findtext(".//ArticleDate/Year") or ""
                if not (yr_s.isdigit() and 2011 <= int(yr_s) <= 2026):
                    continue
                yr = int(yr_s)
                title = (art.findtext(".//ArticleTitle") or "").strip()

                for a in aus[:3]:
                    aff = " ".join(x.text or "" for x in a.findall(".//Affiliation"))
                    if any(sub in aff.lower() for sub in org_substrings) and DEPT_CHECK_RE.search(aff):
                        nm = clean_person_name(f"{a.findtext('ForeName') or ''} {a.findtext('LastName') or ''}")
                        if nm:
                            dept_coauth_years[norm_key(nm)].append(yr)

                if not (2 <= len(aus) <= 6):
                    continue
                au1, auL = aus[0], aus[-1]
                aff1 = " ".join(x.text or "" for x in au1.findall(".//Affiliation"))
                aff1_low = aff1.lower()
                if not any(sub in aff1_low for sub in org_substrings) or not DEPT_CHECK_RE.search(aff1):
                    continue

                fn1 = clean_person_name(f"{au1.findtext('ForeName') or ''} {au1.findtext('LastName') or ''}")
                fnL = clean_person_name(f"{auL.findtext('ForeName') or ''} {auL.findtext('LastName') or ''}")
                if not fn1 or not fnL:
                    continue
                f1, l1 = first_last_sig(fn1)
                fL, lL = first_last_sig(fnL)
                if not f1 or len(f1) < 2 or not fL:
                    continue

                raw_fl_cands = by_fl.get((f1, l1), ())
                compat_cands = [c for c in raw_fl_cands if middle_compatible(fn1, c["name"])]

                skip = False
                existing_same_school = None
                for c in compat_cands:
                    # 1. Existing faculty at THIS university
                    if c["id"] in fac_ids or c["current_affiliation"] == canon_school:
                        if c["student_count"] >= 1 or c["year"] is None or c["year"] <= yr - 3:
                            skip = True
                            break
                    # 2. Existing student/graduate at THIS university
                    if c["school"] == canon_school:
                        if c["year"] and c["year"] <= yr - 3:
                            skip = True
                        else:
                            existing_same_school = c
                        break
                    # 3. Established professor elsewhere (`student_count >= 3` and graduated earlier)
                    if c["student_count"] >= 3 and (c["year"] is None or c["year"] <= yr - 2):
                        skip = True
                        break
                    # 4. Rare/unambiguous name (`len(compat_cands) <= 2`) with recent external PhD (postdoc!)
                    if len(compat_cands) <= 2 and c["school"] and c["year"] and (1 <= yr - c["year"] <= 20):
                        skip = True
                        break
                if skip or existing_same_school:
                    continue

                matched_prof = None
                for pid, pnm, pyr, pfld in fac_map.get((fL[0], lL), ()):
                    pf, _ = first_last_sig(pnm)
                    if len(fL) == 1 or pf.startswith(fL) or fL.startswith(pf):
                        if not (pyr and pyr > yr - 3):
                            matched_prof = (pid, pnm, pfld)
                            break
                if not matched_prof:
                    continue

                fld = "Biostatistics" if "biostat" in aff1_low else (
                    matched_prof[2] if matched_prof[2] in ("Statistics", "Biostatistics", "Probability") else "Statistics"
                )
                first_papers[norm_key(fn1)].append((yr, fn1, matched_prof[0], matched_prof[1], fld, len(aus), title))

        sch_added = 0
        for snk, papers in first_papers.items():
            pc = Counter((p[2], p[3], p[4]) for p in papers)
            (adv_id, adv_name, fld), cnt = pc.most_common(1)[0]
            all_yrs = dept_coauth_years.get(snk) or [p[0] for p in papers]
            if max(all_yrs) - min(all_yrs) > 7:
                continue
            min_team = min(p[5] for p in papers if p[2] == adv_id)
            total_dept_papers = len(all_yrs)
            # Accept if >=2 first-author method papers, OR <=5 authors, OR 6 authors with >=2 dept method papers
            if cnt >= 2 or min_team <= 5 or (min_team == 6 and total_dept_papers >= 2):
                disp_nm = max((p[1] for p in papers), key=len)
                sample_t = [p[6] for p in papers if p[2] == adv_id][0]
                est_yr = max(all_yrs)
                ov = VERIFIED_OVERRIDES.get((snk, canon_school))
                discovered.append({
                    "student": disp_nm,
                    "snk": snk,
                    "advisor_id": adv_id,
                    "advisor_name": adv_name,
                    "school": canon_school,
                    "year": ov["year"] if ov else (est_yr if est_yr <= 2025 else None),
                    "field": ov["field"] if ov else fld,
                    "thesis": ov["thesis"] if ov else sample_t,
                })
                sch_added += 1
        if sch_added:
            print(f"    -> {canon_school:48s}: +{sch_added:3d} homonym/collaborative advisees recovered", flush=True)

    print(f"\n[2/2] Integrating {len(discovered):,} newly recovered doctoral advisees into stat_genealogy.db...", flush=True)
    next_id = (cur.execute("SELECT MAX(id) FROM people").fetchone()[0] or 7_500_000) + 100
    existing_edges = set((r[0], r[1]) for r in cur.execute("SELECT advisor_id, student_id FROM edges").fetchall())

    added_p = 0
    added_e = 0
    for item in discovered:
        sid = next_id
        next_id += 1
        sch = item["school"]
        country = "United Kingdom" if "London" in sch else "United States"
        cur.execute(
            """INSERT INTO people (
                id, name, norm_name, search_aliases, school, raw_school, country, year, thesis,
                mgp_subject, primary_field, is_core_stat, is_broad_stat, field_confidence,
                classification_reason, current_affiliation, research_areas, mgp_id, aft_pid,
                wikidata_qid, orcid, homepage, proquest_id, sources, advisor_count, student_count,
                descendant_count, stat_descendant_count
            ) VALUES (?,?,?,?,?,'',?,?,?,NULL,?,1,1,0.86,'PubMed Methodology First/Senior-Author Lineage','','',NULL,NULL,'','','','','PubMed Methodology',1,0,0,0)""",
            (sid, item["student"], item["snk"], "", sch, country, item["year"], item["thesis"], item["field"]),
        )
        added_p += 1
        aid = item["advisor_id"]
        if aid != sid and (aid, sid) not in existing_edges:
            cur.execute("INSERT OR IGNORE INTO edges VALUES (?, ?, 'PubMed Methodology')", (aid, sid))
            existing_edges.add((aid, sid))
            added_e += 1

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
    conn.close()
    print(f"DONE in {time.time() - t0:.1f}s! Added +{added_p} PhDs and +{added_e} edges. Summary: {json.dumps(meta)}", flush=True)


if __name__ == "__main__":
    main()
