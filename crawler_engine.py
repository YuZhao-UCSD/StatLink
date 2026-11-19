#!/usr/bin/env python3
"""
Live Web, Homepage, CV PDF, MGP, PubMed & Global ETD Crawler Engine (`crawler_engine.py`).

Given any professor/researcher in StatLineage, crawls multiple live internet sources to
discover PhD students & doctoral advisees missing from static snapshots:
  1. Live Mathematics Genealogy Project (`mathgenealogy.org/id.php?id=...`) student table
  2. DataCite Global Dissertation API (`contributors.contributorType:Supervisor`)
  3. HAL / theses.fr API (`director_s`)
  4. NIH NCBI PubMed E-Utilities Statistical/Biostatistical Methodology First/Senior-Author Lineages
  5. Professor's Personal Website / Lab Group Pages (`/people`, `/students`, `/group`, `/alumni`)
     — using stored homepage OR discovering their academic homepage & CV PDF live via DuckDuckGo!
  6. Professor's CV PDF (parsed via `pdftotext -layout` when available), extracting Main-Advisor
     PhD Dissertation sections while strictly excluding Committee-Member, Postdoc & Undergrad sections.
"""

import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
from collections import defaultdict
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

PDFTOTEXT_BIN = shutil.which("pdftotext") or (
    r"C:\Program Files\MiKTeX\miktex\bin\x64\pdftotext.exe"
    if os.path.exists(r"C:\Program Files\MiKTeX\miktex\bin\x64\pdftotext.exe")
    else None
)


def fold_ascii(text: str) -> str:
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
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


