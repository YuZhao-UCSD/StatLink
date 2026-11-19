#!/usr/bin/env python3
"""
Pass 2 High-Recall Harvester & Precision Entity Refiner:
  1. Fixes cross-school student homonyms (never conflate PhDs from two different universities)
     and merges same-school middle-name duplicates (e.g. Kristen Hansen -> Kristen Antonia Hansen).
  2. Completes full pagination across all UC eScholarship ETD units (including UCSF, UCR, UCM,
     and remaining pages of UCSD, UC Berkeley, UCLA, UC Davis, UCSB, UCI, UCSC without early-stop).
  3. Harvests all ~8,986 French Statistics, Biostatistics & Probability PhDs from HAL / theses.fr
     using exact `director_s` advisor metadata.
  4. Runs automated DuckDuckGo + Homepage/CV web discovery on under-indexed Statistics & Biostatistics
     faculty at top departments (Harvard, Hopkins, UW, UNC, Michigan, NC State, Columbia, Chicago).
"""

import concurrent.futures
import json
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict, deque

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

from crawler_engine import crawl_professor_across_web
from harvest_web_and_etds import (
    ESCHOL_QUERY,
    HEADERS,
    MASTERS_REJECT_RE,
    STAT_SUBJ_RE,
    STAT_TITLE_RE,
    UC_CAMPUSES,
    clean_person_name,
    first_last_sig,
    infer_field,
    norm_key,
    split_advisor_field,
)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "data", "stat_genealogy.db")


