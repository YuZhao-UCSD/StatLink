#!/usr/bin/env python3
"""
Multi-Source Scholar Profile Link Enrichment Pipeline (`scripts/enrich_scholar_external_links.py`).

Enriches `data/stat_genealogy.db` (`people` table) with verified direct profile links for the
right-hand Scholar Dossier (`Verified Source` section):
  1. Personal Website (`homepage`)
  2. Wikipedia (`wikipedia_url` — English Wikipedia preferred, major native-language fallback)
  3. Google Scholar (`google_scholar_url` — `https://scholar.google.com/citations?user=...`)
  4. LinkedIn (`linkedin_url` — `https://www.linkedin.com/in/...`)
  5. ORCID (`orcid` — `0000-000X-XXXX-XXXX`)
  6. Wikidata (`wikidata_qid` — `Q...`)

Data Sources Harvested:
  - Stage 1: Reclassification & cleanup of existing `people.homepage` URLs (separating Wikipedia,
             Google Scholar, LinkedIn, and ORCID links previously stored in `homepage`).
  - Stage 2: Full partitioned Wikidata SPARQL harvest across all 262k+ `wdt:P549` (MGP ID) items,
             31k+ `wdt:P2381` (AcademicTree ID) items, and existing `wikidata_qid` records.
  - Stage 3: Wikidata `wdt:P496` (ORCID) & institution-verified (`P69` / `P108`) researcher bridge
             for Google Scholar (`P1960`), LinkedIn (`P6634`), Official Website (`P856`), & Wikipedia.
  - Stage 4: Polite 8-worker ORCID Public API (`/v3.0/{orcid}/person`) `researcher-urls` harvester
             for Core Stat / Biostat / Prob scholars (`is_core_stat = 1`).
"""

import concurrent.futures
import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
from collections import defaultdict
from urllib.parse import quote

import requests

sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "scripts"))
from build_database import canonicalize_school

DB_PATH = os.path.join(BASE_DIR, "data", "stat_genealogy.db")
CACHE_DIR = os.path.join(BASE_DIR, "data_raw")
os.makedirs(CACHE_DIR, exist_ok=True)
WD_CACHE_PATH = os.path.join(CACHE_DIR, "wikidata_external_links_cache.json")
ORCID_URLS_CACHE_PATH = os.path.join(CACHE_DIR, "orcid_researcher_urls_cache.json")

WD_SPARQL_URL = "https://query.wikidata.org/sparql"
WD_HEADERS = {
    "User-Agent": "StatLinkAcademicGenealogy/2.0 (https://github.com/YuZhao-UCSD/StatLineage; mailto:research@statlink.org)",
    "Accept": "application/sparql-results+json",
}
ORCID_HEADERS = {
    "User-Agent": "StatLinkAcademicGenealogy/2.0 (https://github.com/YuZhao-UCSD/StatLineage; mailto:research@statlink.org)",
    "Accept": "application/json",
}

ORCID_RE = re.compile(r"\b(\d{4}-\d{4}-\d{4}-\d{3}[\dX])\b", re.I)
GSCHOLAR_USER_RE = re.compile(r"[?&]user=([A-Za-z0-9_\-]{8,20})")
LINKEDIN_SLUG_RE = re.compile(r"linkedin\.com/(?:in|pub)/([^/?#\s]+)", re.I)

SKIP_HOMEPAGE_DOMAINS = (
    "mathgenealogy.org",
    "academictree.org",
    "wikidata.org",
    "viaf.org",
    "id.loc.gov",
    "d-nb.info",
    "isni.org",
    "zbmath.org",
    "mathscinet",
    "ams.org/mathscinet",
    "researchgate.net",
    "scopus.com",
    "webofscience.com",
    "publons.com",
    "twitter.com",
    "x.com",
    "facebook.com",
    "instagram.com",
    "youtube.com",
    "dblp.org",
    "semanticscholar.org",
    "MacTutor",
    "archive.org/details",
)


