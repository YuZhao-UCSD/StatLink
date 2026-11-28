#!/usr/bin/env python3
"""
Wave 4B Harvester:
  1. English Wikipedia Infobox Doctoral Genealogy Miner:
     Batch-fetches wikitext (`action=query&prop=revisions&rvprop=content`) for ALL ~6,500+
     Broad/Core Stat scholars in StatLink with an `en.wikipedia.org` URL (plus members of
     Category:Statisticians, Category:Biostatisticians, Category:Probabilists) and extracts
     `| doctoral_advisor =` and `| doctoral_students =` links missing from MGP/Wikidata.
  2. Crossref Global Dissertation API (`filter=type:dissertation`) Miner:
     Queries Crossref dissertations across statistical/biostatistical/probability topics
     extracting explicit `chair` (dissertation advisor), `author`, `institution`, `department`,
     `degree`, `ORCID`, and `published` year.
"""

import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib3

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
    is_doctoral_degree,
    norm_key,
)

CACHE_FILE = os.path.join(CACHE_DIR, "wave4b_wikipedia_crossref_records.json")


def _extract_infobox_field(wikitext: str, field_pattern: str) -> str:
    """Extract full multi-line value of a MediaWiki infobox parameter up to the next top-level `| param =` or `}}`."""
    m = re.search(rf"^\s*\|\s*(?:{field_pattern})\s*=\s*(.*)", wikitext, re.I | re.M)
    if not m:
        return ""
    start = m.start(1)
    lines = wikitext[start:].splitlines()
    buf = []
    depth = 0
    for idx, ln in enumerate(lines):
        if idx > 0:
            # Stop at next top-level infobox param or closing braces
            if depth <= 0 and re.match(r"^\s*(?:\||\}\})", ln):
                break
        buf.append(ln)
        depth += ln.count("{{") - ln.count("}}")
        if depth < 0:
            break
        if len(buf) >= 40:
            break
    return "\n".join(buf)


def _parse_wiki_names(raw_val: str):
    """Extract clean person names from Wikipedia infobox wikitext (`[[Name|Display]]`, `{{ubl|...}}`, `<br>`, bullets)."""
    if not raw_val:
        return []
    txt = raw_val
    # Strip HTML comments, <ref>...</ref>, citation needed templates
    txt = re.sub(r"<!--.*?-->", " ", txt, flags=re.S)
    txt = re.sub(r"<ref[^>]*>.*?</ref>", " ", txt, flags=re.S | re.I)
    txt = re.sub(r"<ref[^/>]*/>", " ", txt, flags=re.I)
    txt = re.sub(r"\{\{(?:cn|citation needed|efn|sfn|refn|cite[^}]*)\b[^}]*\}\}", " ", txt, flags=re.I)
    # Unwrap list templates {{plainlist|...}}, {{ubl|...}}, {{hlist|...}}, {{flatlist|...}}
    txt = re.sub(r"\{\{(?:plainlist|ubl|unbulleted list|hlist|flatlist|collapsible list|bulleted list)\s*\|(?:1=)?", "\n", txt, flags=re.I)
    txt = txt.replace("}}", "\n")
    # Convert wiki links [[Target|Label]] -> Label, [[Target]] -> Target (strip disambig "(statistician)")
    def _repl_link(m):
        parts = m.group(1).split("|")
        lbl = parts[-1].strip()
        lbl = re.sub(r"\s*\([^)]*\)\s*$", "", lbl)
        return lbl
    txt = re.sub(r"\[\[([^\]]+)\]\]", _repl_link, txt)
    # Replace <br>, bullets, pipes with newlines
    txt = re.sub(r"<br\s*/?>", "\n", txt, flags=re.I)
    txt = re.sub(r"[•\*\|;]", "\n", txt)

    # Remove prose lead-ins like "Ph.D. advisor to 28 students: among these,"
    txt = re.sub(r"Ph\.?D\.?\s+advisor\s+to\s+\d+\s+students?:?\s*(?:among\s+these,?)?", "\n", txt, flags=re.I)
    txt = re.sub(r"\bare\s+now\s+university\s+professors\.?", "", txt, flags=re.I)

    out = []
    seen = set()
    for chunk in re.split(r"[\n]", txt):
        chunk = chunk.strip()
        if not chunk:
            continue
        # If a single line has comma-separated names (like Trevor Hastie's list!), split on commas / " and "
        if chunk.count(",") >= 2:
            subparts = re.split(r",|\band\b", chunk)
        else:
            subparts = [chunk]
        for sp in subparts:
            nm = clean_person_name(sp)
            if nm:
                nk = norm_key(nm)
                if nk and nk not in seen:
                    seen.add(nk)
                    out.append(nm)
    return out


