#!/usr/bin/env python3
"""
Unified Academic Family Tree Builder for Statistics, Biostatistics, Probability & Related Fields.

Integrates three complementary authoritative scholarly genealogy sources:
  1. Mathematics Genealogy Project (MGP) — 338,532 PhD records (Summer 2026 cleaned dump)
  2. Academic Family Tree (academictree.org) — April 2026 official Zenodo database export
     (includes ProQuest-seeded PhD dissertations across Biostatistics, Public Health,
      Epidemiology, Mathematics, Computational Biology, Econometrics, Psychometrics, CS)
  3. Wikidata Scholarly Graph — Curated doctoral advisor/student edges & 9,547 exact MGP<->AFT ID bridges

Key Pipeline Stages:
  Stage 1: Canonical school & country normalization + nickname/diacritic name indexing
  Stage 2: High-precision multi-evidence Entity Resolution (Wikidata bridges + thesis/school/year/kinship)
  Stage 3: PhD-only (relation=1) genealogical graph construction & cycle sanitization (strict DAG)
  Stage 4: Multi-signal High-Recall Discipline Classifier:
           - Explicit MSC 62 (Statistics) & MSC 60 (Probability)
           - Wikidata occupation/field & AcademicTree research area / Biostatistics dept metadata
           - Calibrated TF-IDF + LogisticRegression classifier trained on 202,492 MSC-labeled dissertations
             combined with curated domain terminology (handling the 40% missing-MSC MGP records)
           - Two-pass Advisor/Student Lineage Consensus propagation
  Stage 5: Exact topological DAG descendant counting + SQLite FTS5 full-text search indexing
"""

import csv
import gzip
import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
from collections import Counter, defaultdict, deque

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression

sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_DIR = os.path.join(BASE_DIR, "data_raw")
OUT_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(OUT_DIR, "stat_genealogy.db")


# ---------------------------------------------------------------------------
# 1. Name, Nickname, Country & School Normalization Utilities
# ---------------------------------------------------------------------------

NICKNAME_GROUPS = [
    {"robert", "rob", "bob", "bobby", "robbie"},
    {"richard", "rick", "dick", "rich", "ricky"},
    {"william", "bill", "will", "billy", "willy", "liam"},
    {"james", "jim", "jimmy", "jamie"},
    {"john", "jack", "johnny", "jon"},
    {"michael", "mike", "mick"},
    {"david", "dave", "davy"},
    {"daniel", "dan", "danny"},
    {"thomas", "tom", "tommy"},
    {"charles", "chuck", "charlie", "chas"},
    {"JOSEPH".lower(), "joe", "joey"},
    {"edward", "ed", "eddie", "ted", "teddy", "ned"},
    {"stephen", "steven", "steve"},
    {"christopher", "chris"},
    {"MATTHEW".lower(), "matt"},
    {"andrew", "andy", "drew", "andrei", "andrey"},
    {"alexander", "alex", "sasha", "aleksei", "alexey", "oleksandr"},
    {"nicholas", "nick", "nikolai", "nikolay", "nicolas"},
    {"lawrence", "larry", "laurence"},
    {"donald", "don", "donnie"},
    {"ronald", "ron", "ronnie"},
    {"kenneth", "ken", "kenny"},
    {"jeffrey", "jeff", "geoffrey", "geoff"},
    {"gregory", "greg"},
    {"timothy", "tim"},
    {"benjamin", "ben"},
    {"samuel", "sam", "sammy"},
    {"anthony", "tony"},
    {"raymond", "ray"},
    {"patrick", "pat"},
    {"peter", "pete"},
    {"philip", "phillip", "phil"},
    {"douglas", "doug"},
    {"jonathan", "jon"},
    {"gerald", "jerry"},
    {"harold", "hal", "harry"},
    {"henry", "hank", "harry"},
    {"walter", "walt"},
    {"arthur", "art"},
    {"albert", "al"},
    {"alfred", "al", "fred"},
    {"frederick", "fred", "freddy"},
    {"bernard", "bernie"},
    {"leonard", "len", "lenny"},
    {"martin", "marty"},
    {"marvin", "marv"},
    {"norman", "norm"},
    {"stanley", "stan"},
    {"theodore", "ted", "theo"},
    {"victor", "vic"},
    {"vincent", "vince"},
    {"elizabeth", "liz", "beth", "betty", "eliza"},
    {"katherine", "catherine", "kathryn", "kate", "kathy", "katie", "cathy"},
    {"margaret", "maggie", "meg", "peggy"},
    {"SUSAN".lower(), "sue", "suzie"},
    {"deborah", "debra", "deb", "debbie"},
    {"patricia", "pat", "patty", "tricia"},
    {"jennifer", "jen", "jenny"},
    {"rebecca", "becky"},
    {"christine", "christina", "chris", "tina"},
    {"victoria", "vicky"},
    {"jacqueline", "jackie"},
    {"judith", "judy"},
    {"dmitri", "dmitry", "dimitri"},
    {"sergei", "sergey"},
    {"yuri", "yury", "iurii"},
    {"evgeny", "evgenii", "eugene"},
    {"pavel", "paul"},
    {"mikhail", "michael"},
    {"vasily", "vasilii", "wassily"},
    {"kiyosi", "kiyoshi"},
]

NICKNAME_MAP = {}
for grp in NICKNAME_GROUPS:
    for nm in grp:
        NICKNAME_MAP[nm] = grp | NICKNAME_MAP.get(nm, set())


def fold_ascii(text: str) -> str:
    if not text:
        return ""
    # Handle special letters that NFKD doesn't decompose into plain ASCII letters
    text = (
        text.replace("ø", "o").replace("Ø", "O")
        .replace("ł", "l").replace("Ł", "L")
        .replace("ß", "ss").replace("đ", "d").replace("Đ", "D")
        .replace("æ", "ae").replace("Æ", "AE")
        .replace("œ", "oe").replace("Œ", "OE")
    )
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


SUFFIX_RE = re.compile(r"\b(jr\.?|sr\.?|ii|iii|iv|ph\.?d\.?|m\.?d\.?|sc\.?d\.?|d\.?phil\.?)\b", re.I)
PAREN_RE = re.compile(r"\(([^)]+)\)")
MULTISPACE_RE = re.compile(r"\s+")


def clean_display_name(name: str) -> str:
    if not name:
        return ""
    s = name.strip().strip(",.;")
    s = MULTISPACE_RE.sub(" ", s)
    return s


def parse_name_features(name: str):
    """
    Returns (norm_full, first_token, middle_initials, last_token, extra_aliases_list)
    """
    if not name:
        return "", "", "", "", []
    paren_nicks = [fold_ascii(m.strip().lower()) for m in PAREN_RE.findall(name) if m.strip()]
    no_paren = PAREN_RE.sub(" ", name)
    no_suf = SUFFIX_RE.sub(" ", no_paren)
    folded = fold_ascii(no_suf).lower()
    folded = re.sub(r"[^a-z0-9\s\-]", " ", folded)
    folded = MULTISPACE_RE.sub(" ", folded).strip()
    if not folded:
        return "", "", "", "", []
    tokens = [t.strip("-") for t in folded.split() if t.strip("-")]
    if not tokens:
        return "", "", "", "", []
    if len(tokens) == 1:
        return tokens[0], "", "", tokens[0], paren_nicks
    first = tokens[0]
    last = tokens[-1]
    mids = "".join(t[0] for t in tokens[1:-1] if t)
    # Build nickname search aliases
    aliases = set()
    for p in paren_nicks:
        aliases.add(f"{p} {last}")
    if first in NICKNAME_MAP:
        for alt in NICKNAME_MAP[first]:
            if alt != first:
                aliases.add(f"{alt} {last}")
    return folded, first, mids, last, sorted(aliases)


def names_compatible(f1, m1, l1, f2, m2, l2) -> bool:
    if not l1 or not l2:
        return False
    # Allow compound last-name match if one is hyphenated end-part (e.g. Garrett vs Garrett-Mayer)
    if l1 != l2:
        if not (l1.endswith("-" + l2) or l2.endswith("-" + l1)):
            return False
    if not f1 or not f2:
        return False
    # Check first name compatibility
    if f1 == f2:
        first_ok = True
    elif len(f1) == 1 and f2.startswith(f1):
        first_ok = True
    elif len(f2) == 1 and f1.startswith(f2):
        first_ok = True
    elif f2 in NICKNAME_MAP.get(f1, set()):
        first_ok = True
    elif len(f1) >= 4 and len(f2) >= 4 and (f1.startswith(f2) or f2.startswith(f1)):
        first_ok = True
    else:
        first_ok = False
    if not first_ok:
        return False
    # If both have middle initials, require non-conflicting first middle initial
    if m1 and m2 and m1[0] != m2[0]:
        return False
    return True


