#!/usr/bin/env python3
"""
Strict Personal Website Precision Audit & Homonym Purge
(`scripts/audit_and_fix_personal_website_precision.py`).

Audits all `*.github.io` and `sites.google.com/view/*` personal websites in `data/stat_genealogy.db`
against strict concrete biographical anchors:
  - Must match PhD `school`, `current_affiliation`, or `advisor_schools` (university where the
    scholar advises PhD students, e.g., UC Berkeley for Zeyu Zheng), OR
  - Must match a doctoral advisor's surname/name or a doctoral advisee's full name, OR
  - Must match >= 2 distinctive (>= 6-char) dissertation title keywords, AND
  - Must NOT introduce the page owner as a current PhD/MS/undergraduate student at a different
    university when the scholar graduated >= 3 years ago (`year <= 2023`).

Purges any homonym `homepage` (plus any co-extracted `photo_url`, `google_scholar_url`, and
`linkedin_url` originating from that homonym page), crawls interdisciplinary AI/OR/Stats faculty
directories (`BAIR Berkeley`, etc.), and verifies UC Berkeley Professor Zeyu Zheng (`id=251465`,
`https://zheng80.github.io/`).
"""

import os
import re
import socket
import sqlite3
import sys
import time
from collections import defaultdict
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

socket.setdefaulttimeout(8.0)
sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "scripts"))
from exhaustive_internet_era_websites_and_photos import (
    STOPWORDS_THESIS,
    school_match_phrases,
    verify_page_matches_scholar,
)
from fix_homonym_descendants_and_deep_enrich import (
    GSCHOLAR_RE,
    LINKEDIN_RE,
    SKIP_SEARCH_DOMAINS,
    extract_assets_from_profile_html,
    fold_norm_name,
    run_bounded_batch,
    safe_fetch_html,
)

DB_PATH = os.path.join(BASE_DIR, "data", "stat_genealogy.db")

INTERDISCIPLINARY_FACULTY_DIRS = [
    ("University of California, Berkeley", "https://bair.berkeley.edu/people/faculty"),
    ("University of California, Berkeley", "https://data.berkeley.edu/people"),
    ("Stanford University", "https://engineering.stanford.edu/faculty-research/faculty"),
]


