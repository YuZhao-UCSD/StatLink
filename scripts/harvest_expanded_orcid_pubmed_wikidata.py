#!/usr/bin/env python3
"""
Wave 2B Harvester:
  1. Fresh Wikidata SPARQL Doctoral Lineages (`wdt:P184` doctoral advisor + `wdt:P549` MGP + Statistics/Math/CS)
  2. Expanded 65-University ORCID v3.0 + Crossref Co-Author Bridge (targeting gap & international universities,
     strictly rate-limited `max_workers=6` with 429 backoff and doctoral-department verification)
  3. Expanded 65-University NIH PubMed Statistical Methodology Harvester (40+ core journals, 2014–2026)
"""

import concurrent.futures
import json
import os
import re
import sqlite3
import sys
import time
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
    first_last_sig,
    norm_key,
)

CACHE_WIKIDATA = os.path.join(CACHE_DIR, "wave2b_wikidata_records.json")
CACHE_ORCID = os.path.join(CACHE_DIR, "wave2b_orcid_records.json")
CACHE_PUBMED = os.path.join(CACHE_DIR, "wave2b_pubmed_records.json")

# 65 Gap + Flagship Global Stat/Biostat Universities for ORCID + PubMed Expansion
TARGET_UNIVERSITIES = [
    ("Cornell University", "United States", ["Cornell University", "Weill Cornell"]),
    ("University of Pittsburgh", "United States", ["University of Pittsburgh"]),
    ("Rutgers University", "United States", ["Rutgers University", "Rutgers, The State University"]),
    ("University of Southern California", "United States", ["University of Southern California"]),
    ("University of Texas at Austin", "United States", ["University of Texas at Austin"]),
    ("Johns Hopkins University", "United States", ["Johns Hopkins University"]),
    ("University of Iowa", "United States", ["University of Iowa"]),
    ("Carnegie Mellon University", "United States", ["Carnegie Mellon University"]),
    ("Texas A&M University", "United States", ["Texas A&M University"]),
    ("University of Maryland", "United States", ["University of Maryland"]),
    ("Georgia Institute of Technology", "United States", ["Georgia Institute of Technology"]),
    ("North Carolina State University", "United States", ["North Carolina State University"]),
    ("Michigan State University", "United States", ["Michigan State University"]),
    ("Purdue University", "United States", ["Purdue University"]),
    ("University of Illinois Urbana-Champaign", "United States", ["University of Illinois Urbana-Champaign", "University of Illinois at Urbana-Champaign"]),
    ("Ohio State University", "United States", ["Ohio State University"]),
    ("University of Rochester", "United States", ["University of Rochester"]),
    ("University of Chicago", "United States", ["University of Chicago"]),
    ("Columbia University", "United States", ["Columbia University"]),
    ("Harvard University", "United States", ["Harvard University", "Harvard T.H. Chan"]),
    ("Stanford University", "United States", ["Stanford University"]),
    ("Yale University", "United States", ["Yale University"]),
    ("Princeton University", "United States", ["Princeton University"]),
    ("New York University", "United States", ["New York University"]),
    ("University of Pennsylvania", "United States", ["University of Pennsylvania"]),
    ("Duke University", "United States", ["Duke University"]),
    ("University of Michigan", "United States", ["University of Michigan"]),
    ("University of Washington", "United States", ["University of Washington"]),
    ("University of North Carolina at Chapel Hill", "United States", ["University of North Carolina at Chapel Hill"]),
    ("University of Wisconsin-Madison", "United States", ["University of Wisconsin-Madison", "University of Wisconsin"]),
    ("University of Minnesota", "United States", ["University of Minnesota"]),
    ("Iowa State University", "United States", ["Iowa State University"]),
    ("Pennsylvania State University", "United States", ["Pennsylvania State University"]),
    ("Virginia Tech", "United States", ["Virginia Tech", "Virginia Polytechnic"]),
    ("Boston University", "United States", ["Boston University"]),
    ("Brown University", "United States", ["Brown University"]),
    ("Emory University", "United States", ["Emory University"]),
    ("Vanderbilt University", "United States", ["Vanderbilt University"]),
    ("Washington University in St. Louis", "United States", ["Washington University in St. Louis"]),
    ("Northwestern University", "United States", ["Northwestern University"]),
    ("Rice University", "United States", ["Rice University"]),
    ("Stony Brook University", "United States", ["Stony Brook University"]),
    ("University of Connecticut", "United States", ["University of Connecticut"]),
    ("Florida State University", "United States", ["Florida State University"]),
    ("University of Florida", "United States", ["University of Florida"]),
    ("Massachusetts Institute of Technology", "United States", ["Massachusetts Institute of Technology"]),
    ("University of Toronto", "Canada", ["University of Toronto"]),
    ("University of Waterloo", "Canada", ["University of Waterloo"]),
    ("University of British Columbia", "Canada", ["University of British Columbia"]),
    ("McGill University", "Canada", ["McGill University"]),
    ("University of Oxford", "United Kingdom", ["University of Oxford"]),
    ("University of Cambridge", "United Kingdom", ["University of Cambridge"]),
    ("Imperial College London", "United Kingdom", ["Imperial College London"]),
    ("London School of Economics", "United Kingdom", ["London School of Economics"]),
    ("University of Warwick", "United Kingdom", ["University of Warwick"]),
    ("University College London", "United Kingdom", ["University College London"]),
    ("University of Edinburgh", "United Kingdom", ["University of Edinburgh"]),
    ("ETH Zürich", "Switzerland", ["ETH Zurich", "ETH Zürich"]),
    ("École Polytechnique Fédérale de Lausanne", "Switzerland", ["EPFL", "Ecole Polytechnique Federale de Lausanne"]),
    ("Universiteit van Amsterdam", "Netherlands", ["University of Amsterdam", "Universiteit van Amsterdam"]),
    ("KU Leuven", "Belgium", ["KU Leuven"]),
    ("National University of Singapore", "Singapore", ["National University of Singapore"]),
    ("University of Hong Kong", "Hong Kong", ["University of Hong Kong"]),
    ("Australian National University", "Australia", ["Australian National University"]),
    ("University of Melbourne", "Australia", ["University of Melbourne"]),
]