def fold_norm_name(text: str) -> str:
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
    s = "".join(ch for ch in nfkd if not unicodedata.combining(ch)).lower()
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"[^a-z0-9\s\-]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def normalize_gscholar(val: str) -> str:
    if not val:
        return ""
    val = val.strip()
    if val.startswith("http"):
        m = GSCHOLAR_USER_RE.search(val)
        if m:
            return f"https://scholar.google.com/citations?user={m.group(1)}"
        if "scholar.google." in val.lower() and "/citations" in val.lower():
            return val
        return ""
    if re.fullmatch(r"[A-Za-z0-9_\-]{8,20}", val):
        return f"https://scholar.google.com/citations?user={val}"
    return ""


def normalize_linkedin(val: str) -> str:
    if not val:
        return ""
    val = val.strip()
    if val.startswith("http"):
        m = LINKEDIN_SLUG_RE.search(val)
        if m:
            slug = m.group(1).strip("/")
            return f"https://www.linkedin.com/in/{slug}"
        return ""
    slug = val.strip("/")
    if slug and " " not in slug and "/" not in slug:
        return f"https://www.linkedin.com/in/{slug}"
    return ""


def classify_arbitrary_url(url: str):
    """Returns (kind, normalized_url) where kind in {'wikipedia','gscholar','linkedin','orcid','homepage','skip'}."""
    if not url:
        return ("skip", "")
    u = url.strip()
    if not u.startswith(("http://", "https://")):
        return ("skip", "")
    ul = u.lower()
    if "wikipedia.org/wiki/" in ul:
        return ("wikipedia", u)
    if "scholar.google." in ul:
        gs = normalize_gscholar(u)
        return ("gscholar", gs) if gs else ("skip", "")
    if "linkedin.com/in/" in ul or "linkedin.com/pub/" in ul:
        li = normalize_linkedin(u)
        return ("linkedin", li) if li else ("skip", "")
    if "orcid.org/" in ul:
        m = ORCID_RE.search(u)
        return ("orcid", m.group(1).upper()) if m else ("skip", "")
    if any(dom.lower() in ul for dom in SKIP_HOMEPAGE_DOMAINS):
        return ("skip", "")
    return ("homepage", u)


def run_sparql(session: requests.Session, query: str, retries: int = 4) -> list:
    for attempt in range(retries):
        try:
            resp = session.post(
                WD_SPARQL_URL,
                data={"query": query, "format": "json"},
                headers=WD_HEADERS,
                timeout=55,
            )
            if resp.status_code == 200:
                return resp.json().get("results", {}).get("bindings", [])
            if resp.status_code in (429, 500, 502, 503, 504):
                wait = 3.5 * (attempt + 1)
                print(f"  [SPARQL HTTP {resp.status_code}] retrying in {wait:.1f}s...")
                time.sleep(wait)
                continue
            print(f"  [SPARQL HTTP {resp.status_code}] {resp.text[:160]}")
            return []
        except Exception as exc:
            wait = 3.5 * (attempt + 1)
            print(f"  [SPARQL err: {exc}] retrying in {wait:.1f}s...")
            time.sleep(wait)
    return []


def ensure_columns(conn: sqlite3.Connection):
    existing = {r[1] for r in conn.execute("PRAGMA table_info(people)").fetchall()}
    for col in ("wikipedia_url", "google_scholar_url", "linkedin_url"):
        if col not in existing:
            print(f"Adding column `people.{col}`...")
            conn.execute(f"ALTER TABLE people ADD COLUMN {col} TEXT DEFAULT ''")
    conn.commit()


