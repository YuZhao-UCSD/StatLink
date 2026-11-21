#!/usr/bin/env python3
"""
Multi-Source Scholar Portrait Photo Harvester (`scripts/enrich_scholar_photos.py`).

Populates `people.photo_url` in `data/stat_genealogy.db` for the Dossier Profile Panel and
Directory Cards (which fall back seamlessly to 2-letter initials like `LZ` for "Lin Zheng"):
  - Stage 1: Wikidata `wdt:P18` (Portrait Image) across all MGP (`P549`), AcademicTree (`P2381`),
             and linked `wikidata_qid` scholars, normalized to fast 240px Wikimedia HTTPS thumbnails.
  - Stage 2: English Wikipedia `pageimages` API (`pithumbsize=240`, 50 titles/request) for any
             `en.wikipedia.org` biography lacking a Wikimedia Commons `P18` image.
  - Stage 3: Concurrent 24-worker Faculty & Personal Homepage Portrait Extractor over `is_core_stat = 1`
             homepages (`JSON-LD Person.image`, named/portrait `<img>` tags, and curated `og:image`),
             strictly filtering out logos, icons, banners, and generic university shields.
"""

import concurrent.futures
import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "data", "stat_genealogy.db")
CACHE_DIR = os.path.join(BASE_DIR, "data_raw")
os.makedirs(CACHE_DIR, exist_ok=True)
PHOTO_CACHE_PATH = os.path.join(CACHE_DIR, "scholar_photos_cache.json")

WD_SPARQL_URL = "https://query.wikidata.org/sparql"
HEADERS = {
    "User-Agent": "StatLinkAcademicGenealogy/2.0 (https://github.com/YuZhao-UCSD/StatLineage; mailto:research@statlink.org)",
}

BAD_IMG_SUBSTRINGS = (
    "logo",
    "icon",
    "favicon",
    "banner",
    "header",
    "footer",
    "sprite",
    "badge",
    "shield",
    "seal",
    "wordmark",
    "crest",
    "placeholder",
    "default-avatar",
    "default_avatar",
    "default-profile",
    "default_profile",
    "default-user",
    "default_user",
    "default-photo",
    "default_photo",
    "default.jpg",
    "default.png",
    "silhouette",
    "no-photo",
    "nophoto",
    "no_photo",
    "blank",
    "anonymous",
    "generic",
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
    "map",
    "flag",
    "coat_of_arms",
    "gravatar.com",
    "wp-includes",
    "signature",
    "autograph",
    "plaque",
    "grave",
    "tomb",
    "statue_of_",
)


def fold_ascii(text: str) -> str:
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch)).lower()


def normalize_commons_thumb(raw_url: str, width: int = 240) -> str:
    if not raw_url:
        return ""
    u = raw_url.strip()
    ul = u.lower()
    if ul.endswith((".svg", ".djvu", ".pdf", ".ogg", ".webm", ".ogv")):
        return ""
    if any(b in ul for b in ("signature", "autograph", "coat_of_arms", "plaque", "tombstone", "grave")):
        return ""
    if u.startswith("http://"):
        u = "https://" + u[7:]
    if "commons.wikimedia.org/wiki/Special:FilePath/" in u and "width=" not in u:
        sep = "&" if "?" in u else "?"
        u = f"{u}{sep}width={width}"
    return u


def ensure_photo_column(conn: sqlite3.Connection):
    existing = {r[1] for r in conn.execute("PRAGMA table_info(people)").fetchall()}
    if "photo_url" not in existing:
        print("Adding column `people.photo_url`...")
        conn.execute("ALTER TABLE people ADD COLUMN photo_url TEXT DEFAULT ''")
        conn.commit()


def run_sparql(sess: requests.Session, query: str) -> list:
    for attempt in range(4):
        try:
            r = sess.post(
                WD_SPARQL_URL,
                data={"query": query, "format": "json"},
                headers={**HEADERS, "Accept": "application/sparql-results+json"},
                timeout=50,
            )
            if r.status_code == 200:
                return r.json().get("results", {}).get("bindings", [])
            time.sleep(3.0 * (attempt + 1))
        except Exception:
            time.sleep(3.0 * (attempt + 1))
    return []


