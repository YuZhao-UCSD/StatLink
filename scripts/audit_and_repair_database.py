#!/usr/bin/env python3
"""
Comprehensive Full-Database Audit & Repair Pipeline for StatLink.

Executes 5 surgical passes across all ~552k scholars and ~592k doctoral edges:
  Pass 1: UC eScholarship Master's-vs-PhD Degree Audit (removes MS/MA theses via ProQuest degree code `_XXXX[NMO]_` vs `_XXXXD_`)
  Pass 2: Chronological Edge Audit (purges >75yr gaps, non-MGP student.year < advisor.year homonyms, extreme non-honorary MGP inversions)
  Pass 3: Global Deduplication & Metadata Hygiene (HTML unescaping, 1,304+ duplicate direct students under same advisor,
          shared wikidata_qid/orcid/wikipedia_url/google_scholar_url resolution, placeholder photo_url & broken/shared URL cleanup)
  Pass 4: Live Web Verification Pass on Top Priority Core Stat Advisors (verifies homepage & photo_url HTTP health)
  Pass 5: Fast DAG Finalization (recomputes advisor_count, student_count, topological descendant_count, schools, meta_stats, people_fts)
"""

import concurrent.futures
import html as html_mod
import json
import os
import re
import socket
import sqlite3
import sys
import time
import unicodedata
from collections import Counter, defaultdict, deque

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
socket.setdefaulttimeout(8.0)
sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "data", "stat_genealogy.db")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

UC_CAMPUSES = [
    ("ucb_etd", "University of California, Berkeley"),
    ("ucsd_etd", "University of California, San Diego"),
    ("ucla_etd", "University of California, Los Angeles"),
    ("ucd_etd", "University of California, Davis"),
    ("ucsb_etd", "University of California, Santa Barbara"),
    ("uci_etd", "University of California, Irvine"),
    ("ucsc_etd", "University of California, Santa Cruz"),
    ("ucr_etd", "University of California, Riverside"),
    ("ucsf_etd", "University of California, San Francisco"),
    ("ucm_etd", "University of California, Merced"),
]

ESCHOL_DEGREE_QUERY = """
query($unitId: ID!, $more: String) {
  unit(id: $unitId) {
    items(first: 300, more: $more) {
      more
      nodes {
        title
        published
        nativeFileName
        localIDs { id subScheme }
      }
    }
  }
}
"""

HTML_TAG_RE = re.compile(r"</?(?:q|i|b|em|strong|sup|sub|span|p|br|div|font)[^>]*>", re.I)
BAD_PHOTO_RE = re.compile(
    r"(no[-_]?portrait|placeholder|default[-_]?avatar|default[-_]?profile|default[-_]?photo|"
    r"silhouette|mystery[-_]?man|gravatar\.com|blank[-_]?profile|avatar[-_]?blank)",
    re.I,
)


def fold_ascii(text: str) -> str:
    if not text:
        return ""
    text = (
        text.replace("ø", "o").replace("Ø", "O")
        .replace("ł", "l").replace("Ł", "L")
        .replace("ß", "ss").replace("đ", "d").replace("Đ", "D")
        .replace("æ", "ae").replace("Æ", "AE")
        .replace("œ", "oe").replace("Œ", "OE")
    )
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


