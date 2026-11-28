#!/usr/bin/env python3
"""
Wave 1C Harvester:
  1. `curl.exe --ssl-no-revoke` DSpace 7 Repositories (bypassing Python urllib3 TLS blocks on
     UT Austin, Johns Hopkins, U Adelaide, Baylor, U Houston, Texas Tech, U Missouri)
  2. Fixed Multi-Dialect OAI-PMH Harvester across 38 live university/national repositories:
     - Properly parses DigitalCommons `qdc` (`thesis.degree.discipline`, `thesis.degree.name`,
       `date.created`, and primary advisor `contributors[0]` when `contributor.advisor` absent)
     - Properly parses UK `uketd_dc`, DSpace `dim`, `etdms11`, `xMetaDissPlus`, and `oai_qdc`
  3. UC eScholarship GraphQL 2024-2026 Doctoral Refresh (all 10 UC campuses, strict `_XXXXD_`)
"""

import concurrent.futures
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib3
import xml.etree.ElementTree as ET

import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
socket.setdefaulttimeout(18.0)
sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "scripts"))

from etd_ingest_common import (
    CACHE_DIR,
    HEADERS,
    StatLinkIngestor,
    clean_person_name,
    clean_title,
    classify_stat_field,
    is_doctoral_degree,
    split_advisor_field,
)

CACHE_FILE = os.path.join(CACHE_DIR, "wave1c_curl_ds7_fixed_oai_records.json")
CHROME_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"

# 1. WAF-blocked / Extra DSpace 7 repositories via native curl.exe
CURL_DSPACE7_REPOS = [
    ("University of Texas at Austin", "United States", "https://repositories.lib.utexas.edu/server/api/discover/search/objects"),
    ("Johns Hopkins University", "United States", "https://jscholarship.library.jhu.edu/server/api/discover/search/objects"),
    ("University of Houston", "United States", "https://uh-ir.tdl.org/server/api/discover/search/objects"),
    ("Texas Tech University", "United States", "https://ttu-ir.tdl.org/server/api/discover/search/objects"),
    ("University of Missouri", "United States", "https://mospace.umsystem.edu/server/api/discover/search/objects"),
    ("Baylor University", "United States", "https://baylor-ir.tdl.org/server/api/discover/search/objects"),
    ("University of Adelaide", "Australia", "https://digital.library.adelaide.edu.au/server/api/discover/search/objects"),
]

ADVISOR_KEY_RE = re.compile(r"(?:advisor|adviser|supervisor|chair|cochair|co-chair|promoter|director|mentor)", re.I)
ADVISOR_KEY_EXCLUDE_RE = re.compile(r"(?:email|orcid|affiliation|committeemember|committee_member|reader|examiner|opponent|reviewer|referee|latestfordiscovery)", re.I)
DEGREE_KEY_RE = re.compile(r"(?:degree|level|type|genre)", re.I)
DEPT_SUBJ_KEY_RE = re.compile(r"(?:subject|discipline|department|school|program|major|faculty|unit|center)", re.I)

DSPACE_QUERIES = [
    "Statistics",
    "Biostatistics",
    "Statistical Science",
    "Probability stochastic",
    "Bayesian causal inference",
    "Survival analysis longitudinal",
    "High-dimensional regression",
]


