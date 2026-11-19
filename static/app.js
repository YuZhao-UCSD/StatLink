/* global d3 */
(() => {
  "use strict";

  // -------------------------------------------------------------------------
  // Application State
  // -------------------------------------------------------------------------
  const state = {
    currentView: "explore",
    scope: "core",
    query: "",
    school: "",
    schoolRole: "both",
    yearMin: "",
    yearMax: "",
    sort: "relevance",
    browseMode: false,
    page: 1,
    perPage: 36,

    // Tree Visualizer state
    focalId: 14941, // Default: Bradley Efron
    selectedDossierId: null,
    upDepth: 6,
    downDepth: 2,
    layoutDir: "vertical", // "vertical" | "horizontal"
    treeData: null,
    fullTreeBackup: null,
    activeTraceAncestorId: null,
    pendingTraceAncestorId: null,

    // Kinship state
    kin1: { id: 14941, name: "Bradley Efron" },
    kin2: { id: 47762, name: "Xihong Lin" },

    // Global stats cache
    globalStats: null,
  };

  const FIELD_COLORS = {
    "Statistics": "#3b5bdb",
    "Biostatistics": "#0ca678",
    "Probability": "#7950f2",
    "Related Quantitative": "#d97706",
    "Mathematics & Other": "#64748b",
  };

  // -------------------------------------------------------------------------
  // Utility Helpers
  // -------------------------------------------------------------------------
  function esc(str) {
    if (str === null || str === undefined) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function fmtNum(n) {
    if (n === null || n === undefined) return "0";
    return Number(n).toLocaleString("en-US");
  }

  function trunc(str, maxLen) {
    if (!str) return "";
    return str.length > maxLen ? str.slice(0, maxLen - 1) + "…" : str;
  }

  function debounce(fn, ms) {
    let timer = null;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), ms);
    };
  }

  // -------------------------------------------------------------------------
  // View & Theme Switching
  // -------------------------------------------------------------------------
  function switchView(viewName) {
    state.currentView = viewName;
    document.querySelectorAll(".view-section").forEach((el) => {
      el.classList.toggle("active", el.id === `view-${viewName}`);
    });
    document.querySelectorAll(".nav-tab").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.view === viewName);
    });

    if (viewName === "tree") {
      if (!state.treeData || state.treeData.focal_id !== state.focalId) {
        loadPersonTree(state.focalId);
      } else {
        setTimeout(() => renderGenealogyGraph("#treeSvg", state.treeData, true), 40);
      }
    } else if (viewName === "sources" && state.globalStats) {
      setTimeout(() => renderDecadeChart(state.globalStats.summary.year_histogram || state.globalStats.summary.decade_histogram), 40);
    }
  }

  function initTheme() {
    const saved = localStorage.getItem("statlineage_theme") || "light";
    setTheme(saved);
    document.getElementById("themeToggleBtn").addEventListener("click", () => {
      const curr = document.documentElement.getAttribute("data-theme") || "light";
      setTheme(curr === "light" ? "dark" : "light");
    });
  }

  function setTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("statlineage_theme", theme);
    document.getElementById("themeIconSun").classList.toggle("hidden", theme === "light");
    document.getElementById("themeIconMoon").classList.toggle("hidden", theme === "dark");
    if (state.currentView === "sources" && state.globalStats) {
      renderDecadeChart(state.globalStats.summary.year_histogram || state.globalStats.summary.decade_histogram);
    }
  }

  // -------------------------------------------------------------------------
  // Global Stats, Featured Scholars & Analytics Initialization
  // -------------------------------------------------------------------------
  async function loadGlobalStats() {
    try {
      const resp = await fetch("/api/stats");
      const data = await resp.json();
      state.globalStats = data;

      // Featured chips (5 representative scholars across historical eras)
      const fChips = document.getElementById("featuredChips");
      fChips.innerHTML = (data.featured || [])
        .map((p) => {
          const dispName = p.famous_self ? p.famous_self.display_name : p.name;
          return `<button class="person-chip" data-pid="${p.id}" title="${esc(p.school)} (${p.year || "—"}) · ${fmtNum(p.descendant_count)} descendants">${esc(dispName)}${p.year ? " (" + p.year + ")" : ""}</button>`;
        })
        .join("");
      fChips.querySelectorAll(".person-chip").forEach((btn) => {
        btn.addEventListener("click", () => openPersonInTree(Number(btn.dataset.pid)));
      });

      populateSourcesView(data);
      loadMasterSchoolsDirectory();
    } catch (err) {
      console.error("Failed loading stats:", err);
    }
  }

  let masterSchoolsCache = [];
  async function loadMasterSchoolsDirectory() {
    try {
      const resp = await fetch("/api/master_schools");
      const data = await resp.json();
      masterSchoolsCache = data.schools || [];
      renderMasterSchoolsGrid("");
    } catch (err) {
      console.error("Failed loading master schools:", err);
    }
  }

  function renderMasterSchoolsGrid(filterText) {
    const grid = document.getElementById("masterSchoolsDirectory");
    if (!grid) return;
    const q = (filterText || "").toLowerCase().trim();
    const filtered = !q
      ? masterSchoolsCache
      : masterSchoolsCache.filter(
          (s) =>
            s.school.toLowerCase().includes(q) ||
            (s.country || "").toLowerCase().includes(q) ||
            (s.aliases || "").toLowerCase().includes(q) ||
            (s.departments || []).some((d) => d.toLowerCase().includes(q))
        );

    grid.innerHTML = filtered
      .map(
        (s) => `
        <div class="source-detail-box" style="padding:12px 14px;display:flex;flex-direction:column;justify-content:space-between;gap:8px;">
          <div>
            <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:6px;">
              <strong style="font-size:13.5px;color:var(--text-primary);">${esc(s.school)}</strong>
              <span style="font-size:11px;color:var(--text-muted);white-space:nowrap;">${esc(s.country)}</span>
            </div>
            <div style="font-size:11.5px;color:var(--text-secondary);margin-top:4px;">${esc((s.departments || []).join(" · "))}</div>
          </div>
          <div style="display:flex;justify-content:space-between;align-items:center;padding-top:6px;border-top:1px solid var(--border);gap:6px;">
            <span style="font-size:11.5px;font-weight:600;color:var(--text-muted);">
              🎓 ${fmtNum(s.core_phd_graduates)} PhDs · 👨‍🏫 ${fmtNum(s.core_faculty_advisors)} Faculty
            </span>
            <div style="display:flex;gap:5px;">
              <button class="era-chip ms-jump" data-school="${esc(s.school)}" data-role="faculty">Faculty</button>
              <button class="era-chip ms-jump" data-school="${esc(s.school)}" data-role="graduates">Alumni</button>
            </div>
          </div>
        </div>`
      )
      .join("");

    grid.querySelectorAll(".ms-jump").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.schoolRole = btn.dataset.role;
        document.querySelectorAll("#schoolRoleGroup .era-chip").forEach((c) => {
          c.classList.toggle("active", c.dataset.srole === state.schoolRole);
        });
        applySchoolFilter(btn.dataset.school);
        switchView("explore");
      });
    });
  }

  function populateSourcesView(data) {
    const s = data.summary || {};
    const byField = s.by_field || {};
    const kpis = [
      {
        num: fmtNum(s.core_stat_total),
        title: "Core Stat · Biostat · Prob PhDs",
        desc: `${fmtNum(byField["Statistics"] || 0)} Stat · ${fmtNum(byField["Biostatistics"] || 0)} Biostat · ${fmtNum(byField["Probability"] || 0)} Prob`,
      },
      {
        num: fmtNum(s.broad_stat_total),
        title: "Broad Quantitative PhDs",
        desc: "Includes Econometrics, Statistical ML, OR & Quantitative Epi",
      },
      {
        num: fmtNum(s.total_people),
        title: "Total Connected Scholars",
        desc: "Complete unbroken ancestral & descendant lineages",
      },
      {
        num: fmtNum(s.total_edges),
        title: "Verified PhD Advisor Edges",
        desc: "Strictly doctoral (MGP + AcademicTree relation=1 + Wikidata)",
      },
      {
        num: fmtNum(s.core_stat_schools),
        title: "PhD-Granting Universities",
        desc: `Canonicalized across ${fmtNum(s.total_schools)} global institutions`,
      },
    ];

    document.getElementById("sourcesKpiGrid").innerHTML = kpis
      .map(
        (k) => `
        <div class="kpi-card">
          <div class="kpi-num">${k.num}</div>
          <div class="kpi-title">${esc(k.title)}</div>
          <div class="kpi-desc">${esc(k.desc)}</div>
        </div>`
      )
      .join("");

    const lb = document.getElementById("sourcesSchoolTable");
    lb.innerHTML = (data.top_schools || [])
      .map(
        (sch, idx) => `
        <button class="person-row-btn" data-school="${esc(sch.school)}">
          <div>
            <div class="pr-name">${idx + 1}. ${esc(sch.school)}</div>
            <div class="pr-meta">${esc(sch.country || "")}${sch.aliases ? " · " + esc(sch.aliases) : ""}</div>
          </div>
          <span class="ac-count">${fmtNum(sch.core_stat_count)} PhDs</span>
        </button>`
      )
      .join("");
    lb.querySelectorAll(".person-row-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        applySchoolFilter(btn.dataset.school);
        switchView("explore");
      });
    });

    if (state.currentView === "sources") {
      renderDecadeChart(s.year_histogram || s.decade_histogram);
    }
  }

  function renderDecadeChart(histObj) {
    const container = document.getElementById("decadeChartContainer");
    if (!container || !histObj) return;
    container.innerHTML = "";
    const width = container.clientWidth || 580;
    const height = container.clientHeight || 295;
    const margin = { top: 16, right: 16, bottom: 36, left: 48 };

    const minYear = 1920;
    const maxYear = 2026;
    const years = [];
    for (let y = minYear; y <= maxYear; y++) {
      years.push(y);
    }

    const keys = ["Statistics", "Biostatistics", "Probability"];
    const rows = years.map((yr) => {
      const item = histObj[String(yr)] || {};
      const st = item["Statistics"] || 0;
      const bi = item["Biostatistics"] || 0;
      const pr = item["Probability"] || 0;
      return {
        year: String(yr),
        yearNum: yr,
        Statistics: st,
        Biostatistics: bi,
        Probability: pr,
        total: st + bi + pr,
      };
    });

    const stack = d3.stack().keys(keys)(rows);
    const maxY = d3.max(rows, (r) => r.total) || 100;

    const svg = d3.select(container).append("svg").attr("width", width).attr("height", height);
    const x = d3
      .scaleBand()
      .domain(rows.map((r) => r.year))
      .range([margin.left, width - margin.right])
      .paddingInner(0.14)
      .paddingOuter(0.06);
    const y = d3
      .scaleLinear()
      .domain([0, maxY * 1.08])
      .nice()
      .range([height - margin.bottom, margin.top]);

    // Show clean 10-year tick labels (or 5-year on wide screens) while every bar is 1 single year
    const step = width >= 760 ? 5 : 10;
    const tickYears = rows
      .filter((r) => r.yearNum % step === 0 || r.yearNum === maxYear)
      .map((r) => r.year)
      .filter((yr, i, arr) => !(yr === String(maxYear) && arr.includes("2025") && width < 760));

    svg
      .append("g")
      .attr("transform", `translate(0,${height - margin.bottom})`)
      .call(d3.axisBottom(x).tickValues(tickYears).tickSizeOuter(0))
      .selectAll("text")
      .style("font-size", "10.5px");

    svg
      .append("g")
      .attr("transform", `translate(${margin.left},0)`)
      .call(d3.axisLeft(y).ticks(5))
      .selectAll("text")
      .style("font-size", "10.5px");

    // Floating hover tooltip
    const tip = d3
      .select(container)
      .append("div")
      .attr("class", "tree-tooltip hidden")
      .style("padding", "7px 10px")
      .style("font-size", "11.5px")
      .style("pointer-events", "none");

    svg
      .append("g")
      .selectAll("g")
      .data(stack)
      .join("g")
      .attr("fill", (d) => FIELD_COLORS[d.key])
      .selectAll("rect")
      .data((d) => d)
      .join("rect")
      .attr("x", (d) => x(d.data.year))
      .attr("y", (d) => y(d[1]))
      .attr("height", (d) => Math.max(0, y(d[0]) - y(d[1])))
      .attr("width", Math.max(1.5, x.bandwidth()))
      .attr("rx", 1)
      .on("mouseenter", function (event, d) {
        d3.select(this).attr("opacity", 0.82);
        const r = d.data;
        tip
          .html(
            `<div style="font-weight:800;margin-bottom:3px;">Year ${r.year} — ${fmtNum(r.total)} PhDs</div>` +
              `<div style="color:${FIELD_COLORS.Statistics};font-weight:600;">● Statistics: ${fmtNum(r.Statistics)}</div>` +
              `<div style="color:${FIELD_COLORS.Biostatistics};font-weight:600;">● Biostatistics: ${fmtNum(r.Biostatistics)}</div>` +
              `<div style="color:${FIELD_COLORS.Probability};font-weight:600;">● Probability: ${fmtNum(r.Probability)}</div>`
          )
          .classed("hidden", false);
      })
      .on("mousemove", function (event) {
        const rect = container.getBoundingClientRect();
        const left = Math.min(rect.width - 175, Math.max(8, event.clientX - rect.left + 12));
        const top = Math.max(6, event.clientY - rect.top - 78);
        tip.style("left", `${left}px`).style("top", `${top}px`);
      })
      .on("mouseleave", function () {
        d3.select(this).attr("opacity", 1);
        tip.classed("hidden", true);
      });
  }

  // -------------------------------------------------------------------------
  // Directory Search, School Autocomplete & Filtering
  // -------------------------------------------------------------------------
  function isExploreHomeState() {
    return !state.query && !state.school && !state.yearMin && !state.yearMax && state.scope === "core" && !state.browseMode;
  }

  function syncExploreLayoutMode() {
    const layout = document.getElementById("exploreLayout");
    if (!layout) return;
    const home = isExploreHomeState();
    layout.classList.toggle("is-home", home);
    layout.classList.toggle("has-results", !home);
  }

  function resetToSearchHome() {
    state.query = "";
    state.school = "";
    state.yearMin = "";
    state.yearMax = "";
    state.scope = "core";
    state.sort = "relevance";
    state.browseMode = false;
    state.page = 1;

    const mainInp = document.getElementById("mainSearchInput");
    if (mainInp) mainInp.value = "";
    document.getElementById("clearSearchBtn")?.classList.add("hidden");
    const schInp = document.getElementById("schoolFilterInput");
    if (schInp) schInp.value = "";
    document.getElementById("clearSchoolBtn")?.classList.add("hidden");
    const yminInp = document.getElementById("yearMinInput");
    if (yminInp) yminInp.value = "";
    const ymaxInp = document.getElementById("yearMaxInput");
    if (ymaxInp) ymaxInp.value = "";
    const sortSel = document.getElementById("sortSelect");
    if (sortSel) sortSel.value = "relevance";

    document.querySelectorAll(".era-chip[data-ymin]").forEach((c) => c.classList.remove("active"));
    document.querySelectorAll(".scope-pill").forEach((b) => {
      b.classList.toggle("active", b.dataset.scope === "core");
    });
    toggleMobileFilters(false);
    executeSearch();
  }

  async function executeSearch() {
    syncExploreLayoutMode();
    renderActiveFilterTags();

    if (isExploreHomeState()) {
      const grid = document.getElementById("resultsGrid");
      const pag = document.getElementById("paginationBar");
      const summaryEl = document.getElementById("resultsSummaryText");
      if (grid) grid.innerHTML = "";
      if (pag) pag.innerHTML = "";
      if (summaryEl) summaryEl.textContent = "";
      return;
    }

    const params = new URLSearchParams({
      scope: state.scope,
      sort: state.sort,
      page: String(state.page),
      per_page: String(state.perPage),
    });
    if (state.query) params.set("q", state.query);
    if (state.school) {
      params.set("school", state.school);
      params.set("school_role", state.schoolRole);
    }
    if (state.yearMin) params.set("year_min", state.yearMin);
    if (state.yearMax) params.set("year_max", state.yearMax);

    const summaryEl = document.getElementById("resultsSummaryText");
    summaryEl.textContent = "Searching…";

    try {
      const resp = await fetch(`/api/search?${params.toString()}`);
      const data = await resp.json();
      renderSearchResults(data);
    } catch (err) {
      summaryEl.textContent = "Error loading search results.";
      console.error(err);
    }
  }

  function renderSearchResults(data) {
    const grid = document.getElementById("resultsGrid");
    const summaryEl = document.getElementById("resultsSummaryText");
    const results = data.results || [];
    const total = data.total || 0;

    let sumTxt = `Showing ${fmtNum(results.length)} of ${fmtNum(total)} PhD scholars`;
    if (data.expanded_from_all) {
      sumTxt += " (including exact matches across all mathematical & connected fields)";
    }
    summaryEl.textContent = sumTxt;

    if (!results.length) {
      grid.innerHTML = `
        <div class="analytics-card" style="grid-column: 1 / -1; text-align:center; padding: 34px;">
          <h3>No matching researchers found in current filter</h3>
          <p class="card-sub">Try clearing school or graduation year filters, or switching Disciplinary Scope to "All".</p>
        </div>`;
      document.getElementById("paginationBar").innerHTML = "";
      return;
    }

    grid.innerHTML = results
      .map((p) => {
        const yrStr = p.year ? String(p.year) : "Year n/a";
        const schHtml = p.school
          ? `<span class="school-link-inline" data-school="${esc(p.school)}" title="Filter by ${esc(p.school)}">${esc(p.school)}</span>`
          : `<span>Institution unlisted</span>`;
        const thesisHtml = p.thesis
          ? `<div class="scholar-thesis">“${esc(p.thesis)}”</div>`
          : `<div class="scholar-thesis" style="opacity:0.55">Dissertation title not recorded</div>`;
        const srcBadges = (p.sources || []).join(" · ");

        const affilNote =
          p.current_affiliation && p.current_affiliation !== p.school
            ? `<div style="font-size:11.5px;color:var(--accent);font-weight:600;margin-top:2px;">Faculty / Affiliation: ${esc(p.current_affiliation)}</div>`
            : "";

        const famPrev = p.famous_ancestors_preview || [];
        const famousRowHtml = famPrev.length
          ? `<div class="famous-preview-row">
              <span class="famous-preview-label">★ Ancestors:</span>
              ${famPrev
                .map(
                  (fa) =>
                    `<button type="button" class="famous-preview-chip" data-ancid="${fa.id}" title="${esc(fa.display_name)} (${fa.generations} generations up) — ${esc(fa.epithet)}">${esc(fa.short_name)} · ${fa.generations}↑</button>`
                )
                .join("")}
            </div>`
          : "";

        const selfStar = p.famous_self ? `<span title="${esc(p.famous_self.epithet)}" style="color:var(--gold-main);margin-right:3px;">★</span>` : "";

        return `
        <article class="scholar-card" data-pid="${p.id}" data-field="${esc(p.primary_field)}">
          <div>
            <div class="card-top">
              <h3 class="scholar-name">${selfStar}${esc(p.name)}</h3>
              <span class="field-pill" data-field="${esc(p.primary_field)}">${esc(p.primary_field)}</span>
            </div>
            <div class="scholar-meta">${schHtml} · <strong>${yrStr}</strong>${p.country ? " · " + esc(p.country) : ""}</div>
            ${affilNote}
          </div>
          ${thesisHtml}
          ${famousRowHtml}
          <div class="card-footer">
            <div class="lineage-metrics">
              <span title="Direct PhD Students">🎓 ${fmtNum(p.student_count)} students</span>
              <span title="Total Academic Descendants">🌳 ${fmtNum(p.descendant_count)} desc.</span>
            </div>
            <span class="view-tree-cta" title="Sources: ${esc(srcBadges)}">View Tree →</span>
          </div>
        </article>`;
      })
      .join("");

    grid.querySelectorAll(".scholar-card").forEach((card) => {
      card.addEventListener("click", (e) => {
        const schTarget = e.target.closest(".school-link-inline");
        if (schTarget) {
          e.stopPropagation();
          applySchoolFilter(schTarget.dataset.school);
          return;
        }
        const ancChip = e.target.closest(".famous-preview-chip");
        if (ancChip) {
          e.stopPropagation();
          openPersonInTree(Number(card.dataset.pid), Number(ancChip.dataset.ancid));
          return;
        }
        openPersonInTree(Number(card.dataset.pid));
      });
    });

    // Pagination controls
    const totalPages = Math.max(1, Math.ceil(total / state.perPage));
    const pagBar = document.getElementById("paginationBar");
    if (totalPages <= 1) {
      pagBar.innerHTML = "";
    } else {
      pagBar.innerHTML = `
        <button class="page-btn" id="prevPageBtn" ${state.page <= 1 ? "disabled" : ""}>← Previous</button>
        <span style="font-size:13px;font-weight:600;color:var(--text-secondary);">Page ${state.page} of ${fmtNum(totalPages)}</span>
        <button class="page-btn" id="nextPageBtn" ${state.page >= totalPages ? "disabled" : ""}>Next →</button>
      `;
      document.getElementById("prevPageBtn")?.addEventListener("click", () => {
        if (state.page > 1) {
          state.page--;
          executeSearch();
          window.scrollTo({ top: 0, behavior: "smooth" });
        }
      });
      document.getElementById("nextPageBtn")?.addEventListener("click", () => {
        if (state.page < totalPages) {
          state.page++;
          executeSearch();
          window.scrollTo({ top: 0, behavior: "smooth" });
        }
      });
    }
  }

  let filterOverflowTimer = null;
  function toggleMobileFilters(forceOpen) {
    const collapse = document.getElementById("inlineFiltersCollapse");
    const layout = document.getElementById("exploreLayout");
    const heroBtn = document.getElementById("openFiltersHeroBtn");
    if (!collapse) return;
    const isOpen = forceOpen !== undefined ? forceOpen : !collapse.classList.contains("is-open");
    clearTimeout(filterOverflowTimer);
    if (isOpen) {
      collapse.classList.add("is-open");
      filterOverflowTimer = setTimeout(() => {
        if (collapse.classList.contains("is-open")) {
          collapse.classList.add("overflow-ready");
        }
      }, 390);
    } else {
      collapse.classList.remove("overflow-ready");
      collapse.classList.remove("is-open");
    }
    if (layout) layout.classList.toggle("filters-open", isOpen);
    if (heroBtn) heroBtn.setAttribute("aria-expanded", isOpen ? "true" : "false");
  }

  function renderActiveFilterTags() {
    const wrap = document.getElementById("activeFilterTags");
    const tags = [];
    if (state.school) {
      tags.push(`<span class="filter-tag">School: ${esc(state.school)} <button data-clear="school">&times;</button></span>`);
    }
    if (state.yearMin || state.yearMax) {
      tags.push(
        `<span class="filter-tag">Years: ${esc(state.yearMin || "…")}–${esc(state.yearMax || "…")} <button data-clear="year">&times;</button></span>`
      );
    }
    if (state.scope !== "core") {
      tags.push(`<span class="filter-tag">Scope: ${esc(state.scope)} <button data-clear="scope">&times;</button></span>`);
    }
    const fBadge = document.getElementById("mobileFilterCountBadge");
    if (fBadge) {
      fBadge.textContent = String(tags.length);
      fBadge.classList.toggle("hidden", tags.length === 0);
    }
    wrap.innerHTML = tags.join("");
    wrap.querySelectorAll("button[data-clear]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const kind = btn.dataset.clear;
        if (kind === "school") applySchoolFilter("");
        if (kind === "year") {
          state.yearMin = "";
          state.yearMax = "";
          document.getElementById("yearMinInput").value = "";
          document.getElementById("yearMaxInput").value = "";
          document.querySelectorAll(".era-chip[data-ymin]").forEach((c) => c.classList.remove("active"));
          state.page = 1;
          executeSearch();
        }
        if (kind === "scope") {
          setScope("core");
        }
      });
    });
  }

  function applySchoolFilter(schoolName) {
    state.school = schoolName;
    state.page = 1;
    const inp = document.getElementById("schoolFilterInput");
    inp.value = schoolName;
    document.getElementById("clearSchoolBtn").classList.toggle("hidden", !schoolName);
    document.getElementById("schoolDropdown").classList.add("hidden");
    executeSearch();
  }

  function setScope(newScope) {
    state.scope = newScope;
    state.page = 1;
    document.querySelectorAll(".scope-pill").forEach((b) => {
      b.classList.toggle("active", b.dataset.scope === newScope);
    });
    executeSearch();
  }

  // -------------------------------------------------------------------------
  // Interactive Family Tree Visualizer & Dossier
  // -------------------------------------------------------------------------
  async function openPersonInTree(personId, traceAncestorId = null) {
    state.focalId = personId;
    state.pendingTraceAncestorId = traceAncestorId;
    if (state.currentView !== "tree") {
      switchView("tree");
    } else {
      await loadPersonTree(personId);
    }
  }

  async function loadPersonTree(personId) {
    state.focalId = personId;
    state.selectedDossierId = personId;
    state.activeTraceAncestorId = null;
    const overlay = document.getElementById("treeLoadingOverlay");
    overlay.classList.remove("hidden");

    try {
      const [treeResp, personResp] = await Promise.all([
        fetch(`/api/tree/${personId}?up_depth=${state.upDepth}&down_depth=${state.downDepth}&max_children=45`),
        fetch(`/api/person/${personId}`),
      ]);
      const treeData = await treeResp.json();
      const personData = await personResp.json();

      if (treeData.error) {
        overlay.textContent = treeData.error;
        return;
      }

      state.treeData = treeData;
      state.fullTreeBackup = treeData;

      // Update active badge
      const chip = document.getElementById("activePersonChip");
      chip.textContent = personData.name;
      chip.classList.remove("hidden");

      document.getElementById("exitTraceModeBtn")?.classList.add("hidden");
      renderDossier(personData);

      const pendingAnc = state.pendingTraceAncestorId;
      state.pendingTraceAncestorId = null;
      if (pendingAnc) {
        await traceFamousAncestorOnCanvas(personId, pendingAnc);
      } else {
        renderGenealogyGraph("#treeSvg", treeData, true);
        updateTreeStatsBar(treeData);
      }
      window.location.hash = `person=${personId}`;
    } catch (err) {
      console.error("Error loading tree:", err);
    } finally {
      overlay.classList.add("hidden");
    }
  }

  async function traceFamousAncestorOnCanvas(studentId, ancestorId) {
    try {
      const resp = await fetch(`/api/lineage_path?student_id=${studentId}&ancestor_id=${ancestorId}`);
      const pathData = await resp.json();
      if (pathData.error || !pathData.nodes) return;
      state.activeTraceAncestorId = ancestorId;
      state.treeData = pathData;
      document.getElementById("exitTraceModeBtn")?.classList.remove("hidden");
      renderGenealogyGraph("#treeSvg", pathData, true);
      document.getElementById("treeStatsSummary").textContent =
        `Tracing academic lineage: ${pathData.ancestor_name} → ${pathData.focal_name} (${pathData.generations} generations · ${pathData.nodes.length} scholars shown)`;
    } catch (err) {
      console.error("Error tracing famous ancestor path:", err);
    }
  }

  async function loadDossierOnly(personId) {
    state.selectedDossierId = personId;
    const dp = document.getElementById("dossierPanel");
    if (dp && dp.classList.contains("mobile-minimized")) {
      dp.classList.remove("mobile-minimized");
      const minBtn = document.getElementById("mobileDossierMinBtn");
      if (minBtn) minBtn.textContent = "Hide ▾";
    }
    d3.select("#treeSvg")
      .selectAll(".tree-node")
      .classed("selected", (d) => d.id === personId);
    try {
      const resp = await fetch(`/api/person/${personId}`);
      const p = await resp.json();
      if (!p.error) renderDossier(p);
    } catch (err) {
      console.error(err);
    }
  }

  function updateTreeStatsBar(treeData) {
    const st = treeData.stats || {};
    const ea = st.earliest_ancestor;
    const eaTxt = ea ? ` · Earliest root shown: ${ea.name}${ea.year ? " (" + ea.year + ")" : ""}` : "";
    document.getElementById("treeStatsSummary").textContent =
      `Showing ${fmtNum(st.total_nodes_shown)} scholars (${st.ancestor_count} ancestors ↑, ${st.descendant_shown_count} of ${fmtNum(st.total_descendant_count)} total descendants ↓)${eaTxt}`;
  }

  function renderDossier(p) {
    const box = document.getElementById("dossierContent");
    const isFocal = p.id === state.focalId;
    const mTitle = document.getElementById("mobileDossierTitle");
    if (mTitle) {
      mTitle.textContent = `${p.name}${p.year ? " · PhD " + p.year : ""}`;
    }
    if (box) box.scrollTop = 0;

    const extLinks = [];
    if (p.mgp_id) {
      extLinks.push(
        `<a class="ext-pill" href="https://www.mathgenealogy.org/id.php?id=${p.mgp_id}" target="_blank" rel="noopener">MGP #${p.mgp_id} ↗</a>`
      );
    }
    if (p.aft_pid) {
      extLinks.push(
        `<a class="ext-pill" href="https://academictree.org/math/peopleinfo.php?pid=${p.aft_pid}" target="_blank" rel="noopener">AcademicTree #${p.aft_pid} ↗</a>`
      );
    }
    if (p.wikidata_qid) {
      extLinks.push(
        `<a class="ext-pill" href="https://www.wikidata.org/wiki/${esc(p.wikidata_qid)}" target="_blank" rel="noopener">Wikidata (${esc(p.wikidata_qid)}) ↗</a>`
      );
    }
    if (p.orcid) {
      extLinks.push(
        `<a class="ext-pill" href="https://orcid.org/${esc(p.orcid)}" target="_blank" rel="noopener">ORCID ↗</a>`
      );
    }
    if (p.homepage) {
      extLinks.push(`<a class="ext-pill" href="${esc(p.homepage)}" target="_blank" rel="noopener">Homepage ↗</a>`);
    }

    const renderCompactRows = (arr, emptyMsg) => {
      if (!arr || !arr.length) return `<div style="font-size:12px;color:var(--text-muted);">${emptyMsg}</div>`;
      return `<div class="person-list-compact">${arr
        .map(
          (x) => `
          <button class="person-row-btn" data-pid="${x.id}">
            <div>
              <div class="pr-name">${x.famous_self ? '<span style="color:var(--gold-main);margin-right:3px;">★</span>' : ""}${esc(x.name)}</div>
              <div class="pr-meta">${esc(x.school || "School unlisted")}${x.year ? " · " + x.year : ""}</div>
            </div>
            <span class="field-pill" data-field="${esc(x.primary_field)}">${x.descendant_count > 0 ? "🌳 " + fmtNum(x.descendant_count) : esc(x.primary_field.split(" ")[0])}</span>
          </button>`
        )
        .join("")}</div>`;
    };

    box.innerHTML = `
      <div class="dossier-header">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:6px;">
          <span class="field-pill" data-field="${esc(p.primary_field)}">${esc(p.primary_field)}</span>
          <span style="font-size:11px;color:var(--text-muted);" title="Classification provenance">${esc(p.classification_reason)}</span>
        </div>
        <h2 class="dossier-title">${p.famous_self ? '<span style="color:var(--gold-main);margin-right:4px;">★</span>' : ""}${esc(p.name)}</h2>
        <div class="dossier-sub">
          ${p.school ? `<a href="#" class="school-link-inline" id="dossierSchoolLink" data-school="${esc(p.school)}">${esc(p.school)}</a>` : "Institution unlisted"}
          ${p.year ? ` · <strong>PhD ${p.year}</strong>` : ""}
          ${p.country ? ` · ${esc(p.country)}` : ""}
        </div>
        ${p.current_affiliation && p.current_affiliation !== p.school ? `<div style="font-size:12px;color:var(--text-muted);margin-top:3px;">Affiliation: ${esc(p.current_affiliation)}</div>` : ""}
        ${p.research_areas ? `<div style="font-size:12px;color:var(--text-muted);margin-top:2px;">Areas: ${esc(p.research_areas)}</div>` : ""}
        ${p.famous_self ? `<div class="famous-self-banner">★ World-Famous Scientist — ${esc(p.famous_self.epithet)}</div>` : ""}

        <div class="dossier-actions">
          ${!isFocal ? `<button class="btn-primary" id="recenterDossierBtn" style="padding:6px 12px;font-size:12px;">Center Tree on ${esc(p.name)}</button>` : ""}
          <button class="btn-secondary" id="compareKinBtn" style="padding:6px 11px;font-size:12px;">Compare Kinship ↔</button>
        </div>
      </div>

      <div class="dossier-kpis">
        <div class="d-kpi">
          <div class="d-kpi-val">${fmtNum(p.advisor_count)}</div>
          <div class="d-kpi-lbl">PhD Advisors</div>
        </div>
        <div class="d-kpi">
          <div class="d-kpi-val">${fmtNum(p.student_count)}</div>
          <div class="d-kpi-lbl">PhD Students</div>
        </div>
        <div class="d-kpi">
          <div class="d-kpi-val">${fmtNum(p.descendant_count)}</div>
          <div class="d-kpi-lbl">Total Descendants</div>
        </div>
      </div>

      ${
        p.thesis
          ? `<div class="dossier-block">
              <h4>Doctoral Dissertation</h4>
              <div class="thesis-quote">“${esc(p.thesis)}”</div>
            </div>`
          : ""
      }

      ${
        extLinks.length
          ? `<div class="dossier-block">
              <h4>Verified Source Records (${esc((p.sources || []).join(", "))})</h4>
              <div class="ext-links">${extLinks.join("")}</div>
            </div>`
          : ""
      }

      <div class="dossier-block">
        <h4>Doctoral Advisor${(p.advisors || []).length === 1 ? "" : "s"} (${(p.advisors || []).length})</h4>
        ${renderCompactRows(p.advisors, "No doctoral advisor recorded in database.")}
      </div>

      ${(() => {
        const famObj = p.famous_ancestors || {};
        const hl = famObj.highlighted || [];
        const more = famObj.more || [];
        if (!hl.length) return "";

        const renderFamousItem = (fa) => {
          const chainSteps = (fa.chain || [])
            .map(
              (c, idx) =>
                `${idx > 0 ? `<span class="chain-arrow">→</span>` : ""}<button type="button" class="chain-step-btn" data-pid="${c.id}" title="${esc(c.school || "")}${c.year ? " (" + c.year + ")" : ""}">${esc(c.name)}${c.year ? " ’" + String(c.year).slice(-2) : ""}</button>`
            )
            .join("");

          return `
            <div class="famous-anc-item">
              <div class="famous-anc-top">
                <span class="famous-anc-name" data-pid="${fa.id}">★ ${esc(fa.display_name)}${fa.year ? ` <span style="font-weight:500;color:var(--text-muted);">(${fa.year})</span>` : ""}</span>
                <span class="famous-gen-badge">${fa.generations} gen${fa.generations === 1 ? "" : "s"} ↑</span>
              </div>
              <div class="famous-anc-epithet">${esc(fa.epithet)}</div>
              <div class="famous-anc-actions">
                <button type="button" class="famous-mini-btn trace-on-canvas-btn" data-stu="${p.id}" data-anc="${fa.id}">Trace Path on Canvas</button>
                <button type="button" class="famous-mini-btn toggle-chain-btn" data-target="chain-${p.id}-${fa.id}">Lineage Steps ▾</button>
                <button type="button" class="famous-mini-btn open-anc-tree-btn" data-pid="${fa.id}">View Tree →</button>
              </div>
              <div id="chain-${p.id}-${fa.id}" class="famous-chain-box hidden">${chainSteps}</div>
            </div>`;
        };

        return `
          <div class="famous-ancestors-card">
            <h4>
              <span>★ World-Famous Scientist Ancestors</span>
              <span style="font-weight:700;opacity:0.85;">${famObj.total_count || hl.length} found</span>
            </h4>
            <div class="famous-anc-list">
              ${hl.map(renderFamousItem).join("")}
            </div>
            ${
              more.length
                ? `<button type="button" id="toggleMoreFamousBtn" class="famous-mini-btn" style="width:100%;margin-top:8px;text-align:center;">Show ${more.length} more notable scientist ancestor${more.length === 1 ? "" : "s"} ▾</button>
                   <div id="moreFamousAncList" class="famous-anc-list hidden" style="margin-top:7px;">
                     ${more.map(renderFamousItem).join("")}
                   </div>`
                : ""
            }
          </div>`;
      })()}

      <div class="dossier-block">
        <h4>Direct PhD Students (${fmtNum((p.students || []).length)})</h4>
        ${renderCompactRows(p.students, "No doctoral students recorded.")}
      </div>

      <div class="dossier-block">
        <h4>Academic Siblings (${fmtNum(p.sibling_total || 0)} co-students)</h4>
        ${renderCompactRows(p.siblings, "No co-students recorded.")}
      </div>
    `;

    document.getElementById("recenterDossierBtn")?.addEventListener("click", () => {
      loadPersonTree(p.id);
    });

    document.getElementById("compareKinBtn")?.addEventListener("click", () => {
      state.kin1 = { id: p.id, name: p.name };
      document.getElementById("kinSelected1").textContent = `Selected: ${p.name}`;
      document.getElementById("kinInput1").value = p.name;
      switchView("kinship");
      runKinshipSearch();
    });

    document.getElementById("dossierSchoolLink")?.addEventListener("click", (e) => {
      e.preventDefault();
      applySchoolFilter(e.currentTarget.dataset.school);
      switchView("explore");
    });

    box.querySelectorAll(".person-row-btn[data-pid]").forEach((btn) => {
      btn.addEventListener("click", () => loadPersonTree(Number(btn.dataset.pid)));
    });

    // World-Famous Scientist Ancestors interactive handlers
    box.querySelectorAll(".trace-on-canvas-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const stuId = Number(btn.dataset.stu);
        const ancId = Number(btn.dataset.anc);
        if (stuId !== state.focalId) {
          await openPersonInTree(stuId, ancId);
        } else {
          await traceFamousAncestorOnCanvas(stuId, ancId);
        }
      });
    });

    box.querySelectorAll(".toggle-chain-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const target = document.getElementById(btn.dataset.target);
        if (target) {
          const isHidden = target.classList.toggle("hidden");
          btn.textContent = isHidden ? "Lineage Steps ▾" : "Hide Steps ▴";
        }
      });
    });

    box.querySelectorAll(".open-anc-tree-btn, .famous-anc-name, .chain-step-btn").forEach((el) => {
      el.addEventListener("click", () => {
        const pid = Number(el.dataset.pid);
        if (pid) loadPersonTree(pid);
      });
    });

    document.getElementById("toggleMoreFamousBtn")?.addEventListener("click", (e) => {
      const moreBox = document.getElementById("moreFamousAncList");
      if (moreBox) {
        const isHidden = moreBox.classList.toggle("hidden");
        const cnt = moreBox.querySelectorAll(".famous-anc-item").length;
        e.currentTarget.textContent = isHidden
          ? `Show ${cnt} more notable scientist ancestor${cnt === 1 ? "" : "s"} ▾`
          : `Hide extra notable scientist ancestors ▴`;
      }
    });
  }

  // -------------------------------------------------------------------------
  // D3.js Layered DAG Layout & Interactive Genealogy Renderer
  // -------------------------------------------------------------------------
  let currentZoomBehavior = null;
  let focalCoordsCache = { x: 0, y: 0 };

  function computeLayeredDagLayout(nodes, links, isHorizontal) {
    const CARD_W = 204;
    const CARD_H = 68;
    const GAP_MAIN = isHorizontal ? 86 : 68; // spacing between generations
    const GAP_CROSS = isHorizontal ? 18 : 22; // spacing within a generation

    const nodeById = new Map(nodes.map((n) => [n.id, { ...n }]));
    const parentsOf = new Map();
    const childrenOf = new Map();
    nodes.forEach((n) => {
      parentsOf.set(n.id, []);
      childrenOf.set(n.id, []);
    });

    links.forEach((l) => {
      const s = typeof l.source === "object" ? l.source.id : l.source;
      const t = typeof l.target === "object" ? l.target.id : l.target;
      if (nodeById.has(s) && nodeById.has(t)) {
        childrenOf.get(s).push(t);
        parentsOf.get(t).push(s);
      }
    });

    // Group nodes by generation offset (`gen_offset`)
    // Special smart Cohort Sub-Grid: if `gen_offset === 1` has > 16 leaf nodes (e.g. 60 direct students with down_depth=1),
    // split pure leaves into stacked rows of up to 12 so the tree stays compact and readable!
    const rawGenGroups = new Map();
    nodeById.forEach((n) => {
      const g = n.gen_offset || 0;
      if (!rawGenGroups.has(g)) rawGenGroups.set(g, []);
      rawGenGroups.get(g).push(n);
    });

    const genGroups = new Map();
    rawGenGroups.forEach((arr, g) => {
      if (g >= 1 && arr.length > 16 && arr.every((n) => (childrenOf.get(n.id) || []).length === 0)) {
        // Sort chronologically and wrap into sub-rows of 10
        arr.sort((a, b) => (a.year || 9999) - (b.year || 9999) || a.name.localeCompare(b.name));
        const perRow = 10;
        arr.forEach((n, idx) => {
          const virtualGen = g + Math.floor(idx / perRow) * 0.82;
          if (!genGroups.has(virtualGen)) genGroups.set(virtualGen, []);
          genGroups.get(virtualGen).push(n);
        });
      } else {
        genGroups.set(g, arr);
      }
    });

    const sortedGens = Array.from(genGroups.keys()).sort((a, b) => a - b);

    // Position focal row (gen 0) at coordinate 0
    const placeRow = (arr, centerTarget = 0) => {
      const step = (isHorizontal ? CARD_H : CARD_W) + GAP_CROSS;
      const totalSpan = (arr.length - 1) * step;
      const start = centerTarget - totalSpan / 2;
      arr.forEach((n, i) => {
        n._cross = start + i * step;
      });
    };

    const g0 = genGroups.get(0) || [];
    placeRow(g0, 0);

    // Sweep UPWARD (g < 0) ordered by average `_cross` of children
    const negGens = sortedGens.filter((g) => g < 0).sort((a, b) => b - a); // -1, -2, ...
    negGens.forEach((g) => {
      const arr = genGroups.get(g);
      arr.forEach((n) => {
        const ch = (childrenOf.get(n.id) || []).map((cid) => nodeById.get(cid)).filter((c) => c && c._cross !== undefined);
        n._bary = ch.length ? d3.mean(ch, (c) => c._cross) : 0;
      });
      arr.sort((a, b) => a._bary - b._bary || (a.year || 9999) - (b.year || 9999));
      const meanBary = d3.mean(arr, (n) => n._bary) || 0;
      placeRow(arr, meanBary);
    });

    // Sweep DOWNWARD (g > 0) ordered by average `_cross` of parents, then chronological year
    const posGens = sortedGens.filter((g) => g > 0).sort((a, b) => a - b); // +1, +2, ...
    posGens.forEach((g) => {
      const arr = genGroups.get(g);
      arr.forEach((n) => {
        const pa = (parentsOf.get(n.id) || []).map((pid) => nodeById.get(pid)).filter((p) => p && p._cross !== undefined);
        n._bary = pa.length ? d3.mean(pa, (p) => p._cross) : 0;
      });
      arr.sort((a, b) => a._bary - b._bary || (a.year || 9999) - (b.year || 9999) || a.name.localeCompare(b.name));
      const meanBary = d3.mean(arr, (n) => n._bary) || 0;
      placeRow(arr, meanBary);
    });

    // Assign final (x, y) center coordinates
    const mainStep = (isHorizontal ? CARD_W : CARD_H) + GAP_MAIN;
    nodeById.forEach((n) => {
      // Find which virtual/actual generation bucket n was placed into
    });
    sortedGens.forEach((g) => {
      const arr = genGroups.get(g);
      const mainCoord = g * mainStep;
      arr.forEach((n) => {
        if (isHorizontal) {
          n.x = mainCoord;
          n.y = n._cross;
        } else {
          n.x = n._cross;
          n.y = mainCoord;
        }
      });
    });

    const laidOutNodes = Array.from(nodeById.values());
    const laidOutLinks = links
      .map((l) => {
        const sId = typeof l.source === "object" ? l.source.id : l.source;
        const tId = typeof l.target === "object" ? l.target.id : l.target;
        return {
          source: nodeById.get(sId),
          target: nodeById.get(tId),
          edge_source: l.edge_source,
        };
      })
      .filter((l) => l.source && l.target);

    return { nodes: laidOutNodes, links: laidOutLinks, CARD_W, CARD_H, parentsOf, childrenOf };
  }

  function renderGenealogyGraph(svgSelector, graphData, recenterOnFocal = true) {
    const svgEl = document.querySelector(svgSelector);
    if (!svgEl || !graphData || !graphData.nodes) return;

    const isHorizontal = svgSelector === "#treeSvg" && state.layoutDir === "horizontal";
    const { nodes, links, CARD_W, CARD_H, parentsOf, childrenOf } = computeLayeredDagLayout(
      graphData.nodes,
      graphData.links,
      isHorizontal
    );

    const svg = d3.select(svgEl);
    const prevTransform = d3.zoomTransform(svgEl);
    svg.selectAll("*").remove();

    const width = svgEl.clientWidth || 900;
    const height = svgEl.clientHeight || 620;

    // Arrowhead marker definition
    const defs = svg.append("defs");
    defs
      .append("marker")
      .attr("id", `arrow-${svgSelector.slice(1)}`)
      .attr("viewBox", "0 -5 10 10")
      .attr("refX", 8)
      .attr("refY", 0)
      .attr("markerWidth", 6)
      .attr("markerHeight", 6)
      .attr("orient", "auto")
      .append("path")
      .attr("d", "M0,-4.5L10,0L0,4.5")
      .attr("fill", "var(--edge-stroke)");

    const gRoot = svg.append("g").attr("class", "zoom-layer");

    const zoom = d3
      .zoom()
      .scaleExtent([0.12, 2.5])
      .on("zoom", (event) => {
        gRoot.attr("transform", event.transform);
      });
    svg.call(zoom).on("dblclick.zoom", null);

    if (svgSelector === "#treeSvg") {
      currentZoomBehavior = zoom;
    }

    // Cubic Bézier Links
    const linkPath = (d) => {
      if (isHorizontal) {
        const sx = d.source.x + CARD_W / 2;
        const sy = d.source.y;
        const tx = d.target.x - CARD_W / 2;
        const ty = d.target.y;
        const mx = (sx + tx) / 2;
        return `M${sx},${sy} C${mx},${sy} ${mx},${ty} ${tx},${ty}`;
      } else {
        const sx = d.source.x;
        const sy = d.source.y + CARD_H / 2;
        const tx = d.target.x;
        const ty = d.target.y - CARD_H / 2;
        const my = (sy + ty) / 2;
        return `M${sx},${sy} C${sx},${my} ${tx},${my} ${tx},${ty}`;
      }
    };

    const linkSel = gRoot
      .append("g")
      .attr("class", "links-group")
      .selectAll("path")
      .data(links)
      .join("path")
      .attr("class", "tree-link")
      .attr("d", linkPath)
      .attr("marker-end", `url(#arrow-${svgSelector.slice(1)})`);

    // Scholar Node Cards
    const nodeSel = gRoot
      .append("g")
      .attr("class", "nodes-group")
      .selectAll("g")
      .data(nodes, (d) => d.id)
      .join("g")
      .attr(
        "class",
        (d) =>
          `tree-node ${d.role === "focal" || d.role === "mrca" ? "focal" : ""} ${d.id === state.selectedDossierId ? "selected" : ""}`
      )
      .attr("transform", (d) => `translate(${d.x - CARD_W / 2},${d.y - CARD_H / 2})`);

    // Main Card Rounded Rect
    nodeSel
      .append("rect")
      .attr("class", "node-rect")
      .attr("width", CARD_W)
      .attr("height", CARD_H)
      .attr("rx", 9)
      .attr("ry", 9);

    // Left Subfield Color Stripe
    nodeSel
      .append("rect")
      .attr("x", 0)
      .attr("y", 0)
      .attr("width", 5.5)
      .attr("height", CARD_H)
      .attr("rx", 3)
      .attr("fill", (d) => FIELD_COLORS[d.primary_field] || FIELD_COLORS["Mathematics & Other"]);

    // Role Badge for Focal / MRCA
    const badgeNodes = nodeSel.filter((d) => d.role === "focal" || d.role === "mrca");
    badgeNodes
      .append("rect")
      .attr("x", CARD_W - 64)
      .attr("y", -9)
      .attr("width", 58)
      .attr("height", 16)
      .attr("rx", 8)
      .attr("fill", (d) => (d.role === "mrca" ? "#0ca678" : "var(--accent)"));

    badgeNodes
      .append("text")
      .attr("x", CARD_W - 35)
      .attr("y", 2)
      .attr("text-anchor", "middle")
      .attr("fill", "#ffffff")
      .attr("font-size", "8.5px")
      .attr("font-weight", "800")
      .text((d) => (d.role === "mrca" ? "COMMON ROOT" : "FOCAL"));

    // Researcher Name (with gold ★ prefix for World-Famous Scientists)
    const nameText = nodeSel
      .append("text")
      .attr("x", 13)
      .attr("y", 21)
      .attr("fill", "var(--text-primary)")
      .attr("font-size", "12.2px")
      .attr("font-weight", "700");

    nameText
      .filter((d) => Boolean(d.famous_self))
      .append("tspan")
      .attr("fill", "var(--gold-main)")
      .text("★ ");

    nameText
      .append("tspan")
      .text((d) => trunc(d.name, d.famous_self ? 22 : 24));

    // School + Graduation Year
    nodeSel
      .append("text")
      .attr("x", 13)
      .attr("y", 38)
      .attr("fill", "var(--text-secondary)")
      .attr("font-size", "10.5px")
      .text((d) => {
        const yr = d.year ? String(d.year) : "n/a";
        const sch = d.school ? trunc(d.school, 21) : "School n/a";
        return `${yr} · ${sch}`;
      });

    // Bottom Field + Descendants Micro-Row
    nodeSel
      .append("text")
      .attr("x", 13)
      .attr("y", 56)
      .attr("fill", (d) => FIELD_COLORS[d.primary_field] || "var(--text-muted)")
      .attr("font-size", "9.8px")
      .attr("font-weight", "700")
      .text((d) => {
        const shortField =
          d.primary_field === "Related Quantitative"
            ? "Related"
            : d.primary_field === "Mathematics & Other"
            ? "Math/Root"
            : d.primary_field;
        const extra = d.hidden_student_count ? ` (+${d.hidden_student_count})` : "";
        return `${shortField} · 🎓${d.student_count} · 🌳${fmtNum(d.descendant_count)}${extra}`;
      });

    // Hover Lineage Highlighting & Rich Tooltip
    const tooltip = document.getElementById("treeTooltip");

    nodeSel
      .on("mouseenter", function (event, d) {
        // Compute connected lineage chain (ancestors + descendants of hovered node `d`)
        const activeIds = new Set([d.id]);
        const qUp = [d.id];
        while (qUp.length) {
          const curr = qUp.pop();
          for (const p of parentsOf.get(curr) || []) {
            if (!activeIds.has(p)) {
              activeIds.add(p);
              qUp.push(p);
            }
          }
        }
        const qDn = [d.id];
        while (qDn.length) {
          const curr = qDn.pop();
          for (const c of childrenOf.get(curr) || []) {
            if (!activeIds.has(c)) {
              activeIds.add(c);
              qDn.push(c);
            }
          }
        }

        nodeSel.classed("dimmed", (n) => !activeIds.has(n.id));
        linkSel
          .classed("highlighted", (l) => activeIds.has(l.source.id) && activeIds.has(l.target.id))
          .classed("dimmed", (l) => !(activeIds.has(l.source.id) && activeIds.has(l.target.id)));

        if (svgSelector === "#treeSvg" && tooltip) {
          const famTip = d.famous_self
            ? `<div style="color:var(--gold-main);font-weight:700;margin-bottom:4px;">★ World-Famous Scientist · ${esc(d.famous_self.epithet)}</div>`
            : "";
          tooltip.innerHTML = `
            <div style="font-weight:800;font-size:13px;margin-bottom:2px;">${esc(d.famous_self ? d.famous_self.display_name : d.name)}</div>
            ${famTip}
            <div style="color:var(--text-secondary);margin-bottom:5px;">
              ${esc(d.school || "Institution unlisted")} ${d.year ? `(${d.year})` : ""} · <strong>${esc(d.primary_field)}</strong>
            </div>
            ${d.thesis ? `<div style="font-style:italic;margin-bottom:5px;color:var(--text-primary);">“${esc(trunc(d.thesis, 130))}”</div>` : ""}
            <div style="font-size:11px;color:var(--text-muted);">
              🎓 ${fmtNum(d.student_count)} students · 🌳 ${fmtNum(d.descendant_count)} descendants · Sources: ${esc((d.sources || []).join(", "))}
              <br/><span style="color:var(--accent);font-weight:600;">Click to inspect · Double-click to re-center tree</span>
            </div>
          `;
          tooltip.classList.remove("hidden");
        }
      })
      .on("mousemove", function (event) {
        if (svgSelector === "#treeSvg" && tooltip) {
          const rect = document.getElementById("treeCanvasContainer").getBoundingClientRect();
          const left = Math.min(rect.width - 320, event.clientX - rect.left + 16);
          const top = Math.max(10, event.clientY - rect.top - 16);
          tooltip.style.left = `${left}px`;
          tooltip.style.top = `${top}px`;
        }
      })
      .on("mouseleave", function () {
        nodeSel.classed("dimmed", false);
        linkSel.classed("highlighted", false).classed("dimmed", false);
        tooltip?.classList.add("hidden");
      })
      .on("click", function (event, d) {
        event.stopPropagation();
        if (svgSelector === "#treeSvg") {
          loadDossierOnly(d.id);
        } else {
          openPersonInTree(d.id);
        }
      })
      .on("dblclick", function (event, d) {
        event.stopPropagation();
        openPersonInTree(d.id);
      });

    // Position viewport cleanly
    const focalNode = nodes.find((n) => n.role === "focal" || n.role === "mrca") || nodes[0];
    if (focalNode && svgSelector === "#treeSvg") {
      focalCoordsCache = { x: focalNode.x, y: focalNode.y };
    }

    const defaultScale = width < 640 ? 0.72 : 0.92;
    if (recenterOnFocal && focalNode) {
      if (svgSelector === "#kinshipSvg" || nodes.length <= 28) {
        fitGraphToViewport(svg, zoom, nodes, CARD_W, CARD_H, width, height);
      } else {
        // Center smoothly on Focal Scholar at comfortable reading zoom
        const tx = width / 2 - focalNode.x * defaultScale;
        const ty = height * 0.46 - focalNode.y * defaultScale;
        svg.call(zoom.transform, d3.zoomIdentity.translate(tx, ty).scale(defaultScale));
      }
    } else if (prevTransform && (prevTransform.x !== 0 || prevTransform.y !== 0 || prevTransform.k !== 1)) {
      svg.call(zoom.transform, prevTransform);
    } else if (focalNode) {
      const tx = width / 2 - focalNode.x * defaultScale;
      const ty = height * 0.46 - focalNode.y * defaultScale;
      svg.call(zoom.transform, d3.zoomIdentity.translate(tx, ty).scale(defaultScale));
    }
  }

  function fitGraphToViewport(svg, zoom, nodes, cardW, cardH, width, height) {
    if (!nodes.length) return;
    const minX = d3.min(nodes, (n) => n.x - cardW / 2) - 40;
    const maxX = d3.max(nodes, (n) => n.x + cardW / 2) + 40;
    const minY = d3.min(nodes, (n) => n.y - cardH / 2) - 40;
    const maxY = d3.max(nodes, (n) => n.y + cardH / 2) + 40;
    const bw = Math.max(1, maxX - minX);
    const bh = Math.max(1, maxY - minY);
    const scale = Math.min(1.05, Math.max(0.16, 0.92 * Math.min(width / bw, height / bh)));
    const cx = (minX + maxX) / 2;
    const cy = (minY + maxY) / 2;
    svg
      .transition()
      .duration(420)
      .call(zoom.transform, d3.zoomIdentity.translate(width / 2 - cx * scale, height / 2 - cy * scale).scale(scale));
  }

  // -------------------------------------------------------------------------
  // Academic Kinship / Common Ancestor Finder
  // -------------------------------------------------------------------------
  async function runKinshipSearch() {
    const banner = document.getElementById("kinshipResultBanner");
    if (!state.kin1?.id || !state.kin2?.id) {
      banner.textContent = "Please select two researchers to trace their shared academic lineage.";
      banner.classList.remove("hidden");
      return;
    }
    banner.textContent = "Tracing ancestral lineages up to 25 generations…";
    banner.classList.remove("hidden");

    try {
      const resp = await fetch(`/api/connection?id1=${state.kin1.id}&id2=${state.kin2.id}`);
      const data = await resp.json();
      if (!data.connected) {
        banner.textContent = data.message || data.error || "No shared academic ancestor found.";
        d3.select("#kinshipSvg").selectAll("*").remove();
        return;
      }
      const m = data.mrca;
      banner.innerHTML = `✨ <strong>${esc(data.relationship)}</strong> — Most Recent Common Ancestor: <u>${esc(m.name)}</u> (${esc(m.school || "Historical")}${m.year ? ", " + m.year : ""}). <em>Click any card to open their full family tree!</em>`;
      renderGenealogyGraph("#kinshipSvg", { nodes: data.nodes, links: data.links }, true);
    } catch (err) {
      banner.textContent = "Failed to compute kinship path.";
      console.error(err);
    }
  }

  // -------------------------------------------------------------------------
  // Autocomplete Dropdown Helpers (School & Person Quick Pickers)
  // -------------------------------------------------------------------------
  function setupSchoolAutocomplete() {
    const inp = document.getElementById("schoolFilterInput");
    const drop = document.getElementById("schoolDropdown");
    const clearBtn = document.getElementById("clearSchoolBtn");

    const fetchSchools = debounce(async (val) => {
      const resp = await fetch(`/api/schools?q=${encodeURIComponent(val)}&scope=${state.scope}&limit=25`);
      const data = await resp.json();
      const schools = data.schools || [];
      if (!schools.length) {
        drop.classList.add("hidden");
        return;
      }
      drop.innerHTML = schools
        .map(
          (s) => `
          <div class="ac-item" data-school="${esc(s.school)}">
            <div>
              <div class="ac-main">${esc(s.school)}</div>
              <div class="ac-sub">${esc(s.country || "")}${s.aliases ? " · " + esc(s.aliases) : ""}</div>
            </div>
            <span class="ac-count">${fmtNum(s.core_stat_count)} Stat · ${fmtNum(s.total_phd_count)} All</span>
          </div>`
        )
        .join("");
      drop.classList.remove("hidden");
      drop.querySelectorAll(".ac-item").forEach((el) => {
        el.addEventListener("click", () => applySchoolFilter(el.dataset.school));
      });
    }, 180);

    inp.addEventListener("focus", () => fetchSchools(inp.value.trim()));
    inp.addEventListener("input", () => {
      clearBtn.classList.toggle("hidden", !inp.value);
      fetchSchools(inp.value.trim());
    });
    inp.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        drop.classList.add("hidden");
        applySchoolFilter(inp.value.trim());
      }
    });
    clearBtn.addEventListener("click", () => applySchoolFilter(""));
  }

  function setupPersonQuickPicker(inputId, dropId, onSelect) {
    const inp = document.getElementById(inputId);
    const drop = document.getElementById(dropId);

    const doLookup = debounce(async () => {
      const q = inp.value.trim();
      if (q.length < 2) {
        drop.classList.add("hidden");
        return;
      }
      const resp = await fetch(`/api/search?q=${encodeURIComponent(q)}&scope=all&per_page=12`);
      const data = await resp.json();
      const rows = data.results || [];
      if (!rows.length) {
        drop.classList.add("hidden");
        return;
      }
      drop.innerHTML = rows
        .map(
          (p) => `
          <div class="ac-item" data-pid="${p.id}" data-pname="${esc(p.name)}">
            <div>
              <div class="ac-main">${esc(p.name)}</div>
              <div class="ac-sub">${esc(p.school || "School n/a")}${p.year ? " · " + p.year : ""} · ${esc(p.primary_field)}</div>
            </div>
            <span class="ac-count">🌳 ${fmtNum(p.descendant_count)}</span>
          </div>`
        )
        .join("");
      drop.classList.remove("hidden");
      drop.querySelectorAll(".ac-item").forEach((el) => {
        el.addEventListener("click", () => {
          drop.classList.add("hidden");
          onSelect(Number(el.dataset.pid), el.dataset.pname);
        });
      });
    }, 200);

    inp.addEventListener("input", doLookup);
  }

  // -------------------------------------------------------------------------
  // Export SVG
  // -------------------------------------------------------------------------
  function exportTreeSvg() {
    const svgEl = document.getElementById("treeSvg");
    if (!svgEl) return;
    const clone = svgEl.cloneNode(true);
    clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
    const styleEl = document.createElementNS("http://www.w3.org/2000/svg", "style");
    styleEl.textContent = `
      text { font-family: Inter, -apple-system, BlinkMacSystemFont, sans-serif; }
      .tree-link { fill: none; stroke: #94a3b8; stroke-width: 1.65px; opacity: 0.75; }
      .node-rect { fill: #ffffff; stroke: #cbd5e1; stroke-width: 1.4px; }
      .focal .node-rect { stroke: #3b5bdb; stroke-width: 2.8px; }
    `;
    clone.prepend(styleEl);
    const xml = new XMLSerializer().serializeToString(clone);
    const blob = new Blob([xml], { type: "image/svg+xml;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `academic_tree_${state.focalId}.svg`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }

  // -------------------------------------------------------------------------
  // Event Listeners & Bootstrapping
  // -------------------------------------------------------------------------
  function bindEvents() {
    // Top tabs & brand logo
    document.querySelectorAll(".nav-tab").forEach((btn) => {
      btn.addEventListener("click", () => switchView(btn.dataset.view));
    });
    document.getElementById("brandHomeBtn").addEventListener("click", () => {
      switchView("explore");
      resetToSearchHome();
    });

    // Main Search Input & Google-Style Front-Page Actions
    const mainInp = document.getElementById("mainSearchInput");
    const clearSearchBtn = document.getElementById("clearSearchBtn");
    const triggerMainSearch = debounce(() => {
      state.query = mainInp.value.trim();
      state.page = 1;
      clearSearchBtn.classList.toggle("hidden", !state.query);
      executeSearch();
    }, 220);

    mainInp.addEventListener("input", triggerMainSearch);
    mainInp.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        state.query = mainInp.value.trim();
        if (!state.query) state.browseMode = true;
        state.page = 1;
        clearSearchBtn.classList.toggle("hidden", !state.query);
        executeSearch();
      }
    });
    clearSearchBtn.addEventListener("click", () => {
      mainInp.value = "";
      state.query = "";
      state.page = 1;
      clearSearchBtn.classList.add("hidden");
      executeSearch();
      mainInp.focus();
    });

    document.getElementById("feelingLuckyBtn")?.addEventListener("click", async (e) => {
      const btn = e.currentTarget;
      btn.disabled = true;
      try {
        const resp = await fetch("/api/random_person");
        const person = await resp.json();
        if (person && person.id) {
          openPersonInTree(person.id);
        }
      } catch (err) {
        console.error("Lucky jump failed:", err);
      } finally {
        btn.disabled = false;
      }
    });
    document.getElementById("openFiltersHeroBtn")?.addEventListener("click", () => toggleMobileFilters());
    document.getElementById("backToSearchHomeBtn")?.addEventListener("click", () => resetToSearchHome());

    // Scope buttons
    document.querySelectorAll(".scope-pill").forEach((btn) => {
      btn.addEventListener("click", () => setScope(btn.dataset.scope));
    });

    // School Role chips (Grads + Faculty | PhD Alumni | Faculty / Advisors)
    document.querySelectorAll("#schoolRoleGroup .era-chip").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.schoolRole = btn.dataset.srole;
        document.querySelectorAll("#schoolRoleGroup .era-chip").forEach((c) => {
          c.classList.toggle("active", c.dataset.srole === state.schoolRole);
        });
        if (state.school) {
          state.page = 1;
          executeSearch();
        }
      });
    });

    document.getElementById("masterSchoolSearchInput")?.addEventListener("input", (e) => {
      renderMasterSchoolsGrid(e.target.value);
    });

    // Year Range & Era Chips
    const yminInp = document.getElementById("yearMinInput");
    const ymaxInp = document.getElementById("yearMaxInput");
    const onYearChange = debounce(() => {
      state.yearMin = yminInp.value.trim();
      state.yearMax = ymaxInp.value.trim();
      state.page = 1;
      document.querySelectorAll(".era-chip[data-ymin]").forEach((c) => c.classList.remove("active"));
      executeSearch();
    }, 300);
    yminInp.addEventListener("input", onYearChange);
    ymaxInp.addEventListener("input", onYearChange);

    document.querySelectorAll(".era-chip[data-ymin]").forEach((chip) => {
      chip.addEventListener("click", () => {
        const isAlready = chip.classList.contains("active");
        document.querySelectorAll(".era-chip[data-ymin]").forEach((c) => c.classList.remove("active"));
        if (isAlready) {
          yminInp.value = "";
          ymaxInp.value = "";
          state.yearMin = "";
          state.yearMax = "";
        } else {
          chip.classList.add("active");
          yminInp.value = chip.dataset.ymin;
          ymaxInp.value = chip.dataset.ymax;
          state.yearMin = chip.dataset.ymin;
          state.yearMax = chip.dataset.ymax;
        }
        state.page = 1;
        executeSearch();
      });
    });

    // Sort Select
    document.getElementById("sortSelect").addEventListener("change", (e) => {
      state.sort = e.target.value;
      state.browseMode = true;
      state.page = 1;
      executeSearch();
    });

    // Reset All Filters
    document.getElementById("resetFiltersBtn").addEventListener("click", () => {
      resetToSearchHome();
    });

    // Close dropdowns when clicking outside
    document.addEventListener("click", (e) => {
      if (!e.target.closest(".autocomplete-wrap") && !e.target.closest(".tree-quick-jump")) {
        document.querySelectorAll(".autocomplete-dropdown").forEach((d) => d.classList.add("hidden"));
      }
    });

    // Tree Toolbar Controls
    document.getElementById("ancMinusBtn").addEventListener("click", () => {
      if (state.upDepth > 1) {
        state.upDepth--;
        document.getElementById("ancDepthVal").textContent = state.upDepth;
        loadPersonTree(state.focalId);
      }
    });
    document.getElementById("ancPlusBtn").addEventListener("click", () => {
      if (state.upDepth < 45) {
        state.upDepth++;
        document.getElementById("ancDepthVal").textContent = state.upDepth;
        loadPersonTree(state.focalId);
      }
    });
    document.getElementById("ancAllRootsBtn").addEventListener("click", () => {
      state.upDepth = 45;
      document.getElementById("ancDepthVal").textContent = "All";
      loadPersonTree(state.focalId);
    });

    document.getElementById("descMinusBtn").addEventListener("click", () => {
      if (state.downDepth > 0) {
        state.downDepth--;
        document.getElementById("descDepthVal").textContent = state.downDepth;
        loadPersonTree(state.focalId);
      }
    });
    document.getElementById("descPlusBtn").addEventListener("click", () => {
      if (state.downDepth < 4) {
        state.downDepth++;
        document.getElementById("descDepthVal").textContent = state.downDepth;
        loadPersonTree(state.focalId);
      }
    });

    document.getElementById("exitTraceModeBtn")?.addEventListener("click", () => {
      if (state.fullTreeBackup) {
        state.activeTraceAncestorId = null;
        state.treeData = state.fullTreeBackup;
        document.getElementById("exitTraceModeBtn")?.classList.add("hidden");
        renderGenealogyGraph("#treeSvg", state.treeData, true);
        updateTreeStatsBar(state.treeData);
      }
    });

    document.getElementById("toggleLayoutDirBtn").addEventListener("click", () => {
      state.layoutDir = state.layoutDir === "vertical" ? "horizontal" : "vertical";
      document.getElementById("layoutDirLabel").textContent =
        state.layoutDir === "vertical" ? "Vertical ↕" : "Horizontal ↔";
      if (state.treeData) renderGenealogyGraph("#treeSvg", state.treeData, true);
    });

    document.getElementById("centerFocalBtn").addEventListener("click", () => {
      const svgEl = document.getElementById("treeSvg");
      if (!svgEl || !currentZoomBehavior) return;
      const w = svgEl.clientWidth || 900;
      const h = svgEl.clientHeight || 620;
      const scale = w < 640 ? 0.72 : 0.95;
      d3.select(svgEl)
        .transition()
        .duration(380)
        .call(
          currentZoomBehavior.transform,
          d3.zoomIdentity.translate(w / 2 - focalCoordsCache.x * scale, h * 0.45 - focalCoordsCache.y * scale).scale(scale)
        );
    });

    document.getElementById("fitTreeBtn").addEventListener("click", () => {
      if (state.treeData) {
        const svgEl = document.getElementById("treeSvg");
        const isH = state.layoutDir === "horizontal";
        const { nodes, CARD_W, CARD_H } = computeLayeredDagLayout(state.treeData.nodes, state.treeData.links, isH);
        fitGraphToViewport(
          d3.select(svgEl),
          currentZoomBehavior,
          nodes,
          CARD_W,
          CARD_H,
          svgEl.clientWidth || 900,
          svgEl.clientHeight || 620
        );
      }
    });

    document.getElementById("exportSvgBtn").addEventListener("click", exportTreeSvg);

    // Quick pickers
    setupSchoolAutocomplete();
    setupPersonQuickPicker("treeQuickSearchInput", "treeQuickDropdown", (pid) => {
      document.getElementById("treeQuickSearchInput").value = "";
      loadPersonTree(pid);
    });
    setupPersonQuickPicker("kinInput1", "kinDrop1", (pid, pname) => {
      state.kin1 = { id: pid, name: pname };
      document.getElementById("kinInput1").value = pname;
      document.getElementById("kinSelected1").textContent = `Selected: ${pname}`;
    });
    setupPersonQuickPicker("kinInput2", "kinDrop2", (pid, pname) => {
      state.kin2 = { id: pid, name: pname };
      document.getElementById("kinInput2").value = pname;
      document.getElementById("kinSelected2").textContent = `Selected: ${pname}`;
    });

    document.getElementById("findKinshipBtn").addEventListener("click", runKinshipSearch);
    document.querySelectorAll(".kin-preset").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.kin1 = { id: Number(btn.dataset.id1), name: btn.dataset.n1 };
        state.kin2 = { id: Number(btn.dataset.id2), name: btn.dataset.n2 };
        document.getElementById("kinInput1").value = btn.dataset.n1;
        document.getElementById("kinInput2").value = btn.dataset.n2;
        document.getElementById("kinSelected1").textContent = `Selected: ${btn.dataset.n1}`;
        document.getElementById("kinSelected2").textContent = `Selected: ${btn.dataset.n2}`;
        runKinshipSearch();
      });
    });

    // Inline Filters Panel & Mobile Dossier Bottom-Sheet Controls
    document.getElementById("mobileCloseFiltersBtn")?.addEventListener("click", () => {
      state.browseMode = true;
      toggleMobileFilters(false);
      executeSearch();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") toggleMobileFilters(false);
    });

    const dossierPanel = document.getElementById("dossierPanel");
    const expBtn = document.getElementById("mobileDossierExpandBtn");
    const minBtn = document.getElementById("mobileDossierMinBtn");

    expBtn?.addEventListener("click", () => {
      if (!dossierPanel) return;
      dossierPanel.classList.remove("mobile-minimized");
      const isExp = dossierPanel.classList.toggle("mobile-expanded");
      expBtn.textContent = isExp ? "Split ▾" : "Expand ▴";
      if (minBtn) minBtn.textContent = "Hide ▾";
    });

    minBtn?.addEventListener("click", () => {
      if (!dossierPanel) return;
      dossierPanel.classList.remove("mobile-expanded");
      if (expBtn) expBtn.textContent = "Expand ▴";
      const isMin = dossierPanel.classList.toggle("mobile-minimized");
      minBtn.textContent = isMin ? "Show ▴" : "Hide ▾";
    });

    // Responsive redraw on orientation change / resize
    window.addEventListener(
      "resize",
      debounce(() => {
        if (state.currentView === "sources" && state.globalStats) {
          renderDecadeChart(state.globalStats.summary.year_histogram || state.globalStats.summary.decade_histogram);
        }
      }, 220)
    );
  }

  // Boot!
  document.addEventListener("DOMContentLoaded", () => {
    initTheme();
    bindEvents();
    loadGlobalStats();

    document.getElementById("kinInput1").value = state.kin1.name;
    document.getElementById("kinSelected1").textContent = `Selected: ${state.kin1.name}`;
    document.getElementById("kinInput2").value = state.kin2.name;
    document.getElementById("kinSelected2").textContent = `Selected: ${state.kin2.name}`;

    // Check URL hash for deep link (#person=14941)
    const hashMatch = window.location.hash.match(/person=(\d+)/);
    if (hashMatch) {
      executeSearch();
      openPersonInTree(Number(hashMatch[1]));
    } else {
      executeSearch();
    }
  });
})();
