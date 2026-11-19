#!/usr/bin/env python3
"""
Zero-FullText-Cost OpenAlex Indexed Statistics Dissertation Harvester (2014–2021)
(`scripts/harvest_openalex_indexed_2015_2020.py`).

Uses pure indexed OpenAlex filters (`type:dissertation,publication_year:2014-2021,concepts.id:C105795698`,
costing $0.0001 per 200-row page instead of $0.01 for `title.search`) to scan 16,000+ top statistical
dissertations across Harvard DASH, UMich Deep Blue, Texas A&M OakTrust, Waterloo UWSpace, UBC cIRcle,
Oxford ORA, White Rose UK, Delft, Utrecht, SNU, ETH Zurich, etc., and filters titles locally in Python.
"""

import json
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict, deque

import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "scripts"))

from complete_2014_2025_gap_and_aft_precision import (
    GENUINE_STAT_THESIS_BROAD_RE,
    NON_STAT_THESIS_NOISE_RE,
     resolve_oa_school,
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


def main():
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    known_schools = {r[0] for r in cur.execute("SELECT school FROM schools WHERE total_phd_count >= 3").fetchall()}
    sess = requests.Session()
    sess.headers.update({"User-Agent": "StatLineage/1.0"})

    # Pure indexed filter ($0.0001/page): C105795698 = Statistics, C62649853 = Mathematical statistics / Biostatistics
    cursor = "*"
    pages = 0
    out = []
    seen_ids = set()

    print("[1/2] Paginating OpenAlex indexed `concepts.id:C105795698` dissertations (2014–2021)...", flush=True)
    while cursor and pages < 90:
        pages += 1
        try:
            r = sess.get(
                "https://api.openalex.org/works",
                params={
                    "filter": "type:dissertation,publication_year:2014-2021,concepts.id:C105795698",
                    "per-page": 200,
                    "cursor": cursor,
                },
                timeout=18,
            )
            if r.status_code != 200:
                print(f"    Stopped at page {pages} (HTTP {r.status_code})", flush=True)
                break
            data = r.json()
            results = data.get("results") or []
            if not results:
                break
            cursor = (data.get("meta") or {}).get("next_cursor")
            time.sleep(0.06)

            for w in results:
                wid = w.get("id")
                if wid in seen_ids:
                    continue
                seen_ids.add(wid)

                yr = w.get("publication_year")
                if not (isinstance(yr, int) and 2014 <= yr <= 2021):
                    continue
                title = (w.get("title") or "").strip()
                if not title or len(title) < 16:
                    continue
                if NON_PHD_REJECT_RE.search(title) or NON_STAT_DOMAIN_REJECT_RE.search(title) or NON_STAT_THESIS_NOISE_RE.search(title):
                    continue

                src_disp = (((w.get("primary_location") or {}).get("source") or {}).get("display_name") or "").lower()
                if any(b in src_disp for b in ["student papers", "bachelor", "master", "undergraduate"]):
                    continue

                # Require strong statistical/biostatistical/probabilistic methodology signal in title OR high-confidence Statistics concept score >= 0.62!
                stat_score = 0.0
                for c_obj in (w.get("concepts") or []):
                    if c_obj.get("id", "").endswith("C105795698"):
                        stat_score = float(c_obj.get("score") or 0.0)
                        break

                if not (CORE_STAT_TITLE_RE.search(title) or (stat_score >= 0.62 and GENUINE_STAT_THESIS_BROAD_RE.search(title))):
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

                sch = resolve_oa_school(w, known_schools)
                if not sch:
                    continue

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
                    "year": yr,
                    "thesis": title,
                    "advisors": advisors,
                    "orcid": oid,
                    "field": fld,
                    "source": "OpenAlex Global ETD",
                })
            if pages % 25 == 0:
                print(f"    ... scanned {pages * 200:,} dissertations -> {len(out):,} verified Stat/Biostat/Prob PhDs", flush=True)
        except Exception as exc:
            print(f"    Exception at page {pages}: {exc}", flush=True)
            break

    print(f"  -> Extracted {len(out):,} verified 2014–2021 Stat/Biostat/Prob dissertations across {pages} pages!", flush=True)

    print(f"\n[2/2] Integrating into stat_genealogy.db & rebuilding FTS5...", flush=True)
    by_orcid = {}
    by_norm_school = defaultdict(list)
    by_fl_school = defaultdict(list)
    fac_by_school_fl = defaultdict(list)

    for r in cur.execute("SELECT id, name, norm_name, school, year, thesis, orcid FROM people"):
        d = dict(r)
        if d["orcid"]:
            by_orcid[d["orcid"]] = d
        if d["school"]:
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
    next_id = (cur.execute("SELECT MAX(id) FROM people").fetchone()[0] or 7_800_000) + 100
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
            ctry = school_country.get(sch, "")
            cur.execute(
                """INSERT INTO people (
                    id, name, norm_name, search_aliases, school, raw_school, country, year, thesis,
                    mgp_subject, primary_field, is_core_stat, is_broad_stat, field_confidence,
                    classification_reason, current_affiliation, research_areas, mgp_id, aft_pid,
                    wikidata_qid, orcid, homepage, proquest_id, sources, advisor_count, student_count,
                    descendant_count, stat_descendant_count
                ) VALUES (?,?,?,?,?,'',?,?,?,NULL,?,1,1,0.89,?,'','',NULL,NULL,'',?,'','',?,0,0,0,0)""",
                (sid, stu_nm, snk, "", sch, ctry, yr, th, fld, f"Verified via {src}", oid, src),
            )
            new_d = {"id": sid, "name": stu_nm, "norm_name": snk, "school": sch, "year": yr, "thesis": th, "orcid": oid}
            by_norm_school[(snk, sch)].append(new_d)
            by_fl_school[(f1, l1, sch)].append(new_d)
            if oid:
                by_orcid[oid] = new_d
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

    print(f"  Added +{added_p:,} net-new 2014–2021 PhDs, +{added_e:,} advisor edges, and enriched {enriched_p:,} records!", flush=True)

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

    print("\n=== UPDATED ANNUAL CORE COUNTS (2008–2025) ===", flush=True)
    for yr in range(2008, 2026):
        c_yr = cur.execute("SELECT COUNT(*) FROM people WHERE is_core_stat=1 AND year=?", (yr,)).fetchone()[0]
        print(f"  {yr}: {c_yr:,}", flush=True)
    conn.close()
    print(f"DONE in {time.time() - t0:.1f}s!", flush=True)


if __name__ == "__main__":
    main()
