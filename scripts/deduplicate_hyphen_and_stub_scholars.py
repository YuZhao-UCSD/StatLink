#!/usr/bin/env python3
"""
Global Hyphen / Space / Middle-Initial & Undated-Stub Scholar Deduplicator
(`scripts/deduplicate_hyphen_and_stub_scholars.py`).

Resolves split duplicate records across MGP, AcademicTree, Wikidata, ORCID, eScholarship,
DataCite, DigitalCommons, PubMed, and HAL where the same individual appears under:
  1. Hyphenated / Spaced / Joined Compound Names:
     - e.g. `Wenxin Zhou` (`176298`) <-> `Wen-Xin Zhou` (`4004578`) at UCSD
     - `Run-Ze Li` <-> `Runze Li` (Penn State / UNC)
     - `Xu-Ming He` <-> `Xuming He` (UIUC / UMich)
     - `Xin-Hua Liu` <-> `Xinhua Liu` (JHU / Columbia)
     - `Ta-Chung Liu` <-> `Ta Chung Liu` (Cornell)
     - `Nicole Mayer Hamblett` <-> `Nicole Mayer-Hamblett` (UW)
  2. Undated Advisor / Alumni Stubs (`year IS NULL`):
     - Exact-name or middle-initial-compatible undated stubs sharing an institution,
       doctoral advisor, or doctoral student with a verified scholar record.

Precision Guards:
  - Never merges two distinct MGP IDs (`mgp_id_1 != mgp_id_2`) or distinct ORCIDs.
  - Never merges conflicting PhD graduation years (`abs(y1 - y2) > 2`) or conflicting PhD schools.
  - Never merges non-overlapping non-empty dissertation titles.
  - Enforces career timeline guard (`min_student_year - phd_year >= 3`).
  - Always preserves `FAMOUS_BY_ID` (`famous_ancestors.py`), `MGP` IDs, and smaller canonical IDs
    as the surviving `keep_id`, appending all spelling variants (hyphenated, spaced, joined) into
    `search_aliases`, sanitizing DAG cycles, and rebuilding topological `descendant_count`,
    `schools`, `advisor_schools`, `meta_stats`, and `people_fts`.
"""

import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
from collections import Counter, defaultdict, deque

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
from famous_ancestors import FAMOUS_BY_ID

DB_PATH = os.path.join(BASE_DIR, "data", "stat_genealogy.db")
PROTECTED_IDS = set(FAMOUS_BY_ID.keys()) | {18231, 12694, 4209, 14941, 47762}


