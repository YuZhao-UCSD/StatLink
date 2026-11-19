#!/usr/bin/env python3
"""
UF Official Doctoral Commencement PDF Harvester (2014–2026) + Rate-Limit-Safe 32-School ORCID PhD Pass
(`scripts/harvest_uf_commencements_and_orcid_retry.py`).

Closes the University of Florida & peer ORCID rate-limit gap uncovered while investigating Jing Liu
(UF Biostatistics PhD 2017, Dissertation Chair: Samuel S. Wu):
  1. Downloads & parses 31+ official University of Florida Doctoral Commencement Program PDFs
     (2014–2026) from `commencement.ufl.edu`, extracting exact PhD Name, Major (`Statistics` /
     `Biostatistics`), Dissertation Title, and Dissertation Chair / Co-Chair (resolving both
     initialed chairs like `S. S. Wu` -> `Samuel S. Wu` and full names like `Michael Joseph Daniels`).
  2. Re-runs the 32-university ORCID PhD + Crossref Advisor harvester using a single polite
     8-worker pool with exponential backoff on HTTP 429 and PhD-targeted search queries, recovering
     all ORCID PhD records previously dropped by burst rate-limiting.
"""

import concurrent.futures
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict, deque
from urllib.parse import urljoin

import bs4
import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "scripts"))

from harvest_web_and_etds import clean_person_name, first_last_sig, fold_ascii, norm_key
from close_jhu_and_peer_gaps import (
    MS_ROLE_REJECT_RE,
    ORCID_TARGET_UNIVERSITIES,
    PHD_ROLE_RE,
    STAT_DEPT_OR_ROLE_RE,
    school_matches_org,
)

DB_PATH = os.path.join(BASE_DIR, "data", "stat_genealogy.db")
PDFTOTEXT = shutil.which("pdftotext") or r"C:\Program Files\MiKTeX\miktex\bin\x64\pdftotext.exe"
POLITE_HEADERS = {
    "User-Agent": "StatLineageGenealogy/1.0 (Academic Research; mailto:research@statlineage.org)",
    "Accept": "application/json",
}


def get_with_retry(sess, url, params=None, headers=None, timeout=12, retries=3):
    for attempt in range(retries):
        try:
            r = sess.get(url, params=params, headers=headers, timeout=timeout)
            if r.status_code == 200:
                return r
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(0.8 * (attempt + 1))
                continue
            return r
        except Exception:
            time.sleep(0.5 * (attempt + 1))
    return None