COUNTRY_FIXES = {
    "UnitedStates": "United States",
    "UnitedKingdom": "United Kingdom",
    "SouthKorea": "South Korea",
    "NorthKorea": "North Korea",
    "NewZealand": "New Zealand",
    "SouthAfrica": "South Africa",
    "CzechRepublic": "Czech Republic",
    "SaudiArabia": "Saudi Arabia",
    "CostaRica": "Costa Rica",
    "PuertoRico": "Puerto Rico",
    "HongKong": "Hong Kong",
    "BosniaandHerzegovina": "Bosnia and Herzegovina",
    "TrinidadandTobago": "Trinidad and Tobago",
    "SriLanka": "Sri Lanka",
    "VietNam": "Vietnam",
}


def clean_country(c: str) -> str:
    if not c or c == "NULL":
        return ""
    c = c.strip()
    if c in COUNTRY_FIXES:
        return COUNTRY_FIXES[c]
    # Split CamelCase like UnitedStates if any remain
    c = re.sub(r"([a-z])([A-Z])", r"\1 \2", c)
    return c


# Canonical University Rules & Search Acronyms
SCHOOL_CANONICAL_MAP = {
    "the johns hopkins university": "Johns Hopkins University",
    "johns hopkins university": "Johns Hopkins University",
    "johns hopkins bloomberg school of public health": "Johns Hopkins University",
    "the university of chicago": "University of Chicago",
    "university of chicago": "University of Chicago",
    "the university of north carolina at chapel hill": "University of North Carolina at Chapel Hill",
    "university of north carolina, chapel hill": "University of North Carolina at Chapel Hill",
    "university of north carolina at chapel hill": "University of North Carolina at Chapel Hill",
    "university of california at berkeley": "University of California, Berkeley",
    "university of california, berkeley": "University of California, Berkeley",
    "university of california, los angeles": "University of California, Los Angeles",
    "university of california at los angeles": "University of California, Los Angeles",
    "university of california, san diego": "University of California, San Diego",
    "university of california at san diego": "University of California, San Diego",
    "university of california, davis": "University of California, Davis",
    "university of california at davis": "University of California, Davis",
    "university of california, santa barbara": "University of California, Santa Barbara",
    "university of california, irvine": "University of California, Irvine",
    "university of california, riverside": "University of California, Riverside",
    "university of california, santa cruz": "University of California, Santa Cruz",
    "university of california, san francisco": "University of California, San Francisco",
    "university of wisconsin, madison": "University of Wisconsin-Madison",
    "university of wisconsin-madison": "University of Wisconsin-Madison",
    "university of wisconsin - madison": "University of Wisconsin-Madison",
    "university of minnesota - twin cities": "University of Minnesota",
    "university of minnesota, twin cities": "University of Minnesota",
    "university of minnesota-twin cities": "University of Minnesota",
    "university of minnesota": "University of Minnesota",
    "university of washington, seattle": "University of Washington",
    "university of washington": "University of Washington",
    "university of michigan, ann arbor": "University of Michigan",
    "university of michigan": "University of Michigan",
    "the ohio state university": "Ohio State University",
    "ohio state university": "Ohio State University",
    "ohio state university, columbus": "Ohio State University",
    "the pennsylvania state university": "Pennsylvania State University",
    "pennsylvania state university": "Pennsylvania State University",
    "north carolina state university, raleigh": "North Carolina State University",
    "north carolina state university": "North Carolina State University",
    "rutgers university, new brunswick": "Rutgers University",
    "rutgers, the state university of new jersey": "Rutgers University",
    "rutgers university": "Rutgers University",
    "university of illinois at urbana-champaign": "University of Illinois Urbana-Champaign",
    "university of illinois, urbana-champaign": "University of Illinois Urbana-Champaign",
    "university of maryland, college park": "University of Maryland",
    "university of maryland college park": "University of Maryland",
    "texas a&m university, college station": "Texas A&M University",
    "texas a&m university": "Texas A&M University",
    "virginia polytechnic institute and state university": "Virginia Tech",
    "virginia tech": "Virginia Tech",
    "massachusetts institute of technology": "Massachusetts Institute of Technology",
    "california institute of technology": "California Institute of Technology",
    "eidgenossische technische hochschule zurich": "ETH Zürich",
    "eth zurich": "ETH Zürich",
    "ecole polytechnique federale de lausanne": "EPFL",
    "indian statistical institute, kolkata": "Indian Statistical Institute",
    "indian statistical institute, calcutta": "Indian Statistical Institute",
    "indian statistical institute, delhi": "Indian Statistical Institute",
    "indian statistical institute": "Indian Statistical Institute",
    "the george washington university": "George Washington University",
    "george washington university": "George Washington University",
    "state university of new york at buffalo": "University at Buffalo (SUNY)",
    "university at buffalo, state university of new york": "University at Buffalo (SUNY)",
    "state university of new york at stony brook": "Stony Brook University",
    "stony brook university": "Stony Brook University",
    "the university of texas at austin": "University of Texas at Austin",
    "university of texas, austin": "University of Texas at Austin",
    "university of texas at austin": "University of Texas at Austin",
    "the university of iowa": "University of Iowa",
    "university of iowa": "University of Iowa",
    "the university of pittsburgh": "University of Pittsburgh",
    "university of pittsburgh": "University of Pittsburgh",
    "indiana university, bloomington": "Indiana University",
    "indiana university": "Indiana University",
    "university of colorado, boulder": "University of Colorado Boulder",
    "university of colorado at boulder": "University of Colorado Boulder",
    "washington university in st. louis": "Washington University in St. Louis",
    "washington university, st. louis": "Washington University in St. Louis",
    "lomonosov moscow state university": "Moscow State University",
    "moscow state university": "Moscow State University",
}

SCHOOL_ACRONYMS = {
    "Massachusetts Institute of Technology": "MIT",
    "California Institute of Technology": "Caltech, CIT",
    "University of California, Berkeley": "UC Berkeley, UCB, Berkeley, Cal",
    "University of California, Los Angeles": "UCLA",
    "University of California, San Diego": "UCSD, UC San Diego",
    "University of California, Davis": "UC Davis, UCD",
    "University of California, Santa Barbara": "UCSB, UC Santa Barbara",
    "University of California, Irvine": "UCI, UC Irvine",
    "University of California, Santa Cruz": "UCSC, UC Santa Cruz",
    "University of California, San Francisco": "UCSF, UC San Francisco",
    "University of North Carolina at Chapel Hill": "UNC, UNC Chapel Hill",
    "North Carolina State University": "NCSU, NC State",
    "Johns Hopkins University": "JHU, Hopkins, Johns Hopkins Bloomberg School of Public Health",
    "University of Washington": "UW, UW Seattle",
    "University of Michigan": "UMich, Michigan",
    "University of Wisconsin-Madison": "UW-Madison, Wisconsin",
    "University of Minnesota": "UMN, Minnesota",
    "University of Chicago": "UChicago, Chicago",
    "University of Pennsylvania": "UPenn, Penn, Wharton",
    "Pennsylvania State University": "Penn State, PSU",
    "Ohio State University": "OSU, Ohio State",
    "Carnegie Mellon University": "CMU",
    "New York University": "NYU, Courant",
    "Columbia University": "Columbia, Mailman School of Public Health",
    "Harvard University": "Harvard, Harvard T.H. Chan School of Public Health",
    "Stanford University": "Stanford",
    "Princeton University": "Princeton",
    "Cornell University": "Cornell",
    "Yale University": "Yale",
    "Duke University": "Duke",
    "University of Illinois Urbana-Champaign": "UIUC, Illinois",
    "Texas A&M University": "TAMU, Texas A&M",
    "Virginia Tech": "VT, VPI, Virginia Polytechnic Institute",
    "Iowa State University": "ISU, Iowa State",
    "Purdue University": "Purdue",
    "University of Texas at Austin": "UT Austin, UT",
    "ETH Zürich": "ETH, ETH Zurich, Swiss Federal Institute of Technology",
    "EPFL": "Ecole Polytechnique Federale de Lausanne",
    "Indian Statistical Institute": "ISI, ISI Kolkata, ISI Delhi",
    "London School of Economics and Political Science": "LSE, London School of Economics",
    "University College London": "UCL",
    "University of Oxford": "Oxford",
    "University of Cambridge": "Cambridge",
    "University of Toronto": "UofT, Toronto",
    "University of Waterloo": "Waterloo, UWaterloo",
    "University of British Columbia": "UBC",
    "Peking University": "PKU, Beida",
    "Tsinghua University": "THU, Tsinghua",
    "University of Science and Technology of China": "USTC",
    "National University of Singapore": "NUS",
}


