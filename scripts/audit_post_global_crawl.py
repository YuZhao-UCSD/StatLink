#!/usr/bin/env python3
"""
Wave 5A: Post-Global-Crawl Precision Scrub, School Normalization & Duplicate Stub Merger.
Audits and cleans all newly harvested records (`id >= 4500000`) before running the full
StatLink deduplication, homonym guard, external link/photo enrichment, and 5-pass DB audit.
"""

import os
import re
import sqlite3
import sys
from collections import defaultdict

sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "scripts"))
from etd_ingest_common import DB_PATH, first_last_sig, norm_key

# Non-person phrase tokens that indicate a scraped CV/HTML/Wikipedia artifact rather than a scholar name
NOISE_TOKENS = {
    "statistic", "statistics", "statistical", "algorithm", "algorithms", "estimation", "estimator",
    "engineering", "theorem", "theorems", "process", "processes", "model", "models", "modeling",
    "website", "visit", "method", "methods", "methodology", "distribution", "distributions",
    "regression", "analysis", "analyses", "application", "applications", "journal", "conference",
    "workshop", "symposium", "award", "awards", "fellow", "fellows", "fellowship", "grant", "grants",
    "google", "microsoft", "amazon", "meta", "apple", "uber", "netflix", "citadel", "goldman",
    "jpmorgan", "capital", "bank", "corp", "inc", "llc", "ltd", "press", "review", "annals",
    "biometrika", "jasa", "jrss", "society", "association", "academy", "foundation", "hospital",
    "clinic", "center", "centre", "lab", "laboratory", "group", "team", "project", "projects",
    "program", "programme", "school", "college", "faculty", "dept", "department", "division",
    "theory", "inference", "probability", "stochastic", "bayesian", "causal", "learning",
    "network", "networks", "data", "science", "sciences", "computational", "mathematical",
    "mathematics", "quantitative", "biostatistics", "economics", "econometrics", "finance",
    "financial", "physics", "biology", "chemistry", "medicine", "medical", "health", "clinical",
    "genomics", "genetics", "research", "committee", "dissertation", "thesis", "paper", "papers",
    "book", "books", "chapter", "course", "courses", "seminar", "lecture", "poster", "talk",
    "honors", "prize", "medal", "scholarship", "summer", "fall", "spring", "winter", "january",
    "february", "march", "april", "june", "july", "august", "september", "october", "november",
    "december", "professor", "assistant", "associate", "chair", "director", "dean", "president",
    "postdoc", "postdoctoral", "student", "students", "alumni", "candidate", "visiting", "intern",
    "emeritus", "university", "institute", "national", "international", "american", "european",
    "royal", "federal", "public", "global", "general", "special", "applied", "theoretical",
    "advanced", "modern", "linear", "nonlinear", "random", "optimal", "robust", "sparse",
    "high-dimensional", "neural", "machine", "artificial", "intelligence", "software", "package",
    "system", "systems", "framework", "platform", "tool", "tools", "library", "database",
    "matrix", "matrices", "vector", "graph", "graphical", "tree", "forest", "cluster", "clustering",
    "spatial", "temporal", "series", "panel", "survival", "design", "control", "testing",
    "prediction", "selection", "classification", "optimization", "simulation", "sampling",
    "filtering", "smoothing", "scaling", "approximation", "convergence", "asymptotic",
    "consistency", "efficiency", "variance", "covariance", "correlation", "likelihood",
    "posterior", "prior", "entropy", "information", "metric", "measure", "operator", "equation",
    "differential", "integral", "geometry", "topology", "algebra", "combinatorics", "logic",
    "finite", "infinite", "discrete", "continuous", "dynamic", "static", "parametric",
    "nonparametric", "semiparametric", "empirical", "technical", "working", "preprint",
    "published", "accepted", "submitted", "report", "notes", "slides", "handout", "draft",
    "curriculum", "vitae", "biography", "profile", "homepage", "page", "link", "click", "here",
    "their", "his", "her", "our", "your", "this", "that", "these", "those", "with", "from",
    "using", "based", "via", "for", "and", "the", "of", "in", "on", "to", "at", "by", "as",
    "under", "over", "between", "through", "into", "about", "after", "before", "during",
    "without", "within", "along", "across", "among", "towards", "toward", "against",
    "statistical", "austral", "northwestern", "stanford", "harvard", "berkeley", "cornell",
    "columbia", "chicago", "princeton", "yale", "oxford", "cambridge", "mit", "ucla", "ucsd",
    "nyu", "cmu", "duke", "michigan", "washington", "wisconsin", "minnesota", "purdue",
    "toronto", "waterloo", "mcgill", "zurich", "london", "paris", "berlin", "munich", "tokyo",
}