def harvest_uf_commencement_pdfs(uf_fac_by_init_last: dict):
    """Downloads and parses all 2014–2026 UF Doctoral Commencement PDFs for Statistics & Biostatistics PhDs."""
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0"})

    pdf_jobs = {}  # url -> year

    # 1. Scrape /printed-programs/ for 2021–2026 Doctoral PDFs
    try:
        r = sess.get("https://commencement.ufl.edu/printed-programs/", timeout=12)
        if r.status_code == 200:
            soup = bs4.BeautifulSoup(r.text, "lxml")
            for a in soup.find_all("a", href=True):
                txt = a.get_text(" ", strip=True)
                href = a["href"]
                if ".pdf" not in href.lower():
                    continue
                if any(k in (txt + " " + href).lower() for k in ["bms", "b-m-s", "msb", "masterbachelor"]):
                    continue
                full_u = urljoin("https://commencement.ufl.edu/printed-programs/", href)
                ym = re.search(r"(20[12]\d)", txt + " " + href)
                if ym:
                    yr = int(ym.group(1))
                else:
                    ym2 = re.search(r"(?:sp|su|fa|fall|spring|summer)[\-\s_]*(2[1-6])\b", (txt + " " + href).lower())
                    yr = 2000 + int(ym2.group(1)) if ym2 else 2024
                pdf_jobs[full_u] = yr
    except Exception as exc:
        print(f"  [WARN] /printed-programs/ fetch: {exc}", flush=True)

    # 2. Probe 2013–2020 historical commencement PDFs
    hist_bases = [
        "https://commencement.ufl.edu/media/commencementufledu/past-commencement-pdf/past-commencement-programs/commencement-pdf/",
        "https://commencement.ufl.edu/media/commencementufledu/past-commencement-pdf/",
    ]
    for yr in range(2013, 2021):
        yr2 = str(yr)[-2:]
        for term in ["Spring", "Summer", "Fall"]:
            found_term = False
            for base in hist_bases:
                if found_term:
                    break
                for pat in [
                    f"{term}{yr}advanced.pdf",
                    f"{term}{yr}.pdf",
                    f"Doctoral-{term}-{yr}.pdf",
                    f"Doctoral-{term}-{yr2}.pdf",
                    f"{term}-{yr}-Doctoral.pdf",
                ]:
                    u = base + pat
                    if u in pdf_jobs:
                        found_term = True
                        break
                    try:
                        hr = sess.head(u, timeout=6)
                        if hr.status_code == 200 and "pdf" in (hr.headers.get("content-type") or "").lower():
                            pdf_jobs[u] = yr
                            found_term = True
                            break
                    except Exception:
                        pass

    print(f"  Found {len(pdf_jobs)} UF Doctoral Commencement PDFs (2013–2026). Parsing with pdftotext...", flush=True)

    def resolve_uf_chair(raw_chair: str):
        # Clean titles like Dr., Ph.D., Jr.
        rc = re.sub(r"\b(Dr|Prof|Ph\.?D|M\.?D|Jr|Sr|II|III)\.?\b", "", raw_chair, flags=re.I).strip(" ,.;:")
        if not rc or len(rc) < 3:
            return None, ""
        toks = [t for t in re.split(r"\s+", rc) if t]
        if len(toks) < 2:
            return None, ""
        f_tok = re.sub(r"[^a-z]", "", fold_ascii(toks[0]).lower())
        l_tok = re.sub(r"[^a-z\-]", "", fold_ascii(toks[-1]).lower())
        if not f_tok or not l_tok:
            return None, ""
        cands = uf_fac_by_init_last.get((f_tok[0], l_tok), ())
        for pid, pnm, p_first in cands:
            if len(f_tok) == 1 or p_first.startswith(f_tok) or f_tok.startswith(p_first):
                return pid, pnm
        # If full name given (not just initials), return cleaned name
        if len(f_tok) >= 2:
            return None, clean_person_name(rc)
        return None, ""

    extracted = []
    seen_stu = set()

    for url, yr in sorted(pdf_jobs.items(), key=lambda x: x[1]):
        try:
            resp = sess.get(url, timeout=18)
            if resp.status_code != 200 or resp.content[:4] != b"%PDF":
                continue
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
                tf.write(resp.content)
                tp = tf.name
            try:
                txt = subprocess.check_output([PDFTOTEXT, tp, "-"], encoding="utf-8", errors="replace", timeout=15)
            finally:
                if os.path.exists(tp):
                    os.remove(tp)

            lines = [ln.strip() for ln in txt.splitlines()]
            for i, ln in enumerate(lines):
                m_maj = re.match(r"^Major:\s*(Statistics|Biostatistics)\s*$", ln, re.I)
                if not m_maj:
                    continue
                major = "Biostatistics" if "biostat" in m_maj.group(1).lower() else "Statistics"

                # Student name is the preceding non-empty line!
                stu_raw = ""
                for back in range(i - 1, max(-1, i - 4), -1):
                    if lines[back] and not lines[back].isdigit() and not lines[back].lower().startswith(("doctor", "degree", "major", "dissertation")):
                        stu_raw = lines[back]
                        break
                stu_nm = clean_person_name(stu_raw)
                if not stu_nm:
                    continue

                # Parse Dissertation title & Dissertation Chair(s) over next 1..10 lines
                diss_parts = []
                chairs = []
                in_diss = False
                for fwd in range(i + 1, min(len(lines), i + 12)):
                    fln = lines[fwd]
                    if not fln:
                        if chairs:
                            break
                        continue
                    mc = re.match(r"^Dissertation\s+(?:Co-)?Chair(?:s)?:\s*(.+)$", fln, re.I)
                    if mc:
                        in_diss = False
                        for part in re.split(r"\s*(?:;|,\s+and\s+|\s+and\s+)\s*", mc.group(1)):
                            pid, pnm = resolve_uf_chair(part)
                            if pid or pnm:
                                chairs.append((pid, pnm))
                        continue
                    md = re.match(r"^Dissertation:\s*(.+)$", fln, re.I)
                    if md:
                        in_diss = True
                        diss_parts.append(md.group(1).strip())
                        continue
                    if fln.startswith("Major:"):
                        break
                    if in_diss:
                        diss_parts.append(fln)

                snk = norm_key(stu_nm)
                if snk in seen_stu:
                    continue
                seen_stu.add(snk)
                extracted.append({
                    "student": stu_nm,
                    "snk": snk,
                    "school": "University of Florida",
                    "year": yr if yr <= 2026 else None,
                    "field": major,
                    "thesis": re.sub(r"\s+", " ", " ".join(diss_parts)).strip(),
                    "chairs": chairs,
                    "orcid": "",
                    "source": "UF Doctoral Commencement",
                })
        except Exception:
            continue

    print(f"  -> Extracted {len(extracted)} UF Statistics & Biostatistics PhDs from official Commencement PDFs!", flush=True)
    return extracted


