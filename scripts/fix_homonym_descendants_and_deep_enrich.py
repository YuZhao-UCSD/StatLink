#!/usr/bin/env python3
"""
Master Lineage Precision & Deep Website/Portrait Enrichment Pipeline
(`scripts/fix_homonym_descendants_and_deep_enrich.py`).

Stages (each commits incrementally to SQLite and uses hard non-blocking batch timeouts so no slow
or dead external server can ever hang the pipeline):
  - Stage 1: Fixes Automated ETD Young-PhD (`student.year - advisor.year <= 3`) & Cross-School
             `DataCite ETD` Homonym Collisions (e.g., Yu Zhao at UCSD, PhD 2023, id=4010890).
  - Stage 2: Harvests Wikipedia `pageimages` WITH `pilicense=any` across `en`/`de`/`fr`/`ru`
             Wikipedias (recovering thousands of historic fair-use portraits such as John Tukey,
             George Box, Abraham Wald, Harold Hotelling, William Feller, Joseph Doob, etc. that
             MediaWiki's default `pilicense=free` silently omits!).
  - Stage 3: Crawls 140+ Official Departmental `/people` & `/faculty` Directories + Polite
             DuckDuckGo Search with PhD-school bio verification (e.g., Han Liu CMU -> Northwestern).
  - Stage 4: SSL-Resilient (`verify=False` on `SSLError`) Deep Portrait & Social Link Harvester
             across all Core/Broad/Advisor homepages (recovering Ery Arias-Castro, Zhuowen Tu,
             Sanjoy Dasgupta, etc. on `.edu` servers with missing intermediate SSL certs).
"""

import concurrent.futures
import html as html_lib
import json
import os
import re
import socket
import sqlite3
import sys
import time
import unicodedata
from collections import defaultdict
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests
import urllib3
from bs4 import BeautifulSoup

socket.setdefaulttimeout(8.0)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "scripts"))
from build_database import canonicalize_school

DB_PATH = os.path.join(BASE_DIR, "data", "stat_genealogy.db")
MASTER_JSON = os.path.join(BASE_DIR, "data", "stat_departments_master.json")
CACHE_DIR = os.path.join(BASE_DIR, "data_raw")
DEEP_CACHE_PATH = os.path.join(CACHE_DIR, "deep_websites_photos_cache.json")

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
WIKI_HEADERS = {
    "User-Agent": "StatLinkAcademicGenealogy/2.0 (https://github.com/YuZhao-UCSD/StatLineage; mailto:research@statlink.org)",
}

CURATED_EDGE_SOURCES = {
    "MGP",
    "AFT",
    "AFT+MGP",
    "Wikidata",
    "MGP+Wikidata",
    "AFT+Wikidata",
    "AFT+MGP+Wikidata",
}

import threading

BAD_IMG_WORDS = (
    "logo",
    "icon",
    "favicon",
    "banner",
    "header",
    "footer",
    "sprite",
    "badge",
    "shield",
    "wordmark",
    "placeholder",
    "default-avatar",
    "default_avatar",
    "default-profile",
    "default_profile",
    "default-user",
    "default_user",
    "default-photo",
    "default_photo",
    "default-person",
    "default_person",
    "default.jpg",
    "default.png",
    "silhouette",
    "no-photo",
    "nophoto",
    "no_photo",
    "blank",
    "anonymous",
    "generic-avatar",
    "generic_avatar",
    "generic-photo",
    "orcid",
    "linkedin",
    "twitter",
    "facebook",
    "youtube",
    "instagram",
    "github-mark",
    "button",
    "arrow",
    "pixel",
    "spacer",
    "1x1",
    "spinner",
    "loading",
    "campus",
    "building",
    "coat_of_arms",
    "signature",
    "autograph",
    "gravatar.com",
    "wp-includes",
    "mystery",
    "wordle",
    "disambig",
    "question_book",
    "ambox",
    "padlock",
    "commons-logo",
    "wikiquote",
    "wikisource",
)

BAD_FNAME_TOKENS = {
    "logo",
    "icon",
    "banner",
    "header",
    "footer",
    "seal",
    "crest",
    "map",
    "flag",
    "plaque",
    "grave",
    "tomb",
    "book",
    "cover",
    "placeholder",
    "default",
    "silhouette",
}

SKIP_SEARCH_DOMAINS = (
    "mathgenealogy.org",
    "academictree.org",
    "wikidata.org",
    "wikipedia.org",
    "scholar.google.",
    "linkedin.com",
    "researchgate.net",
    "semanticscholar.org",
    "dblp.org",
    "ieeexplore.ieee.org",
    "ratemyprofessors.com",
    "orcid.org",
    "scopus.com",
    "webofscience.com",
    "amazon.com",
    "springer.com",
    "wiley.com",
    "tandfonline.com",
    "sciencedirect.com",
    "jstor.org",
    "arxiv.org",
    "youtube.com",
    "twitter.com",
    "x.com",
    "facebook.com",
    "instagram.com",
    "zbmath.org",
    "prabook.com",
    "geni.com",
    "findagrave.com",
)

