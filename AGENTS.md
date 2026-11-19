# StatLineage Workspace Rules

- **Keep the Ground-Zero Blueprint Skill Synchronized**: Whenever you modify any frontend UI (`static/index.html`, `static/styles.css`, `static/app.js`), backend route (`app.py`, `famous_ancestors.py`), database schema, or ETL script (`scripts/*.py`) in this workspace, you **must** update `.agents/skills/statlineage-blueprint/SKILL.md` and its companion files in `.agents/skills/statlineage-blueprint/references/` (`backend_and_frontend_spec.md` and/or `data_sources_and_etl_pipeline.md`) in the same turn so the blueprint remains 100% accurate and reproducible from ground zero.
- **Read-Only Web UI**: Keep `app.py` and the browser UI strictly read-only (`GET` endpoints only; no browser-triggered live web scraping or SQLite mutation routes).