def fold_norm(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"\([^)]*@[^)]*\)", " ", text)  # strip parenthetical emails
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
    s = re.sub(r"\b(jr|sr|ii|iii|iv|phd|dr|prof|professor)\b", " ", s)
    s = re.sub(r"[^a-z0-9\s\-]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def compound_squash_key(norm_nm: str):
    """ collapses 'wen-xin zhou', 'wen xin zhou', 'wenxin zhou' -> ('wenxin', 'zhou') """
    cleaned = re.sub(r"[^a-z0-9\s]", " ", (norm_nm or "").replace("-", " "))
    toks = [t for t in cleaned.split() if t]
    if len(toks) < 2:
        return None
    return ("".join(toks[:-1]), toks[-1])


def first_last_initial_sig(norm_nm: str):
    """ returns (first_squashed_or_tok0, surname, mid_initial_or_empty) for middle-initial stub check """
    parts_raw = [t for t in (norm_nm or "").split() if t]
    if len(parts_raw) < 2:
        return None
    first_clean = re.sub(r"[^a-z0-9]", "", parts_raw[0])
    last_clean = re.sub(r"[^a-z0-9]", "", parts_raw[-1])
    mid_tokens = [re.sub(r"[^a-z0-9]", "", x) for x in parts_raw[1:-1]]
    mid_tokens = [x for x in mid_tokens if x]
    # Only treat middle token as a middle initial/name if len == 1 (explicit initial like 'Samuel S. Wu')
    if len(mid_tokens) == 1 and len(mid_tokens[0]) == 1:
        return (first_clean, last_clean, mid_tokens[0])
    elif len(mid_tokens) == 0:
        return (first_clean, last_clean, "")
    return None


def generate_spelling_aliases(names_iter) -> list:
    aliases = set()
    for nm in names_iter:
        if not nm:
            continue
        clean_nm = re.sub(r"\([^)]*@[^)]*\)", "", nm).strip()
        if clean_nm:
            aliases.add(clean_nm)
        fn = fold_norm(clean_nm)
        if fn:
            aliases.add(fn)
            dehyp = fn.replace("-", " ")
            aliases.add(re.sub(r"\s+", " ", dehyp).strip())
            toks = [t for t in re.sub(r"[^a-z0-9\s]", " ", dehyp).split() if t]
            if len(toks) >= 3:
                # Also add joined given-name variant e.g. 'wen xin zhou' -> 'wenxin zhou'
                aliases.add(f"{''.join(toks[:-1])} {toks[-1]}")
            if "-" in fn:
                # 'wen-xin zhou' -> 'wenxin zhou'
                joined_hyp = " ".join(p.replace("-", "") for p in fn.split())
                aliases.add(joined_hyp)
    return sorted(a for a in aliases if len(a) >= 3)


def record_quality_rank(r: dict):
    """Higher tuple wins as surviving `keep_id`."""
    return (
        1 if r["id"] in PROTECTED_IDS else 0,
        1 if r["mgp_id"] is not None else 0,
        1 if r["year"] is not None else 0,
        1 if (r["school"] or "") != "" else 0,
        1 if (r["orcid"] or "") != "" else 0,
        1 if (r["homepage"] or "") != "" else 0,
        1 if (r["photo_url"] or "") != "" else 0,
        r["is_core_stat"] or 0,
        r["student_count"] or 0,
        -r["id"],  # Prefer smaller canonical ID
    )


def main():
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    print("Loading scholar records and mentorship graph for deduplication...")
    rows = {r["id"]: dict(r) for r in cur.execute("SELECT * FROM people").fetchall()}

    adv_schools = defaultdict(set)
    for pid, sch in cur.execute("SELECT person_id, school FROM advisor_schools"):
        if sch:
            adv_schools[pid].add(sch)

    stu_schools_of_adv = defaultdict(set)
    stu_min_yr_of_adv = {}
    adv_ids_of_stu = defaultdict(set)
    stu_ids_of_adv = defaultdict(set)

    for aid, sid in cur.execute("SELECT advisor_id, student_id FROM edges"):
        if aid not in rows or sid not in rows:
            continue
        adv_ids_of_stu[sid].add(aid)
        stu_ids_of_adv[aid].add(sid)
        ssch = rows[sid]["school"] or ""
        if ssch:
            stu_schools_of_adv[aid].add(ssch)
        syr = rows[sid]["year"]
        if syr:
            if aid not in stu_min_yr_of_adv or syr < stu_min_yr_of_adv[aid]:
                stu_min_yr_of_adv[aid] = syr

    def all_insts(uid: int) -> set:
        r = rows[uid]
        s = {x for x in (r["school"], r["current_affiliation"]) if x}
        return s | adv_schools[uid] | stu_schools_of_adv[uid]

    def can_merge(a: dict, b: dict, mode: str) -> bool:
        if a["id"] == b["id"]:
            return False
        if a["id"] in PROTECTED_IDS and b["id"] in PROTECTED_IDS:
            return False
        if a["mgp_id"] is not None and b["mgp_id"] is not None and a["mgp_id"] != b["mgp_id"]:
            return False
        if a["orcid"] and b["orcid"] and a["orcid"].upper() != b["orcid"].upper():
            return False
        if a["wikidata_qid"] and b["wikidata_qid"] and a["wikidata_qid"] != b["wikidata_qid"]:
            return False
        if a["year"] and b["year"] and abs(a["year"] - b["year"]) > 2:
            return False
        if a["school"] and b["school"] and a["school"] != b["school"]:
            return False

        # Never merge an advisor with their own student!
        if b["id"] in adv_ids_of_stu[a["id"]] or a["id"] in adv_ids_of_stu[b["id"]]:
            return False

        # Non-overlapping dissertation guard
        ta = set(re.findall(r"[a-z]{5,}", fold_norm(a["thesis"] or "")))
        tb = set(re.findall(r"[a-z]{5,}", fold_norm(b["thesis"] or "")))
        if len(ta) >= 3 and len(tb) >= 3 and not (ta & tb):
            return False

        # Career timeline guard
        yr_known = a["year"] or b["year"]
        min_s = min(
            [x for x in (stu_min_yr_of_adv.get(a["id"]), stu_min_yr_of_adv.get(b["id"])) if x is not None],
            default=None,
        )
        if yr_known and min_s and (min_s - yr_known) <= 2:
            return False

        shared_sch = all_insts(a["id"]) & all_insts(b["id"])
        shared_adv = adv_ids_of_stu[a["id"]] & adv_ids_of_stu[b["id"]]
        shared_stu = stu_ids_of_adv[a["id"]] & stu_ids_of_adv[b["id"]]

        if mode == "hyphen_or_space":
            if shared_sch or shared_adv or shared_stu:
                return True
            # Globally unique Core Stat name where one is an undated school-less stub
            if (a["is_core_stat"] or b["is_core_stat"]) and (
                (a["year"] is None and not a["school"]) or (b["year"] is None and not b["school"])
            ):
                return True
            return False

        elif mode == "exact_stub":
            both_aft = bool(a["aft_pid"] is not None and b["aft_pid"] is not None and a["aft_pid"] != b["aft_pid"])
            if both_aft and not (shared_adv or shared_stu):
                return False
            if (a["year"] is None or b["year"] is None or shared_adv or shared_stu or (ta and ta == tb)):
                return bool(shared_sch or shared_adv or shared_stu)
            return False

        elif mode == "mid_initial_stub":
            # Strictly require one record to be an undated stub (`year IS NULL`) AND share school/advisor/student!
            if not (a["year"] is None or b["year"] is None):
                return False
            both_aft = bool(a["aft_pid"] is not None and b["aft_pid"] is not None and a["aft_pid"] != b["aft_pid"])
            if both_aft and not (shared_adv or shared_stu):
                return False
            return bool(shared_sch or shared_adv or shared_stu)

        return False

    # Union-Find with strict pairwise validation against current cluster root
    parent = {uid: uid for uid in rows}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def try_union(u, v, mode, group_size=2):
        ru, rv = find(u), find(v)
        if ru == rv:
            return False
        ra, rb = rows[ru], rows[rv]
        if mode == "hyphen_or_space" and group_size > 2:
            # Disable `uniq_stub` fallback when >2 records share squashed key
            shared_any = bool(
                (all_insts(ru) & all_insts(rv))
                or (adv_ids_of_stu[ru] & adv_ids_of_stu[rv])
                or (stu_ids_of_adv[ru] & stu_ids_of_adv[rv])
            )
            if not shared_any:
                return False
        if not can_merge(ra, rb, mode):
            return False
        # Keep higher-quality record as root!
        if record_quality_rank(ra) >= record_quality_rank(rb):
            keep, drop = ru, rv
        else:
            keep, drop = rv, ru
        parent[drop] = keep
        return True

    # Pass 1: Compound Hyphen / Space Variants & Exact-Name Undated Stubs
    by_squash = defaultdict(list)
    for uid, r in rows.items():
        nn = fold_norm(r["name"])
        k = compound_squash_key(nn)
        if k and len(k[0]) >= 3 and len(k[1]) >= 2:
            by_squash[k].append(uid)

    cnt_hyphen = 0
    cnt_exact_stub = 0
    for k, uids in by_squash.items():
        if len(uids) < 2 or len(uids) > 25:
            continue
        gsize = len(uids)
        for i in range(len(uids)):
            for j in range(i + 1, len(uids)):
                u, v = uids[i], uids[j]
                nu = fold_norm(rows[u]["name"])
                nv = fold_norm(rows[v]["name"])
                if nu != nv:
                    if try_union(u, v, "hyphen_or_space", gsize):
                        cnt_hyphen += 1
                else:
                    if try_union(u, v, "exact_stub", gsize):
                        cnt_exact_stub += 1

    # Pass 2: Middle-Initial Undated Advisor/Alumni Stubs (e.g. `Samuel Wu` <-> `Samuel S. Wu` at UF)
    by_fl = defaultdict(list)
    for uid, r in rows.items():
        sig = first_last_initial_sig(fold_norm(r["name"]))
        if sig and len(sig[0]) >= 4 and len(sig[1]) >= 3:
            by_fl[(sig[0], sig[1])].append((uid, sig[2]))

    cnt_mid_stub = 0
    for fl_key, items in by_fl.items():
        if len(items) < 2 or len(items) > 15:
            continue
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                u, mid_u = items[i]
                v, mid_v = items[j]
                # Require one with middle initial and one without (compatible!)
                if bool(mid_u) == bool(mid_v):
                    continue
                if try_union(u, v, "mid_initial_stub", len(items)):
                    cnt_mid_stub += 1

    clusters = defaultdict(list)
    for uid in rows:
        r_id = find(uid)
        if r_id != uid:
            clusters[r_id].append(uid)

    total_dropped = sum(len(v) for v in clusters.values())
    print(f"Identified {len(clusters):,} merged scholar clusters ({total_dropped:,} duplicate rows):")
    print(f"  - Hyphen / Space / Diacritic variants : {cnt_hyphen:,}")
    print(f"  - Exact-name undated stubs            : {cnt_exact_stub:,}")
    print(f"  - Middle-initial undated stubs        : {cnt_mid_stub:,}")

    if not clusters:
        print("Database already fully deduplicated!")
        conn.close()
        return

    # Apply merges transactionally
    drop_to_keep = {}
    for keep_id, drop_ids in clusters.items():
        kp = rows[keep_id]
        member_ids = [keep_id] + drop_ids
        members = [rows[m] for m in member_ids]

        for did in drop_ids:
            drop_to_keep[did] = keep_id

        # Prefer clean non-email name with proper capitalization
        clean_kp_name = re.sub(r"\([^)]*@[^)]*\)", "", kp["name"]).strip()
        kp["name"] = clean_kp_name
        kp["norm_name"] = fold_norm(clean_kp_name)

        all_names_and_aliases = [m["name"] for m in members]
        for m in members:
            if m["search_aliases"]:
                all_names_and_aliases.extend(x.strip() for x in m["search_aliases"].split("|") if x.strip())
        kp["search_aliases"] = " | ".join(generate_spelling_aliases(all_names_and_aliases))

        for field in (
            "school", "raw_school", "country", "year", "thesis", "mgp_subject",
            "current_affiliation", "research_areas", "mgp_id", "aft_pid",
            "wikidata_qid", "orcid", "homepage", "wikipedia_url",
            "google_scholar_url", "linkedin_url", "photo_url", "proquest_id",
        ):
            if kp.get(field) in (None, ""):
                for m in members[1:]:
                    if m.get(field) not in (None, ""):
                        kp[field] = m[field]
                        break

        # Upgrade field classification if any merged record is Core / Broad Stat
        if not kp["is_core_stat"] and any(m["is_core_stat"] for m in members):
            core_m = next(m for m in members if m["is_core_stat"])
            kp["is_core_stat"] = 1
            kp["is_broad_stat"] = 1
            kp["primary_field"] = core_m["primary_field"]
            kp["classification_reason"] = core_m["classification_reason"]
        elif not kp["is_broad_stat"] and any(m["is_broad_stat"] for m in members):
            broad_m = next(m for m in members if m["is_broad_stat"])
            kp["is_broad_stat"] = 1
            if kp["primary_field"] == "Mathematics & Other":
                kp["primary_field"] = broad_m["primary_field"]

        # Combine provenance sources cleanly
        src_list = []
        for m in members:
            for s in (m["sources"] or "").split(","):
                s = s.strip()
                if s and s not in src_list:
                    src_list.append(s)
        kp["sources"] = ",".join(src_list)

        # Avoid UNIQUE constraint collision on `mgp_id` / `aft_pid` before UPDATE by clearing dropped rows first!
        cur.executemany("DELETE FROM people WHERE id = ?", [(did,) for did in drop_ids])

        cur.execute(
            """
            UPDATE people
            SET name = ?, norm_name = ?, search_aliases = ?, school = ?, raw_school = ?,
                country = ?, year = ?, thesis = ?, mgp_subject = ?, primary_field = ?,
                is_core_stat = ?, is_broad_stat = ?, classification_reason = ?,
                current_affiliation = ?, research_areas = ?, mgp_id = ?, aft_pid = ?,
                wikidata_qid = ?, orcid = ?, homepage = ?, wikipedia_url = ?,
                google_scholar_url = ?, linkedin_url = ?, photo_url = ?, proquest_id = ?,
                sources = ?
            WHERE id = ?
            """,
            (
                kp["name"], kp["norm_name"], kp["search_aliases"], kp["school"], kp["raw_school"],
                kp["country"], kp["year"], kp["thesis"], kp["mgp_subject"], kp["primary_field"],
                kp["is_core_stat"], kp["is_broad_stat"], kp["classification_reason"],
                kp["current_affiliation"], kp["research_areas"], kp["mgp_id"], kp["aft_pid"],
                kp["wikidata_qid"], kp["orcid"], kp["homepage"], kp["wikipedia_url"],
                kp["google_scholar_url"], kp["linkedin_url"], kp["photo_url"], kp["proquest_id"],
                kp["sources"], keep_id,
            ),
        )

    # Also enrich search_aliases with hyphen-free/joined variants for ALL remaining hyphenated names!
    hyp_rows = cur.execute("SELECT id, name, norm_name, COALESCE(search_aliases, '') FROM people WHERE name LIKE '%-%'").fetchall()
    hyp_updates = []
    for uid, nm, nn, sa in hyp_rows:
        existing = [x.strip() for x in sa.split("|") if x.strip()]
        merged_sa = " | ".join(generate_spelling_aliases([nm] + existing))
        if merged_sa != sa:
            hyp_updates.append((merged_sa, uid))
    if hyp_updates:
        cur.executemany("UPDATE people SET search_aliases = ? WHERE id = ?", hyp_updates)
        print(f"Added de-hyphenated & compound search aliases to {len(hyp_updates):,} hyphenated scholars.")

    # Remap `edges` cleanly and break any 2-cycles / multi-hop cycles if introduced
    raw_edges = cur.execute("SELECT advisor_id, student_id, source FROM edges").fetchall()
    edge_merged = {}
    surviving_ids = set(r[0] for r in cur.execute("SELECT id FROM people").fetchall())

    for aid, sid, src in raw_edges:
        na = drop_to_keep.get(aid, aid)
        ns = drop_to_keep.get(sid, sid)
        if na == ns or na not in surviving_ids or ns not in surviving_ids:
            continue
        key = (na, ns)
        if key not in edge_merged:
            edge_merged[key] = src
        else:
            parts = list(dict.fromkeys((edge_merged[key] + "+" + src).split("+")))
            edge_merged[key] = "+".join(parts[:4])

    # Verify strict DAG acyclicity via Kahn's topological sort
    in_deg = Counter()
    adj = defaultdict(list)
    nodes_in_edges = set()
    for (aid, sid) in edge_merged:
        adj[aid].append(sid)
        in_deg[sid] += 1
        nodes_in_edges.add(aid)
        nodes_in_edges.add(sid)

    q_kahn = deque(u for u in nodes_in_edges if in_deg[u] == 0)
    visited = set()
    while q_kahn:
        u = q_kahn.popleft()
        visited.add(u)
        for v in adj[u]:
            in_deg[v] -= 1
            if in_deg[v] == 0:
                q_kahn.append(v)

    if len(visited) < len(nodes_in_edges):
        cyc_nodes = nodes_in_edges - visited
        to_drop_edges = []
        for (aid, sid), src in edge_merged.items():
            if aid in cyc_nodes and sid in cyc_nodes:
                ay = rows.get(aid, {}).get("year") or 0
                sy = rows.get(sid, {}).get("year") or 9999
                if ay > sy or (aid, sid) > (sid, aid):
                    to_drop_edges.append((aid, sid))
        for k in to_drop_edges:
            edge_merged.pop(k, None)
        print(f"  Sanitized {len(to_drop_edges)} cycle edge(s) after merge.")

    old_edge_map = {(aid, sid): src for aid, sid, src in raw_edges}
    edges_to_delete = [k for k in old_edge_map if k not in edge_merged]
    edges_to_upsert = [(aid, sid, src) for (aid, sid), src in edge_merged.items() if old_edge_map.get((aid, sid)) != src]
    if edges_to_delete:
        cur.executemany("DELETE FROM edges WHERE advisor_id = ? AND student_id = ?", edges_to_delete)
    if edges_to_upsert:
        cur.executemany("INSERT OR REPLACE INTO edges (advisor_id, student_id, source) VALUES (?, ?, ?)", edges_to_upsert)
    print(f"  Updated edges delta: -{len(edges_to_delete):,} removed, +{len(edges_to_upsert):,} upserted.")

    # Delta-update `advisor_schools` for affected scholars
    affected_pids = set(drop_to_keep.keys()) | set(clusters.keys())
    if affected_pids:
        cur.executemany("DELETE FROM advisor_schools WHERE person_id = ?", [(pid,) for pid in affected_pids])
    school_of_surviving = {}
    old_counts_map = {}
    core_ids_set = set()
    for uid, sch, is_c, ac, sc, dc, sdc in cur.execute(
        "SELECT id, school, is_core_stat, advisor_count, student_count, descendant_count, stat_descendant_count FROM people"
    ):
        school_of_surviving[uid] = sch or ""
        old_counts_map[uid] = (ac or 0, sc or 0, dc or 0, sdc or 0)
        if is_c == 1:
            core_ids_set.add(uid)

    adv_sch_counts = Counter()
    for (aid, sid) in edge_merged:
        if aid in clusters:
            ssch = school_of_surviving.get(sid, "")
            if ssch:
                adv_sch_counts[(aid, ssch)] += 1
    for keep_id in clusters:
        aff = rows[keep_id].get("current_affiliation") or ""
        if aff:
            adv_sch_counts[(keep_id, aff)] += 0
    if adv_sch_counts:
        cur.executemany(
            "INSERT OR REPLACE INTO advisor_schools (person_id, school, advised_at_school) VALUES (?, ?, ?)",
            [(pid, sch, cnt) for (pid, sch), cnt in adv_sch_counts.items()],
        )

    # Recompute `advisor_count`, `student_count`, and C-accelerated reverse-topological DAG `descendant_count`
    print("Recomputing direct student counts and exact reverse-topological DAG descendant counts...")
    adv_cnt_map = Counter()
    stu_cnt_map = Counter()
    parents_of = defaultdict(list)
    rev_children = defaultdict(list)
    out_deg = {uid: 0 for uid in old_counts_map}

    for (aid, sid) in edge_merged:
        stu_cnt_map[aid] += 1
        adv_cnt_map[sid] += 1
        parents_of[sid].append(aid)
        rev_children[aid].append(sid)
        out_deg[aid] += 1

    rem_parents = {uid: len(parents_of[uid]) for uid in old_counts_map}
    dq = deque(uid for uid, deg in out_deg.items() if deg == 0)
    desc_sets = {}
    final_counts = {}

    while dq:
        u = dq.popleft()
        ch = rev_children.get(u)
        if ch:
            s = set(ch)
            for c in ch:
                cs = desc_sets.get(c)
                if cs:
                    s |= cs
                rem_parents[c] -= 1
                if rem_parents[c] <= 0:
                    desc_sets.pop(c, None)
            sc = len(s & core_ids_set)  # Fast C-level set intersection!
            final_counts[u] = (len(s), sc)
            if rem_parents[u] > 0:
                desc_sets[u] = s
        else:
            final_counts[u] = (0, 0)

        for p in parents_of.get(u, ()):
            out_deg[p] -= 1
            if out_deg[p] == 0:
                dq.append(p)

    changed_people_counts = []
    for uid, old_tup in old_counts_map.items():
        dc_val, sdc_val = final_counts.get(uid, (0, 0))
        new_tup = (adv_cnt_map.get(uid, 0), stu_cnt_map.get(uid, 0), dc_val, sdc_val)
        if new_tup != old_tup:
            changed_people_counts.append((*new_tup, uid))

    if changed_people_counts:
        cur.executemany(
            "UPDATE people SET advisor_count = ?, student_count = ?, descendant_count = ?, stat_descendant_count = ? WHERE id = ?",
            changed_people_counts,
        )
    print(f"  Delta-updated {len(changed_people_counts):,} scholars whose lineage counts changed.")

    # Fast single-pass `GROUP BY school` update
    sch_agg = {
        sch: (c_cnt, b_cnt, t_cnt)
        for sch, c_cnt, b_cnt, t_cnt in cur.execute(
            "SELECT school, SUM(is_core_stat), SUM(is_broad_stat), COUNT(*) FROM people WHERE school != '' AND school IS NOT NULL GROUP BY school"
        )
    }
    sch_updates = []
    for sch, old_c, old_b, old_t in cur.execute("SELECT school, core_stat_count, broad_stat_count, total_phd_count FROM schools"):
        nc, nb, nt = sch_agg.get(sch, (0, 0, 0))
        if (nc, nb, nt) != (old_c, old_b, old_t):
            sch_updates.append((nc, nb, nt, sch))
    if sch_updates:
        cur.executemany(
            "UPDATE schools SET core_stat_count = ?, broad_stat_count = ?, total_phd_count = ? WHERE school = ?",
            sch_updates,
        )

    row_sum = cur.execute("SELECT value_json FROM meta_stats WHERE key = 'summary'").fetchone()
    summary = json.loads(row_sum[0]) if row_sum else {}
    summary["total_people"] = cur.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    summary["total_edges"] = cur.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    summary["core_stat_total"] = cur.execute("SELECT COUNT(*) FROM people WHERE is_core_stat = 1").fetchone()[0]
    summary["broad_stat_total"] = cur.execute("SELECT COUNT(*) FROM people WHERE is_broad_stat = 1").fetchone()[0]
    summary["by_field"] = {
        pf: cnt for pf, cnt in cur.execute("SELECT primary_field, COUNT(*) FROM people GROUP BY primary_field")
    }
    summary["core_stat_schools"] = cur.execute("SELECT COUNT(*) FROM schools WHERE core_stat_count > 0").fetchone()[0]
    summary["total_schools"] = cur.execute("SELECT COUNT(*) FROM schools").fetchone()[0]
    cur.execute("INSERT OR REPLACE INTO meta_stats (key, value_json) VALUES ('summary', ?)", (json.dumps(summary),))

    print("Rebuilding FTS5 index (`people_fts`)...")
    cur.execute("INSERT INTO people_fts(people_fts) VALUES('rebuild')")
    conn.commit()

    wz = cur.execute(
        "SELECT id, name, search_aliases, school, current_affiliation, year, advisor_count, student_count, descendant_count, homepage FROM people WHERE id = 176298"
    ).fetchone()
    print("\nVerified Wenxin Zhou after merge:", dict(wz) if wz else None)
    wz_stus = [dict(x) for x in cur.execute("SELECT p.id, p.name, p.year, p.school FROM edges e JOIN people p ON p.id = e.student_id WHERE e.advisor_id = 176298 ORDER BY p.year")]
    print("  Wenxin Zhou advisees:", wz_stus)
    print(f"Done in {time.time() - t0:.1f}s! Final scholars={summary['total_people']:,}, edges={summary['total_edges']:,}")
    conn.close()


if __name__ == "__main__":
    main()
