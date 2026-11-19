#!/usr/bin/env python3
"""
Backend Web Server & REST API for the Statistics Academic Family Tree Explorer.
"""

import json
import math
import os
import random
import re
import sqlite3
import threading
import unicodedata
import zlib
from collections import Counter, defaultdict, deque

from flask import Flask, g, jsonify, request, send_from_directory

from famous_ancestors import ANCESTRY_INDEX, FAMOUS_BY_ID

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "stat_genealogy.db")
DB_PARTS_DIR = os.path.join(BASE_DIR, "data", "db_parts")
MASTER_SCHOOLS_PATH = os.path.join(BASE_DIR, "data", "stat_departments_master.json")
STATIC_DIR = os.path.join(BASE_DIR, "static")

_DB_UNPACK_LOCK = threading.Lock()


def ensure_database_ready():
    """Automatically reconstructs `data/stat_genealogy.db` from `data/db_parts/*.part*` on fresh git clones."""
    if os.path.exists(DB_PATH) and os.path.getsize(DB_PATH) > 1_000_000:
        return
    with _DB_UNPACK_LOCK:
        if os.path.exists(DB_PATH) and os.path.getsize(DB_PATH) > 1_000_000:
            return
        if not os.path.isdir(DB_PARTS_DIR):
            raise FileNotFoundError(f"Missing {DB_PATH} and {DB_PARTS_DIR}")
        parts = sorted(
            os.path.join(DB_PARTS_DIR, fn)
            for fn in os.listdir(DB_PARTS_DIR)
            if fn.startswith("stat_genealogy.db.gz.part")
        )
        if not parts:
            raise FileNotFoundError(f"No database archive parts found in {DB_PARTS_DIR}")
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        tmp_path = DB_PATH + ".tmp"
        decomp = zlib.decompressobj(16 + zlib.MAX_WBITS)
        with open(tmp_path, "wb") as f_out:
            for part_path in parts:
                with open(part_path, "rb") as f_in:
                    while True:
                        chunk = f_in.read(4 * 1024 * 1024)
                        if not chunk:
                            break
                        f_out.write(decomp.decompress(chunk))
            f_out.write(decomp.flush())
        os.replace(tmp_path, DB_PATH)

app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/static")
app.config["JSON_AS_ASCII"] = False
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0


@app.after_request
def add_no_cache_headers(response):
    if request.path == "/" or request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


# ---------------------------------------------------------------------------
# Nickname & Text Normalization Helpers for Query Expansion
# ---------------------------------------------------------------------------

NICKNAME_GROUPS = [
    {"robert", "rob", "bob", "bobby", "robbie"},
    {"richard", "rick", "dick", "rich", "ricky"},
    {"william", "bill", "will", "billy", "liam"},
    {"james", "jim", "jimmy", "jamie"},
    {"john", "jack", "johnny", "jon"},
    {"michael", "mike", "mikhail"},
    {"david", "dave"},
    {"daniel", "dan", "danny"},
    {"thomas", "tom", "tommy"},
    {"charles", "chuck", "charlie"},
    {"joseph", "joe", "joey"},
    {"edward", "ed", "ted", "ned"},
    {"stephen", "steven", "steve"},
    {"christopher", "chris"},
    {"matthew", "matt"},
    {"andrew", "andy", "andrei", "andrey"},
    {"alexander", "alex", "sasha", "aleksei", "alexey"},
    {"nicholas", "nick", "nikolai", "nikolay", "nicolas"},
    {"lawrence", "larry", "laurence"},
    {"donald", "don"},
    {"ronald", "ron"},
    {"kenneth", "ken"},
    {"jeffrey", "jeff", "geoffrey", "geoff"},
    {"gregory", "greg"},
    {"timothy", "tim"},
    {"benjamin", "ben"},
    {"samuel", "sam"},
    {"anthony", "tony"},
    {"peter", "pete"},
    {"philip", "phillip", "phil"},
    {"douglas", "doug"},
    {"elizabeth", "liz", "beth", "betty"},
    {"katherine", "catherine", "kathryn", "kate", "kathy", "katie"},
    {"susan", "sue"},
    {"deborah", "debra", "deb"},
    {"jennifer", "jen"},
    {"kiyosi", "kiyoshi"},
    {"vasily", "vasilii", "wassily"},
]

NICKNAME_MAP = {}
for grp in NICKNAME_GROUPS:
    for nm in grp:
        NICKNAME_MAP[nm] = grp | NICKNAME_MAP.get(nm, set())


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


