#!/usr/bin/env python3
"""
Wave 1B + 2A Harvester:
  1. French National Theses API (theses.fr) — defended Statistics/Biostatistics/Probability PhDs
  2. German National Library (DNB SRU MARC21-XML) — doctoral dissertations with explicit `dgs` advisor
  3. 26 Live OAI-PMH University & National Repositories (US DigitalCommons `qdc`, Iowa Esploro,
     Texas A&M `dim`, FSU, Utah, UK EPrints/ORA `uketd_dc`, ETH Zurich `qdc`, Alberta `etdms`,
     Swedish DiVA `oai_etdms`)
  4. Global DataCite Dissertation API (2000-2026 expanded sweep with explicit Supervisor/Advisor)
"""

import concurrent.futures
import json
import os
import re
import socket
import sys
import time
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

CACHE_FILE = os.path.join(CACHE_DIR, "wave1b_oai_national_datacite_records.json")


# ---------------------------------------------------------------------------
# 1. French National Theses API (theses.fr)
# ---------------------------------------------------------------------------

def harvest_theses_fr():
    records = []
    seen_ids = set()
    sess = requests.Session()
    sess.headers.update(HEADERS)

    queries = [
        'discipline:(statistique OR statistiques) AND status:soutenue',
        'discipline:(biostatistique OR biostatistiques OR biometrie) AND status:soutenue',
        'discipline:(probabilite OR probabilites OR stochastique) AND status:soutenue',
        'discipline:(econometrie OR actuariat) AND status:soutenue',
        '(titrePrincipal:(statistique OR biostatistique OR bayesian OR baysien OR stochastique OR probabilite OR survie OR "causal" OR "high-dimensional" OR "grande dimension") AND status:soutenue)',
    ]

    for q in queries:
        for debut in range(0, 4500, 100):
            try:
                r = sess.get(
                    "https://theses.fr/api/v1/theses/recherche/",
                    params={"q": q, "nombre": 100, "debut": debut},
                    timeout=18,
                )
                if r.status_code != 200:
                    break
                data = r.json()
                theses = data.get("theses") or []
                if not theses:
                    break
                for t in theses:
                    tid = t.get("id")
                    if not tid or tid in seen_ids:
                        continue
                    seen_ids.add(tid)
                    if (t.get("status") or "").lower() != "soutenue":
                        continue
                    auteurs = t.get("auteurs") or []
                    dirs = t.get("directeurs") or []
                    if len(auteurs) != 1 or not dirs:
                        continue
                    a0 = auteurs[0]
                    student = clean_person_name(f"{a0.get('prenom') or ''} {a0.get('nom') or ''}")
                    if not student:
                        continue
                    advisors = []
                    for d in dirs[:3]:
                        anm = clean_person_name(f"{d.get('prenom') or ''} {d.get('nom') or ''}")
                        if anm:
                            advisors.append(anm)
                    if not advisors:
                        continue
                    title = clean_title(t.get("titreEN") or t.get("titrePrincipal") or "")
                    if not title:
                        continue
                    disc = t.get("discipline") or ""
                    sujets = " ".join(
                        (s.get("libelle") or "") for s in (t.get("sujets") or []) if isinstance(s, dict)
                    )
                    subjs = f"{disc}, {sujets}".strip(", ")
                    if not classify_stat_field(subjs, title, advisor_is_core=False):
                        continue
                    yr = None
                    ds = t.get("dateSoutenance") or ""
                    ym = re.search(r"\b(19\d{2}|20[0-2]\d)\b", ds)
                    if ym:
                        yr = int(ym.group(1))
                    school = (t.get("etabSoutenanceN") or "").strip()
                    if not school:
                        continue
                    records.append({
                        "student": student,
                        "advisors": advisors,
                        "school": school,
                        "country": "France",
                        "year": yr,
                        "thesis": title,
                        "subjects": subjs,
                        "degree": "Doctorat",
                        "source": "theses.fr",
                    })
                if len(theses) < 100 or debut + 100 >= (data.get("totalHits") or 0):
                    break
            except Exception:
                break
    print(f"  [theses.fr] Harvested {len(records):,} defended French doctoral theses")
    return records


# ---------------------------------------------------------------------------
# 2. German National Library (DNB SRU MARC21-XML)
# ---------------------------------------------------------------------------