def harvest_wikidata_p18(target_qids: list) -> dict:
    qid_photos = {}
    sess = requests.Session()

    print("Stage 1A: Harvesting Wikidata P18 portraits for MGP (P549) & AcademicTree (P2381)...")
    for prop in ("P549", "P2381"):
        q = f"""
        SELECT ?item ?img WHERE {{
          ?item wdt:{prop} [] ;
                wdt:P18 ?img .
        }}
        """
        rows = run_sparql(sess, q)
        for b in rows:
            uri = b.get("item", {}).get("value", "")
            img = b.get("img", {}).get("value", "")
            if "/entity/Q" in uri and img:
                qid = uri.rsplit("/", 1)[-1]
                thumb = normalize_commons_thumb(img)
                if thumb and qid not in qid_photos:
                    qid_photos[qid] = thumb
        print(f"  {prop}: {len(rows):,} raw P18 bindings ({len(qid_photos):,} valid portraits so far)")
        time.sleep(0.4)

    rem_qids = [q for q in target_qids if q not in qid_photos and q.startswith("Q")]
    if rem_qids:
        print(f"Stage 1B: Checking {len(rem_qids):,} remaining linked QIDs for P18 portraits...")
        for start in range(0, len(rem_qids), 400):
            batch = rem_qids[start : start + 400]
            vals = " ".join(f"wd:{q}" for q in batch)
            q_str = f"SELECT ?item ?img WHERE {{ VALUES ?item {{ {vals} }} ?item wdt:P18 ?img . }}"
            for b in run_sparql(sess, q_str):
                uri = b.get("item", {}).get("value", "")
                img = b.get("img", {}).get("value", "")
                if "/entity/Q" in uri and img:
                    qid = uri.rsplit("/", 1)[-1]
                    thumb = normalize_commons_thumb(img)
                    if thumb and qid not in qid_photos:
                        qid_photos[qid] = thumb
            time.sleep(0.25)

    print(f"Stage 1 total Wikidata P18 portraits: {len(qid_photos):,}")
    return qid_photos


def harvest_wikipedia_pageimages(wiki_urls: list) -> dict:
    """Fetches 240px thumbnails in batches of 50 from en.wikipedia.org for articles lacking P18."""
    title_to_urls = {}
    for u in wiki_urls:
        if "en.wikipedia.org/wiki/" not in u:
            continue
        raw_title = u.split("en.wikipedia.org/wiki/", 1)[1].split("#")[0].split("?")[0]
        dec_title = unquote(raw_title).replace("_", " ").strip()
        if dec_title:
            title_to_urls.setdefault(dec_title, []).append(u)

    titles = list(title_to_urls.keys())
    print(f"Stage 2: Querying English Wikipedia `pageimages` for {len(titles):,} biographies...")
    url_to_thumb = {}
    sess = requests.Session()

    for start in range(0, len(titles), 50):
        batch = titles[start : start + 50]
        try:
            r = sess.get(
                "https://en.wikipedia.org/w/api.php",
                params={
                    "action": "query",
                    "titles": "|".join(batch),
                    "prop": "pageimages",
                    "pilicense": "any",
                    "pithumbsize": "240",
                    "redirects": "1",
                    "format": "json",
                },
                headers=HEADERS,
                timeout=15,
            )
            if r.status_code != 200:
                continue
            data = r.json().get("query", {})
            # Map normalized & redirected titles back to original batch strings
            rev_map = {t: t for t in batch}
            for n in data.get("normalized", []):
                if n["from"] in rev_map:
                    rev_map[n["to"]] = rev_map[n["from"]]
            for rd in data.get("redirects", []):
                if rd["from"] in rev_map:
                    rev_map[rd["to"]] = rev_map[rd["from"]]

            for pg in (data.get("pages") or {}).values():
                t = pg.get("title", "")
                src = (pg.get("thumbnail") or {}).get("source", "")
                if not src:
                    continue
                sl = src.lower()
                if sl.endswith(".svg") or any(b in sl for b in BAD_IMG_SUBSTRINGS):
                    continue
                orig_t = rev_map.get(t, t)
                for orig_u in title_to_urls.get(orig_t, ()):
                    url_to_thumb[orig_u] = src
        except Exception:
            pass
        time.sleep(0.1)

    print(f"Stage 2 recovered {len(url_to_thumb):,} additional Wikipedia portrait thumbnails.")
    return url_to_thumb


def is_clean_portrait_url(img_url: str) -> bool:
    if not img_url or not img_url.startswith(("http://", "https://")):
        return False
    ul = img_url.lower()
    path = urlparse(ul).path
    if path.endswith((".svg", ".ico", ".gif", ".pdf", ".mp4", ".webm")):
        return False
    if any(b in ul for b in BAD_IMG_SUBSTRINGS):
        return False
    return True


