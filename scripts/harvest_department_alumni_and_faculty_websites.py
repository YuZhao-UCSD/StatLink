#!/usr/bin/env python3
"""
Wave 3 + 4 Harvester:
  1. Departmental PhD Alumni & Recent Dissertation Web Pages across tracked Stat/Biostat universities
  2. Exhaustive Crawl of ALL verified modern Core Stat faculty personal websites,
     lab `/students` & `/people` subpages, and CV PDFs (`pdftotext -layout`) using
     strict doctoral-only section parsing (`crawler_engine.py`).
"""

import concurrent.futures
import json
import os
import re
import socket
import sqlite3
import sys
import time
import urllib3
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
socket.setdefaulttimeout(9.0)
sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "scripts"))

import crawler_engine as ce
from etd_ingest_common import (
    CACHE_DIR,
    DB_PATH,
    HEADERS,
    StatLinkIngestor,
    clean_person_name,
    clean_title,
    norm_key,
    split_advisor_field,
)

CACHE_DEPT_FILE = os.path.join(CACHE_DIR, "wave3_dept_alumni_records.json")
CACHE_FAC_FILE = os.path.join(CACHE_DIR, "wave4_faculty_websites_records.json")

# Curated Departmental PhD Alumni / Dissertation Listing Pages (high-value gap & flagship depts)
DEPT_ALUMNI_PAGES = [
    ("Harvard University", "United States", "Statistics", "https://statistics.fas.harvard.edu/alumni"),
    ("Harvard University", "United States", "Biostatistics", "https://hsph.harvard.edu/department/biostatistics/alumni/"),
    ("Stanford University", "United States", "Statistics", "https://statistics.stanford.edu/people/alumni"),
    ("University of California, Berkeley", "United States", "Statistics", "https://statistics.berkeley.edu/people/alumni"),
    ("Cornell University", "United States", "Statistics", "https://stat.cornell.edu/people/alumni"),
    ("Cornell University", "United States", "Statistics", "https://stat.cornell.edu/academics/phd/recent-phd-graduates"),
    ("Columbia University", "United States", "Statistics", "https://stat.columbia.edu/phd-alumni/"),
    ("Columbia University", "United States", "Statistics", "https://stat.columbia.edu/people/alumni/"),
    ("University of Chicago", "United States", "Statistics", "https://stat.uchicago.edu/people/alumni/"),
    ("University of Chicago", "United States", "Statistics", "https://stat.uchicago.edu/academics/graduate-programs/ph-d-alumni/"),
    ("Carnegie Mellon University", "United States", "Statistics", "https://www.cmu.edu/dietrich/statistics-datascience/people/alumni/index.html"),
    ("University of Pennsylvania", "United States", "Statistics", "https://statistics.wharton.upenn.edu/programs/phd/placement/"),
    ("Duke University", "United States", "Statistics", "https://stat.duke.edu/people/alumni"),
    ("University of Michigan", "United States", "Statistics", "https://lsa.umich.edu/stats/people/alumni.html"),
    ("University of Michigan", "United States", "Biostatistics", "https://sph.umich.edu/biostat/alumni/"),
    ("University of Washington", "United States", "Statistics", "https://stat.uw.edu/people/alumni"),
    ("University of Washington", "United States", "Biostatistics", "https://www.biostat.washington.edu/people/alumni"),
    ("University of North Carolina at Chapel Hill", "United States", "Statistics", "https://stor.unc.edu/people/alumni/"),
    ("University of North Carolina at Chapel Hill", "United States", "Biostatistics", "https://sph.unc.edu/bios/bios-alumni/"),
    ("Johns Hopkins University", "United States", "Biostatistics", "https://publichealth.jhu.edu/departments/biostatistics/people/alumni"),
    ("Yale University", "United States", "Statistics", "https://statistics.yale.edu/people/alumni"),
    ("Princeton University", "United States", "Statistics", "https://orfe.princeton.edu/people/alumni"),
    ("University of Wisconsin-Madison", "United States", "Statistics", "https://stat.wisc.edu/phd-alumni/"),
    ("Iowa State University", "United States", "Statistics", "https://www.stat.iastate.edu/people/alumni"),
    ("Purdue University", "United States", "Statistics", "https://www.stat.purdue.edu/people/alumni/index.html"),
    ("North Carolina State University", "United States", "Statistics", "https://statistics.sciences.ncsu.edu/people/alumni/"),
    ("Texas A&M University", "United States", "Statistics", "https://stat.tamu.edu/people/alumni/"),
    ("University of Illinois Urbana-Champaign", "United States", "Statistics", "https://stat.illinois.edu/directory/alumni"),
    ("Ohio State University", "United States", "Statistics", "https://stat.osu.edu/people/alumni"),
    ("University of Minnesota", "United States", "Statistics", "https://cla.umn.edu/statistics/people/alumni"),
    ("Pennsylvania State University", "United States", "Statistics", "https://science.psu.edu/stat/people/alumni"),
    ("Rutgers University", "United States", "Statistics", "https://statistics.rutgers.edu/people/alumni"),
    ("University of Pittsburgh", "United States", "Statistics", "https://www.stat.pitt.edu/people/alumni"),
    ("University of Pittsburgh", "United States", "Biostatistics", "https://www.sph.pitt.edu/biostatistics/alumni"),
    ("University of Maryland", "United States", "Statistics", "https://www-math.umd.edu/stat-alumni.html"),
    ("University of Texas at Austin", "United States", "Statistics", "https://stat.utexas.edu/people/alumni"),
    ("University of Southern California", "United States", "Biostatistics", "https://pphs.usc.edu/biostatistics/alumni/"),
    ("University of Iowa", "United States", "Statistics", "https://stat.uiowa.edu/people/alumni"),
    ("University of Iowa", "United States", "Biostatistics", "https://www.public-health.uiowa.edu/biostatistics-alumni/"),
    ("Michigan State University", "United States", "Statistics", "https://stt.natsci.msu.edu/people/alumni/"),
    ("Florida State University", "United States", "Statistics", "https://stat.fsu.edu/people/alumni"),
    ("University of Florida", "United States", "Statistics", "https://stat.ufl.edu/people/alumni/"),
    ("University of Connecticut", "United States", "Statistics", "https://stat.uconn.edu/phd-alumni/"),
    ("Brown University", "United States", "Biostatistics", "https://biostatistics.sph.brown.edu/people/alumni"),
    ("Boston University", "United States", "Biostatistics", "https://www.bu.edu/sph/about/departments/biostatistics/alumni/"),
    ("Emory University", "United States", "Biostatistics", "https://sph.emory.edu/departments/bios/alumni/index.html"),
    ("Vanderbilt University", "United States", "Biostatistics", "https://www.vumc.org/biostatistics/alumni"),
    ("Rice University", "United States", "Statistics", "https://statistics.rice.edu/people/alumni"),
    ("Northwestern University", "United States", "Statistics", "https://statistics.northwestern.edu/people/alumni/"),
    ("New York University", "United States", "Statistics", "https://cds.nyu.edu/phd-alumni/"),
    ("University of Rochester", "United States", "Biostatistics", "https://www.urmc.rochester.edu/biostatistics-computational-biology/alumni.aspx"),
    ("University of Toronto", "Canada", "Statistics", "https://www.statistics.utoronto.ca/people/alumni"),
    ("University of Waterloo", "Canada", "Statistics", "https://uwaterloo.ca/statistics-and-actuarial-science/people-profiles/alumni"),
    ("University of British Columbia", "Canada", "Statistics", "https://www.stat.ubc.ca/alumni"),
    ("University of Oxford", "United Kingdom", "Statistics", "https://www.stats.ox.ac.uk/people/alumni"),
    ("ETH Zürich", "Switzerland", "Statistics", "https://stat.math.ethz.ch/people/alumni.html"),
]