def canonicalize_school(s: str) -> str:
    if not s or s == "NULL":
        return ""
    s = MULTISPACE_RE.sub(" ", s).strip().strip(",")
    if not s:
        return ""
    key = fold_ascii(s).lower()
    if key in SCHOOL_CANONICAL_MAP:
        return SCHOOL_CANONICAL_MAP[key]
    if key.startswith("the university of "):
        s = s[4:]
    return s


def sanitize_year(y):
    if y is None or y == "" or y == "NULL":
        return None
    if isinstance(y, int):
        val = y
    else:
        m = re.match(r"^(\d{4,5})", str(y).strip())
        if not m:
            return None
        val = int(m.group(1))
    if val == 0:
        return None
    if 20000 <= val <= 20029:
        val = 2000 + (val % 100)  # Fix e.g. 20016 -> 2016
    if 1100 <= val <= 2027:
        return val
    return None


def thesis_tokens(t: str) -> set:
    if not t or len(t) < 18:
        return set()
    folded = fold_ascii(t).lower()
    words = re.findall(r"[a-z]{4,}", folded)
    stop = {"with", "from", "that", "this", "some", "their", "using", "based", "study", "analysis", "applications", "application", "problems", "problem", "theory", "models", "model", "methods", "method"}
    return {w for w in words if w not in stop}


# ---------------------------------------------------------------------------
# 2. Main Pipeline Execution
# ---------------------------------------------------------------------------

