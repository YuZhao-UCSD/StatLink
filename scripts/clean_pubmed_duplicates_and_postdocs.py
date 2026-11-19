#!/usr/bin/env python3
"""
Precision Cleanup & Middle-Initial Entity Resolution for PubMed Methodology Lineages
(`scripts/clean_pubmed_duplicates_and_postdocs.py`).

1. Removes any net-new PubMed record whose first token is only a 1-letter initial (e.g. 'X Wang', 'T Cai').
2. Removes any PubMed record/edge where (first_token, last_token) with compatible middle initial
   matches an existing faculty member or earlier PhD at the same school (e.g. 'Martin A Lindquist' ->
   'Martin Lindquist', 'Ciprian M Crainiceanu' -> 'Ciprian Mugur Crainiceanu', 'Eric J Tchetgen Tchetgen' ->
   'Eric Tchetgen Tchetgen') or a recent postdoc with a prior external PhD (within 18 years).
3. Merges middle-initial student variants at the same school (e.g. 'Lucy L Gao' -> 'Lucy Gao',
   'Harrison T Reeder' -> 'Harrison Thomas Reeder') onto the canonical student ID.
4. Recomputes DAG counts, schools, meta_stats, and FTS5 index.
"""

import json
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict, deque

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "scripts"))
from harvest_web_and_etds import first_last_sig, fold_ascii

DB_PATH = os.path.join(BASE_DIR, "data", "stat_genealogy.db")


def middle_initials(name: str):
    s = re.sub(r"\([^)]*\)|\"[^\"]*\"", " ", fold_ascii(name or ""))
    s = re.sub(r"\b(jr|sr|ii|iii|iv)\b", " ", s, flags=re.I)
    toks = [t for t in re.findall(r"[A-Za-z]+", s) if t]
    if len(toks) <= 2:
        return []
    return [t[0].lower() for t in toks[1:-1]]


def middle_compatible(nm1: str, nm2: str) -> bool:
    m1 = middle_initials(nm1)
    m2 = middle_initials(nm2)
    if not m1 or not m2:
        return True
    return m1[0] == m2[0]


def main():
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    fac_at_school = set((r[0], r[1]) for r in cur.execute("SELECT person_id, school FROM advisor_schools").fetchall())
    non_pm_by_fl = defaultdict(list)
    for r in cur.execute(
        "SELECT id, name, school, current_affiliation, year, student_count, sources FROM people WHERE sources != 'PubMed Methodology'"
    ):
        ft, lt = first_last_sig(r["name"])
        if ft and lt:
            non_pm_by_fl[(ft, lt)].append(dict(r))

    pm_rows = cur.execute("SELECT id, name, school, year FROM people WHERE sources = 'PubMed Methodology'").fetchall()

    deleted_ids = set()
    merged_pairs = []

    for r in pm_rows:
        pid = r["id"]
        nm = r["name"]
        sch = r["school"]
        yr = r["year"] or 2025

        # Guard 1: Reject 1-letter first initials (e.g. 'X Wang', 'T Cai', 'S C Kou')
        clean_toks = [t for t in re.findall(r"[A-Za-z]+", fold_ascii(nm)) if t]
        if not clean_toks or len(clean_toks[0]) < 2:
            deleted_ids.add(pid)
            continue

        ft, lt = first_last_sig(nm)
        cands = [c for c in non_pm_by_fl.get((ft, lt), ()) if middle_compatible(nm, c["name"])]

        action = None
        target_id = None
        for c in cands:
            # Guard 2: Existing faculty or prior PhD at the SAME school
            if (c["id"], sch) in fac_at_school or c["current_affiliation"] == sch:
                if c["student_count"] >= 1 or c["year"] is None or c["year"] <= yr - 3:
                    action = "delete"
                    break
            if c["school"] == sch and c["year"] and c["year"] <= yr - 3:
                action = "delete"
                break
            # Guard 3: Recent postdoc / visiting scholar who earned an external PhD 1..18 years prior
            if len(cands) <= 2 and c["school"] and c["school"] != sch and c["year"] and (1 <= yr - c["year"] <= 18):
                action = "delete"
                break
            # Guard 4: Same-school student within +-3 years -> merge middle-initial duplicate!
            if c["school"] == sch and (c["year"] is None or abs(c["year"] - yr) <= 3):
                action = "merge"
                target_id = c["id"]
                break

        if action == "delete":
            deleted_ids.add(pid)
        elif action == "merge" and target_id:
            merged_pairs.append((pid, target_id))

    # Also check any PubMed Methodology EDGES attached to existing people where student already had an earlier PhD or faculty role!
    bad_pm_edges = []
    for a, s in cur.execute("SELECT advisor_id, student_id FROM edges WHERE source = 'PubMed Methodology'").fetchall():
        if s in deleted_ids:
            continue
        sr = cur.execute("SELECT year, student_count, sources FROM people WHERE id = ?", (s,)).fetchone()
        if sr and sr["sources"] != "PubMed Methodology":
            if sr["student_count"] >= 2:
                bad_pm_edges.append((a, s))

    for a, s in bad_pm_edges:
        cur.execute("DELETE FROM edges WHERE advisor_id = ? AND student_id = ? AND source = 'PubMed Methodology'", (a, s))

    for dup_id, canon_id in merged_pairs:
        for a, src in cur.execute("SELECT advisor_id, source FROM edges WHERE student_id = ?", (dup_id,)).fetchall():
            if a != canon_id:
                cur.execute("INSERT OR IGNORE INTO edges VALUES (?, ?, ?)", (a, canon_id, src))
        cur.execute("DELETE FROM edges WHERE student_id = ? OR advisor_id = ?", (dup_id, dup_id))
        cur.execute("DELETE FROM advisor_schools WHERE person_id = ?", (dup_id,))
        cur.execute("DELETE FROM people WHERE id = ?", (dup_id,))

    for bad_id in deleted_ids:
        cur.execute("DELETE FROM edges WHERE student_id = ? OR advisor_id = ?", (bad_id, bad_id))
        cur.execute("DELETE FROM advisor_schools WHERE person_id = ?", (bad_id,))
        cur.execute("DELETE FROM people WHERE id = ?", (bad_id,))

    print(
        f"Cleaned {len(deleted_ids)} colleague/postdoc/initial false positives, merged {len(merged_pairs)} middle-initial student duplicates, and pruned {len(bad_pm_edges)} colleague edges!",
        flush=True,
    )

    # Recompute advisor_schools, DAG counts, schools, meta_stats & FTS5
    cur.execute("DELETE FROM advisor_schools WHERE person_id NOT IN (SELECT id FROM people)")
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
    conn.close()
    print(f"DONE in {time.time() - t0:.1f}s! Summary: {json.dumps(meta)}", flush=True)


if __name__ == "__main__":
    main()