CORE_STAT_JOURNALS = [
    "Biostatistics", "Biometrics", "J Am Stat Assoc", "Biometrika",
    "Ann Stat", "J R Stat Soc Series B Stat Methodol", "Stat Med",
    "J Comput Graph Stat", "Stat Sin", "Scand J Stat", "Bernoulli",
    "Ann Appl Stat", "Stat Sci", "Stat Methods Med Res", "Genet Epidemiol",
    "Lifetime Data Anal", "Comput Stat Data Anal", "J Multivar Anal",
    "Technometrics", "Bayesian Anal", "Ann Probab", "Electron J Stat",
    "J Causal Inference", "Epidemiology", "Am J Epidemiol", "Int J Biostat",
    "Pharm Stat", "Statistica Neerlandica", "Can J Stat", "Aust N Z J Stat",
    "J Stat Plan Inference", "J Time Ser Anal", "Spatial Stat", "Environmetrics",
    "J Mach Learn Res", "Bioinformatics", "BMC Bioinformatics", "PLoS Comput Biol",
]


# ---------------------------------------------------------------------------
# 1. Fresh Wikidata SPARQL P184 (Doctoral Advisor) Sweep for Stat/Biostat/Prob
# ---------------------------------------------------------------------------

def harvest_fresh_wikidata():
    records = []
    seen = set()
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": "StatLinkAcademicExplorer/3.0 (https://yuzhao-ucsd.github.io/StatLink/; mailto:statlink.research@gmail.com)",
        "Accept": "application/sparql-results+json",
    })

    # Query statisticians, biostatisticians, probabilists, econometricians, data scientists with P184 advisor
    occupations = [
        ("Q2732142", "Statistics"),        # statistician
        ("Q15978963", "Biostatistics"),    # biostatistician
        ("Q23832312", "Probability"),      # probabilist
        ("Q188094", "Statistics"),         # econometrician
        ("Q12483", "Statistics"),          # statistics (field of work P101)
        ("Q194152", "Biostatistics"),      # biostatistics (field of work P101)
        ("Q5862903", "Probability"),       # probability theory (field of work P101)
    ]

    for qid, field in occupations:
        prop = "wdt:P106" if qid in ("Q2732142", "Q15978963", "Q23832312", "Q188094") else "wdt:P101"
        sparql = f"""
        SELECT ?stu ?stuLabel ?advLabel ?schLabel ?orcid ?year WHERE {{
          ?stu {prop} wd:{qid} .
          ?stu wdt:P184 ?adv .
          OPTIONAL {{ ?stu wdt:P496 ?orcid . }}
          OPTIONAL {{
            ?stu p:P69 ?st .
            ?st ps:P69 ?sch .
            OPTIONAL {{ ?st pq:P582 ?end . BIND(YEAR(?end) AS ?year) }}
          }}
          SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en,fr,de". }}
        }} LIMIT 8000
        """
        try:
            r = sess.get("https://query.wikidata.org/sparql", params={"query": sparql}, timeout=55)
            if r.status_code != 200:
                continue
            rows = (r.json().get("results") or {}).get("bindings") or []
            for b in rows:
                stu = clean_person_name((b.get("stuLabel") or {}).get("value") or "")
                adv = clean_person_name((b.get("advLabel") or {}).get("value") or "")
                if not stu or not adv or stu.startswith("Q") or adv.startswith("Q"):
                    continue
                sch = (b.get("schLabel") or {}).get("value") or ""
                if sch.startswith("Q") and sch[1:].isdigit():
                    sch = ""
                yr_s = (b.get("year") or {}).get("value") or ""
                yr = int(yr_s) if yr_s.isdigit() and 1850 <= int(yr_s) <= 2026 else None
                orc = (b.get("orcid") or {}).get("value") or ""
                key = (norm_key(stu), norm_key(adv))
                if key in seen:
                    continue
                seen.add(key)
                records.append({
                    "student": stu,
                    "advisors": [adv],
                    "school": sch,
                    "country": "",
                    "year": yr,
                    "thesis": "",
                    "subjects": field,
                    "degree": "Ph.D.",
                    "force_field": field,
                    "orcid": orc,
                    "source": "Wikidata",
                })
            time.sleep(1.5)
        except Exception:
            pass
    print(f"  [Wikidata Fresh SPARQL] Harvested {len(records):,} verified stat/biostat/prob advisor-student links")
    return records