SKIP_DOMAINS = (
    "wikipedia.org", "wikidata.org", "scholar.google.", "linkedin.com", "orcid.org",
    "mathgenealogy.org", "academictree.org", "researchgate.net", "twitter.com", "x.com",
    "youtube.com", "facebook.com", "instagram.com", "arxiv.org", "dblp.org",
    "amazon.com", "springer.com", "wiley.com", "elsevier.com", "jstor.org", "zbmath.org",
    "mathscinet", "ams.org", "ims.org", "amstat.org", "doi.org", "ncbi.nlm.nih.gov",
)

SUBPAGE_LINK_RE = re.compile(
    r"\b(student|advisee|group|people|team|lab|member|alumni|mentoring|advising|supervision)\b",
    re.I,
)
CV_LINK_RE = re.compile(r"(?:^|/|_|-)(?:cv|vita|vitae|curriculum)(?:[^/]*)\.pdf$", re.I)


def safe_get(sess, url, timeout=8):
    try:
        r = sess.get(url, timeout=timeout, allow_redirects=True)
        return r
    except requests.exceptions.SSLError:
        try:
            r = sess.get(url, timeout=timeout, allow_redirects=True, verify=False)
            return r
        except Exception:
            return None
    except Exception:
        return None


def parse_dept_alumni_page(item):
    school, country, field, url = item
    records = []
    sess = requests.Session()
    sess.headers.update(HEADERS)
    r = safe_get(sess, url, timeout=10)
    if not r or r.status_code != 200 or "html" not in (r.headers.get("content-type") or "").lower():
        return records

    soup = BeautifulSoup(r.text, "lxml")

    # Strategy 1: Parse HTML tables with explicit Name + Advisor (+ Year + Thesis) columns!
    for tbl in soup.find_all("table"):
        rows = tbl.find_all("tr")
        if len(rows) < 3:
            continue
        hdr_cells = [c.get_text(" ", strip=True).lower() for c in rows[0].find_all(["th", "td"])]
        col_name = col_adv = col_yr = col_th = -1
        for idx, h in enumerate(hdr_cells):
            if any(w in h for w in ("name", "student", "graduate", "alumni", "author")) and col_name < 0:
                col_name = idx
            elif any(w in h for w in ("advisor", "adviser", "supervisor", "chair", "mentor")) and col_adv < 0:
                col_adv = idx
            elif any(w in h for w in ("year", "date", "grad", "term")) and col_yr < 0:
                col_yr = idx
            elif any(w in h for w in ("thesis", "dissertation", "title", "topic")) and col_th < 0:
                col_th = idx
        if col_name < 0 or col_adv < 0:
            continue
        for tr in rows[1:]:
            tds = tr.find_all(["td", "th"])
            if len(tds) <= max(col_name, col_adv):
                continue
            row_txt = " ".join(c.get_text(" ", strip=True) for c in tds)
            if re.search(r"\b(m\.s\.|m\.a\.|master|b\.s\.|undergrad)\b", row_txt, re.I) and not re.search(r"\b(ph\.?d|doctor|sc\.?d)\b", row_txt, re.I):
                continue
            stu = clean_person_name(tds[col_name].get_text(" ", strip=True))
            advs = split_advisor_field(tds[col_adv].get_text(" ", strip=True))
            if not stu or not advs:
                continue
            yr = None
            if col_yr >= 0 and col_yr < len(tds):
                ym = re.search(r"\b(19\d{2}|20[0-2]\d)\b", tds[col_yr].get_text(" ", strip=True))
                if ym:
                    yr = int(ym.group(1))
            if not yr:
                ym = re.search(r"\b(19\d{2}|20[0-2]\d)\b", row_txt)
                if ym:
                    yr = int(ym.group(1))
            thesis = clean_title(tds[col_th].get_text(" ", strip=True)) if (col_th >= 0 and col_th < len(tds)) else ""
            records.append({
                "student": stu,
                "advisors": advs,
                "school": school,
                "country": country,
                "year": yr,
                "thesis": thesis,
                "subjects": field,
                "degree": "Ph.D.",
                "force_field": field,
                "source": "Dept Alumni Page",
            })

    # Strategy 2: Parse list items / paragraphs containing explicit "Advisor:" / "Adviser:" / "Supervisor:"
    for el in soup.find_all(["li", "p", "div"]):
        if el.name == "div" and len(el.find_all(["div", "p", "li"])) > 3:
            continue
        txt = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
        if len(txt) < 20 or len(txt) > 550:
            continue
        m_adv = re.search(r"\b(?:Advisor|Adviser|Supervisor|Chair)s?\s*:\s*([^;\n\|]+?)(?:\s*(?:Thesis|Dissertation|Title|Current|Now|Placement|Position|Email|\(|\[|$))", txt, re.I)
        if not m_adv:
            continue
        if re.search(r"\b(m\.s\.|m\.a\.|master|b\.s\.|undergrad)\b", txt, re.I) and not re.search(r"\b(ph\.?d|doctor|sc\.?d)\b", txt, re.I):
            continue
        advs = split_advisor_field(m_adv.group(1))
        if not advs:
            continue
        # Student name is typically at the start of the line/item before year, comma, dash, or "Advisor:"
        head = re.split(r"(?:\(|,|–|—|\s-\s|Advisor|Adviser|Supervisor|Ph\.?D|20\d{2}|19\d{2})", txt, maxsplit=1, flags=re.I)[0].strip()
        stu = clean_person_name(head)
        if not stu:
            continue
        ym = re.search(r"\b(19[6-9]\d|20[0-2]\d)\b", txt)
        yr = int(ym.group(1)) if ym else None
        m_th = re.search(r'(?:Thesis|Dissertation|Title)\s*:\s*"?([^";\|]{12,260})"?', txt, re.I)
        thesis = clean_title(m_th.group(1)) if m_th else ""
        records.append({
            "student": stu,
            "advisors": advs,
            "school": school,
            "country": country,
            "year": yr,
            "thesis": thesis,
            "subjects": field,
            "degree": "Ph.D.",
            "force_field": field,
            "source": "Dept Alumni Page",
        })

    if records:
        print(f"  [Dept Alumni] {school:42s} ({field}): {len(records):4d} PhD alumni from {url}")
    return records


