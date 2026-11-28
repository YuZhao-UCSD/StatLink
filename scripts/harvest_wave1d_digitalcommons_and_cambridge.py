#!/usr/bin/env python3
"""
Wave 1D Harvester:
  1. Polite Low-Concurrency (`max_workers=2`, `curl.exe --ssl-no-revoke`) bepress DigitalCommons `qdc`
     Harvester across 20 US universities (UConn, Purdue e-Pubs, WashU, Clemson, South Carolina, LSU,
     Kentucky, Wayne State, Arkansas, Case Western, Nebraska, BGSU, Stony Brook, Rhode Island,
     Marquette, Memphis, Denver, UTHealth, VCU, USF, UAB)
  2. University of Cambridge Apollo OAI-PMH (`repository.cam.ac.uk/oai/request`)
  3. Quota-Safe OpenAlex Indexed Dissertations (`2020-2026`, `$0.0001`/call) with strict same-university
     verified-faculty Crossref/OpenAlex co-author advisor bridging.
"""

import concurrent.futures
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib3
import xml.etree.ElementTree as ET
from collections import defaultdict

import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "scripts"))

from etd_ingest_common import (
    CACHE_DIR,
    DB_PATH,
    HEADERS,
    StatLinkIngestor,
    clean_person_name,
    clean_title,
    classify_stat_field,
    first_last_sig,
    is_doctoral_degree,
    split_advisor_field,
)

CACHE_FILE = os.path.join(CACHE_DIR, "wave1d_dc_cambridge_openalex_records.json")
CHROME_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"

DIGITALCOMMONS_REPOS = [
    ("University of Connecticut", "United States", "https://digitalcommons.lib.uconn.edu/do/oai/", ["publication:dissertations"], 35),
    ("Purdue University", "United States", "https://docs.lib.purdue.edu/do/oai/", ["publication:open_access_dissertations"], 35),
    ("Washington University in St. Louis", "United States", "https://openscholarship.wustl.edu/do/oai/", ["publication:art_sci_etds", "publication:med_etds"], 30),
    ("University of South Carolina", "United States", "https://scholarcommons.sc.edu/do/oai/", ["publication:etd"], 30),
    ("Clemson University", "United States", "https://tigerprints.clemson.edu/do/oai/", ["publication:all_dissertations"], 30),
    ("Louisiana State University", "United States", "https://repository.lsu.edu/do/oai/", ["publication:gradschool_dissertations"], 30),
    ("University of Kentucky", "United States", "https://uknowledge.uky.edu/do/oai/", ["publication:statistics_etds", "publication:cph_etds"], 25),
    ("University of Nebraska-Lincoln", "United States", "https://digitalcommons.unl.edu/do/oai/", ["publication:statisticsdiss", "publication:dissertations"], 25),
    ("Wayne State University", "United States", "https://digitalcommons.wayne.edu/do/oai/", ["publication:oa_dissertations"], 25),
    ("Bowling Green State University", "United States", "https://scholarworks.bgsu.edu/do/oai/", ["publication:math_diss"], 20),
    ("University of Arkansas", "United States", "https://scholarworks.uark.edu/do/oai/", ["publication:etd"], 25),
    ("University of Rhode Island", "United States", "https://digitalcommons.uri.edu/do/oai/", ["publication:oa_diss"], 20),
    ("Marquette University", "United States", "https://epublications.marquette.edu/do/oai/", ["publication:dissertations_mu"], 20),
    ("University of Memphis", "United States", "https://digitalcommons.memphis.edu/do/oai/", ["publication:etd"], 25),
    ("University of Denver", "United States", "https://digitalcommons.du.edu/do/oai/", ["publication:etd"], 20),
    ("University of Texas Health Science Center at Houston", "United States", "https://digitalcommons.library.tmc.edu/do/oai/", ["publication:uthsph_dissertations"], 25),
    ("Virginia Commonwealth University", "United States", "https://scholarscompass.vcu.edu/do/oai/", ["publication:etd"], 25),
    ("University of South Florida", "United States", "https://digitalcommons.usf.edu/do/oai/", ["publication:etd"], 25),
    ("University of Alabama at Birmingham", "United States", "https://digitalcommons.library.uab.edu/do/oai/", ["publication:etd-collection"], 25),
]