def get_db():
    if "db" not in g:
        ensure_database_ready()
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def row_to_person_brief(r):
    f_info = FAMOUS_BY_ID.get(r["id"])
    return {
        "id": r["id"],
        "name": r["name"],
        "school": r["school"] or "",
        "country": r["country"] or "",
        "year": r["year"],
        "thesis": r["thesis"] or "",
        "primary_field": r["primary_field"],
        "is_core_stat": bool(r["is_core_stat"]),
        "is_broad_stat": bool(r["is_broad_stat"]),
        "classification_reason": r["classification_reason"] or "",
        "current_affiliation": r["current_affiliation"] or "",
        "mgp_id": r["mgp_id"],
        "aft_pid": r["aft_pid"],
        "wikidata_qid": r["wikidata_qid"] or "",
        "sources": (r["sources"] or "").split(","),
        "advisor_count": r["advisor_count"],
        "student_count": r["student_count"],
        "descendant_count": r["descendant_count"],
        "stat_descendant_count": r["stat_descendant_count"],
        "famous_self": (
            {
                "display_name": f_info["display_name"],
                "short_name": f_info["short_name"],
                "category": f_info["category"],
                "epithet": f_info["epithet"],
            }
            if f_info
            else None
        ),
    }


def build_fts_query(raw_q: str) -> str:
    folded = fold_ascii(raw_q).lower()
    tokens = re.findall(r"[a-z0-9]+", folded)
    if not tokens:
        return ""
    parts = []
    for idx, tok in enumerate(tokens):
        is_last = idx == len(tokens) - 1
        if idx == 0 and tok in NICKNAME_MAP and len(tokens) >= 2:
            alts = sorted(NICKNAME_MAP[tok])
            or_expr = " OR ".join(f'"{a}"' for a in alts)
            parts.append(f"({or_expr})")
        elif is_last and len(tok) >= 2:
            parts.append(f'"{tok}"*')
        else:
            parts.append(f'"{tok}"')
    return " AND ".join(parts)


# ---------------------------------------------------------------------------
# Static Frontend Route
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

@app.route("/api/stats")
def api_stats():
    db = get_db()
    row = db.execute("SELECT value_json FROM meta_stats WHERE key = 'summary'").fetchone()
    summary = json.loads(row["value_json"]) if row else {}

    year_hist = defaultdict(dict)
    for yr, pf, cnt in db.execute(
        "SELECT year, primary_field, COUNT(*) FROM people WHERE is_core_stat = 1 AND year BETWEEN 1920 AND 2026 GROUP BY year, primary_field ORDER BY year ASC"
    ):
        year_hist[str(yr)][pf] = cnt
    summary["year_histogram"] = dict(year_hist)

    # 5 representative scholars spanning distinct historical eras (18th c. -> Modern)
    featured_ids = [
        18231,  # Carl Friedrich Gauss (1799 — Classical Foundations)
        12694,  # Jerzy Neyman (1924 — Early 20th-Century Mathematical Statistics)
        4209,   # David Blackwell (1941 — Mid-Century Probability & Bayesian Decision Theory)
        14941,  # Bradley Efron (1964 — Late 20th-Century Computational Statistics)
        47762,  # Xihong Lin (1994 — Modern Biostatistics & Statistical Genomics)
    ]
    placeholders = ",".join("?" for _ in featured_ids)
    f_rows = db.execute(
        f"SELECT * FROM people WHERE id IN ({placeholders})", featured_ids
    ).fetchall()
    f_map = {r["id"]: row_to_person_brief(r) for r in f_rows}
    featured = [f_map[i] for i in featured_ids if i in f_map]

    top_schools = [
        dict(r)
        for r in db.execute(
            "SELECT school, aliases, country, core_stat_count, broad_stat_count, total_phd_count "
            "FROM schools WHERE core_stat_count > 0 ORDER BY core_stat_count DESC LIMIT 25"
        ).fetchall()
    ]

    return jsonify({
        "summary": summary,
        "featured": featured,
        "top_schools": top_schools,
    })


_LUCKY_IDS_CACHE = None


@app.route("/api/random_person")
def api_random_person():
    """Returns a random connected Core Stat/Biostat/Prob PhD scholar for 'I'm Feeling Lucky'."""
    global _LUCKY_IDS_CACHE
    db = get_db()
    if not _LUCKY_IDS_CACHE:
        _LUCKY_IDS_CACHE = [
            r[0]
            for r in db.execute(
                "SELECT id FROM people WHERE is_core_stat = 1 AND (advisor_count > 0 OR student_count > 0)"
            ).fetchall()
        ]
    if not _LUCKY_IDS_CACHE:
        return jsonify({"error": "No scholars available"}), 404
    chosen_id = random.choice(_LUCKY_IDS_CACHE)
    row = db.execute("SELECT * FROM people WHERE id = ?", (chosen_id,)).fetchone()
    return jsonify(row_to_person_brief(row))