def _local_tag(el):
    return el.tag.split("}")[-1] if "}" in el.tag else el.tag


def harvest_dnb_german_theses():
    records = []
    seen_keys = set()
    sess = requests.Session()
    sess.headers.update(HEADERS)

    cql_queries = [
        'hss="Diss" AND sw="Statistik"',
        'hss="Diss" AND sw="Biostatistik"',
        'hss="Diss" AND sw="Stochastik"',
        'hss="Diss" AND sw="Wahrscheinlichkeitstheorie"',
        'hss="Diss" AND sw="Ökonometrie"',
        'hss="Diss" AND tit="Statistics"',
        'hss="Diss" AND tit="Statistical"',
        'hss="Diss" AND tit="Biostatistics"',
        'hss="Diss" AND tit="Stochastic"',
        'hss="Diss" AND tit="Bayesian"',
        'hss="Diss" AND tit="Causal Inference"',
        'hss="Diss" AND tit="Survival Analysis"',
        'hss="Diss" AND tit="High-dimensional"',
    ]

    for cql in cql_queries:
        start = 1
        for _ in range(20):  # Up to 2,000 per query
            try:
                r = sess.get(
                    "https://services.dnb.de/sru/dnb",
                    params={
                        "version": "1.1",
                        "operation": "searchRetrieve",
                        "query": cql,
                        "startRecord": start,
                        "maximumRecords": 100,
                        "recordSchema": "MARC21-xml",
                    },
                    timeout=20,
                )
                if r.status_code != 200 or "<record" not in r.text:
                    break
                root = ET.fromstring(r.text)
                recs = [el for el in root.iter() if _local_tag(el) == "record" and any(_local_tag(c) == "datafield" for c in el)]
                if not recs:
                    break

                for rec in recs:
                    author = ""
                    title = ""
                    advisors = []
                    school = ""
                    yr = None
                    subjs = []
                    deg_str = "Dissertation"

                    for df in rec:
                        if _local_tag(df) != "datafield":
                            continue
                        tag = df.attrib.get("tag", "")
                        sfs = {}
                        for sf in df:
                            code = sf.attrib.get("code", "")
                            if sf.text:
                                sfs.setdefault(code, []).append(sf.text.strip())
                        if tag == "100" and not author:
                            author = clean_person_name(" ".join(sfs.get("a", [])))
                        elif tag == "245" and not title:
                            title = clean_title(" ".join(sfs.get("a", []) + sfs.get("b", [])))
                        elif tag == "502":
                            txt502 = " ".join(v for vals in sfs.values() for v in vals)
                            deg_str = txt502
                            if sfs.get("c"):
                                school = sfs["c"][0]
                            else:
                                # Parse "München, Technische Universität München, Diss., 2016"
                                m_sch = re.search(r",\s*([^,]+(?:Universit[äa]t|University|Hochschule|ETH|TU\s+\w+|LMU|RWTH|KIT)[^,]*)", txt502, re.I)
                                if m_sch:
                                    school = m_sch.group(1).strip()
                            ym = re.search(r"\b(19\d{2}|20[0-2]\d)\b", txt502)
                            if ym:
                                yr = int(ym.group(1))
                        elif tag == "700":
                            rel4 = [x.lower() for x in sfs.get("4", [])]
                            rele = " ".join(sfs.get("e", [])).lower()
                            if "dgs" in rel4 or "betreuer" in rele or "doktorvater" in rele:
                                anm = clean_person_name(" ".join(sfs.get("a", [])))
                                if anm:
                                    advisors.append(anm)
                        elif tag in ("650", "653", "689"):
                            subjs.extend(sfs.get("a", []))

                    # Keep only explicit doctoral dissertations with 1-2 explicit advisors (avoid large examiner panels)
                    if not author or not title or not advisors or len(advisors) > 3 or not school:
                        continue
                    if re.search(r"\b(habil|master|bachelor|diplom|magister)\b", deg_str, re.I):
                        continue
                    subj_str = ", ".join(subjs)
                    if not classify_stat_field(subj_str, title, advisor_is_core=False):
                        continue
                    dkey = (author.lower(), yr or 0, title[:40].lower())
                    if dkey in seen_keys:
                        continue
                    seen_keys.add(dkey)
                    cntry = "United States" if "carnegie mellon" in school.lower() else ("Switzerland" if "eth" in school.lower() or "zürich" in school.lower() else "Germany")
                    records.append({
                        "student": author,
                        "advisors": advisors[:2],
                        "school": school,
                        "country": cntry,
                        "year": yr,
                        "thesis": title,
                        "subjects": subj_str,
                        "degree": deg_str,
                        "source": "DNB German ETD",
                    })

                if len(recs) < 100:
                    break
                start += 100
            except Exception:
                break
    print(f"  [DNB SRU] Harvested {len(records):,} German/European doctoral theses with explicit advisors")
    return records