def crawl_faculty_homepage_worker(fac):
    """Crawl one Core Stat professor's verified homepage + student subpage + CV PDF."""
    adv_id, adv_name, adv_school, adv_country, adv_field, adv_year, homepage = fac
    found = []
    seen_nk = set()

    hp = homepage.strip()
    if not hp.startswith(("http://", "https://")):
        return found
    dom = urlparse(hp).netloc.lower()
    if not dom or any(sd in dom for sd in SKIP_DOMAINS):
        return found
    # Skip bare github.com repos (keep *.github.io personal websites!)
    if dom == "github.com":
        return found

    sess = requests.Session()
    sess.headers.update(HEADERS)

    r = safe_get(sess, hp, timeout=8)
    if not r or r.status_code != 200:
        return found

    ct = (r.headers.get("content-type") or "").lower()
    pages_html = []
    cv_urls = []

    if "pdf" in ct or r.content[:4] == b"%PDF":
        raw_stus = ce.parse_cv_pdf_bytes(r.content, adv_name, adv_school)
    elif "html" in ct:
        pages_html.append((r.url, r.text))
        raw_stus = ce.parse_html_group_page(r.text, adv_name, adv_school)

        # Follow up to 2 student/group subpages and 1 CV PDF on the same host
        try:
            soup = BeautifulSoup(r.text, "lxml")
            sub_urls = []
            seen_u = {r.url}
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                if not href or href.startswith(("#", "mailto:", "javascript:")):
                    continue
                abs_u = urljoin(r.url, href)
                if abs_u in seen_u:
                    continue
                pu = urlparse(abs_u)
                if pu.scheme not in ("http", "https"):
                    continue
                # Only follow same domain or same parent edu domain
                if pu.netloc.lower() != urlparse(r.url).netloc.lower():
                    continue
                atext = a.get_text(" ", strip=True)
                if CV_LINK_RE.search(pu.path) or (pu.path.lower().endswith(".pdf") and re.search(r"\b(cv|vita|curriculum)\b", atext, re.I)):
                    if len(cv_urls) < 1:
                        cv_urls.append(abs_u)
                        seen_u.add(abs_u)
                elif SUBPAGE_LINK_RE.search(atext) or SUBPAGE_LINK_RE.search(pu.path):
                    if not pu.path.lower().endswith((".pdf", ".jpg", ".png", ".zip", ".bib")):
                        if len(sub_urls) < 2:
                            sub_urls.append(abs_u)
                            seen_u.add(abs_u)

            for su in sub_urls:
                rs = safe_get(sess, su, timeout=7)
                if rs and rs.status_code == 200 and "html" in (rs.headers.get("content-type") or "").lower():
                    raw_stus.extend(ce.parse_html_group_page(rs.text, adv_name, adv_school))

            for cu in cv_urls:
                rc = safe_get(sess, cu, timeout=9)
                if rc and rc.status_code == 200 and len(rc.content) < 12_000_000:
                    raw_stus.extend(ce.parse_cv_pdf_bytes(rc.content, adv_name, adv_school))
        except Exception:
            pass
    else:
        return found

    # Filter & validate extracted students strictly!
    # Cap at 60 per faculty page to guard against malformed publication-list parsers
    if len(raw_stus) > 65:
        return []

    adv_nk = norm_key(adv_name)
    for st in raw_stus:
        snm = clean_person_name(st.get("student") or "")
        if not snm:
            continue
        snk = norm_key(snm)
        if not snk or snk == adv_nk or snk in seen_nk:
            continue
        yr = st.get("year")
        # Require a plausible graduation year (or allow undated only if advisor already has students & year<=2018)
        if yr is not None:
            if yr < 1965 or yr > 2026:
                continue
            if adv_year and (yr - adv_year <= 3 or yr - adv_year > 65):
                continue
        else:
            # Skip undated entries from general web scraping to avoid importing current ungraduated students or noise!
            continue
        seen_nk.add(snk)
        found.append({
            "student": snm,
            "advisors": [adv_name],
            "known_advisor_id": adv_id,
            "school": st.get("school") or adv_school,
            "country": adv_country,
            "year": yr,
            "thesis": clean_title(st.get("thesis") or ""),
            "subjects": adv_field,
            "degree": "Ph.D.",
            "force_field": adv_field,
            "source": st.get("source") or "Faculty Web Crawl",
        })
    return found


