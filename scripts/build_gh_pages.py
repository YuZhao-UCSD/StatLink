#!/usr/bin/env python3
"""
Builds a 100% serverless static GitHub Pages bundle in `docs/` for StatLink.

Exports `data/stat_genealogy.db` into compact, CDN-friendly static assets (every file < 15 MB,
total < 85 MB, well under GitHub Pages' 1 GB limit) and copies `static/` + `static_api.js` into `docs/`
so https://<user>.github.io/StatLineage/ runs every feature (fuzzy search, Descendant Of, D3 Family Tree,
World-Famous Scientist Ancestors, Kinship/MRCA, Analytics & 2D Dissertation Topic Landscape) directly
in the browser with zero Python backend required.
"""

import gzip
import json
import os
import shutil
import sqlite3
import struct
import sys
import time
from collections import defaultdict

sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from famous_ancestors import FAMOUS_SCIENTISTS_LIST, REDUNDANT_PAIRS

DB_PATH = os.path.join(BASE_DIR, "data", "stat_genealogy.db")
STATIC_DIR = os.path.join(BASE_DIR, "static")
DOCS_DIR = os.path.join(BASE_DIR, "docs")
DOCS_DATA = os.path.join(DOCS_DIR, "data")
DOCS_DETAILS = os.path.join(DOCS_DATA, "details")

FIELDS_LIST = [
    "Statistics",
    "Biostatistics",
    "Probability",
    "Related Quantitative",
    "Mathematics & Other",
]
FIELD_TO_IDX = {f: i for i, f in enumerate(FIELDS_LIST)}
NUM_SHARDS = 256


def write_gz_json(path: str, obj):
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with gzip.open(path, "wb", compresslevel=6) as f:
        f.write(raw)
    return os.path.getsize(path)


