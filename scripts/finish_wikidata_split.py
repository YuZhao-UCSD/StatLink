#!/usr/bin/env python3
"""
Fetches the massive Wikidata doctoral advisor-student (`P184`) & employer (`P108`) graph
split cleanly by occupation (avoiding the 60s single-response streaming cutoff) and integrates
all missing researchers, affiliations, and doctoral connections into `stat_genealogy.db`.
"""

import json
import os
import sqlite3
import sys
import urllib.parse
import urllib.request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

from build_database import canonicalize_school
from harvest_web_and_etds import clean_person_name, norm_key

DB_PATH = os.path.join(BASE_DIR, "data", "stat_genealogy.db")


def run_sparql(q: str):
    url = "https://query.wikidata.org/sparql?format=json&query=" + urllib.parse.quote(q)
    req = urllib.request.Request(url, headers={"User-Agent": "StatLineageExplorer/2.1 (Academic Research)"})
    with urllib.request.urlopen(req, timeout=45) as resp:
        return json.loads(resp.read().decode("utf-8"))["results"]["bindings"]


def main():
    occs = [
        ("wd:Q19501955", "Biostatistics"),  # biostatistician
        ("wd:Q17167049", "Probability"),    # probabilist
        ("wd:Q2732142", "Statistics"),      # statistician
        ("wd:Q15995642", "Related Quantitative"),  # econometrician
    ]

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    by_mgp = {}
    by_qid = {}
    by_norm = {}
    for r in cur.execute("SELECT id, name, norm_name, school, current_affiliation, mgp_id, wikidata_qid FROM people"):
        d = dict(r)
        if d["mgp_id"]:
            by_mgp[d["mgp_id"]] = d
        if d["wikidata_qid"]:
            by_qid[d["wikidata_qid"]] = d
        by_norm.setdefault(d["norm_name"], []).append(d)

    existing_edges = set((r[0], r[1]) for r in cur.execute("SELECT advisor_id, student_id FROM edges").fetchall())
    next_id = (cur.execute("SELECT MAX(id) FROM people").fetchone()[0] or 6_500_000) + 10

    added_p = 0
    added_e = 0
    updated_affil = 0

    for occ_wd, default_field in occs:
        q = f"""
        SELECT ?item ?itemLabel ?mgp ?orcid ?advisor ?advisorLabel ?advisorMgp ?empLabel WHERE {{
          ?advisor wdt:P106 {occ_wd} .
          ?item wdt:P184 ?advisor .
          OPTIONAL {{ ?item wdt:P549 ?mgp . }}
          OPTIONAL {{ ?item wdt:P496 ?orcid . }}
          OPTIONAL {{ ?advisor wdt:P549 ?advisorMgp . }}
          OPTIONAL {{ ?item wdt:P108 ?emp . }}
          SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en,fr,de,es". }}
        }}
        """
        try:
            rows = run_sparql(q)
            print(f"Fetched {len(rows):,} Wikidata P184 rows for {occ_wd} ({default_field})")
        except Exception as exc:
            print(f"Warn {occ_wd}: {exc}")
            continue

        for row in rows:
            qid = row["item"]["value"].rsplit("/", 1)[-1]
            adv_qid = row["advisor"]["value"].rsplit("/", 1)[-1]
            nm = clean_person_name(row.get("itemLabel", {}).get("value", ""))
            adv_nm = clean_person_name(row.get("advisorLabel", {}).get("value", ""))
            if not nm or nm.startswith("Q") or not adv_nm or adv_nm.startswith("Q"):
                continue

            mid = int(row["mgp"]["value"]) if ("mgp" in row and row["mgp"]["value"].isdigit()) else None
            adv_mid = int(row["advisorMgp"]["value"]) if ("advisorMgp" in row and row["advisorMgp"]["value"].isdigit()) else None
            emp = canonicalize_school(row["empLabel"]["value"]) if "empLabel" in row else ""
            orcid = row.get("orcid", {}).get("value", "")

            # Resolve advisor
            adv_rec = by_mgp.get(adv_mid) or by_qid.get(adv_qid)
            if not adv_rec:
                ank = norm_key(adv_nm)
                if len(by_norm.get(ank, ())) == 1:
                    adv_rec = by_norm[ank][0]
            if not adv_rec:
                aid = next_id
                next_id += 1
                ank = norm_key(adv_nm)
                ic = 1 if default_field in ("Statistics", "Biostatistics", "Probability") else 0
                cur.execute(
                    """INSERT INTO people (id, name, norm_name, search_aliases, school, raw_school, country, year, thesis,
                       mgp_subject, primary_field, is_core_stat, is_broad_stat, field_confidence, classification_reason,
                       current_affiliation, research_areas, mgp_id, aft_pid, wikidata_qid, orcid, homepage, proquest_id,
                       sources, advisor_count, student_count, descendant_count, stat_descendant_count)
                       VALUES (?,?,?,'','','','',NULL,'',NULL,?,?,1,0.9,'Wikidata Doctoral Graph','','',NULL,NULL,?,'','','','Wikidata',0,0,0,0)""",
                    (aid, adv_nm, ank, default_field, ic, adv_qid),
                )
                cur.execute("INSERT INTO people_fts(rowid, name, search_aliases, school, thesis, research_areas) VALUES (?,?,?,'','','')", (aid, adv_nm, ""))
                adv_rec = {"id": aid, "name": adv_nm, "norm_name": ank, "school": "", "current_affiliation": "", "mgp_id": None, "wikidata_qid": adv_qid}
                by_qid[adv_qid] = adv_rec
                by_norm.setdefault(ank, []).append(adv_rec)
                added_p += 1

            # Resolve student
            stu_rec = by_mgp.get(mid) or by_qid.get(qid)
            if not stu_rec:
                snk = norm_key(nm)
                if len(by_norm.get(snk, ())) == 1:
                    stu_rec = by_norm[snk][0]
            if not stu_rec:
                sid = next_id
                next_id += 1
                snk = norm_key(nm)
                ic = 1 if default_field in ("Statistics", "Biostatistics", "Probability") else 0
                cur.execute(
                    """INSERT INTO people (id, name, norm_name, search_aliases, school, raw_school, country, year, thesis,
                       mgp_subject, primary_field, is_core_stat, is_broad_stat, field_confidence, classification_reason,
                       current_affiliation, research_areas, mgp_id, aft_pid, wikidata_qid, orcid, homepage, proquest_id,
                       sources, advisor_count, student_count, descendant_count, stat_descendant_count)
                       VALUES (?,?,?,'','','','',NULL,'',NULL,?,?,1,0.88,'Wikidata Doctoral Graph',?,'',NULL,NULL,?,?,'','','Wikidata',0,0,0,0)""",
                    (sid, nm, snk, default_field, ic, emp, qid, orcid),
                )
                cur.execute("INSERT INTO people_fts(rowid, name, search_aliases, school, thesis, research_areas) VALUES (?,?,?,'','','')", (sid, nm, ""))
                stu_rec = {"id": sid, "name": nm, "norm_name": snk, "school": "", "current_affiliation": emp, "mgp_id": None, "wikidata_qid": qid}
                by_qid[qid] = stu_rec
                by_norm.setdefault(snk, []).append(stu_rec)
                if emp:
                    cur.execute("INSERT OR IGNORE INTO advisor_schools VALUES (?, ?, 0)", (sid, emp))
                added_p += 1
            else:
                sid = stu_rec["id"]
                if emp and not stu_rec["current_affiliation"]:
                    cur.execute("UPDATE people SET current_affiliation = ? WHERE id = ?", (emp, sid))
                    cur.execute("INSERT OR IGNORE INTO advisor_schools VALUES (?, ?, 0)", (sid, emp))
                    stu_rec["current_affiliation"] = emp
                    updated_affil += 1

            aid = adv_rec["id"]
            if aid != sid and (aid, sid) not in existing_edges and (sid, aid) not in existing_edges:
                cur.execute("INSERT OR IGNORE INTO edges VALUES (?, ?, 'Wikidata')", (aid, sid))
                existing_edges.add((aid, sid))
                cur.execute("UPDATE people SET student_count = student_count + 1, descendant_count = MAX(descendant_count, student_count + 1) WHERE id = ?", (aid,))
                cur.execute("UPDATE people SET advisor_count = advisor_count + 1 WHERE id = ?", (sid,))
                added_e += 1

    # Also pull current employer (`P108`) for all statisticians/biostatisticians/probabilists on Wikidata!
    q_emp = """
    SELECT ?mgp ?empLabel WHERE {
      VALUES ?occ { wd:Q2732142 wd:Q19501955 wd:Q17167049 }
      ?item wdt:P106 ?occ ; wdt:P549 ?mgp ; wdt:P108 ?emp .
      SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
    }
    """
    try:
        emp_rows = run_sparql(q_emp)
        for r in emp_rows:
            if r["mgp"]["value"].isdigit():
                mid = int(r["mgp"]["value"])
                emp = canonicalize_school(r["empLabel"]["value"])
                if mid in by_mgp and emp and "university" in emp.lower() or "institute" in emp.lower() or "college" in emp.lower() or "eth" in emp.lower():
                    uid = by_mgp[mid]["id"]
                    cur.execute("UPDATE people SET current_affiliation = CASE WHEN current_affiliation='' THEN ? ELSE current_affiliation END WHERE id=?", (emp, uid))
                    cur.execute("INSERT OR IGNORE INTO advisor_schools VALUES (?, ?, 0)", (uid, emp))
                    updated_affil += 1
    except Exception as exc:
        print("Warn emp query:", exc)

    # Update meta_stats summary
    meta = json.loads(cur.execute("SELECT value_json FROM meta_stats WHERE key='summary'").fetchone()[0])
    meta["total_people"] = cur.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    meta["total_edges"] = cur.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    meta["core_stat_total"] = cur.execute("SELECT COUNT(*) FROM people WHERE is_core_stat=1").fetchone()[0]
    meta["broad_stat_total"] = cur.execute("SELECT COUNT(*) FROM people WHERE is_broad_stat=1").fetchone()[0]
    meta["by_field"] = dict(cur.execute("SELECT primary_field, COUNT(*) FROM people GROUP BY primary_field").fetchall())
    cur.execute("INSERT OR REPLACE INTO meta_stats VALUES ('summary', ?)", (json.dumps(meta),))
    conn.commit()
    conn.close()
    print(f"Split Wikidata Complete! Added +{added_p:,} people, +{added_e:,} doctoral edges, +{updated_affil:,} faculty affiliations!")
    print("Final Summary:", json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