def extract_portrait_from_html(html: str, final_url: str, scholar_name: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    toks = [t for t in re.findall(r"[a-z]{2,}", fold_ascii(scholar_name)) if t not in ("jr", "sr", "ii", "iii")]
    last_tok = toks[-1] if toks else ""
    first_tok = toks[0] if toks else ""

    # 1. Check JSON-LD Person / ProfilePage schema
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
                        full_u = urljoin(final_url, im_u.strip())
                        if is_clean_portrait_url(full_u):
                            return full_u
        except Exception:
            pass

    # 2. Score `<img>` tags on the page
    best_score = 0
    best_url = ""
    for img in soup.find_all("img"):
        raw_src = (img.get("src") or img.get("data-src") or "").strip()
        if not raw_src and img.get("srcset"):
            raw_src = img.get("srcset").split(",")[0].strip().split(" ")[0]
        if not raw_src or raw_src.startswith("data:"):
            continue
        full_u = urljoin(final_url, raw_src)
        if not is_clean_portrait_url(full_u):
            continue

        # Check dimensions if present
        try:
            w = int(re.sub(r"\D", "", str(img.get("width") or "0")) or "0")
            h = int(re.sub(r"\D", "", str(img.get("height") or "0")) or "0")
            if (0 < w < 48) or (0 < h < 48) or (w > 0 and h > 0 and (w > 2.6 * h or h > 3.0 * w)):
                continue
        except Exception:
            pass

        alt_txt = fold_ascii((img.get("alt") or "") + " " + (img.get("title") or ""))
        cls_txt = " ".join(img.get("class") or []).lower() + " " + (img.get("id") or "").lower()
        p_cls = ""
        if img.parent:
            p_cls = " ".join(img.parent.get("class") or []).lower() + " " + (img.parent.get("id") or "").lower()
        all_attr = f"{cls_txt} {p_cls}"
        fname = fold_ascii(unquote(urlparse(full_u).path.rsplit("/", 1)[-1]))

        if any(b in all_attr or b in alt_txt for b in ("logo", "banner", "header", "footer", "icon", "navbar", "brand")):
            continue

        score = 0
        if last_tok and len(last_tok) >= 3 and last_tok in alt_txt:
            score += 55
        if first_tok and len(first_tok) >= 3 and first_tok in alt_txt:
            score += 25
        if any(k in all_attr for k in ("headshot", "portrait", "avatar", "profile", "faculty-photo", "person-photo", "bio-photo", "author", "wp-post-image", "people")):
            score += 40
        if last_tok and len(last_tok) >= 3 and last_tok in fname:
            score += 35
        if any(k in fname for k in ("headshot", "portrait", "avatar", "profile", "me.", "photo", "self")):
            score += 28

        if score > best_score:
            best_score = score
            best_url = full_u

    if best_score >= 35 and best_url:
        return best_url

    # 3. Fallback: curated `og:image` on personal sites or `/people/` / `/profile/` pages
    og = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "twitter:image"})
    if og and og.get("content"):
        og_u = urljoin(final_url, og["content"].strip())
        ogl = og_u.lower()
        ul_page = final_url.lower()
        is_profile_page = any(k in ul_page for k in ("/~", "github.io", "/people/", "/person/", "/profile", "/faculty/", "/staff/", "/author/", "sites.google.com"))
        is_generic_social = any(k in ogl for k in ("social", "share", "opengraph", "og-image", "og_image", "default", "card", " masthead", " fallback"))
        if is_profile_page and not is_generic_social and is_clean_portrait_url(og_u):
            return og_u

    return ""