@app.route("/api/schools")
def api_schools():
    db = get_db()
    q = (request.args.get("q") or "").strip()
    scope = request.args.get("scope") or "core"
    limit = min(int(request.args.get("limit") or 40), 200)

    count_col = (
        "core_stat_count" if scope in ("core", "Statistics", "Biostatistics", "Probability")
        else ("broad_stat_count" if scope == "broad" else "total_phd_count")
    )

    if not q:
        rows = db.execute(
            f"SELECT school, aliases, country, core_stat_count, broad_stat_count, total_phd_count, min_year, max_year "
            f"FROM schools WHERE {count_col} > 0 ORDER BY {count_col} DESC LIMIT ?",
            (limit,),
        ).fetchall()
    else:
        q_fold = fold_ascii(q).lower()
        like_pat = f"%{q_fold}%"
        prefix_pat = f"{q_fold}%"
        alias_tok_pat = f"%, {q_fold}, %"
        rows = db.execute(
            f"""
            SELECT school, aliases, country, core_stat_count, broad_stat_count, total_phd_count, min_year, max_year
            FROM schools
            WHERE (norm_school LIKE ? OR LOWER(aliases) LIKE ?)
              AND ({count_col} > 0 OR total_phd_count >= 3)
            ORDER BY
                CASE
                    WHEN (', ' || LOWER(aliases) || ', ') LIKE ? OR (norm_school = ? AND {count_col} > 0) THEN 0
                    WHEN LOWER(aliases) LIKE ? OR norm_school LIKE ? THEN 1
                    ELSE 2
                END,
                {count_col} DESC,
                total_phd_count DESC
            LIMIT ?
            """,
            (like_pat, like_pat, alias_tok_pat, q_fold, prefix_pat, prefix_pat, limit),
        ).fetchall()

    return jsonify({"schools": [dict(r) for r in rows]})


@app.route("/api/master_schools")
def api_master_schools():
    """Returns the 207+ global universities with Statistics/Biostatistics/Probability PhD departments + live DB counts."""
    db = get_db()
    master_list = []
    if os.path.exists(MASTER_SCHOOLS_PATH):
        with open(MASTER_SCHOOLS_PATH, "r", encoding="utf-8") as f:
            master_list = json.load(f)

    sch_stats = {
        r["school"]: dict(r)
        for r in db.execute(
            "SELECT school, aliases, country, core_stat_count, broad_stat_count, total_phd_count, min_year, max_year FROM schools"
        ).fetchall()
    }

    has_adv_table = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='advisor_schools'"
    ).fetchone()
    fac_counts = {}
    if has_adv_table:
        fac_counts = {
            r[0]: r[1]
            for r in db.execute(
                """
                SELECT a.school, COUNT(DISTINCT a.person_id)
                FROM advisor_schools a
                JOIN people p ON p.id = a.person_id
                WHERE p.is_core_stat = 1
                GROUP BY a.school
                """
            ).fetchall()
        }

    enriched = []
    for item in master_list:
        sname = item["school"]
        st = sch_stats.get(sname, {})
        enriched.append({
            "school": sname,
            "country": item.get("country") or st.get("country") or "",
            "departments": item.get("departments") or [],
            "aliases": st.get("aliases") or "",
            "core_phd_graduates": st.get("core_stat_count", 0),
            "broad_phd_graduates": st.get("broad_stat_count", 0),
            "total_phd_graduates": st.get("total_phd_count", 0),
            "core_faculty_advisors": fac_counts.get(sname, 0),
        })

    enriched.sort(key=lambda x: -(x["core_phd_graduates"] + x["core_faculty_advisors"] * 2))
    return jsonify({"schools": enriched, "total": len(enriched)})