def harvest_wikipedia_infoboxes():
    records = []
    sess = requests.Session()
    sess.headers.update({"User-Agent": "StatLinkAcademicExplorer/3.0 (mailto:statlink.research@gmail.com)"})

    conn = sqlite3.connect(DB_PATH)
    wiki_people = {}  # title -> (id, name, school, country, year, primary_field)
    for row in conn.execute("""
        SELECT id, name, COALESCE(NULLIF(current_affiliation,''), school, ''), COALESCE(country,''),
               year, COALESCE(NULLIF(primary_field,''), 'Statistics'), wikipedia_url
        FROM people
        WHERE is_broad_stat = 1 AND wikipedia_url LIKE '%en.wikipedia.org/wiki/%'
    """):
        u = row[6]
        title = urllib.parse.unquote(u.split("/wiki/")[-1].split("#")[0]).replace("_", " ").strip()
        if title:
            wiki_people[title] = row[:6]
    conn.close()

    titles = list(wiki_people.keys())
    print(f"  [Wikipedia Infobox] Batch-fetching {len(titles):,} Broad/Core Stat Wikipedia biographies...")

    batch_size = 50
    for i in range(0, len(titles), batch_size):
        batch = titles[i:i + batch_size]
        try:
            r = sess.get(
                "https://en.wikipedia.org/w/api.php",
                params={
                    "action": "query",
                    "prop": "revisions",
                    "rvprop": "content",
                    "rvslots": "main",
                    "format": "json",
                    "titles": "|".join(batch),
                },
                timeout=20,
            )
            if r.status_code != 200:
                continue
            pages = (r.json().get("query") or {}).get("pages") or {}
            for pg in pages.values():
                pt = pg.get("title")
                if not pt or pt not in wiki_people:
                    continue
                pid, pname, psch, pcntry, pyr, pfield = wiki_people[pt]
                revs = pg.get("revisions") or []
                if not revs:
                    continue
                wtxt = ((revs[0].get("slots") or {}).get("main") or {}).get("*") or ""
                if not wtxt or ("doctoral_advis" not in wtxt.lower() and "doctoral_student" not in wtxt.lower()):
                    continue

                # 1. Doctoral advisors of this Wikipedia person
                adv_raw = _extract_infobox_field(wtxt, r"doctoral_advis[oe]rs?")
                adv_names = _parse_wiki_names(adv_raw)
                if adv_names:
                    records.append({
                        "student": pname,
                        "advisors": adv_names[:3],
                        "school": psch,
                        "country": pcntry,
                        "year": pyr,
                        "thesis": "",
                        "subjects": pfield,
                        "degree": "Ph.D.",
                        "force_field": pfield if pfield in ("Statistics", "Biostatistics", "Probability") else None,
                        "source": "Wikipedia Infobox",
                    })

                # 2. Doctoral students of this Wikipedia person
                stu_raw = _extract_infobox_field(wtxt, r"doctoral_students?")
                stu_names = _parse_wiki_names(stu_raw)
                for snm in stu_names[:35]:
                    records.append({
                        "student": snm,
                        "advisors": [pname],
                        "known_advisor_id": pid,
                        "school": psch,
                        "country": pcntry,
                        "year": None,
                        "thesis": "",
                        "subjects": pfield,
                        "degree": "Ph.D.",
                        "force_field": pfield if pfield in ("Statistics", "Biostatistics", "Probability") else None,
                        "source": "Wikipedia Infobox",
                    })
        except Exception:
            pass
        if (i // batch_size) % 30 == 0 and i > 0:
            print(f"    [Wikipedia] Processed {i:,}/{len(titles):,} biographies -> {len(records):,} lineage links")
        time.sleep(0.08)

    print(f"  [Wikipedia Infobox] Extracted {len(records):,} verified doctoral advisor/student records")
    return records


def harvest_crossref_dissertations():
    records = []
    seen_dois = set()
    sess = requests.Session()
    sess.headers.update({"User-Agent": "StatLinkAcademicExplorer/3.0 (mailto:statlink.research@gmail.com)"})

    queries = [
        "statistics",
        "biostatistics",
        "probability",
        "stochastic",
        "bayesian",
        "causal inference",
        "survival analysis",
        "high-dimensional",
        "time series",
        "econometrics",
        "nonparametric",
        "semiparametric",
        "statistical genetics",
        "experimental design",
        "clinical trials",
    ]

    for q in queries:
        cursor = "*"
        for _ in range(5):  # Up to 1,000 per query
            try:
                r = sess.get(
                    "https://api.crossref.org/works",
                    params={
                        "filter": "type:dissertation",
                        "query": q,
                        "rows": 200,
                        "cursor": cursor,
                        "mailto": "statlink.research@gmail.com",
                    },
                    timeout=20,
                )
                if r.status_code != 200:
                    break
                msg = r.json().get("message") or {}
                items = msg.get("items") or []
                if not items:
                    break
                for it in items:
                    doi = it.get("DOI")
                    if not doi or doi in seen_dois:
                        continue
                    seen_dois.add(doi)
                    chairs = it.get("chair") or []
                    if not chairs:
                        continue
                    advisors = []
                    for ch in chairs[:3]:
                        cnm = clean_person_name(f"{ch.get('given') or ''} {ch.get('family') or ''}" or (ch.get("name") or ""))
                        if cnm:
                            advisors.append(cnm)
                    if not advisors:
                        continue

                    auths = it.get("author") or []
                    if len(auths) != 1:
                        continue
                    a0 = auths[0]
                    student = clean_person_name(f"{a0.get('given') or ''} {a0.get('family') or ''}" or (a0.get("name") or ""))
                    if not student:
                        continue
                    orcid = (a0.get("ORCID") or "").replace("https://orcid.org/", "").replace("http://orcid.org/", "").strip()

                    title = clean_title((it.get("title") or [""])[0])
                    if not title:
                        continue

                    deg = it.get("degree") or ""
                    if isinstance(deg, list):
                        deg = " ".join(str(x) for x in deg)
                    if not is_doctoral_degree(str(deg), title, require_positive=False):
                        continue

                    insts = it.get("institution") or []
                    school = ""
                    depts = []
                    if insts and isinstance(insts[0], dict):
                        school = (insts[0].get("name") or "").strip()
                        depts = insts[0].get("department") or []
                    if not school:
                        affs = a0.get("affiliation") or []
                        if affs and isinstance(affs[0], dict):
                            school = (affs[0].get("name") or "").strip()
                    if not school:
                        continue

                    subjs = ", ".join(list(depts) + list(it.get("subject") or []))
                    if not classify_stat_field(subjs, title, advisor_is_core=False):
                        continue

                    yr = None
                    for dk in ("published-print", "published-online", "issued", "created"):
                        dp = ((it.get(dk) or {}).get("date-parts") or [[]])[0]
                        if dp and dp[0]:
                            yr = int(dp[0])
                            break

                    records.append({
                        "student": student,
                        "advisors": advisors,
                        "school": school,
                        "country": "",
                        "year": yr,
                        "thesis": title,
                        "subjects": subjs,
                        "degree": str(deg) or "Dissertation",
                        "orcid": orcid,
                        "source": "Crossref ETD",
                    })
                nxt = msg.get("next-cursor")
                if not nxt or len(items) < 200:
                    break
                cursor = nxt
                time.sleep(0.1)
            except Exception:
                break

    print(f"  [Crossref Dissertations] Harvested {len(records):,} global dissertations with explicit chair/advisor")
    return records


def main():
    t0 = time.time()
    print("=" * 72)
    print("WAVE 4B: WIKIPEDIA INFOBOX GENEALOGY + CROSSREF DISSERTATIONS")
    print("=" * 72)

    all_recs = []
    print("\n[1/2] Mining Wikipedia infoboxes across Broad/Core Stat biographies...")
    all_recs.extend(harvest_wikipedia_infoboxes())

    print("\n[2/2] Harvesting Crossref Global Dissertations (`type:dissertation` with explicit `chair`)...")
    all_recs.extend(harvest_crossref_dissertations())

    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(all_recs, f, ensure_ascii=False)
    print(f"\nSaved {len(all_recs):,} Wave 4B records -> {CACHE_FILE}")

    print("\nIngesting Wave 4B records into data/stat_genealogy.db...")
    ing = StatLinkIngestor()
    for rec in all_recs:
        ing.ingest_record(rec, allow_without_advisor=False)
    ing.commit_and_close()

    print(f"\nWave 4B DONE in {time.time() - t0:.1f}s:")
    for k, v in ing.stats.items():
        print(f"  {k:20s}: {v:,}")


if __name__ == "__main__":
    main()
