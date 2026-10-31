# AGENT.md

## Structure
- `src/db.py`: SQLite schema (`repositories` + `snapshots`), WAL mode, composite PK (`timeframe, period_key, language_filter, repository_full_name`), `idx_snapshots_export` index, slice replacement (`replace_snapshot_slice()`), dropout pruning.
- `src/extractor.py`: RSC Flight stream parser. `SUPPORTED_LANGUAGES` defines 15 languages + overall `all` (16 filters). Derives timeframe/period from payload fields (`year`, `week`, `month`, `date`).
- `src/export_json.py`: Compiles SQLite into `data/{timeframe}/{timeframe}-{lang}.json` plus `data/index.json`. Swap via `.tmp` / `.old` so a crash never leaves `data/` missing.
- `src/backfill.py`: Async parallel pipeline (`httpx` + `asyncio.Semaphore(10)`) backfilling 2,768 endpoints across 173 periods × 16 language filters.
- `src/sync_daily.py`: Daily updater fetching 64 target URLs (4 core endpoints × 16 language filters). Semaphore(10), shared connection, retry transport.
- `.github/workflows/daily_sync.yml`: Cron at 20:17 UTC (primary, end-of-day peak) and 23:43 UTC (pre-midnight backup) → download DB release → sync → commit JSON → upload DB to release asset.
- `trendshift.db`: Master SQLite database containing historical and daily snapshots across unique repositories.
- `data/`: 64 static JSON API files (~37 MB total). Data split by timeframe×language only.

## Non-Obvious Discoveries
- Trendshift provides separate top-25 rankings per programming language via `?language={Lang}` query params for 15 languages: `C`, `C#`, `C++`, `Dart`, `Go`, `Java`, `JavaScript`, `Kotlin`, `PHP`, `Python`, `Ruby`, `Rust`, `Swift`, `TypeScript`, `Zig`.
- `language_filter` distinguishes overall top-25 (`"all"`) from dedicated language top-25 rankings (`"Python"`, `"Rust"`, etc.).
- RSC requests 307-redirect to `?_rsc`; `httpx.AsyncClient` MUST use `follow_redirects=True`.
- Never interpolate `?language={lang}`. Use `ranking_url()` (`urlencode`). Raw `#` is a fragment; `+` is a space. Live: `?language=C#` → C, `C%23` → C#; `?language=C++` → overall, `C%2B%2B` → C++.
- Payload items contain `year`, `week`, `month`, and `date` fields. Use these to derive `period_key` instead of URL path.
- Payload dictionary keys can contain explicit `null` values; always use `item.get(k) is not None` and `(item.get(...) or [])`.
- `idx_snapshots_export` index on `(timeframe, language_filter, period_key DESC, rank ASC)` serves static API queries as pure covering index scans with 0 temp b-tree sorts.
- GitHub Pages is enabled on `main` root (`https://quantavil.github.io/trendshift/`), serving `data/` with CORS (`Access-Control-Allow-Origin: *`) for external client consumption.

## Blunders
- **Incomplete per-language rankings**: Originally filtered overall top-25 repos client-side by language. Fixed by requesting `?language={Lang}` endpoints directly.
- **Ghost dropouts in open periods**: `upsert_snapshot()` never evicted repos that fell out of top 25 during open period re-syncs, causing slices to grow to 40+ rows. Fixed by replacing slices before inserting (`replace_snapshot_slice()`) and pruning existing DB ghost rows (`prune_ghost_dropouts()`).
- **Fail-open CI release download**: `continue-on-error: true` allowed blank DB creation if release download failed. Fixed with explicit `gh release view` check and `if: success()` upload guard.
- **Non-atomic export**: Direct `shutil.rmtree(data/)` then rebuild meant crashes lost files. Fixed with `.tmp` dir and atomic rename.