def norm_key(name: str) -> str:
    s = re.sub(r"\([^)]*\)", " ", name or "")
    s = re.sub(r"\b(jr|sr|ii|iii|iv)\b", " ", fold_ascii(s).lower())
    s = re.sub(r"[^a-z0-9\s\-]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


BAD_NAME_TOKENS = {
    "graduate", "student", "students", "postdoctoral", "postdoc", "fellow", "fellows",
    "professor", "assistant", "associate", "university", "college", "institute", "school",
    "department", "division", "biostatistics", "statistics", "probability", "mathematics",
    "data", "science", "research", "group", "lab", "laboratory", "team", "members", "member",
    "current", "former", "past", "alumni", "curriculum", "vitae", "google", "scholar",
    "thesis", "dissertation", "phd", "doctoral", "master", "masters", "undergraduate",
    "undergraduates", "visiting", "staff", "intern", "interns", "collaborators", "projects",
    "publications", "teaching", "courses", "awards", "news", "contact", "home", "about",
    "share", "download", "full", "screen", "now", "present", "expected", "chair", "advisor",
    "co-advisor", "committee", "california", "stanford", "harvard", "berkeley", "washington",
}


def clean_extracted_person_name(raw: str) -> str:
    if not raw:
        return ""
    s = re.sub(r"\([^)]*\)", "", raw).strip()
    s = re.sub(r",?\s*(?:Ph\.?\s*D\.?|Sc\.?\s*D\.?|M\.?\s*S\.?|M\.?\s*A\.?|B\.?\s*S\.?).*$", "", s, flags=re.I)
    s = re.sub(r"^(?:[•\-\*\d\.\)\s]+|(?:Dr|Prof|Mr|Ms|Mrs)\.?\s+)+", "", s, flags=re.I)
    if "," in s:
        parts = [p.strip(" .") for p in s.split(",") if p.strip(" .")]
        if len(parts) == 2:
            s = f"{parts[1]} {parts[0]}"
    s = re.sub(r"\s+", " ", s).strip(" ,.;:–—-")
    if len(s) < 4 or len(s) > 55 or " " not in s:
        return ""
    toks = s.split()
    if len(toks) < 2 or len(toks) > 4:
        return ""
    # Every main word token should look like a capitalized name part (allowing van/de/von/al)
    particles = {"van", "von", "de", "del", "da", "di", "la", "le", "al", "el", "bin", "ibn"}
    for t in toks:
        tl = fold_ascii(t).lower().strip(".-'")
        if not tl or tl in BAD_NAME_TOKENS:
            return ""
        if tl not in particles and not t[0].isupper():
            return ""
        if any(ch.isdigit() for ch in t):
            return ""
    return s


# ---------------------------------------------------------------------------
# 1. Live MGP Student Table Fetcher
# ---------------------------------------------------------------------------

def fetch_live_mgp_students(mgp_id: int, advisor_name: str):
    out = []
    if not mgp_id:
        return out
    try:
        r = requests.get(f"https://www.mathgenealogy.org/id.php?id={mgp_id}", headers=HEADERS, timeout=10)
        if r.status_code != 200:
            return out
        soup = BeautifulSoup(r.text, "lxml")
        for table in soup.find_all("table"):
            if "id.php?id=" not in str(table):
                continue
            for tr in table.find_all("tr"):
                tds = tr.find_all("td")
                if len(tds) >= 3:
                    a = tds[0].find("a", href=re.compile(r"id\.php\?id=(\d+)"))
                    if not a:
                        continue
                    m = re.search(r"id=(\d+)", a["href"])
                    stu_mgp_id = int(m.group(1)) if m else None
                    nm = clean_extracted_person_name(a.get_text(" ", strip=True))
                    sch = tds[1].get_text(" ", strip=True)
                    yr_txt = tds[2].get_text(" ", strip=True)
                    yr = int(yr_txt) if yr_txt.isdigit() and 1800 <= int(yr_txt) <= 2027 else None
                    if nm:
                        out.append({
                            "student": nm,
                            "school": sch,
                            "year": yr,
                            "thesis": "",
                            "mgp_id": stu_mgp_id,
                            "source": "MGP (Live)",
                        })
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# 2. Live DataCite, HAL & NIH PubMed Methodology Advisor Queries
# ---------------------------------------------------------------------------

def fetch_live_datacite_and_hal(advisor_name: str):
    out = []
    nk = norm_key(advisor_name)
    toks = nk.split()
    if len(toks) < 2:
        return out
    first_t, last_t = toks[0], toks[-1]

    # DataCite Supervisor search
    try:
        q = f'contributors.contributorType:Supervisor AND contributors.name:*{last_t}* AND types.resourceTypeGeneral:Dissertation'
        r = requests.get(
            "https://api.datacite.org/dois",
            params={"query": q, "page[size]": 60},
            headers=HEADERS,
            timeout=10,
        )
        if r.status_code == 200:
            for it in r.json().get("data") or []:
                attr = it.get("attributes") or {}
                matched_sup = False
                for c in attr.get("contributors") or []:
                    if c.get("contributorType") == "Supervisor":
                        cn = norm_key(clean_extracted_person_name(c.get("name") or ""))
                        if cn and last_t in cn.split() and cn[0] == first_t[0]:
                            matched_sup = True
                            break
                if not matched_sup:
                    continue
                creators = attr.get("creators") or []
                if len(creators) != 1:
                    continue
                stu = clean_extracted_person_name(creators[0].get("name") or "")
                if not stu:
                    continue
                titles = attr.get("titles") or []
                title = (titles[0].get("title") if titles else "").strip()
                pub = (attr.get("publisher") or "").strip()
                yr = attr.get("publicationYear")
                yr = int(yr) if yr and str(yr).isdigit() else None
                out.append({
                    "student": stu,
                    "school": pub,
                    "year": yr,
                    "thesis": title,
                    "mgp_id": None,
                    "source": "DataCite ETD",
                })
    except Exception:
        pass

    # HAL Theses search
    try:
        r2 = requests.get(
            "https://api.archives-ouvertes.fr/search/tel/",
            params={
                "q": f'director_s:"{advisor_name}"',
                "rows": 80,
                "fl": "title_s,authFullName_s,director_s,defenseDateY_i,authorityInstitution_s,instStructName_s",
                "wt": "json",
            },
            headers=HEADERS,
            timeout=8,
        )
        if r2.status_code == 200:
            for d in ((r2.json().get("response") or {}).get("docs")) or []:
                auths = d.get("authFullName_s") or []
                if not auths:
                    continue
                stu = clean_extracted_person_name(auths[0])
                if not stu:
                    continue
                titles = d.get("title_s") or [""]
                insts = d.get("authorityInstitution_s") or d.get("instStructName_s") or [""]
                yr = d.get("defenseDateY_i")
                out.append({
                    "student": stu,
                    "school": insts[0],
                    "year": int(yr) if isinstance(yr, int) else None,
                    "thesis": titles[0],
                    "mgp_id": None,
                    "source": "HAL Theses",
                })
    except Exception:
        pass

    return out


PUBMED_METHOD_JOURNALS = (
    '("Biostatistics"[ta] OR "Biometrics"[ta] OR "J Am Stat Assoc"[ta] OR "Stat Med"[ta] '
    'OR "Stat Methods Med Res"[ta] OR "Ann Appl Stat"[ta] OR "Biometrika"[ta] OR "Ann Stat"[ta] '
    'OR "Stat Sci"[ta] OR "J R Stat Soc Series B Stat Methodol"[ta] OR "J R Stat Soc Ser C Appl Stat"[ta] '
    'OR "J Comput Graph Stat"[ta] OR "Comput Stat Data Anal"[ta] OR "Stat Sin"[ta] OR "Scand J Stat"[ta] '
    'OR "Lifetime Data Anal"[ta] OR "Biom J"[ta] OR "Pharm Stat"[ta] OR "J Biopharm Stat"[ta] '
    'OR "Clin Trials"[ta] OR "Bioinformatics"[ta] OR "Genome Biol"[ta] OR "PLoS Comput Biol"[ta] '
    'OR "Genet Epidemiol"[ta] OR "Am J Hum Genet"[ta] OR "Neuroimage"[ta] OR "Am J Epidemiol"[ta])'
)

PUBMED_DEPT_RE = re.compile(
    r"\b(biostat\w*|department\s+of\s+statistics|school\s+of\s+statistics|statistical\s+science\w*|"
    r"applied\s+mathematics\s+and\s+statistics|division\s+of\s+statistics|data\s+science)\b",
    re.I,
)


def fetch_live_pubmed_methodology_students(advisor_name: str, school: str = ""):
    """
    Queries NIH NCBI PubMed E-Utilities for small-team (2-5 author) statistical/biostatistical
    methodology papers where `advisor_name` is Senior (Last) Author and First Author carries a
    Statistics/Biostatistics/Data Science departmental affiliation at `school`.
    """
    out = []
    nk = norm_key(advisor_name)
    toks = nk.split()
    if len(toks) < 2:
        return out
    first_t, last_t = toks[0], toks[-1]
    sch_clean = re.sub(r"\([^)]*\)", "", school or "").strip()
    sch_words = [
        w.lower() for w in re.findall(r"[A-Za-z]+", sch_clean)
        if w.lower() not in {"university", "of", "the", "at", "in", "and", "college", "school", "institute"}
    ]
    aff_clause = f' AND "{sch_clean}"[Affiliation]' if sch_clean else ""
    term = f'"{last_t} {first_t[0]}"[Author]{aff_clause} AND {PUBMED_METHOD_JOURNALS}'
    try:
        es = requests.get(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
            params={"db": "pubmed", "term": term, "retmax": 200, "retmode": "json"},
            headers=HEADERS,
            timeout=9,
        )
        if es.status_code != 200:
            return out
        pmids = ((es.json().get("esearchresult") or {}).get("idlist")) or []
        if not pmids:
            return out
        ef = requests.get(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
            params={"db": "pubmed", "id": ",".join(pmids[:200]), "retmode": "xml"},
            headers=HEADERS,
            timeout=12,
        )
        if ef.status_code != 200:
            return out
        root = ET.fromstring(ef.text)
        by_stu = defaultdict(list)
        for art in root.findall(".//PubmedArticle"):
            all_au = art.findall(".//Author")
            if not (2 <= len(all_au) <= 5):
                continue
            au1, auL = all_au[0], all_au[-1]
            fnL = clean_extracted_person_name(f"{auL.findtext('ForeName') or ''} {auL.findtext('LastName') or ''}")
            nkL = norm_key(fnL)
            if not nkL or nkL.split()[-1] != last_t or nkL[0] != first_t[0]:
                continue
            fn1 = clean_extracted_person_name(f"{au1.findtext('ForeName') or ''} {au1.findtext('LastName') or ''}")
            nk1 = norm_key(fn1)
            if not nk1 or nk1 == nk or len(nk1.split()[0]) < 2:
                continue
            aff1 = " ".join(x.text or "" for x in au1.findall(".//Affiliation"))
            if not PUBMED_DEPT_RE.search(aff1):
                continue
            if sch_words and not any(w in aff1.lower() for w in sch_words):
                continue
            yr_s = art.findtext(".//PubDate/Year") or art.findtext(".//ArticleDate/Year") or ""
            yr = int(yr_s) if yr_s.isdigit() and 1995 <= int(yr_s) <= 2026 else None
            title = (art.findtext(".//ArticleTitle") or "").strip()
            by_stu[nk1].append((yr, fn1, len(all_au), title))
        for snk, papers in by_stu.items():
            yrs = [p[0] for p in papers if p[0]]
            if yrs and (max(yrs) - min(yrs) > 7):
                continue
            min_team = min(p[2] for p in papers)
            if len(papers) >= 2 or min_team <= 4:
                disp = max((p[1] for p in papers), key=len)
                max_yr = max(yrs) if yrs else None
                out.append({
                    "student": disp,
                    "school": school,
                    "year": max_yr if (max_yr and max_yr <= 2025) else None,
                    "thesis": papers[0][3],
                    "mgp_id": None,
                    "source": "PubMed Methodology",
                })
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# 3. HTML & CV PDF Student Extraction + DuckDuckGo Homepage/CV Discovery
# ---------------------------------------------------------------------------

POS_SEC_RE = re.compile(
    r"\b(ph\.?d\.?\s*(?:students?|alumni|graduates?|advisees?|dissertations?|theses|committees?)|"
    r"doctoral\s*(?:students?|alumni|graduates?|advisees?|dissertations?)|"
    r"(?:current|former|past|graduated)\s*(?:group\s*members?|students?|ph\.?d\.?\s*students?|members?)|"
    r"alumni|research\s+advising\s*\(doctoral\)|main\s+advis[eo]r)\b",
    re.I,
)

NEG_SEC_RE = re.compile(
    r"\b(postdoc\w*|post-doc\w*|master'?s|m\.?s\.?\s*students?|undergrad\w*|b\.?s\.?\s*students?|"
    r"committees?\s*\(member\)|committee\s*member|reader|examiner|visiting|high\s*school|"
    r"publications?|papers?|teaching|courses?|grants?|awards?)\b",
    re.I,
)

YEAR_RE = re.compile(r"\b(19[7-9]\d|20[0-2]\d)\b")


def parse_html_group_page(html: str, advisor_name: str, default_school: str):
    soup = BeautifulSoup(html, "lxml")
    adv_nk = norm_key(advisor_name)
    found = []
    seen = set()

    def record(raw_name, yr=None, topic="", sch=""):
        nm = clean_extracted_person_name(raw_name)
        if not nm:
            return
        nk = norm_key(nm)
        if not nk or nk == adv_nk or nk in seen:
            return
        seen.add(nk)
        found.append({
            "student": nm,
            "school": sch or default_school,
            "year": yr,
            "thesis": topic,
            "mgp_id": None,
            "source": "Faculty Web Crawl",
        })

    for hdr in soup.find_all(["h1", "h2", "h3", "h4", "h5", "strong"]):
        htxt = hdr.get_text(" ", strip=True)
        if len(htxt) > 95:
            continue
        if not POS_SEC_RE.search(htxt) or NEG_SEC_RE.search(htxt):
            continue

        curr = hdr.find_next_sibling()
        steps = 0
        while curr and steps < 20:
            steps += 1
            if curr.name in ("h1", "h2", "h3", "h4") and curr != hdr:
                break
            stxt = curr.get_text(" ", strip=True)
            if len(stxt) < 85 and NEG_SEC_RE.search(stxt):
                break

            if curr.name in ("ul", "ol"):
                for li in curr.find_all("li", recursive=False):
                    ltxt = re.sub(r"\s+", " ", li.get_text(" ", strip=True))
                    if re.search(r"\b(postdoc\w*|undergrad\w*|master'?s|m\.s\.\s*student|visiting)\b", ltxt, re.I) and not re.search(r"\bph\.?d\b", ltxt, re.I):
                        continue
                    ym = YEAR_RE.search(ltxt)
                    yr = int(ym.group(1)) if ym else None
                    head = re.split(r"[\(\[:,–—\-]", ltxt)[0].strip()
                    record(head, yr, "")
            else:
                # Structured cards (e.g. Owlstown / Hugo / Bootstrap person cards)
                cards = curr.find_all(["div", "li", "tr"], recursive=True) if curr.name in ("div", "section", "table") else [curr]
                for c in cards:
                    lines = [x.strip() for x in c.get_text("\n", strip=True).split("\n") if x.strip()]
                    if 2 <= len(lines) <= 5:
                        role_txt = " ".join(lines[1:])
                        if re.search(r"\b(Ph\.?D\.?|Doctoral|Graduate\s+student)\b", role_txt, re.I) and not re.search(r"\b(postdoc\w*|undergrad\w*|master|m\.s\.)\b", role_txt, re.I):
                            ym = YEAR_RE.search(role_txt)
                            yr = int(ym.group(1)) if ym else None
                            topic = lines[2] if len(lines) >= 3 and not lines[2].lower().startswith("now at") else ""
                            record(lines[0], yr, topic)
            curr = curr.find_next_sibling()

    return found


def parse_cv_pdf_bytes(pdf_bytes: bytes, advisor_name: str, default_school: str):
    if not PDFTOTEXT_BIN or not pdf_bytes or pdf_bytes[:4] != b"%PDF":
        return []
    found = []
    seen = set()
    adv_nk = norm_key(advisor_name)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
            tf.write(pdf_bytes)
            tmp_path = tf.name
        txt = subprocess.check_output(
            [PDFTOTEXT_BIN, "-layout", tmp_path, "-"], encoding="utf-8", errors="replace", timeout=10
        )
        in_phd_advisor_sec = False
        for raw_line in txt.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            # Check section transitions
            if len(line) < 85 and not line.startswith(("•", "-", "*")):
                if NEG_SEC_RE.search(line):
                    in_phd_advisor_sec = False
                    continue
                if POS_SEC_RE.search(line) and not NEG_SEC_RE.search(line):
                    in_phd_advisor_sec = True
                    continue

            if in_phd_advisor_sec:
                m = re.match(r"^[•\-\*\d\.\)\s]*([A-Z][A-Za-z\u00C0-\u017F'\-]+(?:\s+[A-Z][A-Za-z\u00C0-\u017F'\-\.]*){1,3})\s*(.*)$", line)
                if m:
                    nm = clean_extracted_person_name(m.group(1))
                    rest = m.group(2)
                    if nm and norm_key(nm) != adv_nk and norm_key(nm) not in seen:
                        if re.search(r"\b(postdoc\w*|undergrad\w*|master|m\.s\.)\b", rest, re.I) and not re.search(r"\bph\.?d\b", rest, re.I):
                            continue
                        yrs = [int(y) for y in YEAR_RE.findall(rest)]
                        grad_yr = max(yrs) if (yrs and not re.search(r"present|current", rest, re.I)) else None
                        seen.add(norm_key(nm))
                        found.append({
                            "student": nm,
                            "school": default_school,
                            "year": grad_yr,
                            "thesis": "",
                            "mgp_id": None,
                            "source": "Professor CV PDF",
                        })
    except Exception:
        pass
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    return found


def discover_urls_via_ddg(advisor_name: str, school: str = ""):
    """Queries DuckDuckGo HTML search to discover a professor's homepage, /people page, and CV PDF."""
    urls = []
    q = f'"{advisor_name}" (students OR people OR alumni OR "PhD" OR CV) {school}'.strip()
    try:
        r = requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": q},
            headers=HEADERS,
            timeout=10,
        )
        if r.status_code != 200:
            return urls
        soup = BeautifulSoup(r.text, "lxml")
        skip_domains = {
            "scholar.google.com", "researchgate.net", "linkedin.com", "wikipedia.org",
            "mathgenealogy.org", "academictree.org", "rate-my-professor", "ratemyprofessors.com",
            "youtube.com", "twitter.com", "x.com", "facebook.com", "amazon.com",
        }
        for a in soup.select(".result__a"):
            href = a.get("href") or ""
            if "uddg=" in href:
                qs = parse_qs(urlparse(href).query)
                if "uddg" in qs:
                    href = unquote(qs["uddg"][0])
            if not href.startswith("http"):
                continue
            dom = urlparse(href).netloc.lower()
            if any(sd in dom for sd in skip_domains):
                continue
            if href not in urls:
                urls.append(href)
            if len(urls) >= 5:
                break
    except Exception:
        pass
    return urls