def _local_tag(el):
    return el.tag.split("}")[-1] if "}" in el.tag else el.tag


def curl_get(url, timeout=18):
    try:
        return subprocess.check_output(
            ["curl.exe", "-sS", "-g", "-m", str(timeout), "-L", "--ssl-no-revoke", "-A", CHROME_UA, url],
            text=True, encoding="utf-8", errors="ignore", timeout=timeout + 3,
        )
    except Exception:
        return ""


def harvest_one_dc_polite(cfg):
    school, country, base_url, sets, max_pages = cfg
    records = []
    seen_ids = set()
    pages_used = 0
    t_start = time.time()

    for sp in sets:
        if pages_used >= max_pages or (time.time() - t_start) > 110:
            break
        token = None
        first_call = True
        while pages_used < max_pages and (time.time() - t_start) <= 110:
            if first_call:
                qs = {"verb": "ListRecords", "metadataPrefix": "qdc", "set": sp}
                if "stat" not in sp.lower() and "sph" not in sp.lower() and "math" not in sp.lower():
                    qs["from"] = "2012-01-01"
                first_call = False
            else:
                qs = {"verb": "ListRecords", "resumptionToken": token}
            full_url = f"{base_url}?{urllib.parse.urlencode(qs)}"
            xml_txt = curl_get(full_url, timeout=18)
            pages_used += 1
            time.sleep(0.45)
            if not xml_txt or "<record" not in xml_txt:
                break
            try:
                root = ET.fromstring(xml_txt)
            except Exception:
                break

            for rec in root.iter():
                if _local_tag(rec) != "record":
                    continue
                hdr = meta = None
                for ch in rec:
                    lt = _local_tag(ch)
                    if lt == "header":
                        hdr = ch
                    elif lt == "metadata":
                        meta = ch
                if hdr is not None and hdr.attrib.get("status") == "deleted":
                    continue
                if meta is None:
                    continue
                rid = ""
                if hdr is not None:
                    for ch in hdr:
                        if _local_tag(ch) == "identifier" and ch.text:
                            rid = ch.text.strip()
                if rid and rid in seen_ids:
                    continue
                if rid:
                    seen_ids.add(rid)

                authors = []
                explicit_advs = []
                generic_contribs = []
                titles = []
                dates = []
                subjs = []
                degrees = []

                for el in meta.iter():
                    lt = _local_tag(el).lower()
                    txt = (el.text or "").strip()
                    if not txt:
                        continue
                    if lt in ("creator", "author"):
                        authors.append(txt)
                    elif any(w in lt for w in ("advisor", "supervisor", "chair")) and "committee" not in lt:
                        explicit_advs.extend(split_advisor_field(txt))
                    elif lt == "contributor":
                        generic_contribs.append(txt)
                    elif lt == "title":
                        titles.append(txt)
                    elif lt.startswith("date") or lt in ("issued", "created"):
                        dates.append(txt)
                    elif any(w in lt for w in ("discipline", "department", "subject")):
                        subjs.append(txt)
                    elif any(w in lt for w in ("degree.name", "degree.level", "type", "genre")):
                        degrees.append(txt)

                advisors = explicit_advs if explicit_advs else (split_advisor_field(generic_contribs[0]) if generic_contribs else [])
                if len(authors) != 1 or not advisors or not titles:
                    continue
                student = clean_person_name(authors[0])
                if not student:
                    continue
                title = clean_title(titles[0])
                deg_str = " | ".join(degrees)
                if not is_doctoral_degree(deg_str, title, require_positive=True):
                    continue
                subj_str = ", ".join(subjs)
                if not classify_stat_field(subj_str, title, advisor_is_core=False):
                    continue
                yr = None
                for dt in dates:
                    ym = re.search(r"\b(19\d{2}|20[0-2]\d)\b", dt)
                    if ym:
                        yr = int(ym.group(1))
                        break

                records.append({
                    "student": student,
                    "advisors": advisors[:2],
                    "school": school,
                    "country": country,
                    "year": yr,
                    "thesis": title,
                    "subjects": subj_str,
                    "degree": deg_str,
                    "source": "DigitalCommons ETD",
                })

            rt = None
            for el in root.iter():
                if _local_tag(el) == "resumptionToken" and el.text and el.text.strip():
                    rt = el.text.strip()
                    break
            if not rt:
                break
            token = rt

    print(f"  [Polite-DC] {school:46s}: {len(records):4d} doctoral stat/biostat/prob ETDs (pages={pages_used})")
    return records