@app.route("/api/search")
def api_search():
    db = get_db()
    q = (request.args.get("q") or "").strip()
    school = (request.args.get("school") or "").strip()
    school_role = (request.args.get("school_role") or "both").strip()  # 'both' | 'graduates' | 'faculty'
    country = (request.args.get("country") or "").strip()
    scope = (request.args.get("scope") or "core").strip()
    sort = (request.args.get("sort") or ("relevance" if q else "descendants")).strip()
    page = max(1, int(request.args.get("page") or 1))
    per_page = min(100, max(10, int(request.args.get("per_page") or 36)))

    year_min = request.args.get("year_min")
    year_max = request.args.get("year_max")
    year_min = int(year_min) if (year_min and str(year_min).lstrip("-").isdigit()) else None
    year_max = int(year_max) if (year_max and str(year_max).lstrip("-").isdigit()) else None

    where_clauses = []
    params = []

    def add_scope_clause(sc, target_clauses, target_params):
        if sc == "core":
            target_clauses.append("p.is_core_stat = 1")
        elif sc in ("Statistics", "Biostatistics", "Probability", "Related Quantitative"):
            target_clauses.append("p.primary_field = ?")
            target_params.append(sc)
        elif sc == "broad":
            target_clauses.append("p.is_broad_stat = 1")

    has_adv_table = bool(
        db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='advisor_schools'").fetchone()
    )

    if school:
        exact_sch = db.execute("SELECT school FROM schools WHERE school = ?", (school,)).fetchone()
        target_schools = [school] if exact_sch else [
            r["school"]
            for r in db.execute(
                "SELECT school FROM schools WHERE norm_school LIKE ? OR LOWER(aliases) LIKE ? LIMIT 25",
                (f"%{fold_ascii(school).lower()}%", f"%{fold_ascii(school).lower()}%"),
            ).fetchall()
        ]
        if target_schools:
            ph = ",".join("?" for _ in target_schools)
            if school_role == "faculty" and has_adv_table:
                where_clauses.append(
                    f"p.id IN (SELECT person_id FROM advisor_schools WHERE school IN ({ph}))"
                )
                params.extend(target_schools)
            elif school_role == "both" and has_adv_table:
                where_clauses.append(
                    f"(p.school IN ({ph}) OR p.id IN (SELECT person_id FROM advisor_schools WHERE school IN ({ph})))"
                )
                params.extend(target_schools + target_schools)
            else:
                where_clauses.append(f"p.school IN ({ph})")
                params.extend(target_schools)
        else:
            where_clauses.append("(LOWER(p.school) LIKE ? OR LOWER(p.current_affiliation) LIKE ?)")
            params.extend([f"%{school.lower()}%", f"%{school.lower()}%"])

    if country:
        where_clauses.append("LOWER(p.country) = ?")
        params.append(country.lower())

    if year_min is not None:
        where_clauses.append("p.year >= ?")
        params.append(year_min)

    if year_max is not None:
        where_clauses.append("p.year <= ?")
        params.append(year_max)

    # Case 1: Full-Text / Name Search (`q` provided)
    if q:
        fts_q = build_fts_query(q)
        q_norm = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s\-]", " ", fold_ascii(q).lower())).strip()

        if not fts_q:
            return jsonify({"results": [], "total": 0, "page": page, "per_page": per_page})

        scoped_clauses = list(where_clauses)
        scoped_params = list(params)
        add_scope_clause(scope, scoped_clauses, scoped_params)

        where_sql = ("WHERE " + " AND ".join(scoped_clauses)) if scoped_clauses else ""
        sql = f"""
            WITH fts_hits AS MATERIALIZED (
                SELECT rowid, bm25(people_fts, 10.0, 6.0, 2.0, 1.5, 1.5) AS fts_rank
                FROM people_fts
                WHERE people_fts MATCH ?
                ORDER BY rank
                LIMIT 1500
            )
            SELECT p.*, fts_hits.fts_rank
            FROM fts_hits
            JOIN people p ON p.id = fts_hits.rowid
            {where_sql}
            LIMIT 800
        """
        rows = db.execute(sql, [fts_q] + scoped_params).fetchall()

        expanded_from_all = False
        broader_total_hint = 0
        # High-Recall Guarantee: If user typed a person query under restricted scope (e.g. 'core')
        # and there are very few matches (or an exact name exists in broader fields like Gauss / Judea Pearl),
        # seamlessly check broader graph so nobody is ever "missing"!
        if scope != "all":
            all_where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
            if len(rows) < 5:
                rows_all = db.execute(
                    f"""
                    WITH fts_hits AS MATERIALIZED (
                        SELECT rowid, bm25(people_fts, 10.0, 6.0, 2.0, 1.5, 1.5) AS fts_rank
                        FROM people_fts
                        WHERE people_fts MATCH ?
                        ORDER BY rank
                        LIMIT 600
                    )
                    SELECT p.*, fts_hits.fts_rank
                    FROM fts_hits
                    JOIN people p ON p.id = fts_hits.rowid
                    {all_where_sql}
                    LIMIT 400
                    """,
                    [fts_q] + params,
                ).fetchall()
                if len(rows_all) > len(rows):
                    seen_ids = {r["id"] for r in rows}
                    for r in rows_all:
                        if r["id"] not in seen_ids:
                            rows.append(r)
                    expanded_from_all = True
            else:
                extra_where = (" AND " + " AND ".join(where_clauses)) if where_clauses else ""
                exact_extra = db.execute(
                    f"SELECT *, -20.0 AS fts_rank FROM people p WHERE p.norm_name = ? {extra_where} LIMIT 10",
                    [q_norm] + params,
                ).fetchall()
                seen_ids = {r["id"] for r in rows}
                for r in exact_extra:
                    if r["id"] not in seen_ids:
                        rows.append(r)
                        expanded_from_all = True

        q_tokens = q_norm.split()

        def rank_key(r):
            nm = r["norm_name"] or ""
            aliases = (r["search_aliases"] or "").lower()
            exact_full = 1 if nm == q_norm else 0
            alias_exact = 1 if q_norm in [x.strip() for x in aliases.split("|")] else 0
            all_in_name = 1 if all(t in nm for t in q_tokens) else 0
            prefix_name = 1 if nm.startswith(q_norm) else 0
            last_match = 1 if (q_tokens and nm.endswith(q_tokens[-1])) else 0
            core_boost = 1.5 if r["is_core_stat"] else (0.6 if r["is_broad_stat"] else 0.0)
            impact = math.log1p(r["stat_descendant_count"] * 2 + r["descendant_count"] + r["student_count"] * 3)
            # Higher score is better
            score = (
                exact_full * 120.0
                + alias_exact * 85.0
                + all_in_name * 45.0
                + prefix_name * 20.0
                + last_match * 15.0
                + core_boost * 14.0
                + impact * 2.8
                - float(r["fts_rank"])
            )
            return -score

        if sort == "descendants":
            rows.sort(key=lambda r: (-r["is_core_stat"], -r["descendant_count"], -r["student_count"], r["name"]))
        elif sort == "students":
            rows.sort(key=lambda r: (-r["is_core_stat"], -r["student_count"], -r["descendant_count"], r["name"]))
        elif sort == "year_desc":
            rows.sort(key=lambda r: (r["year"] is None, -(r["year"] or 0), -r["student_count"]))
        elif sort == "year_asc":
            rows.sort(key=lambda r: (r["year"] is None, (r["year"] or 9999), -r["student_count"]))
        elif sort == "name_asc":
            rows.sort(key=lambda r: (r["name"].lower(), -(r["year"] or 0)))
        else:
            rows.sort(key=rank_key)

        total = len(rows)
        start = (page - 1) * per_page
        page_rows = rows[start : start + per_page]
        out_cards = []
        for r in page_rows:
            card = row_to_person_brief(r)
            card["famous_ancestors_preview"] = ANCESTRY_INDEX.get_preview(db, r["id"], limit=3)
            out_cards.append(card)
        return jsonify({
            "results": out_cards,
            "total": total,
            "page": page,
            "per_page": per_page,
            "expanded_from_all": expanded_from_all,
            "broader_total_hint": broader_total_hint,
        })

    # Case 2: Filter / Browse Mode (no text query `q`, filtering by school / year / field)
    scoped_clauses = list(where_clauses)
    scoped_params = list(params)
    add_scope_clause(scope, scoped_clauses, scoped_params)
    where_sql = ("WHERE " + " AND ".join(scoped_clauses)) if scoped_clauses else ""

    order_map = {
        "relevance": "p.stat_descendant_count DESC, p.descendant_count DESC, p.student_count DESC, p.name ASC",
        "descendants": "p.descendant_count DESC, p.stat_descendant_count DESC, p.student_count DESC, p.name ASC",
        "students": "p.student_count DESC, p.descendant_count DESC, p.name ASC",
        "year_desc": "CASE WHEN p.year IS NULL THEN 1 ELSE 0 END, p.year DESC, p.student_count DESC, p.name ASC",
        "year_asc": "CASE WHEN p.year IS NULL THEN 1 ELSE 0 END, p.year ASC, p.student_count DESC, p.name ASC",
        "name_asc": "p.name ASC",
    }
    order_sql = order_map.get(sort, order_map["descendants"])

    total = db.execute(f"SELECT COUNT(*) AS c FROM people p {where_sql}", scoped_params).fetchone()["c"]
    offset = (page - 1) * per_page
    rows = db.execute(
        f"SELECT p.* FROM people p {where_sql} ORDER BY {order_sql} LIMIT ? OFFSET ?",
        scoped_params + [per_page, offset],
    ).fetchall()

    out_cards = []
    for r in rows:
        card = row_to_person_brief(r)
        card["famous_ancestors_preview"] = ANCESTRY_INDEX.get_preview(db, r["id"], limit=3)
        out_cards.append(card)

    return jsonify({
        "results": out_cards,
        "total": total,
        "page": page,
        "per_page": per_page,
        "expanded_from_all": False,
    })