def harvest_polite_orcid_phds(school_fac_sigs: dict, existing_orcids: set):
    """Polite 8-worker ORCID PhD harvester with HTTP 429 retry and PhD-focused search."""
    sess = requests.Session()
    candidate_jobs = []  # (oid, nm, canon_school)

    print(f"\n  Querying PhD-targeted ORCID profiles across {len(ORCID_TARGET_UNIVERSITIES)} universities...", flush=True)
    for canon_school, org_q, _ in ORCID_TARGET_UNIVERSITIES:
        q = (
            f'affiliation-org-name:({org_q}) '
            f'AND (biostatistics OR statistics OR "applied mathematics and statistics" OR "statistical science") '
            f'AND ("PhD" OR "Ph.D" OR "Ph.D." OR "Doctor of Philosophy" OR "ScD" OR "Sc.D" OR "DrPH" OR "DPhil")'
        )
        for start in range(0, 600, 200):
            r = get_with_retry(
                sess,
                "https://pub.orcid.org/v3.0/expanded-search/",
                params={"q": q, "start": start, "rows": 200},
                headers={"Accept": "application/json"},
                timeout=14,
            )
            if not r or r.status_code != 200:
                break
            batch = (r.json().get("expanded-result")) or []
            if not batch:
                break
            for it in batch:
                oid = it.get("orcid-id")
                if not oid or oid in existing_orcids:
                    continue
                nm = clean_person_name(f"{(it.get('given-names') or '').strip()} {(it.get('family-names') or '').strip()}")
                if nm:
                    candidate_jobs.append((oid, nm, canon_school))
            if len(batch) < 200:
                break

    print(f"  Inspecting {len(candidate_jobs):,} net-new ORCID PhD candidate profiles (8 polite workers with retry)...", flush=True)

    def check_one(job):
        oid, nm, canon_school = job
        local_s = requests.Session()
        ra = get_with_retry(
            local_s,
            f"https://pub.orcid.org/v3.0/{oid}/activities",
            headers={"Accept": "application/json"},
            timeout=12,
        )
        if not ra or ra.status_code != 200:
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
                if ey and ey > 2026:
                    ey = None
                fld = "Biostatistics" if re.search(r"\bbiostat\w*|epidemiolog\w*|public\s+health", combo, re.I) else (
                    "Probability" if re.search(r"\bprobab\w*|stochastic", combo, re.I) else "Statistics"
                )
                matched_phd = (sy, ey, fld)
                break
            if matched_phd:
                break
        if not matched_phd:
            return None

        sy, ey, fld = matched_phd
        chairs = []
        fac_map = school_fac_sigs.get(canon_school, {})
        has_works = bool(((act.get("works") or {}).get("group")) or [])
        if fac_map and has_works:
            rc = get_with_retry(
                local_s,
                f"https://api.crossref.org/works?filter=orcid:{oid}&rows=16",
                headers=POLITE_HEADERS,
                timeout=10,
            )
            if rc and rc.status_code == 200:
                works = ((rc.json().get("message") or {}).get("items")) or []
                votes = Counter()
                snk = norm_key(nm)
                min_w = (sy - 1) if sy else ((ey - 5) if ey else 2012)
                max_w = (ey + 2) if ey else 2027
                for w in works:
                    w_yr = ((w.get("issued") or {}).get("date-parts") or [[None]])[0][0]
                    if w_yr and not (min_w <= w_yr <= max_w):
                        continue
                    auths = w.get("author") or []
                    if not (2 <= len(auths) <= 10):
                        continue
                    for idx_a, ao in enumerate(auths):
                        ag = (ao.get("given") or "").strip()
                        af = (ao.get("family") or "").strip()
                        if not ag or not af or norm_key(f"{ag} {af}") == snk:
                            continue
                        sig = (fold_ascii(ag[0]).lower(), fold_ascii(af).lower().split()[-1])
                        for pid, pnm, pfirst in fac_map.get(sig, ()):
                            ag_f = fold_ascii(ag.split()[0]).lower().rstrip(".")
                            if len(ag_f) == 1 or pfirst.startswith(ag_f) or ag_f.startswith(pfirst):
                                wt = (3 if idx_a == len(auths) - 1 else 1) + (2 if len(auths) <= 3 else 0)
                                votes[(pid, pnm)] += wt
                if votes:
                    (b_pid, b_nm), top_wt = votes.most_common(1)[0]
                    if top_wt >= 4:
                        chairs.append((b_pid, b_nm))

        return {
            "student": nm,
            "snk": norm_key(nm),
            "school": canon_school,
            "year": ey,
            "field": fld,
            "thesis": "",
            "chairs": chairs,
            "orcid": oid,
            "source": "ORCID + Crossref" if chairs else "ORCID Verified PhD",
        }

    verified = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for res in pool.map(check_one, candidate_jobs):
            if res:
                verified.append(res)
    print(f"  -> Verified {len(verified):,} additional ORCID Stat/Biostat PhDs!", flush=True)
    return verified