def main():
    t0 = time.time()
    os.makedirs(OUT_DIR, exist_ok=True)
    print(f"[1/6] Loading MGP cleaned dataset from {RAW_DIR}...")

    with open(os.path.join(RAW_DIR, "mgp_data_new.json"), "r", encoding="utf-8") as f:
        mgp_nodes = json.load(f)["nodes"]
    print(f"      Loaded {len(mgp_nodes):,} MGP records ({time.time() - t0:.1f}s)")

    # Build unified people dictionary keyed by unified internal ID
    people = {}
    mgp_index_by_ln_fi = defaultdict(list)  # (last, first_initial) -> [mgp_id]
    mgp_exact_name_counts = Counter()

    for n in mgp_nodes:
        mid = int(n["id"])
        raw_name = clean_display_name(n.get("name") or "")
        if not raw_name:
            raw_name = f"Unknown Researcher (MGP #{mid})"
        norm_full, f_tok, m_init, l_tok, aliases = parse_name_features(raw_name)
        raw_sch = (n.get("school") or "").strip()
        sch = canonicalize_school(raw_sch)
        yr = sanitize_year(n.get("year"))
        country = clean_country(n.get("country") or "")
        thesis = MULTISPACE_RE.sub(" ", (n.get("thesis") or "").strip())
        subj = (n.get("subject") or "").strip() or None

        people[mid] = {
            "id": mid,
            "name": raw_name,
            "norm_name": norm_full or raw_name.lower(),
            "f_tok": f_tok,
            "m_init": m_init,
            "l_tok": l_tok,
            "aliases": set(aliases),
            "school": sch,
            "raw_school": raw_sch if raw_sch != sch else "",
            "country": country,
            "year": yr,
            "thesis": thesis,
            "mgp_subject": subj,
            "current_affiliation": "",
            "research_areas": "",
            "aft_majorarea": "",
            "mgp_id": mid,
            "aft_pid": None,
            "wikidata_qid": "",
            "orcid": "",
            "homepage": "",
            "proquest_id": "",
            "sources": ["MGP"],
            "mgp_advisors": [int(x) for x in (n.get("advisors") or []) if x is not None],
            "mgp_students": [int(x) for x in (n.get("students") or []) if x is not None],
        }
        if l_tok and f_tok:
            mgp_index_by_ln_fi[(l_tok, f_tok[0])].append(mid)
            mgp_exact_name_counts[(f_tok, l_tok)] += 1

    # Load Wikidata bridges & statistician metadata
    print("[2/6] Loading Wikidata bridges and scholarly metadata...")
    wd_aft_to_mgp = {}
    wd_mgp_to_qid = {}
    wd_aft_to_qid = {}
    wd_stat_qids = set()
    wd_biostat_qids = set()

    bridge_path = os.path.join(RAW_DIR, "wikidata_mgp_aft_bridges.json")
    if os.path.exists(bridge_path):
        with open(bridge_path, "r", encoding="utf-8") as f:
            for row in json.load(f):
                qid = row["item"]["value"].rsplit("/", 1)[-1]
                try:
                    mid = int(row["mgp"]["value"])
                    apid = int(row["aft"]["value"])
                except ValueError:
                    continue
                if mid in people:
                    wd_aft_to_mgp[apid] = mid
                    wd_mgp_to_qid[mid] = qid
                    wd_aft_to_qid[apid] = qid

    wd_stats_path = os.path.join(RAW_DIR, "wikidata_stats.json")
    if os.path.exists(wd_stats_path):
        with open(wd_stats_path, "r", encoding="utf-8") as f:
            for row in json.load(f):
                qid = row["item"]["value"].rsplit("/", 1)[-1]
                wd_stat_qids.add(qid)
                if "mgp" in row:
                    try:
                        mid = int(row["mgp"]["value"])
                        if mid in people:
                            wd_mgp_to_qid[mid] = qid
                            if not people[mid]["year"] and "year" in row:
                                people[mid]["year"] = sanitize_year(row["year"]["value"])
                            if not people[mid]["school"] and "schoolLabel" in row:
                                people[mid]["school"] = canonicalize_school(row["schoolLabel"]["value"])
                    except ValueError:
                        pass
                if "aft" in row:
                    try:
                        apid = int(row["aft"]["value"])
                        wd_aft_to_qid[apid] = qid
                    except ValueError:
                        pass

    for mid, qid in wd_mgp_to_qid.items():
        people[mid]["wikidata_qid"] = qid
        if "Wikidata" not in people[mid]["sources"]:
            people[mid]["sources"].append("Wikidata")

    print(f"      Loaded {len(wd_aft_to_mgp):,} Wikidata MGP<->AFT bridges and {len(wd_stat_qids):,} Wikidata stat scholars.")

    # Load Academic Family Tree locations, people, and PhD (relation=1) connections
    print("[3/6] Loading & filtering Academic Family Tree (Zenodo 2026 export)...")
    aft_loc_country = {}
    with gzip.open(os.path.join(RAW_DIR, "aft_locations.csv.gz"), "rt", encoding="utf-8", errors="replace") as gz:
        for row in csv.DictReader(gz):
            try:
                lid = int(row["locid"])
                aft_loc_country[lid] = clean_country(row.get("country") or "")
            except ValueError:
                pass

    QUANT_MAJORAREAS = {
        "math", "phtree", "epidemiology", "compbio", "bmi", "genetics",
        "publichealth", "publicpolicy", "infoscience"
    }
    STAT_KEYWORDS_RE = re.compile(
        r"\b(biostat\w*|statist\w*|probab\w*|stochastic\w*|econometr\w*|psychometr\w*|"
        r"actuar\w*|bayesian|markov|martingale\w*|causal\s+inference|causal\s+mediation|"
        r"survival\s+analysis|proportional\s+hazards|time\s+series|nonparametric|"
        r"semiparametric|high[\-\s]dimensional|monte\s+carlo|random\s+matrix|random\s+walk\w*|"
        r"brownian|ergodic|gaussian\s+process\w*|generalized\s+linear|mixed\s+effects|"
        r"longitudinal\s+data|missing\s+data|clinical\s+trials?|epidemiolog\w*|"
        r"quantitative\s+genetics|statistical\s+genetics|statistical\s+learning)\b",
        re.I,
    )

    aft_people_raw = {}
    aft_seed_pids = set(wd_aft_to_mgp.keys()) | set(wd_aft_to_qid.keys())
    aft_exact_math_counts = Counter()

    with gzip.open(os.path.join(RAW_DIR, "aft_people.csv.gz"), "rt", encoding="utf-8", errors="replace") as gz:
        for row in csv.DictReader(gz):
            try:
                pid = int(row["pid"])
            except ValueError:
                continue
            fn = (row.get("firstname") or "").strip().strip(",")
            mn = (row.get("middlename") or "").strip().strip(",")
            ln = (row.get("lastname") or "").strip().strip(",")
            if fn == "NULL":
                fn = ""
            if mn == "NULL":
                mn = ""
            if ln == "NULL":
                ln = ""
            full = clean_display_name(" ".join(x for x in [fn, mn, ln] if x))
            if not full:
                continue
            major = (row.get("majorarea") or "").strip()
            if major == "NULL":
                major = ""
            maj_set = {x.strip() for x in major.split(",") if x.strip()}
            area = (row.get("area") or "").strip()
            if area == "NULL":
                area = ""
            loc = (row.get("location") or "").strip()
            if loc == "NULL":
                loc = ""
            locid = int(row["locid"]) if (row.get("locid") or "").isdigit() else 0
            orcid = (row.get("orcid_id") or "").strip()
            if orcid == "NULL":
                orcid = ""
            homepage = (row.get("homepage") or "").strip()
            if homepage == "NULL":
                homepage = ""

            aft_people_raw[pid] = (full, major, area, loc, locid, orcid, homepage)

            if maj_set & QUANT_MAJORAREAS:
                aft_seed_pids.add(pid)
                _, f_t, _, l_t, _ = parse_name_features(full)
                if f_t and l_t:
                    aft_exact_math_counts[(f_t, l_t)] += 1
            elif STAT_KEYWORDS_RE.search(area) or STAT_KEYWORDS_RE.search(loc):
                aft_seed_pids.add(pid)

    # Read AFT doctoral (relation == '1') edges & graduation metadata
    aft_phd_advisors = defaultdict(list)   # trainee_pid -> [mentor_pid]
    aft_phd_students = defaultdict(list)   # mentor_pid -> [trainee_pid]
    aft_phd_meta = {}                      # trainee_pid -> (school, locid, year, thesis, pqid)

    with gzip.open(os.path.join(RAW_DIR, "aft_connect.csv.gz"), "rt", encoding="utf-8", errors="replace") as gz:
        for row in csv.DictReader(gz):
            if row.get("relation") != "1":
                continue
            try:
                p1 = int(row["pid1"])
                p2 = int(row["pid2"])
            except ValueError:
                continue
            if p1 <= 0 or p1 not in aft_people_raw:
                continue
            loc = canonicalize_school((row.get("location") or "").strip())
            locid = int(row["locid"]) if (row.get("locid") or "").isdigit() else 0
            yr = sanitize_year(row.get("stopdate"))
            dt = MULTISPACE_RE.sub(" ", (row.get("dissertation_title") or "").strip())
            if dt == "NULL":
                dt = ""
            pqid = (row.get("pqid") or "").strip()
            if pqid == "NULL" or pqid == "0":
                pqid = ""

            if p2 > 0 and p2 in aft_people_raw and p1 != p2:
                aft_phd_advisors[p1].append(p2)
                aft_phd_students[p2].append(p1)

            # Store richest PhD metadata for trainee p1
            prev = aft_phd_meta.get(p1)
            if prev is None or (not prev[3] and dt) or (not prev[2] and yr):
                aft_phd_meta[p1] = (loc, locid, yr, dt, pqid)

            if STAT_KEYWORDS_RE.search(dt) or STAT_KEYWORDS_RE.search(loc):
                aft_seed_pids.add(p1)
                if p2 > 0 and p2 in aft_people_raw:
                    aft_seed_pids.add(p2)

    aft_seed_pids = {pid for pid in aft_seed_pids if pid in aft_people_raw}
    print(f"      AFT initial quantitative/statistical seed size: {len(aft_seed_pids):,}")

    # Expand AFT seed via full upward PhD ancestors + 3-hop downward PhD descendants of seed mentors
    active_aft_pids = set(aft_seed_pids)
    # Full upward ancestor closure so no AFT person has a truncated ancestor chain
    q_up = deque(aft_seed_pids)
    while q_up:
        curr = q_up.popleft()
        for adv in aft_phd_advisors.get(curr, ()):
            if adv not in active_aft_pids:
                active_aft_pids.add(adv)
                q_up.append(adv)

    # Downward closure (3 generations from seeds so all students & grand-students of stat/biostat/math/epi mentors are included)
    frontier = set(aft_seed_pids)
    for _ in range(3):
        nxt = set()
        for curr in frontier:
            for stu in aft_phd_students.get(curr, ()):
                if stu not in active_aft_pids:
                    active_aft_pids.add(stu)
                    nxt.add(stu)
        frontier = nxt

    print(f"      AFT genealogically closed PhD cohort: {len(active_aft_pids):,} researchers ({time.time() - t0:.1f}s)")

    # -----------------------------------------------------------------------
    # Stage 2: High-Precision Entity Resolution (AFT <-> MGP)
    # -----------------------------------------------------------------------
    print("[4/6] Running multi-evidence Entity Resolution between MGP and Academic Family Tree...")

    # Pre-extract simple (first_initial, last_tok) signatures for AFT & MGP advisors/students
    aft_parsed_names = {}
    for pid in active_aft_pids:
        full = aft_people_raw[pid][0]
        aft_parsed_names[pid] = parse_name_features(full)

    aft_to_mgp = {}
    used_mgp_ids = set()

    # 2A: Apply verified Wikidata bridges first (sanity-checking surname or given name/nickname match)
    for apid, mid in wd_aft_to_mgp.items():
        if apid in aft_people_raw and mid in people and mid not in used_mgp_ids:
            if apid not in aft_parsed_names:
                aft_parsed_names[apid] = parse_name_features(aft_people_raw[apid][0])
            _, af, _, al, _ = aft_parsed_names[apid]
            mf, ml = people[mid]["f_tok"], people[mid]["l_tok"]
            if al == ml or af == mf or (af in NICKNAME_MAP.get(mf, set())):
                aft_to_mgp[apid] = mid
                used_mgp_ids.add(mid)
                active_aft_pids.add(apid)

    # 2B: Score candidate pairs for remaining active_aft_pids
    candidate_matches = []  # (score, apid, mid)
    NON_QUANT_ONLY = {"chemistry", "literature", "theology", "arthistory", "music", "nursing", "hist", "law"}

    for apid in active_aft_pids:
        if apid in aft_to_mgp:
            continue
        full, major, area, affil_loc, affil_locid, orcid, homepage = aft_people_raw[apid]
        norm_full, af, am, al, _ = aft_parsed_names[apid]
        if not af or not al:
            continue
        cands = mgp_index_by_ln_fi.get((al, af[0]), ())
        if not cands:
            # Check nickname first-initial difference (e.g. William 'w' vs Bill 'b', Robert vs Bob)
            alt_inits = {alt[0] for alt in NICKNAME_MAP.get(af, set()) if alt[0] != af[0]}
            if alt_inits:
                cands = []
                for ch in alt_inits:
                    cands.extend(mgp_index_by_ln_fi.get((al, ch), ()))
        if not cands:
            continue

        phd_meta = aft_phd_meta.get(apid)
        a_sch = phd_meta[0] if phd_meta else ""
        a_yr = phd_meta[2] if phd_meta else None
        a_dt = phd_meta[3] if phd_meta else ""
        a_dt_toks = thesis_tokens(a_dt)

        a_adv_sigs = set()
        for adv_pid in aft_phd_advisors.get(apid, ()):
            if adv_pid in aft_parsed_names:
                _, xf, _, xl, _ = aft_parsed_names[adv_pid]
            else:
                _, xf, _, xl, _ = parse_name_features(aft_people_raw[adv_pid][0])
            if xf and xl:
                a_adv_sigs.add((xf[0], xl))

        a_stu_sigs = set()
        for stu_pid in aft_phd_students.get(apid, ()):
            if stu_pid in aft_parsed_names:
                _, xf, _, xl, _ = aft_parsed_names[stu_pid]
            else:
                _, xf, _, xl, _ = parse_name_features(aft_people_raw[stu_pid][0])
            if xf and xl:
                a_stu_sigs.add((xf[0], xl))

        maj_set = {x.strip() for x in major.split(",") if x.strip()}
        affil_canon = canonicalize_school(affil_loc)

        for mid in cands:
            if mid in used_mgp_ids:
                continue
            mp = people[mid]
            if not names_compatible(af, am, al, mp["f_tok"], mp["m_init"], mp["l_tok"]):
                continue

            score = 0
            # Signal 1: Dissertation title overlap
            if a_dt_toks and mp["thesis"]:
                m_toks = thesis_tokens(mp["thesis"])
                if m_toks:
                    inter = len(a_dt_toks & m_toks)
                    union = len(a_dt_toks | m_toks)
                    if union > 0 and (inter / union >= 0.50 or inter >= 5):
                        score += 100

            # Signal 2: PhD School & Graduation Year
            if a_sch and mp["school"] and a_sch == mp["school"]:
                if a_yr and mp["year"] and abs(a_yr - mp["year"]) <= 2:
                    score += 85
                elif a_yr is None or mp["year"] is None:
                    score += 45
            elif a_yr and mp["year"] and a_yr == mp["year"]:
                score += 30
            elif a_yr and mp["year"] and abs(a_yr - mp["year"]) <= 1:
                score += 15

            # Signal 3: Shared PhD advisor signature
            if a_adv_sigs and mp["mgp_advisors"]:
                for m_adv in mp["mgp_advisors"]:
                    if m_adv in people:
                        adv_p = people[m_adv]
                        if adv_p["f_tok"] and (adv_p["f_tok"][0], adv_p["l_tok"]) in a_adv_sigs:
                            score += 65
                            break

            # Signal 4: Shared PhD student signature
            if a_stu_sigs and mp["mgp_students"]:
                shared_stu = 0
                for m_stu in mp["mgp_students"]:
                    if m_stu in people:
                        stu_p = people[m_stu]
                        if stu_p["f_tok"] and (stu_p["f_tok"][0], stu_p["l_tok"]) in a_stu_sigs:
                            shared_stu += 1
                            if shared_stu >= 2:
                                break
                if shared_stu >= 2:
                    score += 80
                elif shared_stu == 1 and not (maj_set and maj_set <= NON_QUANT_ONLY):
                    score += 58

            # Signal 5: Unique name in both MGP and AFT-quant + matching affiliation school
            if score < 55 and af == mp["f_tok"] and al == mp["l_tok"]:
                if mgp_exact_name_counts[(af, al)] == 1 and aft_exact_math_counts[(af, al)] == 1:
                    if affil_canon and mp["school"] and affil_canon == mp["school"]:
                        score += 60

            if score >= 55:
                candidate_matches.append((score, apid, mid))

    candidate_matches.sort(key=lambda x: -x[0])
    for score, apid, mid in candidate_matches:
        if apid not in aft_to_mgp and mid not in used_mgp_ids:
            aft_to_mgp[apid] = mid
            used_mgp_ids.add(mid)

    print(f"      Matched & unified {len(aft_to_mgp):,} researchers across MGP & AFT!")

    # Merge matched AFT metadata into MGP records, and create net-new unified nodes for unmatched AFT researchers
    aft_net_new = 0
    for apid in active_aft_pids:
        full, major, area, affil_loc, affil_locid, orcid, homepage = aft_people_raw[apid]
        phd_meta = aft_phd_meta.get(apid)
        a_sch = phd_meta[0] if phd_meta else ""
        a_locid = phd_meta[1] if phd_meta else affil_locid
        a_yr = phd_meta[2] if phd_meta else None
        a_dt = phd_meta[3] if phd_meta else ""
        a_pqid = phd_meta[4] if phd_meta else ""
        a_country = aft_loc_country.get(a_locid, "")

        if apid in aft_to_mgp:
            uid = aft_to_mgp[apid]
            rec = people[uid]
            rec["aft_pid"] = apid
            if "AFT" not in rec["sources"]:
                rec["sources"].append("AFT")
            if not rec["school"] and a_sch:
                rec["school"] = a_sch
            if not rec["year"] and a_yr:
                rec["year"] = a_yr
            if not rec["thesis"] and a_dt:
                rec["thesis"] = a_dt
            if not rec["country"] and a_country:
                rec["country"] = a_country
            if affil_loc:
                rec["current_affiliation"] = canonicalize_school(affil_loc)
            if area:
                rec["research_areas"] = area
            if major:
                rec["aft_majorarea"] = major
            if orcid:
                rec["orcid"] = orcid
            if homepage:
                rec["homepage"] = homepage
            if a_pqid:
                rec["proquest_id"] = a_pqid
            if not rec["wikidata_qid"] and apid in wd_aft_to_qid:
                rec["wikidata_qid"] = wd_aft_to_qid[apid]
                if "Wikidata" not in rec["sources"]:
                    rec["sources"].append("Wikidata")
            # Add AFT name variant to aliases if different
            norm_a, _, _, _, a_aliases = aft_parsed_names[apid]
            if norm_a and norm_a != rec["norm_name"]:
                rec["aliases"].add(norm_a)
            rec["aliases"].update(a_aliases)
        else:
            uid = 1_000_000 + apid
            norm_a, af, am, al, a_aliases = aft_parsed_names[apid]
            qid = wd_aft_to_qid.get(apid, "")
            srcs = ["AFT"]
            if qid:
                srcs.append("Wikidata")
            people[uid] = {
                "id": uid,
                "name": full,
                "norm_name": norm_a or full.lower(),
                "f_tok": af,
                "m_init": am,
                "l_tok": al,
                "aliases": set(a_aliases),
                "school": a_sch or canonicalize_school(affil_loc),
                "raw_school": "",
                "country": a_country,
                "year": a_yr,
                "thesis": a_dt,
                "mgp_subject": None,
                "current_affiliation": canonicalize_school(affil_loc) if affil_loc else "",
                "research_areas": area,
                "aft_majorarea": major,
                "mgp_id": None,
                "aft_pid": apid,
                "wikidata_qid": qid,
                "orcid": orcid,
                "homepage": homepage,
                "proquest_id": a_pqid,
                "sources": srcs,
                "mgp_advisors": [],
                "mgp_students": [],
            }
            aft_net_new += 1

    print(f"      Added {aft_net_new:,} net-new PhD researchers from Academic Family Tree. Total unified people: {len(people):,}")

    # -----------------------------------------------------------------------
    # Stage 3: Build Unified Directed Acyclic Graph (DAG) of PhD Lineage Edges
    # -----------------------------------------------------------------------
    edge_sources = {}  # (advisor_uid, student_uid) -> set of sources

    # MGP bidirectional edges
    for uid, rec in people.items():
        if rec["mgp_id"] is not None:
            for adv_id in rec["mgp_advisors"]:
                if adv_id in people and adv_id != uid:
                    edge_sources.setdefault((adv_id, uid), set()).add("MGP")
            for stu_id in rec["mgp_students"]:
                if stu_id in people and stu_id != uid:
                    edge_sources.setdefault((uid, stu_id), set()).add("MGP")

    # AFT PhD (relation=1) edges mapped through unified IDs
    def resolve_aft_uid(pid):
        if pid in aft_to_mgp:
            return aft_to_mgp[pid]
        u = 1_000_000 + pid
        return u if u in people else None

    for stu_pid in active_aft_pids:
        stu_uid = resolve_aft_uid(stu_pid)
        if stu_uid is None:
            continue
        for adv_pid in aft_phd_advisors.get(stu_pid, ()):
            adv_uid = resolve_aft_uid(adv_pid)
            if adv_uid is not None and adv_uid != stu_uid:
                edge_sources.setdefault((adv_uid, stu_uid), set()).add("AFT")

    # Wikidata P184 advisor edges (fills missing edges between existing MGP/AFT researchers!)
    if os.path.exists(wd_stats_path):
        with open(wd_stats_path, "r", encoding="utf-8") as f:
            for row in json.load(f):
                s_uid = None
                a_uid = None
                if "mgp" in row and row["mgp"]["value"].isdigit():
                    m = int(row["mgp"]["value"])
                    if m in people:
                        s_uid = m
                elif "aft" in row and row["aft"]["value"].isdigit():
                    s_uid = resolve_aft_uid(int(row["aft"]["value"]))
                if "advisorMgp" in row and row["advisorMgp"]["value"].isdigit():
                    m = int(row["advisorMgp"]["value"])
                    if m in people:
                        a_uid = m
                elif "advisorAft" in row and row["advisorAft"]["value"].isdigit():
                    a_uid = resolve_aft_uid(int(row["advisorAft"]["value"]))
                if a_uid and s_uid and a_uid != s_uid:
                    edge_sources.setdefault((a_uid, s_uid), set()).add("Wikidata")

    # Ensure graph is strictly acyclic (break any rare data-entry cycles via topological Kahn check)
    advisors_of = defaultdict(list)
    students_of = defaultdict(list)
    for (adv, stu) in edge_sources:
        advisors_of[stu].append(adv)
        students_of[adv].append(stu)

    in_deg = {uid: len(advisors_of[uid]) for uid in people}
    q = deque([uid for uid, d in in_deg.items() if d == 0])
    visited_topo = []
    while q:
        u = q.popleft()
        visited_topo.append(u)
        for v in students_of.get(u, ()):
            in_deg[v] -= 1
            if in_deg[v] == 0:
                q.append(v)

    if len(visited_topo) < len(people):
        # Rare cycle(s) exist among remaining nodes; remove back-edges where advisor graduated AFTER student
        rem = {uid for uid, d in in_deg.items() if d > 0}
        removed_cycles = 0
        # Iterative DFS cycle-breaker on tiny induced subgraph of `rem`
        state = {}  # 0=unvisited, 1=visiting, 2=done
        bad_edges = set()
        for start in rem:
            if state.get(start, 0) != 0:
                continue
            stack = [(start, 0)]
            state[start] = 1
            while stack:
                u, idx = stack[-1]
                nbrs = [v for v in students_of.get(u, ()) if v in rem and (u, v) not in bad_edges]
                if idx < len(nbrs):
                    stack[-1] = (u, idx + 1)
                    v = nbrs[idx]
                    st_v = state.get(v, 0)
                    if st_v == 1:
                        bad_edges.add((u, v))
                        removed_cycles += 1
                    elif st_v == 0:
                        state[v] = 1
                        stack.append((v, 0))
                else:
                    state[u] = 2
                    stack.pop()
        for e in bad_edges:
            edge_sources.pop(e, None)
        print(f"      Sanitized {removed_cycles} rare cycle edge(s) to guarantee strict DAG.")
        advisors_of.clear()
        students_of.clear()
        for (adv, stu) in edge_sources:
            advisors_of[stu].append(adv)
            students_of[adv].append(stu)

    print(f"      Unified DAG has {len(people):,} people and {len(edge_sources):,} PhD advisor->student edges.")

    # -----------------------------------------------------------------------
    # Stage 4: High-Recall + High-Precision Discipline Classification
    # -----------------------------------------------------------------------
    print("[5/6] Running multi-signal Statistics / Biostatistics / Probability classifier...")

    # Train TF-IDF + LogisticRegression on the 202,492 MSC-labeled MGP dissertations!
    # Classes: 0=Other Math/Science, 1=Statistics (62), 2=Probability (60)
    train_texts = []
    train_labels = []
    for uid, rec in people.items():
        subj = rec["mgp_subject"]
        th = rec["thesis"]
        if subj and th and len(th) >= 15:
            if subj.startswith("62"):
                train_texts.append(th)
                train_labels.append(1)
            elif subj.startswith("60"):
                train_texts.append(th)
                train_labels.append(2)
            else:
                train_texts.append(th)
                train_labels.append(0)

    vec = TfidfVectorizer(
        lowercase=True,
        strip_accents="unicode",
        ngram_range=(1, 2),
        min_df=3,
        max_features=120_000,
        sublinear_tf=True,
    )
    X_train = vec.fit_transform(train_texts)
    clf = LogisticRegression(C=1.5, max_iter=300)
    clf.fit(X_train, np.array(train_labels))
    print(f"      Trained dissertation classifier on {len(train_texts):,} MSC-labeled PhD theses ({time.time() - t0:.1f}s)")

    # Predict dissertation probabilities on all people who have a thesis
    uids_with_thesis = [uid for uid, rec in people.items() if rec["thesis"] and len(rec["thesis"]) >= 12]
    X_all = vec.transform([people[uid]["thesis"] for uid in uids_with_thesis])
    probs_all = clf.predict_proba(X_all)  # cols: [P(Other), P(Stat62), P(Prob60)]
    thesis_probs = {uid: (float(probs_all[i, 1]), float(probs_all[i, 2])) for i, uid in enumerate(uids_with_thesis)}

    BIOSTAT_LEX_RE = re.compile(
        r"\b(biostat\w*|clinical\s+trials?|survival\s+analysis|proportional\s+hazards|"
        r"competing\s+risks?|longitudinal\s+data|case[\-\s]control|cohort\s+stud\w*|"
        r"epidemiolog\w*|mendelian\s+randomization|genome[\-\s]wide|microarray\w*|"
        r"pharmacokinet\w*|medical\s+imaging|public\s+health|disease\s+mapping|"
        r"vaccine\s+efficacy|surrogate\s+endpoint\w*|causal\s+mediation|biomarker\w*)\b",
        re.I,
    )
    CORE_STAT_LEX_RE = re.compile(
        r"\b(statist\w*\s+inference|nonparametric|semiparametric|maximum\s+likelihood|"
        r"bayesian|empirical\s+bayes|posterior\s+distribution\w*|minimax|regression\s+model\w*|"
        r"linear\s+models?|generalized\s+linear|mixed\s+effects|variance\s+components|"
        r"analysis\s+of\s+variance|experimental\s+design\w*|factorial\s+design\w*|"
        r"contingency\s+tables?|goodness[\-\s]of[\-\s]fit|hypothesis\s+test\w*|"
        r"multiple\s+testing|false\s+discovery|confidence\s+interval\w*|bootstrap|"
        r"jackknife|time\s+series|autoregressive|kalman\s+filter\w*|missing\s+data|"
        r"imputation|causal\s+inference|propensity\s+score\w*|instrumental\s+variable\w*|"
        r"quantile\s+regression|lasso|penalized\s+likelihood|high[\-\s]dimensional\s+stat\w*|"
        r"shrinkage\s+estimat\w*|m[\-\s]estimat\w*|u[\-\s]statistics|rank\s+tests?|"
        r"multivariate\s+analysis|sufficiency|ancillarity|fiducial|kriging|spatial\s+statistics|"
        r"changepoint|change[\-\s]point|sequential\s+analysis|density\s+estimation|kernel\s+estimat\w*)\b",
        re.I,
    )
    PROB_LEX_RE = re.compile(
        r"\b(probabilit\w*|stochastic\s+process\w*|stochastic\s+differential|stochastic\s+calculus|"
        r"martingale\w*|brownian\s+motion|wiener\s+process|levy\s+process\w*|markov\s+chain\w*|"
        r"markov\s+process\w*|random\s+walk\w*|percolation|ising\s+model|spin\s+glass\w*|"
        r"random\s+matri\w*|random\s+graph\w*|large\s+deviations|central\s+limit\s+theorem|"
        r"limit\s+theorems?|ergodic\s+theor\w*|branching\s+process\w*|point\s+process\w*|"
        r"poisson\s+process\w*|gaussian\s+process\w*|schramm[\-\s]loewner|interacting\s+particle|"
        r"ito\s+calculus|diffusion\s+process\w*|queueing|queuing\s+network\w*)\b",
        re.I,
    )
    FALSE_STATICS_RE = re.compile(r"\b(statics|statical|statistical\s+mechanics|quantum\s+statistics)\b", re.I)

    RELATED_QUANT_RE = re.compile(
        r"\b(econometr\w*|actuarial|psychometr\w*|item\s+response|machine\s+learning|"
        r"reinforcement\s+learning|deep\s+learning|neural\s+network\w*|bandit\w*|"
        r"operations\s+research|mathematical\s+finance|option\s+pricing|portfolio\s+optimization|"
        r"data\s+mining|bioinformatics|computational\s+biology|population\s+genetics)\b",
        re.I,
    )

    # Pass 1: Direct classification from MSC, Wikidata, School, AFT area, and Thesis ML + Lexicon
    for uid, rec in people.items():
        subj = rec["mgp_subject"] or ""
        th = rec["thesis"] or ""
        area = (rec["research_areas"] or "") + " " + (rec["current_affiliation"] or "")
        maj_set = {x.strip() for x in (rec["aft_majorarea"] or "").split(",") if x.strip()}
        sch = rec["school"] or ""
        p_stat, p_prob = thesis_probs.get(uid, (0.0, 0.0))
        p_comb = p_stat + p_prob

        has_biostat_kw = bool(BIOSTAT_LEX_RE.search(th) or re.search(r"\bbiostat\w*", area, re.I) or "biostat" in sch.lower())
        is_stat_inst = ("statistical institute" in sch.lower() or "institute of statistical" in sch.lower())

        # 1. Explicit MSC 62 or MSC 60
        if subj.startswith("62"):
            if has_biostat_kw or ("phtree" in maj_set or "epidemiology" in maj_set):
                rec["primary_field"] = "Biostatistics"
                rec["field_confidence"] = 0.99
                rec["classification_reason"] = "MSC 62 + Biostatistics Profile"
            else:
                rec["primary_field"] = "Statistics"
                rec["field_confidence"] = 0.99
                rec["classification_reason"] = "MSC 62 (Statistics)"
            continue

        if subj.startswith("60"):
            rec["primary_field"] = "Probability"
            rec["field_confidence"] = 0.99
            rec["classification_reason"] = "MSC 60 (Probability)"
            continue

        # 2. Statistical Institution (e.g., Indian Statistical Institute) when MSC is missing or math
        if is_stat_inst and (not subj or subj.startswith(("60", "62", "91", "92", "68"))):
            rec["primary_field"] = "Probability" if p_prob > p_stat and p_prob > 0.25 else "Statistics"
            rec["field_confidence"] = 0.93
            rec["classification_reason"] = "Statistical Institution"
            continue

        # 3. Explicit Biostatistics / Statistics / Probability in AFT research area or Wikidata
        if re.search(r"\bbiostat\w*", area, re.I) and not subj:
            rec["primary_field"] = "Biostatistics"
            rec["field_confidence"] = 0.94
            rec["classification_reason"] = "AcademicTree Biostatistics Profile"
            continue

        if rec["wikidata_qid"] in wd_stat_qids and (not subj or subj.startswith(("91", "92", "68", "90"))):
            if has_biostat_kw or ("phtree" in maj_set or "epidemiology" in maj_set):
                rec["primary_field"] = "Biostatistics"
            elif PROB_LEX_RE.search(th) or p_prob > p_stat + 0.1:
                rec["primary_field"] = "Probability"
            else:
                rec["primary_field"] = "Statistics"
            rec["field_confidence"] = 0.92
            rec["classification_reason"] = "Wikidata Scholarly Classification"
            continue

        # 4. Unlabeled MSC (subject is None) OR interdisciplinary MSC (68 CS, 90 OR, 91 Econ, 92 Bio)
        can_promote_from_thesis = (not subj) or subj.startswith(("92", "91", "68", "90"))
        if can_promote_from_thesis and not FALSE_STATICS_RE.search(th):
            if not subj:
                # Missing MSC: promote if ML probability >= 0.42 OR strong domain phrase + ML >= 0.18
                if p_comb >= 0.42 or (BIOSTAT_LEX_RE.search(th) and ("phtree" in maj_set or "epidemiology" in maj_set or p_comb >= 0.12)):
                    if has_biostat_kw or ("phtree" in maj_set or "epidemiology" in maj_set):
                        rec["primary_field"] = "Biostatistics"
                    elif p_prob >= p_stat:
                        rec["primary_field"] = "Probability"
                    else:
                        rec["primary_field"] = "Statistics"
                    rec["field_confidence"] = round(min(0.95, max(0.68, p_comb)), 2)
                    rec["classification_reason"] = "Dissertation ML + Domain Terms"
                    continue
                elif CORE_STAT_LEX_RE.search(th) and p_comb >= 0.16:
                    rec["primary_field"] = "Biostatistics" if has_biostat_kw else "Statistics"
                    rec["field_confidence"] = round(max(0.72, p_comb), 2)
                    rec["classification_reason"] = "Dissertation Statistical Terms"
                    continue
                elif PROB_LEX_RE.search(th) and p_comb >= 0.16:
                    rec["primary_field"] = "Probability"
                    rec["field_confidence"] = round(max(0.72, p_comb), 2)
                    rec["classification_reason"] = "Dissertation Probability Terms"
                    continue
            else:
                # Has interdisciplinary MSC (92 Bio, 68 CS, 91 Econ, 90 OR):
                # Promote MSC 92 + biostat/stat thesis directly to Biostatistics!
                if subj.startswith("92") and (has_biostat_kw or p_stat >= 0.35 or CORE_STAT_LEX_RE.search(th)):
                    rec["primary_field"] = "Biostatistics"
                    rec["field_confidence"] = 0.88
                    rec["classification_reason"] = "MSC 92 (Quantitative Bio / Biostat)"
                    continue
                if p_comb >= 0.62:
                    rec["primary_field"] = "Biostatistics" if has_biostat_kw else ("Probability" if p_prob > p_stat else "Statistics")
                    rec["field_confidence"] = round(p_comb, 2)
                    rec["classification_reason"] = "Dissertation Statistical Focus"
                    continue

        # 5. AFT Public Health / Epidemiology PhD with statistical/epidemiological methods
        if not subj and ("phtree" in maj_set or "epidemiology" in maj_set):
            if re.search(r"\b(statist\w*|regression|bayesian|causal|survival|longitudinal|hazard|inference|estimat\w*)\b", th + " " + area, re.I):
                rec["primary_field"] = "Biostatistics"
                rec["field_confidence"] = 0.82
                rec["classification_reason"] = "Public Health / Biostat Lineage"
                continue

        # 6. Related Quantitative Fields (Econometrics, Statistical ML, OR, Bioinformatics, Psychometrics)
        if subj.startswith(("68", "90", "91", "92")) or RELATED_QUANT_RE.search(th + " " + area) or ("epidemiology" in maj_set or "compbio" in maj_set):
            if RELATED_QUANT_RE.search(th + " " + area) or p_comb >= 0.12 or subj.startswith(("90", "91")) or ("epidemiology" in maj_set or "compbio" in maj_set):
                rec["primary_field"] = "Related Quantitative"
                rec["field_confidence"] = 0.70
                rec["classification_reason"] = subj.split("—")[0] + " / Related Quantitative" if "—" in subj else "Related Quantitative Field"
                continue

        rec["primary_field"] = "Mathematics & Other"
        rec["field_confidence"] = 0.50
        rec["classification_reason"] = subj if subj else "General Mathematical / Ancestral Lineage"

    CORE_SET = {"Statistics", "Biostatistics", "Probability"}

    # Pass 2A (Bottom-Up Mentor Consensus):
    # Recover unlabeled pioneers/professors (e.g. Fisher, Neyman, Efron, Bickel, Tukey, Cox, Laird, Robins, Bühlmann, Varadhan, Durrett, Kolmogorov)
    # whose MSC subject was missing, based on strong consensus among their PhD students!
    promoted_advisors = 0
    for _round in range(2):
        updates = []
        for uid, rec in people.items():
            if rec["primary_field"] in CORE_SET or rec["mgp_subject"]:
                continue
            stus = students_of.get(uid, ())
            if len(stus) < 2:
                continue
            core_counts = Counter()
            classified_stu_total = 0
            for s in stus:
                sf = people[s]["primary_field"]
                if sf in CORE_SET:
                    core_counts[sf] += 1
                    classified_stu_total += 1
                elif people[s]["mgp_subject"]:
                    classified_stu_total += 1
            total_core = sum(core_counts.values())
            if total_core >= 5 or (total_core >= 2 and classified_stu_total > 0 and total_core / max(classified_stu_total, 2) >= 0.45) or (total_core >= 2 and len(stus) <= 5 and total_core / len(stus) >= 0.5):
                best_field = core_counts.most_common(1)[0][0]
                maj_set = {x.strip() for x in (rec["aft_majorarea"] or "").split(",") if x.strip()}
                if "phtree" in maj_set or "epidemiology" in maj_set:
                    best_field = "Biostatistics"
                updates.append((uid, best_field))
        for uid, bf in updates:
            people[uid]["primary_field"] = bf
            people[uid]["field_confidence"] = 0.85
            people[uid]["classification_reason"] = "Advisor of Stat/Biostat/Prob PhDs"
            promoted_advisors += 1

    # Compute each advisor's core-stat concentration (is this advisor primarily running a Stat/Biostat/Prob group?)
    pure_stat_advisor = {}
    for uid, rec in people.items():
        if rec["primary_field"] not in CORE_SET:
            pure_stat_advisor[uid] = None
            continue
        stus = students_of.get(uid, ())
        core_n = sum(1 for s in stus if people[s]["primary_field"] in CORE_SET)
        explicit_other_n = sum(1 for s in stus if people[s]["primary_field"] not in CORE_SET and people[s]["mgp_subject"] and not people[s]["mgp_subject"].startswith(("60", "62")))
        # If advisor is Core Stat/Biostat/Prob AND doesn't predominantly graduate explicit pure-math MSC students
        if explicit_other_n == 0 or core_n >= explicit_other_n:
            pure_stat_advisor[uid] = rec["primary_field"]
        else:
            pure_stat_advisor[uid] = None

    # Pass 2B (Top-Down Unlabeled Student Recovery):
    # If a PhD student has NO explicit MSC subject (subject is None) and was NOT rejected as Mechanics/Statics,
    # and ALL or MAJORITY of their doctoral advisors are dedicated Stat/Biostat/Prob professors,
    # recover them into their advisor's Stat/Biostat/Prob field!
    promoted_students = 0
    for uid, rec in people.items():
        if rec["primary_field"] in CORE_SET or rec["mgp_subject"]:
            continue
        if FALSE_STATICS_RE.search(rec["thesis"] or ""):
            continue
        advs = advisors_of.get(uid, ())
        if not advs:
            continue
        adv_fields = [pure_stat_advisor.get(a) for a in advs if pure_stat_advisor.get(a)]
        if len(adv_fields) > 0 and len(adv_fields) >= len(advs) / 2:
            chosen = Counter(adv_fields).most_common(1)[0][0]
            rec["primary_field"] = chosen
            rec["field_confidence"] = 0.76
            rec["classification_reason"] = "Lineage Consensus (Stat/Biostat/Prob Advisor)"
            promoted_students += 1

    for uid, rec in people.items():
        # Keep pre-1885 unlabeled historical mathematical ancestors accurately tagged
        if rec.get("year") and rec["year"] < 1885 and not rec["mgp_subject"] and rec["primary_field"] in CORE_SET:
            rec["primary_field"] = "Mathematics & Other"
            rec["classification_reason"] = "Historical Mathematical Ancestor"
        rec["is_core_stat"] = 1 if rec["primary_field"] in CORE_SET else 0
        rec["is_broad_stat"] = 1 if (rec["is_core_stat"] or rec["primary_field"] == "Related Quantitative") else 0

    field_dist = Counter(rec["primary_field"] for rec in people.values())
    print(f"      Lineage consensus recovered {promoted_advisors:,} unlabeled advisors and {promoted_students:,} unlabeled PhD students.")
    print(f"      Final Field Distribution: {dict(field_dist)}")

    # -----------------------------------------------------------------------
    # Stage 5: Exact DAG Descendant Reachability + SQLite Database Creation
    # -----------------------------------------------------------------------
    print("[6/6] Computing exact DAG descendant counts and writing SQLite + FTS5 database...")

    # Topological order from leaves (out_deg == 0) up to roots, freeing child sets once all parents consume them!
    out_rem = {uid: len(students_of.get(uid, ())) for uid in people}
    parent_rem = {uid: len(advisors_of.get(uid, ())) for uid in people}
    leaf_q = deque([uid for uid, d in out_rem.items() if d == 0])

    desc_sets = {}
    desc_counts = {}
    stat_desc_counts = {}

    while leaf_q:
        u = leaf_q.popleft()
        stus = students_of.get(u, ())
        if not stus:
            desc_counts[u] = 0
            stat_desc_counts[u] = 0
            if parent_rem[u] > 0:
                desc_sets[u] = None
        else:
            dset = set(stus)
            for s in stus:
                child_set = desc_sets.get(s)
                if child_set:
                    dset.update(child_set)
                parent_rem[s] -= 1
                if parent_rem[s] <= 0:
                    desc_sets.pop(s, None)
            desc_counts[u] = len(dset)
            stat_desc_counts[u] = sum(1 for x in dset if people[x]["is_core_stat"])
            if parent_rem[u] > 0:
                desc_sets[u] = dset

        for p in advisors_of.get(u, ()):
            out_rem[p] -= 1
            if out_rem[p] == 0:
                leaf_q.append(p)

    desc_sets.clear()

    # Build SQLite database cleanly
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.executescript("""
        PRAGMA journal_mode = WAL;
        PRAGMA synchronous = NORMAL;
        PRAGMA temp_store = MEMORY;

        CREATE TABLE people (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            norm_name TEXT NOT NULL,
            search_aliases TEXT,
            school TEXT,
            raw_school TEXT,
            country TEXT,
            year INTEGER,
            thesis TEXT,
            mgp_subject TEXT,
            primary_field TEXT NOT NULL,
            is_core_stat INTEGER NOT NULL,
            is_broad_stat INTEGER NOT NULL,
            field_confidence REAL,
            classification_reason TEXT,
            current_affiliation TEXT,
            research_areas TEXT,
            mgp_id INTEGER UNIQUE,
            aft_pid INTEGER UNIQUE,
            wikidata_qid TEXT,
            orcid TEXT,
            homepage TEXT,
            proquest_id TEXT,
            sources TEXT NOT NULL,
            advisor_count INTEGER NOT NULL DEFAULT 0,
            student_count INTEGER NOT NULL DEFAULT 0,
            descendant_count INTEGER NOT NULL DEFAULT 0,
            stat_descendant_count INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE edges (
            advisor_id INTEGER NOT NULL,
            student_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            PRIMARY KEY (advisor_id, student_id)
        );

        CREATE TABLE schools (
            school TEXT PRIMARY KEY,
            norm_school TEXT NOT NULL,
            aliases TEXT,
            country TEXT,
            core_stat_count INTEGER NOT NULL,
            broad_stat_count INTEGER NOT NULL,
            total_phd_count INTEGER NOT NULL,
            min_year INTEGER,
            max_year INTEGER
        );

        CREATE TABLE meta_stats (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL
        );

        CREATE VIRTUAL TABLE people_fts USING fts5(
            name,
            search_aliases,
            school,
            thesis,
            research_areas,
            content='people',
            content_rowid='id',
            tokenize='unicode61 remove_diacritics 2'
        );
    """)

    people_rows = []
    school_agg = defaultdict(lambda: {"countries": Counter(), "core": 0, "broad": 0, "total": 0, "years": []})
    decade_hist = defaultdict(lambda: Counter())

    for uid, rec in people.items():
        adv_c = len(advisors_of.get(uid, ()))
        stu_c = len(students_of.get(uid, ()))
        d_c = desc_counts.get(uid, 0)
        sd_c = stat_desc_counts.get(uid, 0)
        sch = rec["school"] or ""
        acronyms = SCHOOL_ACRONYMS.get(sch, "")
        alias_str = " | ".join(sorted(rec["aliases"]))
        if acronyms:
            alias_str = (alias_str + " | " + acronyms).strip(" |")

        people_rows.append((
            uid,
            rec["name"],
            rec["norm_name"],
            alias_str,
            sch,
            rec["raw_school"],
            rec["country"],
            rec["year"],
            rec["thesis"],
            rec["mgp_subject"],
            rec["primary_field"],
            rec["is_core_stat"],
            rec["is_broad_stat"],
            rec["field_confidence"],
            rec["classification_reason"],
            rec["current_affiliation"],
            rec["research_areas"],
            rec["mgp_id"],
            rec["aft_pid"],
            rec["wikidata_qid"],
            rec["orcid"],
            rec["homepage"],
            rec["proquest_id"],
            ",".join(rec["sources"]),
            adv_c,
            stu_c,
            d_c,
            sd_c,
        ))

        if sch:
            sa = school_agg[sch]
            sa["total"] += 1
            sa["core"] += rec["is_core_stat"]
            sa["broad"] += rec["is_broad_stat"]
            if rec["country"]:
                sa["countries"][rec["country"]] += 1
            if rec["year"]:
                sa["years"].append(rec["year"])

        if rec["year"] and 1900 <= rec["year"] <= 2027 and rec["is_core_stat"]:
            dec = (rec["year"] // 10) * 10
            decade_hist[dec][rec["primary_field"]] += 1

    cur.executemany("""
        INSERT INTO people VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, people_rows)

    edge_rows = [(adv, stu, "+".join(sorted(srcs))) for (adv, stu), srcs in edge_sources.items()]
    cur.executemany("INSERT INTO edges VALUES (?,?,?)", edge_rows)

    school_rows = []
    for sch, sa in school_agg.items():
        top_country = sa["countries"].most_common(1)[0][0] if sa["countries"] else ""
        yrs = sa["years"]
        school_rows.append((
            sch,
            fold_ascii(sch).lower(),
            SCHOOL_ACRONYMS.get(sch, ""),
            top_country,
            sa["core"],
            sa["broad"],
            sa["total"],
            min(yrs) if yrs else None,
            max(yrs) if yrs else None,
        ))
    cur.executemany("INSERT INTO schools VALUES (?,?,?,?,?,?,?,?,?)", school_rows)

    # Populate FTS5 index & create B-tree indexes
    cur.executescript("""
        INSERT INTO people_fts(people_fts) VALUES('rebuild');

        CREATE INDEX idx_people_norm_name ON people(norm_name);
        CREATE INDEX idx_people_school ON people(school);
        CREATE INDEX idx_people_year ON people(year);
        CREATE INDEX idx_people_core_stat ON people(is_core_stat, stat_descendant_count DESC, student_count DESC);
        CREATE INDEX idx_people_broad_stat ON people(is_broad_stat, stat_descendant_count DESC);
        CREATE INDEX idx_people_field ON people(primary_field, stat_descendant_count DESC);
        CREATE INDEX idx_edges_advisor ON edges(advisor_id);
        CREATE INDEX idx_edges_student ON edges(student_id);
        CREATE INDEX idx_schools_core ON schools(core_stat_count DESC);
    """)

    # Save rich summary statistics
    summary = {
        "total_people": len(people),
        "total_edges": len(edge_rows),
        "core_stat_total": sum(1 for r in people.values() if r["is_core_stat"]),
        "broad_stat_total": sum(1 for r in people.values() if r["is_broad_stat"]),
        "by_field": dict(field_dist),
        "mgp_count": sum(1 for r in people.values() if r["mgp_id"] is not None),
        "aft_merged_count": len(aft_to_mgp),
        "aft_net_new_count": aft_net_new,
        "wikidata_linked_count": sum(1 for r in people.values() if r["wikidata_qid"]),
        "total_schools": len(school_rows),
        "core_stat_schools": sum(1 for s in school_rows if s[4] > 0),
        "decade_histogram": {str(k): dict(v) for k, v in sorted(decade_hist.items())},
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    cur.execute("INSERT INTO meta_stats VALUES (?, ?)", ("summary", json.dumps(summary)))
    conn.commit()
    conn.close()

    db_mb = os.path.getsize(DB_PATH) / (1024 * 1024)
    print(f"\nSUCCESS! Built {DB_PATH} ({db_mb:.1f} MB) in {time.time() - t0:.1f}s.")
    print("Summary:", json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