def harvest_wikidata_all() -> dict:
    if os.path.exists(WD_CACHE_PATH) and os.path.getsize(WD_CACHE_PATH) > 100_000:
        print(f"Loading cached Wikidata enrichment from {WD_CACHE_PATH}...")
        with open(WD_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)

    sess = requests.Session()
    by_qid = {}

    def ingest_bindings(bindings):
        for b in bindings:
            item_uri = b.get("item", {}).get("value", "")
            if "/entity/Q" not in item_uri:
                continue
            qid = item_uri.rsplit("/", 1)[-1]
            rec = by_qid.setdefault(qid, {"qid": qid})
            for k in ("mgp", "aft", "orcid", "gscholar", "linkedin", "website", "enwiki", "anywiki", "label", "instLabel"):
                v = b.get(k, {}).get("value", "").strip()
                if v and not rec.get(k):
                    rec[k] = v

    # 1. All P549 (MGP ID) items partitioned into 20 two-digit suffix buckets (00..99 in groups of 5 -> ~13k rows each)
    print("Stage 2A: Harvesting all ~263,000 Wikidata MGP (P549) scholar profiles in 20 fast slices...")
    suffix_groups = [[f"{i:02d}" for i in range(g, g + 5)] for g in range(0, 100, 5)]
    for idx, grp in enumerate(suffix_groups, 1):
        # Also include 1-digit MGP IDs (1..9) in slice 1!
        conds = " || ".join(f'STRENDS(?mgp, "{s}")' for s in grp)
        if idx == 1:
            conds = f"({conds} || STRLEN(?mgp) = 1)"
        q = f"""
        SELECT ?item ?mgp ?aft ?orcid ?gscholar ?linkedin ?website ?enwiki WHERE {{
          ?item wdt:P549 ?mgp .
          FILTER({conds})
          OPTIONAL {{ ?item wdt:P2381 ?aft . }}
          OPTIONAL {{ ?item wdt:P496 ?orcid . }}
          OPTIONAL {{ ?item wdt:P1960 ?gscholar . }}
          OPTIONAL {{ ?item wdt:P6634 ?linkedin . }}
          OPTIONAL {{ ?item wdt:P856 ?website . }}
          OPTIONAL {{
            ?enwiki schema:about ?item ;
                    schema:isPartOf <https://en.wikipedia.org/> .
          }}
        }}
        """
        rows = run_sparql(sess, q)
        ingest_bindings(rows)
        print(f"  P549 slice {idx:02d}/20: {len(rows):,} bindings (unique QIDs so far: {len(by_qid):,})")
        time.sleep(0.45)

    # 2. All P2381 (AcademicTree ID) items in 4 slices (~8k rows each)
    print("Stage 2B: Harvesting all ~31,600 Wikidata AcademicTree (P2381) scholar profiles...")
    aft_groups = [["0", "1", "2"], ["3", "4"], ["5", "6"], ["7", "8", "9"]]
    for idx, grp in enumerate(aft_groups, 1):
        conds = " || ".join(f'STRENDS(?aft, "{s}")' for s in grp)
        q = f"""
        SELECT ?item ?mgp ?aft ?orcid ?gscholar ?linkedin ?website ?enwiki WHERE {{
          ?item wdt:P2381 ?aft .
          FILTER({conds})
          OPTIONAL {{ ?item wdt:P549 ?mgp . }}
          OPTIONAL {{ ?item wdt:P496 ?orcid . }}
          OPTIONAL {{ ?item wdt:P1960 ?gscholar . }}
          OPTIONAL {{ ?item wdt:P6634 ?linkedin . }}
          OPTIONAL {{ ?item wdt:P856 ?website . }}
          OPTIONAL {{
            ?enwiki schema:about ?item ;
                    schema:isPartOf <https://en.wikipedia.org/> .
          }}
        }}
        """
        rows = run_sparql(sess, q)
        ingest_bindings(rows)
        print(f"  P2381 slice {idx}/4: {len(rows):,} bindings (unique QIDs so far: {len(by_qid):,})")
        time.sleep(0.45)

    # 3. Fallback major-language Wikipedia sitelinks (de, fr, ru, es, it, nl, pl, sv, zh, ja) for P549 items without enwiki
    print("Stage 2C: Harvesting major non-English Wikipedia sitelinks for historical European/Asian scholars...")
    for lang in ("de", "fr", "ru"):
        q = f"""
        SELECT ?item ?anywiki WHERE {{
          ?item wdt:P549 ?mgp .
          ?anywiki schema:about ?item ;
                   schema:isPartOf <https://{lang}.wikipedia.org/> .
          FILTER NOT EXISTS {{
            ?enw schema:about ?item ;
                 schema:isPartOf <https://en.wikipedia.org/> .
          }}
        }}
        """
        rows = run_sparql(sess, q)
        ingest_bindings(rows)
        print(f"  {lang}.wikipedia fallback: {len(rows):,} sitelinks")
        time.sleep(0.45)

    # 4. Statisticians / Mathematicians / Researchers with Google Scholar (P1960) or LinkedIn (P6634) + University/ORCID
    print("Stage 2D: Harvesting Wikidata Google Scholar (P1960) & LinkedIn (P6634) profiles for academic researchers...")
    for prop, varname in [("P1960", "gscholar"), ("P6634", "linkedin")]:
        for idx, grp in enumerate([["0", "1", "2", "3", "4"], ["5", "6", "7", "8", "9"]], 1):
            conds = " || ".join(f'STRENDS(STR(?item), "{d}")' for d in grp)
            q = f"""
            SELECT ?item ?label ?orcid ?{varname} ?website ?enwiki ?instLabel WHERE {{
              ?item wdt:{prop} ?{varname} ;
                    wdt:P31 wd:Q5 .
              FILTER({conds})
              FILTER NOT EXISTS {{ ?item wdt:P549 [] . }}
              FILTER NOT EXISTS {{ ?item wdt:P2381 [] . }}
              OPTIONAL {{ ?item wdt:P496 ?orcid . }}
              OPTIONAL {{ ?item wdt:P856 ?website . }}
              OPTIONAL {{
                ?enwiki schema:about ?item ;
                        schema:isPartOf <https://en.wikipedia.org/> .
              }}
              OPTIONAL {{
                {{ ?item wdt:P69 ?inst . }} UNION {{ ?item wdt:P108 ?inst . }}
                ?inst rdfs:label ?instLabel .
                FILTER(LANG(?instLabel) = "en")
              }}
              ?item rdfs:label ?label .
              FILTER(LANG(?label) = "en")
            }} LIMIT 45000
            """
            rows = run_sparql(sess, q)
            # Aggregate multiple instLabels per QID
            for b in rows:
                item_uri = b.get("item", {}).get("value", "")
                if "/entity/Q" not in item_uri:
                    continue
                qid = item_uri.rsplit("/", 1)[-1]
                rec = by_qid.setdefault(qid, {"qid": qid})
                for k in ("orcid", "gscholar", "linkedin", "website", "enwiki", "label"):
                    v = b.get(k, {}).get("value", "").strip()
                    if v and not rec.get(k):
                        rec[k] = v
                inst_v = b.get("instLabel", {}).get("value", "").strip()
                if inst_v:
                    insts = rec.setdefault("insts", [])
                    if inst_v not in insts:
                        insts.append(inst_v)
            print(f"  {prop} researcher slice {idx}/2: {len(rows):,} bindings (total QIDs: {len(by_qid):,})")
            time.sleep(0.5)

    with open(WD_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(by_qid, f, ensure_ascii=False)
    print(f"Saved {len(by_qid):,} enriched Wikidata scholar records to {WD_CACHE_PATH}")
    return by_qid


def harvest_orcid_researcher_urls(target_orcids: list) -> dict:
    cached = {}
    if os.path.exists(ORCID_URLS_CACHE_PATH):
        try:
            with open(ORCID_URLS_CACHE_PATH, "r", encoding="utf-8") as f:
                cached = json.load(f)
        except Exception:
            cached = {}

    todo = [o for o in target_orcids if o not in cached]
    print(f"Stage 4: Checking ORCID `/person` researcher-urls for {len(todo):,} Core Stat ORCIDs ({len(cached):,} cached)...")
    if not todo:
        return cached

    def fetch_one(orc: str):
        url = f"https://pub.orcid.org/v3.0/{orc}/person"
        for attempt in range(3):
            try:
                r = requests.get(url, headers=ORCID_HEADERS, timeout=10)
                if r.status_code == 200:
                    data = r.json()
                    r_urls = (data.get("researcher-urls") or {}).get("researcher-url") or []
                    extracted = []
                    for item in r_urls:
                        u_val = ((item.get("url") or {}).get("value") or "").strip()
                        u_name = (item.get("url-name") or "").strip()
                        if u_val:
                            extracted.append({"name": u_name, "url": u_val})
                    return orc, extracted
                if r.status_code == 429:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                return orc, []
            except Exception:
                time.sleep(1.0 * (attempt + 1))
        return orc, []

    done_cnt = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for orc, urls in pool.map(fetch_one, todo):
            cached[orc] = urls
            done_cnt += 1
            if done_cnt % 500 == 0:
                print(f"  ORCID `/person` progress: {done_cnt:,}/{len(todo):,}")

    with open(ORCID_URLS_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cached, f, ensure_ascii=False)
    return cached


def main():
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    ensure_columns(conn)

    print("Loading people & school indexes from SQLite...")
    people = {}
    mgp_to_id = {}
    aft_to_id = {}
    orcid_to_id = {}
    qid_to_id = {}
    name_school_to_ids = defaultdict(list)

    adv_schools_by_pid = defaultdict(set)
    for r in conn.execute("SELECT person_id, school FROM advisor_schools"):
        if r[1]:
            adv_schools_by_pid[r[0]].add(r[1])

    for r in conn.execute(
        "SELECT id, name, norm_name, school, current_affiliation, is_core_stat, "
        "mgp_id, aft_pid, wikidata_qid, orcid, homepage, wikipedia_url, google_scholar_url, linkedin_url "
        "FROM people"
    ):
        uid = r["id"]
        rec = {
            "id": uid,
            "name": r["name"],
            "norm_name": r["norm_name"] or fold_norm_name(r["name"]),
            "school": r["school"] or "",
            "affil": r["current_affiliation"] or "",
            "is_core_stat": r["is_core_stat"],
            "mgp_id": r["mgp_id"],
            "aft_pid": r["aft_pid"],
            "wikidata_qid": (r["wikidata_qid"] or "").strip(),
            "orcid": (r["orcid"] or "").strip(),
            "homepage": (r["homepage"] or "").strip(),
            "wikipedia_url": (r["wikipedia_url"] or "").strip(),
            "google_scholar_url": (r["google_scholar_url"] or "").strip(),
            "linkedin_url": (r["linkedin_url"] or "").strip(),
            "dirty": False,
        }
        # Stage 1: Reclassify existing homepage URLs cleanly
        if rec["homepage"]:
            kind, clean_u = classify_arbitrary_url(rec["homepage"])
            if kind == "wikipedia":
                if not rec["wikipedia_url"]:
                    rec["wikipedia_url"] = clean_u
                rec["homepage"] = ""
                rec["dirty"] = True
            elif kind == "gscholar":
                if not rec["google_scholar_url"]:
                    rec["google_scholar_url"] = clean_u
                rec["homepage"] = ""
                rec["dirty"] = True
            elif kind == "linkedin":
                if not rec["linkedin_url"]:
                    rec["linkedin_url"] = clean_u
                rec["homepage"] = ""
                rec["dirty"] = True
            elif kind == "orcid":
                if not rec["orcid"]:
                    rec["orcid"] = clean_u
                rec["homepage"] = ""
                rec["dirty"] = True
            elif kind == "skip":
                rec["homepage"] = ""
                rec["dirty"] = True

        people[uid] = rec
        if rec["mgp_id"] is not None:
            mgp_to_id[str(rec["mgp_id"])] = uid
        if rec["aft_pid"] is not None:
            aft_to_id[str(rec["aft_pid"])] = uid
        if rec["orcid"]:
            orcid_to_id[rec["orcid"].upper()] = uid
        if rec["wikidata_qid"]:
            qid_to_id[rec["wikidata_qid"]] = uid

        nn = rec["norm_name"]
        if len(nn.split()) >= 2:
            sch_set = {s for s in (rec["school"], rec["affil"]) if s} | adv_schools_by_pid.get(uid, set())
            for sc in sch_set:
                name_school_to_ids[(nn, sc)].append(uid)

    stage1_dirty = sum(1 for r in people.values() if r["dirty"])
    print(f"Stage 1 reclassified/cleaned {stage1_dirty:,} legacy homepage entries.")

    # Stage 2 & 3: Harvest Wikidata & apply matches
    by_qid = harvest_wikidata_all()

    # Also query any pre-existing wikidata_qid in `people` that wasn't returned yet
    missing_qids = [q for q in qid_to_id if q not in by_qid and q.startswith("Q")]
    if missing_qids:
        print(f"Fetching {len(missing_qids):,} pre-existing QIDs not yet in cache...")
        sess = requests.Session()
        for start in range(0, len(missing_qids), 350):
            batch = missing_qids[start : start + 350]
            vals = " ".join(f"wd:{q}" for q in batch)
            q_str = f"""
            SELECT ?item ?orcid ?gscholar ?linkedin ?website ?enwiki WHERE {{
              VALUES ?item {{ {vals} }}
              OPTIONAL {{ ?item wdt:P496 ?orcid . }}
              OPTIONAL {{ ?item wdt:P1960 ?gscholar . }}
              OPTIONAL {{ ?item wdt:P6634 ?linkedin . }}
              OPTIONAL {{ ?item wdt:P856 ?website . }}
              OPTIONAL {{
                ?enwiki schema:about ?item ;
                        schema:isPartOf <https://en.wikipedia.org/> .
              }}
            }}
            """
            for b in run_sparql(sess, q_str):
                item_uri = b.get("item", {}).get("value", "")
                if "/entity/Q" in item_uri:
                    qid = item_uri.rsplit("/", 1)[-1]
                    rec = by_qid.setdefault(qid, {"qid": qid})
                    for k in ("orcid", "gscholar", "linkedin", "website", "enwiki"):
                        v = b.get(k, {}).get("value", "").strip()
                        if v and not rec.get(k):
                            rec[k] = v
            time.sleep(0.35)
        with open(WD_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(by_qid, f, ensure_ascii=False)

    matched_wd = 0
    for qid, w in by_qid.items():
        uid = None
        if w.get("mgp") and w["mgp"] in mgp_to_id:
            uid = mgp_to_id[w["mgp"]]
        elif w.get("aft") and w["aft"] in aft_to_id:
            uid = aft_to_id[w["aft"]]
        elif qid in qid_to_id:
            uid = qid_to_id[qid]
        elif w.get("orcid") and w["orcid"].upper() in orcid_to_id:
            uid = orcid_to_id[w["orcid"].upper()]
        elif w.get("label") and w.get("insts"):
            nn = fold_norm_name(w["label"])
            if len(nn.split()) >= 2:
                cand_uids = set()
                for raw_inst in w["insts"]:
                    canon_inst = canonicalize_school(raw_inst)
                    for u_cand in name_school_to_ids.get((nn, canon_inst), ()):
                        cand_uids.add(u_cand)
                if len(cand_uids) == 1:
                    uid = next(iter(cand_uids))

        if uid is None:
            continue

        matched_wd += 1
        p = people[uid]
        if not p["wikidata_qid"]:
            p["wikidata_qid"] = qid
            p["dirty"] = True

        if w.get("orcid") and not p["orcid"]:
            m = ORCID_RE.search(w["orcid"])
            if m:
                p["orcid"] = m.group(1).upper()
                orcid_to_id[p["orcid"]] = uid
                p["dirty"] = True

        wiki = w.get("enwiki") or w.get("anywiki") or ""
        if wiki and (not p["wikipedia_url"] or ("en.wikipedia.org" in wiki and "en.wikipedia.org" not in p["wikipedia_url"])):
            p["wikipedia_url"] = wiki
            p["dirty"] = True

        gs = normalize_gscholar(w.get("gscholar", ""))
        if gs and not p["google_scholar_url"]:
            p["google_scholar_url"] = gs
            p["dirty"] = True

        li = normalize_linkedin(w.get("linkedin", ""))
        if li and not p["linkedin_url"]:
            p["linkedin_url"] = li
            p["dirty"] = True

        if w.get("website"):
            kind, clean_u = classify_arbitrary_url(w["website"])
            if kind == "homepage" and not p["homepage"]:
                p["homepage"] = clean_u
                p["dirty"] = True
            elif kind == "gscholar" and not p["google_scholar_url"]:
                p["google_scholar_url"] = clean_u
                p["dirty"] = True
            elif kind == "linkedin" and not p["linkedin_url"]:
                p["linkedin_url"] = clean_u
                p["dirty"] = True
            elif kind == "wikipedia" and not p["wikipedia_url"]:
                p["wikipedia_url"] = clean_u
                p["dirty"] = True

    print(f"Matched {matched_wd:,} Wikidata profiles onto local scholars.")

    # Stage 4: Enrich Core Stat ORCID profiles via ORCID `/person` researcher-urls
    core_orcids = sorted({
        p["orcid"]
        for p in people.values()
        if p["is_core_stat"] == 1
        and p["orcid"]
        and (not p["homepage"] or not p["google_scholar_url"] or not p["linkedin_url"])
    })
    orcid_urls_map = harvest_orcid_researcher_urls(core_orcids)

    orcid_enriched = 0
    for p in people.values():
        orc = p["orcid"]
        if not orc or orc not in orcid_urls_map:
            continue
        changed = False
        for item in orcid_urls_map[orc]:
            kind, clean_u = classify_arbitrary_url(item.get("url", ""))
            if kind == "gscholar" and not p["google_scholar_url"]:
                p["google_scholar_url"] = clean_u
                changed = True
            elif kind == "linkedin" and not p["linkedin_url"]:
                p["linkedin_url"] = clean_u
                changed = True
            elif kind == "wikipedia" and not p["wikipedia_url"]:
                p["wikipedia_url"] = clean_u
                changed = True
            elif kind == "homepage" and not p["homepage"]:
                p["homepage"] = clean_u
                changed = True
        if changed:
            p["dirty"] = True
            orcid_enriched += 1

    print(f"ORCID `/person` researcher-urls enriched {orcid_enriched:,} additional scholars.")

    # Write updates back to SQLite in a single fast transaction
    dirty_rows = [
        (
            p["wikidata_qid"],
            p["orcid"],
            p["homepage"],
            p["wikipedia_url"],
            p["google_scholar_url"],
            p["linkedin_url"],
            p["id"],
        )
        for p in people.values()
        if p["dirty"]
    ]
    print(f"Writing {len(dirty_rows):,} updated scholar records to SQLite...")
    conn.executemany(
        """
        UPDATE people
        SET wikidata_qid = ?,
            orcid = ?,
            homepage = ?,
            wikipedia_url = ?,
            google_scholar_url = ?,
            linkedin_url = ?
        WHERE id = ?
        """,
        dirty_rows,
    )
    conn.commit()

    # Print final summary counts
    print("\n=== ENRICHED VERIFIED LINK TOTALS ===")
    for col, label in [
        ("wikidata_qid != ''", "Wikidata"),
        ("orcid != ''", "ORCID"),
        ("homepage != ''", "Personal Website"),
        ("wikipedia_url != ''", "Wikipedia"),
        ("google_scholar_url != ''", "Google Scholar"),
        ("linkedin_url != ''", "LinkedIn"),
    ]:
        tot = conn.execute(f"SELECT COUNT(*) FROM people WHERE {col}").fetchone()[0]
        core = conn.execute(f"SELECT COUNT(*) FROM people WHERE is_core_stat = 1 AND {col}").fetchone()[0]
        print(f"  {label:20s}: {tot:8,d} total  |  {core:6,d} Core Stat/Biostat/Prob")

    conn.close()
    print(f"Completed in {time.time() - t0:.1f}s!")


if __name__ == "__main__":
    main()
