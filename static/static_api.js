/**
 * StatLink Serverless Static API Shim for GitHub Pages (`docs/`).
 * Intercepts `/api/*` fetch() calls and serves 100% identical JSON directly from
 * compact static data bundles in `data/` with zero Python backend required.
 */
(() => {
  "use strict";

  const origFetch = window.fetch.bind(window);
  const FIELDS = [
    "Statistics",
    "Biostatistics",
    "Probability",
    "Related Quantitative",
    "Mathematics & Other",
  ];
  const NUM_SHARDS = 256;

  // State & Lazy Caches
  let statsPromise = null;
  let topicMapPromise = null;
  let corePromise = null;
  let schoolsList = [];
  let schoolNameByIdx = [];
  let schoolIdxByName = new Map();
  let peopleArr = []; // Compact rows
  let personById = new Map();
  let parentsMap = new Map(); // student_id -> int[]
  let childrenMap = new Map(); // advisor_id -> int[]
  let advSchoolsMap = new Map(); // school_idx -> Set(person_id)
  let famousById = new Map();
  let redundantPairs = [];
  let luckyIds = [];
  const shardCache = new Map(); // shardIdx -> Promise<Object>
  const previewMemo = new Map();

  function foldAscii(str) {
    if (!str) return "";
    return String(str)
      .replace(/[øØ]/g, "o")
      .replace(/[łŁ]/g, "l")
      .replace(/ß/g, "ss")
      .replace(/[đĐ]/g, "d")
      .replace(/[æÆ]/g, "ae")
      .replace(/[œŒ]/g, "oe")
      .normalize("NFKD")
      .replace(/[\u0300-\u036f]/g, "");
  }

  function normStr(s) {
    return foldAscii(s).toLowerCase().replace(/[^a-z0-9\s\-]/g, " ").replace(/\s+/g, " ").trim();
  }

  function damerauLe(a, b, maxD = 2) {
    if (a === b) return 0;
    let la = a.length, lb = b.length;
    if (Math.abs(la - lb) > maxD) return maxD + 1;
    if (la > lb) { const t = a; a = b; b = t; const tl = la; la = lb; lb = tl; }
    let prev2 = null;
    let prev1 = Array.from({ length: lb + 1 }, (_, i) => i);
    for (let i = 1; i <= la; i++) {
      const ca = a[i - 1];
      const curr = new Array(lb + 1);
      curr[0] = i;
      let rowMin = i;
      for (let j = 1; j <= lb; j++) {
        const cost = ca === b[j - 1] ? 0 : 1;
        let v = Math.min(prev1[j] + 1, curr[j - 1] + 1, prev1[j - 1] + cost);
        if (i > 1 && j > 1 && ca === b[j - 2] && a[i - 2] === b[j - 1]) {
          v = Math.min(v, prev2[j - 2] + 1);
        }
        curr[j] = v;
        if (v < rowMin) rowMin = v;
      }
      if (rowMin > maxD) return maxD + 1;
      prev2 = prev1;
      prev1 = curr;
    }
    return prev1[lb];
  }

  async function fetchGzBytes(relUrl) {
    const resp = await origFetch(relUrl);
    if (!resp.ok) throw new Error(`Failed loading ${relUrl}: ${resp.status}`);
    if (typeof DecompressionStream !== "undefined") {
      const ds = new DecompressionStream("gzip");
      const decompResp = new Response(resp.body.pipeThrough(ds));
      return await decompResp.arrayBuffer();
    }
    return await resp.arrayBuffer();
  }

  async function fetchGzJson(relUrl) {
    const buf = await fetchGzBytes(relUrl);
    return JSON.parse(new TextDecoder("utf-8").decode(buf));
  }

  async function ensureCoreLoaded() {
    if (corePromise) return corePromise;
    corePromise = (async () => {
      const [schools, famousData, pRows, graphBuf, advSchObj] = await Promise.all([
        origFetch("data/schools.json").then((r) => r.json()),
        origFetch("data/famous.json").then((r) => r.json()),
        fetchGzJson("data/people_index.json.gz"),
        fetchGzBytes("data/graph.bin.gz"),
        fetchGzJson("data/advisor_schools.json.gz"),
      ]);

      schoolsList = schools;
      schoolNameByIdx = schools.map((s) => s.school);
      schools.forEach((s, idx) => schoolIdxByName.set(s.school, idx));

      (famousData.scientists || []).forEach((f) => famousById.set(f.id, f));
      redundantPairs = famousData.redundant_pairs || [];

      // Unpack people rows: [id, name, norm_name, aliases, sidx, year, fidx, flags, adv, stu, desc, stat_desc]
      peopleArr = pRows;
      luckyIds = [];
      for (let i = 0; i < pRows.length; i++) {
        const r = pRows[i];
        personById.set(r[0], r);
        if ((r[7] & 1) && (r[8] > 0 || r[9] > 0)) {
          luckyIds.push(r[0]);
        }
      }

      // Unpack binary uint32 graph [advisor_id, student_id]
      const u32 = new Uint32Array(graphBuf);
      for (let i = 0; i < u32.length; i += 2) {
        const aid = u32[i];
        const sid = u32[i + 1];
        let pArr = parentsMap.get(sid);
        if (!pArr) { pArr = []; parentsMap.set(sid, pArr); }
        pArr.push(aid);
        let cArr = childrenMap.get(aid);
        if (!cArr) { cArr = []; childrenMap.set(aid, cArr); }
        cArr.push(sid);
      }

      for (const [k, arr] of Object.entries(advSchObj)) {
        advSchoolsMap.set(Number(k), new Set(arr));
      }
    })();
    return corePromise;
  }

  function loadShard(shardIdx) {
    if (!shardCache.has(shardIdx)) {
      const pad = String(shardIdx).padStart(3, "0");
      shardCache.set(
        shardIdx,
        origFetch(`data/details/s${pad}.json`).then((r) => (r.ok ? r.json() : {})).catch(() => ({}))
      );
    }
    return shardCache.get(shardIdx);
  }

  async function fetchDetailsForIds(ids) {
    const neededShards = new Set();
    for (const id of ids) neededShards.add(id % NUM_SHARDS);
    const entries = await Promise.all(Array.from(neededShards).map(async (s) => [s, await loadShard(s)]));
    const byShard = new Map(entries);
    const out = new Map();
    for (const id of ids) {
      const d = (byShard.get(id % NUM_SHARDS) || {})[String(id)];
      if (d) out.set(id, d);
    }
    return out;
  }

  function rowToBrief(r, det = null) {
    const id = r[0];
    const fi = famousById.get(id);
    const sch = r[4] >= 0 ? (schoolNameByIdx[r[4]] || "") : "";
    return {
      id,
      name: r[1],
      school: sch,
      country: (det && det.c) || "",
      year: r[5] || null,
      thesis: (det && det.t) || "",
      primary_field: FIELDS[r[6]] || "Mathematics & Other",
      is_core_stat: Boolean(r[7] & 1),
      is_broad_stat: Boolean(r[7] & 2),
      classification_reason: (det && det.cr) || "",
      current_affiliation: (det && det.a) || "",
      mgp_id: det && det.mgp !== undefined ? det.mgp : null,
      aft_pid: det && det.aft !== undefined ? det.aft : null,
      wikidata_qid: (det && det.wd) || "",
      photo_url: (det && det.ph) || "",
      sources: det && det.src ? det.src.split(",") : [],
      advisor_count: r[8],
      student_count: r[9],
      descendant_count: r[10],
      stat_descendant_count: r[11],
      famous_self: fi
        ? { display_name: fi.display_name, short_name: fi.short_name, category: fi.category, epithet: fi.epithet }
        : null,
    };
  }

  // Ancestry & Descendant BFS helpers
  function walkUpward(personId, maxHops = 60) {
    const dist = new Map([[personId, 0]]);
    const nextDown = new Map();
    let frontier = [personId];
    for (let step = 1; step <= maxHops && frontier.length; step++) {
      const nxt = [];
      for (const s of frontier) {
        const ps = parentsMap.get(s);
        if (!ps) continue;
        for (const a of ps) {
          if (!dist.has(a)) {
            dist.set(a, step);
            nextDown.set(a, s);
            nxt.push(a);
          }
        }
      }
      frontier = nxt;
    }
    return { dist, nextDown };
  }

  function getDescendantsMap(ancestorId, directOnly = false) {
    const dist = new Map();
    if (directOnly) {
      for (const c of childrenMap.get(ancestorId) || []) {
        if (c !== ancestorId) dist.set(c, 1);
      }
      return dist;
    }
    const visited = new Set([ancestorId]);
    let frontier = [ancestorId];
    let step = 0;
    while (frontier.length) {
      step++;
      const nxt = [];
      for (const u of frontier) {
        for (const c of childrenMap.get(u) || []) {
          if (!visited.has(c)) {
            visited.add(c);
            dist.set(c, step);
            nxt.push(c);
          }
        }
      }
      frontier = nxt;
    }
    return dist;
  }

  function selectFamousHighlights(found, maxHl = 6) {
    if (!found.length) return { highlighted: [], more: [] };
    const byId = new Map(found.map((x) => [x.id, x]));
    const statC = found.filter((x) => x.category === "Statistics & Probability").sort((a, b) => a.generations - b.generations || b.fame_score - a.fame_score);
    const sciC = found.filter((x) => x.category === "Physics & Science").sort((a, b) => b.fame_score - a.fame_score || a.generations - b.generations);
    const mathC = found.filter((x) => x.category === "Mathematics").sort((a, b) => b.fame_score - a.fame_score || a.generations - b.generations);
    const chosen = [];
    const tryPick = (pool, lim) => {
      let added = 0;
      for (const it of pool) {
        if (added >= lim || chosen.length >= maxHl) break;
        if (chosen.includes(it.id)) continue;
        if (found.length > maxHl) {
          const conflict = redundantPairs.some(([a, b]) => (it.id === a && chosen.includes(b)) || (it.id === b && chosen.includes(a)));
          if (conflict) continue;
        }
        chosen.push(it.id);
        added++;
      }
    };
    tryPick(statC, 2);
    tryPick(sciC, 2);
    tryPick(mathC, 2);
    if (chosen.length < maxHl) {
      for (const it of found.slice().sort((a, b) => b.fame_score - a.fame_score || a.generations - b.generations)) {
        if (chosen.length >= maxHl) break;
        if (!chosen.includes(it.id)) chosen.push(it.id);
      }
    }
    const chosenSet = new Set(chosen);
    return {
      highlighted: chosen.map((i) => byId.get(i)).sort((a, b) => a.generations - b.generations || b.fame_score - a.fame_score),
      more: found.filter((x) => !chosenSet.has(x.id)).sort((a, b) => a.generations - b.generations || b.fame_score - a.fame_score),
    };
  }

  function getFamousPreview(personId, limit = 3) {
    if (previewMemo.has(personId)) return previewMemo.get(personId).slice(0, limit);
    const { dist } = walkUpward(personId);
    const found = [];
    for (const [aid, gens] of dist.entries()) {
      if (aid === personId) continue;
      const fi = famousById.get(aid);
      if (fi) found.push({ ...fi, generations: gens });
    }
    const { highlighted } = selectFamousHighlights(found, Math.max(3, limit));
    const top = highlighted.slice().sort((a, b) => b.fame_score - a.fame_score || a.generations - b.generations).slice(0, limit);
    previewMemo.set(personId, top);
    return top;
  }

  function getFamousForPerson(personId) {
    const { dist, nextDown } = walkUpward(personId);
    const found = [];
    for (const [aid, gens] of dist.entries()) {
      if (aid === personId) continue;
      const fi = famousById.get(aid);
      if (!fi) continue;
      const chainIds = [aid];
      let cur = aid;
      let guard = 0;
      while (cur !== personId && nextDown.has(cur) && guard < 70) {
        cur = nextDown.get(cur);
        chainIds.push(cur);
        guard++;
      }
      const chain = chainIds.map((cid) => {
        const r = personById.get(cid);
        return r
          ? { id: cid, name: r[1], year: r[5] || null, school: r[4] >= 0 ? (schoolNameByIdx[r[4]] || "") : "" }
          : { id: cid, name: `#${cid}`, year: null, school: "" };
      });
      found.push({ ...fi, generations: gens, chain });
    }
    const { highlighted, more } = selectFamousHighlights(found, 6);
    return { total_count: found.length, highlighted, more };
  }

  // Endpoint Handlers
  async function handleSchools(sp) {
    await ensureCoreLoaded();
    const q = normStr(sp.get("q") || "");
    const scope = sp.get("scope") || "core";
    const limit = Math.min(50, Number(sp.get("limit") || "25"));
    const countCol = scope === "broad" ? "broad_stat_count" : scope === "all" ? "total_phd_count" : "core_stat_count";

    if (!q) {
      return { schools: schoolsList.filter((s) => s[countCol] > 0).sort((a, b) => b[countCol] - a[countCol]).slice(0, limit) };
    }
    const exact = [];
    for (const s of schoolsList) {
      if (s.norm_school.includes(q) || s.aliases.toLowerCase().includes(q)) {
        const bonus = s.aliases.toLowerCase().split(",").some((a) => a.trim() === q) ? 100000 : s.norm_school.startsWith(q) ? 50000 : 0;
        exact.push({ sc: bonus + s[countCol] * 10 + s.total_phd_count, s });
      }
    }
    if (exact.length) {
      exact.sort((a, b) => b.sc - a.sc);
      return { schools: exact.slice(0, limit).map((x) => x.s) };
    }
    // Fuzzy fallback
    const qToks = q.split(/\s+/).filter((t) => t.length >= 2 && !["of", "the", "at", "in", "and"].includes(t));
    const scored = [];
    for (const s of schoolsList) {
      if (s[countCol] <= 0 && s.total_phd_count < 3) continue;
      const sToks = (s.norm_school + " " + s.aliases.toLowerCase()).match(/[a-z0-9]{2,}/g) || [];
      let totD = 0, ok = true;
      for (const qt of qToks) {
        const maxD = qt.length <= 4 ? 1 : 2;
        let best = maxD + 1;
        for (const st of sToks) {
          const d = st.startsWith(qt) ? 0 : damerauLe(qt, st, maxD);
          if (d < best) best = d;
          if (best === 0) break;
        }
        if (best > maxD) { ok = false; break; }
        totD += best;
      }
      if (ok) scored.push({ totD, c: s[countCol], t: s.total_phd_count, s });
    }
    scored.sort((a, b) => a.totD - b.totD || b.c - a.c || b.t - a.t);
    return { schools: scored.slice(0, limit).map((x) => x.s) };
  }

  function matchScope(r, scope) {
    if (scope === "all") return true;
    if (scope === "core") return Boolean(r[7] & 1);
    if (scope === "broad") return Boolean(r[7] & 2);
    return FIELDS[r[6]] === scope;
  }

  async function handleSearch(sp) {
    await ensureCoreLoaded();
    const rawQ = (sp.get("q") || "").trim();
    const school = (sp.get("school") || "").trim();
    const schoolRole = (sp.get("school_role") || "both").trim();
    const scope = (sp.get("scope") || "core").trim();
    const sort = (sp.get("sort") || "relevance").trim();
    const page = Math.max(1, Number(sp.get("page") || "1"));
    const perPage = Math.min(60, Math.max(1, Number(sp.get("per_page") || "36")));
    const ymin = sp.get("year_min") ? Number(sp.get("year_min")) : null;
    const ymax = sp.get("year_max") ? Number(sp.get("year_max")) : null;
    const ancRaw = (sp.get("ancestor_id") || "").trim();
    const ancestorId = /^\d+$/.test(ancRaw) ? Number(ancRaw) : null;
    const ancestorMode = (sp.get("ancestor_mode") || "all").trim();

    let descGenMap = null;
    let ancestorInfo = null;
    if (ancestorId !== null) {
      const ar = personById.get(ancestorId);
      if (ar) {
        ancestorInfo = { id: ar[0], name: ar[1], school: ar[4] >= 0 ? (schoolNameByIdx[ar[4]] || "") : "", year: ar[5] || null };
      }
      descGenMap = getDescendantsMap(ancestorId, ancestorMode === "direct");
      if (!descGenMap.size) {
        return { results: [], total: 0, page, per_page: perPage, expanded_from_all: false, ancestor: ancestorInfo };
      }
    }

    // Resolve school filter
    let targetSchoolIdxs = null;
    let facultySet = null;
    if (school) {
      targetSchoolIdxs = new Set();
      if (schoolIdxByName.has(school)) {
        targetSchoolIdxs.add(schoolIdxByName.get(school));
      } else {
        const sq = normStr(school);
        schoolsList.forEach((s, idx) => {
          if (s.norm_school.includes(sq) || s.aliases.toLowerCase().includes(sq)) targetSchoolIdxs.add(idx);
        });
      }
      if (schoolRole === "faculty" || schoolRole === "both") {
        facultySet = new Set();
        for (const sidx of targetSchoolIdxs) {
          const fs = advSchoolsMap.get(sidx);
          if (fs) for (const pid of fs) facultySet.add(pid);
        }
      }
    }

    const passesBaseFilters = (r) => {
      if (descGenMap && !descGenMap.has(r[0])) return false;
      if (ymin !== null && (!r[5] || r[5] < ymin)) return false;
      if (ymax !== null && (!r[5] || r[5] > ymax)) return false;
      if (targetSchoolIdxs) {
        const isGrad = targetSchoolIdxs.has(r[4]);
        const isFac = facultySet && facultySet.has(r[0]);
        if (schoolRole === "faculty") { if (!isFac) return false; }
        else if (schoolRole === "graduates") { if (!isGrad) return false; }
        else { if (!isGrad && !isFac) return false; }
      }
      return true;
    };

    const qNorm = normStr(rawQ);
    const qToks = qNorm ? (qNorm.match(/[a-z0-9]+/g) || []) : [];
    const qSquashed = qToks.join("");

    let matches = [];
    let expandedFromAll = false;
    let fuzzyQuery = "";

    const scoreMatch = (r, isFuzzy = false) => {
      const nn = r[2];
      const al = r[3] ? r[3].toLowerCase() : "";
      const hay = al ? `${nn} ${al}` : nn;
      const sch = r[4] >= 0 ? (schoolsList[r[4]] ? schoolsList[r[4]].norm_school : "") : "";
      let sc = 0;
      if (nn === qNorm) sc += 200;
      else if (nn.replace(/[\s\-]/g, "") === qSquashed) sc += 170;
      else if (nn.startsWith(qNorm)) sc += 120;
      else if (hay.includes(qNorm)) sc += 90;
      else if (qToks.every((t) => hay.includes(t))) sc += 75;
      else if (sch && qToks.every((t) => hay.includes(t) || sch.includes(t))) sc += 30;
      else if (isFuzzy) sc += 25;
      else return -1;
      if (r[7] & 1) sc += 25;
      else if (r[7] & 2) sc += 8;
      sc += Math.log1p(r[10] + r[9] * 3) * 4.5;
      return sc;
    };

    if (qToks.length) {
      const pool = descGenMap ? Array.from(descGenMap.keys()).map((id) => personById.get(id)).filter(Boolean) : peopleArr;
      const scopedHits = [];
      const allHits = [];
      for (let i = 0; i < pool.length; i++) {
        const r = pool[i];
        if (!passesBaseFilters(r)) continue;
        const sc = scoreMatch(r, false);
        if (sc >= 0) {
          if (matchScope(r, scope)) scopedHits.push([sc, r]);
          else if (scope !== "all") allHits.push([sc, r]);
        }
      }
      matches = scopedHits;
      if (scope !== "all" && matches.length < 5 && allHits.length) {
        matches = matches.concat(allHits);
        expandedFromAll = true;
      }
      // Fuzzy Damerau-Levenshtein fallback when <5 exact hits
      if (matches.length < 5 && qToks.length <= 4) {
        const seen = new Set(matches.map((m) => m[1][0]));
        const fuzzyHits = [];
        for (let i = 0; i < pool.length; i++) {
          const r = pool[i];
          if (seen.has(r[0]) || !passesBaseFilters(r)) continue;
          const rToks = (r[2] + " " + (r[3] || "").toLowerCase()).match(/[a-z0-9]+/g) || [];
          if (!rToks.length) continue;
          let totD = 0, allOk = true;
          for (const qt of qToks) {
            const maxD = qt.length <= 4 ? 1 : 2;
            let best = maxD + 1;
            for (const rt of rToks) {
              const d = rt.startsWith(qt) ? 0 : damerauLe(qt, rt, maxD);
              if (d < best) best = d;
              if (best === 0) break;
            }
            if (best > maxD) { allOk = false; break; }
            totD += best;
          }
          if (allOk) {
            const imp = (r[7] & 1 ? 20 : 0) + Math.log1p(r[10] + r[9] * 3) * 4 - totD * 14;
            fuzzyHits.push([imp, r]);
            if (fuzzyHits.length >= 250) break;
          }
        }
        if (fuzzyHits.length) {
          fuzzyHits.sort((a, b) => b[0] - a[0]);
          fuzzyQuery = fuzzyHits[0][1][1];
          matches = matches.concat(fuzzyHits.slice(0, 80));
        }
      }
    } else {
      const pool = descGenMap ? Array.from(descGenMap.keys()).map((id) => personById.get(id)).filter(Boolean) : peopleArr;
      for (let i = 0; i < pool.length; i++) {
        const r = pool[i];
        if (!passesBaseFilters(r)) continue;
        if (matchScope(r, scope)) matches.push([0, r]);
      }
      if (!matches.length && ancestorId !== null && scope !== "all") {
        for (let i = 0; i < pool.length; i++) {
          const r = pool[i];
          if (passesBaseFilters(r)) matches.push([0, r]);
        }
        expandedFromAll = matches.length > 0;
      }
    }

    // Sort
    if (sort === "descendants") matches.sort((a, b) => b[1][10] - a[1][10] || b[1][9] - a[1][9]);
    else if (sort === "students") matches.sort((a, b) => b[1][9] - a[1][9] || b[1][10] - a[1][10]);
    else if (sort === "year_desc") matches.sort((a, b) => (b[1][5] || 0) - (a[1][5] || 0) || b[1][9] - a[1][9]);
    else if (sort === "year_asc") matches.sort((a, b) => (a[1][5] || 9999) - (b[1][5] || 9999) || b[1][9] - a[1][9]);
    else if (sort === "name_asc") matches.sort((a, b) => a[1][1].localeCompare(b[1][1]));
    else if (qToks.length) matches.sort((a, b) => b[0] - a[0] || b[1][10] - a[1][10]);
    else matches.sort((a, b) => b[1][11] - a[1][11] || b[1][10] - a[1][10] || b[1][9] - a[1][9]);

    const total = matches.length;
    const start = (page - 1) * perPage;
    const pageSlice = matches.slice(start, start + perPage).map((m) => m[1]);
    const detMap = await fetchDetailsForIds(pageSlice.map((r) => r[0]));

    const results = pageSlice.map((r) => {
      const card = rowToBrief(r, detMap.get(r[0]));
      card.famous_ancestors_preview = getFamousPreview(r[0], 3);
      if (descGenMap) card.ancestor_generations = descGenMap.get(r[0]);
      return card;
    });

    return { results, total, page, per_page: perPage, expanded_from_all: expandedFromAll, fuzzy_query: fuzzyQuery, ancestor: ancestorInfo };
  }

  async function handlePerson(personId) {
    await ensureCoreLoaded();
    const r = personById.get(personId);
    if (!r) return { error: "Researcher not found" };

    const advIds = parentsMap.get(personId) || [];
    const stuIds = (childrenMap.get(personId) || []).slice().sort((a, b) => {
      const ra = personById.get(a), rb = personById.get(b);
      return ((ra && ra[5]) || 9999) - ((rb && rb[5]) || 9999);
    });
    const sibSet = new Set();
    for (const aid of advIds) {
      for (const sid of childrenMap.get(aid) || []) {
        if (sid !== personId) sibSet.add(sid);
      }
    }
    const sibIds = Array.from(sibSet)
      .sort((a, b) => {
        const ra = personById.get(a), rb = personById.get(b);
        const da = r[5] && ra && ra[5] ? Math.abs(ra[5] - r[5]) : 999;
        const db = r[5] && rb && rb[5] ? Math.abs(rb[5] - r[5]) : 999;
        return da - db || ((rb && rb[10]) || 0) - ((ra && ra[10]) || 0);
      })
      .slice(0, 40);

    const allIds = [personId, ...advIds, ...stuIds.slice(0, 120), ...sibIds];
    const detMap = await fetchDetailsForIds(allIds);
    const det = detMap.get(personId) || {};
    const out = rowToBrief(r, det);
    out.orcid = det.or || "";
    out.homepage = det.hp || "";
    out.wikipedia_url = det.wk || "";
    out.google_scholar_url = det.gs || "";
    out.linkedin_url = det.li || "";
    out.advisors = advIds.map((id) => personById.get(id)).filter(Boolean).map((x) => rowToBrief(x, detMap.get(x[0])));
    out.students = stuIds.map((id) => personById.get(id)).filter(Boolean).map((x) => rowToBrief(x, detMap.get(x[0])));
    out.siblings = sibIds.map((id) => personById.get(id)).filter(Boolean).map((x) => rowToBrief(x, detMap.get(x[0])));
    out.sibling_total = sibSet.size;
    out.famous_ancestors = getFamousForPerson(personId);
    return out;
  }

  async function handleTree(personId, sp) {
    await ensureCoreLoaded();
    const focal = personById.get(personId);
    if (!focal) return { error: "Researcher not found" };
    const upDepth = Math.min(45, Math.max(0, Number(sp.get("up_depth") || "6")));
    const downDepth = Math.min(5, Math.max(0, Number(sp.get("down_depth") || "2")));
    const maxChildren = Math.min(120, Math.max(5, Number(sp.get("max_children") || "45")));

    const genOffset = new Map([[personId, 0]]);
    const edgeSet = new Set();
    const hiddenMap = new Map();

    // Upward BFS
    let frontierUp = [personId];
    for (let step = 1; step <= upDepth && frontierUp.length; step++) {
      const nxt = [];
      for (const sid of frontierUp) {
        for (const aid of parentsMap.get(sid) || []) {
          edgeSet.add(`${aid},${sid}`);
          if (!genOffset.has(aid) || -step < genOffset.get(aid)) {
            genOffset.set(aid, -step);
            nxt.push(aid);
          }
        }
      }
      frontierUp = Array.from(new Set(nxt));
    }

    // Downward BFS
    let frontierDn = [personId];
    for (let step = 1; step <= downDepth && frontierDn.length; step++) {
      const nxt = [];
      for (const aid of frontierDn) {
        const ch = (childrenMap.get(aid) || [])
          .map((id) => personById.get(id))
          .filter(Boolean)
          .sort((a, b) => (b[7] & 1) - (a[7] & 1) || b[10] - a[10] || b[9] - a[9]);
        const cap = aid === personId ? Math.max(maxChildren, 90) : maxChildren;
        if (ch.length > cap) hiddenMap.set(aid, ch.length - cap);
        for (const cr of ch.slice(0, cap)) {
          const cid = cr[0];
          edgeSet.add(`${aid},${cid}`);
          if (!genOffset.has(cid)) {
            genOffset.set(cid, step);
            nxt.push(cid);
          }
        }
      }
      frontierDn = Array.from(new Set(nxt));
    }

    const nodeIds = Array.from(genOffset.keys());
    const detMap = await fetchDetailsForIds(nodeIds);
    const nodes = nodeIds
      .map((id) => {
        const r = personById.get(id);
        if (!r) return null;
        const b = rowToBrief(r, detMap.get(id));
        const g = genOffset.get(id);
        b.gen_offset = g;
        b.role = g === 0 ? "focal" : g < 0 ? "ancestor" : "descendant";
        b.hidden_student_count = hiddenMap.get(id) || 0;
        return b;
      })
      .filter(Boolean);

    const links = Array.from(edgeSet).map((k) => {
      const [s, t] = k.split(",").map(Number);
      return { source: s, target: t };
    });

    return { focal_id: personId, nodes, links };
  }

  async function handleLineagePath(sp) {
    await ensureCoreLoaded();
    const stuId = Number(sp.get("student_id"));
    const ancId = Number(sp.get("ancestor_id"));
    const { dist: upDist } = walkUpward(stuId, 60);
    if (!upDist.has(ancId)) return { error: "No directed lineage path found." };
    const upSet = new Set(upDist.keys());

    // Forward BFS from ancId restricted to upSet
    const fDist = new Map([[ancId, 0]]);
    const fEdges = [];
    let frontier = [ancId];
    while (frontier.length) {
      const nxt = [];
      for (const u of frontier) {
        const d = fDist.get(u);
        for (const c of childrenMap.get(u) || []) {
          if (!upSet.has(c)) continue;
          fEdges.push([u, c]);
          if (!fDist.has(c)) {
            fDist.set(c, d + 1);
            nxt.push(c);
          }
        }
      }
      frontier = nxt;
    }

    let keepIds = Array.from(fDist.keys());
    let keepEdges = fEdges;
    if (keepIds.length > 42) {
      // Shortest chain fallback
      const { nextDown } = walkUpward(stuId, 60);
      keepIds = [ancId];
      keepEdges = [];
      let cur = ancId;
      while (cur !== stuId && nextDown.has(cur)) {
        const nxt = nextDown.get(cur);
        keepEdges.push([cur, nxt]);
        keepIds.push(nxt);
        cur = nxt;
      }
    }

    const detMap = await fetchDetailsForIds(keepIds);
    const keepSet = new Set(keepIds);
    const maxG = Math.max(...keepIds.map((id) => fDist.get(id) || 0));
    const nodes = keepIds
      .map((id) => {
        const r = personById.get(id);
        if (!r) return null;
        const b = rowToBrief(r, detMap.get(id));
        b.gen_offset = (fDist.get(id) || 0) - maxG;
        b.role = id === stuId ? "focal" : id === ancId ? "mrca" : "ancestor";
        return b;
      })
      .filter(Boolean);
    const links = keepEdges.filter(([s, t]) => keepSet.has(s) && keepSet.has(t)).map(([s, t]) => ({ source: s, target: t }));
    return { focal_id: stuId, ancestor_id: ancId, nodes, links };
  }

  async function handleConnection(sp) {
    await ensureCoreLoaded();
    const id1 = Number(sp.get("id1"));
    const id2 = Number(sp.get("id2"));
    if (!personById.has(id1) || !personById.has(id2)) return { connected: false, error: "Scholar not found." };
    const u1 = walkUpward(id1, 45);
    const u2 = walkUpward(id2, 45);
    let bestA = null, bestSum = 1e9;
    for (const [a, d1] of u1.dist.entries()) {
      if (u2.dist.has(a)) {
        const sum = d1 + u2.dist.get(a);
        if (sum < bestSum) { bestSum = sum; bestA = a; }
      }
    }
    if (bestA === null) return { connected: false, message: "No shared academic ancestor found within 45 generations." };

    const tracePath = (startA, targetId, nextDown) => {
      const path = [startA];
      let cur = startA;
      while (cur !== targetId && nextDown.has(cur)) {
        cur = nextDown.get(cur);
        path.push(cur);
      }
      return path;
    };
    const p1 = tracePath(bestA, id1, u1.nextDown);
    const p2 = tracePath(bestA, id2, u2.nextDown);
    const d1 = p1.length - 1, d2 = p2.length - 1;
    let rel = "";
    if (d1 === 0) rel = `${personById.get(id1)[1]} is a direct academic ancestor of ${personById.get(id2)[1]} (${d2} gen${d2 === 1 ? "" : "s"} apart)`;
    else if (d2 === 0) rel = `${personById.get(id2)[1]} is a direct academic ancestor of ${personById.get(id1)[1]} (${d1} gen${d1 === 1 ? "" : "s"} apart)`;
    else if (d1 === 1 && d2 === 1) rel = "Academic Siblings (shared direct PhD advisor)";
    else rel = `Academic Cousins (${d1}↑ and ${d2}↑ generations from MRCA)`;

    const nodeGen = new Map();
    const edgeSet = new Set();
    p1.forEach((id, idx) => { nodeGen.set(id, idx); if (idx > 0) edgeSet.add(`${p1[idx - 1]},${id}`); });
    p2.forEach((id, idx) => { if (!nodeGen.has(id)) nodeGen.set(id, idx); if (idx > 0) edgeSet.add(`${p2[idx - 1]},${id}`); });
    const allIds = Array.from(nodeGen.keys());
    const detMap = await fetchDetailsForIds(allIds);
    const nodes = allIds.map((id) => {
      const b = rowToBrief(personById.get(id), detMap.get(id));
      b.gen_offset = nodeGen.get(id);
      b.role = id === bestA ? "mrca" : id === id1 || id === id2 ? "focal" : "ancestor";
      return b;
    });
    const links = Array.from(edgeSet).map((k) => {
      const [s, t] = k.split(",").map(Number);
      return { source: s, target: t };
    });
    return { connected: true, relationship: rel, mrca: rowToBrief(personById.get(bestA), detMap.get(bestA)), nodes, links };
  }

  function jsonResponse(obj, status = 200) {
    return new Response(JSON.stringify(obj), {
      status,
      headers: { "Content-Type": "application/json; charset=utf-8" },
    });
  }

  // Intercept `/api/*` calls seamlessly
  window.fetch = async (input, init) => {
    const urlStr = typeof input === "string" ? input : input && input.url ? input.url : "";
    if (!urlStr.startsWith("/api/")) {
      return origFetch(input, init);
    }
    const u = new URL(urlStr, window.location.origin);
    const path = u.pathname;
    const sp = u.searchParams;

    try {
      if (path === "/api/stats") {
        if (!statsPromise) statsPromise = origFetch("data/stats.json").then((r) => r.json());
        return jsonResponse(await statsPromise);
      }
      if (path === "/api/topic_map") {
        if (!topicMapPromise) topicMapPromise = origFetch("data/topic_map.json").then((r) => r.json());
        return jsonResponse(await topicMapPromise);
      }
      if (path === "/api/random_person") {
        await ensureCoreLoaded();
        const pid = luckyIds[Math.floor(Math.random() * luckyIds.length)];
        return jsonResponse(rowToBrief(personById.get(pid)));
      }
      if (path === "/api/schools") return jsonResponse(await handleSchools(sp));
      if (path === "/api/search") return jsonResponse(await handleSearch(sp));
      if (path === "/api/lineage_path") return jsonResponse(await handleLineagePath(sp));
      if (path === "/api/connection") return jsonResponse(await handleConnection(sp));
      const mPerson = path.match(/^\/api\/person\/(\d+)$/);
      if (mPerson) return jsonResponse(await handlePerson(Number(mPerson[1])));
      const mTree = path.match(/^\/api\/tree\/(\d+)$/);
      if (mTree) return jsonResponse(await handleTree(Number(mTree[1]), sp));
      return jsonResponse({ error: "Not found" }, 404);
    } catch (err) {
      console.error("Static API error:", path, err);
      return jsonResponse({ error: String(err) }, 500);
    }
  };
})();