@app.route("/api/person/<int:person_id>")
def api_person(person_id: int):
    db = get_db()
    p = db.execute("SELECT * FROM people WHERE id = ?", (person_id,)).fetchone()
    if not p:
        return jsonify({"error": "Researcher not found"}), 404

    adv_rows = db.execute(
        """
        SELECT p.*, e.source AS edge_source
        FROM edges e
        JOIN people p ON p.id = e.advisor_id
        WHERE e.student_id = ?
        ORDER BY CASE WHEN p.year IS NULL THEN 1 ELSE 0 END, p.year ASC, p.name ASC
        """,
        (person_id,),
    ).fetchall()

    stu_rows = db.execute(
        """
        SELECT p.*, e.source AS edge_source
        FROM edges e
        JOIN people p ON p.id = e.student_id
        WHERE e.advisor_id = ?
        ORDER BY CASE WHEN p.year IS NULL THEN 1 ELSE 0 END, p.year ASC, p.name ASC
        """,
        (person_id,),
    ).fetchall()

    # Academic siblings (co-students sharing at least one advisor)
    adv_ids = [r["id"] for r in adv_rows]
    siblings = []
    sibling_total = 0
    if adv_ids:
        ph = ",".join("?" for _ in adv_ids)
        focal_yr = p["year"] or 2000
        sib_count_row = db.execute(
            f"SELECT COUNT(DISTINCT student_id) AS c FROM edges WHERE advisor_id IN ({ph}) AND student_id != ?",
            adv_ids + [person_id],
        ).fetchone()
        sibling_total = sib_count_row["c"] if sib_count_row else 0
        sib_rows = db.execute(
            f"""
            SELECT DISTINCT p.*
            FROM edges e
            JOIN people p ON p.id = e.student_id
            WHERE e.advisor_id IN ({ph}) AND e.student_id != ?
            ORDER BY ABS(COALESCE(p.year, ?) - ?) ASC, p.descendant_count DESC
            LIMIT 40
            """,
            adv_ids + [person_id, focal_yr, focal_yr],
        ).fetchall()
        siblings = [row_to_person_brief(r) for r in sib_rows]

    profile = row_to_person_brief(p)
    famous_anc = ANCESTRY_INDEX.get_full_for_person(db, person_id, include_chains=True)
    profile.update({
        "raw_school": p["raw_school"] or "",
        "mgp_subject": p["mgp_subject"] or "",
        "field_confidence": p["field_confidence"],
        "research_areas": p["research_areas"] or "",
        "orcid": p["orcid"] or "",
        "homepage": p["homepage"] or "",
        "proquest_id": p["proquest_id"] or "",
        "advisors": [{**row_to_person_brief(r), "edge_source": r["edge_source"]} for r in adv_rows],
        "students": [{**row_to_person_brief(r), "edge_source": r["edge_source"]} for r in stu_rows],
        "siblings": siblings,
        "sibling_total": sibling_total,
        "famous_ancestors": famous_anc,
    })
    return jsonify(profile)


