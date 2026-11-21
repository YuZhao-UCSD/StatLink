# StatLink Workspace Rules

- **Keep Both Workspace Skills 100% Synchronized**: Whenever you modify any frontend UI (`static/index.html`, `static/styles.css`, `static/app.js`), backend route (`app.py`, `famous_ancestors.py`), database schema, crawler (`crawler_engine.py`), or ETL/refresh script (`scripts/*.py`) in this workspace, you **must** update **both** skills in the same turn so ground-zero reproduction and incremental data refreshes never drift from the live codebase:
  1. `.agents/skills/statlineage-blueprint/SKILL.md` and its companion files in `.agents/skills/statlineage-blueprint/references/` (`backend_and_frontend_spec.md` and/or `data_sources_and_etl_pipeline.md`).
  2. `.agents/skills/statlink-data-refresh/SKILL.md` and its companion file `.agents/skills/statlink-data-refresh/references/source_catalog_and_crawlers.md` (whenever data sources, crawlers, enrichers, year bounds, caches, or database finalization steps change).
- **Read-Only Web UI**: Keep `app.py` and the browser UI strictly read-only (`GET` endpoints only; no browser-triggered live web scraping or SQLite mutation routes).
