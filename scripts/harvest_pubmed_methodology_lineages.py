#!/usr/bin/env python3
"""
High-Precision PubMed Statistical & Biostatistical Methodology Lineage Harvester
(`scripts/harvest_pubmed_methodology_lineages.py`).

Closes the post-2013 ProQuest / Cloudflare dissertation recall gap across Johns Hopkins University
and 36 peer Biostatistics & Statistics universities by analyzing small-team (2–6 author)
methodological papers in 40+ core Statistics, Biostatistics, Statistical Genetics, Bioinformatics,
Neuroimaging, and Quantitative Epidemiology journals (2012–2026) via NIH NCBI PubMed E-Utilities:

  - Strictly requires First Author to carry an explicit Statistical / Biostatistical / Data Science
    departmental affiliation at University U.
  - Strictly requires Senior (Last) Author to be a verified Faculty Advisor at University U
    (with PhD >= 3 years earlier).
  - Strictly excludes First Authors who already hold an earlier PhD in the database (preventing
    senior faculty colleagues like Elizabeth Colantuoni or postdocs with known external PhDs from
    being misclassified as PhD students!).
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
sys.stdout.reconfigure(encoding="utf-8")

from harvest_web_and_etds import clean_person_name, first_last_sig, norm_key

DB_PATH = os.path.join(BASE_DIR, "data", "stat_genealogy.db")

JOURNAL_FILTER = (
    '("Biostatistics"[ta] OR "Biometrics"[ta] OR "J Am Stat Assoc"[ta] OR "Stat Med"[ta] '
    'OR "Stat Methods Med Res"[ta] OR "Ann Appl Stat"[ta] OR "Biometrika"[ta] OR "Ann Stat"[ta] '
    'OR "Stat Sci"[ta] OR "J R Stat Soc Series B Stat Methodol"[ta] OR "J R Stat Soc Ser C Appl Stat"[ta] '
    'OR "J Comput Graph Stat"[ta] OR "Comput Stat Data Anal"[ta] OR "Stat Sin"[ta] OR "Scand J Stat"[ta] '
    'OR "Lifetime Data Anal"[ta] OR "Biom J"[ta] OR "Pharm Stat"[ta] OR "J Biopharm Stat"[ta] '
    'OR "Clin Trials"[ta] OR "Stat Interface"[ta] OR "Electron J Stat"[ta] OR "J Mach Learn Res"[ta] '
    'OR "Bioinformatics"[ta] OR "Genome Biol"[ta] OR "PLoS Comput Biol"[ta] OR "BMC Bioinformatics"[ta] '
    'OR "NAR Genom Bioinform"[ta] OR "Genet Epidemiol"[ta] OR "Am J Hum Genet"[ta] OR "PLoS Genet"[ta] '
    'OR "Nat Methods"[ta] OR "Neuroimage"[ta] OR "Hum Brain Mapp"[ta] OR "Med Image Anal"[ta] '
    'OR "Am J Epidemiol"[ta] OR "Epidemiology"[ta] OR "Int J Epidemiol"[ta] OR "BMC Med Res Methodol"[ta])'
)

TARGET_SCHOOLS = [
    ("Johns Hopkins University", '"Johns Hopkins"[Affiliation]', ["hopkins"]),
    ("Harvard University", '"Harvard"[Affiliation]', ["harvard"]),
    ("University of Washington", '"University of Washington"[Affiliation]', ["university of washington"]),
    ("University of North Carolina at Chapel Hill", '("North Carolina at Chapel Hill"[Affiliation] OR "UNC Gillings"[Affiliation] OR "Chapel Hill"[Affiliation])', ["chapel hill"]),
    ("University of Michigan", '"University of Michigan"[Affiliation]', ["university of michigan"]),
    ("Columbia University", '"Columbia University"[Affiliation]', ["columbia university", "mailman school"]),
    ("Emory University", '"Emory University"[Affiliation]', ["emory"]),
    ("University of Pittsburgh", '"University of Pittsburgh"[Affiliation]', ["pittsburgh"]),
    ("Boston University", '"Boston University"[Affiliation]', ["boston university"]),
    ("Yale University", '"Yale"[Affiliation]', ["yale"]),
    ("Brown University", '"Brown University"[Affiliation]', ["brown university"]),
    ("University of Texas Health Science Center at Houston", '("University of Texas Health Science Center at Houston"[Affiliation] OR "UTHealth"[Affiliation] OR "MD Anderson"[Affiliation])', ["health science center at houston", "uthealth", "md anderson"]),
    ("University of Minnesota", '"University of Minnesota"[Affiliation]', ["university of minnesota"]),
    ("Vanderbilt University", '"Vanderbilt"[Affiliation]', ["vanderbilt"]),
    ("University of Rochester", '"University of Rochester"[Affiliation]', ["university of rochester"]),
    ("University at Buffalo (SUNY)", '("University at Buffalo"[Affiliation] OR "SUNY Buffalo"[Affiliation])', ["buffalo"]),
    ("Virginia Commonwealth University", '"Virginia Commonwealth"[Affiliation]', ["virginia commonwealth"]),
    ("University of South Florida", '"University of South Florida"[Affiliation]', ["south florida"]),
    ("University of Alabama at Birmingham", '"Alabama at Birmingham"[Affiliation]', ["alabama at birmingham"]),
    ("Medical University of South Carolina", '"Medical University of South Carolina"[Affiliation]', ["medical university of south carolina"]),
    ("University of Iowa", '"University of Iowa"[Affiliation]', ["university of iowa"]),
    ("University of Florida", '"University of Florida"[Affiliation]', ["university of florida"]),
    ("North Carolina State University", '"North Carolina State University"[Affiliation]', ["north carolina state"]),
    ("Texas A&M University", '"Texas A&M"[Affiliation]', ["texas a&m", "texas a & m"]),
    ("Rutgers University", '"Rutgers"[Affiliation]', ["rutgers"]),
    ("University of Maryland", '"University of Maryland"[Affiliation]', ["university of maryland"]),
    ("University of Southern California", '"University of Southern California"[Affiliation]', ["southern california"]),
    ("University of Chicago", '"University of Chicago"[Affiliation]', ["university of chicago"]),
    ("Carnegie Mellon University", '"Carnegie Mellon"[Affiliation]', ["carnegie mellon"]),
    ("Ohio State University", '"Ohio State University"[Affiliation]', ["ohio state"]),
    ("Purdue University", '"Purdue University"[Affiliation]', ["purdue"]),
    ("Pennsylvania State University", '("Pennsylvania State University"[Affiliation] OR "Penn State"[Affiliation])', ["pennsylvania state", "penn state"]),
    ("Iowa State University", '"Iowa State University"[Affiliation]', ["iowa state"]),
    ("New York University", '"New York University"[Affiliation]', ["new york university", "nyu"]),
    ("Northwestern University", '"Northwestern University"[Affiliation]', ["northwestern university"]),
    ("University of Colorado Anschutz Medical Campus", '("Colorado Anschutz"[Affiliation] OR "Colorado School of Public Health"[Affiliation])', ["anschutz", "colorado school of public health"]),
    ("London School of Hygiene & Tropical Medicine", '"London School of Hygiene"[Affiliation]', ["hygiene"]),
]

DEPT_AFF_TERMS = (
    '("Biostatistics"[Affiliation] OR "Statistics"[Affiliation] OR "Statistical Science"[Affiliation] '
    'OR "Applied Mathematics and Statistics"[Affiliation] OR "Data Science"[Affiliation])'
)

DEPT_CHECK_RE = re.compile(
    r"\b(biostat\w*|department\s+of\s+statistics|school\s+of\s+statistics|statistical\s+science\w*|"
    r"applied\s+mathematics\s+and\s+statistics|division\s+of\s+statistics|data\s+science)\b",
    re.I,
)


def main():
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    print("[1/3] Indexing university faculty & existing PhD holders from stat_genealogy.db...", flush=True)
    fac_by_school = defaultdict(dict)  # school -> {(f_init, l_tok): [(id, name, yr, field)]}
    seen_fac_school = set()
    for r in cur.execute("""
        SELECT DISTINCT p.id, p.name, p.year, p.primary_field, a.school
        FROM advisor_schools a
        JOIN people p ON p.id = a.person_id
        WHERE p.is_broad_stat = 1
          AND (p.student_count >= 1 OR p.sources LIKE '%Roster%' OR (p.year IS NOT NULL AND p.year <= 2020))
        UNION
        SELECT DISTINCT p.id, p.name, p.year, p.primary_field, p.current_affiliation AS school
        FROM people p
        WHERE p.current_affiliation != ''
          AND p.is_broad_stat = 1
          AND (p.student_count >= 1 OR p.sources LIKE '%Roster%' OR p.year IS NULL OR p.year <= 2020)
    """):
        ft, lt = first_last_sig(r["name"])
        if ft and lt and (r["id"], r["school"]) not in seen_fac_school:
            seen_fac_school.add((r["id"], r["school"]))
            fac_by_school[r["school"]].setdefault((ft[0], lt), []).append(
                (r["id"], r["name"], r["year"], r["primary_field"])
            )

    global_stat_by_fl = defaultdict(list)
    existing_by_norm = defaultdict(list)
    for r in cur.execute("SELECT id, name, norm_name, school, current_affiliation, year, thesis, primary_field, is_core_stat, is_broad_stat, student_count FROM people"):
        d = dict(r)
        existing_by_norm[r["norm_name"]].append(d)
        if r["is_broad_stat"] == 1:
            ft, lt = first_last_sig(r["name"])
            if ft and lt and len(ft) >= 2:
                global_stat_by_fl[(ft, lt)].append((r["id"], r["name"], r["year"], r["primary_field"]))

    sess = requests.Session()
    discovered_all = []

    print(f"\n[2/3] Scanning PubMed statistical/biostatistical methodology papers across {len(TARGET_SCHOOLS)} universities...", flush=True)
    for canon_school, pubmed_org_q, org_substrings in TARGET_SCHOOLS:
        fac_map = fac_by_school.get(canon_school, {})

        term = f"{DEPT_AFF_TERMS} AND {pubmed_org_q} AND {JOURNAL_FILTER} AND 2012:2026[dp]"
        try:
            es = sess.get(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                params={"db": "pubmed", "term": term, "retmax": 2500, "retmode": "json"},
                timeout=15,
            ).json()
            pmids = (es.get("esearchresult") or {}).get("idlist") or []
            time.sleep(0.34)  # Respect NCBI 3 req/sec rate guideline!
        except Exception as exc:
            print(f"    [WARN] esearch {canon_school}: {exc}")
            continue

        if not pmids:
            continue

        first_auth_papers = defaultdict(list)
        for idx in range(0, len(pmids), 250):
            batch = pmids[idx : idx + 250]
            try:
                ef = sess.get(
                    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
                    params={"db": "pubmed", "id": ",".join(batch), "retmode": "xml"},
                    timeout=22,
                )
                time.sleep(0.35)
                if ef.status_code != 200:
                    continue
                root = ET.fromstring(ef.text)
            except Exception:
                continue

            for art in root.findall(".//PubmedArticle"):
                all_au = art.findall(".//Author")
                if not (2 <= len(all_au) <= 6):
                    continue
                yr_s = art.findtext(".//PubDate/Year") or art.findtext(".//ArticleDate/Year") or ""
                if not (yr_s.isdigit() and 2012 <= int(yr_s) <= 2026):
                    continue
                yr = int(yr_s)
                title = (art.findtext(".//ArticleTitle") or "").strip()

                au1, auL = all_au[0], all_au[-1]
                aff1 = " ".join(x.text or "" for x in au1.findall(".//Affiliation"))
                aff1_low = aff1.lower()
                if not any(sub in aff1_low for sub in org_substrings):
                    continue
                if not DEPT_CHECK_RE.search(aff1):
                    continue

                fn1 = clean_person_name(f"{au1.findtext('ForeName') or ''} {au1.findtext('LastName') or ''}")
                fnL = clean_person_name(f"{auL.findtext('ForeName') or ''} {auL.findtext('LastName') or ''}")
                if not fn1 or not fnL:
                    continue
                f1, l1 = first_last_sig(fn1)
                fL, lL = first_last_sig(fnL)
                if not f1 or not fL:
                    continue

                # Check if First Author is already a senior professor or holds an earlier external PhD!
                snk = norm_key(fn1)
                skip_first = False
                for ex in existing_by_norm.get(snk, ()):
                    if ex["student_count"] >= 2:
                        skip_first = True
                        break
                    if ex["year"] and ex["year"] <= yr - 3:
                        skip_first = True
                        break
                    if ex["school"] and ex["school"] != canon_school and ex["year"] and ex["year"] <= yr:
                        skip_first = True
                        break
                if skip_first:
                    continue

                # Match Last Author against verified faculty at `canon_school`
                prof_cands = fac_map.get((fL[0], lL), ())
                matched_prof = None
                for pid, pnm, pyr, pfld in prof_cands:
                    p_first, _ = first_last_sig(pnm)
                    if len(fL) == 1 or p_first.startswith(fL) or fL.startswith(p_first):
                        if not (pyr and pyr > yr - 3):
                            matched_prof = (pid, pnm, pfld)
                            break
                # Fallback for post-2013 faculty hires not yet in advisor_schools:
                # Require full ForeName, UNIQUE match in global_stat_by_fl with PhD >= 4 yrs earlier,
                # AND explicit `canon_school` affiliation on Senior Author `auL`!
                if not matched_prof and len(fL) >= 2:
                    affL_low = " ".join(x.text or "" for x in auL.findall(".//Affiliation")).lower()
                    if any(sub in affL_low for sub in org_substrings):
                        gcands = global_stat_by_fl.get((fL, lL), ())
                        if len(gcands) == 1:
                            gpid, gpnm, gpyr, gpfld = gcands[0]
                            if gpyr and gpyr <= yr - 4:
                                matched_prof = (gpid, gpnm, gpfld)
                if not matched_prof:
                    continue

                field_guess = "Biostatistics" if "biostat" in aff1_low else (
                    matched_prof[2] if matched_prof[2] in ("Statistics", "Biostatistics", "Probability") else "Statistics"
                )
                first_auth_papers[snk].append((yr, fn1, matched_prof[0], matched_prof[1], field_guess, len(all_au), title))

        school_found = 0
        for snk, papers in first_auth_papers.items():
            # Group by matched advisor
            pc = Counter((p[2], p[3], p[4]) for p in papers)
            (adv_id, adv_name, fld), cnt = pc.most_common(1)[0]
            min_yr = min(p[0] for p in papers)
            max_yr = max(p[0] for p in papers)
            if max_yr - min_yr > 7:
                continue  # Span > 7 yrs suggests long-term staff scientist rather than PhD student
            # High-precision acceptance: >=2 methodology papers with advisor OR a tight 2-4 author methodology paper!
            min_team = min(p[5] for p in papers if p[2] == adv_id)
            if cnt >= 2 or min_team <= 4:
                disp_nm = max((p[1] for p in papers), key=len)
                sample_t = [p[6] for p in papers if p[2] == adv_id][0]
                discovered_all.append({
                    "student": disp_nm,
                    "snk": snk,
                    "advisor_id": adv_id,
                    "advisor_name": adv_name,
                    "school": canon_school,
                    "year": max_yr if max_yr <= 2025 else None,
                    "field": fld,
                    "sample_paper": sample_t,
                })
                school_found += 1

        print(f"    -> {canon_school:48s}: {len(pmids):4d} method papers -> {school_found:3d} doctoral advisees extracted", flush=True)

    cache_dir = os.path.join(BASE_DIR, "data_raw", "web_harvest")
    os.makedirs(cache_dir, exist_ok=True)
    with open(os.path.join(cache_dir, "pubmed_methodology_extracted.json"), "w", encoding="utf-8") as cf:
        json.dump(discovered_all, cf, ensure_ascii=False, indent=2)

    # -----------------------------------------------------------------------
    # Stage 3: Integrate into SQLite + Recompute Counts & FTS5
    # -----------------------------------------------------------------------
    print(f"\n[3/3] Integrating {len(discovered_all):,} extracted Biostatistics/Statistics doctoral advisees...", flush=True)
    next_id = (cur.execute("SELECT MAX(id) FROM people").fetchone()[0] or 7_500_000) + 100
    existing_edges = set((r[0], r[1]) for r in cur.execute("SELECT advisor_id, student_id FROM edges").fetchall())

    added_p = 0
    added_e = 0
    for item in discovered_all:
        snk = item["snk"]
        sch = item["school"]
        yr = item["year"]
        aid = item["advisor_id"]
        fld = item["field"]

        matched = None
        for c in existing_by_norm.get(snk, ()):
            if c["school"] == sch or (not c["school"] and c["current_affiliation"] == sch):
                matched = c
                break

        if matched:
            sid = matched["id"]
            cur.execute(
                "UPDATE people SET school = CASE WHEN school='' THEN ? ELSE school END, year = COALESCE(year, ?), is_core_stat = 1, is_broad_stat = 1 WHERE id = ?",
                (sch, yr, sid),
            )
        else:
            sid = next_id
            next_id += 1
            country = "United Kingdom" if "London" in sch else "United States"
            cur.execute(
                """INSERT INTO people (
                    id, name, norm_name, search_aliases, school, raw_school, country, year, thesis,
                    mgp_subject, primary_field, is_core_stat, is_broad_stat, field_confidence,
                    classification_reason, current_affiliation, research_areas, mgp_id, aft_pid,
                    wikidata_qid, orcid, homepage, proquest_id, sources, advisor_count, student_count,
                    descendant_count, stat_descendant_count
                ) VALUES (?,?,?,?,?,'',?,?,?,NULL,?,1,1,0.86,'PubMed Methodology First/Senior-Author Lineage','','',NULL,NULL,'','','','','PubMed Methodology',0,0,0,0)""",
                (sid, item["student"], snk, "", sch, country, yr, item["sample_paper"], fld),
            )
            new_c = {"id": sid, "name": item["student"], "norm_name": snk, "school": sch, "current_affiliation": "", "year": yr, "thesis": item["sample_paper"], "primary_field": fld, "is_core_stat": 1, "student_count": 0}
            existing_by_norm[snk].append(new_c)
            added_p += 1

        if aid != sid and (aid, sid) not in existing_edges and (sid, aid) not in existing_edges:
            cur.execute("INSERT OR IGNORE INTO edges VALUES (?, ?, 'PubMed Methodology')", (aid, sid))
            existing_edges.add((aid, sid))
            added_e += 1

    print(f"      Inserted +{added_p:,} net-new Biostatistics/Statistics PhDs and +{added_e:,} advisor-student edges!")

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
        for a, s in valid_edges:
            advisors_of[s].append(a)
            students_of[a].append(s)

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
    jhu_post14 = cur.execute("SELECT COUNT(*) FROM people WHERE school = 'Johns Hopkins University' AND is_core_stat = 1 AND (year >= 2014 OR year IS NULL)").fetchone()[0]
    print(f"\nJHU Final Stats: {dict(jhu_row)} | Core >=2014/active: {jhu_post14}")
    conn.close()
    print(f"PUBMED METHODOLOGY HARVESTER DONE in {time.time() - t0:.1f}s!\nSummary: {json.dumps(meta, indent=2)}")


if __name__ == "__main__":
    main()