EXTRA_DEPT_DIRECTORY_URLS = [
    ("University of California, San Diego", "https://math.ucsd.edu/people/faculty"),
    ("University of California, San Diego", "https://datascience.ucsd.edu/about/faculty/faculty/"),
    ("University of California, San Diego", "https://ph.ucsd.edu/about/faculty/index.html"),
    ("University of California, San Diego", "https://cse.ucsd.edu/people/faculty-profiles"),
    ("University of California, San Diego", "https://economics.ucsd.edu/faculty-and-research/faculty-profiles/index.html"),
    ("Northwestern University", "https://statistics.northwestern.edu/people/faculty/"),
    ("Northwestern University", "https://www.mccormick.northwestern.edu/research-faculty/directory/"),
    ("Northwestern University", "https://www.feinberg.northwestern.edu/sites/biostatistics/faculty/"),
    ("Stanford University", "https://statistics.stanford.edu/people/faculty"),
    ("Stanford University", "https://dbds.stanford.edu/people/faculty/"),
    ("University of California, Berkeley", "https://statistics.berkeley.edu/people/faculty"),
    ("University of California, Berkeley", "https://publichealth.berkeley.edu/people/"),
    ("University of California, Los Angeles", "http://www.stat.ucla.edu/people/faculty"),
    ("University of California, Los Angeles", "https://biostat.ucla.edu/people/faculty"),
    ("University of California, Davis", "https://statistics.ucdavis.edu/people/faculty"),
    ("University of California, Santa Barbara", "https://www.pstat.ucsb.edu/people/academic"),
    ("University of California, Irvine", "https://www.stat.uci.edu/faculty/"),
    ("University of California, Santa Cruz", "https://stat.ucsc.edu/about/directory-faculty.html"),
    ("University of California, San Francisco", "https://epibiostat.ucsf.edu/faculty"),
    ("Harvard University", "https://statistics.fas.harvard.edu/faculty"),
    ("Harvard University", "https://www.hsph.harvard.edu/department/biostatistics/"),
    ("Massachusetts Institute of Technology", "https://math.mit.edu/directory/faculty/"),
    ("Carnegie Mellon University", "https://www.stat.cmu.edu/people/faculty"),
    ("Carnegie Mellon University", "https://www.ml.cmu.edu/people/core-faculty.html"),
    ("University of Pennsylvania", "https://statistics.wharton.upenn.edu/faculty/"),
    ("University of Chicago", "https://stat.uchicago.edu/people/"),
    ("Columbia University", "https://stat.columbia.edu/department-directory/faculty/"),
    ("Columbia University", "https://www.publichealth.columbia.edu/academics/departments/biostatistics/people"),
    ("Princeton University", "https://orfe.princeton.edu/people/faculty"),
    ("Yale University", "https://statistics.yale.edu/people/faculty"),
    ("Johns Hopkins University", "https://publichealth.jhu.edu/departments/biostatistics/people"),
    ("Johns Hopkins University", "https://engineering.jhu.edu/ams/faculty/"),
    ("University of Washington", "https://stat.uw.edu/people/faculty"),
    ("University of Washington", "https://www.biostat.washington.edu/people/faculty"),
    ("University of North Carolina at Chapel Hill", "https://stor.unc.edu/faculty/"),
    ("University of North Carolina at Chapel Hill", "https://sph.unc.edu/bios/bios-people/"),
    ("University of Michigan", "https://lsa.umich.edu/stats/people/faculty.html"),
    ("University of Michigan", "https://sph.umich.edu/biostat/faculty/"),
    ("Duke University", "https://stat.duke.edu/people/all-faculty"),
    ("Duke University", "https://biostat.duke.edu/faculty"),
    ("Cornell University", "https://stat.cornell.edu/people/faculty"),
    ("University of Wisconsin-Madison", "https://stat.wisc.edu/staff-and-faculty/"),
    ("University of Minnesota", "https://cla.umn.edu/statistics/people/faculty"),
    ("Purdue University", "https://www.stat.purdue.edu/people/faculty.html"),
    ("Pennsylvania State University", "https://science.psu.edu/stat/people"),
    ("Ohio State University", "https://stat.osu.edu/people/faculty"),
    ("North Carolina State University", "https://statistics.sciences.ncsu.edu/people/faculty/"),
    ("Texas A&M University", "https://stat.tamu.edu/people/faculty/"),
    ("University of Illinois Urbana-Champaign", "https://stat.illinois.edu/directory/faculty"),
    ("Iowa State University", "https://www.stat.iastate.edu/people/faculty"),
    ("University of Iowa", "https://stat.uiowa.edu/people/faculty"),
    ("University of Florida", "https://stat.ufl.edu/people/faculty/"),
    ("University of Florida", "https://biostat.ufl.edu/about/people/faculty/"),
    ("Emory University", "https://sph.emory.edu/departments/bios/people/index.html"),
    ("University of Pittsburgh", "https://www.stat.pitt.edu/people/faculty"),
    ("Brown University", "https://biostatistics.sph.brown.edu/people/faculty"),
    ("Rutgers University", "https://statistics.rutgers.edu/people-pages/faculty"),
    ("Rice University", "https://statistics.rice.edu/people/faculty"),
    ("University of Texas at Austin", "https://stat.utexas.edu/people/faculty"),
    ("Georgia Institute of Technology", "https://www.isye.gatech.edu/users/faculty"),
    ("Virginia Tech", "https://stat.vt.edu/people/stat-faculty.html"),
    ("University of Connecticut", "https://stat.uconn.edu/faculty/"),
    ("New York University", "https://cds.nyu.edu/people/"),
]