# ---------------------------------------------------------------------------
# 2. Expanded 65-University ORCID v3.0 + Crossref Co-Author Bridge
# ---------------------------------------------------------------------------

def build_faculty_lookup_by_school():
    conn = sqlite3.connect(DB_PATH)
    # school -> list of (id, name, norm_name, first, last, year, primary_field)
    fac_by_school = defaultdict(list)
    seen_pair = set()
    rows = conn.execute("""
        SELECT DISTINCT p.id, p.name, p.norm_name, p.year, p.primary_field, s.school
        FROM (
            SELECT person_id AS pid, school FROM advisor_schools
            UNION
            SELECT id AS pid, current_affiliation AS school FROM people WHERE current_affiliation != '' AND is_broad_stat = 1
        ) s
        JOIN people p ON p.id = s.pid
        WHERE p.is_broad_stat = 1 AND s.school IS NOT NULL AND s.school != ''
    """).fetchall()
    conn.close()
    for pid, name, nn, yr, pf, sch in rows:
        if (sch, pid) in seen_pair:
            continue
        seen_pair.add((sch, pid))
        f, l = first_last_sig(name)
        if f and l:
            fac_by_school[sch].append((pid, name, nn, f, l, yr, pf or "Statistics"))
    return fac_by_school


def harvest_expanded_orcid(fac_by_school):
    records = []
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": "StatLinkAcademicExplorer/3.0 (mailto:statlink.research@gmail.com)",
        "Accept": "application/json",
    })

    # Load existing ORCIDs already in DB to avoid redundant API calls on already-linked people
    conn = sqlite3.connect(DB_PATH)
    known_orcids_with_adv = set(
        r[0] for r in conn.execute(
            "SELECT orcid FROM people WHERE orcid IS NOT NULL AND orcid != '' AND advisor_count >= 1"
        )
    )
    conn.close()

    candidate_orcids = []  # (orcid, school, country)
    seen_orc = set()

    print(f"  [ORCID] Searching expanded-search across {len(TARGET_UNIVERSITIES)} universities...")
    for school, country, aliases in TARGET_UNIVERSITIES:
        if school not in fac_by_school or len(fac_by_school[school]) < 3:
            continue
        q_school = aliases[0]
        for dept_kw in ("Statistics", "Biostatistics"):
            q = f'affiliation-org-name:"{q_school}" AND ("{dept_kw}" OR "Statistical Science")'
            for attempt in range(3):
                try:
                    r = sess.get(
                        "https://pub.orcid.org/v3.0/expanded-search/",
                        params={"q": q, "rows": 120},
                        timeout=15,
                    )
                    if r.status_code == 429:
                        time.sleep(2.5 * (attempt + 1))
                        continue
                    if r.status_code == 200:
                        for it in (r.json().get("expanded-result") or []):
                            oid = it.get("orcid-id")
                            if oid and oid not in seen_orc and oid not in known_orcids_with_adv:
                                seen_orc.add(oid)
                                candidate_orcids.append((oid, school, country))
                    break
                except Exception:
                    time.sleep(1.0)
            time.sleep(0.18)

    print(f"  [ORCID] Inspecting {len(candidate_orcids):,} candidate ORCID profiles (rate-safe max_workers=6)...")

    def inspect_orcid(item):
        oid, school, country = item
        s_local = requests.Session()
        s_local.headers.update({
            "User-Agent": "StatLinkAcademicExplorer/3.0 (mailto:statlink.research@gmail.com)",
            "Accept": "application/json",
        })
        try:
            r = None
            for att in range(3):
                r = s_local.get(f"https://pub.orcid.org/v3.0/{oid}/educations", timeout=12)
                if r.status_code == 429:
                    time.sleep(2.0 * (att + 1))
                    continue
                break
            if not r or r.status_code != 200:
                return None
            edu_data = r.json()
            phd_year = None
            phd_field = None
            sch_low = school.lower()
            sch_tok = [w for w in re.findall(r"[a-z]{4,}", sch_low) if w not in ("university", "college", "institute", "state", "north", "south")]

            for grp in (edu_data.get("affiliation-group") or []):
                for sum_wrap in (grp.get("summaries") or []):
                    es = sum_wrap.get("education-summary") or {}
                    org = ((es.get("organization") or {}).get("name") or "").lower()
                    role = (es.get("role-title") or "")
                    dept = (es.get("department-name") or "")
                    if not re.search(r"\b(ph\.?\s*d\.?|doctor|d\.?\s*phil|sc\.?\s*d)\b", role, re.I):
                        continue
                    if not re.search(r"\b(statist\w*|biostat\w*|probabilit\w*|stochast\w*|statistical\s+science)\b", f"{dept} {role}", re.I):
                        continue
                    # Verify organization matches target school
                    if sch_tok and not any(t in org for t in sch_tok[:2]):
                        continue
                    ed = es.get("end-date") or {}
                    yval = (ed.get("year") or {}).get("value")
                    if yval and str(yval).isdigit():
                        yr_i = int(yval)
                        if 2008 <= yr_i <= 2026:
                            phd_year = yr_i
                            phd_field = "Biostatistics" if "biostat" in f"{dept} {role}".lower() else "Statistics"
                            break
                if phd_year:
                    break
            if not phd_year:
                return None

            # Get person name
            rp = s_local.get(f"https://pub.orcid.org/v3.0/{oid}/person", timeout=10)
            if rp.status_code != 200:
                return None
            pjson = rp.json()
            nm_obj = pjson.get("name") or {}
            gn = (nm_obj.get("given-names") or {}).get("value") or ""
            fn = (nm_obj.get("family-name") or {}).get("value") or ""
            stu_name = clean_person_name(f"{gn} {fn}")
            if not stu_name:
                return None

            # Crossref co-author bridge against verified faculty at `school` within [phd_year-3, phd_year+2]
            rc = s_local.get(
                "https://api.crossref.org/works",
                params={"filter": f"orcid:{oid}", "rows": 20},
                timeout=12,
            )
            if rc.status_code != 200:
                return None
            works = ((rc.json().get("message") or {}).get("items")) or []
            fac_pool = fac_by_school.get(school, [])
            fac_by_last = defaultdict(list)
            for f_item in fac_pool:
                fac_by_last[f_item[4]].append(f_item)

            coauthor_scores = defaultdict(int)
            coauthor_meta = {}
            stu_sig = first_last_sig(stu_name)
            for w in works:
                auths = w.get("author") or []
                if len(auths) < 2 or len(auths) > 8:
                    continue
                # Extract publication year
                w_yr = None
                for dk in ("published-print", "published-online", "issued", "created"):
                    dp = ((w.get(dk) or {}).get("date-parts") or [[]])[0]
                    if dp and dp[0]:
                        w_yr = int(dp[0])
                        break
                if not w_yr or not (phd_year - 3 <= w_yr <= phd_year + 2):
                    continue
                # Match senior co-authors against verified faculty at `school`
                for idx, a in enumerate(auths):
                    ag = (a.get("given") or "").strip()
                    af = (a.get("family") or "").strip()
                    cnm = clean_person_name(f"{ag} {af}")
                    if not cnm:
                        continue
                    csig = first_last_sig(cnm)
                    if not csig[0] or not csig[1] or csig == stu_sig:
                        continue
                    for f_item in fac_by_last.get(csig[1], ()):
                        fid, fname, fnn, ff, fl, fyr, fpf = f_item
                        if fyr and (phd_year - fyr < 5 or phd_year - fyr > 60):
                            continue
                        if csig[0] == ff or (len(csig[0]) == 1 and ff.startswith(csig[0])) or (len(ff) == 1 and csig[0].startswith(ff)):
                            # Last author gets extra weight
                            wt = 3 if idx == len(auths) - 1 else 1
                            coauthor_scores[fid] += wt
                            coauthor_meta[fid] = f_item
                            break

            if not coauthor_scores:
                return None
            best_fid, best_wt = max(coauthor_scores.items(), key=lambda x: x[1])
            if best_wt < 3:  # Require either >=1 senior/last-author paper or >=3 co-authored papers
                return None
            f_item = coauthor_meta[best_fid]
            return {
                "student": stu_name,
                "advisors": [f_item[1]],
                "known_advisor_id": best_fid,
                "school": school,
                "country": country,
                "year": phd_year,
                "thesis": "",
                "subjects": phd_field,
                "degree": "Ph.D.",
                "force_field": phd_field,
                "orcid": oid,
                "source": "ORCID + Crossref",
            }
        except Exception:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        done = 0
        for res in pool.map(inspect_orcid, candidate_orcids[:2500]):
            done += 1
            if res:
                records.append(res)
            if done % 500 == 0:
                print(f"    [ORCID] {done:,}/{min(len(candidate_orcids), 2500):,} verified -> {len(records):,} PhD advisor links")

    print(f"  [ORCID + Crossref Expanded] Harvested {len(records):,} verified PhD advisor-student links")
    return records