# ---------------------------------------------------------------------------
# 3. Live OAI-PMH University & National Thesis Repositories (24+ repositories)
# ---------------------------------------------------------------------------

OAI_REPOS = [
    # (school, country, url, preferred_prefix, set_keywords, max_pages)
    ("University of Connecticut", "United States", "https://digitalcommons.lib.uconn.edu/do/oai/", "qdc", ["dissertation", "etd"], 25),
    ("Washington University in St. Louis", "United States", "https://openscholarship.wustl.edu/do/oai/", "qdc", ["etd", "dissertation"], 25),
    ("Purdue University", "United States", "https://docs.lib.purdue.edu/do/oai/", "qdc", ["dissertation", "open_access_dissertations", "etd"], 30),
    ("University of Kentucky", "United States", "https://uknowledge.uky.edu/do/oai/", "qdc", ["statistics_etds", "biostat", "etd", "dissertation"], 25),
    ("University of South Carolina", "United States", "https://scholarcommons.sc.edu/do/oai/", "qdc", ["etd"], 25),
    ("University of Nebraska-Lincoln", "United States", "https://digitalcommons.unl.edu/do/oai/", "qdc", ["statistics", "dissertation", "etd"], 20),
    ("Clemson University", "United States", "https://tigerprints.clemson.edu/do/oai/", "qdc", ["all_dissertations", "dissertation"], 25),
    ("Southern Methodist University", "United States", "https://scholar.smu.edu/do/oai/", "qdc", ["stat", "etd", "dissertation"], 15),
    ("Louisiana State University", "United States", "https://repository.lsu.edu/do/oai/", "qdc", ["gradschool_dissertations"], 25),
    ("Wayne State University", "United States", "https://digitalcommons.wayne.edu/do/oai/", "qdc", ["oa_dissertations"], 20),
    ("Bowling Green State University", "United States", "https://scholarworks.bgsu.edu/do/oai/", "qdc", ["math_diss", "stat", "diss"], 15),
    ("Stony Brook University", "United States", "https://commons.library.stonybrook.edu/do/oai/", "qdc", ["dissertation", "etd", "ams"], 20),
    ("Case Western Reserve University", "United States", "https://commons.case.edu/do/oai/", "qdc", ["etd", "dissertation"], 20),
    ("Florida State University", "United States", "https://diginole.lib.fsu.edu/oai2", "oai_qdc", ["etd", "dissertation", "stat"], 25),
    ("University of Utah", "United States", "https://collections.lib.utah.edu/oai", "qdc", ["etd", "dissertation"], 20),
    ("Texas A&M University", "United States", "https://oaktrust.library.tamu.edu/server/oai/request", "dim", ["dissertation", "thesis", "etd", "stat"], 30),
    ("University of Iowa", "United States", "https://iro.uiowa.edu/view/oai/01IOWA_INST/request", "oai_qdc", ["etd", "dissertation", "thesis"], 25),
    ("ETH Zürich", "Switzerland", "https://www.research-collection.ethz.ch/oai/request", "qdc", ["doctoral", "dissertation", "thesis"], 30),
    ("University of Alberta", "Canada", "https://era.library.ualberta.ca/oai", "qdc", ["thesis", "dissertation"], 25),
    ("University of Oxford", "United Kingdom", "https://ora.ox.ac.uk/oai2", "uketd_dc", ["thesis", "theses", "stat"], 25),
    ("London School of Economics", "United Kingdom", "https://etheses.lse.ac.uk/cgi/oai2", "uketd_dc", ["stat", "thesis", "phd"], 20),
    ("University of Warwick", "United Kingdom", "https://wrap.warwick.ac.uk/cgi/oai2", "uketd_dc", ["stat", "thesis"], 25),
    ("University of Glasgow", "United Kingdom", "https://theses.gla.ac.uk/cgi/oai2", "uketd_dc", ["stat", "math", "phd"], 20),
    ("University of Southampton", "United Kingdom", "https://eprints.soton.ac.uk/cgi/oai2", "uketd_dc", ["stat", "math", "thesis"], 20),
    ("White Rose (Sheffield/Leeds/York)", "United Kingdom", "https://etheses.whiterose.ac.uk/cgi/oai2", "uketd_dc", ["stat", "math", "phd"], 25),
    ("University of Birmingham", "United Kingdom", "https://etheses.bham.ac.uk/cgi/oai2", "uketd_dc", ["stat", "math", "phd"], 20),
    ("Lancaster University", "United Kingdom", "https://eprints.lancs.ac.uk/cgi/oai2", "uketd_dc", ["stat", "math", "thesis"], 20),
    ("Swedish DiVA Portal", "Sweden", "https://www.diva-portal.org/dice/oai", "oai_etdms", ["doctoral", "dissertation", "stat"], 25),
]