SCHOOL_PAREN_STRIP_RE = re.compile(
    r"\s*\((?:adoptive|uk|united\s+kingdom|usa|u\.s\.a\.|us|canada|france|germany|switzerland|"
    r"australia|netherlands|italy|spain|sweden|china|japan|india|korea|singapore|hong\s+kong|"
    r"phd\s+candidate|master'?s?|montreal\s+neurological\s+institute|joint|visiting|emeritus|"
    r"former|retired|courant|wharton|kellogg|booth|sloan|hsph|jhsph|sph)\)\s*$",
    re.I,
)


def is_noisy_scraped_name(name: str, src: str) -> bool:
    if not name:
        return True
    if any(ch in name for ch in "/:|\"()[]{}<>@#\$%^*+=_~\\0123456789"):
        return True
    toks = name.split()
    if len(toks) < 2 or len(toks) > 4:
        return True
    # Reject single-initial first name on web/CV/wiki scrapes ("H. Lee", "C. Smith")
    if len(toks[0].strip(".")) <= 1:
        return True
    # Reject single-letter last name
    if len(toks[-1].strip(".")) <= 1:
        return True
    # Check noise tokens
    low_toks = [t.lower().strip(".,;:-'") for t in toks]
    for lt in low_toks:
        if lt in NOISE_TOKENS:
            return True
    # Reject if any token looks like an all-lowercase English word that isn't a noble particle
    particles = {"van", "von", "de", "del", "della", "da", "di", "du", "la", "le", "des", "den", "der", "ten", "ter", "al", "el", "bin", "ibn", "dos", "das", "do", "e", "y"}
    for t, lt in zip(toks, low_toks):
        if lt not in particles and t[0].islower():
            return True
    return False