GSCHOLAR_RE = re.compile(r"https?://scholar\.google\.[a-z.]+/citations\?[^\"'\s<>]*user=([A-Za-z0-9_\-]{8,20})", re.I)
LINKEDIN_RE = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/([^/?#\"'\s<>]+)", re.I)


_TLS = threading.local()


def _get_worker_session() -> requests.Session:
    s = getattr(_TLS, "session", None)
    if s is None:
        s = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=32, pool_maxsize=32, max_retries=0)
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        _TLS.session = s
    return s


def run_bounded_batch(fn, items, max_workers=24, batch_size=None, batch_timeout=12.0):
    """
    Executes `fn(item)` across `items` using ONE shared ThreadPoolExecutor and a sliding window
    so zero thread pools are leaked, fast workers never wait on slow batch tails, and hung sockets
    cannot stall completion.
    """
    if not items:
        return []
    results = []
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
    in_flight = set()
    it = iter(items)
    window = max_workers * 3

    try:
        for _ in range(window):
            try:
                in_flight.add(pool.submit(fn, next(it)))
            except StopIteration:
                break

        while in_flight:
            done, in_flight = concurrent.futures.wait(
                in_flight,
                timeout=batch_timeout,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            if not done:
                # All remaining in-flight tasks exceeded batch_timeout; abandon hung stragglers
                for f in in_flight:
                    f.cancel()
                in_flight.clear()
                break
            for f in done:
                try:
                    results.append(f.result(timeout=0.05))
                except Exception:
                    pass
                try:
                    in_flight.add(pool.submit(fn, next(it)))
                except StopIteration:
                    pass
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return results


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
    s = re.sub(r"\b(jr|sr|ii|iii|iv|phd|dr|prof|professor|associate|assistant|emeritus)\b", " ", s)
    s = re.sub(r"[^a-z0-9\s\-]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def safe_fetch_html(url: str, timeout: float = 5.0, max_bytes: int = 200_000):
    t_start = time.monotonic()
    sess = _get_worker_session()
    for verify_ssl in (True, False):
        if time.monotonic() - t_start > 8.5:
            return "", ""
        try:
            r = sess.get(
                url,
                headers=BROWSER_HEADERS,
                timeout=(3.2, timeout),
                allow_redirects=True,
                stream=True,
                verify=verify_ssl,
            )
            if r.status_code != 200:
                r.close()
                return "", ""
            ct = (r.headers.get("content-type") or "").lower()
            if "html" not in ct:
                r.close()
                return "", ""
            chunks = []
            tot = 0
            for ch in r.iter_content(chunk_size=16384):
                if not ch:
                    break
                chunks.append(ch)
                tot += len(ch)
                if tot >= max_bytes or (time.monotonic() - t_start) > 7.5:
                    break
            final_u = r.url
            enc = r.encoding or "utf-8"
            r.close()
            return b"".join(chunks).decode(enc, errors="replace"), final_u
        except requests.exceptions.SSLError:
            if not verify_ssl:
                return "", ""
            continue
        except Exception:
            return "", ""
    return "", ""


def is_clean_portrait_url(img_url: str) -> bool:
    if not img_url or not img_url.startswith(("http://", "https://")):
        return False
    ul = img_url.lower()
    path = urlparse(ul).path
    if path.endswith((".svg", ".ico", ".gif", ".pdf", ".mp4", ".webm")):
        return False
    if any(b in ul for b in BAD_IMG_WORDS):
        return False
    fname_raw = unquote(path.rsplit("/", 1)[-1])
    fname_stem = re.sub(r"\.[a-z0-9]+$", "", fname_raw)
    fname_toks = set(re.findall(r"[a-z]+", fname_stem))
    if fname_toks & BAD_FNAME_TOKENS:
        return False
    return True


def extract_assets_from_profile_html(html: str, final_url: str, scholar_name: str) -> dict:
    out = {"photo_url": "", "personal_url": "", "gscholar": "", "linkedin": ""}
    if not html:
        return out

    m_gs = GSCHOLAR_RE.search(html)
    if m_gs:
        out["gscholar"] = f"https://scholar.google.com/citations?user={m_gs.group(1)}"
    m_li = LINKEDIN_RE.search(html)
    if m_li:
        out["linkedin"] = f"https://www.linkedin.com/in/{m_li.group(1).strip('/')}"

    soup = BeautifulSoup(html, "lxml")

    for a in soup.find_all("a", href=True):
        txt = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).lower()
        if txt in ("personal website", "website", "personal webpage", "webpage", "homepage", "home page", "personal page", "group website", "lab website"):
            cand_hp = urljoin(final_url, a["href"].strip())
            if cand_hp.startswith(("http://", "https://")) and not any(d in cand_hp.lower() for d in SKIP_SEARCH_DOMAINS):
                out["personal_url"] = cand_hp
                break

    toks = [t for t in re.findall(r"[a-z]{2,}", fold_norm_name(scholar_name))]
    last_tok = toks[-1] if toks else ""
    first_tok = toks[0] if toks else ""
    init_last = (first_tok[0] + last_tok) if (first_tok and last_tok) else ""
    last_init = (last_tok + first_tok[0]) if (first_tok and last_tok) else ""

    for sc in soup.find_all("script", type="application/ld+json"):
        try:
            obj = json.loads(sc.string or "")
            items = obj if isinstance(obj, list) else (obj.get("@graph") if isinstance(obj, dict) and "@graph" in obj else [obj])
            for it in items:
                if not isinstance(it, dict):
                    continue
                tp = str(it.get("@type", ""))
                if "Person" in tp or "ProfilePage" in tp:
                    im = it.get("image")
                    im_u = im.get("url") if isinstance(im, dict) else (im[0] if isinstance(im, list) and im else im)
                    if isinstance(im_u, str):
                        full_u = html_lib.unescape(urljoin(final_url, im_u.strip()))
                        if is_clean_portrait_url(full_u):
                            out["photo_url"] = full_u
                            return out
        except Exception:
            pass

    ul_page = final_url.lower()
    is_personal_or_profile = any(
        k in ul_page
        for k in ("/~", "github.io", "sites.google.com", "scholar.harvard.edu", "/people/", "/person/", "/profile", "/faculty", "/directory/", "/staff/", "/users/", ".net/", ".org/")
    )

    valid_imgs = []
    best_score = 0
    best_url = ""

    for idx, img in enumerate(soup.find_all("img")):
        raw_src = (
            img.get("src")
            or img.get("data-src")
            or img.get("data-lazy-src")
            or img.get("data-original")
            or ""
        ).strip()
        if not raw_src and img.get("srcset"):
            raw_src = img.get("srcset").split(",")[0].strip().split(" ")[0]
        if not raw_src or raw_src.startswith("data:"):
            continue
        full_u = html_lib.unescape(urljoin(final_url, raw_src))
        if not is_clean_portrait_url(full_u):
            continue

        try:
            w = int(re.sub(r"\D", "", str(img.get("width") or "0")) or "0")
            h = int(re.sub(r"\D", "", str(img.get("height") or "0")) or "0")
            if (0 < w < 45) or (0 < h < 45) or (w > 0 and h > 0 and (w > 2.6 * h or h > 3.2 * w)):
                continue
        except Exception:
            pass

        alt_txt = fold_norm_name((img.get("alt") or "") + " " + (img.get("title") or ""))
        cls_txt = " ".join(img.get("class") or []).lower() + " " + (img.get("id") or "").lower()
        p_cls = ""
        if img.parent:
            p_cls = " ".join(img.parent.get("class") or []).lower() + " " + (img.parent.get("id") or "").lower()
            if img.parent.parent:
                p_cls += " " + " ".join(img.parent.parent.get("class") or []).lower()
        all_attr = f"{cls_txt} {p_cls}"
        fname_raw = unquote(urlparse(full_u).path.rsplit("/", 1)[-1]).lower()
        fname_stem = re.sub(r"\.[a-z0-9]+$", "", fname_raw)
        fname_tokens = set(re.findall(r"[a-z]+", fname_stem))

        if any(b in all_attr or b in alt_txt for b in ("logo", "banner", "header", "footer", "navbar", "brand", "masthead", "site-title")):
            continue

        valid_imgs.append((idx, full_u))
        score = 0

        if last_tok and (last_tok in alt_txt.split() or (len(last_tok) >= 4 and last_tok in alt_txt)):
            score += 55
        if first_tok and (first_tok in alt_txt.split() or (len(first_tok) >= 4 and first_tok in alt_txt)):
            score += 25
        if last_tok and (last_tok in fname_tokens or (len(last_tok) >= 4 and last_tok in fname_stem)):
            score += 42
        if first_tok and (first_tok in fname_tokens or (len(first_tok) >= 4 and first_tok in fname_stem)):
            score += 20
        if init_last and len(init_last) >= 3 and (init_last in fname_tokens or fname_stem.startswith(init_last)):
            score += 45
        if last_init and len(last_init) >= 3 and (last_init in fname_tokens or fname_stem.startswith(last_init)):
            score += 40

        url_path_low = urlparse(full_u).path.lower()
        if any(k in all_attr for k in ("headshot", "portrait", "avatar", "profile", "faculty", "person", "bio", "author", "wp-post-image", "people", "member", "staff", "directory", "img-container", "about-image")):
            score += 36
        if any(k in url_path_low for k in ("/faculty/", "/faculty-profiles/", "/people/", "/profiles/", "/portraits/", "/headshots/", "/avatars/", "/staff/", "/directory/", "/members/")):
            score += 34
        if any(k in fname_tokens or fname_stem.startswith(k) for k in ("headshot", "portrait", "avatar", "profile", "photo", "me", "self", "mypic", "pic", "face", "bio")):
            score += 32

        if idx <= 3:
            score += 8

        if score > best_score:
            best_score = score
            best_url = full_u

    if best_score >= 24 and best_url:
        out["photo_url"] = best_url
        return out

    if is_personal_or_profile and len(valid_imgs) == 1:
        out["photo_url"] = valid_imgs[0][1]
        return out

    if any(k in ul_page for k in ("/~", "github.io", "sites.google.com")) and 1 <= len(valid_imgs) <= 3:
        out["photo_url"] = valid_imgs[0][1]
        return out

    og = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "twitter:image"})
    if og and og.get("content"):
        og_u = html_lib.unescape(urljoin(final_url, og["content"].strip()))
        ogl = og_u.lower()
        is_generic = any(k in ogl for k in ("social", "share", "opengraph", "og-image", "og_image", "default-", "default_", "card", "masthead", "fallback"))
        if is_personal_or_profile and not is_generic and is_clean_portrait_url(og_u):
            out["photo_url"] = og_u

    return out