@app.route("/api/tree/<int:person_id>")
def api_tree(person_id: int):
    db = get_db()
    focal = db.execute("SELECT * FROM people WHERE id = ?", (person_id,)).fetchone()
    if not focal:
        return jsonify({"error": "Researcher not found"}), 404

    up_depth = min(45, max(0, int(request.args.get("up_depth") or 6)))
    down_depth = min(5, max(0, int(request.args.get("down_depth") or 2)))
    max_children = min(250, max(8, int(request.args.get("max_children") or 40)))

    node_rows = {person_id: focal}
    gen_offset = {person_id: 0}
    roles = {person_id: "focal"}
    hidden_children = Counter()
    links_map = {}  # (adv, stu) -> edge_source

    # 1. Walk UPWARD (Advisors / Ancestors) up to `up_depth`
    frontier = [person_id]
    for step in range(1, up_depth + 1):
        if not frontier:
            break
        ph = ",".join("?" for _ in frontier)
        e_rows = db.execute(
            f"""
            SELECT e.advisor_id, e.student_id, e.source, p.*
            FROM edges e
            JOIN people p ON p.id = e.advisor_id
            WHERE e.student_id IN ({ph})
            """,
            frontier,
        ).fetchall()
        nxt = []
        for r in e_rows:
            aid = r["advisor_id"]
            sid = r["student_id"]
            links_map[(aid, sid)] = r["source"]
            if aid not in node_rows:
                node_rows[aid] = r
                gen_offset[aid] = -step
                roles[aid] = "ancestor"
                nxt.append(aid)
            else:
                # Push ancestor further up if reached via a longer ancestor path (keeps DAG layered cleanly!)
                if roles[aid] == "ancestor" and -step < gen_offset[aid]:
                    gen_offset[aid] = -step
        frontier = nxt

    # 2. Walk DOWNWARD (Students / Descendants) up to `down_depth`
    frontier = [person_id]
    for step in range(1, down_depth + 1):
        if not frontier:
            break
        ph = ",".join("?" for _ in frontier)
        e_rows = db.execute(
            f"""
            SELECT e.advisor_id, e.student_id, e.source, p.*
            FROM edges e
            JOIN people p ON p.id = e.student_id
            WHERE e.advisor_id IN ({ph})
            ORDER BY p.is_core_stat DESC, p.descendant_count DESC, p.student_count DESC, COALESCE(p.year, 9999) ASC
            """,
            frontier,
        ).fetchall()

        grouped = defaultdict(list)
        for r in e_rows:
            grouped[r["advisor_id"]].append(r)

        nxt = []
        # Focal person allows higher direct student cap than distant descendants
        for aid, children in grouped.items():
            cap = max(max_children, 90) if aid == person_id else max_children
            shown = children[:cap]
            if len(children) > cap:
                hidden_children[aid] = len(children) - cap
            for r in shown:
                sid = r["student_id"]
                links_map[(aid, sid)] = r["source"]
                if sid not in node_rows:
                    node_rows[sid] = r
                    gen_offset[sid] = step
                    roles[sid] = "descendant"
                    nxt.append(sid)
        frontier = nxt

    nodes = []
    school_counts = Counter()
    field_counts = Counter()
    earliest_anc = None

    for uid, r in node_rows.items():
        nb = row_to_person_brief(r)
        nb["gen_offset"] = gen_offset[uid]
        nb["role"] = roles[uid]
        nb["hidden_student_count"] = hidden_children.get(uid, 0)
        nodes.append(nb)
        if nb["school"]:
            school_counts[nb["school"]] += 1
        field_counts[nb["primary_field"]] += 1
        if roles[uid] == "ancestor":
            if earliest_anc is None or (nb["year"] and (earliest_anc["year"] is None or nb["year"] < earliest_anc["year"])) or (earliest_anc["year"] is None and gen_offset[uid] < earliest_anc["gen_offset"]):
                earliest_anc = {"id": uid, "name": nb["name"], "year": nb["year"], "school": nb["school"], "gen_offset": gen_offset[uid]}

    links = [
        {"source": adv, "target": stu, "edge_source": src}
        for (adv, stu), src in links_map.items()
        if adv in node_rows and stu in node_rows
    ]

    focal_famous = ANCESTRY_INDEX.get_full_for_person(db, person_id, include_chains=False)

    return jsonify({
        "focal_id": person_id,
        "focal_name": focal["name"],
        "focal_famous_ancestors": focal_famous,
        "nodes": nodes,
        "links": links,
        "stats": {
            "total_nodes_shown": len(nodes),
            "ancestor_count": sum(1 for v in roles.values() if v == "ancestor"),
            "descendant_shown_count": sum(1 for v in roles.values() if v == "descendant"),
            "total_descendant_count": focal["descendant_count"],
            "stat_descendant_count": focal["stat_descendant_count"],
            "max_up_gen": abs(min(gen_offset.values())),
            "max_down_gen": max(gen_offset.values()),
            "earliest_ancestor": earliest_anc,
            "top_schools": school_counts.most_common(6),
            "field_breakdown": dict(field_counts),
        },
    })