def run_bounded_batch(worker_fn, items, max_workers=24, label="Batch"):
    """Single shared ThreadPoolExecutor with FIRST_COMPLETED sliding window (no thread leaks)."""
    results = []
    total = len(items)
    done = 0
    it = iter(items)
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        in_flight = set()
        for _ in range(min(max_workers * 2, total)):
            try:
                in_flight.add(pool.submit(worker_fn, next(it)))
            except StopIteration:
                break
        while in_flight:
            finished, in_flight = concurrent.futures.wait(
                in_flight, return_when=concurrent.futures.FIRST_COMPLETED
            )
            for fut in finished:
                done += 1
                try:
                    res = fut.result()
                    if res:
                        results.extend(res)
                except Exception:
                    pass
                try:
                    in_flight.add(pool.submit(worker_fn, next(it)))
                except StopIteration:
                    pass
            if done % 400 == 0 or done == total:
                print(f"    [{label}] {done:,}/{total:,} crawled -> {len(results):,} graduated PhD advisees ({time.time() - t0:.1f}s)")
    return results


def main():
    t0 = time.time()
    print("=" * 72)
    print("WAVE 3 + 4: DEPT ALUMNI PAGES + ALL VERIFIED CORE STAT FACULTY WEBSITES")
    print("=" * 72)

    # Stage 1: Departmental Alumni Pages
    print(f"\n[1/2] Crawling {len(DEPT_ALUMNI_PAGES)} departmental PhD Alumni / Dissertation pages...")
    dept_records = run_bounded_batch(parse_dept_alumni_page, DEPT_ALUMNI_PAGES, max_workers=14, label="DeptAlumni")
    with open(CACHE_DEPT_FILE, "w", encoding="utf-8") as f:
        json.dump(dept_records, f, ensure_ascii=False)
    print(f"  -> Saved {len(dept_records):,} departmental alumni records -> {CACHE_DEPT_FILE}")

    # Stage 2: Load all verified modern Core Stat faculty homepages from SQLite
    conn = sqlite3.connect(DB_PATH)
    fac_rows = conn.execute("""
        SELECT p.id, p.name,
               COALESCE(NULLIF(p.current_affiliation,''), p.school, '') AS eff_school,
               COALESCE(p.country, ''),
               COALESCE(NULLIF(p.primary_field,''), 'Statistics'),
               p.year,
               p.homepage
        FROM people p
        WHERE p.is_core_stat = 1
          AND p.homepage IS NOT NULL AND p.homepage != ''
          AND (p.student_count >= 1 OR (p.year IS NOT NULL AND p.year BETWEEN 1960 AND 2022) OR (p.year IS NULL AND p.current_affiliation != ''))
        ORDER BY p.student_count DESC, p.descendant_count DESC, p.year DESC
    """).fetchall()
    conn.close()

    # Deduplicate by homepage URL so shared department pages aren't crawled 50 times
    seen_hp = set()
    faculty_targets = []
    for row in fac_rows:
        hp = (row[6] or "").strip().rstrip("/")
        if not hp or hp in seen_hp:
            continue
        dom = urlparse(hp).netloc.lower()
        if not dom or any(sd in dom for sd in SKIP_DOMAINS) or dom == "github.com":
            continue
        seen_hp.add(hp)
        faculty_targets.append(row)

    print(f"\n[2/2] Crawling {len(faculty_targets):,} unique verified Core Stat faculty websites + CV PDFs...")
    fac_records = run_bounded_batch(crawl_faculty_homepage_worker, faculty_targets, max_workers=28, label="FacultyWeb")
    with open(CACHE_FAC_FILE, "w", encoding="utf-8") as f:
        json.dump(fac_records, f, ensure_ascii=False)
    print(f"  -> Saved {len(fac_records):,} faculty-website PhD advisee records -> {CACHE_FAC_FILE}")

    # Ingest into SQLite
    all_recs = dept_records + fac_records
    print(f"\nIngesting {len(all_recs):,} Wave 3+4 records into data/stat_genealogy.db...")
    ing = StatLinkIngestor()
    for rec in all_recs:
        ing.ingest_record(rec, allow_without_advisor=False)
    ing.commit_and_close()

    print(f"\nWave 3 + 4 DONE in {time.time() - t0:.1f}s:")
    for k, v in ing.stats.items():
        print(f"  {k:20s}: {v:,}")


if __name__ == "__main__":
    main()