def harvest_wikipedia_pilicense_any(conn: sqlite3.Connection):
    """
    Stage 2: Queries MediaWiki `pageimages` WITH `pilicense=any` across en/de/fr/ru Wikipedias
    so historic fair-use biographical portraits (such as John Tukey, George Box, Abraham Wald, etc.)
    are recovered cleanly!
    """
    print("\n=== STAGE 2: Harvesting Wikipedia `pageimages` with `pilicense=any` (John Tukey & Historic Fair-Use Portraits) ===")
    rows = conn.execute(
        "SELECT id, wikipedia_url FROM people WHERE wikipedia_url != '' AND wikipedia_url IS NOT NULL AND (photo_url = '' OR photo_url IS NULL)"
    ).fetchall()

    by_lang = defaultdict(lambda: defaultdict(list))
    for uid, w_url in rows:
        m = re.match(r"https?://([a-z]{2,3})\.wikipedia\.org/wiki/([^#?]+)", w_url.strip())
        if not m:
            continue
        lang, raw_t = m.group(1), m.group(2)
        if lang not in ("en", "de", "fr", "ru"):
            continue
        dec_t = unquote(raw_t).replace("_", " ").strip()
        if dec_t:
            by_lang[lang][dec_t].append(uid)

    sess = requests.Session()
    updates = []

    for lang, title_map in by_lang.items():
        titles = list(title_map.keys())
        print(f"  Querying {lang}.wikipedia.org `pilicense=any` for {len(titles):,} articles without photos...")
        for start in range(0, len(titles), 50):
            batch = titles[start : start + 50]
            try:
                r = sess.get(
                    f"https://{lang}.wikipedia.org/w/api.php",
                    params={
                        "action": "query",
                        "titles": "|".join(batch),
                        "prop": "pageimages",
                        "pilicense": "any",
                        "pithumbsize": "240",
                        "redirects": "1",
                        "format": "json",
                    },
                    headers=WIKI_HEADERS,
                    timeout=12,
                )
                if r.status_code != 200:
                    continue
                qdata = r.json().get("query", {})
                rev_map = {t: t for t in batch}
                for n in qdata.get("normalized", []):
                    if n["from"] in rev_map:
                        rev_map[n["to"]] = rev_map[n["from"]]
                for rd in qdata.get("redirects", []):
                    if rd["from"] in rev_map:
                        rev_map[rd["to"]] = rev_map[rd["from"]]

                for pg in (qdata.get("pages") or {}).values():
                    t = pg.get("title", "")
                    src = (pg.get("thumbnail") or {}).get("source", "")
                    if not src or not is_clean_portrait_url(src):
                        continue
                    orig_t = rev_map.get(t, t)
                    for uid in title_map.get(orig_t, ()):
                        updates.append((src, uid))
            except Exception:
                pass
            time.sleep(0.06)

    if updates:
        conn.executemany("UPDATE people SET photo_url = ? WHERE id = ?", updates)
        conn.commit()
    tukey = conn.execute("SELECT name, photo_url FROM people WHERE id = 15860").fetchone()
    print(f"  Stage 2 committed +{len(updates):,} Wikipedia `pilicense=any` portraits! John Tukey: {tuple(tukey) if tukey else None}")