def harvest_cambridge_oai():
    records = []
    seen = set()
    base = "https://www.repository.cam.ac.uk/oai/request"
    # Probe sets for Pure Mathematics and Mathematical Statistics (DPMMS / StatLab)
    sets_xml = curl_get(f"{base}?verb=ListSets", timeout=15)
    target_sets = []
    if sets_xml and "<set>" in sets_xml:
        try:
            root = ET.fromstring(sets_xml)
            for s_el in root.iter():
                if _local_tag(s_el) != "set":
                    continue
                sp = nm = ""
                for ch in s_el:
                    lt = _local_tag(ch)
                    if lt == "setSpec" and ch.text:
                        sp = ch.text.strip()
                    elif lt == "setName" and ch.text:
                        nm = ch.text.strip()
                if sp and re.search(r"(statist|biostat|mathemat|probabilit|epidemiol|mrc\s+biostat)", nm, re.I):
                    target_sets.append(sp)
        except Exception:
            pass
    if not target_sets:
        target_sets = [None]

    pages = 0
    for sp in target_sets[:6]:
        token = None
        first_call = True
        while pages < 30:
            if first_call:
                qs = {"verb": "ListRecords", "metadataPrefix": "dim"}
                if sp:
                    qs["set"] = sp
                else:
                    qs["from"] = "2014-01-01"
                first_call = False
            else:
                qs = {"verb": "ListRecords", "resumptionToken": token}
            xml_txt = curl_get(f"{base}?{urllib.parse.urlencode(qs)}", timeout=20)
            pages += 1
            if not xml_txt or "<record" not in xml_txt:
                break
            try:
                root = ET.fromstring(xml_txt)
            except Exception:
                break
            for rec in root.iter():
                if _local_tag(rec) != "record":
                    continue
                meta = None
                for ch in rec:
                    if _local_tag(ch) == "metadata":
                        meta = ch
                if meta is None:
                    continue
                authors, advisors, titles, dates, subjs, degrees = [], [], [], [], [], []
                for el in meta.iter():
                    if _local_tag(el).lower() != "field":
                        continue
                    txt = (el.text or "").strip()
                    if not txt:
                        continue
                    elem = (el.attrib.get("element") or "").lower()
                    qual = (el.attrib.get("qualifier") or "").lower()
                    key = f"{elem}.{qual}" if qual else elem
                    if key in ("contributor.author", "creator"):
                        authors.append(txt)
                    elif any(w in key for w in ("advisor", "supervisor", "chair")) and "email" not in key:
                        advisors.extend(split_advisor_field(txt))
                    elif key == "title":
                        titles.append(txt)
                    elif elem == "date" and qual in ("issued", "awarded", "created", ""):
                        dates.append(txt)
                    elif elem in ("subject", "department") or qual in ("department", "discipline"):
                        subjs.append(txt)
                    elif "degree" in key or elem == "type":
                        degrees.append(txt)

                if len(authors) != 1 or not advisors or not titles:
                    continue
                stu = clean_person_name(authors[0])
                title = clean_title(titles[0])
                deg_str = " | ".join(degrees)
                if not stu or not title or not is_doctoral_degree(deg_str, title, require_positive=True):
                    continue
                subj_str = ", ".join(subjs)
                if not classify_stat_field(subj_str, title, advisor_is_core=False):
                    continue
                yr = None
                for dt in dates:
                    ym = re.search(r"\b(19\d{2}|20[0-2]\d)\b", dt)
                    if ym:
                        yr = int(ym.group(1))
                        break
                k = (stu.lower(), yr or 0)
                if k in seen:
                    continue
                seen.add(k)
                records.append({
                    "student": stu,
                    "advisors": advisors[:2],
                    "school": "University of Cambridge",
                    "country": "United Kingdom",
                    "year": yr,
                    "thesis": title,
                    "subjects": subj_str,
                    "degree": deg_str,
                    "source": "OAI-PMH ETD",
                })
            rt = None
            for el in root.iter():
                if _local_tag(el) == "resumptionToken" and el.text and el.text.strip():
                    rt = el.text.strip()
                    break
            if not rt:
                break
            token = rt

    print(f"  [Cambridge Apollo OAI] Harvested {len(records):4d} doctoral stat/biostat/prob ETDs (sets={len(target_sets)}, pages={pages})")
    return records