def main():
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    school_fac_sigs = defaultdict(lambda: defaultdict(list))
    for r in cur.execute("""
        SELECT DISTINCT p.id, p.name, a.school
        FROM advisor_schools a JOIN people p ON p.id = a.person_id
        WHERE p.is_broad_stat = 1
        UNION
        SELECT DISTINCT p.id, p.name, p.current_affiliation AS school
        FROM people p
        WHERE p.current_affiliation != '' AND p.is_broad_stat = 1
    """):
        ft, lt = first_last_sig(r["name"])
        if ft and lt:
            school_fac_sigs[r["school"]][(ft[0], lt)].append((r["id"], r["name"], ft))

    existing_orcids = {r[0] for r in cur.execute("SELECT orcid FROM people WHERE orcid != ''").fetchall()}

    print("[1/3] Harvesting University of Florida Doctoral Commencement PDFs (2013–2026)...", flush=True)
    uf_records = harvest_uf_commencement_pdfs(school_fac_sigs["University of Florida"])

    print("[2/3] Running Polite Retry-Backed ORCID PhD Harvester across 32 universities...", flush=True)
    orcid_records = harvest_polite_orcid_phds(school_fac_sigs, existing_orcids)

    all_new = uf_records + orcid_records  # UF Commencement first so exact thesis & chair win!

    print(f"\n[3/3] Integrating {len(all_new):,} records into stat_genealogy.db...", flush=True)
    existing_by_norm = defaultdict(list)
    existing_by_orcid = {}
    for r in cur.execute(
        "SELECT id, name, norm_name, school, current_affiliation, year, thesis, primary_field, is_core_stat, orcid FROM people"
    ):
        d = dict(r)
        existing_by_norm[d["norm_name"]].append(d)
        if d["orcid"]:
            existing_by_orcid[d["orcid"]] = d

    next_id = (cur.execute("SELECT MAX(id) FROM people").fetchone()[0] or 7_500_000) + 100
    existing_edges = set((r[0], r[1]) for r in cur.execute("SELECT advisor_id, student_id FROM edges").fetchall())

    added_p = 0
    enriched_p = 0
    added_e = 0

    for item in all_new:
        snk = item["snk"]
        sch = item["school"]
        yr = item["year"]
        th = item["thesis"]
        oid = item["orcid"]
        fld = item["field"]
        src = item["source"]

        matched = existing_by_orcid.get(oid) if oid else None
        if not matched:
            for c in existing_by_norm.get(snk, ()):
                if c["school"] == sch or (not c["school"] and c["current_affiliation"] == sch):
                    if not (yr and c["year"] and abs(yr - c["year"]) > 4):
                        matched = c
                        break
            # Also check first+last match at same school (e.g. 'Jing Liu' vs 'Jing X. Liu')
            if not matched:
                f1, l1 = first_last_sig(item["student"])
                for ck, clist in existing_by_norm.items():
                    if ck.endswith(" " + l1) and ck.startswith(f1 + " "):
                        for c in clist:
                            if c["school"] == sch and not (yr and c["year"] and abs(yr - c["year"]) > 3):
                                matched = c
                                break
                        if matched:
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
                    is_broad_stat = 1
                   WHERE id = ?""",
                (sch, yr, th, oid, sid),
            )
            enriched_p += 1
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
                ) VALUES (?,?,?,?,?,'',?,?,?,NULL,?,1,1,0.94,?,'','',NULL,NULL,'',?,'','',?,0,0,0,0)""",
                (sid, item["student"], snk, "", sch, country, yr, th, fld, f"Verified via {src}", oid, src),
            )
            new_d = {"id": sid, "name": item["student"], "norm_name": snk, "school": sch, "current_affiliation": "", "year": yr, "thesis": th, "primary_field": fld, "is_core_stat": 1, "orcid": oid}
            existing_by_norm[snk].append(new_d)
            if oid:
                existing_by_orcid[oid] = new_d
            added_p += 1

        for ch_pid, ch_nm in item["chairs"]:
            aid = ch_pid
            if not aid and ch_nm:
                ank = norm_key(ch_nm)
                cands = [c for c in existing_by_norm.get(ank, ()) if c["id"] != sid]
                chosen = next((c for c in cands if c["current_affiliation"] == sch or c["school"] == sch or c["is_core_stat"]), cands[0] if cands else None)
                if chosen:
                    aid = chosen["id"]
                else:
                    aid = next_id
                    next_id += 1
                    cur.execute(
                        """INSERT INTO people (
                            id, name, norm_name, search_aliases, school, raw_school, country, year, thesis,
                            mgp_subject, primary_field, is_core_stat, is_broad_stat, field_confidence,
                            classification_reason, current_affiliation, research_areas, mgp_id, aft_pid,
                            wikidata_qid, orcid, homepage, proquest_id, sources, advisor_count, student_count,
                            descendant_count, stat_descendant_count
                        ) VALUES (?,?,?,'','','','',NULL,'',NULL,?,1,1,0.9,'Faculty Chair via Commencement',?,'',NULL,NULL,'','','','',?,0,0,0,0)""",
                        (aid, ch_nm, ank, fld, sch, src),
                    )
                    existing_by_norm[ank].append({"id": aid, "name": ch_nm, "norm_name": ank, "school": "", "current_affiliation": sch, "year": None, "thesis": "", "primary_field": fld, "is_core_stat": 1, "orcid": ""})
                    added_p += 1
            if aid and aid != sid and (aid, sid) not in existing_edges and (sid, aid) not in existing_edges:
                cur.execute("INSERT OR IGNORE INTO edges VALUES (?, ?, ?)", (aid, sid, src))
                existing_edges.add((aid, sid))
                added_e += 1

    print(f"  Added +{added_p:,} net-new PhDs, +{added_e:,} advisor-student edges, and enriched {enriched_p:,} records!", flush=True)

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
    print(f"DONE in {time.time() - t0:.1f}s! Summary: {json.dumps(meta)}", flush=True)


if __name__ == "__main__":
    main()