@app.route("/api/lineage_path")
def api_lineage_path():
    """Returns a D3-ready subgraph of all directed lineage paths from `ancestor_id` down to `student_id`."""
    db = get_db()
    try:
        student_id = int(request.args.get("student_id") or 0)
        ancestor_id = int(request.args.get("ancestor_id") or 0)
    except ValueError:
        return jsonify({"error": "Invalid IDs"}), 400

    if not student_id or not ancestor_id or student_id == ancestor_id:
        return jsonify({"error": "Distinct student_id and ancestor_id required"}), 400

    res = ANCESTRY_INDEX.build_lineage_subgraph(db, student_id, ancestor_id)
    if not res:
        return jsonify({"error": "No directed academic lineage found between these scholars."}), 404

    path_ids, gen_map, hops = res
    ph = ",".join("?" for _ in path_ids)
    p_rows = db.execute(f"SELECT * FROM people WHERE id IN ({ph})", list(path_ids)).fetchall()
    max_g = gen_map.get(student_id, hops)

    nodes = []
    for r in p_rows:
        uid = r["id"]
        nb = row_to_person_brief(r)
        # Position focal student at gen 0 and ancestor at negative offset so layout matches standard tree!
        nb["gen_offset"] = gen_map.get(uid, 0) - max_g
        nb["role"] = "focal" if uid == student_id else ("famous_root" if uid == ancestor_id else "ancestor")
        nb["hidden_student_count"] = 0
        nodes.append(nb)

    e_rows = db.execute(
        f"SELECT advisor_id, student_id, source FROM edges WHERE advisor_id IN ({ph}) AND student_id IN ({ph})",
        list(path_ids) + list(path_ids),
    ).fetchall()
    links = [{"source": er["advisor_id"], "target": er["student_id"], "edge_source": er["source"]} for er in e_rows]

    anc_info = FAMOUS_BY_ID.get(ancestor_id)
    anc_row = next((n for n in nodes if n["id"] == ancestor_id), None)
    stu_row = next((n for n in nodes if n["id"] == student_id), None)

    return jsonify({
        "focal_id": student_id,
        "focal_name": stu_row["name"] if stu_row else "",
        "ancestor_id": ancestor_id,
        "ancestor_name": (anc_info["display_name"] if anc_info else (anc_row["name"] if anc_row else "")),
        "ancestor_epithet": anc_info["epithet"] if anc_info else "",
        "generations": hops,
        "nodes": nodes,
        "links": links,
        "stats": {
            "total_nodes_shown": len(nodes),
            "ancestor_count": len(nodes) - 1,
            "descendant_shown_count": 0,
            "total_descendant_count": stu_row["descendant_count"] if stu_row else 0,
            "stat_descendant_count": stu_row["stat_descendant_count"] if stu_row else 0,
            "max_up_gen": hops,
            "max_down_gen": 0,
            "earliest_ancestor": {
                "id": ancestor_id,
                "name": (anc_info["display_name"] if anc_info else (anc_row["name"] if anc_row else "")),
                "year": anc_row["year"] if anc_row else None,
            },
        },
    })