def harvest_openalex_2020_2026():
    """Quota-safe OpenAlex Indexed Concepts ($0.0001/call) dissertation sweep for 2020-2026."""
    records = []
    sess = requests.Session()
    sess.headers.update({"User-Agent": "StatLinkAcademicExplorer/3.0 (mailto:statlink.research@gmail.com)"})

    # Load faculty lookup by school to link OpenAlex dissertation authors via their OpenAlex co-authors
    conn = sqlite3.connect(DB_PATH)
    fac_by_school = defaultdict(lambda: defaultdict(list))  # school -> last_name -> [(id, name, first, year)]
    for pid, name, yr, sch in conn.execute("""
        SELECT DISTINCT p.id, p.name, p.year, s.school
        FROM (
            SELECT person_id AS pid, school FROM advisor_schools
            UNION
            SELECT id AS pid, current_affiliation AS school FROM people WHERE current_affiliation != '' AND is_broad_stat = 1
        ) s
        JOIN people p ON p.id = s.pid
        WHERE p.is_broad_stat = 1 AND s.school != ''
    """):
        f, l = first_last_sig(name)
        if f and l:
            fac_by_school[sch][l].append((pid, name, f, yr))
    conn.close()

    ing_tmp = StatLinkIngestor()
    candidates = []
    seen_oa = set()

    for yr in range(2020, 2027):
        cursor = "*"
        for _ in range(4):  # Up to 800 dissertations/year ($0.0004/year!)
            try:
                r = sess.get(
                    "https://api.openalex.org/works",
                    params={
                        "filter": f"type:dissertation,publication_year:{yr},concepts.id:C105795698|C62649853|C149782125",
                        "per-page": 200,
                        "cursor": cursor,
                        "mailto": "statlink.research@gmail.com",
                    },
                    timeout=18,
                )
                if r.status_code != 200:
                    break
                data = r.json()
                results = data.get("results") or []
                if not results:
                    break
                for w in results:
                    wid = w.get("id")
                    if not wid or wid in seen_oa:
                        continue
                    seen_oa.add(wid)
                    auths = w.get("authorships") or []
                    if len(auths) != 1:
                        continue
                    a0 = auths[0]
                    au = a0.get("author") or {}
                    au_id = (au.get("id") or "").split("/")[-1]
                    stu_nm = clean_person_name(au.get("display_name") or "")
                    if not stu_nm or not au_id:
                        continue
                    insts = a0.get("institutions") or []
                    raw_sch = insts[0].get("display_name") if insts else ""
                    if not raw_sch:
                        continue
                    sch, cntry = ing_tmp.canonical_school(raw_sch, (insts[0].get("country_code") or ""))
                    if sch not in fac_by_school:
                        continue
                    title = clean_title(w.get("title") or "")
                    if not title:
                        continue
                    concepts = ", ".join(c.get("display_name") or "" for c in (w.get("concepts") or [])[:6])
                    fld = classify_stat_field(concepts, title, advisor_is_core=False)
                    if not fld:
                        continue
                    orc = (au.get("orcid") or "").replace("https://orcid.org/", "").strip()
                    candidates.append((au_id, stu_nm, sch, cntry, yr, title, fld, orc))
                nxt = (data.get("meta") or {}).get("next_cursor")
                if not nxt or len(results) < 200:
                    break
                cursor = nxt
            except Exception:
                break
    ing_tmp.conn.close()

    print(f"  [OpenAlex 2020-2026] Found {len(candidates):,} candidate dissertations at tracked universities; bridging co-author advisors...")
    # Batch query author co-authored articles (10 authors per call via `author.id:A1|A2|...` -> super quota-efficient!)
    for i in range(0, min(len(candidates), 1200), 10):
        batch = candidates[i:i + 10]
        by_auid = {b[0]: b for b in batch}
        id_str = "|".join(by_auid.keys())
        try:
            r = sess.get(
                "https://api.openalex.org/works",
                params={
                    "filter": f"author.id:{id_str},type:article|preprint",
                    "per-page": 100,
                    "mailto": "statlink.research@gmail.com",
                },
                timeout=18,
            )
            if r.status_code != 200:
                continue
            co_scores = defaultdict(lambda: defaultdict(int))  # auid -> fac_id -> weight
            co_names = {}
            for w in (r.json().get("results") or []):
                w_yr = w.get("publication_year")
                auths = w.get("authorships") or []
                if len(auths) < 2 or len(auths) > 8:
                    continue
                present_students = [
                    (a.get("author") or {}).get("id", "").split("/")[-1]
                    for a in auths
                    if ((a.get("author") or {}).get("id", "").split("/")[-1]) in by_auid
                ]
                for sauid in present_students:
                    _, stu_nm, sch, _, pyr, _, _, _ = by_auid[sauid]
                    if w_yr and not (pyr - 3 <= w_yr <= pyr + 2):
                        continue
                    stu_sig = first_last_sig(stu_nm)
                    for idx, a in enumerate(auths):
                        cnm = clean_person_name((a.get("author") or {}).get("display_name") or "")
                        if not cnm:
                            continue
                        csig = first_last_sig(cnm)
                        if not csig[0] or not csig[1] or csig == stu_sig:
                            continue
                        for fid, fname, ff, fyr in fac_by_school[sch].get(csig[1], ()):
                            if fyr and (pyr - fyr < 5 or pyr - fyr > 60):
                                continue
                            if csig[0] == ff or (len(csig[0]) == 1 and ff.startswith(csig[0])):
                                wt = 3 if idx == len(auths) - 1 else 1
                                co_scores[sauid][fid] += wt
                                co_names[fid] = fname
                                break
            for sauid, fscores in co_scores.items():
                best_fid, best_wt = max(fscores.items(), key=lambda x: x[1])
                if best_wt >= 3:
                    _, stu_nm, sch, cntry, pyr, title, fld, orc = by_auid[sauid]
                    records.append({
                        "student": stu_nm,
                        "advisors": [co_names[best_fid]],
                        "known_advisor_id": best_fid,
                        "school": sch,
                        "country": cntry,
                        "year": pyr,
                        "thesis": title,
                        "subjects": fld,
                        "degree": "Ph.D.",
                        "force_field": fld,
                        "orcid": orc,
                        "source": "OpenAlex Global ETD",
                    })
        except Exception:
            pass
        time.sleep(0.12)

    print(f"  [OpenAlex 2020-2026] Bridged {len(records):,} verified doctoral dissertations with advisors")
    return records