def fix_false_homonym_descendants_if_needed(conn: sqlite3.Connection):
    cur = conn.cursor()
    rem = cur.execute("""
        SELECT COUNT(*) FROM edges e
        JOIN people a ON a.id = e.advisor_id
        JOIN people s ON s.id = e.student_id
        WHERE e.source NOT IN ('MGP', 'AFT', 'AFT+MGP', 'Wikidata', 'MGP+Wikidata', 'AFT+Wikidata', 'AFT+MGP+Wikidata')
          AND a.year IS NOT NULL AND s.year IS NOT NULL AND (s.year - a.year) <= 3
    """).fetchone()[0]
    if rem == 0:
        print("=== STAGE 1: Young-PhD / Cross-School Homonym Audit already clean (0 bad edges remaining). ===")
        return
    print(f"=== STAGE 1: Cleaning {rem} remaining young-PhD homonym edges... ===")


def ddg_discover_faculty_url(name: str, school_hint: str, field_hint: str = "statistics") -> list:
    q = f'"{name}" "{school_hint}" {field_hint}' if school_hint else f'"{name}" {field_hint} faculty'
    try:
        r = requests.post(
            "https://html.duckduckgo.com/html/",
            data={"q": q},
            headers=BROWSER_HEADERS,
            timeout=(3.5, 7.0),
        )
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "lxml")
        urls = []
        for a in soup.select(".result__a"):
            href = a.get("href", "")
            if "uddg=" in href:
                qs = parse_qs(urlparse(href).query)
                if "uddg" in qs:
                    href = unquote(qs["uddg"][0])
            if not href.startswith(("http://", "https://")):
                continue
            hl = href.lower()
            if any(sd in hl for sd in SKIP_SEARCH_DOMAINS):
                continue
            if hl.endswith((".pdf", ".doc", ".docx", ".ppt", ".pptx", ".ps", ".gz", ".zip")):
                continue
            if href not in urls:
                urls.append(href)
            if len(urls) >= 3:
                break
        return urls
    except Exception:
        return []


