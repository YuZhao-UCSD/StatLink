#!/usr/bin/env python3
"""
Complete 2014–2026 OpenAlex Global Dissertation Harvest + AFT Non-Stat Subtree Recalibration
(`scripts/complete_2014_2025_gap_and_aft_precision.py`).

1. Paginates all ~20,800 OpenAlex `2014–2026` `type:dissertation` candidates using the Polite Pool
   (`mailto=research@statlineage.org` + HTTP 429 backoff) and a comprehensive repository-to-university
   resolver (`Harvard DASH`, `UMich Deep Blue`, `Texas A&M OakTrust`, `CMU KiltHub`, `Waterloo UWSpace`,
   `UBC cIRcle`, `Oxford ORA`, `Cambridge Apollo`, `Imperial Spiral`, `UCL Discovery`, `ETH Zurich`,
   `Purdue e-Pubs`, `Delft`, `Utrecht`, `KTH`, `Seoul National`, etc.).
2. Recalibrates `sources = 'AFT'` records that were pulled into `is_core_stat = 1` purely via multi-hop
   evolutionary/neuroscience/psychology tree propagation (e.g. salamander mimicry, passerine birds,
   Ancient Greek dialects, rodent behavior) to `is_core_stat = 0, is_broad_stat = 1` (`Related Quantitative`),
   while keeping 100% of AFT Biostatistics/Statistics profiles and statistical-thesis AFT PhDs in `Core`
   and deleting zero records.
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

from build_database import canonicalize_school
from close_jhu_and_peer_gaps import EXTRA_SCHOOL_FIXES
from fill_post2014_drop import (
    BIOSTAT_TITLE_RE,
    CORE_STAT_TITLE_RE,
    NON_PHD_REJECT_RE,
    NON_STAT_DOMAIN_REJECT_RE,
    PROB_TITLE_RE,
)
from harvest_web_and_etds import clean_person_name, first_last_sig, norm_key

DB_PATH = os.path.join(BASE_DIR, "data", "stat_genealogy.db")

REPO_SUBSTRING_MAP = [
    ("kilthub", "Carnegie Mellon University"),
    ("oaktrust", "Texas A&M University"),
    ("digital access to scholarship at harvard", "Harvard University"),
    ("(harvard university)", "Harvard University"),
    ("deep blue", "University of Michigan"),
    ("uwspace", "University of Waterloo"),
    ("circle (university of british columbia)", "University of British Columbia"),
    ("oxford university research archive", "University of Oxford"),
    ("apollo", "University of Cambridge"),
    ("spiral", "Imperial College London"),
    ("ucl discovery", "University College London"),
    ("edinburgh research archive", "University of Edinburgh"),
    ("purdue e-pubs", "Purdue University"),
    ("purdue university", "Purdue University"),
    ("dukespace", "Duke University"),
    ("scholarlycommons", "University of Pennsylvania"),
    ("dspace@mit", "Massachusetts Institute of Technology"),
    ("ideals", "University of Illinois Urbana-Champaign"),
    ("researchworks at the university of washington", "University of Washington"),
    ("tspace", "University of Toronto"),
    ("university of minnesota digital conservancy", "University of Minnesota"),
    ("delft university of technology", "Delft University of Technology"),
    ("utrecht university", "Utrecht University"),
    ("kth publication", "KTH Royal Institute of Technology"),
    ("lund university", "Lund University"),
    ("seoul national university", "Seoul National University"),
    ("eth zurich", "ETH Zürich"),
    ("monash university", "Monash University"),
    ("australian national university", "Australian National University"),
    ("university of melbourne", "University of Melbourne"),
    ("university of sydney", "University of Sydney"),
    ("ku leuven", "KU Leuven"),
    ("ghana", "University of Ghana"),
]

GENUINE_STAT_THESIS_BROAD_RE = re.compile(
    r"\b(statistic\w*|biostatistic\w*|bayesian|probabilit\w*|stochastic\w*|markov\w*|martingale\w*|"
    r"regression|semiparametric\w*|nonparametric\w*|survival\s+analysis|proportional\s+hazards|"
    r"competing\s+risks|longitudinal|time\s+series|econometric\w*|causal\s+inference|propensity\s+score\w*|"
    r"instrumental\s+variable\w*|random\s+matri(?:x|ces)|brownian|percolation|levy|monte\s+carlo|"
    r"variational\s+inference|high[\-\s]dimensional|graphical\s+model\w*|multiple\s+testing|"
    r"false\s+discovery|experimental\s+design|clinical\s+trial\w*|survey\s+sampling|missing\s+data|"
    r"quantile|empirical\s+likelihood|empirical\s+bayes|splines?|wavelet\w*|kriging|spatio[\-\s]temporal|"
    r"latent\s+variable\w*|item\s+response|psychometric\w*|mixed[\-\s]effects|generalized\s+linear|"
    r"change[\-\s]point|extreme\s+value\w*|copula\w*|bootstrap|jackknife|minimax|asymptotic\w*)\b",
    re.I,
)

GENUINE_STAT_AREA_RE = re.compile(
    r"\b(statist\w*|biostat\w*|probab\w*|stochastic\w*|econometr\w*|biometr\w*|epidemiol\w*|"
    r"psychometr\w*|actuar\w*|bayesian|causal|time\s+series|demograph\w*|data\s+science)\b",
    re.I,
)

NON_STAT_THESIS_NOISE_RE = re.compile(
    r"\b(salamander\w*|passerine\w*|songbird\w*|rodent\w*|in\s+the\s+mouse|mule\s+deer|wildlife|"
    r"ancient\s+greek|dialect\w*|suicid\w*|amyotrophic\s+lateral|bacterial\s+societies|"
    r"tonality|music\s+theory|poetry|novel\w*\s+of\s+the|medieval|renaissance|theolog\w*|"
    r"ipomoea|drosophila\s+courtship|foraging\s+behavior|predator[\-\s]prey)\b",
    re.I,
)


def resolve_oa_school(w: dict, known_schools: set) -> str:
    auths = w.get("authorships") or []
    a0 = auths[0] if auths else {}
    src_obj = ((w.get("primary_location") or {}).get("source")) or {}
    disp_s = (src_obj.get("display_name") or "").strip()
    host_s = (src_obj.get("host_organization_name") or "").strip()
    combo_low = f"{disp_s} | {host_s}".lower()

    for sub, mapped in REPO_SUBSTRING_MAP:
        if sub in combo_low:
            return mapped

    insts = a0.get("institutions") or []
    if insts and insts[0].get("display_name"):
        c = canonicalize_school(insts[0]["display_name"])
        return EXTRA_SCHOOL_FIXES.get(c, c)

    for raw in (disp_s, host_s):
        if not raw:
            continue
        m = re.search(r"\(([^)]*(?:University|Universität|Université|Universidad|Universiteit|Institut|College|ETH)[^)]*)\)", raw, re.I)
        cand_str = m.group(1).strip() if m else raw
        cand_str = re.sub(r"\s+(?:Libraries|Library|Repository|Open\s+Repository|Digital\s+Conservancy|Research\s+Archive|System)$", "", cand_str, flags=re.I).strip()
        c = canonicalize_school(cand_str)
        c = EXTRA_SCHOOL_FIXES.get(c, c)
        if c in known_schools or re.search(r"\b(University|Universität|Université|Universidad|Universiteit|Universidade|ETH|College\s+London)\b", c, re.I):
            if not any(b in c.lower() for b in ["press", "california digital", "zenodo", "figshare", "springer", "elsevier", "wiley", "national library"]):
                return c
    return ""


def harvest_full_openalex(known_schools: set):
    sess = requests.Session()
    sess.headers.update({"User-Agent": "StatLineage/1.0 (mailto:research@statlineage.org)"})

    # Query 1: Statistical/Biostatistical/Probabilistic Methodology Title Search
    # Query 2: OpenAlex Concept=Statistics (C105795698) with statistical title terms
    queries = [
        (
            "type:dissertation,publication_year:2014-2026,"
            "title.search:statistics|biostatistics|bayesian|semiparametric|nonparametric|regression|"
            "markov|stochastic|martingale|survival|causal|quantile|conformal|spatio-temporal|minimax|copula|wavelet|bootstrap"
        ),
    ]

    out = []
    seen_oa_ids = set()

    for q_filter in queries:
        cursor = "*"
        pages = 0
        while cursor and pages < 125:
            pages += 1
            data = None
            for attempt in range(4):
                try:
                    r = sess.get(
                        "https://api.openalex.org/works",
                        params={
                            "filter": q_filter,
                            "per-page": 200,
                            "cursor": cursor,
                            "mailto": "research@statlineage.org",
                        },
                        timeout=20,
                    )
                    if r.status_code == 200:
                        data = r.json()
                        break
                    if r.status_code in (429, 500, 502, 503, 504):
                        time.sleep(1.2 * (attempt + 1))
                except Exception:
                    time.sleep(0.8 * (attempt + 1))

            if not data:
                break
            results = data.get("results") or []
            if not results:
                break
            cursor = (data.get("meta") or {}).get("next_cursor")
            time.sleep(0.11)

            for w in results:
                wid = w.get("id")
                if wid in seen_oa_ids:
                    continue
                seen_oa_ids.add(wid)

                yr = w.get("publication_year")
                if not (isinstance(yr, int) and 2014 <= yr <= 2026):
                    continue
                title = (w.get("title") or "").strip()
                if not title or len(title) < 16:
                    continue
                if NON_PHD_REJECT_RE.search(title) or NON_STAT_DOMAIN_REJECT_RE.search(title) or NON_STAT_THESIS_NOISE_RE.search(title):
                    continue
                # Reject Swedish/Scandinavian undergraduate/master "Student Papers"
                src_disp = (((w.get("primary_location") or {}).get("source") or {}).get("display_name") or "").lower()
                if "student papers" in src_disp or "bachelor" in src_disp or "master" in src_disp:
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
                    "year": yr if yr <= 2025 else None,
                    "thesis": title,
                    "advisors": advisors,
                    "orcid": oid,
                    "field": fld,
                    "source": "OpenAlex Global ETD",
                })

            if pages % 25 == 0:
                print(f"    ... scanned {pages * 200:,} OpenAlex dissertations -> {len(out):,} kept so far", flush=True)

    print(f"  -> OpenAlex Polite Harvest complete: {len(out):,} verified 2014–2026 Stat/Biostat/Prob PhDs!", flush=True)
    return out


def main():
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    known_schools = {r[0] for r in cur.execute("SELECT school FROM schools WHERE total_phd_count >= 3").fetchall()}

    print("[1/3] Running Full Polite OpenAlex 2014–2026 Global Dissertation Harvest...", flush=True)
    oa_recs = harvest_full_openalex(known_schools)

    print("\n[2/3] Auditing `sources = 'AFT'` records for non-statistical subtree propagation noise...", flush=True)
    demote_ids = []
    for r in cur.execute("""
        SELECT id, school, thesis, research_areas, classification_reason
        FROM people
        WHERE is_core_stat = 1 AND sources = 'AFT'
    """):
        rsn = r["classification_reason"] or ""
        # Keep explicit AFT biostatistics / public health / statistical institution profiles
        if rsn in ("AcademicTree Biostatistics Profile", "Public Health / Biostat Lineage", "Statistical Institution"):
            continue
        ra = r["research_areas"] or ""
        th = r["thesis"] or ""
        # Reject obvious non-PhD stub text like 'Emeritus' or 'Ph.D.'
        if th.strip().lower() in ("emeritus", "ph.d.", "phd", "professor"):
            th = ""
        # Reject explicit zoological/humanities noise
        if NON_STAT_THESIS_NOISE_RE.search(th) or NON_STAT_DOMAIN_REJECT_RE.search(th):
            demote_ids.append((r["id"],))
            continue
        # Keep if research_areas OR thesis has genuine statistical/biostatistical/probabilistic signal
        if GENUINE_STAT_AREA_RE.search(ra) or GENUINE_STAT_THESIS_BROAD_RE.search(th):
            continue
        # Otherwise (blank thesis + non-stat/blank research_areas propagated down neuro/evol/psych tree!), move to Related Quantitative!
        demote_ids.append((r["id"],))

    cur.executemany(
        "UPDATE people SET is_core_stat = 0, is_broad_stat = 1, primary_field = 'Related Quantitative' WHERE id = ?",
        demote_ids,
    )
    print(f"      Recalibrated {len(demote_ids):,} non-statistical AFT subtree nodes to Related Quantitative (0 records deleted!).", flush=True)

    print(f"\n[3/3] Integrating {len(oa_recs):,} OpenAlex dissertations & rebuilding DAG/FTS5...", flush=True)
    by_orcid = {}
    by_norm_school = defaultdict(list)
    by_fl_school = defaultdict(list)
    fac_by_school_fl = defaultdict(list)

    for r in cur.execute("SELECT id, name, norm_name, school, current_affiliation, year, thesis, orcid FROM people"):
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
    next_id = (cur.execute("SELECT MAX(id) FROM people").fetchone()[0] or 7_700_000) + 100
    existing_edges = set((r[0], r[1]) for r in cur.execute("SELECT advisor_id, student_id FROM edges").fetchall())

    added_p = 0
    enriched_p = 0
    added_e = 0

    for item in oa_recs:
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
            new_d = {"id": sid, "name": stu_nm, "norm_name": snk, "school": sch, "current_affiliation": "", "year": yr, "thesis": th, "orcid": oid}
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

    print(f"      OpenAlex Integration: +{added_p:,} net-new PhDs, +{added_e:,} advisor edges, and {enriched_p:,} enriched records!", flush=True)

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

    print("\n=== FINAL ANNUAL CORE STAT/BIOSTAT/PROB COUNTS (2006–2025) ===", flush=True)
    for yr in range(2006, 2026):
        c_yr = cur.execute("SELECT COUNT(*) FROM people WHERE is_core_stat=1 AND year=?", (yr,)).fetchone()[0]
        print(f"  {yr}: {c_yr:,}", flush=True)
    conn.close()
    print(f"DONE in {time.time() - t0:.1f}s! Summary: {json.dumps(meta)}", flush=True)


if __name__ == "__main__":
    main()
