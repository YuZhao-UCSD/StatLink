#!/usr/bin/env python3
"""
Wave 1E Harvester: Newly discovered DSpace 7 API subdomains & Canadian Scholaris Network:
  - University of Cambridge (`api.repository.cam.ac.uk`)
  - University of Maryland (`api.drum.lib.umd.edu`)
  - McGill University (`mcgill.scholaris.ca`)
  - Western University (`uwo.scholaris.ca`)
  - Université de Montréal (`umontreal.scholaris.ca`)
  - Carleton University, University of Windsor, University of Regina, Brock University,
    Université de Sherbrooke (`*.scholaris.ca`)
"""

import concurrent.futures
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse

sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "scripts"))

from etd_ingest_common import (
    CACHE_DIR,
    StatLinkIngestor,
    clean_person_name,
    clean_title,
    classify_stat_field,
    is_doctoral_degree,
    split_advisor_field,
)

CACHE_FILE = os.path.join(CACHE_DIR, "wave1e_cambridge_umd_scholaris_records.json")
CHROME_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"

REPOS = [
    ("University of Maryland", "United States", "https://api.drum.lib.umd.edu/server/api/discover/search/objects"),
    ("University of Cambridge", "United Kingdom", "https://api.repository.cam.ac.uk/server/api/discover/search/objects"),
    ("McGill University", "Canada", "https://mcgill.scholaris.ca/server/api/discover/search/objects"),
    ("Western University", "Canada", "https://uwo.scholaris.ca/server/api/discover/search/objects"),
    ("Université de Montréal", "Canada", "https://umontreal.scholaris.ca/server/api/discover/search/objects"),
    ("Carleton University", "Canada", "https://carleton.scholaris.ca/server/api/discover/search/objects"),
    ("University of Windsor", "Canada", "https://uwindsor.scholaris.ca/server/api/discover/search/objects"),
    ("University of Regina", "Canada", "https://uregina.scholaris.ca/server/api/discover/search/objects"),
    ("Brock University", "Canada", "https://brocku.scholaris.ca/server/api/discover/search/objects"),
    ("Université de Sherbrooke", "Canada", "https://usherbrooke.scholaris.ca/server/api/discover/search/objects"),
]

ADVISOR_KEY_RE = re.compile(r"(?:advisor|adviser|supervisor|chair|cochair|co-chair|promoter|director|mentor)", re.I)
ADVISOR_KEY_EXCLUDE_RE = re.compile(r"(?:email|orcid|affiliation|committeemember|committee_member|reader|examiner|opponent|reviewer|referee|latestfordiscovery)", re.I)
DEGREE_KEY_RE = re.compile(r"(?:degree|level|type|genre)", re.I)
DEPT_SUBJ_KEY_RE = re.compile(r"(?:subject|discipline|department|school|program|major|faculty|unit|center)", re.I)

QUERIES = [
    "Statistics",
    "Biostatistics",
    "Statistique",
    "Probability stochastic",
    "Bayesian causal inference",
    "Survival analysis longitudinal",
    "High-dimensional regression",
]