def main():
    t0 = time.time()
    print("=" * 72)
    print("WAVE 1D: POLITE DIGITALCOMMONS (19 UNIV) + CAMBRIDGE OAI + OPENALEX 2020-2026")
    print("=" * 72)

    all_records = []

    print("\n[1/3] Crawling 19 US DigitalCommons OAI-PMH repos politely (max_workers=2)...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        for res in pool.map(harvest_one_dc_polite, DIGITALCOMMONS_REPOS):
            all_records.extend(res)

    print("\n[2/3] Crawling University of Cambridge Apollo OAI-PMH...")
    all_records.extend(harvest_cambridge_oai())

    print("\n[3/3] Harvesting OpenAlex Indexed Dissertations (2020-2026, quota-safe)...")
    all_records.extend(harvest_openalex_2020_2026())

    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(all_records, f, ensure_ascii=False)
    print(f"\nSaved {len(all_records):,} Wave 1D doctoral records -> {CACHE_FILE}")

    print("\nIngesting Wave 1D records into data/stat_genealogy.db...")
    ing = StatLinkIngestor()
    for rec in all_records:
        ing.ingest_record(rec, allow_without_advisor=False)
    ing.commit_and_close()

    print(f"\nWave 1D DONE in {time.time() - t0:.1f}s:")
    for k, v in ing.stats.items():
        print(f"  {k:20s}: {v:,}")


if __name__ == "__main__":
    main()
