# Trendshift Data Pipeline & Static API

An automated data collection pipeline and static JSON API for [Trendshift.io](https://trendshift.io) trending GitHub repositories.

Captures snapshot rankings across thousands of unique GitHub repositories, tracking both overall top-25 rankings and dedicated top-25 rankings for 15 programming languages across daily, weekly, monthly, and yearly timeframes.

---

## Features

- **Complete Historical Coverage**: Backfills all available historical time periods across weekly, monthly, and yearly timeframes across 16 filters (`all` + 15 languages).
- **15 Dedicated Language Rankings**: Fetches complete 25-repo rankings for `C`, `C#`, `C++`, `Dart`, `Go`, `Java`, `JavaScript`, `Kotlin`, `PHP`, `Python`, `Ruby`, `Rust`, `Swift`, `TypeScript`, and `Zig`.
- **Dual Storage**:
  - **SQLite** (`trendshift.db`): Relational database (69.2k snapshots) uploaded to GitHub Releases (`db-latest`).
  - **Static JSON API** (`data/`): 64 language-segmented JSON files + `data/index.json`.
- **Automated Daily Sync**: GitHub Actions workflow at `20:17 UTC` (primary) and `23:43 UTC` (backup) via `uv` + `httpx`.

---

## Static API Access (GitHub Pages CDN)

All data is published as free, minified JSON endpoints hosted via GitHub Pages with **CORS enabled** (`Access-Control-Allow-Origin: *`). No API key or server required.

### Base Endpoint
`https://quantavil.github.io/trendshift/data/`

### Example Endpoints
- **Index Directory**: [`data/index.json`](https://quantavil.github.io/trendshift/data/index.json)
- **Daily Overall**: [`data/daily/daily-all.json`](https://quantavil.github.io/trendshift/data/daily/daily-all.json)
- **Daily Python**: [`data/daily/daily-python.json`](https://quantavil.github.io/trendshift/data/daily/daily-python.json)
- **Weekly Rust**: [`data/weekly/weekly-rust.json`](https://quantavil.github.io/trendshift/data/weekly/weekly-rust.json)
- **Monthly C++**: [`data/monthly/monthly-cpp.json`](https://quantavil.github.io/trendshift/data/monthly/monthly-cpp.json)

---

### Quick Start Code Snippets

#### JavaScript / Browser
```javascript
// Fetch top daily Python repositories directly in any frontend web app
const res = await fetch("https://quantavil.github.io/trendshift/data/daily/daily-python.json");
const repos = await res.json();

console.log(repos[0]);
// { rank: 1, full_name: "owner/repo", stars_gained: 450, github_url: "...", ... }
```

#### Python
```python
import requests

url = "https://quantavil.github.io/trendshift/data/weekly/weekly-rust.json"
data = requests.get(url).json()

for repo in data[:5]:
    print(f"#{repo['rank']} {repo['full_name']} (+{repo['stars_gained']} stars)")
```

#### cURL
```bash
curl -s https://quantavil.github.io/trendshift/data/daily/daily-all.json | jq '.[0:3]'
```

---

## Directory Structure

```
trendshift/
├── pyproject.toml
├── .github/workflows/
│   └── daily_sync.yml            # Cron workflow (20:17 UTC primary, 23:43 UTC backup)
├── data/
│   ├── index.json                # schema_version + per-file row counts
│   ├── daily/
│   │   ├── daily-all.json        # Overall daily top 25
│   │   └── daily-{language}.json # Dedicated top 25 per language (15 files)
│   ├── weekly/
│   ├── monthly/
│   └── yearly/
├── src/
│   ├── db.py                     # SQLite schema and upsert engine
│   ├── extractor.py              # RSC Flight stream parser
│   ├── export_json.py            # DB → JSON compiler
│   ├── backfill.py               # Parallel async historical backfill
│   └── sync_daily.py             # Daily sync of current endpoints across all languages
├── tests/
│   └── test_pipeline.py
└── trendshift.db                 # Master database (generated, not committed)
```

---

## Install

```bash
uv pip install --system -r pyproject.toml
```

## Test

```bash
python3 -m unittest discover -s tests -q
```

## Usage

### Run Daily Sync
Fetches current endpoints across all 16 filters, upserts into DB, regenerates JSON:
```bash
python3 src/sync_daily.py
```

### Run Full Historical Backfill
Concurrently fetches all historical endpoint variants (16 filters):
```bash
python3 src/backfill.py
```

Target one or more language filters (no overall `all` unless you pass it):
```bash
python3 src/backfill.py 'C#' 'C++'
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