def harvest_one(item):
    school, country, api_url = item
    records = []
    seen = set()
    t0 = time.time()
    for q in QUERIES:
        if (time.time() - t0) > 95:
            break
        if q == "Statistique" and school not in ("Université de Montréal", "Université de Sherbrooke", "McGill University"):
            continue
        max_pages = 15 if q in ("Statistics", "Biostatistics", "Statistique") else 6
        for page in range(0, max_pages):
            if (time.time() - t0) > 95:
                break
            try:
                u = f"{api_url}?{urllib.parse.urlencode({'query': q, 'dsoType': 'item', 'size': 100, 'page': page})}"
                out = subprocess.check_output(
                    ["curl.exe", "-sS", "-g", "-m", "15", "-L", "--ssl-no-revoke", "-A", CHROME_UA, "-H", "Accept: application/json", u],
                    text=True, encoding="utf-8", errors="ignore", timeout=18,
                )
                if not out or "searchResult" not in out:
                    break
                data = json.loads(out)
                sr = (data.get("_embedded") or {}).get("searchResult") or {}
                objs = (sr.get("_embedded") or {}).get("objects") or []
                if not objs:
                    break
                for obj in objs:
                    idx_obj = (obj.get("_embedded") or {}).get("indexableObject") or {}
                    uuid = idx_obj.get("uuid") or idx_obj.get("id")
                    if not uuid or uuid in seen:
                        continue
                    seen.add(uuid)
                    meta = idx_obj.get("metadata") or {}

                    advisors = []
                    for k, vals in meta.items():
                        if ADVISOR_KEY_RE.search(k) and not ADVISOR_KEY_EXCLUDE_RE.search(k):
                            for v in vals or []:
                                val = (v.get("value") or "").strip()
                                if val and not re.match(r"^[0-9a-f\-]{20,}$", val, re.I):
                                    advisors.extend(split_advisor_field(val))
                    if not advisors:
                        continue

                    auth_raw = meta.get("dc.contributor.author") or meta.get("dc.creator") or []
                    if not auth_raw or len(auth_raw) > 1:
                        continue
                    student = clean_person_name(auth_raw[0].get("value") or "")
                    if not student:
                        continue

                    deg_vals = []
                    for k, vals in meta.items():
                        if DEGREE_KEY_RE.search(k):
                            for v in vals or []:
                                if v.get("value"):
                                    deg_vals.append(str(v["value"]))
                    deg_str = " | ".join(deg_vals)
                    title_raw = meta.get("dc.title") or []
                    title = clean_title(title_raw[0].get("value") if title_raw else "")
                    if not title or not is_doctoral_degree(deg_str, title, require_positive=False):
                        continue

                    yr = None
                    for dk in ("dc.date.issued", "dc.date.created", "dc.date.submitted", "dc.date.awarded", "dc.date"):
                        dvals = meta.get(dk) or []
                        if dvals:
                            m = re.search(r"\b(19\d{2}|20[0-2]\d)\b", str(dvals[0].get("value") or ""))
                            if m:
                                yr = int(m.group(1))
                                break

                    subj_parts = []
                    for k, vals in meta.items():
                        if DEPT_SUBJ_KEY_RE.search(k):
                            for v in vals or []:
                                if v.get("value"):
                                    subj_parts.append(str(v["value"]))
                    subjs = ", ".join(subj_parts)
                    if not classify_stat_field(subjs, title, advisor_is_core=False):
                        continue

                    records.append({
                        "student": student,
                        "advisors": advisors[:3],
                        "school": school,
                        "country": country,
                        "year": yr,
                        "thesis": title,
                        "subjects": subjs,
                        "degree": deg_str,
                        "source": "University ETD",
                    })
                tot_pages = (sr.get("page") or {}).get("totalPages") or 1
                if page + 1 >= tot_pages:
                    break
            except Exception:
                break
    print(f"  [Wave1E-DS7] {school:36s}: {len(records):4d} doctoral stat/biostat/prob ETDs")
    return records


def main():
    t0 = time.time()
    print("=" * 72)
    print("WAVE 1E: UMD DRUM + CAMBRIDGE APOLLO + CANADIAN SCHOLARIS NETWORK")
    print("=" * 72)
    all_recs = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        for res in pool.map(harvest_one, REPOS):
            all_recs.extend(res)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(all_recs, f, ensure_ascii=False)
    print(f"\nSaved {len(all_recs):,} Wave 1E records -> {CACHE_FILE}")

    ing = StatLinkIngestor()
    for r in all_recs:
        ing.ingest_record(r, allow_without_advisor=False)
    ing.commit_and_close()
    print(f"\nWave 1E DONE in {time.time() - t0:.1f}s:")
    for k, v in ing.stats.items():
        print(f"  {k:20s}: {v:,}")


if __name__ == "__main__":
    main()
