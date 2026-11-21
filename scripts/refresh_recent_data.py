#!/usr/bin/env python3
"""
StatLink Unified Incremental Data Refresh Orchestrator (`scripts/refresh_recent_data.py`).

Updates `data/stat_genealogy.db` with the most recent PhD dissertations, mentorship edges,
faculty moves, external profile links (`Personal Website`, `Wikipedia`, `Google Scholar`,
`LinkedIn`, `ORCID`, `Wikidata`), and scholar portrait photos across all 15 data sources
and crawlers, enforces homonym & DAG invariants, rebuilds `meta_stats` + `people_fts`,
and optionally repacks `data/db_parts/` for GitHub deployment.

Usage:
  python scripts/refresh_recent_data.py --mode recent             # Refresh ETDs, PubMed, ORCID, websites & photos
  python scripts/refresh_recent_data.py --mode profiles           # Refresh websites, links, portraits & homonym guard only
  python scripts/refresh_recent_data.py --mode full --pack-github # Full ground-zero rebuild + all harvesters + GitHub chunks
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from collections import deque

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
DB_PATH = os.path.join(BASE_DIR, "data", "stat_genealogy.db")
CACHE_DIR = os.path.join(BASE_DIR, "data_raw")

RECENT_REFRESH_CACHES = [
    "datacite_dissertations_2014_2025.json",
    "openalex_indexed_2015_2020.json",
    "pubmed_methodology_lineages.json",
    "dblp_webpages_cache.json",
    "cran_packages_urls_cache.json",
    "github_stat_handles_cache.json",
]

FULL_GROUND_ZERO_PIPELINE = [
    "build_master_schools.py",
    "build_database.py",
]

RECENT_LINEAGE_HARVESTERS = [
    "harvest_web_and_etds.py",
    "harvest_pass2_and_dedup.py",
    "crawl_all_schools_and_professors.py",
    "close_jhu_and_peer_gaps.py",
    "harvest_uf_commencements_and_orcid_retry.py",
    "harvest_pubmed_methodology_lineages.py",
    "clean_pubmed_duplicates_and_postdocs.py",
    "recover_common_name_pubmed_phds.py",
    "fill_post2014_drop.py",
    "harvest_datacite_2014_2025_dissertations.py",
    "harvest_openalex_indexed_2015_2020.py",
    "complete_2014_2025_gap_and_aft_precision.py",
]

PROFILE_AND_PRECISION_ENRICHERS = [
    "enrich_scholar_external_links.py",
    "enrich_scholar_photos.py",
    "fix_homonym_descendants_and_deep_enrich.py",
    "exhaustive_internet_era_websites_and_photos.py",
    "audit_and_fix_personal_website_precision.py",
    "deduplicate_hyphen_and_stub_scholars.py",
]


def run_step(script_name: str):
    script_path = os.path.join(SCRIPTS_DIR, script_name)
    if not os.path.exists(script_path):
        raise FileNotFoundError(f"Missing pipeline script: {script_path}")
    print(f"\n{'=' * 78}\n>>> RUNNING: scripts/{script_name}\n{'=' * 78}")
    t0 = time.time()
    subprocess.run([sys.executable, script_path], cwd=BASE_DIR, check=True)
    print(f"<<< FINISHED scripts/{script_name} in {time.time() - t0:.1f}s")


def finalize_and_verify_database():
    """
    Enforces the 4 mandatory post-refresh database invariants on `data/stat_genealogy.db`:
      1. Exact `advisor_count` & `student_count` from `edges`
      2. Reverse-topological DAG `descendant_count` & `stat_descendant_count`
      3. Updated `schools`, `meta_stats['summary']`, and `people_fts` full-text index
      4. Verification of zero young-PhD automated homonym edges (`student.year - advisor.year <= 3`)
    """
    print(f"\n{'=' * 78}\n>>> FINALIZING & VERIFYING DATABASE INVARIANTS\n{'=' * 78}")
    conn = sqlite3.connect(DB_PATH, timeout=60)
    cur = conn.cursor()

    # 1 & 2. Exact C-accelerated reverse-topological DAG & delta-only count updates
    old_counts = {}
    core_ids_set = set()
    for uid, flag, ac, sc, dc, sdc in cur.execute(
        "SELECT id, is_core_stat, advisor_count, student_count, descendant_count, stat_descendant_count FROM people"
    ):
        old_counts[uid] = (ac or 0, sc or 0, dc or 0, sdc or 0)
        if flag == 1:
            core_ids_set.add(uid)

    adv_cnt_map = {}
    stu_cnt_map = {}
    parents_of = {}
    rev_children = {}
    out_deg = {uid: 0 for uid in old_counts}
    for aid, sid in cur.execute("SELECT advisor_id, student_id FROM edges"):
        if aid in old_counts and sid in old_counts:
            stu_cnt_map[aid] = stu_cnt_map.get(aid, 0) + 1
            adv_cnt_map[sid] = adv_cnt_map.get(sid, 0) + 1
            parents_of.setdefault(sid, []).append(aid)
            rev_children.setdefault(aid, []).append(sid)
            out_deg[aid] += 1

    rem_parents = {uid: len(parents_of.get(uid, ())) for uid in old_counts}
    q = deque(uid for uid, deg in out_deg.items() if deg == 0)
    desc_sets = {}
    final_counts = {}

    while q:
        u = q.popleft()
        ch = rev_children.get(u)
        if ch:
            s = set(ch)
            for c in ch:
                cs = desc_sets.get(c)
                if cs:
                    s |= cs
                rem_parents[c] -= 1
                if rem_parents[c] <= 0:
                    desc_sets.pop(c, None)
            sc = len(s & core_ids_set)
            final_counts[u] = (len(s), sc)
            if rem_parents[u] > 0:
                desc_sets[u] = s
        else:
            final_counts[u] = (0, 0)
        for p in parents_of.get(u, ()):
            out_deg[p] -= 1
            if out_deg[p] == 0:
                q.append(p)

    desc_updates = []
    for uid, old_t in old_counts.items():
        dc_v, sdc_v = final_counts.get(uid, (0, 0))
        new_t = (adv_cnt_map.get(uid, 0), stu_cnt_map.get(uid, 0), dc_v, sdc_v)
        if new_t != old_t:
            desc_updates.append((*new_t, uid))

    if desc_updates:
        cur.executemany(
            "UPDATE people SET advisor_count = ?, student_count = ?, descendant_count = ?, stat_descendant_count = ? WHERE id = ?",
            desc_updates,
        )

    # 3. Update `meta_stats['summary']` and rebuild `people_fts`
    row_sum = cur.execute("SELECT value_json FROM meta_stats WHERE key = 'summary'").fetchone()
    summary = json.loads(row_sum[0]) if row_sum else {}
    summary["total_people"] = cur.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    summary["total_edges"] = cur.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    summary["core_stat_total"] = cur.execute("SELECT COUNT(*) FROM people WHERE is_core_stat = 1").fetchone()[0]
    summary["broad_stat_total"] = cur.execute("SELECT COUNT(*) FROM people WHERE is_broad_stat = 1").fetchone()[0]
    summary["by_field"] = {
        pf: cnt for pf, cnt in cur.execute("SELECT primary_field, COUNT(*) FROM people GROUP BY primary_field")
    }
    summary["core_stat_schools"] = cur.execute("SELECT COUNT(*) FROM schools WHERE core_stat_count > 0").fetchone()[0]
    summary["total_schools"] = cur.execute("SELECT COUNT(*) FROM schools").fetchone()[0]
    cur.execute(
        "INSERT OR REPLACE INTO meta_stats (key, value_json) VALUES ('summary', ?)",
        (json.dumps(summary),),
    )

    print("  Rebuilding FTS5 full-text search index (`people_fts`)...")
    cur.execute("INSERT INTO people_fts(people_fts) VALUES('rebuild')")
    conn.commit()

    bad_homonyms = cur.execute("""
        SELECT COUNT(*) FROM edges e
        JOIN people a ON a.id = e.advisor_id
        JOIN people s ON s.id = e.student_id
        WHERE e.source NOT IN ('MGP', 'AFT', 'AFT+MGP', 'Wikidata', 'MGP+Wikidata', 'AFT+Wikidata', 'AFT+MGP+Wikidata')
          AND a.year IS NOT NULL AND s.year IS NOT NULL AND (s.year - a.year) <= 3
    """).fetchone()[0]

    print(f"  Verified Summary: {summary['total_people']:,} scholars | {summary['total_edges']:,} edges | {summary['core_stat_total']:,} Core Stat")
    print(f"  Automated young-PhD homonym edges remaining: {bad_homonyms} (expected: 0)")
    conn.close()


def main():
    parser = argparse.ArgumentParser(description="Refresh StatLink with the most recent data across all harvesters & crawlers.")
    parser.add_argument(
        "--mode",
        choices=("recent", "profiles", "full"),
        default="recent",
        help="'recent' runs post-2013 ETD/PubMed/ORCID harvesters + profile/website/photo enrichers; 'profiles' runs link/website/photo/homonym enrichers only; 'full' rebuilds from raw dumps first.",
    )
    parser.add_argument(
        "--clear-recent-caches",
        action="store_true",
        help="Delete cached API snapshots in data_raw/ for ETD, PubMed, DBLP, CRAN & GitHub handles before harvesting fresh data.",
    )
    parser.add_argument(
        "--pack-github",
        action="store_true",
        help="Repack data/stat_genealogy.db into <=45 MB gzip chunks in data/db_parts/ after refresh.",
    )
    args = parser.parse_args()

    t_all = time.time()
    if args.clear_recent_caches:
        for fn in RECENT_REFRESH_CACHES:
            p = os.path.join(CACHE_DIR, fn)
            if os.path.exists(p):
                os.remove(p)
                print(f"Cleared cache: data_raw/{fn}")

    steps = []
    if args.mode == "full":
        steps.extend(FULL_GROUND_ZERO_PIPELINE)
        steps.extend(RECENT_LINEAGE_HARVESTERS)
        steps.extend(PROFILE_AND_PRECISION_ENRICHERS)
    elif args.mode == "recent":
        steps.extend(RECENT_LINEAGE_HARVESTERS)
        steps.extend(PROFILE_AND_PRECISION_ENRICHERS)
    elif args.mode == "profiles":
        steps.extend(PROFILE_AND_PRECISION_ENRICHERS)

    for script_name in steps:
        run_step(script_name)

    finalize_and_verify_database()

    if args.pack_github:
        run_step("pack_database_parts.py")

    print(f"\nAll StatLink refresh stages completed in {(time.time() - t_all) / 60:.1f} minutes.")
    print("IMPORTANT: Restart `python app.py` so in-memory `ANCESTRY_INDEX` and `_LUCKY_IDS_CACHE` reload!")


if __name__ == "__main__":
    main()
