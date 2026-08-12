# Trendshift Historical Data Pipeline & Static API (2024 – Present)

An automated data collection pipeline and static JSON API for [Trendshift.io](https://trendshift.io) trending GitHub repositories from **January 2024 to present**.

Captures **69,200 total snapshots** across **7,634 unique GitHub repositories**, tracking both overall top-25 rankings and dedicated top-25 rankings for 15 programming languages across 173 historical periods.

---

## Features

- **Complete Historical Coverage**: Backfills 173 time periods (137 weeks, 32 months, 3 years) across 16 language filters = **2,768 target requests**.
- **15 Dedicated Language Rankings**: Fetches complete 25-repo rankings for `C`, `C#`, `C++`, `Dart`, `Go`, `Java`, `JavaScript`, `Kotlin`, `PHP`, `Python`, `Ruby`, `Rust`, `Swift`, `TypeScript`, and `Zig`.
- **Dual Storage**:
  - **SQLite** (`trendshift.db`): Relational database (69.2k snapshots) uploaded to GitHub Releases (`db-latest`).
  - **Static JSON API** (`data/`): 64 language-segmented JSON files organized by timeframe.
- **Automated Daily Sync**: GitHub Actions workflow at `20:17 UTC` (primary) and `23:43 UTC` (backup) via `uv` + `httpx`.

---

## Directory Structure

```
trendshift/
├── .github/workflows/
│   └── daily_sync.yml            # Cron workflow (20:17 UTC primary, 23:43 UTC backup)
├── data/
│   ├── daily/
│   │   ├── daily-all.json        # Overall daily top 25
│   │   └── daily-{language}.json # Dedicated top 25 per language (15 files)
│   ├── weekly/
│   │   ├── weekly-all.json       # Overall weekly rankings (3,425 records / 137 weeks)
│   │   └── weekly-{language}.json# Dedicated top 25 per language (e.g. 3,425 Python records)
│   ├── monthly/
│   │   ├── monthly-all.json      # Overall monthly rankings (800 records / 32 months)
│   │   └── monthly-{language}.json# Dedicated top 25 per language (e.g. 800 Rust records)
│   └── yearly/
│       ├── yearly-all.json       # Overall yearly rankings (75 records / 3 years)
│       └── yearly-{language}.json# Dedicated top 25 per language (e.g. 75 Go records)
├── src/
│   ├── db.py                     # SQLite schema and upsert engine
│   ├── extractor.py              # RSC Flight stream parser
│   ├── export_json.py            # DB → JSON compiler
│   ├── backfill.py               # Parallel async historical backfill (2024–present)
│   └── sync_daily.py             # Daily sync of current endpoints across all languages
└── trendshift.db                 # Master database (generated, not committed)
```

---

## Usage

### Run Daily Sync
Fetches current endpoints across all 16 language filters, upserts into DB, regenerates JSON:
```bash
python3 src/sync_daily.py
```

### Run Full Historical Backfill (2024 – Present)
Concurrently fetches all 2,768 endpoint variants:
```bash
python3 src/backfill.py
```

### Regenerate JSON from Database
```bash
python3 src/export_json.py
```

---

## Database Schema (`trendshift.db`)

```sql
CREATE TABLE repositories (
    full_name    TEXT PRIMARY KEY,
    description  TEXT,
    language     TEXT,
    created_at   TEXT
);

CREATE TABLE snapshots (
    timeframe             TEXT NOT NULL CHECK(timeframe IN ('daily','weekly','monthly','yearly')),
    period_key            TEXT NOT NULL,
    language_filter       TEXT NOT NULL DEFAULT 'all',
    repository_full_name  TEXT NOT NULL REFERENCES repositories(full_name),
    rank                  INTEGER NOT NULL,
    score                 INTEGER,
    language              TEXT,
    stars_total           INTEGER,
    stars_gained          INTEGER,
    forks_total           INTEGER,
    forks_gained          INTEGER,
    tags_json             TEXT,
    social_mentions_json  TEXT,
    fetched_at            TEXT NOT NULL,
    PRIMARY KEY (timeframe, period_key, language_filter, repository_full_name)
);
```

---

## License

**GNU Affero General Public License v3.0 (AGPL-3.0)** — see [LICENSE](LICENSE).