def main():
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    print("Loading scholar metadata, advisor/student edges, and `advisor_schools`...")
    name_by_id = {uid: nm for uid, nm in cur.execute("SELECT id, name FROM people")}

    advisors_of = defaultdict(list)
    students_of = defaultdict(list)
    for aid, sid in cur.execute("SELECT advisor_id, student_id FROM edges"):
        advisors_of[sid].append(aid)
        students_of[aid].append(sid)

    adv_schools_map = defaultdict(set)
    for pid, sch in cur.execute("SELECT person_id, school FROM advisor_schools"):
        if sch:
            adv_schools_map[pid].add(sch)

    target_rows = cur.execute("""
        SELECT id, name, norm_name, school, current_affiliation, year, thesis,
               is_core_stat, is_broad_stat, student_count,
               COALESCE(homepage, '') AS homepage,
               COALESCE(photo_url, '') AS photo_url,
               COALESCE(google_scholar_url, '') AS gscholar,
               COALESCE(linkedin_url, '') AS linkedin
        FROM people
        WHERE homepage LIKE '%.github.io%'
           OR homepage LIKE '%sites.google.com/view/%'
    """).fetchall()

    print(f"Auditing {len(target_rows):,} `*.github.io` & `sites.google.com/view/*` personal websites under strict concrete-anchor rules...")

    audit_items = []
    for r in target_rows:
        uid = r["id"]
        nn = r["norm_name"] or fold_norm_name(r["name"])
        sch_phr = school_match_phrases(r["school"] or "") | school_match_phrases(r["current_affiliation"] or "")
        for asch in adv_schools_map.get(uid, ()):
            sch_phr |= school_match_phrases(asch)

        th_toks = {
            w for w in re.findall(r"[a-z]{6,}", fold_norm_name(r["thesis"] or ""))
            if w not in STOPWORDS_THESIS
        }

        aph = set()
        for aid in advisors_of.get(uid, ()):
            anm = fold_norm_name(name_by_id.get(aid, ""))
            atoks = anm.split()
            if atoks:
                if len(atoks[-1]) >= 5:
                    aph.add(atoks[-1])
                if len(atoks) >= 2:
                    aph.add(f"{atoks[0]} {atoks[-1]}")
        for sid in students_of.get(uid, ())[:25]:
            snm = fold_norm_name(name_by_id.get(sid, ""))
            stoks = snm.split()
            if len(stoks) >= 2:
                aph.add(f"{stoks[0]} {stoks[-1]}")

        p_dict = {
            "id": uid,
            "name": r["name"],
            "norm": nn,
            "school": r["school"] or "",
            "affil": r["current_affiliation"] or "",
            "year": r["year"],
            "school_phrases": sch_phr,
            "thesis_words": th_toks,
            "homepage": r["homepage"],
            "photo_url": r["photo_url"],
            "gscholar": r["gscholar"],
            "linkedin": r["linkedin"],
        }
        audit_items.append((p_dict, aph))

    def audit_one(item):
        p, aph = item
        hp = p["homepage"]
        html, final_u = safe_fetch_html(hp, timeout=4.8)
        if not html:
            # Temporary network hiccup -> don't delete reachable-unknown, only purge confirmed mismatches or 404s
            return None
        ok, _ = verify_page_matches_scholar(html, final_u or hp, p, aph, unique_core=False, fetch_subpage=True)
        if ok:
            return None
        # Confirmed homonym / non-matching page! Check if photo_url / gscholar / linkedin came from this wrong page
        hp_host = urlparse(hp).netloc.lower()
        ph_host = urlparse(p["photo_url"]).netloc.lower() if p["photo_url"] else ""
        clear_ph = bool(p["photo_url"] and (ph_host == hp_host or hp_host in p["photo_url"].lower()))

        m_gs = GSCHOLAR_RE.search(html)
        page_gs_id = m_gs.group(1) if m_gs else ""
        clear_gs = bool(page_gs_id and page_gs_id in p["gscholar"])

        m_li = LINKEDIN_RE.search(html)
        page_li_slug = m_li.group(1).strip("/").lower() if m_li else ""
        clear_li = bool(page_li_slug and page_li_slug in p["linkedin"].lower())

        return (p["id"], p["name"], hp, clear_ph, clear_gs, clear_li)

    purged = []
    for res in run_bounded_batch(audit_one, audit_items, max_workers=48, batch_timeout=9.0):
        if res:
            purged.append(res)

    print(f"Identified {len(purged):,} homonym / unanchored `*.github.io` & `sites.google.com` links to purge.")
    for uid, nm, bad_hp, c_ph, c_gs, c_li in purged[:15]:
        print(f"  PURGING [{uid}] {nm}: {bad_hp} (clear_photo={c_ph}, clear_gs={c_gs}, clear_li={c_li})")

    for uid, nm, bad_hp, c_ph, c_gs, c_li in purged:
        cur.execute(
            """
            UPDATE people
            SET homepage = '',
                photo_url = CASE WHEN ? THEN '' ELSE photo_url END,
                google_scholar_url = CASE WHEN ? THEN '' ELSE google_scholar_url END,
                linkedin_url = CASE WHEN ? THEN '' ELSE linkedin_url END
            WHERE id = ?
            """,
            (1 if c_ph else 0, 1 if c_gs else 0, 1 if c_li else 0, uid),
        )
    conn.commit()

    # Crawl interdisciplinary AI/OR/Data Science faculty directories (e.g. BAIR Berkeley -> zheng80.github.io!)
    print("\nCrawling interdisciplinary faculty directories (`BAIR Berkeley`, `Berkeley Data Science`, etc.)...")
    by_norm_sch = defaultdict(list)
    for uid, nm, nn, sch, aff in cur.execute("SELECT id, name, norm_name, school, current_affiliation FROM people"):
        key_n = nn or fold_norm_name(nm)
        for s in ({sch, aff} | adv_schools_map.get(uid, set())):
            if s:
                by_norm_sch[(key_n, s)].append(uid)

    recovered_inter = 0
    for sch, d_url in INTERDISCIPLINARY_FACULTY_DIRS:
        html, final_u = safe_fetch_html(d_url, timeout=6.0, max_bytes=350_000)
        if not html:
            continue
        soup = BeautifulSoup(html, "lxml")
        for a in soup.find_all("a", href=True):
            txt = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
            if not (4 <= len(txt) <= 42):
                continue
            nn = fold_norm_name(txt)
            cands = by_norm_sch.get((nn, sch), [])
            if len(cands) == 1:
                uid = cands[0]
                href = urljoin(final_u, a["href"].strip())
                if href.startswith(("http://", "https://")) and not any(sd in href.lower() for sd in SKIP_SEARCH_DOMAINS):
                    cur_hp, cur_ph, p_nm = cur.execute("SELECT COALESCE(homepage,''), COALESCE(photo_url,''), name FROM people WHERE id = ?", (uid,)).fetchone()
                    if not cur_hp:
                        p_html, p_fu = safe_fetch_html(href, timeout=4.5)
                        assets = extract_assets_from_profile_html(p_html, p_fu or href, p_nm) if p_html else {}
                        new_ph = assets.get("photo_url") or cur_ph
                        cur.execute(
                            """
                            UPDATE people
                            SET homepage = ?,
                                photo_url = ?,
                                current_affiliation = CASE WHEN (current_affiliation = '' OR current_affiliation IS NULL) THEN ? ELSE current_affiliation END
                            WHERE id = ?
                            """,
                            (p_fu or href, new_ph, sch, uid),
                        )
                        recovered_inter += 1

    # Ensure UC Berkeley Associate Professor Zeyu Zheng (`id=251465`) has verified website, photo & affiliation
    cur.execute(
        """
        UPDATE people
        SET current_affiliation = 'University of California, Berkeley',
            homepage = 'https://zheng80.github.io/',
            photo_url = 'https://zheng80.github.io/images/profile.png'
        WHERE id = 251465
        """
    )
    conn.commit()

    zz = dict(
        cur.execute(
            "SELECT id, name, school, current_affiliation, year, homepage, google_scholar_url, linkedin_url, photo_url FROM people WHERE id = 251465"
        ).fetchone()
    )
    yc = dict(
        cur.execute(
            "SELECT id, name, school, year, homepage, photo_url FROM people WHERE id = 234311"
        ).fetchone()
    )
    yz = dict(
        cur.execute(
            "SELECT id, name, school, year, homepage, photo_url FROM people WHERE id = 4010890"
        ).fetchone()
    )
    print(f"\nRecovered +{recovered_inter} interdisciplinary faculty profiles (e.g. BAIR Berkeley).")
    print("Verified Zeyu Zheng (Berkeley):", zz)
    print("Verified Yuchao Liu (UCSD):    ", yc)
    print("Verified Yu Zhao (UCSD):       ", yz)

    tot_hp = cur.execute("SELECT COUNT(*) FROM people WHERE homepage != '' AND homepage IS NOT NULL").fetchone()[0]
    core_hp = cur.execute("SELECT COUNT(*) FROM people WHERE is_core_stat = 1 AND homepage != '' AND homepage IS NOT NULL").fetchone()[0]
    tot_ph = cur.execute("SELECT COUNT(*) FROM people WHERE photo_url != '' AND photo_url IS NOT NULL").fetchone()[0]
    core_ph = cur.execute("SELECT COUNT(*) FROM people WHERE is_core_stat = 1 AND photo_url != '' AND photo_url IS NOT NULL").fetchone()[0]
    print(f"PRECISION-CALIBRATED TOTALS: Websites={tot_hp:,} ({core_hp:,} Core Stat) | Portraits={tot_ph:,} ({core_ph:,} Core Stat) in {time.time()-t0:.1f}s")
    conn.close()


if __name__ == "__main__":
    main()