# ---------------------------------------------------------------------------
# 3. Expanded 65-University NIH PubMed Statistical Methodology Harvester (2014–2026)
# ---------------------------------------------------------------------------

def harvest_expanded_pubmed(fac_by_school):
    records = []
    sess = requests.Session()
    sess.headers.update(HEADERS)
    j_clause = " OR ".join(f'"{j}"[ta]' for j in CORE_STAT_JOURNALS)

    print(f"  [PubMed] Sweeping {len(TARGET_UNIVERSITIES)} universities across {len(CORE_STAT_JOURNALS)} core stat/biostat journals (2014-2026)...")
    for school, country, aliases in TARGET_UNIVERSITIES:
        fac_pool = fac_by_school.get(school, [])
        if len(fac_pool) < 3:
            continue
        fac_by_last = defaultdict(list)
        for f_item in fac_pool:
            fac_by_last[f_item[4]].append(f_item)

        aff_q = f'("{aliases[0]}"[Affiliation] AND ("Statistics"[Affiliation] OR "Biostatistics"[Affiliation]))'
        term = f"({aff_q}) AND ({j_clause}) AND 2014:2026[dp]"
        try:
            r = sess.get(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                params={"db": "pubmed", "term": term, "retmax": 500, "retmode": "json"},
                timeout=15,
            )
            if r.status_code != 200:
                continue
            pmids = ((r.json().get("esearchresult") or {}).get("idlist")) or []
            if not pmids:
                continue

            # Fetch in chunks of 150
            pair_counts = defaultdict(list)  # (stu_name, fac_id) -> list of (pub_year, field, title)
            fac_lookup = {f[0]: f for f in fac_pool}
            for i in range(0, len(pmids), 150):
                batch = pmids[i:i + 150]
                rf = sess.get(
                    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
                    params={"db": "pubmed", "id": ",".join(batch), "retmode": "xml"},
                    timeout=25,
                )
                if rf.status_code != 200:
                    continue
                try:
                    root = ET.fromstring(rf.text)
                except Exception:
                    continue
                for art in root.findall(".//PubmedArticle"):
                    auths = art.findall(".//AuthorList/Author")
                    if len(auths) < 2 or len(auths) > 7:
                        continue
                    yr_el = art.find(".//Journal/JournalIssue/PubDate/Year")
                    if yr_el is None or not yr_el.text or not yr_el.text.isdigit():
                        continue
                    pub_yr = int(yr_el.text)
                    if not (2014 <= pub_yr <= 2026):
                        continue

                    a0 = auths[0]
                    alast = auths[-1]
                    a0_aff = " ".join((x.text or "") for x in a0.findall(".//Affiliation")).lower()
                    if not re.search(r"\b(statist\w*|biostat\w*)\b", a0_aff):
                        continue
                    if not any(tok in a0_aff for tok in [w.lower() for w in aliases[0].split() if len(w) >= 5][:2]):
                        continue

                    fn0 = (a0.findtext("ForeName") or "").strip()
                    ln0 = (a0.findtext("LastName") or "").strip()
                    stu_nm = clean_person_name(f"{fn0} {ln0}")
                    if not stu_nm or len(fn0.replace(".", "").strip()) < 2:
                        continue

                    fnl = (alast.findtext("ForeName") or "").strip()
                    lnl = (alast.findtext("LastName") or "").strip()
                    adv_nm = clean_person_name(f"{fnl} {lnl}")
                    if not adv_nm:
                        continue
                    adv_sig = first_last_sig(adv_nm)
                    stu_sig = first_last_sig(stu_nm)
                    if not adv_sig[0] or not adv_sig[1] or adv_sig == stu_sig:
                        continue

                    # Skip if first author is already a known faculty member at this school
                    if any(f[3] == stu_sig[0] for f in fac_by_last.get(stu_sig[1], ())):
                        continue

                    # Match last author to verified senior faculty at `school`
                    matched_fac = None
                    for f_item in fac_by_last.get(adv_sig[1], ()):
                        fid, fname, fnn, ff, fl, fyr, fpf = f_item
                        if fyr and (pub_yr - fyr < 5 or pub_yr - fyr > 60):
                            continue
                        if adv_sig[0] == ff or (len(adv_sig[0]) == 1 and ff.startswith(adv_sig[0])):
                            matched_fac = f_item
                            break
                    if not matched_fac:
                        continue

                    fld = "Biostatistics" if "biostat" in a0_aff else "Statistics"
                    ptitle = clean_title(art.findtext(".//ArticleTitle") or "")
                    pair_counts[(stu_nm, matched_fac[0])].append((pub_yr, fld, ptitle))
                time.sleep(0.35)

            # Only emit student-advisor pairs with >= 2 first/last-author methodology papers (high precision, anti-postdoc!)
            sch_added = 0
            for (stu_nm, fid), hits in pair_counts.items():
                if len(hits) < 2:
                    continue
                yrs = [h[0] for h in hits]
                if max(yrs) - min(yrs) > 7:
                    continue
                f_item = fac_lookup[fid]
                fld = hits[0][1]
                records.append({
                    "student": stu_nm,
                    "advisors": [f_item[1]],
                    "known_advisor_id": fid,
                    "school": school,
                    "country": country,
                    "year": max(yrs),
                    "thesis": "",
                    "subjects": fld,
                    "degree": "Ph.D.",
                    "force_field": fld,
                    "source": "PubMed Methodology",
                })
                sch_added += 1
            if sch_added:
                print(f"    [PubMed] {school:42s}: {sch_added:3d} multi-paper methodology lineages")
            time.sleep(0.35)
        except Exception:
            pass

    print(f"  [PubMed Expanded] Harvested {len(records):,} verified multi-paper methodology lineages")
    return records