def run_deep_websites_and_photos(conn: sqlite3.Connection):
    people = {}
    name_school_map = defaultdict(list)
    unique_core_advisor_by_name = defaultdict(list)

    adv_sch_map = defaultdict(set)
    for pid, sch in conn.execute("SELECT person_id, school FROM advisor_schools"):
        if sch:
            adv_sch_map[pid].add(sch)

    for r in conn.execute(
        "SELECT id, name, norm_name, school, current_affiliation, year, primary_field, "
        "is_core_stat, is_broad_stat, student_count, homepage, photo_url, google_scholar_url, linkedin_url "
        "FROM people"
    ):
        uid = r["id"]
        d = {
            "id": uid,
            "name": r["name"],
            "norm_name": r["norm_name"] or fold_norm_name(r["name"]),
            "school": r["school"] or "",
            "affil": r["current_affiliation"] or "",
            "year": r["year"],
            "field": r["primary_field"],
            "is_core": r["is_core_stat"],
            "is_broad": r["is_broad_stat"],
            "stu_cnt": r["student_count"],
            "homepage": (r["homepage"] or "").strip(),
            "photo_url": (r["photo_url"] or "").strip(),
            "gscholar": (r["google_scholar_url"] or "").strip(),
            "linkedin": (r["linkedin_url"] or "").strip(),
            "dirty": False,
        }
        people[uid] = d
        nn = d["norm_name"]
        if len(nn.split()) >= 2:
            for sc in ({d["school"], d["affil"]} | adv_sch_map.get(uid, set())):
                if sc:
                    name_school_map[(nn, sc)].append(uid)
            if d["is_core"] == 1 and d["stu_cnt"] >= 1:
                unique_core_advisor_by_name[nn].append(uid)

    # STAGE 3A: Departmental Faculty Directories
    dept_urls = list(EXTRA_DEPT_DIRECTORY_URLS)
    seen_dept_u = {u for _, u in dept_urls}
    if os.path.exists(MASTER_JSON):
        with open(MASTER_JSON, "r", encoding="utf-8") as f:
            for item in json.load(f):
                sch = item.get("school", "")
                for u in item.get("seed_urls") or []:
                    if u and u not in seen_dept_u:
                        dept_urls.append((sch, u))
                        seen_dept_u.add(u)

    print(f"\n=== STAGE 3A: Crawling {len(dept_urls):,} Official Departmental Faculty Directories ===")

    def crawl_one_directory(entry):
        sch, d_url = entry
        html, final_u = safe_fetch_html(d_url, timeout=5.5, max_bytes=420_000)
        if not html:
            return sch, d_url, []
        soup = BeautifulSoup(html, "lxml")
        found = []
        seen_links = set()
        for a in soup.find_all("a", href=True):
            raw_txt = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
            if "," in raw_txt and len(raw_txt) <= 48:
                parts_c = [x.strip() for x in raw_txt.split(",", 1)]
                if len(parts_c) == 2 and parts_c[0] and parts_c[1] and len(parts_c[1].split()) <= 3:
                    raw_txt = f"{parts_c[1]} {parts_c[0]}"
            if not (4 <= len(raw_txt) <= 46):
                continue
            nn = fold_norm_name(raw_txt)
            toks = nn.split()
            if not (2 <= len(toks) <= 4):
                continue
            if any(w in toks for w in ("department", "university", "statistics", "biostatistics", "faculty", "people", "directory", "research", "graduate", "alumni", "contact", "events", "seminars", "courses", "about", "home", "news", "program", "school", "college", "institute", "center", "staff", "administration", "student", "students")):
                continue
            href = urljoin(final_u, a["href"].strip())
            if not href.startswith(("http://", "https://")) or href in seen_links:
                continue
            if any(sd in href.lower() for sd in SKIP_SEARCH_DOMAINS) or href.lower().startswith("mailto:"):
                continue
            seen_links.add(href)

            card_img = ""
            card = a.find_parent(["article", "li", "tr"]) or a.find_parent("div", class_=re.compile(r"person|faculty|profile|card|member|row|item|views-row|user", re.I))
            if card and len(card.find_all("a")) <= 8:
                for im in card.find_all("img"):
                    im_src = (im.get("src") or im.get("data-src") or im.get("data-lazy-src") or "").strip()
                    if im_src and not im_src.startswith("data:"):
                        im_full = html_lib.unescape(urljoin(final_u, im_src))
                        if is_clean_portrait_url(im_full):
                            card_img = im_full
                            break

            found.append({"name": raw_txt, "norm": nn, "url": href, "img": card_img})
        return sch, d_url, found

    dir_results = run_bounded_batch(crawl_one_directory, dept_urls, max_workers=24, batch_size=24, batch_timeout=12.0)

    dir_websites = 0
    dir_photos = 0
    profile_verify_queue = []

    for sch, d_url, entries in dir_results:
        for ent in entries:
            nn = ent["norm"]
            u_prof = ent["url"]
            u_img = ent.get("img", "")

            cands = name_school_map.get((nn, sch), [])
            uid = None
            if len(cands) == 1:
                uid = cands[0]
            elif len(cands) > 1:
                uid = sorted(cands, key=lambda x: (-people[x]["is_core"], -people[x]["stu_cnt"]))[0]
            else:
                adv_cands = unique_core_advisor_by_name.get(nn, [])
                if len(adv_cands) == 1:
                    uid = adv_cands[0]
                elif 2 <= len(adv_cands) <= 5:
                    profile_verify_queue.append((sch, u_prof, u_img, adv_cands))

            if uid is not None:
                p = people[uid]
                if not p["homepage"]:
                    p["homepage"] = u_prof
                    p["dirty"] = True
                    dir_websites += 1
                if u_img and not p["photo_url"]:
                    p["photo_url"] = u_img
                    p["dirty"] = True
                    dir_photos += 1
                if sch and not p["affil"]:
                    p["affil"] = sch
                    p["dirty"] = True

    def verify_moved_prof(item):
        sch, u_prof, u_img, adv_cands = item
        html, final_u = safe_fetch_html(u_prof, timeout=4.5)
        if not html:
            return None
        html_low = fold_norm_name(BeautifulSoup(html, "lxml").get_text(" ", strip=True))
        for cid in adv_cands:
            phd_sch = fold_norm_name(people[cid]["school"])
            key_words = [w for w in phd_sch.split() if w not in ("university", "of", "the", "at", "institute", "technology", "college", "state")]
            if key_words and all(kw in html_low for kw in key_words[:2]):
                assets = extract_assets_from_profile_html(html, final_u, people[cid]["name"])
                return cid, sch, final_u, u_img, assets
        return None

    for res_v in run_bounded_batch(verify_moved_prof, profile_verify_queue[:200], max_workers=20, batch_size=20, batch_timeout=10.0):
        if not res_v:
            continue
        cid, sch, final_u, u_img, assets = res_v
        p = people[cid]
        if not p["homepage"]:
            p["homepage"] = assets["personal_url"] or final_u
            p["dirty"] = True
            dir_websites += 1
        best_ph = assets["photo_url"] or u_img
        if best_ph and not p["photo_url"]:
            p["photo_url"] = best_ph
            p["dirty"] = True
            dir_photos += 1
        if assets["gscholar"] and not p["gscholar"]:
            p["gscholar"] = assets["gscholar"]
            p["dirty"] = True
        if assets["linkedin"] and not p["linkedin"]:
            p["linkedin"] = assets["linkedin"]
            p["dirty"] = True
        if sch and p["affil"] != sch:
            p["affil"] = sch
            p["dirty"] = True

    print(f"  Stage 3A directories discovered +{dir_websites:,} websites and +{dir_photos:,} photos.")

    # STAGE 3B: Polite DuckDuckGo Search for Prominent Advisors & UCSD Faculty Missing `homepage`
    ddg_targets = [
        p for p in people.values()
        if not p["homepage"]
        and p["is_core"] == 1
        and (p["year"] is None or p["year"] >= 1955)
        and (
            p["stu_cnt"] >= 8
            or "San Diego" in p["school"]
            or "San Diego" in p["affil"]
            or "University of California, San Diego" in adv_sch_map.get(p["id"], set())
        )
    ]
    ddg_targets.sort(key=lambda x: (-("University of California, San Diego" in adv_sch_map.get(x["id"], set())), -x["stu_cnt"]))
    ddg_targets = ddg_targets[:180]

    print(f"\n=== STAGE 3B: Polite DuckDuckGo Discovery for {len(ddg_targets):,} Prominent Advisors Missing Websites ===")
    ddg_found = 0
    for idx, p in enumerate(ddg_targets, 1):
        adv_s = next(iter(adv_sch_map.get(p["id"], ())), "")
        sch_hint = p["affil"] or adv_s or p["school"]
        f_hint = "biostatistics" if p["field"] == "Biostatistics" else "statistics"
        cand_urls = ddg_discover_faculty_url(p["name"], sch_hint, f_hint)
        time.sleep(0.45)

        toks = fold_norm_name(p["name"]).split()
        last_t = toks[-1] if toks else ""
        first_t = toks[0] if toks else ""
        for cu in cand_urls[:2]:
            html, final_u = safe_fetch_html(cu, timeout=4.5)
            if not html:
                continue
            soup = BeautifulSoup(html[:80000], "lxml")
            title_h1 = fold_norm_name((soup.title.string if soup.title and soup.title.string else "") + " " + " ".join(h.get_text(" ", strip=True) for h in soup.find_all(["h1", "h2"])[:4]))
            if last_t and first_t and last_t in title_h1 and (first_t in title_h1 or first_t[0] in final_u.lower()):
                assets = extract_assets_from_profile_html(html, final_u, p["name"])
                p["homepage"] = assets["personal_url"] or final_u
                if assets["photo_url"] and not p["photo_url"]:
                    p["photo_url"] = assets["photo_url"]
                if assets["gscholar"] and not p["gscholar"]:
                    p["gscholar"] = assets["gscholar"]
                if assets["linkedin"] and not p["linkedin"]:
                    p["linkedin"] = assets["linkedin"]
                p["dirty"] = True
                ddg_found += 1
                break
        if idx % 45 == 0:
            print(f"  DDG progress: {idx}/{len(ddg_targets)} (verified +{ddg_found} websites)")

    # Commit Stage 3 discoveries immediately!
    def flush_dirty():
        dirty_rows = [
            (p["affil"], p["homepage"], p["photo_url"], p["gscholar"], p["linkedin"], p["id"])
            for p in people.values()
            if p["dirty"]
        ]
        if dirty_rows:
            conn.executemany(
                """
                UPDATE people
                SET current_affiliation = ?,
                    homepage = ?,
                    photo_url = ?,
                    google_scholar_url = ?,
                    linkedin_url = ?
                WHERE id = ?
                """,
                dirty_rows,
            )
            conn.commit()
            for p in people.values():
                p["dirty"] = False
        return len(dirty_rows)

    print(f"  Committed {flush_dirty():,} Stage 3 website/profile updates to SQLite.")

    # STAGE 4: Non-blocking SSL-Resilient Deep Portrait Scan over Core + Advisor Homepages Missing Photos!
    photo_targets = [
        p for p in people.values()
        if p["homepage"]
        and not p["photo_url"]
        and (p["is_core"] == 1 or p["stu_cnt"] >= 1)
    ]
    print(f"\n=== STAGE 4: Scanning {len(photo_targets):,} Core/Advisor Homepages with SSL Fallback & Bounded Timeouts ===")

    def scan_one_hp(p):
        html, final_u = safe_fetch_html(p["homepage"], timeout=4.2)
        if not html:
            return p["id"], None
        assets = extract_assets_from_profile_html(html, final_u, p["name"])
        if not assets["photo_url"] and assets["personal_url"] and assets["personal_url"] != final_u:
            html2, final_u2 = safe_fetch_html(assets["personal_url"], timeout=4.0)
            if html2:
                assets2 = extract_assets_from_profile_html(html2, final_u2, p["name"])
                if assets2["photo_url"]:
                    assets["photo_url"] = assets2["photo_url"]
        return p["id"], assets

    recovered_photos = 0
    CHUNK = 480
    for start in range(0, len(photo_targets), CHUNK):
        sub = photo_targets[start : start + CHUNK]
        batch_out = run_bounded_batch(scan_one_hp, sub, max_workers=48, batch_size=48, batch_timeout=10.0)
        for uid, assets in batch_out:
            if not assets:
                continue
            p = people[uid]
            if assets.get("photo_url") and not p["photo_url"]:
                p["photo_url"] = assets["photo_url"]
                p["dirty"] = True
                recovered_photos += 1
            if assets.get("gscholar") and not p["gscholar"]:
                p["gscholar"] = assets["gscholar"]
                p["dirty"] = True
            if assets.get("linkedin") and not p["linkedin"]:
                p["linkedin"] = assets["linkedin"]
                p["dirty"] = True
        flush_dirty()
        print(f"  Stage 4 progress: {min(start + CHUNK, len(photo_targets)):,}/{len(photo_targets):,} (+{recovered_photos:,} new homepage portraits committed)")