def harvest_homepage_portraits(candidates: list) -> dict:
    """candidates: list of (uid, name, homepage_url)"""
    cached = {}
    if os.path.exists(PHOTO_CACHE_PATH):
        try:
            with open(PHOTO_CACHE_PATH, "r", encoding="utf-8") as f:
                cached = json.load(f)
        except Exception:
            cached = {}

    todo = [(uid, nm, hp) for (uid, nm, hp) in candidates if str(uid) not in cached]
    print(f"Stage 3: Scanning {len(todo):,} Core Stat faculty/personal homepages for portraits ({len(cached):,} cached)...")
    if not todo:
        return cached

    browser_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    def fetch_one(item):
        uid, nm, hp = item
        try:
            r = requests.get(hp, headers=browser_headers, timeout=5.5, allow_redirects=True, stream=True)
            if r.status_code != 200:
                r.close()
                return str(uid), ""
            ct = (r.headers.get("content-type") or "").lower()
            if "html" not in ct:
                r.close()
                return str(uid), ""
            chunks = []
            read_b = 0
            for ch in r.iter_content(chunk_size=16384):
                if not ch:
                    break
                chunks.append(ch)
                read_b += len(ch)
                if read_b >= 180_000:
                    break
            r.close()
            html = b"".join(chunks).decode(r.encoding or "utf-8", errors="replace")
            img_u = extract_portrait_from_html(html, r.url, nm)
            if img_u.startswith("http://"):
                # Keep http if server doesn't support https, or upgrade known https hosts
                pass
            return str(uid), img_u
        except Exception:
            return str(uid), ""

    done = 0
    found_new = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=24) as pool:
        for k_uid, img_u in pool.map(fetch_one, todo):
            cached[k_uid] = img_u
            if img_u:
                found_new += 1
            done += 1
            if done % 500 == 0:
                print(f"  Homepage portrait scan: {done:,}/{len(todo):,} (found {found_new:,} portraits)")

    with open(PHOTO_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cached, f, ensure_ascii=False)
    print(f"Stage 3 discovered {sum(1 for v in cached.values() if v):,} faculty homepage portraits.")
    return cached


def main():
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    ensure_photo_column(conn)

    rows = conn.execute(
        "SELECT id, name, is_core_stat, wikidata_qid, wikipedia_url, homepage, photo_url FROM people"
    ).fetchall()

    all_qids = sorted({(r["wikidata_qid"] or "").strip() for r in rows if (r["wikidata_qid"] or "").strip()})
    qid_photos = harvest_wikidata_p18(all_qids)

    # Map Stage 1 photos
    photo_by_id = {}
    wiki_todo_urls = []
    for r in rows:
        uid = r["id"]
        cur_p = (r["photo_url"] or "").strip()
        qid = (r["wikidata_qid"] or "").strip()
        w_url = (r["wikipedia_url"] or "").strip()
        if cur_p:
            photo_by_id[uid] = cur_p
        elif qid and qid in qid_photos:
            photo_by_id[uid] = qid_photos[qid]
        elif w_url and "en.wikipedia.org/wiki/" in w_url:
            wiki_todo_urls.append(w_url)

    # Stage 2: English Wikipedia pageimages
    wiki_thumbs = harvest_wikipedia_pageimages(wiki_todo_urls)
    for r in rows:
        uid = r["id"]
        if uid not in photo_by_id:
            w_url = (r["wikipedia_url"] or "").strip()
            if w_url and w_url in wiki_thumbs:
                photo_by_id[uid] = wiki_thumbs[w_url]

    # Stage 3: Faculty / personal homepages for Core Stat scholars still without a photo
    hp_candidates = []
    for r in rows:
        uid = r["id"]
        hp = (r["homepage"] or "").strip()
        if uid not in photo_by_id and r["is_core_stat"] == 1 and hp:
            hp_candidates.append((uid, r["name"], hp))

    hp_photos = harvest_homepage_portraits(hp_candidates)
    for uid, _, _ in hp_candidates:
        u_img = hp_photos.get(str(uid), "")
        if u_img:
            photo_by_id[uid] = u_img

    # Persist updates
    updates = [(url, uid) for uid, url in photo_by_id.items() if url]
    print(f"Writing {len(updates):,} scholar portrait URLs to SQLite...")
    conn.executemany("UPDATE people SET photo_url = ? WHERE id = ?", updates)
    conn.commit()

    tot_photos = conn.execute("SELECT COUNT(*) FROM people WHERE photo_url != '' AND photo_url IS NOT NULL").fetchone()[0]
    core_photos = conn.execute("SELECT COUNT(*) FROM people WHERE is_core_stat = 1 AND photo_url != '' AND photo_url IS NOT NULL").fetchone()[0]
    print(f"\n=== PORTRAIT ENRICHMENT COMPLETE ({time.time() - t0:.1f}s) ===")
    print(f"  Total scholars with verified portrait photo : {tot_photos:,}")
    print(f"  Core Stat/Biostat/Prob with portrait photo  : {core_photos:,}")
    print("  (All remaining scholars automatically render crisp 2-letter subfield-tinted initials, e.g. 'Lin Zheng' -> 'LZ')")
    conn.close()


if __name__ == "__main__":
    main()