def crawl_professor_across_web(prof_name: str, school: str = "", homepage: str = "", mgp_id: int = None):
    """
    Master on-demand discovery routine for a single professor.
    Returns (discovered_students_list, discovered_homepage, sources_checked_list).
    """
    all_found = []
    sources_checked = []

    # 1. Live MGP check
    if mgp_id:
        sources_checked.append(f"MGP Live (#{mgp_id})")
        all_found.extend(fetch_live_mgp_students(mgp_id, prof_name))

    # 2. DataCite & HAL live supervisor check
    sources_checked.append("DataCite & HAL ETDs")
    all_found.extend(fetch_live_datacite_and_hal(prof_name))

    # 3. NIH PubMed Methodology First/Senior-Author check
    sources_checked.append("NIH PubMed Methodology")
    all_found.extend(fetch_live_pubmed_methodology_students(prof_name, school))

    # 4. Homepage + DuckDuckGo web discovery
    candidate_urls = []
    if homepage and homepage.startswith("http"):
        candidate_urls.append(homepage)
    ddg_urls = discover_urls_via_ddg(prof_name, school)
    if ddg_urls:
        sources_checked.append("Web Search & Faculty Pages")
        for u in ddg_urls:
            if u not in candidate_urls:
                candidate_urls.append(u)

    discovered_homepage = homepage
    visited = set()
    session = requests.Session()

    def safe_get(url_target, t_out=8):
        try:
            return session.get(url_target, headers=HEADERS, timeout=t_out, allow_redirects=True)
        except requests.exceptions.SSLError:
            return session.get(url_target, headers=HEADERS, timeout=t_out, allow_redirects=True, verify=False)

    for u in candidate_urls[:5]:
        if u in visited:
            continue
        visited.add(u)
        try:
            resp = safe_get(u, 8)
            if resp.status_code != 200:
                continue
            ct = resp.headers.get("content-type", "").lower()
            if "pdf" in ct or resp.content[:4] == b"%PDF":
                sources_checked.append("CV PDF")
                all_found.extend(parse_cv_pdf_bytes(resp.content, prof_name, school))
                continue
            if "html" not in ct:
                continue

            if not discovered_homepage:
                discovered_homepage = resp.url

            all_found.extend(parse_html_group_page(resp.text, prof_name, school))

            # Follow internal /people, /students, /group, /alumni links on the professor's site
            soup = BeautifulSoup(resp.text, "lxml")
            host = urlparse(resp.url).netloc
            sub_links = []
            for a in soup.find_all("a", href=True):
                txt = a.get_text(" ", strip=True).lower()
                href = a["href"].strip()
                if any(w in txt or w in href.lower() for w in ["people", "student", "group", "alumni", "team", "member", "advis"]):
                    fu = urljoin(resp.url, href)
                    if urlparse(fu).netloc == host and fu not in visited:
                        sub_links.append(fu)
                        if len(sub_links) >= 3:
                            break
            for su in sub_links:
                visited.add(su)
                try:
                    sr = safe_get(su, 7)
                    if sr.status_code == 200 and "html" in sr.headers.get("content-type", "").lower():
                        all_found.extend(parse_html_group_page(sr.text, prof_name, school))
                except Exception:
                    pass
        except Exception:
            pass

    # Deduplicate discovered students by normalized name (preferring records with year & thesis)
    dedup = {}
    for item in all_found:
        nk = norm_key(item["student"])
        if not nk:
            continue
        prev = dedup.get(nk)
        if prev is None or (not prev.get("year") and item.get("year")) or (not prev.get("thesis") and item.get("thesis")):
            dedup[nk] = item

    return list(dedup.values()), discovered_homepage, list(dict.fromkeys(sources_checked))