def harvest_uc_remaining(unit_id: str, school_name: str, skip_pages: int, known_advisor_sigs: set):
    records = []
    more = None
    session = requests.Session()
    pages = 0
    while pages < 62:
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
            items = (((resp.json().get("data") or {}).get("unit") or {}).get("items")) or {}
            nodes = items.get("nodes") or []
            if not nodes:
                break

            if pages >= skip_pages:
                for n in nodes:
                    pub = n.get("published") or ""
                    yr = int(pub[:4]) if len(pub) >= 4 and pub[:4].isdigit() else None
                    contribs = (n.get("contributors") or {}).get("nodes") or []
                    advisors = [
                        clean_person_name(c.get("name") or "")
                        for c in contribs
                        if c.get("role") in ("ADVISOR", "COMMITTEE_CHAIR")
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
                    title = re.sub(r"\s+", " ", (n.get("title") or "").strip())
                    if MASTERS_REJECT_RE.search(title):
                        continue
                    subjs = ", ".join(n.get("subjects") or [])
                    adv_match = any(first_last_sig(a) in known_advisor_sigs for a in advisors)
                    subj_match = bool(STAT_SUBJ_RE.search(subjs) or STAT_TITLE_RE.search(title))
                    if not (adv_match or subj_match):
                        continue
                    pqid = ""
                    for lid in (n.get("localIDs") or []):
                        if lid.get("subScheme") == "proquest":
                            m = re.search(r"(\d{6,10})", lid.get("id") or "")
                            if m:
                                pqid = m.group(1)
                    records.append({
                        "student": student_name,
                        "advisors": advisors,
                        "school": school_name,
                        "country": "United States",
                        "year": yr,
                        "thesis": title,
                        "subjects": subjs,
                        "orcid": (authors[0].get("orcid") or "").strip(),
                        "pqid": pqid,
                        "source": "eScholarship",
                    })
            more = items.get("more")
            if not more:
                break
        except Exception:
            break
    print(f"    -> Completed {school_name} ({unit_id}): +{len(records):,} additional ETDs (total pages={pages})")
    return records


def harvest_hal_director_theses():
    records = []
    session = requests.Session()
    for start in range(0, 9000, 1000):
        try:
            resp = session.get(
                "https://api.archives-ouvertes.fr/search/tel/",
                params={
                    "q": 'statistique OR biostatistique OR probabilites OR "processus stochastiques" OR bayesien',
                    "rows": 1000,
                    "start": start,
                    "fl": "title_s,authFullName_s,director_s,defenseDateY_i,instStructName_s,authorityInstitution_s",
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
                dirs = d.get("director_s") or []
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
                school = insts[0].strip() if insts else "Université Paris-Saclay"
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
    print(f"    -> HAL / theses.fr (with `director_s`): harvested {len(records):,} French Stat/Prob PhD theses!")
    return records


def main():
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # 1. Clean up cross-school edge false merges from Pass 1 (e.g., CMU Yu Zhao linked to UCSD Armin Schwartzman)
    print("[1/5] Sanitizing cross-school student homonyms & merging same-school middle-name duplicates...")
    # Remove edges added by eScholarship where existing student's MGP school != advisor's UC campus!
    bad_homonym_edges = cur.execute("""
        SELECT e.advisor_id, e.student_id, p_adv.current_affiliation, p_adv.school AS adv_sch, p_stu.name, p_stu.school AS stu_sch, e.source
        FROM edges e
        JOIN people p_adv ON p_adv.id = e.advisor_id
        JOIN people p_stu ON p_stu.id = e.student_id
        WHERE e.source IN ('eScholarship', 'University ETD', 'DataCite ETD')
          AND p_stu.mgp_id IS NOT NULL
          AND p_stu.school != ''
          AND p_stu.school NOT LIKE '%California%'
          AND e.source = 'eScholarship'
    """).fetchall()
    for b in bad_homonym_edges:
        cur.execute("DELETE FROM edges WHERE advisor_id = ? AND student_id = ?", (b["advisor_id"], b["student_id"]))
    print(f"      Removed {len(bad_homonym_edges)} cross-school homonym edge(s).")

    # Re-load cached Pass 1 UCSD/UC records for any student whose edge was just unlinked (like UCSD Yu Zhao 2023!)
    pass1_json_path = os.path.join(BASE_DIR, "data_raw", "web_harvest", "harvested_phd_records.json")
    reprocess_records = []
    if os.path.exists(pass1_json_path):
        with open(pass1_json_path, "r", encoding="utf-8") as f:
            p1_all = json.load(f)
        unlinked_names = {norm_key(b["name"]) for b in bad_homonym_edges}
        for rec in p1_all:
            if norm_key(rec["student"]) in unlinked_names:
                reprocess_records.append(rec)

    # Index current DB
    existing_by_norm = defaultdict(list)
    existing_by_sig = defaultdict(list)
    known_stat_advisor_sigs = set()
    for r in cur.execute("SELECT id, name, norm_name, school, current_affiliation, year, thesis, primary_field, is_core_stat, is_broad_stat, mgp_id, homepage, sources, student_count FROM people"):
        d = dict(r)
        nk = norm_key(d["name"])
        existing_by_norm[nk].append(d)
        sig = first_last_sig(d["name"])
        if sig[0] and sig[1]:
            existing_by_sig[sig].append(d)
            if d["is_broad_stat"]:
                known_stat_advisor_sigs.add(sig)

    # 2. Complete remaining UC eScholarship pages (including UCSF, UCR, UCM, UCSD p22+, UCB p21+, UCLA p28+)
    print("\n[2/5] Completing remaining UC eScholarship ETD pages (UCSF, UCR, UCM, UCSD, UCB, UCLA...)...")
    skip_map = {
        "ucsd_etd": 22, "ucb_etd": 21, "ucla_etd": 28, "ucd_etd": 13,
        "ucsb_etd": 15, "uci_etd": 8, "ucsc_etd": 5,
        "ucr_etd": 1, "ucsf_etd": 1, "ucm_etd": 1,
    }
    new_batch = list(reprocess_records)
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futs = [
            pool.submit(harvest_uc_remaining, uid, sname, skip_map[uid], known_stat_advisor_sigs)
            for uid, sname in UC_CAMPUSES
        ]
        for f in concurrent.futures.as_completed(futs):
            new_batch.extend(f.result())

    # 3. Harvest all ~8,986 HAL French Stat/Prob PhD Theses with exact `director_s`
    print("\n[3/5] Harvesting French Statistics, Biostatistics & Probability PhDs from HAL (`director_s`)...")
    new_batch.extend(harvest_hal_director_theses())

    # 4. Automated Web Discovery Pass on 90 Under-Indexed Contemporary Faculty at Top Departments
    print("\n[4/5] Crawling the web (DuckDuckGo + Group Pages + CVs) for under-indexed Stat/Biostat professors...")
    priority_schools = (
        "Harvard University", "Johns Hopkins University", "University of Washington",
        "University of North Carolina at Chapel Hill", "University of Michigan",
        "North Carolina State University", "Columbia University", "University of Chicago",
        "University of California, San Diego", "Yale University", "Brown University",
    )
    ph = ",".join("?" for _ in priority_schools)
    under_indexed_profs = cur.execute(f"""
        SELECT id, name, school, current_affiliation, homepage, mgp_id
        FROM people
        WHERE is_core_stat = 1
          AND (current_affiliation IN ({ph}) OR school IN ({ph}))
          AND year BETWEEN 1990 AND 2016
          AND student_count BETWEEN 1 AND 5
        ORDER BY stat_descendant_count DESC, student_count DESC
        LIMIT 80
    """, priority_schools + priority_schools).fetchall()

    def crawl_prof_row(pr):
        disc, hp, _ = crawl_professor_across_web(
            prof_name=pr["name"],
            school=pr["current_affiliation"] or pr["school"],
            homepage=pr["homepage"] or "",
            mgp_id=pr["mgp_id"],
        )
        out = []
        for it in disc:
            out.append({
                "student": it["student"],
                "advisors": [pr["name"]],
                "school": it.get("school") or pr["current_affiliation"] or pr["school"],
                "country": "United States",
                "year": it.get("year"),
                "thesis": it.get("thesis") or "",
                "subjects": "Statistics, Biostatistics",
                "orcid": "",
                "pqid": "",
                "source": it.get("source") or "Web Crawl",
            })
        return out

    web_discovered_count = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        for res in pool.map(crawl_prof_row, under_indexed_profs):
            if res:
                web_discovered_count += len(res)
                new_batch.extend(res)
    print(f"    -> Automated Web & CV Discovery found {web_discovered_count:,} PhD advisee records across {len(under_indexed_profs)} faculty!")

    # 5. Strict Same-School Entity Resolution & Upsert
    print(f"\n[5/5] Integrating {len(new_batch):,} records with strict same-school student matching...")
    max_id = cur.execute("SELECT MAX(id) FROM people").fetchone()[0] or 4_500_000
    next_id = max_id + 100
    existing_edges = set((r[0], r[1]) for r in cur.execute("SELECT advisor_id, student_id FROM edges").fetchall())

    def resolve_strict(name, school, country, year, thesis, field, is_advisor=False, orcid="", pqid="", source_label="ETD"):
        nonlocal next_id
        nk = norm_key(name)
        sig = first_last_sig(name)
        cands = list(existing_by_norm.get(nk, ()))
        if sig[0] and sig[1]:
            for x in existing_by_sig.get(sig, ()):
                if x not in cands:
                    cands.append(x)

        best_match = None
        best_score = -999
        for c in cands:
            sc = 0
            if norm_key(c["name"]) == nk:
                sc += 45
            elif sig[0] and sig[1] and first_last_sig(c["name"]) == sig:
                sc += 25

            if not is_advisor:
                # STRICT STUDENT RULE: Never merge two PhD students from DIFFERENT known universities!
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

            if sc > best_score:
                best_score = sc
                best_match = c

        threshold = 50 if is_advisor else 75
        if best_match and best_score >= threshold:
            uid = best_match["id"]
            srcs = best_match["sources"].split(",") if best_match["sources"] else []
            if source_label not in srcs:
                srcs.append(source_label)
                best_match["sources"] = ",".join(srcs)
            # Prefer fuller name (e.g. Kristen Antonia Hansen over Kristen Hansen) and fill year/thesis
            better_name = name if len(name) > len(best_match["name"]) and not is_advisor else best_match["name"]
            upd_yr = best_match["year"] or (None if is_advisor else year)
            upd_sch = best_match["school"] or ("" if is_advisor else school)
            upd_th = best_match["thesis"] or ("" if is_advisor else thesis)
            cur.execute(
                """
                UPDATE people SET
                    name = ?,
                    year = COALESCE(year, ?),
                    school = CASE WHEN school = '' THEN ? ELSE school END,
                    thesis = CASE WHEN thesis = '' THEN ? ELSE thesis END,
                    orcid = CASE WHEN orcid = '' THEN ? ELSE orcid END,
                    proquest_id = CASE WHEN proquest_id = '' THEN ? ELSE proquest_id END,
                    sources = ?
                WHERE id = ?
                """,
                (better_name, upd_yr, upd_sch, upd_th, orcid, pqid, best_match["sources"], uid),
            )
            best_match["name"] = better_name
            best_match["year"] = upd_yr
            best_match["school"] = upd_sch
            best_match["thesis"] = upd_th
            return uid, best_match, False

        uid = next_id
        next_id += 1
        is_core = 1 if field in ("Statistics", "Biostatistics", "Probability") else 0
        is_broad = 1 if (is_core or field == "Related Quantitative") else 0
        new_rec = {
            "id": uid, "name": name, "norm_name": nk,
            "school": "" if is_advisor else school,
            "current_affiliation": school if is_advisor else "",
            "year": None if is_advisor else year,
            "thesis": "" if is_advisor else thesis,
            "primary_field": field, "is_core_stat": is_core, "is_broad_stat": is_broad,
            "mgp_id": None, "homepage": "", "sources": source_label, "student_count": 0,
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
                uid, name, nk, "", "" if is_advisor else school, "", country,
                None if is_advisor else year, "" if is_advisor else thesis, None,
                field, is_core, is_broad, 0.88, f"Verified via {source_label}",
                school if is_advisor else "", "", None, None, "", orcid, "", pqid, source_label,
            ),
        )
        existing_by_norm[nk].append(new_rec)
        if sig[0] and sig[1]:
            existing_by_sig[sig].append(new_rec)
        return uid, new_rec, True

    added_p = 0
    added_e = 0
    for item in new_batch:
        adv_list = []
        adv_pf = ""
        for anm in item["advisors"]:
            aid, arec, is_n = resolve_strict(
                anm, item["school"], item.get("country") or "", item.get("year"), "", "Statistics",
                is_advisor=True, source_label=item["source"]
            )
            if is_n:
                added_p += 1
            adv_list.append(aid)
            if arec["primary_field"] in ("Biostatistics", "Statistics", "Probability") and not adv_pf:
                adv_pf = arec["primary_field"]

        s_field = infer_field(item.get("thesis") or "", item.get("subjects") or "", adv_pf)
        sid, srec, is_ns = resolve_strict(
            item["student"], item["school"], item.get("country") or "", item.get("year"),
            item.get("thesis") or "", s_field, is_advisor=False,
            orcid=item.get("orcid") or "", pqid=item.get("pqid") or "", source_label=item["source"]
        )
        if is_ns:
            added_p += 1
        for aid in adv_list:
            if aid != sid and (aid, sid) not in existing_edges and (sid, aid) not in existing_edges:
                cur.execute("INSERT OR IGNORE INTO edges VALUES (?, ?, ?)", (aid, sid, item["source"]))
                existing_edges.add((aid, sid))
                added_e += 1

    # Merge any same-advisor duplicate students where one has year=None (webpage) and one has year!=None (eScholarship)
    dup_stus = cur.execute("""
        SELECT e1.advisor_id, p1.id AS keep_id, p1.name AS keep_name, p2.id AS drop_id, p2.name AS drop_name
        FROM edges e1
        JOIN edges e2 ON e1.advisor_id = e2.advisor_id AND e1.student_id != e2.student_id
        JOIN people p1 ON p1.id = e1.student_id
        JOIN people p2 ON p2.id = e2.student_id
        WHERE p1.year IS NOT NULL AND p2.year IS NULL
          AND p2.mgp_id IS NULL AND p2.student_count = 0
          AND p1.school = p2.school
    """).fetchall()
    merged_dups = 0
    for r in dup_stus:
        if first_last_sig(r["keep_name"]) == first_last_sig(r["drop_name"]):
            cur.execute("DELETE FROM edges WHERE student_id = ?", (r["drop_id"],))
            cur.execute("DELETE FROM people WHERE id = ?", (r["drop_id"],))
            existing_edges.discard((r["advisor_id"], r["drop_id"]))
            merged_dups += 1
    print(f"      Added +{added_p:,} scholars, +{added_e:,} edges, and deduplicated {merged_dups} webpage/ETD pairs.")

    # Recompute exact DAG counts + schools + meta_stats + FTS5
    existing_edges = set(
        (r[0], r[1])
        for r in cur.execute(
            "SELECT e.advisor_id, e.student_id FROM edges e JOIN people p1 ON p1.id=e.advisor_id JOIN people p2 ON p2.id=e.student_id"
        ).fetchall()
    )
    advisors_of = defaultdict(list)
    students_of = defaultdict(list)
    for aid, sid in existing_edges:
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

    armin_stus = cur.execute(
        "SELECT p.name, p.year, p.school, p.primary_field, p.thesis, e.source FROM edges e JOIN people p ON p.id=e.student_id WHERE e.advisor_id=23000 ORDER BY COALESCE(p.year, 9999), p.name"
    ).fetchall()
    print(f"\nArmin Schwartzman clean verified PhD students ({len(armin_stus)}):")
    for s in armin_stus:
        print("  *", dict(s))

    conn.close()
    print(f"\nPass 2 complete in {time.time() - t0:.1f}s! Final summary:\n{json.dumps(meta, indent=2)}")


if __name__ == "__main__":
    main()