def _pick_oai_sets(sess, url, keywords):
    """Auto-discover matching thesis/statistics setSpecs via ListSets."""
    sets = []
    try:
        r = sess.get(url, params={"verb": "ListSets"}, timeout=15, verify=False)
        if r.status_code != 200 or "<set>" not in r.text:
            return [None]
        root = ET.fromstring(r.text)
        all_sets = []
        for s_el in root.iter():
            if _local_tag(s_el) != "set":
                continue
            spec = ""
            sname = ""
            for ch in s_el:
                lt = _local_tag(ch)
                if lt == "setSpec" and ch.text:
                    spec = ch.text.strip()
                elif lt == "setName" and ch.text:
                    sname = ch.text.strip()
            if spec:
                all_sets.append((spec, sname))

        # Priority 1: Explicit Statistics/Biostatistics thesis sets
        stat_sets = [
            sp for sp, nm in all_sets
            if re.search(r"\b(statist|biostat|probabilit|stochast|actuar)\b", f"{sp} {nm}", re.I)
            and not re.search(r"\b(master|undergrad|honors|news|newsletter)\b", f"{sp} {nm}", re.I)
        ]
        if stat_sets:
            sets.extend(stat_sets[:10])

        # Priority 2: Explicit doctoral dissertation sets
        diss_sets = [
            sp for sp, nm in all_sets
            if any(k in f"{sp} {nm}".lower() for k in keywords)
            and not re.search(r"\b(master|undergrad|honors|capstone|journal|article)\b", f"{sp} {nm}", re.I)
            and sp not in sets
        ]
        if diss_sets:
            # Prefer "dissertation" over generic "thesis"
            diss_sets.sort(key=lambda sp: (0 if "dissert" in sp.lower() or "phd" in sp.lower() or "doct" in sp.lower() else 1, len(sp)))
            sets.extend(diss_sets[:3])
    except Exception:
        pass
    return sets if sets else [None]