@app.route("/api/connection")
def api_connection():
    """Finds direct lineage or Most Recent Common Academic Ancestor (MRCA) connecting two researchers."""
    db = get_db()
    try:
        id1 = int(request.args.get("id1") or 0)
        id2 = int(request.args.get("id2") or 0)
    except ValueError:
        return jsonify({"error": "Invalid researcher IDs"}), 400

    if not id1 or not id2 or id1 == id2:
        return jsonify({"error": "Please select two distinct researchers."}), 400

    def walk_ancestors(start_id, max_hops=45):
        dist = {start_id: 0}
        child_on_path = {}  # anc_id -> next_id toward start_id
        frontier = [start_id]
        for step in range(1, max_hops + 1):
            if not frontier:
                break
            ph = ",".join("?" for _ in frontier)
            rows = db.execute(
                f"SELECT advisor_id, student_id, source FROM edges WHERE student_id IN ({ph})",
                frontier,
            ).fetchall()
            nxt = []
            for r in rows:
                a, s = r["advisor_id"], r["student_id"]
                if a not in dist:
                    dist[a] = step
                    child_on_path[a] = (s, r["source"])
                    nxt.append(a)
            frontier = nxt
        return dist, child_on_path

    dist1, path1 = walk_ancestors(id1)
    dist2, path2 = walk_ancestors(id2)

    common = set(dist1.keys()) & set(dist2.keys())
    if not common:
        return jsonify({"connected": False, "message": "No common academic ancestor found within 25 generations."})

    best_mrca = min(common, key=lambda a: (dist1[a] + dist2[a], max(dist1[a], dist2[a])))

    # Reconstruct nodes & edges from best_mrca -> id1 and best_mrca -> id2
    path_nodes_ids = {best_mrca, id1, id2}
    links = {}

    curr = best_mrca
    chain1 = [curr]
    while curr != id1:
        nxt_s, src = path1[curr]
        links[(curr, nxt_s)] = src
        path_nodes_ids.add(nxt_s)
        chain1.append(nxt_s)
        curr = nxt_s

    curr = best_mrca
    chain2 = [curr]
    while curr != id2:
        nxt_s, src = path2[curr]
        links[(curr, nxt_s)] = src
        path_nodes_ids.add(nxt_s)
        chain2.append(nxt_s)
        curr = nxt_s

    ph = ",".join("?" for _ in path_nodes_ids)
    p_rows = db.execute(f"SELECT * FROM people WHERE id IN ({ph})", list(path_nodes_ids)).fetchall()
    p_map = {r["id"]: row_to_person_brief(r) for r in p_rows}

    # Assign generation offsets relative to MRCA (gen 0)
    nodes = []
    for uid, nb in p_map.items():
        if uid == best_mrca:
            nb["role"] = "mrca"
            nb["gen_offset"] = 0
        elif uid in (id1, id2):
            nb["role"] = "focal"
            nb["gen_offset"] = dist1[best_mrca] - dist1[uid] if uid in dist1 and uid in chain1 else dist2[best_mrca] - dist2[uid]
        else:
            nb["role"] = "bridge"
            if uid in chain1:
                nb["gen_offset"] = chain1.index(uid)
            else:
                nb["gen_offset"] = chain2.index(uid)
        if uid == id1:
            nb["gen_offset"] = len(chain1) - 1
        elif uid == id2:
            nb["gen_offset"] = len(chain2) - 1
        nodes.append(nb)

    relationship = (
        f"{p_map[id1]['name']} is an academic ancestor ({dist2[id1]} gen) of {p_map[id2]['name']}"
        if best_mrca == id1
        else (
            f"{p_map[id2]['name']} is an academic ancestor ({dist1[id2]} gen) of {p_map[id1]['name']}"
            if best_mrca == id2
            else f"Connected via common academic ancestor {p_map[best_mrca]['name']} ({dist1[best_mrca]} gen ↑, {dist2[best_mrca]} gen ↓)"
        )
    )

    return jsonify({
        "connected": True,
        "mrca": p_map[best_mrca],
        "hops_1": dist1[best_mrca],
        "hops_2": dist2[best_mrca],
        "relationship": relationship,
        "nodes": nodes,
        "links": [{"source": a, "target": s, "edge_source": src} for (a, s), src in links.items()],
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    print(f"Starting Statistics Academic Family Tree Explorer at http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