def norm_name_str(name: str) -> str:
    s = fold_ascii(name or "").lower()
    s = re.sub(r"[^a-z0-9\s\-]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_title_str(title: str) -> str:
    s = fold_ascii(title or "").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return s.strip()


def clean_html_text(val: str) -> str:
    if not val:
        return val
    s = html_mod.unescape(val)
    s = HTML_TAG_RE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def name_tokens(name: str) -> list:
    return [t for t in re.findall(r"[a-z]+", fold_ascii(name or "").lower()) if len(t) >= 1]


def same_person_compatible(r1: dict, r2: dict, max_yr_diff: int = 4) -> bool:
    t1 = name_tokens(r1["name"])
    t2 = name_tokens(r2["name"])
    if not t1 or not t2:
        return False
    # Surname check (handle minor 1-char typo like McGreevey vs McGreevy or compound hyphenation)
    s1, s2 = t1[-1], t2[-1]
    if s1 != s2 and not (len(s1) >= 5 and len(s2) >= 5 and (s1[:5] == s2[:5] or s1 in "".join(t2) or s2 in "".join(t1))):
        return False
    # First name / initial compatibility
    f1, f2 = t1[0], t2[0]
    first_ok = (
        f1 == f2
        or f1[0] == f2[0]
        or (len(t1) >= 2 and t1[1][0] == f2[0])
        or (len(t2) >= 2 and t2[1][0] == f1[0])
        or "".join(t1[:-1]) == "".join(t2[:-1])
    )
    if not first_ok:
        return False
    y1, y2 = r1["year"], r2["year"]
    if y1 is not None and y2 is not None and abs(y1 - y2) > max_yr_diff:
        return False
    return True


def canonical_score(r: dict) -> tuple:
    return (
        1 if r["mgp_id"] is not None else 0,
        1 if r["aft_pid"] is not None else 0,
        1 if r["is_core_stat"] else 0,
        1 if r["year"] is not None else 0,
        (r["student_count"] or 0) + (r["advisor_count"] or 0),
        sum(
            1
            for k in ("homepage", "photo_url", "wikipedia_url", "google_scholar_url", "orcid", "wikidata_qid", "thesis")
            if (r.get(k) or "").strip()
        ),
        -r["id"],
    )


def merge_person_into(db: sqlite3.Connection, keep_id: int, drop_id: int) -> bool:
    if keep_id == drop_id:
        return False
    kr = db.execute("SELECT * FROM people WHERE id = ?", (keep_id,)).fetchone()
    dr = db.execute("SELECT * FROM people WHERE id = ?", (drop_id,)).fetchone()
    if not kr or not dr:
        return False
    k = dict(kr)
    d = dict(dr)

    # 1. Re-point edges (advisor_id and student_id)
    for e in db.execute("SELECT advisor_id, student_id, source FROM edges WHERE advisor_id = ?", (drop_id,)).fetchall():
        sid = e["student_id"]
        if sid != keep_id:
            db.execute(
                "INSERT OR IGNORE INTO edges (advisor_id, student_id, source) VALUES (?, ?, ?)",
                (keep_id, sid, e["source"]),
            )
    for e in db.execute("SELECT advisor_id, student_id, source FROM edges WHERE student_id = ?", (drop_id,)).fetchall():
        aid = e["advisor_id"]
        if aid != keep_id:
            db.execute(
                "INSERT OR IGNORE INTO edges (advisor_id, student_id, source) VALUES (?, ?, ?)",
                (aid, keep_id, e["source"]),
            )
    db.execute("DELETE FROM edges WHERE advisor_id = ? OR student_id = ?", (drop_id, drop_id))

    # 2. Re-point advisor_schools
    for r_as in db.execute("SELECT school, advised_at_school FROM advisor_schools WHERE person_id = ?", (drop_id,)).fetchall():
        db.execute(
            "INSERT OR IGNORE INTO advisor_schools (person_id, school, advised_at_school) VALUES (?, ?, ?)",
            (keep_id, r_as["school"], r_as["advised_at_school"]),
        )
    db.execute("DELETE FROM advisor_schools WHERE person_id = ?", (drop_id,))

    # 3. Merge metadata onto keep_id (after clearing UNIQUE columns on drop_id if needed)
    merged_sources = ",".join(
        sorted(set(s.strip() for s in (k["sources"] or "").split(",") + (d["sources"] or "").split(",") if s.strip()))
    )
    alias_parts = [a.strip() for a in (k["search_aliases"] or "").split("|") + (d["search_aliases"] or "").split("|") if a.strip()]
    if d["name"] and d["name"].lower() != (k["name"] or "").lower():
        alias_parts.append(d["name"])
    merged_aliases = " | ".join(dict.fromkeys(alias_parts))

    updates = {
        "sources": merged_sources or k["sources"],
        "search_aliases": merged_aliases,
        "is_core_stat": 1 if (k["is_core_stat"] or d["is_core_stat"]) else 0,
        "is_broad_stat": 1 if (k["is_broad_stat"] or d["is_broad_stat"]) else 0,
    }
    if k["primary_field"] == "Mathematics & Other" and d["primary_field"] != "Mathematics & Other":
        updates["primary_field"] = d["primary_field"]
    for col in (
        "school", "raw_school", "country", "year", "thesis", "mgp_subject",
        "current_affiliation", "research_areas", "wikidata_qid", "orcid",
        "homepage", "wikipedia_url", "google_scholar_url", "linkedin_url",
        "photo_url", "proquest_id",
    ):
        kv = k.get(col)
        dv = d.get(col)
        if (kv is None or (isinstance(kv, str) and not kv.strip())) and (dv is not None and (not isinstance(dv, str) or dv.strip())):
            updates[col] = dv
        elif col == "thesis" and isinstance(kv, str) and isinstance(dv, str) and len(dv.strip()) > len(kv.strip()) + 15:
            updates[col] = dv

    # Handle UNIQUE columns mgp_id / aft_pid safely
    take_mgp = k["mgp_id"] is None and d["mgp_id"] is not None
    take_aft = k["aft_pid"] is None and d["aft_pid"] is not None

    # Delete drop_id first so UNIQUE constraints on mgp_id / aft_pid don't collide
    db.execute("DELETE FROM people WHERE id = ?", (drop_id,))
    if take_mgp:
        updates["mgp_id"] = d["mgp_id"]
    if take_aft:
        updates["aft_pid"] = d["aft_pid"]

    set_clause = ", ".join(f"{col} = ?" for col in updates)
    db.execute(f"UPDATE people SET {set_clause} WHERE id = ?", list(updates.values()) + [keep_id])
    return True


# ---------------------------------------------------------------------------
# PASS 1: UC eScholarship Master's-vs-PhD Degree Audit
# ---------------------------------------------------------------------------

def scan_uc_campus_masters(unit_id: str, school_name: str, target_pqids: set, target_titles: set):
    """Scans a UC campus eScholarship unit and returns (masters_pqids, masters_titles)."""
    masters_pqids = set()
    masters_titles = set()
    session = requests.Session()
    more = None
    pages = 0
    matched_items = 0
    while pages < 70:
        pages += 1
        try:
            resp = session.post(
                "https://escholarship.org/graphql",
                json={"query": ESCHOL_DEGREE_QUERY, "variables": {"unitId": unit_id, "more": more}},
                headers=HEADERS,
                timeout=25,
            )
            if resp.status_code != 200:
                break
            items = (((resp.json().get("data") or {}).get("unit") or {}).get("items")) or {}
            nodes = items.get("nodes") or []
            if not nodes:
                break
            old_cnt = 0
            for n in nodes:
                pub = n.get("published") or ""
                yr = int(pub[:4]) if len(pub) >= 4 and pub[:4].isdigit() else None
                if yr and yr < 2004:
                    old_cnt += 1
                nfn = (n.get("nativeFileName") or "").strip()
                m_deg = re.search(r"_\d{4}([A-Za-z])_", nfn)
                is_master = bool(m_deg and m_deg.group(1).upper() != "D")
                if not is_master:
                    continue
                pqid = ""
                for lid in (n.get("localIDs") or []):
                    if lid.get("subScheme") == "proquest":
                        m = re.search(r"(\d{6,10})", lid.get("id") or "")
                        if m:
                            pqid = m.group(1)
                nt = norm_title_str(n.get("title") or "")
                if pqid and pqid in target_pqids:
                    masters_pqids.add(pqid)
                    matched_items += 1
                if nt and nt in target_titles:
                    masters_titles.add(nt)
                    matched_items += 1
            more = items.get("more")
            if not more or old_cnt >= len(nodes) * 0.8:
                break
        except Exception:
            break
    print(f"    [{unit_id}] scanned {pages} pages -> found {len(masters_pqids)} Master's pqids, {len(masters_titles)} Master's titles matching DB")
    return masters_pqids, masters_titles


def pass1_uc_escholarship_masters_audit(db: sqlite3.Connection):
    print("\n=== PASS 1: UC eScholarship Master's-vs-PhD Degree Audit ===")
    rows = db.execute("""
        SELECT id, name, school, year, thesis, proquest_id, sources, student_count
        FROM people
        WHERE sources = 'eScholarship' AND student_count = 0
    """).fetchall()
    print(f"  Candidate eScholarship-only rows (student_count=0): {len(rows):,}")
    if not rows:
        return 0

    by_school = defaultdict(list)
    for r in rows:
        by_school[r["school"] or ""].append(dict(r))

    all_masters_pqids = set()
    all_masters_titles = set()
    tasks = []
    for unit_id, school_name in UC_CAMPUSES:
        cands = by_school.get(school_name, [])
        if not cands:
            continue
        pq_set = {c["proquest_id"].strip() for c in cands if (c["proquest_id"] or "").strip()}
        ti_set = {norm_title_str(c["thesis"]) for c in cands if (c["thesis"] or "").strip()}
        tasks.append((unit_id, school_name, pq_set, ti_set))

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        futs = [ex.submit(scan_uc_campus_masters, *t) for t in tasks]
        for f in concurrent.futures.as_completed(futs):
            mpq, mti = f.result()
            all_masters_pqids.update(mpq)
            all_masters_titles.update(mti)

    to_delete = []
    for r in rows:
        pq = (r["proquest_id"] or "").strip()
        nt = norm_title_str(r["thesis"] or "")
        if (pq and pq in all_masters_pqids) or (nt and nt in all_masters_titles):
            to_delete.append(r["id"])

    if to_delete:
        for i in range(0, len(to_delete), 500):
            chunk = to_delete[i : i + 500]
            ph = ",".join("?" for _ in chunk)
            db.execute(f"DELETE FROM edges WHERE advisor_id IN ({ph}) OR student_id IN ({ph})", chunk + chunk)
            db.execute(f"DELETE FROM advisor_schools WHERE person_id IN ({ph})", chunk)
            db.execute(f"DELETE FROM people WHERE id IN ({ph})", chunk)
        db.commit()

    yw_left = db.execute("SELECT COUNT(*) FROM edges WHERE advisor_id = 106752").fetchone()[0]
    fs_left = db.execute("SELECT COUNT(*) FROM edges WHERE advisor_id = 1379616").fetchone()[0]
    print(f"  Removed {len(to_delete):,} UC eScholarship Master's (MS/MA) thesis records!")
    print(f"  Post-Pass1 direct students: Yingnian Wu (106752) = {yw_left}, Frederic Schoenberg (1379616) = {fs_left}")
    return len(to_delete)


# ---------------------------------------------------------------------------
# PASS 2: Chronological Edge Audit
# ---------------------------------------------------------------------------

def pass2_chronological_edge_audit(db: sqlite3.Connection):
    print("\n=== PASS 2: Chronological Edge Audit ===")
    # 2a. Impossible >75 year gap
    cur1 = db.execute("""
        DELETE FROM edges
        WHERE rowid IN (
            SELECT e.rowid FROM edges e
            JOIN people a ON a.id = e.advisor_id
            JOIN people s ON s.id = e.student_id
            WHERE a.year IS NOT NULL AND s.year IS NOT NULL AND (s.year - a.year) > 75
        )
    """)
    del_huge = cur1.rowcount

    # 2b. Non-MGP edges where student.year < advisor.year (100% homonym/inverted errors)
    cur2 = db.execute("""
        DELETE FROM edges
        WHERE rowid IN (
            SELECT e.rowid FROM edges e
            JOIN people a ON a.id = e.advisor_id
            JOIN people s ON s.id = e.student_id
            WHERE a.year IS NOT NULL AND s.year IS NOT NULL
              AND s.year < a.year
              AND e.source NOT LIKE '%MGP%'
        )
    """)
    del_non_mgp_neg = cur2.rowcount

    # 2c. Extreme non-honorary MGP impossibilities (advisor graduated >25 years AFTER student)
    cur3 = db.execute("""
        DELETE FROM edges
        WHERE rowid IN (
            SELECT e.rowid FROM edges e
            JOIN people a ON a.id = e.advisor_id
            JOIN people s ON s.id = e.student_id
            WHERE a.year IS NOT NULL AND s.year IS NOT NULL
              AND (a.year - s.year) > 25
              AND LOWER(COALESCE(a.school, '') || ' ' || COALESCE(a.raw_school, '')) NOT LIKE '%honorary%'
        )
    """)
    del_mgp_extreme = cur3.rowcount

    db.commit()
    print(f"  Deleted >75yr impossible edges: {del_huge}")
    print(f"  Deleted non-MGP student.year < advisor.year edges: {del_non_mgp_neg}")
    print(f"  Deleted extreme non-honorary MGP inverted edges (>25yr): {del_mgp_extreme}")
    return del_huge + del_non_mgp_neg + del_mgp_extreme


# ---------------------------------------------------------------------------
# PASS 3: Global Deduplication & Metadata Hygiene
# ---------------------------------------------------------------------------

def pass3_dedup_and_metadata_hygiene(db: sqlite3.Connection):
    print("\n=== PASS 3: Global Deduplication & Metadata Hygiene ===")

    # 3a. Clean HTML entities & tags in name, school, thesis + fix future year > 2027
    html_rows = db.execute("""
        SELECT id, name, school, thesis FROM people
        WHERE name LIKE '%&%' OR name LIKE '%<%'
           OR school LIKE '%&%' OR school LIKE '%<%'
           OR thesis LIKE '%<%' OR thesis LIKE '%&amp;%' OR thesis LIKE '%&#%'
    """).fetchall()
    cleaned_html = 0
    for r in html_rows:
        nn = clean_html_text(r["name"] or "")
        ns = clean_html_text(r["school"] or "")
        nt = clean_html_text(r["thesis"] or "")
        if nn != (r["name"] or "") or ns != (r["school"] or "") or nt != (r["thesis"] or ""):
            db.execute(
                "UPDATE people SET name = ?, norm_name = ?, school = ?, thesis = ? WHERE id = ?",
                (nn, norm_name_str(nn), ns, nt, r["id"]),
            )
            cleaned_html += 1

    fut_cur = db.execute("UPDATE people SET year = 2026 WHERE year > 2027")
    print(f"  3a. Cleaned HTML entities/tags on {cleaned_html:,} rows; clamped {fut_cur.rowcount} future years (>2027).")

    # 3b. Merge duplicate direct students under the same advisor (same norm_name)
    dup_groups = db.execute("""
        SELECT e.advisor_id, s.norm_name
        FROM edges e
        JOIN people s ON s.id = e.student_id
        GROUP BY e.advisor_id, s.norm_name
        HAVING COUNT(*) > 1
    """).fetchall()
    merged_stu = 0
    for g in dup_groups:
        members = [
            dict(r)
            for r in db.execute(
                """
                SELECT p.* FROM edges e
                JOIN people p ON p.id = e.student_id
                WHERE e.advisor_id = ? AND p.norm_name = ?
                """,
                (g["advisor_id"], g["norm_name"]),
            ).fetchall()
        ]
        if len(members) < 2:
            continue
        members.sort(key=canonical_score, reverse=True)
        keep = members[0]
        for other in members[1:]:
            if other["year"] is not None and keep["year"] is not None and abs(other["year"] - keep["year"]) > 3:
                continue
            if merge_person_into(db, keep["id"], other["id"]):
                merged_stu += 1
    db.commit()
    print(f"  3b. Merged {merged_stu:,} duplicate direct students under the same advisor.")

    # 3c. Deduplicate or resolve shared wikidata_qid, orcid, wikipedia_url, google_scholar_url
    merged_id_dups = 0
    cleared_bad_ids = 0
    for col in ("wikidata_qid", "orcid", "wikipedia_url", "google_scholar_url"):
        shared = db.execute(f"""
            SELECT LOWER(TRIM({col})) AS val, COUNT(*) AS cnt
            FROM people
            WHERE {col} IS NOT NULL AND TRIM({col}) != ''
            GROUP BY LOWER(TRIM({col}))
            HAVING cnt > 1
        """).fetchall()
        for sg in shared:
            members = [
                dict(r)
                for r in db.execute(
                    f"SELECT * FROM people WHERE LOWER(TRIM({col})) = ?", (sg["val"],)
                ).fetchall()
            ]
            if len(members) < 2:
                continue
            members.sort(key=canonical_score, reverse=True)
            # First merge compatible duplicates into cluster heads
            clusters = []
            for m in members:
                placed = False
                for head in clusters:
                    if same_person_compatible(head, m, max_yr_diff=4):
                        if merge_person_into(db, head["id"], m["id"]):
                            merged_id_dups += 1
                        placed = True
                        break
                if not placed:
                    clusters.append(m)
            # If >= 2 distinct incompatible people still share the same external ID:
            if len(clusters) >= 2:
                # Special case: Albert Einstein Google Scholar ID `qc6CJjYAAAAJ` copied onto unrelated people
                if col == "google_scholar_url" and "qc6cjy" in sg["val"].replace("j", "j"):
                    pass
                if col == "google_scholar_url" and len(clusters) >= 3:
                    # Keep on famous Einstein (id=53269) if present, else clear on all!
                    for c in clusters:
                        if c["id"] == 53269 and "qc6cjjyaaaaj" in sg["val"]:
                            continue
                        db.execute(f"UPDATE people SET {col} = '' WHERE id = ?", (c["id"],))
                        cleared_bad_ids += 1
                else:
                    # Clear ambiguous shared ID on all non-canonical incompatible rows (or all if >= 3)
                    for idx, c in enumerate(clusters):
                        if idx == 0 and len(clusters) == 2 and col == "wikipedia_url":
                            # Check if wikipedia slug contains cluster[0]'s surname
                            s0 = name_tokens(c["name"])[-1] if name_tokens(c["name"]) else ""
                            if s0 and s0 in sg["val"]:
                                continue
                        db.execute(f"UPDATE people SET {col} = '' WHERE id = ?", (c["id"],))
                        cleared_bad_ids += 1
    db.commit()
    print(f"  3c. Merged {merged_id_dups:,} duplicate scholars via shared IDs; cleared {cleared_bad_ids:,} mismatched shared IDs.")

    # 3d. Clean placeholder & shared photo_url, broken/shared linkedin_url, shared/translate homepage
    bad_photos = 0
    for r in db.execute("SELECT id, photo_url FROM people WHERE photo_url IS NOT NULL AND TRIM(photo_url) != ''").fetchall():
        if BAD_PHOTO_RE.search(r["photo_url"]):
            db.execute("UPDATE people SET photo_url = '' WHERE id = ?", (r["id"],))
            bad_photos += 1

    shared_photos = db.execute("""
        SELECT LOWER(TRIM(photo_url)) AS val FROM people
        WHERE photo_url IS NOT NULL AND TRIM(photo_url) != ''
        GROUP BY LOWER(TRIM(photo_url)) HAVING COUNT(*) >= 2
    """).fetchall()
    shared_photo_cleared = 0
    for sp in shared_photos:
        c = db.execute("UPDATE people SET photo_url = '' WHERE LOWER(TRIM(photo_url)) = ?", (sp["val"],)).rowcount
        shared_photo_cleared += c

    bad_li = db.execute("""
        UPDATE people SET linkedin_url = ''
        WHERE linkedin_url LIKE '%linkedin.com/in/https:%'
           OR LOWER(TRIM(linkedin_url)) IN (
               SELECT LOWER(TRIM(linkedin_url)) FROM people
               WHERE linkedin_url IS NOT NULL AND TRIM(linkedin_url) != ''
               GROUP BY LOWER(TRIM(linkedin_url)) HAVING COUNT(*) >= 2
           )
    """).rowcount

    bad_hp = db.execute("""
        UPDATE people SET homepage = ''
        WHERE homepage LIKE '%translate.google.com%'
           OR LOWER(TRIM(homepage)) IN (
               SELECT LOWER(TRIM(homepage)) FROM people
               WHERE homepage IS NOT NULL AND TRIM(homepage) != ''
               GROUP BY LOWER(TRIM(homepage)) HAVING COUNT(*) >= 2
           )
    """).rowcount

    db.commit()
    print(f"  3d. Cleared {bad_photos:,} placeholder photos, {shared_photo_cleared:,} shared/banner photos, {bad_li:,} broken/dept LinkedIn URLs, {bad_hp:,} shared/translate homepages.")
    return merged_stu + merged_id_dups


# ---------------------------------------------------------------------------
# PASS 4: Live Web Verification on Priority Core Stat Advisors
# ---------------------------------------------------------------------------

FLAGSHIP_SCHOOLS = (
    "University of California, San Diego",
    "Stanford University",
    "University of California, Berkeley",
    "University of California, Los Angeles",
    "Harvard University",
    "Johns Hopkins University",
)


def check_photo_live(url: str) -> bool:
    """Returns True if photo_url is a live, valid image; False if dead (404/410/text-html/tiny)."""
    if not url or not url.startswith("http"):
        return False
    sess = requests.Session()
    for verify in (True, False):
        try:
            r = sess.get(url, headers=HEADERS, timeout=7, stream=True, verify=verify)
            ct = (r.headers.get("Content-Type") or "").lower()
            if r.status_code in (404, 410):
                r.close()
                return False
            if r.status_code == 200:
                chunk = next(r.iter_content(2048), b"")
                r.close()
                if "text/html" in ct:
                    return False
                if len(chunk) < 400 and ("image" not in ct):
                    return False
                return True
            r.close()
            # Keep 403/429 (Cloudflare/bot protection on valid university CDN)
            return True
        except requests.exceptions.SSLError:
            if verify:
                continue
            return True
        except Exception:
            return True
    return True


def check_homepage_live(url: str) -> bool:
    """Returns False only on confirmed hard 404/410 or domain-for-sale parked pages."""
    if not url or not url.startswith("http"):
        return False
    sess = requests.Session()
    for verify in (True, False):
        try:
            r = sess.head(url, headers=HEADERS, timeout=7, allow_redirects=True, verify=verify)
            if r.status_code in (404, 410):
                # Double-check with GET (some servers reject HEAD with 404)
                r2 = sess.get(url, headers=HEADERS, timeout=7, allow_redirects=True, verify=verify)
                if r2.status_code in (404, 410):
                    return False
                return True
            return True
        except requests.exceptions.SSLError:
            if verify:
                continue
            return True
        except Exception:
            return True
    return True


def pass4_live_web_verification(db: sqlite3.Connection):
    print("\n=== PASS 4: Live Web Verification on Priority Core Stat Advisors ===")
    ph_sch = ",".join("?" for _ in FLAGSHIP_SCHOOLS)
    rows = db.execute(f"""
        SELECT id, name, school, year, homepage, photo_url
        FROM people
        WHERE is_core_stat = 1
          AND (TRIM(COALESCE(homepage, '')) != '' OR TRIM(COALESCE(photo_url, '')) != '')
          AND (
              id IN (
                  SELECT id FROM people
                  WHERE is_core_stat = 1 AND year >= 1955
                  ORDER BY stat_descendant_count DESC, student_count DESC
                  LIMIT 250
              )
              OR (school IN ({ph_sch}) AND student_count >= 5)
              OR (current_affiliation IN ({ph_sch}) AND student_count >= 5)
          )
    """, FLAGSHIP_SCHOOLS + FLAGSHIP_SCHOOLS).fetchall()

    print(f"  Verifying live URLs for {len(rows):,} priority Core Stat advisors...")
    photo_tasks = [(r["id"], r["name"], r["photo_url"]) for r in rows if (r["photo_url"] or "").strip()]
    hp_tasks = [(r["id"], r["name"], r["homepage"]) for r in rows if (r["homepage"] or "").strip()]

    dead_photos = []
    dead_hps = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
        f_photos = {ex.submit(check_photo_live, url): (pid, name, url) for pid, name, url in photo_tasks}
        f_hps = {ex.submit(check_homepage_live, url): (pid, name, url) for pid, name, url in hp_tasks}
        for f in concurrent.futures.as_completed(f_photos):
            pid, name, url = f_photos[f]
            if not f.result():
                dead_photos.append((pid, name, url))
        for f in concurrent.futures.as_completed(f_hps):
            pid, name, url = f_hps[f]
            if not f.result():
                dead_hps.append((pid, name, url))

    for pid, name, url in dead_photos:
        db.execute("UPDATE people SET photo_url = '' WHERE id = ?", (pid,))
    for pid, name, url in dead_hps:
        db.execute("UPDATE people SET homepage = '' WHERE id = ?", (pid,))
    db.commit()

    print(f"  Checked {len(photo_tasks)} portraits -> cleared {len(dead_photos)} dead/broken portraits.")
    for pid, name, url in dead_photos[:8]:
        print(f"    cleared dead photo: {name} ({pid}) -> {url[:75]}")
    print(f"  Checked {len(hp_tasks)} homepages -> cleared {len(dead_hps)} hard-404 homepages.")
    for pid, name, url in dead_hps[:8]:
        print(f"    cleared 404 homepage: {name} ({pid}) -> {url[:75]}")


# ---------------------------------------------------------------------------
# PASS 5: Fast DAG Finalization (Counts, Topological Descendants, FTS)
# ---------------------------------------------------------------------------

def pass5_finalize_database(db: sqlite3.Connection):
    print("\n=== PASS 5: Recomputing DAG Counts, Schools, Meta Stats & FTS ===")
    t0 = time.time()

    # Load graph into memory
    children = defaultdict(list)
    parents = defaultdict(list)
    in_deg = defaultdict(int)
    for adv_id, stu_id in db.execute("SELECT advisor_id, student_id FROM edges"):
        if adv_id == stu_id:
            continue
        children[adv_id].append(stu_id)
        parents[stu_id].append(adv_id)
        in_deg[stu_id] += 1

    old_counts = {}
    core_ids = set()
    all_ids = []
    for pid, ic, ac, sc, dc, sdc in db.execute(
        "SELECT id, is_core_stat, advisor_count, student_count, descendant_count, stat_descendant_count FROM people"
    ):
        all_ids.append(pid)
        old_counts[pid] = (ac, sc, dc, sdc)
        if ic:
            core_ids.add(pid)

    # Topological order (Kahn's algorithm)
    q = deque([pid for pid in all_ids if in_deg[pid] == 0])
    topo = []
    while q:
        u = q.popleft()
        topo.append(u)
        for v in children.get(u, ()):
            in_deg[v] -= 1
            if in_deg[v] == 0:
                q.append(v)

    # Reverse-topological exact descendant sets with reference-counted cleanup
    rem_parents = {pid: len(parents.get(pid, ())) for pid in all_ids}
    desc_sets = {}
    new_dc = {}
    new_sdc = {}
    for u in reversed(topo):
        ch = children.get(u)
        if not ch:
            new_dc[u] = 0
            new_sdc[u] = 0
            continue
        s = set()
        for c in ch:
            s.add(c)
            cs = desc_sets.get(c)
            if cs:
                s.update(cs)
            rem_parents[c] -= 1
            if rem_parents[c] <= 0 and c in desc_sets:
                del desc_sets[c]
        new_dc[u] = len(s)
        new_sdc[u] = len(s & core_ids)
        if rem_parents[u] > 0:
            desc_sets[u] = s
    desc_sets.clear()

    # Delta-only UPDATE on people
    deltas = []
    for pid in all_ids:
        nac = len(parents.get(pid, ()))
        nsc = len(children.get(pid, ()))
        ndc = new_dc.get(pid, 0)
        nsdc = new_sdc.get(pid, 0)
        if (nac, nsc, ndc, nsdc) != old_counts[pid]:
            deltas.append((nac, nsc, ndc, nsdc, pid))

    db.executemany(
        "UPDATE people SET advisor_count = ?, student_count = ?, descendant_count = ?, stat_descendant_count = ? WHERE id = ?",
        deltas,
    )
    print(f"  Updated {len(deltas):,} changed people count rows in {time.time() - t0:.1f}s.")

    # Refresh schools counts (single-pass GROUP BY instead of 3 correlated scans -> 0.3s!)
    t1 = time.time()
    sch_counts = {}
    for sch, tot, cs, bs in db.execute("""
        SELECT school, COUNT(*), SUM(CASE WHEN is_core_stat=1 THEN 1 ELSE 0 END),
               SUM(CASE WHEN is_broad_stat=1 THEN 1 ELSE 0 END)
        FROM people WHERE school IS NOT NULL AND school != '' GROUP BY school
    """):
        sch_counts[sch] = (cs or 0, bs or 0, tot or 0)
    db.execute("UPDATE schools SET core_stat_count = 0, broad_stat_count = 0, total_phd_count = 0")
    db.executemany(
        "UPDATE schools SET core_stat_count = ?, broad_stat_count = ?, total_phd_count = ? WHERE school = ?",
        [(cs, bs, tot, sch) for sch, (cs, bs, tot) in sch_counts.items()],
    )
    # Also ensure any newly added university from our global ETD harvest is inserted into `schools`!
    existing_sch = set(r[0] for r in db.execute("SELECT school FROM schools"))
    new_sch_rows = []
    for sch, (cs, bs, tot) in sch_counts.items():
        if sch not in existing_sch and tot >= 1:
            new_sch_rows.append((sch, sch.lower(), "", "", cs, bs, tot, None, None))
    if new_sch_rows:
        db.executemany(
            "INSERT OR IGNORE INTO schools (school, norm_school, aliases, country, core_stat_count, broad_stat_count, total_phd_count, min_year, max_year) VALUES (?,?,?,?,?,?,?,?,?)",
            new_sch_rows,
        )

    # Refresh meta_stats['summary']
    row_meta = db.execute("SELECT value_json FROM meta_stats WHERE key = 'summary'").fetchone()
    summary = json.loads(row_meta["value_json"]) if row_meta else {}
    summary["total_people"] = db.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    summary["total_edges"] = db.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    summary["core_stat_total"] = db.execute("SELECT COUNT(*) FROM people WHERE is_core_stat = 1").fetchone()[0]
    summary["broad_stat_total"] = db.execute("SELECT COUNT(*) FROM people WHERE is_broad_stat = 1").fetchone()[0]
    summary["core_stat_schools"] = db.execute("SELECT COUNT(*) FROM schools WHERE core_stat_count > 0").fetchone()[0]
    summary["total_schools"] = db.execute("SELECT COUNT(*) FROM schools WHERE total_phd_count > 0").fetchone()[0]
    summary["by_field"] = {
        r[0]: r[1] for r in db.execute("SELECT primary_field, COUNT(*) FROM people GROUP BY primary_field")
    }
    db.execute(
        "INSERT OR REPLACE INTO meta_stats (key, value_json) VALUES ('summary', ?)",
        (json.dumps(summary, ensure_ascii=False),),
    )

    # Rebuild people_fts
    db.execute("INSERT INTO people_fts(people_fts) VALUES('rebuild')")
    db.commit()
    print(f"  Rebuilt schools, meta_stats & people_fts in {time.time() - t1:.1f}s.")
    print(f"  FINAL TOTALS: {summary['total_people']:,} people ({summary['core_stat_total']:,} Core Stat), {summary['total_edges']:,} edges.")


def main():
    t_start = time.time()
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("PRAGMA synchronous = NORMAL")

    pass1_uc_escholarship_masters_audit(db)
    pass2_chronological_edge_audit(db)
    pass3_dedup_and_metadata_hygiene(db)
    pass4_live_web_verification(db)
    pass5_finalize_database(db)

    db.close()
    print(f"\nALL 5 AUDIT & REPAIR PASSES COMPLETED in {time.time() - t_start:.1f}s!")


if __name__ == "__main__":
    main()