def harvest_one_oai_repo(cfg):
    school_default, country_default, url, prefix, set_kws, max_pages = cfg
    records = []
    seen_ids = set()
    sess = requests.Session()
    sess.headers.update(HEADERS)

    target_sets = _pick_oai_sets(sess, url, set_kws)
    pages_used = 0
    t_repo_start = time.time()

    for sp in target_sets:
        if pages_used >= max_pages or (time.time() - t_repo_start) > 120:
            break
        token = None
        first_call = True
        while pages_used < max_pages and (time.time() - t_repo_start) <= 120:
            try:
                if first_call:
                    params = {"verb": "ListRecords", "metadataPrefix": prefix}
                    if sp:
                        params["set"] = sp
                    # Focus on 2012+ for huge general sets so we cover recent years fast
                    if sp is None or ("stat" not in sp.lower() and "biostat" not in sp.lower()):
                        params["from"] = "2012-01-01"
                    first_call = False
                else:
                    params = {"verb": "ListRecords", "resumptionToken": token}
                r = sess.get(url, params=params, timeout=22, verify=False)
                pages_used += 1
                if r.status_code != 200 or "<record" not in r.text:
                    break
                root = ET.fromstring(r.text)
                for rec in root.iter():
                    if _local_tag(rec) != "record":
                        continue
                    hdr = None
                    meta = None
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

                    # Generic multi-schema XML extractor (qdc, oai_qdc, uketd_dc, oai_etdms, dim)
                    authors = []
                    advisors = []
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
                            # DSpace DIM schema: <dim:field mdschema="dc" element="contributor" qualifier="advisor">
                            elem = (el.attrib.get("element") or "").lower()
                            qual = (el.attrib.get("qualifier") or "").lower()
                            key = f"{elem}.{qual}" if qual else elem
                            if key in ("contributor.author", "creator"):
                                authors.append(txt)
                            elif any(w in key for w in ("advisor", "chair", "supervisor", "promoter")) and "committee" not in key and "email" not in key:
                                advisors.extend(split_advisor_field(txt))
                            elif key == "title":
                                titles.append(txt)
                            elif elem == "date" and qual in ("issued", "created", "awarded", ""):
                                dates.append(txt)
                            elif elem in ("subject", "department", "discipline") or qual in ("department", "discipline"):
                                subjs.append(txt)
                            elif "degree" in key or elem == "type":
                                degrees.append(txt)
                            elif qual == "grantor":
                                insts.append(txt)
                        else:
                            if lt in ("creator", "author"):
                                authors.append(txt)
                            elif lt in ("advisor", "contributor.advisor", "supervisor", "thesisadvisor"):
                                advisors.extend(split_advisor_field(txt))
                            elif lt == "contributor":
                                # In uketd_dc / oai_etdms, check role attribute or keep only if advisor/supervisor role
                                role = (el.attrib.get("role") or el.attrib.get("type") or "").lower()
                                if any(w in role for w in ("advisor", "supervisor", "chair", "director", "promoter")):
                                    advisors.extend(split_advisor_field(txt))
                            elif lt == "title":
                                titles.append(txt)
                            elif lt in ("date", "issued", "created"):
                                dates.append(txt)
                            elif lt in ("subject", "department", "discipline", "description.department"):
                                subjs.append(txt)
                            elif lt in ("type", "qualificationlevel", "qualificationname", "level", "degree", "genre"):
                                degrees.append(txt)
                            elif lt in ("institution", "grantor", "publisher"):
                                insts.append(txt)

                    if len(authors) != 1 or not advisors or not titles:
                        continue
                    student = clean_person_name(authors[0])
                    if not student:
                        continue
                    title = clean_title(titles[0])
                    deg_str = " | ".join(degrees)
                    if not is_doctoral_degree(deg_str, title, require_positive=False):
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

                    # Resolve actual institution for multi-university portals (White Rose, Swedish DiVA)
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
                        "advisors": advisors[:3],
                        "school": school,
                        "country": country_default,
                        "year": yr,
                        "thesis": title,
                        "subjects": subj_str,
                        "degree": deg_str,
                        "source": "OAI-PMH ETD",
                    })

                # Check resumptionToken
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

    print(f"  [OAI-PMH] {school_default:42s}: {len(records):4d} doctoral stat/biostat/prob ETDs (sets={len(target_sets)}, pages={pages_used})")
    return records


# ---------------------------------------------------------------------------
# 4. DataCite Global Dissertation Expanded Sweep (2000–2026, ZERO daily quota)
# ---------------------------------------------------------------------------