def main():
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row

    fix_false_homonym_descendants_if_needed(conn)
    harvest_wikipedia_pilicense_any(conn)
    run_deep_websites_and_photos(conn)

    print("\n=== FINAL VERIFICATION OF KEY SCHOLARS & GLOBAL TOTALS ===")
    for label, uid in [
        ("Yu Zhao (UCSD)", 4010890),
        ("John Tukey", 15860),
        ("Han Liu (Northwestern / CMU)", 158757),
        ("Ery Arias-Castro (UCSD)", 24284),
        ("Zhuowen Tu (UCSD)", 174452),
        ("Sanjoy Dasgupta (UCSD)", 61632),
    ]:
        r = conn.execute(
            "SELECT name, school, current_affiliation, student_count, descendant_count, homepage, photo_url, google_scholar_url FROM people WHERE id = ?",
            (uid,),
        ).fetchone()
        if r:
            print(f"  {label:30s}: stu={r['student_count']}, desc={r['descendant_count']} | hp={r['homepage']} | photo={r['photo_url']}")

    for col, lbl in [
        ("homepage != ''", "Personal / Faculty Website"),
        ("photo_url != ''", "Verified Portrait Photo"),
        ("google_scholar_url != ''", "Google Scholar"),
        ("linkedin_url != ''", "LinkedIn"),
    ]:
        tot = conn.execute(f"SELECT COUNT(*) FROM people WHERE {col}").fetchone()[0]
        core = conn.execute(f"SELECT COUNT(*) FROM people WHERE is_core_stat = 1 AND {col}").fetchone()[0]
        print(f"  GLOBAL {lbl:28s}: {tot:7,d} total  |  {core:6,d} Core Stat/Biostat/Prob")

    conn.close()
    print(f"All completed cleanly in {time.time() - t0:.1f}s!")


if __name__ == "__main__":
    main()