def main():
    t0 = time.time()
    print("=" * 72)
    print("WAVE 2B: FRESH WIKIDATA + 65-UNIV ORCID/CROSSREF + 65-UNIV PUBMED")
    print("=" * 72)

    fac_by_school = build_faculty_lookup_by_school()
    print(f"Loaded faculty lookup covering {len(fac_by_school):,} universities.")

    print("\n[1/3] Querying fresh Wikidata SPARQL doctoral lineages...")
    wd_recs = harvest_fresh_wikidata()
    with open(CACHE_WIKIDATA, "w", encoding="utf-8") as f:
        json.dump(wd_recs, f, ensure_ascii=False)

    print("\n[2/3] Harvesting 65-university ORCID v3.0 + Crossref lineages...")
    orc_recs = harvest_expanded_orcid(fac_by_school)
    with open(CACHE_ORCID, "w", encoding="utf-8") as f:
        json.dump(orc_recs, f, ensure_ascii=False)

    print("\n[3/3] Harvesting 65-university NIH PubMed statistical methodology lineages...")
    pm_recs = harvest_expanded_pubmed(fac_by_school)
    with open(CACHE_PUBMED, "w", encoding="utf-8") as f:
        json.dump(pm_recs, f, ensure_ascii=False)

    all_recs = wd_recs + orc_recs + pm_recs
    print(f"\nIngesting {len(all_recs):,} Wave 2B records into data/stat_genealogy.db...")
    ing = StatLinkIngestor()
    for rec in all_recs:
        ing.ingest_record(rec, allow_without_advisor=False)
    ing.commit_and_close()

    print(f"\nWave 2B DONE in {time.time() - t0:.1f}s:")
    for k, v in ing.stats.items():
        print(f"  {k:20s}: {v:,}")


if __name__ == "__main__":
    main()