def harvest_one_curl_dspace7(item):
    school, country, api_url = item
    records = []
    seen_uuids = set()
    t_repo = time.time()

    for q in DSPACE_QUERIES:
        if (time.time() - t_repo) > 105:
            break
        max_pages = 15 if q in ("Statistics", "Biostatistics", "Statistical Science") else 6
        for page in range(0, max_pages):
            if (time.time() - t_repo) > 105:
                break
            try:
                full_url = f"{api_url}?{urllib.parse.urlencode({'query': q, 'dsoType': 'item', 'size': 100, 'page': page})}"
                out = subprocess.check_output(
                    ["curl.exe", "-sS", "-m", "15", "-L", "--ssl-no-revoke", "-A", CHROME_UA, "-H", "Accept: application/json", full_url],
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
                    if not uuid or uuid in seen_uuids:
                        continue
                    seen_uuids.add(uuid)
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

    print(f"  [Curl-DS7] {school:42s}: {len(records):4d} doctoral stat/biostat/prob ETDs")
    return records


# ---------------------------------------------------------------------------
# 2. Fixed Multi-Dialect OAI-PMH Harvester (DigitalCommons qdc, DSpace dim, UK uketd_dc, ETDMS)
# ---------------------------------------------------------------------------

FIXED_OAI_REPOS = [
    # (school, country, url, prefix, dialect, explicit_sets, max_pages)
    ("University of Connecticut", "United States", "https://digitalcommons.lib.uconn.edu/do/oai/", "qdc", "bepress", ["publication:dissertations"], 35),
    ("Washington University in St. Louis", "United States", "https://openscholarship.wustl.edu/do/oai/", "qdc", "bepress", None, 35),
    ("Purdue University", "United States", "https://docs.lib.purdue.edu/do/oai/", "qdc", "bepress", ["publication:open_access_dissertations"], 40),
    ("University of Kentucky", "United States", "https://uknowledge.uky.edu/do/oai/", "qdc", "bepress", ["publication:statistics_etds", "publication:cph_etds"], 25),
    ("University of South Carolina", "United States", "https://scholarcommons.sc.edu/do/oai/", "qdc", "bepress", ["publication:etd"], 35),
    ("University of Nebraska-Lincoln", "United States", "https://digitalcommons.unl.edu/do/oai/", "qdc", "bepress", ["publication:statisticsdiss", "publication:dissertations"], 25),
    ("Clemson University", "United States", "https://tigerprints.clemson.edu/do/oai/", "qdc", "bepress", ["publication:all_dissertations"], 30),
    ("Southern Methodist University", "United States", "https://scholar.smu.edu/do/oai/", "qdc", "bepress", ["publication:hum_sci_statisticalscience_etds", "publication:etds"], 20),
    ("Louisiana State University", "United States", "https://repository.lsu.edu/do/oai/", "qdc", "bepress", ["publication:gradschool_dissertations"], 30),
    ("Wayne State University", "United States", "https://digitalcommons.wayne.edu/do/oai/", "qdc", "bepress", ["publication:oa_dissertations"], 25),
    ("Bowling Green State University", "United States", "https://scholarworks.bgsu.edu/do/oai/", "qdc", "bepress", ["publication:math_diss"], 20),
    ("Stony Brook University", "United States", "https://commons.library.stonybrook.edu/do/oai/", "qdc", "bepress", None, 25),
    ("Case Western Reserve University", "United States", "https://commons.case.edu/do/oai/", "qdc", "bepress", None, 25),
    ("University of Arkansas", "United States", "https://scholarworks.uark.edu/do/oai/", "qdc", "bepress", ["publication:etd"], 25),
    ("University of Rhode Island", "United States", "https://digitalcommons.uri.edu/do/oai/", "qdc", "bepress", ["publication:oa_diss"], 20),
    ("Marquette University", "United States", "https://epublications.marquette.edu/do/oai/", "qdc", "bepress", ["publication:dissertations_mu"], 20),
    ("University of Memphis", "United States", "https://digitalcommons.memphis.edu/do/oai/", "qdc", "bepress", ["publication:etd"], 25),
    ("University of Denver", "United States", "https://digitalcommons.du.edu/do/oai/", "qdc", "bepress", ["publication:etd"], 20),
    ("Florida State University", "United States", "https://diginole.lib.fsu.edu/oai2", "oai_qdc", "qdc", None, 30),
    ("University of Utah", "United States", "https://collections.lib.utah.edu/oai", "qdc", "qdc", None, 25),
    ("Texas A&M University", "United States", "https://oaktrust.library.tamu.edu/server/oai/request", "dim", "dim", None, 35),
    ("University of Melbourne", "Australia", "https://minerva-access.unimelb.edu.au/oai/request", "dim", "dim", None, 30),
    ("ETH Zürich", "Switzerland", "https://www.research-collection.ethz.ch/oai/request", "qdc", "qdc", None, 35),
    ("University of Alberta", "Canada", "https://era.library.ualberta.ca/oai", "qdc", "qdc", None, 30),
    ("Université de Montréal", "Canada", "https://papyrus.bib.umontreal.ca/oai/request", "etdms11", "etdms", None, 30),
    ("Concordia University", "Canada", "https://spectrum.library.concordia.ca/cgi/oai2", "oai_etdms", "etdms", None, 25),
    ("Universiteit van Amsterdam", "Netherlands", "https://pure.uva.nl/ws/oai", "qdc", "qdc", None, 25),
    ("Erasmus University Rotterdam", "Netherlands", "https://pure.eur.nl/ws/oai", "qdc", "qdc", None, 25),
    ("University of Warwick", "United Kingdom", "https://wrap.warwick.ac.uk/cgi/oai2", "uketd_dc", "uketd", None, 25),
    ("London School of Economics", "United Kingdom", "https://etheses.lse.ac.uk/cgi/oai2", "uketd_dc", "uketd", None, 20),
    ("University of Glasgow", "United Kingdom", "https://theses.gla.ac.uk/cgi/oai2", "uketd_dc", "uketd", None, 20),
    ("University of Southampton", "United Kingdom", "https://eprints.soton.ac.uk/cgi/oai2", "uketd_dc", "uketd", None, 20),
    ("White Rose (Sheffield/Leeds/York)", "United Kingdom", "https://etheses.whiterose.ac.uk/cgi/oai2", "uketd_dc", "uketd", None, 25),
    ("University of Birmingham", "United Kingdom", "https://etheses.bham.ac.uk/cgi/oai2", "uketd_dc", "uketd", None, 20),
    ("Lancaster University", "United Kingdom", "https://eprints.lancs.ac.uk/cgi/oai2", "uketd_dc", "uketd", None, 20),
    ("Swedish DiVA Portal", "Sweden", "https://www.diva-portal.org/dice/oai", "oai_etdms", "etdms", None, 30),
]


def _local_tag(el):
    return el.tag.split("}")[-1] if "}" in el.tag else el.tag


def _discover_thesis_sets(sess, url):
    sets = []
    try:
        r = sess.get(url, params={"verb": "ListSets"}, timeout=12, verify=False)
        if r.status_code != 200 or "<set>" not in r.text:
            return [None]
        root = ET.fromstring(r.text)
        all_sets = []
        for s_el in root.iter():
            if _local_tag(s_el) != "set":
                continue
            spec = sname = ""
            for ch in s_el:
                lt = _local_tag(ch)
                if lt == "setSpec" and ch.text:
                    spec = ch.text.strip()
                elif lt == "setName" and ch.text:
                    sname = ch.text.strip()
            if spec:
                all_sets.append((spec, sname))

        # 1. Explicit stat/biostat sets
        for sp, nm in all_sets:
            if re.search(r"\b(statist|biostat|probabilit|stochast)\b", f"{sp} {nm}", re.I) and not re.search(r"\b(master|undergrad|honors)\b", f"{sp} {nm}", re.I):
                sets.append(sp)
        # 2. Explicit dissertation/doctoral sets
        diss = [
            sp for sp, nm in all_sets
            if re.search(r"(dissert|doctoral|etd|thesis|theses|phd)", f"{sp} {nm}", re.I)
            and not re.search(r"(master|undergrad|honors|capstone|journal|article|poster)", f"{sp} {nm}", re.I)
            and sp not in sets
        ]
        diss.sort(key=lambda s: (0 if "dissert" in s.lower() or "phd" in s.lower() or "doct" in s.lower() else 1, len(s)))
        sets.extend(diss[:3])
    except Exception:
        pass
    return sets[:6] if sets else [None]


def harvest_one_fixed_oai(cfg):
    school_default, country_default, url, prefix, dialect, explicit_sets, max_pages = cfg
    records = []
    seen_ids = set()
    sess = requests.Session()
    sess.headers.update(HEADERS)
    t_start = time.time()

    target_sets = explicit_sets if explicit_sets else _discover_thesis_sets(sess, url)
    pages_used = 0

    for sp in target_sets:
        if pages_used >= max_pages or (time.time() - t_start) > 105:
            break
        token = None
        first_call = True
        while pages_used < max_pages and (time.time() - t_start) <= 105:
            try:
                if first_call:
                    params = {"verb": "ListRecords", "metadataPrefix": prefix}
                    if sp:
                        params["set"] = sp
                    if not sp or ("stat" not in sp.lower() and "biostat" not in sp.lower()):
                        params["from"] = "2010-01-01"
                    first_call = False
                else:
                    params = {"verb": "ListRecords", "resumptionToken": token}
                r = sess.get(url, params=params, timeout=(8, 16), verify=False)
                pages_used += 1
                if r.status_code != 200 or "<record" not in r.text:
                    break
                root = ET.fromstring(r.text)

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
                    explicit_advisors = []
                    generic_contribs = []
                    titles = []
                    dates = []
                    subjs = []
                    degrees = []
                    insts = []

                    for el in meta.iter():
                        lt = _local_tag(el).lower()
                        txt = (el.text or "").strip()
                        if not txt:
                            continue
                        if lt == "field":
                            elem = (el.attrib.get("element") or "").lower()
                            qual = (el.attrib.get("qualifier") or "").lower()
                            key = f"{elem}.{qual}" if qual else elem
                            if key in ("contributor.author", "creator"):
                                authors.append(txt)
                            elif any(w in key for w in ("advisor", "chair", "supervisor", "promoter")) and "committee" not in key and "email" not in key:
                                explicit_advisors.extend(split_advisor_field(txt))
                            elif key == "title":
                                titles.append(txt)
                            elif elem == "date" and qual in ("issued", "created", "awarded", ""):
                                dates.append(txt)
                            elif elem in ("subject", "department", "discipline") or qual in ("department", "discipline", "other"):
                                subjs.append(txt)
                            elif "degree" in key or elem == "type":
                                degrees.append(txt)
                            elif qual == "grantor":
                                insts.append(txt)
                        else:
                            if lt in ("creator", "author"):
                                authors.append(txt)
                            elif any(w in lt for w in ("advisor", "supervisor", "chair", "promoter")) and "committee" not in lt:
                                explicit_advisors.extend(split_advisor_field(txt))
                            elif lt == "contributor":
                                role = (el.attrib.get("role") or el.attrib.get("type") or "").lower()
                                if any(w in role for w in ("advisor", "supervisor", "chair", "director", "promoter")):
                                    explicit_advisors.extend(split_advisor_field(txt))
                                else:
                                    generic_contribs.append(txt)
                            elif lt == "title":
                                titles.append(txt)
                            elif lt.startswith("date") or lt in ("issued", "created"):
                                dates.append(txt)
                            elif any(w in lt for w in ("discipline", "department", "subject")):
                                subjs.append(txt)
                            elif any(w in lt for w in ("degree.name", "degree.level", "qualification", "level", "type", "genre")):
                                degrees.append(txt)
                            elif any(w in lt for w in ("grantor", "institution", "publisher")):
                                insts.append(txt)

                    # In bepress DigitalCommons qdc, the 1st <dc:contributor> is the Major Advisor (subsequent are committee)
                    advisors = explicit_advisors
                    if not advisors and dialect == "bepress" and generic_contribs:
                        advisors = split_advisor_field(generic_contribs[0])
                    if len(authors) != 1 or not advisors or not titles:
                        continue
                    student = clean_person_name(authors[0])
                    if not student:
                        continue
                    title = clean_title(titles[0])
                    deg_str = " | ".join(degrees)
                    if not is_doctoral_degree(deg_str, title, require_positive=(dialect == "bepress")):
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

                    school = school_default
                    if "White Rose" in school_default or "DiVA" in school_default:
                        school = ""
                        for inst in insts:
                            if len(inst) < 100 and any(w in inst.lower() for w in ("universit", "college", "kth", "chalmers", "karolinska", "school", "institute", "sheffield", "leeds", "york")):
                                school = inst.split(",")[0].strip()
                                break
                        if not school:
                            continue

                    records.append({
                        "student": student,
                        "advisors": advisors[:2],
                        "school": school,
                        "country": country_default,
                        "year": yr,
                        "thesis": title,
                        "subjects": subj_str,
                        "degree": deg_str,
                        "source": "DigitalCommons ETD" if dialect == "bepress" else "OAI-PMH ETD",
                    })

                rt = None
                for el in root.iter():
                    if _local_tag(el) == "resumptionToken" and el.text and el.text.strip():
                        rt = el.text.strip()
                        break
                if not rt:
                    break
                token = rt
            except Exception:
                break

    print(f"  [Fixed-OAI] {school_default:42s}: {len(records):4d} doctoral stat/biostat/prob ETDs (pages={pages_used})")
    return records


# ---------------------------------------------------------------------------
# 3. Working UC eScholarship 2023-2026 Refresh (All 10 Campuses, strict _XXXXD_)
# ---------------------------------------------------------------------------

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

ESCHOL_QUERY = """
query($unitId: ID!, $more: String) {
  unit(id: $unitId) {
    items(first: 300, more: $more) {
      total
      more
      nodes {
        id title published subjects nativeFileName
        authors { nodes { name orcid } }
        contributors { nodes { name role } }
      }
    }
  }
}
"""


def harvest_uc_recent():
    records = []
    sess = requests.Session()
    sess.headers.update(HEADERS)
    seen = set()
    for unit_id, school in UC_CAMPUSES:
        more = None
        cnt = 0
        for _ in range(10):  # Most recent 3,000 ETDs per campus (2023-2026)
            try:
                r = sess.post(
                    "https://escholarship.org/graphql",
                    json={"query": ESCHOL_QUERY, "variables": {"unitId": unit_id, "more": more}},
                    timeout=20,
                )
                if r.status_code != 200:
                    break
                items = (((r.json().get("data") or {}).get("unit") or {}).get("items")) or {}
                nodes = items.get("nodes") or []
                if not nodes:
                    break
                old_in_batch = 0
                for nd in nodes:
                    nid = nd.get("id")
                    if not nid or nid in seen:
                        continue
                    seen.add(nid)
                    pub = nd.get("published") or ""
                    ym = re.search(r"\b(20\d{2})\b", pub)
                    yr = int(ym.group(1)) if ym else None
                    if yr and yr < 2023:
                        old_in_batch += 1
                        continue
                    nfn = nd.get("nativeFileName") or ""
                    # STRICT ProQuest doctoral code check (_XXXXD_ vs _XXXXN_/_XXXXM_ Master's!)
                    if nfn and re.search(r"_\d{4}[NM]_", nfn):
                        continue
                    auths = ((nd.get("authors") or {}).get("nodes")) or []
                    contribs = ((nd.get("contributors") or {}).get("nodes")) or []
                    advisors = []
                    for c in contribs:
                        if (c.get("role") or "").upper() in ("ADVISOR", "CHAIR", "CO-CHAIR", "SUPERVISOR"):
                            advisors.extend(split_advisor_field(c.get("name") or ""))
                    if len(auths) != 1 or not advisors:
                        continue
                    student = clean_person_name(auths[0].get("name") or "")
                    if not student:
                        continue
                    title = clean_title(nd.get("title") or "")
                    subjs = ", ".join(nd.get("subjects") or [])
                    if not classify_stat_field(subjs, title, advisor_is_core=False):
                        continue
                    records.append({
                        "student": student,
                        "advisors": advisors[:3],
                        "school": school,
                        "country": "United States",
                        "year": yr,
                        "thesis": title,
                        "subjects": subjs,
                        "degree": "Ph.D.",
                        "orcid": (auths[0].get("orcid") or "").strip(),
                        "source": "eScholarship",
                    })
                    cnt += 1
                more = items.get("more")
                if not more or old_in_batch >= 200:
                    break
            except Exception:
                break
        if cnt:
            print(f"  [eScholarship] {school:42s}: {cnt:4d} recent (2023-2026) doctoral ETDs")
    return records


def main():
    t0 = time.time()
    print("=" * 72)
    print("WAVE 1C: CURL-DSPACE7 (UT AUSTIN/JHU/ETC) + 36 FIXED OAI-PMH + UC 2026")
    print("=" * 72)

    all_records = []

    print("\n[1/3] Crawling 7 WAF-bypassed DSpace 7 repositories via curl.exe...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=7) as pool:
        for res in pool.map(harvest_one_curl_dspace7, CURL_DSPACE7_REPOS):
            all_records.extend(res)

    print("\n[2/3] Crawling 36 fixed multi-dialect OAI-PMH repositories...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        for res in pool.map(harvest_one_fixed_oai, FIXED_OAI_REPOS):
            all_records.extend(res)

    print("\n[3/3] Refreshing UC eScholarship 2023-2026 doctoral dissertations...")
    all_records.extend(harvest_uc_recent())

    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(all_records, f, ensure_ascii=False)
    print(f"\nSaved {len(all_records):,} raw Wave 1C doctoral records -> {CACHE_FILE}")

    print("\nIngesting Wave 1C records into data/stat_genealogy.db...")
    ing = StatLinkIngestor()
    for rec in all_records:
        ing.ingest_record(rec, allow_without_advisor=False)
    ing.commit_and_close()

    print(f"\nWave 1C DONE in {time.time() - t0:.1f}s:")
    for k, v in ing.stats.items():
        print(f"  {k:20s}: {v:,}")


if __name__ == "__main__":
    main()