def main():
    t0 = time.time()
    os.makedirs(DOCS_DETAILS, exist_ok=True)
    os.makedirs(os.path.join(DOCS_DIR, "static"), exist_ok=True)

    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row

    # 1. stats.json (matches GET /api/stats)
    row_meta = db.execute("SELECT value_json FROM meta_stats WHERE key = 'summary'").fetchone()
    summary = json.loads(row_meta["value_json"]) if row_meta else {}
    year_hist = defaultdict(dict)
    for yr, pf, cnt in db.execute(
        "SELECT year, primary_field, COUNT(*) FROM people WHERE is_core_stat = 1 AND year BETWEEN 1920 AND 2026 GROUP BY year, primary_field ORDER BY year ASC"
    ):
        year_hist[str(yr)][pf] = cnt
    summary["year_histogram"] = dict(year_hist)

    featured_ids = [18231, 12694, 4209, 14941, 47762]
    ph = ",".join("?" for _ in featured_ids)
    f_rows = {r["id"]: dict(r) for r in db.execute(f"SELECT * FROM people WHERE id IN ({ph})", featured_ids).fetchall()}
    famous_by_id = {x["id"]: x for x in FAMOUS_SCIENTISTS_LIST}
    featured = []
    for fid in featured_ids:
        r = f_rows.get(fid)
        if not r:
            continue
        fi = famous_by_id.get(fid)
        featured.append({
            "id": r["id"],
            "name": r["name"],
            "school": r["school"] or "",
            "year": r["year"],
            "descendant_count": r["descendant_count"],
            "famous_self": {
                "display_name": fi["display_name"],
                "short_name": fi["short_name"],
                "category": fi["category"],
                "epithet": fi["epithet"],
            } if fi else None,
        })

    top_schools = [
        dict(r)
        for r in db.execute(
            "SELECT school, aliases, country, core_stat_count, broad_stat_count, total_phd_count "
            "FROM schools WHERE core_stat_count > 0 ORDER BY core_stat_count DESC LIMIT 25"
        ).fetchall()
    ]
    with open(os.path.join(DOCS_DATA, "stats.json"), "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "featured": featured, "top_schools": top_schools}, f, ensure_ascii=False, separators=(",", ":"))

    # 2. topic_map.json (matches GET /api/topic_map)
    tm_row = db.execute("SELECT value_json FROM meta_stats WHERE key = 'dissertation_topic_map'").fetchone()
    if tm_row and tm_row["value_json"]:
        with open(os.path.join(DOCS_DATA, "topic_map.json"), "w", encoding="utf-8") as f:
            f.write(tm_row["value_json"])

    # 3. famous.json (109 curated historical/modern scientists + redundant pairs)
    famous_meta = {}
    fids = [x["id"] for x in FAMOUS_SCIENTISTS_LIST]
    ph_f = ",".join("?" for _ in fids)
    for r in db.execute(f"SELECT id, year, school FROM people WHERE id IN ({ph_f})", fids):
        famous_meta[r["id"]] = (r["year"], r["school"] or "")
    famous_out = []
    for x in FAMOUS_SCIENTISTS_LIST:
        yr, sch = famous_meta.get(x["id"], (None, ""))
        famous_out.append({**x, "year": yr, "school": sch})
    with open(os.path.join(DOCS_DATA, "famous.json"), "w", encoding="utf-8") as f:
        json.dump({"scientists": famous_out, "redundant_pairs": [list(p) for p in REDUNDANT_PAIRS]}, f, ensure_ascii=False, separators=(",", ":"))

    # 4. schools.json (dictionary-encoded schools)
    school_rows = [
        dict(r)
        for r in db.execute(
            "SELECT school, norm_school, COALESCE(aliases, '') AS aliases, COALESCE(country, '') AS country, "
            "core_stat_count, broad_stat_count, total_phd_count FROM schools ORDER BY core_stat_count DESC, total_phd_count DESC"
        ).fetchall()
    ]
    school_to_idx = {r["school"]: idx for idx, r in enumerate(school_rows)}
    with open(os.path.join(DOCS_DATA, "schools.json"), "w", encoding="utf-8") as f:
        json.dump(school_rows, f, ensure_ascii=False, separators=(",", ":"))

    # 5. advisor_schools.json.gz (school_idx -> list of faculty person_ids)
    adv_sch_map = defaultdict(list)
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='advisor_schools'").fetchone():
        for pid, sch in db.execute("SELECT person_id, school FROM advisor_schools"):
            sidx = school_to_idx.get(sch)
            if sidx is not None:
                adv_sch_map[str(sidx)].append(pid)
    adv_sz = write_gz_json(os.path.join(DOCS_DATA, "advisor_schools.json.gz"), dict(adv_sch_map))

    # 6. graph.bin.gz (packed uint32 pairs [advisor_id, student_id] for all ~590k edges)
    edges = db.execute("SELECT advisor_id, student_id FROM edges").fetchall()
    edge_buf = bytearray(len(edges) * 8)
    for i, (aid, sid) in enumerate(edges):
        struct.pack_into("<II", edge_buf, i * 8, aid, sid)
    graph_path = os.path.join(DOCS_DATA, "graph.bin.gz")
    with gzip.open(graph_path, "wb", compresslevel=6) as f:
        f.write(edge_buf)
    graph_sz = os.path.getsize(graph_path)

    # 7. people_index.json.gz (compact array for all ~550k scholars) + 256 detail shards
    # Compact row format: [id, name, norm_name, aliases, school_idx, year, field_idx, flags(is_core|is_broad<<1), adv_cnt, stu_cnt, desc_cnt, stat_desc_cnt]
    people_compact = []
    shards = [dict() for _ in range(NUM_SHARDS)]
    for r in db.execute("""
        SELECT id, name, norm_name, COALESCE(search_aliases, '') AS search_aliases,
               COALESCE(school, '') AS school, COALESCE(country, '') AS country,
               year, COALESCE(thesis, '') AS thesis, primary_field,
               is_core_stat, is_broad_stat, COALESCE(classification_reason, '') AS classification_reason,
               COALESCE(current_affiliation, '') AS current_affiliation,
               mgp_id, aft_pid, COALESCE(wikidata_qid, '') AS wikidata_qid,
               COALESCE(orcid, '') AS orcid, COALESCE(homepage, '') AS homepage,
               COALESCE(wikipedia_url, '') AS wikipedia_url,
               COALESCE(google_scholar_url, '') AS google_scholar_url,
               COALESCE(linkedin_url, '') AS linkedin_url,
               COALESCE(photo_url, '') AS photo_url,
               COALESCE(sources, '') AS sources,
               advisor_count, student_count, descendant_count, stat_descendant_count
        FROM people
        ORDER BY is_core_stat DESC, stat_descendant_count DESC, descendant_count DESC, student_count DESC, id ASC
    """):
        pid = r["id"]
        sidx = school_to_idx.get(r["school"], -1)
        fidx = FIELD_TO_IDX.get(r["primary_field"], 4)
        flags = (1 if r["is_core_stat"] else 0) | ((1 if r["is_broad_stat"] else 0) << 1)
        aliases = r["search_aliases"] if r["search_aliases"].lower() != r["norm_name"] else ""
        people_compact.append([
            pid,
            r["name"],
            r["norm_name"],
            aliases,
            sidx,
            r["year"] if r["year"] is not None else 0,
            fidx,
            flags,
            r["advisor_count"],
            r["student_count"],
            r["descendant_count"],
            r["stat_descendant_count"],
        ])
        # Only store non-empty rich detail fields to keep shards tiny!
        det = {}
        if r["thesis"]:
            det["t"] = r["thesis"]
        if r["country"]:
            det["c"] = r["country"]
        if r["current_affiliation"] and r["current_affiliation"] != r["school"]:
            det["a"] = r["current_affiliation"]
        if r["classification_reason"]:
            det["cr"] = r["classification_reason"]
        if r["homepage"]:
            det["hp"] = r["homepage"]
        if r["photo_url"]:
            det["ph"] = r["photo_url"]
        if r["wikipedia_url"]:
            det["wk"] = r["wikipedia_url"]
        if r["google_scholar_url"]:
            det["gs"] = r["google_scholar_url"]
        if r["linkedin_url"]:
            det["li"] = r["linkedin_url"]
        if r["orcid"]:
            det["or"] = r["orcid"]
        if r["wikidata_qid"]:
            det["wd"] = r["wikidata_qid"]
        if r["mgp_id"] is not None:
            det["mgp"] = r["mgp_id"]
        if r["aft_pid"] is not None:
            det["aft"] = r["aft_pid"]
        if r["sources"]:
            det["src"] = r["sources"]
        if det:
            shards[pid % NUM_SHARDS][str(pid)] = det

    pidx_sz = write_gz_json(os.path.join(DOCS_DATA, "people_index.json.gz"), people_compact)

    tot_shard_bytes = 0
    for s_i, s_dict in enumerate(shards):
        sp = os.path.join(DOCS_DETAILS, f"s{s_i:03d}.json")
        with open(sp, "w", encoding="utf-8") as f:
            json.dump(s_dict, f, ensure_ascii=False, separators=(",", ":"))
        tot_shard_bytes += os.path.getsize(sp)

    # 8. Copy static assets (styles.css, app.js, static_api.js) & generate docs/index.html + .nojekyll
    for fn in ("styles.css", "app.js", "static_api.js"):
        src_p = os.path.join(STATIC_DIR, fn)
        if os.path.exists(src_p):
            shutil.copy2(src_p, os.path.join(DOCS_DIR, "static", fn))

    with open(os.path.join(STATIC_DIR, "index.html"), "r", encoding="utf-8") as f:
        html_txt = f.read()

    # Convert root-relative `/static/...` paths to relative `static/...` so GitHub Pages project URLs
    # (https://<user>.github.io/StatLineage/) resolve cleanly, and inject `static/static_api.js` before `app.js`!
    html_txt = html_txt.replace('href="/static/', 'href="static/')
    html_txt = html_txt.replace('src="/static/app.js', 'src="static/static_api.js?v=2"></script>\n  <script src="static/app.js')
    with open(os.path.join(DOCS_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(html_txt)

    with open(os.path.join(DOCS_DIR, ".nojekyll"), "w", encoding="utf-8") as f:
        f.write("")

    db.close()
    print(f"Built GitHub Pages static bundle in {DOCS_DIR} in {time.time() - t0:.1f}s:")
    print(f"  people_index.json.gz : {pidx_sz / 1048576:.2f} MB ({len(people_compact):,} scholars)")
    print(f"  graph.bin.gz         : {graph_sz / 1048576:.2f} MB ({len(edges):,} edges)")
    print(f"  advisor_schools.gz   : {adv_sz / 1048576:.2f} MB")
    print(f"  256 detail shards    : {tot_shard_bytes / 1048576:.2f} MB total (avg {tot_shard_bytes / (NUM_SHARDS * 1024):.1f} KB/shard)")


if __name__ == "__main__":
    main()