def harvest_datacite_expanded():
    records = []
    seen_dois = set()
    sess = requests.Session()
    sess.headers.update(HEADERS)

    dc_queries = [
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
        "random matrix",
        "graphical models",
        "experimental design",
        "longitudinal data",
        "missing data",
        "clinical trials",
        "conformal prediction",
        "statistical genetics",
    ]

    for q in dc_queries:
        url = "https://api.datacite.org/dois"
        params = {
            "resource-type-id": "dissertation",
            "query": q,
            "page[size]": 500,
            "page[cursor]": 1,
        }
        for _ in range(10):  # Up to 5,000 per keyword
            try:
                r = sess.get(url, params=params, timeout=22)
                if r.status_code != 200:
                    break
                payload = r.json()
                items = payload.get("data") or []
                if not items:
                    break
                for it in items:
                    doi = it.get("id")
                    if not doi or doi in seen_dois:
                        continue
                    seen_dois.add(doi)
                    attr = it.get("attributes") or {}
                    contribs = attr.get("contributors") or []
                    advisors = []
                    for c in contribs:
                        ctype = (c.get("contributorType") or "").lower()
                        if ctype in ("supervisor", "advisor", "thesisadvisor"):
                            cname = c.get("name") or f"{c.get('givenName') or ''} {c.get('familyName') or ''}"
                            advisors.extend(split_advisor_field(cname))
                    if not advisors:
                        continue

                    creators = attr.get("creators") or []
                    if len(creators) != 1:
                        continue
                    cr0 = creators[0]
                    student = clean_person_name(cr0.get("name") or f"{cr0.get('givenName') or ''} {cr0.get('familyName') or ''}")
                    if not student:
                        continue

                    orcid = ""
                    for ni in (cr0.get("nameIdentifiers") or []):
                        om = re.search(r"(\d{4}-\d{4}-\d{4}-\d{3}[\dX])", str(ni.get("nameIdentifier") or ""))
                        if om:
                            orcid = om.group(1)
                            break

                    titles = attr.get("titles") or []
                    title = clean_title(titles[0].get("title") if titles else "")
                    if not title:
                        continue

                    types_obj = attr.get("types") or {}
                    deg_str = " ".join(str(x) for x in (types_obj.get("resourceType") or "", types_obj.get("resourceTypeGeneral") or ""))
                    if not is_doctoral_degree(deg_str, title, require_positive=False):
                        continue

                    subjs = ", ".join((s.get("subject") or "") for s in (attr.get("subjects") or []) if isinstance(s, dict))
                    if not classify_stat_field(subjs, title, advisor_is_core=False):
                        continue

                    yr = attr.get("publicationYear")
                    if yr is not None:
                        try:
                            yr = int(yr)
                        except Exception:
                            yr = None
                    if yr and (yr < 1950 or yr > 2027):
                        continue

                    # Resolve university from creator affiliation or publisher
                    school = ""
                    for aff in (cr0.get("affiliation") or []):
                        aname = aff.get("name") if isinstance(aff, dict) else str(aff)
                        if aname and len(aname) < 120:
                            school = aname.strip()
                            break
                    if not school:
                        pub = attr.get("publisher")
                        if isinstance(pub, dict):
                            pub = pub.get("name") or ""
                        pub = str(pub or "").strip()
                        if pub and any(w in pub.lower() for w in ("universit", "institute", "college", "school", "eth", "epfl", "mit", "stanford", "harvard", "oxford", "cambridge")):
                            school = pub
                    if not school:
                        continue

                    records.append({
                        "student": student,
                        "advisors": advisors[:3],
                        "school": school,
                        "country": "",
                        "year": yr,
                        "thesis": title,
                        "subjects": subjs,
                        "degree": deg_str,
                        "orcid": orcid,
                        "source": "DataCite ETD",
                    })

                nxt = (payload.get("links") or {}).get("next")
                if not nxt:
                    break
                url = nxt
                params = None
            except Exception:
                break
    print(f"  [DataCite Expanded] Harvested {len(records):,} global doctoral dissertations with explicit supervisors")
    return records


def main():
    t0 = time.time()
    print("=" * 72)
    print("WAVE 1B + 2A: THESES.FR + DNB GERMAN + 28 OAI-PMH REPOS + DATACITE")
    print("=" * 72)

    all_records = []

    print("\n[1/4] Harvesting French National Theses API (theses.fr)...")
    all_records.extend(harvest_theses_fr())

    print("\n[2/4] Harvesting German National Library (DNB SRU MARC21-XML)...")
    all_records.extend(harvest_dnb_german_theses())

    print("\n[3/4] Harvesting 28 live OAI-PMH university & national thesis repositories...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        for res in pool.map(harvest_one_oai_repo, OAI_REPOS):
            all_records.extend(res)

    print("\n[4/4] Harvesting expanded global DataCite Dissertations (2000-2026)...")
    all_records.extend(harvest_datacite_expanded())

    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(all_records, f, ensure_ascii=False)
    print(f"\nSaved {len(all_records):,} raw doctoral records -> {CACHE_FILE}")

    print("\nIngesting Wave 1B + 2A records into data/stat_genealogy.db...")
    ing = StatLinkIngestor()
    for rec in all_records:
        ing.ingest_record(rec, allow_without_advisor=False)
    ing.commit_and_close()

    print(f"\nWave 1B + 2A DONE in {time.time() - t0:.1f}s:")
    for k, v in ing.stats.items():
        print(f"  {k:20s}: {v:,}")


if __name__ == "__main__":
    main()