def main():
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    cur = conn.cursor()

    print("=" * 72)
    print("WAVE 5A: POST-CRAWL PRECISION SCRUB, SCHOOL CLEANUP & STUB MERGER")
    print("=" * 72)

    # 1. Purge noisy Dept Alumni Page records + noisy web/CV/Wikipedia artifacts (id >= 4500000)
    bad_ids = set()
    for pid, name, src, thesis in cur.execute("""
        SELECT id, name, COALESCE(sources,''), COALESCE(thesis,'')
        FROM people WHERE id >= 4500000
    """):
        srcs = [s.strip() for s in src.split(",") if s.strip()]
        # Purge dirty Dept Alumni Page-only records (Yale HTML parse artifacts)
        if srcs == ["Dept Alumni Page"]:
            bad_ids.add(pid)
            continue
        # Strict name audit on web-scraped / CV / Wikipedia Infobox-only records
        if srcs and all(s in ("Faculty Web Crawl", "Professor CV PDF", "Wikipedia Infobox", "Dept Alumni Page") for s in srcs):
            if is_noisy_scraped_name(name, src):
                bad_ids.add(pid)
                continue
        # General sanity check across ALL new sources (no slashes, URLs, or "Website")
        if any(ch in name for ch in "/|[]<>") or "website" in name.lower() or "visit their" in name.lower():
            bad_ids.add(pid)

    print(f"[1/4] Purging {len(bad_ids):,} noisy scraped/HTML artifact nodes (id >= 4500000)...")
    for pid in bad_ids:
        cur.execute("DELETE FROM edges WHERE advisor_id=? OR student_id=?", (pid, pid))
        cur.execute("DELETE FROM advisor_schools WHERE person_id=?", (pid,))
        cur.execute("DELETE FROM people WHERE id=?", (pid,))

    # 2. Clean parenthetical suffixes on school & current_affiliation & advisor_schools
    print("[2/4] Cleaning parenthetical suffixes in `school`, `current_affiliation`, and `advisor_schools`...")
    sch_fixed = 0
    for pid, sch, aff in cur.execute("""
        SELECT id, COALESCE(school,''), COALESCE(current_affiliation,'')
        FROM people
        WHERE school LIKE '%(%' OR current_affiliation LIKE '%(%'
    """).fetchall():
        nsch = SCHOOL_PAREN_STRIP_RE.sub("", sch).strip() if sch else sch
        naff = SCHOOL_PAREN_STRIP_RE.sub("", aff).strip() if aff else aff
        if nsch != sch or naff != aff:
            cur.execute("UPDATE people SET school=?, current_affiliation=? WHERE id=?", (nsch, naff, pid))
            sch_fixed += 1

    # Also clean advisor_schools parentheticals
    adv_sch_rows = cur.execute("SELECT person_id, school, advised_at_school FROM advisor_schools WHERE school LIKE '%(%'").fetchall()
    for pid, sch, cnt in adv_sch_rows:
        nsch = SCHOOL_PAREN_STRIP_RE.sub("", sch).strip()
        if nsch and nsch != sch:
            cur.execute("DELETE FROM advisor_schools WHERE person_id=? AND school=?", (pid, sch))
            cur.execute("INSERT OR IGNORE INTO advisor_schools (person_id, school, advised_at_school) VALUES (?, ?, ?)", (pid, nsch, cnt))
    print(f"  -> Cleaned {sch_fixed:,} person school/affiliation parentheticals")

    # 3. Merge duplicate newly-created records (id >= 4500000) onto canonical existing scholars (id < 4500000)
    print("[3/4] Merging duplicate newly-created stubs (id >= 4500000) onto canonical scholars...")
    canon_by_norm = defaultdict(list)
    canon_by_sig = defaultdict(list)
    for row in cur.execute("""
        SELECT id, name, norm_name, COALESCE(school,''), year, COALESCE(thesis,''),
               COALESCE(orcid,''), COALESCE(homepage,''), COALESCE(sources,''),
               is_core_stat, is_broad_stat, COALESCE(primary_field,'')
        FROM people WHERE id < 4500000
    """):
        rec = {
            "id": row[0], "name": row[1], "norm_name": row[2], "school": row[3], "year": row[4],
            "thesis": row[5], "orcid": row[6], "homepage": row[7], "sources": row[8],
            "is_core_stat": row[9], "is_broad_stat": row[10], "primary_field": row[11],
        }
        nk = row[2] or norm_key(row[1])
        if nk:
            canon_by_norm[nk].append(rec)
        sig = first_last_sig(row[1])
        if sig[0] and sig[1]:
            canon_by_sig[sig].append(rec)

    # Preload edges for overlap check
    adv_of = defaultdict(set)
    stu_of = defaultdict(set)
    for a, s in cur.execute("SELECT advisor_id, student_id FROM edges"):
        adv_of[s].add(a)
        stu_of[a].add(s)

    new_rows = cur.execute("""
        SELECT id, name, norm_name, COALESCE(school,''), year, COALESCE(thesis,''),
               COALESCE(orcid,''), COALESCE(homepage,''), COALESCE(sources,''),
               is_core_stat, is_broad_stat, COALESCE(primary_field,'')
        FROM people WHERE id >= 4500000
    """).fetchall()

    merged_cnt = 0
    for row in new_rows:
        nid, name, nn, sch, yr, th, orc, hp, src, core, broad, pf = row
        nk = nn or norm_key(name)
        sig = first_last_sig(name)

        cands = list(canon_by_norm.get(nk, ()))
        if not cands and sig[0] and sig[1]:
            cands = list(canon_by_sig.get(sig, ()))
        if not cands:
            continue

        target = None
        best_sc = -1
        for c in cands:
            # Strict chronological incompatibility check
            if yr and c["year"] and abs(yr - c["year"]) > 4:
                continue
            sc = 0
            exact_nm = (norm_key(c["name"]) == nk)
            if exact_nm:
                sc += 50
            elif sig[0] and sig[1] and first_last_sig(c["name"]) == sig:
                sc += 30
            if sch and c["school"] and sch.lower() == c["school"].lower():
                sc += 50
            if yr and c["year"] and abs(yr - c["year"]) <= 2:
                sc += 45
            # Shared advisor or student!
            if adv_of[nid] & adv_of[c["id"]]:
                sc += 65
            if stu_of[nid] & stu_of[c["id"]]:
                sc += 65
            # Undated Wikipedia/Wikidata stub matching unique exact-name canonical scholar
            if yr is None and exact_nm and len(cands) == 1:
                sc += 40
            elif yr is None and exact_nm and c["is_core_stat"]:
                sc += 32
            if sc > best_sc:
                best_sc = sc
                target = c

        if target and best_sc >= 82:
            tid = target["id"]
            # Transfer edges (avoiding self-loops and reverse edges)
            for aid in list(adv_of[nid]):
                if aid != tid and tid not in adv_of[aid]:
                    cur.execute("INSERT OR IGNORE INTO edges (advisor_id, student_id, source) VALUES (?, ?, ?)", (aid, tid, src.split(",")[0] or "ETD"))
                    adv_of[tid].add(aid)
                    stu_of[aid].add(tid)
            for sid in list(stu_of[nid]):
                if sid != tid and sid not in adv_of[tid]:
                    cur.execute("INSERT OR IGNORE INTO edges (advisor_id, student_id, source) VALUES (?, ?, ?)", (tid, sid, src.split(",")[0] or "ETD"))
                    stu_of[tid].add(sid)
                    adv_of[sid].add(tid)
            # Enrich target
            merged_srcs = list(dict.fromkeys([s.strip() for s in (target["sources"] + "," + src).split(",") if s.strip()]))
            cur.execute("""
                UPDATE people SET
                    school = CASE WHEN school = '' OR school IS NULL THEN ? ELSE school END,
                    year = COALESCE(year, ?),
                    thesis = CASE WHEN thesis = '' OR thesis IS NULL THEN ? ELSE thesis END,
                    orcid = CASE WHEN orcid = '' OR orcid IS NULL THEN ? ELSE orcid END,
                    homepage = CASE WHEN homepage = '' OR homepage IS NULL THEN ? ELSE homepage END,
                    sources = ?
                WHERE id = ?
            """, (sch, yr, th, orc, hp, ",".join(merged_srcs), tid))
            # Delete duplicate stub
            cur.execute("DELETE FROM edges WHERE advisor_id=? OR student_id=?", (nid, nid))
            cur.execute("DELETE FROM advisor_schools WHERE person_id=?", (nid,))
            cur.execute("DELETE FROM people WHERE id=?", (nid,))
            merged_cnt += 1

    print(f"  -> Merged {merged_cnt:,} duplicate newly-created stubs onto canonical scholars")

    # 4. Purge any newly-created orphan nodes (id >= 4500000) with 0 edges AND empty thesis
    orphans = cur.execute("""
        SELECT p.id FROM people p
        WHERE p.id >= 4500000
          AND (p.thesis IS NULL OR p.thesis = '')
          AND NOT EXISTS (SELECT 1 FROM edges e WHERE e.advisor_id = p.id OR e.student_id = p.id)
    """).fetchall()
    for (oid,) in orphans:
        cur.execute("DELETE FROM advisor_schools WHERE person_id=?", (oid,))
        cur.execute("DELETE FROM people WHERE id=?", (oid,))
    print(f"[4/4] Purged {len(orphans):,} edge-less/thesis-less newly created orphan stubs")

    conn.commit()
    conn.close()
    print("\nWave 5A precision scrub COMPLETE!")


if __name__ == "__main__":
    main()
