# EMTA Transit Tracker: Deep-Dive Code Explanation

This document is a master class on your entire codebase. It breaks down every file, focusing on the overarching goal of the file first, and then zooming into the logic code block by code block. 

---

## 1. The Core Automation Engine (`.github/workflows/pipeline.yml`)

### Overall Purpose:
This file instructs GitHub servers to run the primary data ingestion code automatically in the cloud. It acts as an invisible computer that turns on, does the work, and turns off, allowing you to capture live transit data continuously without keeping your personal laptop open.

### Block-by-Block Logic:
```yaml
on:
  schedule:
    - cron: '*/5 * * * *'
```
* **The Trigger**: The `cron: '*/5 * * * *'` expression tells GitHub's servers to execute this entire file exactly every 5 minutes, 24/7. 

```yaml
    - name: Set up Python
      uses: actions/setup-python@v5
```
* **The Environment Setup**: GitHub provides a blank Linux computer. This block automatically installs Python 3.10 and caches the libraries so the pipeline runs fast. 

```yaml
    - name: Run ETL Pipeline
      env:
        SUPABASE_DB_URL: ${{ secrets.SUPABASE_DB_URL }}
        ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```
* **Security Injection**: Your code needs the Supabase password to save data. Since you can't push your `.env` file to public GitHub, GitHub Actions extracts the secrets you safely hid in the repository settings and temporarily injects them as active environment variables solely for the duration of the run.

```yaml
      run: |
        python3 -m ingestion.fetch_realtime
        python3 -m transform.silver --days-back 1
        python3 -m transform.gold
```
* **Execution flow**: It runs the ELT (Extract, Load, Transform) pipeline in strict sequence. If any prior step crashes, the entire pipeline immediately stops to prevent data corruption.

---

## 2. Global Rules (`ingestion/config.py`)

### Overall Purpose:
This is the central nervous system for your variables. Instead of hard-coding the database password or API urls 15 times across 5 different scripts, every script imports its settings from here.

### Block-by-Block Logic:
```python
load_dotenv()
SUPABASE_DB_URL = os.environ["SUPABASE_DB_URL"]
```
* **Environment Loading**: It calls `load_dotenv()` so that when you test this on your local Mac, it reads your local `.env` file. Then it pulls the `SUPABASE_DB_URL` into a Python string.

```python
SERVICE_START_HOUR = 5   # 5 AM ET
SERVICE_END_HOUR = 23    # 11 PM ET
```
* **Guardrails**: These integers are imported by the ingestion scripts to prevent scraping data in the middle of the night when buses are parked at the depot. 

```python
EARLY_THRESHOLD = -1       
ON_TIME_MAX = 5            
LATE_MAX = 15              
```
* **The Math Logic**: This establishes exactly how we grade transit performance. If a bus is 4 minutes delayed, it's considered "on-time". If it hits 6 minutes, it's technically "late". Defining these globally means you can change your entire system's definition of "tardiness" by editing one single number.

---

## 3. The Scraper - Bronze Layer (`ingestion/fetch_realtime.py`)

### Overall Purpose:
The pure data extraction layer. Its only job is to contact the EMTA API, ask "where is every bus right now?", and dump the exact, raw response into the `bronze_vehicle_pings` table without altering the original values.

### Block-by-Block Logic:

```python
def is_service_hours() -> bool:
    now_et = datetime.now(EASTERN)
    return SERVICE_START_HOUR <= now_et.hour < SERVICE_END_HOUR
```
* **The "Are we open?" Check**: Because GitHub calls this script every 5 minutes around the clock, this function calculates the current Eastern Standard Time (EDT). If it's outside the hours we defined in config.py, the script halts instantly to save energy and API costs.

```python
def vehicle_to_row(v, route_map):
    return (
        v.get("VehicleId"),
        ...
        json.dumps(v)
    )
```
* **Data Mapping**: We take the giant JSON payload from the API (`v`), and convert it into a flat tuple that exactly matches the columns in our Supabase Bronze table. We even save the full raw JSON via `json.dumps(v)` as a backup column!

```python
        with conn.cursor() as cur:
            cur.executemany(INSERT_SQL, rows)
```
* **Bulk Insertion**: `executemany` packages all the buses into a single massive payload and shoots them to Supabase simultaneously, saving massive amounts of time compared to inserting one by one.
 
---

## 4. The Cleaner - Silver Layer (`transform/silver.py`)

### Overall Purpose:
To grab the noisy, messy Bronze data, categorize the delays, isolate active buses, and parse timestamps into readable formats (e.g., "Monday") so data analysts can easily query it. 

### Block-by-Block Logic:

```python
def classify_delay(minutes: int) -> str:
    if minutes < EARLY_THRESHOLD:
        return "early"
```
* **The Categorizer**: As it loops over every single bus ping, it tags it early/on-time/late using the master config thresholds.

```python
    observed_et = observed_at.astimezone(EASTERN)
    hour_of_day = observed_et.hour
    day_name = DAY_NAMES[day_of_week]
```
* **Time Warping**: The database stores time globally (UTC). This block shifts the time to local Erie timezone (Eastern), and extracts the exact `hour` and `day_name`. This makes the Silver table incredibly human-readable.

```python
    cur.execute(DELETE_SILVER_RANGE, (cutoff,))
    cur.executemany(INSERT_SILVER, silver_rows)
```
* **Idempotency**: Before it inserts the new translated data, it deletes any silver data from the same time range. This guarantees a clean replacement without accidentally duplicating rows if run multiple times.

---

## 5. The Scoreboard - Gold Layer (`transform/gold.py`)

### Overall Purpose:
Provides highly compressed, high-value metrics. Instead of tracking one specific bus, Gold calculates the overall reliability score for *entire routes* grouped by the hour and day of the week over all of history.

### Block-by-Block Logic:

```sql
SELECT
    route_name, hour_of_day, day_name,
    COUNT(*)                                                    AS total_pings,
    COUNT(*) FILTER (WHERE delay_bucket = 'on_time')            AS on_time_count,
```
* **The Aggregator**: This executes raw PostgreSQL to smash the silver data together. It groups them up (e.g., "Route 4 on Mondays at 8 AM"). It counts total pings, and specifically counts how many of those pings were "on-time".

```sql
ON CONFLICT (route_id, hour_of_day, day_of_week)
DO UPDATE SET
    on_time_count = EXCLUDED.on_time_count...
```
* **The Upsert Trick**: We define a unique conflict key: Route + Hour + Day. That means if we recalculate Route 5 on Monday at 8 AM, Postgres realizes this row already exists and gracefully overwrites the old numbers with the newly calculated ones. 

---

## 6. The Weekly AI Analyst (`ai_agent/insights.py`)

### Overall Purpose:
Acts as a virtual transit analyst. It queries the tightly aggregated *historical* (Gold) data to write a factual, pinpoint-accurate summary for the week without making things up ("hallucinating").

### Block-by-Block Logic:

```python
def fetch_gold_summary(conn):
    WHERE total_pings >= 5
    ORDER BY reliability_score ASC
    LIMIT 40
```
* **Data Context Retrieval**: It retrieves only the 40 worst-performing segments (ordered by reliability score going ascending) so we don't overwhelm the AI model's context window, saving token costs.

```python
    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        messages=[{"role": "user", "content": prompt}],
    )
```
* **The Inference Call**: It ships the data and prompt off to Anthropic's Claude 3.5 Sonnet API across the web, waits for the AI to "think", and returns the massive string response.

```python
    parts = text.split("---TWEET---")
```
* **The AI Parser**: The python script splits the giant string exactly where the custom markers (like `---TWEET---`) sit, mapping the outputs to safe variables before writing them to the database.

---

## 7. The Weekly Automated AI Trigger (`.github/workflows/ai_weekly.yml`)

### Overall Purpose:
A separate GitHub Actions workflow dedicated exclusively to running the weekly AI script automatically so you don't have to trigger it manually.

### Block-by-Block Logic:
```yaml
on:
  schedule:
    - cron: '0 8 * * 0'
```
* **The Sunday Timer**: This specific cron formula (`0 8 * * 0`) translates to precisely 8:00 AM every Sunday. Because we designed this as a separate file, the heavy AI report runs quietly in the background without tangling up with the 5-minute data scraper!

---

## 8. The Daily AI Precision Tool (`ai_agent/daily_insights.py`)

### Overall Purpose:
A powerful CLI terminal tool. While the weekly script looks at historical averages from the Gold table, this script looks dynamically at the Silver table to write detailed AI performance reviews for *specific past days*.

### Block-by-Block Logic:
```python
        WHERE observed_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/New_York' >= %s::date
          AND observed_at < (%s::date + INTERVAL '1 day')
```
* **The 24-Hour Scope**: When it queries the `silver_arrivals` table, it does timezone math on the database side to strictly trap records that occurred between midnight and 11:59 PM Eastern Time for whatever target date was requested.

```python
def main():
    parser.add_argument("--date", type=str, help="YYYY-MM-DD format. Defaults to yesterday.")
```
* **The CLI Engine**: This makes the Python code an interactive tool. When you type `--date 2026-04-18` in the terminal shell, this block catches the string, turns it into a Python `date` object, and injects it across the rest of the script. If you provide no argument, it beautifully defaults to `yesterday`!




---

## 9. The Oracle Cloud VM & Cron Setup

### Overall Purpose

GitHub Actions is the primary automation engine, but it has a real limitation: its scheduler has a queue. The `*/5 * * * *` cron you defined means "GitHub, please run this as close to every 5 minutes as possible" — not a hard guarantee. During high-traffic periods, GitHub can delay a run by 5–15 minutes without warning. For live transit data that only matters during a 5-hour service window, a skipped or delayed run means permanently lost position pings.

The Oracle Cloud Always Free VM solves this. It is a dedicated micro-instance (1 OCPU / 1 GB RAM) running 24/7 under your control with a real system crontab — millisecond-accurate scheduling, no queue, no shared runner pool.

The architecture is intentionally redundant: both runners hit the same Supabase database. Because `silver.py` is idempotent (it deletes then re-inserts for the cutoff window) and `gold.py` uses `ON CONFLICT DO UPDATE`, duplicate runs from two sources produce correct data with zero row duplication.

---

### The Setup Script (`setup_vm.sh`)

After SSH-ing into the Oracle instance, you run `bash setup_vm.sh` once. It does five things in sequence:

```bash
sudo apt-get install -y python3 python3-pip python3-venv git
```
**System packages**: Oracle's Ubuntu image ships Python 3.10+ but without `venv`. Installing it here prevents the `ensurepip` error you'd hit when trying to create a virtualenv on a bare instance.

```bash
git clone $REPO_URL $PROJECT_DIR
# or: git -C $PROJECT_DIR pull   (if re-running after an update)
```
**Repo cloning**: The script is idempotent — if the directory already exists it does `git pull` instead of cloning again. This means you can re-run the script after pushing new code to update the VM without rebuilding everything.

```bash
python3 -m venv venv && pip install -r requirements.txt
```
**Isolated environment**: Using a `venv` rather than system `pip install` prevents version conflicts with the system Python. The full path to `venv/bin/python` is what gets written into the crontab (see below).

```bash
read -p "SUPABASE_DB_URL: " -s SUPABASE_DB_URL; echo
cat > "$PROJECT_DIR/.env" <<EOF
SUPABASE_DB_URL=$SUPABASE_DB_URL
ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY
EOF
chmod 600 "$PROJECT_DIR/.env"
```
**Secrets injection**: Rather than pushing a `.env` file to GitHub (a security risk), the script prompts you to paste each secret interactively with hidden input (`-s` flag suppresses terminal echo). The resulting `.env` file is `chmod 600` — readable only by the owner. `dotenv` in the pipeline scripts picks this up automatically.

```bash
CRON_LINE="*/5 * * * * cd $PROJECT_DIR && $PYTHON -m ingestion.fetch_realtime >> $LOG 2>&1 && $PYTHON -m transform.silver --days-back 1 >> $LOG 2>&1 && $PYTHON -m transform.gold >> $LOG 2>&1"
(crontab -l 2>/dev/null | grep -v "fetch_realtime"; echo "$CRON_LINE") | crontab -
```
**Crontab installation**: This is the most precise part. A few key decisions baked in:

| Decision | Reason |
|---|---|
| Absolute path for Python (`$HOME/…/venv/bin/python`) | cron runs without your shell profile — `python3` is not in PATH. Relative paths silently fail. |
| `cd $PROJECT_DIR &&` before each command | The modules are run with `-m`, which requires the working directory to be the repo root so Python can find `ingestion/`, `transform/` etc. |
| `>> $LOG 2>&1` | Appends both stdout and stderr to `~/pipeline.log`. Critical for debugging. Without this, cron failures are invisible. |
| `grep -v "fetch_realtime"` before appending | Prevents duplicate cron lines if you re-run `setup_vm.sh` after an update. |

---

### Errors Encountered & How They Were Fixed

#### Error 1: GitHub Actions Node.js Deprecation Warning

```
Node.js 16 actions are deprecated. Please update the following actions to use Node.js 20 or Node.js 24...
```

**What happened**: GitHub announced end-of-life for Node.js 16 in its Actions runner. Even though your Python code has nothing to do with JavaScript, every GitHub Action (like `actions/checkout@v4` and `actions/setup-python@v5`) runs on a Node.js runtime internally. GitHub started emitting this deprecation warning on every run, which was cluttering the logs and risking future breakage.

**Fix**: Added `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true` to the `env:` block at the top of both workflow files. This is a GitHub-provided environment variable that forces the Actions runner to use the Node.js 24 runtime for all action steps, eliminating the warning.

```yaml
env:
  FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true
```

---

#### Error 2: Crontab Python `ModuleNotFoundError`

```
/usr/bin/python3: No module named ingestion
```

**What happened**: The initial crontab used `python3 -m ingestion.fetch_realtime` without specifying the working directory. `cron` starts every job from the user's home directory (`$HOME`), not the project directory. Python's module system could not find the `ingestion` package because there was no `ingestion/` folder in `$HOME`.

**Fix**: Prepended `cd $PROJECT_DIR &&` to every command in the cron line so Python always runs from the repo root:

```bash
*/5 * * * * cd ~/Public_Transport && ~/Public_Transport/venv/bin/python -m ingestion.fetch_realtime >> ~/pipeline.log 2>&1 ...
```

---

#### Error 3: Crontab Ran but `.env` Secrets Were Not Loaded

```
KeyError: 'SUPABASE_DB_URL'
```

**What happened**: `dotenv`'s `load_dotenv()` looks for a `.env` file relative to the current working directory. The initial crontab command lacked the `cd` prefix, so `load_dotenv()` scanned `$HOME` for a `.env` file instead of `~/Public_Transport/.env` and found nothing.

**Fix**: The `cd $PROJECT_DIR &&` prefix (added for Error 2) also resolved this — once Python runs from the repo root, `load_dotenv()` correctly finds `Public_Transport/.env`.

---

### The Dual-Runner Architecture (Summary)

```
GitHub Actions (cloud cron)          Oracle Cloud VM (system cron)
        │                                       │
        └──────────── Every ~5 min ─────────────┘
                              │
                    Supabase PostgreSQL
                    (Bronze / Silver / Gold)
```

Both runners produce identical idempotent writes. If GitHub's queue delays a run, the VM fills the gap. If the VM goes down for maintenance, GitHub Actions continues. The overlap is a feature, not a bug.

---

## 10. The April 21 Dashboard & Analytics Overhaul

This section documents every change made during the April 21, 2026 working session. Each subsection pairs the *what* (code change) with the *why* (the failure mode or design flaw it addresses).

### 10.1 On-Demand Daily AI Digest (`ai_agent/daily_insights.py` + AI Digest tab)

**What changed:** The daily AI agent was wired into the Streamlit dashboard so any visitor can generate a missing day's digest with a single click, instead of requiring the maintainer to SSH in and run `python -m ai_agent.daily_insights` by hand.

**The hybrid cost model.** Running the AI agent on a cron every N hours burns Anthropic tokens even when nobody reads the output. Running it purely on-demand means every viewer pays the latency of a fresh Claude call. The chosen compromise: cache first (store the digest row in `ai_daily_insights`), generate on-demand only for the specific day a user picks, and reuse forever after. This is why the AI Digest tab shows a date picker bounded between *yesterday* and *60 days ago* — yesterday because today's data is still streaming in, 60 days because anything older is Gold-table territory.

```python
# dashboard/app.py — inside the "Generate digest" button handler
from ai_agent.daily_insights import generate_daily_insights
status = generate_daily_insights(selected_day)
_run_query_cached.clear()      # drop the cached "no row" lookup
if status == "generated":
    st.rerun()
```

**Moving-buses filter.** `daily_insights.py` adds `WHERE speed > 2` to its silver query so a bus idling at the depot (speed ~0, adherence large, still pinging) does not poison the delay averages. The prompt sent to Claude carries a visible caption — "Metrics exclude pings where the bus is parked (speed ≤ 2 mph)" — so the reader knows why the numbers can differ from the Gold table.

**Status-string protocol.** The function returns one of five sentinels (`generated`, `exists`, `no_data`, `missing_env`, or an unexpected string) instead of raising. The Streamlit UI branches on each — `exists` reloads silently, `no_data` shows a warning, `missing_env` is a terminal error. Exceptions still surface as `st.error` in the outer `try`. This keeps the UI responsive without leaking a stack trace to end users.

---

### 10.2 The `sys.path` Bootstrap (`dashboard/app.py`)

**What changed:** A six-line block at the top of `app.py` now prepends the project root to `sys.path` before any project import runs.

```python
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
```

**Why it was needed.** `streamlit run dashboard/app.py` only adds `dashboard/` to `sys.path`, not the repo root. That is fine for the initial load — the file has no project imports at module level — but the lazy `from ai_agent.daily_insights import generate_daily_insights` inside the button handler would fail with `ModuleNotFoundError: No module named 'ai_agent'`. Prepending the root at the top of the file makes every import (eager or lazy) resolve consistently, with no impact on `python -m` invocations (which already have the root on the path).

---

### 10.3 The Heatmap That Lied About Delay (Live Map tab)

**What changed — first pass (commit 8bd6eef):** Replaced `px.density_mapbox` with `px.scatter_mapbox`, coloring by `avg_delay` and sizing by `pings`.

**The underlying bug.** `density_mapbox` does not render "the z value per point". It computes a 2-D kernel-density estimate **weighted** by z. With thousands of pings clustered downtown, the KDE integrates to a huge number even when every one of those pings is on-time — so the map painted central Erie bright red regardless of actual delay. Scatter geometry has no such integration: one dot, one color, one delay.

**What changed — second pass (commit 3d6df54):** Re-keyed the map so each dot's color is its `route_name`, not its delay. The legend on the right edge now enumerates every EMTA route seen today.

```python
fig_hm = px.scatter_mapbox(
    heat,
    color="route_name",                              # line identity
    size="pings",                                    # activity density
    color_discrete_sequence=px.colors.qualitative.Alphabet,  # ~26 hues
    hover_data={"avg_delay": True, ...},             # delay still visible on hover
)
```

**Why this framing is better.** A rider does not want to know "where in Erie are buses late on average" — they want to know "which line is running here". Delay is still present in the tooltip, but it is no longer competing with route identity for the visual channel. The `Alphabet` qualitative palette is chosen because it has enough distinct hues (~26) for EMTA's fleet; most qualitative palettes only have 8–10 before they start recycling.

**Caveat left on the table.** The grid query still uses `ROUND(lat/lon, 3)` (~100 m squares). Two routes crossing the same square become two overlapping dots — the later one visually dominates. That is acceptable for the level of zoom Erie needs, and fixing it would require either a Mapbox layer per route (expensive) or a jitter (misleading).

---

### 10.4 Peak-Hour Callout Threshold: 5 → 25 Pings (Overview tab)

**What changed:** The "Worst peak-hour route" SQL filter now requires `total_pings >= 25` instead of `>= 5`.

**Why the old value was wrong.** The Gold table is aggregated across **all of history** per `(route, hour, day_of_week)` bucket. A bucket with only 5 pings represents perhaps a single bad-weather trip that went 20 minutes late. The dashboard was then surfacing that outlier as "worst route" — technically correct, analytically useless. At 25 pings, a bucket represents at least five separate days of service at that hour, so a single anomalous trip cannot dominate.

```sql
WHERE hour_of_day IN (7, 8, 16, 17, 18)
  AND total_pings >= 25                    -- was 5
ORDER BY reliability_score ASC
```

---

### 10.5 The Reliability Formula's Earliness Blind Spot (`transform/gold.py`)

**What changed:** The delay penalty in the `reliability_score` formula now uses `ABS(AVG(adherence_minutes))` instead of `GREATEST(AVG(adherence_minutes), 0)`.

```sql
-- before
+ (1.0 - LEAST(GREATEST(AVG(adherence_minutes), 0), 15) / 15.0) * 100 * 0.3
-- after
+ (1.0 - LEAST(ABS(AVG(adherence_minutes)),       15) / 15.0) * 100 * 0.3
```

**The design flaw.** Adherence is signed: negative = early, positive = late. `GREATEST(x, 0)` floors negatives to zero, meaning a route whose average arrival is 10 minutes **early** scores a perfect 0-minute "delay" — i.e. a perfect 30/30 on the penalty component. From a rider's perspective that is wrong twice over: a bus leaving a stop 10 minutes early is worse than one arriving 10 minutes late, because the late rider at least still gets to ride.

**The fix.** `ABS(...)` treats earliness and lateness symmetrically. Being 10 early and being 10 late both consume two-thirds of the 15-minute penalty budget (the cap is retained so one catastrophic trip cannot tank a score to zero).

**Rollout behaviour.** `transform/gold.py` runs a full `GROUP BY` across `silver_arrivals` every 5 minutes and upserts every bucket. There is no time filter, so the first pipeline tick after this change rewrites every existing Gold row with the new score — no manual backfill required.

---

### 10.6 Pie → Trend Coupling (Overview tab)

**What changed:** The Delay Distribution pie chart now uses Streamlit's plotly event API to drive the adjacent Daily On-Time % line chart. Clicking a bucket slice (e.g. `very_late`) switches the line to `Daily Very Late %` for that same date window, coloured to match the slice. Clicking empty space (deselect) restores the on-time default.

```python
pie_event = st.plotly_chart(
    fig_bucket,
    on_select="rerun",
    selection_mode="points",
    key="delay_bucket_pie",
)

selected_bucket = None
if pie_event and getattr(pie_event, "selection", None):
    pts = pie_event.selection.get("points") or []
    if pts:
        selected_bucket = pts[0].get("label")      # e.g. "late"
```

**Why it needed a rewrite, not a Plotly trick.** Plotly's built-in legend-toggle only hides traces inside the *same* figure. The pie and the line chart are two independent `st.plotly_chart` calls in two different Streamlit columns — Plotly has no cross-figure link. The only way to couple them is at the Python layer: pass `on_select="rerun"` so Streamlit reruns the whole script on every click, read `pie_event.selection`, and branch the SQL on `selected_bucket`.

**Two branches, one query template.** Both branches use the same `COUNT(*) FILTER (WHERE delay_bucket = %s) * 100.0 / COUNT(*)` shape — the difference is only whether `%s` is bound to the user's selection or hard-coded to `'on_time'`. The line title and color are derived from `selected_bucket` via the existing `COLORS` palette so the visual language stays consistent.

**Requirement bump.** `st.plotly_chart(on_select=...)` landed in Streamlit 1.35. The repo's `requirements.txt` used to pin `>=1.30`, which would have silently degraded the feature for fresh clones. It is now `streamlit>=1.35`.

---

### 10.7 Summary Table — Four Commits, One Session

| Commit  | File(s)                                  | One-liner |
|---------|------------------------------------------|-----------|
| 50829f4 | `ai_agent/daily_insights.py`, dashboard  | Add on-demand daily AI digest with moving-buses filter |
| 8bd6eef | `dashboard/app.py`                       | Fix dashboard import path, heatmap coloring, and peak-hour threshold |
| 3d6df54 | `dashboard/app.py`, `requirements.txt`   | Color route map by line; couple Overview pie → daily trend |
| *(next)*| `transform/gold.py`, `Explanation.md`    | Symmetrise the reliability formula; document the session |

The common thread: each change either (a) makes the dashboard tell the truth about the data, or (b) makes the data itself more honestly reflect rider experience. Neither was about adding surface area; both were about removing misleading signals.

---

## 11. The April 21 Dashboard UX Polish

Late on the same session, a round of rider-empathy fixes landed. Each sub-fix either removes a question a user would ask ("what does *Unknown* mean?") or fixes a chart that was technically correct but visually misleading.

### 11.1 Dropping the "Unknown" Direction Option

**What changed:** The sidebar "Direction" radio used to expose four options (`All / Inbound / Outbound / Unknown`). The `Unknown` option is gone; only three remain.

**Why the fourth option was a trap.** A ping with `direction = NULL` is not a transit concept — it's a gap in the Avail API feed, typically a bus on layover, a circular shuttle, or a row the upstream classifier never filled in. Labelling that bucket as `Unknown` in a public filter forced every rider or analyst to stop and wonder whether the data was broken. The option existed only so that `Inbound + Outbound + Unknown = All` totals reconciled, which is an internal QA concern, not a UX one.

**The replacement.** The three remaining options (`All`, `Inbound`, `Outbound`) are self-explanatory. When a user picks either directional filter, a caption slides in under the radio:

```
ℹ️ Inbound/Outbound exclude pings with no direction data from the Avail API
(typically buses on layover or circular routes). Switch to All to see every ping.
```

This tells the user *why* the numbers shrink when they filter, without introducing a confusing fourth bucket. The SQL `AND (direction IS NULL OR ...)` branch was deleted with the option.

---

### 11.2 The Live Map Legend That Hurt (`color_discrete_sequence`)

**What changed:** The route-coloured activity map now uses `px.colors.qualitative.Dark24` on a `carto-darkmatter` basemap, with a dark-theme-aware legend.

**Why Alphabet had to go.** `px.colors.qualitative.Alphabet` is a 26-hue neon palette designed for print — fluorescent yellow, electric cyan, hot magenta. Rendered against the previous `carto-positron` (light grey) basemap with a white legend overlay, the combination was visually fatiguing on the app's dark theme. Users reported eye strain after a few seconds.

**Dark24 + Dark basemap.** `Dark24` is a 24-hue palette tuned for dark backgrounds — the colours are saturated enough to stay distinct but muted enough not to vibrate. Switching the basemap to `carto-darkmatter` means the whole map tab now matches the dashboard's chrome. The legend was re-skinned to match:

```python
legend=dict(
    title=dict(text="Route", font=dict(color="#e5e7eb")),
    bgcolor="rgba(24, 26, 32, 0.85)",
    bordercolor="rgba(120, 120, 130, 0.5)",
    borderwidth=1,
    font=dict(size=11, color="#e5e7eb"),
)
```

The semi-transparent dark panel and thin grey border let the legend float cleanly over the map without bleaching it.

---

### 11.3 The Route Detail Bars That Ran to Hour 60

**What changed — symptom:** On the Route Detail tab, the hourly on-time and avg-delay bar charts had bars as thin as hairlines, with the x-axis sometimes running to 60 instead of 23.

**Root cause.** `hour_of_day` comes back from Postgres as an integer. Plotly treats integer x-values as a *continuous* axis and picks bar width based on the minimum spacing in the data. For a sparse route (say, only hours 6, 15, 19 populated), Plotly computes a huge "natural" bar width, then pads the axis out to accommodate it — producing both the excess x-axis range and the narrow bars at the same time.

**The fix.** Coerce `hour_of_day` to a zero-padded string (`"00"` … `"23"`), pass it through `category_orders`, and force `xaxis=dict(type="category")`. Plotly now renders one uniform-width bar per hour category, no matter how sparse the data:

```python
hour_labels = [f"{int(h['hour_of_day']):02d}" for h in hourly]
for row, lbl in zip(hourly, hour_labels):
    row["hour_label"] = lbl
hour_order = sorted({h["hour_label"] for h in hourly})

fig_h = px.bar(
    hourly, x="hour_label", y="on_time_pct",
    category_orders={"hour_label": hour_order},
    ...
)
fig_h.update_layout(bargap=0.15, yaxis=dict(range=[0, 100]),
                    xaxis=dict(type="category"))
```

`bargap=0.15` gives the bars room to breathe without looking stranded. The y-range clamp on the on-time chart prevents Plotly from auto-zooming a `98%` peak into "looks like 100% across the board".

---

### 11.4 The AI Weekly Digest Empty State

**What changed:** The weekly digest tab used to show a dev-facing message — `Run python -m ai_agent.insights after collecting at least a week of data` — when no insights existed. That instruction is for the maintainer, not the visitor.

**Replacement.** A neutral, user-facing info box:

```
📊 Not enough data yet. The tracker is still young — the weekly digest activates
automatically once we've collected a full week of service data. Check back soon.
```

The tracker's own GitHub Action (`ai_weekly.yml`) runs every Sunday and will populate the table without human intervention, so no instructions are needed here.

---

### 11.5 Tweet Drafts Hidden from the UI

**What changed:** The `tweet_draft` field (generated by both `ai_agent/insights.py` and `ai_agent/daily_insights.py`) is still written to the database, but it no longer appears in the Streamlit UI. All three display sites — the weekly expander loop, the selected daily digest block, and the recent daily digests expander loop — had their `st.markdown("🐦 Tweet draft: ...")` lines removed.

**Why keep the DB column.** The tweet draft is an operator artefact — a one-line share text that the maintainer can copy into a social post. Showing it to every rider made the digest look like a press release instead of a dashboard. Leaving the column populated means a future operator tool (a Slack poster, a scheduled tweet bot) can still consume it without re-running the AI.

---

### 11.6 The AI Daily Digest Visual Upgrade

**What changed:** The "AI Daily Digest" section used to be a headline banner plus a wall of paragraph text. It now has a severity-coloured banner, a four-KPI strip, and a bordered narrative container — all fed from data already in the database.

**Zero extra Anthropic calls.** The upgrade was specifically designed to respect the project's cost preference: no new LLM calls per render. Everything visible is either (a) already in the `ai_daily_insights` row, or (b) a cached SQL aggregate over `silver_arrivals` for the selected day.

**The severity banner.** The side-stripe colour is derived from the on-time percentage:

| OTP %    | Colour  | Label          |
|----------|---------|----------------|
| `< 70`   | red     | 🔴 Poor day    |
| `70–85`  | amber   | 🟡 Mixed day   |
| `≥ 85`   | green   | 🟢 Strong day  |

The banner renders with a 4px coloured left border, the label + date in small uppercase text above, and the AI-written headline below — a pattern cribbed from news-site lead cards.

**The KPI strip.** Four `st.metric` columns sit under the banner:

```python
k1.metric("On-Time %",     f"{kpi['otp_pct']}%")
k2.metric("Avg Delay",     f"{kpi['avg_delay']} min")
k3.metric("Active Routes", kpi["active_routes"])
k4.metric("Very Late",     kpi["very_late"])
```

These are pulled by a single SQL query against `silver_arrivals` with the same `speed > 2` moving-buses filter the AI agent uses, so the numbers above the banner always match the numbers the AI narrative cites.

**The bordered narrative.** `st.container(border=True)` wraps the narrative markdown in a subtle rounded panel — the prose now reads as "the story behind the numbers" instead of being visually indistinguishable from the rest of the page. No new dependencies; `container(border=...)` shipped with Streamlit 1.29.

---

### 11.7 Summary Table — Polish Pass

| Area              | File                | Fix |
|-------------------|---------------------|-----|
| Direction filter  | `dashboard/app.py`  | Drop `Unknown`; explain what filter excludes |
| Live Map          | `dashboard/app.py`  | `Dark24` palette, dark basemap, themed legend |
| Route Detail      | `dashboard/app.py`  | Categorical x-axis fixes thin bars / runaway range |
| AI Weekly Digest  | `dashboard/app.py`  | User-facing "not enough data" empty state |
| AI Digest UI      | `dashboard/app.py`  | Remove tweet drafts; keep them in DB |
| AI Daily Digest   | `dashboard/app.py`  | Severity banner, KPI strip, bordered narrative — all from cached SQL |

Each row is strictly a UX improvement: no schema changes, no new Anthropic calls, no new dependencies.

---

## 12. The Second Polish Pass — Maps, Live Reliability, Sorted Routes

The first polish landed clean, but a morning spent staring at the live dashboard surfaced four more rough edges:

1. The two maps (live vehicles vs. today's activity by route) used different basemaps — the live view sat on the light `carto-positron` tiles while the toggle flipped to pitch-black `carto-darkmatter`. Switching the toggle flashed the whole tab from white to near-black.
2. The "Worst peak-hour route" banner on the Overview tab was frozen. Scrolling the date picker to a different day left the banner unchanged — it always reported `Peach Street at 16:00 — 49.81/100` regardless of filter.
3. The reliability heatmap below it was missing routes. EMTA runs ~20 weekday routes but the heatmap only showed 15.
4. The Route Detail dropdown sorted routes alphabetically by label (`"105 — Corry Loop"` before `"3 — Peach Street"`), which reads as random noise to anyone who knows EMTA routes by number.

Fixes below.

---

### 12.1 Unifying the Two Maps

**What changed:** The live vehicles map on the Live Map tab now uses `carto-darkmatter` — the same basemap the "Show today's activity by route" toggle already used. Both views share a themed legend (dark background, light border, `#e5e7eb` text) so the switch looks like the same map with a different overlay, not a different app.

```python
fig_map.update_layout(
    mapbox_style="carto-darkmatter",
    mapbox_center={"lat": 42.129, "lon": -80.085},
    legend=dict(
        title=dict(text="Status", font=dict(color="#e5e7eb")),
        bgcolor="rgba(24, 26, 32, 0.85)",
        bordercolor="rgba(120, 120, 130, 0.5)",
        borderwidth=1,
        font=dict(size=11, color="#e5e7eb"),
    ),
    **PLOTLY_LAYOUT,
)
```

**Why dark-matter and not positron.** The dashboard frame is dark. A light basemap makes the map panel punch out of the page, which is fine in isolation but jarring when the user toggles between the two views. Dark-matter sits quietly within the frame and lets the coloured vehicle/route dots carry the signal.

---

### 12.2 The Worst-Route Banner That Never Moved

**Root cause.** The banner was querying `gold_route_reliability`, which is the *lifetime* aggregate. The date picker in the sidebar drives every other chart on the page via `observed_at::date BETWEEN %s AND %s` against `silver_arrivals`, but the banner was reading a table that has no date column at all.

**What changed:** Replaced the gold query with a silver query that honours the date range, applies the same `speed > 2` moving-bus filter used everywhere else, and re-derives the reliability score on the fly:

```sql
SELECT route_id, route_name, hour_of_day,
       GREATEST(0, ROUND(
           (100 - LEAST(100, AVG(ABS(adherence_minutes)) * 10))::numeric, 2
       )) AS reliability_score,
       COUNT(*) AS total_pings
FROM silver_arrivals
WHERE observed_at::date BETWEEN %s AND %s
  AND hour_of_day IN (7, 8, 16, 17, 18)
  AND speed > 2
  AND adherence_minutes IS NOT NULL
  {direction_filter_sql}
GROUP BY route_id, route_name, hour_of_day
HAVING COUNT(*) >= 30
ORDER BY reliability_score ASC
LIMIT 1
```

The formula is the same symmetric penalty the gold transform uses: every minute of absolute adherence deviation costs 10 points, capped at 100, floored at 0. Early by 3 min and late by 3 min are penalised equally — the project's stated reliability stance.

**Why `live=True`.** The read passes through the 60 s live cache, not the 3600 s standard cache. That way a user leaving the dashboard open during the day sees the banner refresh within a minute of each 5-minute ETL cycle landing fresh rows.

**Banner copy.** Changed the label from "Worst peak-hour route:" to "Worst peak-hour route (in selected range):" so users understand the number is scoped to the date picker, not all-time.

---

### 12.3 Expanding the Heatmap to All Routes

**Root cause.** Same one. The heatmap was reading `gold_route_reliability WHERE total_pings >= 3`, with no date filter, and gold's `total_pings` is a lifetime count — so a route that ran a handful of trippers last month and hasn't moved since got filtered out.

**What changed:** The heatmap now computes its own route × hour reliability grid from `silver_arrivals` inside the date range, with the same formula as the banner. The threshold dropped to `HAVING COUNT(*) >= 2` per cell, which filters out truly accidental pings while still surfacing low-volume shuttles like the PSB Shuttle or PM Tripper.

**Sort order on the Y-axis.** The original pandas `pivot_table` indexed by `route_label` and pandas sorted that index as a string — meaning `"105 — Corry Loop"` ended up *above* `"14 — Edinboro"`. We now build the route order explicitly by numeric `route_id` and pass it to `pivot.reindex(...)`:

```python
def _route_sort_key(rid):
    rid_s = "" if rid is None else str(rid).strip()
    try:
        return (0, int(rid_s), rid_s)
    except (TypeError, ValueError):
        return (1, 0, rid_s.lower())

route_order = [
    r["route_label"] for r in sorted(
        df_heat[["route_id", "route_label"]].drop_duplicates().to_dict("records"),
        key=lambda r: _route_sort_key(r["route_id"]),
    )
]
pivot = df_heat.pivot_table(...).reindex(route_order)
```

Numeric routes sort first in ascending order (3, 5, 14, 18, 20, …, 105, 260); non-numeric IDs like `"PM Tripper"` fall to the bottom, alphabetised among themselves.

---

### 12.4 Numerically Sorted Route Dropdown

The Route Detail tab's `st.selectbox` options came straight from `SELECT ... ORDER BY route_name`. The labels start with the route number, so the dropdown listed them in string order: 105, 14, 18, 20, 22, 23, 25, 260, 27, 29, 3, 30, 32, 5, 99, PM Tripper.

Same `_route_sort_key` trick as the heatmap, applied client-side after the query:

```python
def _route_sort_key(r):
    rid = "" if r.get("route_id") is None else str(r["route_id"]).strip()
    try:
        return (0, int(rid), rid)
    except (TypeError, ValueError):
        return (1, 0, rid.lower())
routes = sorted(routes, key=_route_sort_key)
```

The dropdown now reads: 3, 5, 14, 18, 20, 22, 23, 25, 27, 29, 30, 32, 99, 105, 260, PM Tripper. Which is how anyone who rides the bus thinks about the bus.

---

### 12.5 Summary Table — Second Polish Pass

| Area              | File                | Fix |
|-------------------|---------------------|-----|
| Live Map          | `dashboard/app.py`  | Both views use `carto-darkmatter`; themed legend on both |
| Overview banner   | `dashboard/app.py`  | Query silver within date range; 60 s cache; same formula as gold |
| Heatmap           | `dashboard/app.py`  | Silver-derived per render; shows every route with ≥ 2 pings in range |
| Heatmap Y-axis    | `dashboard/app.py`  | Numeric-first route order, not alphabetical string order |
| Route dropdown    | `dashboard/app.py`  | Numeric-first sort via `_route_sort_key` |

Zero schema changes. Zero new Anthropic calls. The banner and heatmap now reflect whatever date range the user picks and refresh within 60 s of the pipeline landing new rows.

---

## 13. The Third Polish Pass — Single-Day Selection, 5-Minute Refresh, Executive Digest

The second pass landed the banner-updates-with-filter fix, but only in principle: the Overview page KPIs still looked static, the banner still showed `0/100`, and the heatmap was still sparse. The root cause wasn't any of the queries we had rewritten — it was a bug one layer above them, in the sidebar.

### 13.1 The Single-Day Date Bug

**Root cause.** Streamlit's `st.date_input` returns a `tuple` of length 1 while the user is mid-selection (they've clicked the first handle but not the second), and a bare `date` object if the picker is configured as a single-day picker. Our sidebar code only handled the two-element tuple case and fell back to a 30-day default for everything else:

```python
if isinstance(date_range, tuple) and len(date_range) == 2:
    filter_start, filter_end = date_range
else:
    filter_start, filter_end = default_start, default_end   # 30-day fallback!
```

Every time the user clicked a single date in the picker, the page *looked* like it was honouring a single-day filter (because the picker UI showed that date) but all the underlying SQL was still running against the last 30 days. That's why the "Worst peak-hour route" banner always showed `0/100` with `Peach Street at 16:00` — 30 days of data guaranteed at least one cell where `AVG(|adherence|) ≥ 10 min`, and that cell never moved.

**Fix:**

```python
if isinstance(date_range, tuple):
    if len(date_range) == 2:
        filter_start, filter_end = date_range
    elif len(date_range) == 1:
        filter_start = filter_end = date_range[0]
    else:
        filter_start, filter_end = default_start, default_end
elif isinstance(date_range, date):
    filter_start = filter_end = date_range
else:
    filter_start, filter_end = default_start, default_end
```

Single-day picks now collapse to `(day, day)`, which every downstream query already knew how to honour because of the `BETWEEN %s AND %s` idiom. The banner, the heatmap, the KPI row, the trend chart — all of them suddenly responded to the picker.

---

### 13.2 Dropping the Default Cache TTL from 3600 s to 300 s

**Root cause #2.** The autorefresh timer rerun the app every 5 minutes, but `run_query` wrapped its results in `@st.cache_data(ttl=3600)`. Meaning: the app *ran*, but every query hit the cache and returned the same data for a full hour. New rows from the ETL weren't visible until the cache expired.

**Fix:** dropped the default cache TTL to 300 s — exactly one ETL cycle. The auto-refresh and the cache now line up:

```python
@st.cache_data(ttl=300)
def _run_query_cached(sql: str, params: tuple) -> list[dict]:
    ...
```

The `live=True` path keeps its 60 s TTL for the live vehicle map, because 5 minutes would be too stale for "current positions".

---

### 13.3 A Citywide Reliability KPI Next to the Rest

"Worst route 0/100" has no context on its own — every day of bad service produces at least one cell at the floor of the score. Adding a citywide number next to it turns the bad cell into a data point on a range instead of the whole story.

**What changed:** the Overview metrics row now has a fifth column — **City Reliability** — computed with the exact same symmetric formula as the banner and the heatmap:

```sql
GREATEST(0, ROUND(
    (100 - LEAST(
        100,
        AVG(ABS(adherence_minutes)) FILTER (
            WHERE speed > 2 AND adherence_minutes IS NOT NULL
        ) * 10
    ))::numeric, 1
)) AS city_reliability
```

The tooltip (`help=`) on the metric spells out the formula so the reader doesn't have to guess. Together with the worst-route banner the user now sees something like *"City 82.4/100 — worst route 0/100 at 16:00"*, which is a much more readable story.

### 13.4 A Readable Date Caption

Under the Overview header we now print a single-line caption describing the active filter:

- single day → `📅 Apr 21, 2026`
- range     → `📅 Apr 01 – Apr 21, 2026`

No trailing dash, no guessing what the KPIs are summarising.

---

### 13.5 Brighter Map Palette

Dark24 was legible for most routes on the dark-matter basemap, but a few — route 16 in particular — landed close to the basemap's own accents and became effectively invisible. Switched the "Today's activity by route" map to `px.colors.qualitative.Light24`, which was designed for dark backgrounds and keeps every hue bright enough to read.

---

### 13.6 The Executive-Summary AI Daily Digest

The daily digest had the KPIs and the narrative, but "Very Late" as a raw integer left the reader guessing what it meant, and a wall of paragraph text wasn't the "day in review" feel the project wanted. Three targeted additions — all SQL-driven, zero new Anthropic tokens:

**Tooltips on every KPI.** Each `st.metric` now carries a `help=` string:

| KPI              | Tooltip |
|------------------|---------|
| On-Time %        | Share of moving-bus pings within EMTA's on-time window (−1 to +5 min). |
| Avg Delay        | Average signed adherence. Negative = early, positive = late. |
| Active Routes    | Distinct route IDs that produced ≥ 1 moving-bus ping today. |
| Very Late Pings  | Number of pings where the bus was > 10 minutes behind schedule. |

**Hourly OTP arc chart.** A single coloured bar per hour of the day, red-to-green, showing how on-time % rose and fell across the day. At a glance you can see whether the bad reliability number came from a bad morning or a bad afternoon:

```sql
SELECT hour_of_day,
       ROUND(
           COUNT(*) FILTER (WHERE delay_bucket = 'on_time') * 100.0
           / NULLIF(COUNT(*), 0), 1
       ) AS otp_pct,
       COUNT(*) AS pings
FROM silver_arrivals
WHERE observed_at::date = %s
  AND speed > 2
GROUP BY hour_of_day
ORDER BY hour_of_day
```

**Top 3 problem routes.** A small three-row table under the arc chart, listing the worst routes of the day by the same reliability formula, with their avg delay and ping count. The digest now answers the executive's three questions in the first screen: *how was the day*, *when did it go wrong*, *who were the worst offenders* — and then the AI narrative tells the story behind the numbers.

---

### 13.7 Summary Table — Third Polish Pass

| Area                    | File                | Fix |
|-------------------------|---------------------|-----|
| Sidebar date picker     | `dashboard/app.py`  | Collapse single-day pick to `(day, day)` instead of falling back to 30-day default |
| Default cache TTL       | `dashboard/app.py`  | 3600 s → 300 s so the 5-min auto-refresh actually refreshes |
| Overview KPIs           | `dashboard/app.py`  | Added **City Reliability** as the 5th metric |
| Overview header         | `dashboard/app.py`  | Inline date caption, range vs single-day formatting |
| Live Map palette        | `dashboard/app.py`  | `Dark24` → `Light24` for visibility on dark-matter |
| AI Daily KPIs           | `dashboard/app.py`  | Tooltips on every KPI; renamed "Very Late" → "Very Late Pings" |
| AI Daily digest         | `dashboard/app.py`  | Added hourly OTP arc chart + top-3 problem routes table |

No new dependencies. No new Anthropic calls. Everything new reads from existing silver rows through the same cached SQL path the rest of the page uses.

---

## 14. The Fourth Polish Pass — ET Timestamps, Outlier-Resistant Scores, Adaptive Charts

This session started with three user-visible bugs: a reliability score that always read 0/100, chart bars for hours that hadn't happened yet, and a date picker that showed a trailing dash even when a single day was selected. Diagnosing them required live database inspection, which revealed a deeper cluster of timestamp and outlier issues. Fixes below.

---

### 14.1 The UTC-vs-ET Date Bug (All Tabs)

**Root cause.** Silver's `observed_at` column is stored as `TIMESTAMPTZ` — a UTC wall-clock timestamp. `observed_at::date` in PostgreSQL returns the UTC date, which is the same as the ET date for most of the day but diverges at midnight UTC = 8 PM ET. Any ping that arrived after 8 PM ET on a given day was recorded with the *next* UTC date, and any ping from midnight–4 AM ET appeared under the *current* UTC date.

**The visible symptom.** On the Route Detail hourly chart for "today", hours 20, 21, and 22 showed bars with real data — but it was 7 PM ET and those hours hadn't started yet. The explanation: the UTC-today window (`observed_at::date = CURRENT_DATE`) swept in last night's 8–10 PM ET pings from the previous calendar day. Silver's `hour_of_day` column stores the correct ET local hour, so the bars were labeled 20/21/22 with authentic-looking delay averages from *yesterday evening*.

**Fix.** Every `observed_at::date` comparison in the dashboard was replaced with the ET-converted form:

```sql
-- before
WHERE observed_at::date BETWEEN %s AND %s

-- after
WHERE (observed_at AT TIME ZONE 'America/New_York')::date BETWEEN %s AND %s
```

The Live Map's "today's activity" view used `CURRENT_DATE` (also UTC) for its filter; that became `(NOW() AT TIME ZONE 'America/New_York')::date`. This was a global replace across all 17 occurrences in `dashboard/app.py`. The two interval-based comparisons (`observed_at >= NOW() - INTERVAL '…'`) were left unchanged — they don't involve dates.

---

### 14.2 The Outlier-Driven 0/100 City Reliability Score

**Root cause.** A database inspection of today's `silver_arrivals` showed the following adherence distribution for moving buses:

| Percentile | `\|adherence\|` |
|---|---|
| p50 (median) | 2 min |
| p75 | 6 min |
| p90 | 16 min |
| p95 | 46 min |
| p99 | **196 min** |
| max | **345 min** |

~10% of pings (490 of ~4,700) reported `|adherence| > 30 min`. These are almost certainly buses with stale adherence counters — a vehicle parked at the end of a line or returning to the depot that the Avail API keeps pinging as "on route" with a ballooning lateness figure. `speed > 2` catches most parked buses but not a bus creeping toward the garage at 3 mph while still attached to a ghost trip.

The formula `100 - 10 × AVG(|adherence|)` uses an unweighted mean. The 1% of pings at 196–345 minutes pulled the citywide average past 12 minutes, which floors the score at 0/100 under the `GREATEST(0, …)` clamp.

The median of 2 min and the cap-at-30 average of 5.3 min represent the real service quality far better than the outlier-dominated mean of 12 min.

**Fix.** Per-ping `|adherence|` is clamped at 30 minutes before averaging in every reliability formula on the page:

```sql
-- before (in all four formulas: city KPI, banner, heatmap, digest top-3)
AVG(ABS(adherence_minutes)) * 10

-- after
AVG(LEAST(30, ABS(adherence_minutes))) * 10
```

30 minutes is the standard transit "maximum credible deviation" — a bus 30 min late is bad, a bus 200 min late is almost certainly a data artefact. Capping at 30 means one stranded bus cannot drag the entire system score to zero.

**Verified outcome.** With the cap, today's city reliability reads **47.2/100** instead of 0/100. The worst-peak-hour banner similarly produces non-zero, differentiated scores.

**Display integrity.** The worst-peak-hour banner shows the *uncapped* `AVG(|adherence|)` in its subtitle (`"avg N min late across M pings"`) so the operator still sees the true magnitude of the worst hour, not the scoring proxy. The City Reliability tooltip was updated to explain the cap.

---

### 14.3 Date Picker — Trailing Dash Fix

**Root cause.** The previous code passed `value=(today, today)` to `st.date_input`, which renders Streamlit's **range** picker. Even when both handles sit on the same date, the range picker displays a trailing " – " because it is designed to show `start – end`. So the sidebar showed `2026/04/21 –` at page load, which looked like a half-filled form.

**Fix.** The sidebar now defaults to a plain **single-date** input:

```python
compare_range = st.sidebar.checkbox("Compare a range", value=False)
if compare_range:
    range_start = st.sidebar.date_input("Start date", value=today - timedelta(days=7), ...)
    range_end   = st.sidebar.date_input("End date",   value=today, ...)
    filter_start, filter_end = (
        (range_start, range_end) if range_start <= range_end
        else (range_end, range_start)
    )
else:
    picked = st.sidebar.date_input("Date", value=today, ...)
    filter_start = filter_end = picked
```

Single-date mode opens cleanly as `2026-04-21` with no trailing punctuation. The "Compare a range" checkbox reveals the Start/End pair only when the user needs it, and auto-swaps the dates if the user sets End before Start.

---

### 14.4 Date Caption Format

Single-day captions now read `YYYY-MM-DD` (e.g. `2026-04-21`). Range captions read `YYYY-MM-DD – YYYY-MM-DD`. The previous format used abbreviated month names (`Apr 21, 2026`) which parsed unambiguously but was inconsistent with how date inputs are displayed elsewhere in the UI.

---

### 14.5 City Reliability Now Uses the 60-Second Cache

Before this pass, the five Overview KPIs — including the new City Reliability column — were served by the 300-second standard cache. In theory, the 5-minute autorefresh timer and the 300-second cache expire at the same cadence. In practice, they fire independently and can drift: the page reruns but the cache hasn't expired yet, and the user sees identical numbers.

Fix: the metrics query now passes `live=True`, using the 60-second cache path. This matches the banner and ensures the city score always reflects the latest ETL batch within one minute of it landing.

---

### 14.6 Worst Peak-Hour Banner — Sample Threshold and Tie-Breaking

Two issues in the banner query, both addressed in an earlier pass but documented here for completeness:

**Threshold raised from 30 → 100 pings.** With `HAVING COUNT(*) >= 30`, a single rush-hour window with 30 pings (about one week of daily service on a low-frequency route) qualified as "worst" even if all 30 pings came from an atypical week. At 100 pings the bucket represents meaningful, reproducible evidence. The `HAVING` was also updated to use `COUNT(*) >= 100` consistently — the banner now stays blank rather than show noise when the selected range is too short.

**Tie-breaking on `AVG(ABS(adherence_minutes)) DESC`.** Multiple routes scoring 0/100 previously broke their tie arbitrarily by query-planner row order. The revised `ORDER BY reliability_score ASC, avg_abs_delay DESC` promotes the route with the highest raw average absolute deviation to the top, so the banner always names the *most-off-schedule* route, not a random floor-scorer.

**Synthetic route exclusion.** Routes 98 (AM Tripper), 99 (PM Tripper), and 999 (Deadhead) are excluded from the banner via `AND route_id NOT IN ('98', '99', '999')`. This exclusion was applied consistently to the heatmap and the daily-digest top-3 worst-routes table as well.

---

### 14.7 Route Detail Dropdown — Bronze Source and Avail-Feed Caption

**Previous behaviour.** The dropdown read distinct routes from `silver_arrivals WHERE route_name IS NOT NULL`. Silver requires `adherence_minutes IS NOT NULL` to pass the ETL filter, so any route that showed up in the Avail feed without a schedule attachment (and therefore no adherence value) was invisible in the dropdown.

**Fix.** The source changed to `bronze_vehicle_pings` (last 30 days), which keeps every on-route ping regardless of adherence availability. The `is_on_route = TRUE` restriction was also dropped — all routes in the database came through with `is_on_route = TRUE` in the first place, and the restriction was adding no filtering while potentially masking edge cases.

**The missing routes.** User reported routes 1 Glenwood, 11 Harborcreek, and 15 E 38th as missing. A full scan of bronze confirmed they have never appeared in the Avail feed, even across the oldest data available. EMTA's printed rider schedules use some route numbers that the real-time API simply doesn't publish. A caption below the dropdown now explains this:

```
ℹ️ Routes listed here reflect EMTA's live Avail API feed. Some numbers on printed
rider schedules (e.g. 1 Glenwood, 11 Harborcreek, 15 E 38th) don't appear in the
feed and can't be tracked until EMTA publishes them upstream.
```

---

### 14.8 Adaptive Trend Chart (Overview Tab)

**Problem.** The Overview tab's trend chart always bucketed by calendar day. When the date filter was set to a single day — the new default — only one data point existed: a lone dot, not a line.

**Fix.** The chart granularity is now derived from the selected span at render time:

| Span | Granularity | X-axis format |
|---|---|---|
| 1 day | Hourly (0–23 ET) | `"HH:00"` categorical |
| 2–4 days | 6-hour windows (00 / 06 / 12 / 18 ET) | `"MM/DD HH:00"` datetime |
| 5+ days | Daily | `"Mmm DD"` datetime |

The hourly case uses silver's `hour_of_day` column (already ET-localised) rather than `date_trunc` on the UTC timestamp, so the axis stays timezone-correct without an additional cast. All 24 hour slots are merged against a full `0–23` scaffold in pandas so that hours with no data show as gaps rather than compressed points — this produces a line that spans the full day even when some hours are quiet.

The 6-hour window case constructs a synthetic pandas timestamp as `date + hour_window * 1 hour` (where `hour_window = (hour_of_day // 6) * 6`), keeping the same ET-aligned slots across multi-day ranges.

The bucket key and axis format are chosen before building the SQL so both branches of the "selected metric" logic (on_time vs. early/late/very_late) share identical bucket expressions.

---

### 14.9 Summary Table — Fourth Polish Pass

| Area | File | Fix |
|---|---|---|
| All date filters | `dashboard/app.py` | `(observed_at AT TIME ZONE 'America/New_York')::date` everywhere; fixes phantom future-hour bars |
| City Reliability | `dashboard/app.py` | Per-ping adherence capped at 30 min; score now 47/100 instead of 0/100 |
| City Reliability cache | `dashboard/app.py` | Moved to `live=True` (60 s) to align with ETL cadence |
| Date picker | `dashboard/app.py` | Single-date default with optional "Compare a range" checkbox; no more trailing dash |
| Date caption | `dashboard/app.py` | `YYYY-MM-DD` (single) or `YYYY-MM-DD – YYYY-MM-DD` (range) |
| Worst-route banner | `dashboard/app.py` | Threshold → 100 pings; tiebreak on `AVG(\|adh\|) DESC`; excludes 98/99/999 |
| Route Detail dropdown | `dashboard/app.py` | Sources from bronze (30 d); drops `is_on_route` restriction; Avail-feed caption |
| Heatmap + digest | `dashboard/app.py` | Excludes 98/99/999; reliability cap applied |
| Overview trend chart | `dashboard/app.py` | Adaptive: hourly / 6-hour / daily based on selected span |

Two commits (`2e203dd`, `c7691ae`). No schema changes. No new Anthropic calls.




Why your app works locally but fails on Streamlit Cloud
The most likely culprit, in order of probability:

1. Secrets aren't configured in Streamlit Cloud. Your code uses os.environ["SUPABASE_DB_URL"] and load_dotenv(). Locally, load_dotenv() reads your .env. On Streamlit Cloud there is no .env file — secrets live in the app's Settings → Secrets panel as TOML. If the secret isn't set, the first access of os.environ["SUPABASE_DB_URL"] raises KeyError at import time and the app crashes before the UI even renders.

Fix: In the Streamlit Cloud dashboard, open your app → Settings → Secrets, paste:


SUPABASE_DB_URL = "postgresql://postgres:...@db.xxx.supabase.co:5432/postgres"
ANTHROPIC_API_KEY = "sk-ant-..."
Streamlit Cloud exposes these as environment variables automatically, so your existing os.environ[...] calls will work unchanged.

2. Supabase connection pooler vs direct connection. Streamlit Cloud runs in a shared environment that sometimes has trouble with Supabase's direct 5432 port. If the secret is set correctly and you still see timeouts, switch your SUPABASE_DB_URL from the direct connection string (db.xxx.supabase.co:5432) to the pooler connection string (aws-0-us-east-1.pooler.supabase.com:6543, found in Supabase → Project Settings → Database → Connection pooling). The pooler is IPv4-friendly and far more reliable from shared hosts.

3. Python version mismatch. Your local venv is likely 3.10+, but Streamlit Cloud defaults to whatever the app config says. Add a .python-version file with 3.11 (or whatever matches local) to pin it.

4. The sys.path bootstrap. Your dashboard/app.py has sys.path.insert(0, str(_PROJECT_ROOT)). This works fine on Cloud if the repo root is one level above dashboard/app.py, which it is — so this is probably fine. Only an issue if you set the app entry point incorrectly in Streamlit Cloud's settings.

Most efficient debug path: open the deployed app's Manage app → Logs panel on Streamlit Cloud. The stack trace will tell you in one line whether it's KeyError: 'SUPABASE_DB_URL' (secrets), OperationalError (pooler/IP issue), or something else. Paste the error here and I can pinpoint it.

Why Bronze / Silver / Gold
It's the Medallion architecture — the idea is to never lose raw data, never expose messy data to users, and never re-derive the same aggregate twice.

Layer	Table	What's in it	Why it exists
Bronze	bronze_vehicle_pings	Raw API snapshots + full raw_json backup	Immutable source of truth. If the schema changes, or a bug in Silver deletes good data, you can rebuild from Bronze.
Silver	silver_arrivals	Cleaned: ET-timezone normalized, inactive buses filtered, delay_bucket assigned	Analytics-ready rows. Row-per-ping granularity preserved for flexible queries (per-day, per-hour, per-route).
Gold	gold_route_reliability	Pre-aggregated by (route, hour_of_day, day_of_week) with the weighted reliability score	Cheap, fast reads for dashboards — no GROUP BY over millions of rows every time someone loads the page.
Benefits: (1) raw data is never mutated, so fixes are reproducible; (2) each layer has one job, so bugs are localized; (3) expensive aggregations happen once per pipeline run, not per user query.

Which layer does the daily AI agent read from? Silver. See ai_agent/daily_insights.py:65 and line 96 — both queries hit silver_arrivals. That's the right choice: Gold is a lifetime aggregate (no date column), so you can't ask "how did routes perform on April 22 specifically." Silver has per-ping rows with observed_at, which is exactly what a daily report needs. The weekly agent, by contrast, reads from Gold because it wants the rolling lifetime picture of worst buckets.

---

## 15. The April 26 Session — Reliability Ranking, Prune Overhaul, Weekly Fix

This session fixed three independent bugs and introduced a new chart, then iterated on it through a design challenge. Each subsection documents what broke, why, and the reasoning behind the chosen fix.

---

### 15.1 "Could not load data" on the Overview Route Ranking

**Root cause.** The route reliability ranking query ended with:

```sql
ORDER BY COALESCE(reliability_score, -1) DESC
```

`reliability_score` is a SELECT-list alias defined in the outer query. PostgreSQL can resolve a bare alias in `ORDER BY`, but cannot resolve it *inside an expression* like `COALESCE(alias, -1)` — it interprets the identifier as a column name at the FROM level and raises `UndefinedColumn`. The exception was swallowed by the generic `except Exception: st.error("Could not load data. Check connection.")` handler in `run_query`, so the user saw a connection error rather than a SQL error.

**Fix.** Replaced with `ORDER BY reliability_score DESC NULLS LAST`, which is both valid and the semantically correct behaviour (routes with no usable data fall to the bottom).

**Lesson.** When a generic error handler swallows exceptions, the first debugging step is to replay the query directly against the database. The actual `UndefinedColumn` message made the fix immediately obvious.

---

### 15.2 Storage-Driven Prune (`maintenance/prune_old_data.py`)

**Problem with the old approach.** The prune script ran daily on the Oracle VM with `--days 7`, unconditionally deleting any bronze and silver row older than seven days. The database was at 108 MB out of Supabase's 500 MB free tier — meaning 392 MB of usable history was being discarded to avoid a limit that wasn't close to being hit. The old rationale ("Supabase free tier keeps ~7 days of silver") was a conservative guess made before any real data existed; it became wrong as soon as actual usage proved far lower.

**New behaviour.** The script reads `pg_database_size(current_database())` on every run:

- **Below 400 MB** (80 % of 500 MB): exits as a no-op. All history is preserved.
- **Above 400 MB**: deletes the oldest one calendar day from `bronze_vehicle_pings` and `silver_arrivals` — one day per daily cron run. The incremental approach avoids a big single delete and gives autovacuum time to reclaim pages between runs.
- **Safety floor** of two days: the script refuses to prune below the most recent two days of data, so the live dashboard never loses its immediate history.
- **`--force-days N`**: manual override to reproduce the old time-window behaviour (must be ≥ safety floor).
- **`--vacuum-full`**: opt-in flag that runs `VACUUM FULL` after deleting to reclaim disk space immediately. Not used in the cron because it takes an exclusive lock; reserved for emergency one-shot reclaims.

**Why not loop inside one run until under the target?** `pg_database_size` reports on-disk size, which only shrinks after `VACUUM FULL`. A loop that deletes, checks size, and repeats would always see the same number and either over-delete or loop forever. One-day-per-run with nightly autovacuum is predictable and safe.

`setup_vm.sh` cron line updated from `--days 7` to no arguments. `sql/schema.sql` comment updated to remove the "~7 days of silver" claim.

---

### 15.3 AI Weekly Report Never Firing

**Root cause.** The cron in `.github/workflows/ai_weekly.yml` was `50 3 * * 6`. In cron, `6` is Saturday. Saturday 03:50 UTC is **Friday 23:50 EDT** — before any Saturday service had run. The workflow comment claimed it was "Saturday 23:50 EDT" (a ten-hour error). Combined with the fact that the project was only a week old at the time, the job had fired exactly once — before Saturday service began — and produced no insights because the data window was Friday night.

The `ai_weekly_insights` table had zero rows.

**Fix.** Changed to `0 12 * * 0` — Sunday 12:00 UTC = Sunday 08:00 EDT, after EMTA's full Saturday service has completed. The weekly report was then run manually to fill the gap for the week of 2026-04-20. Claude produced a 3,349-character narrative highlighting Belle Valley's chronic midday delays (avg 174 min late at 11 AM Wednesdays), AM/PM Tripper failures at peak hours, and a probable data-integrity issue with West County - BPS and East County - INF (large negative adherence values suggesting scheduling anomalies rather than genuine early running).

---

### 15.4 Route Reliability Ranking — Colour and Window Design

**Phase 1: Discrete colours.** The ranking bar chart was using a `RdYlGn` continuous colour scale, which doesn't match the rest of the dashboard. Replaced with three discrete buckets using the same hex values used everywhere else: `#22c55e` (green, ≥ 80), `#f59e0b` (amber, 60–79), `#ef4444` (red, < 60), `#6b7280` (grey, no data). A themed legend (dark background, `#e5e7eb` text) matches the maps.

**Phase 2: Window selector — challenge and iteration.**

The original request was for a Live / 1w / 2w / 3w / 4w selector defaulting to Live.

**Challenge raised:** "Live" conflates two different things — cache freshness (60 s vs 300 s TTL) and SQL window length. A reliability ranking with a "live" window of one day is statistically noisy: a single stranded bus can swing a route's score by 20+ points. The other Overview tiles (worst-peak banner, City Reliability KPI) already answer the "what's happening right now?" question. The value of this chart is the multi-day structural reliability picture.

After discussion, the agreed design was:

- Options: **Today** (labelled, not "Live") / 1 week / 2 weeks / 3 weeks / 4 weeks.
- Default: Today.
- Explicit noise caption when "Today" is selected.
- Always use the 60 s cache (`live=True`) so all windows refresh within a minute of each ETL cycle.
- Minimum-ping threshold scales with window: 5 / 10 / 15 / 20 / 25.

**Phase 3: Bind to the sidebar date picker.**

The user then challenged that design: "Users can already choose the date range — why have a separate selector for this one chart?" This is correct. Every other element on the Overview tab is driven by the sidebar date range. A separate window selector for this chart is an inconsistency and a redundant control.

**Challenge raised before implementing:** The sidebar default is a single day, which is the noisiest possible window for a ranking. The dedicated selector had "Today" with an explicit noise caption because it was the *chosen* window; binding to the sidebar means users arrive at the same noisy view without having chosen it. Also, the 30-day permanent window was the only chart on the page giving a long-term structural baseline regardless of the sidebar's setting — removing it loses that view entirely.

**Resolution (Option A):**
- Bind to `filter_start` / `filter_end` from the sidebar.
- Keep the noise caption, but make it auto-fire when `days_in_range <= 2`.
- Scale the minimum-ping threshold continuously: `max(5, 2 × days_in_range)`, so a single-day view admits routes with ≥ 5 pings while a four-week view requires ≥ 56.
- Empty-state info box points users at the sidebar date picker.
- Title and hover label reflect the active range dynamically.
- `live=True` (60 s cache) throughout.

This gives full sidebar consistency while handling the noise and empty-state cases explicitly.

---

### 15.5 Summary Table — April 26 Session

| Area | File | Fix / Change |
|---|---|---|
| Route ranking query | `dashboard/app.py` | `ORDER BY reliability_score DESC NULLS LAST` — fixes `UndefinedColumn` crash |
| Route ranking colours | `dashboard/app.py` | Discrete green/amber/red buckets matching dashboard theme |
| Route ranking window | `dashboard/app.py` | Sidebar-bound; auto-noise caption ≤ 2 days; threshold scales with span |
| Storage-driven prune | `maintenance/prune_old_data.py` | No-op below 400 MB; deletes oldest day when over cap; safety floor 2 days |
| VM cron | `setup_vm.sh` | Drops `--days 7` flag; prune is now self-governing |
| Schema comment | `sql/schema.sql` | Removes stale "~7 days of silver" claim |
| Weekly AI cron | `.github/workflows/ai_weekly.yml` | `50 3 * * 6` → `0 12 * * 0` (Sunday 08:00 EDT vs Friday 23:50 EDT) |
| Weekly AI backfill | `ai_weekly_insights` table | Manual run generated week-of-2026-04-20 report |

No new Anthropic calls per dashboard render. No schema changes. No new dependencies.

---

## 16. The May 2 Session — AI Narrative/KPI Mismatch, End-of-Day Digest, and Streamlit Cloud

This session addressed two bugs that had existed since the digest feature launched, plus a deployment blocker on Streamlit Cloud.

---

### 16.1 AI Narrative Numbers Never Matching the KPI Strip

**The symptom.** The KPI strip above the daily digest showed one on-time percentage (e.g. 71.4%), while the AI-written narrative a few lines below cited a different number (e.g. 73.8%). The mismatch confused readers into thinking the dashboard was broken, and undermined trust in both the data and the AI.

**Root cause — first investigation.** The initial assumption was that the KPI snapshot was being captured *after* the Claude call, so Claude was computing numbers from the same Silver query but at a slightly different moment (Silver rebuilds mid-day as new pings arrive). Moving `fetch_kpi_snapshot()` before the Claude call should have closed the gap — but the numbers still didn't match in testing.

**Root cause — actual.** Claude hallucinated the system-wide statistics despite being given the correct numbers in the prompt. The prompt said "quote these numbers VERBATIM" but Claude's instruction-following is not reliable enough to trust for exact numeric reproduction, especially when the numbers appear deep in a long context alongside a rich data table. Every Claude call — including regenerations — produced its own slightly different summary paragraph, making the mismatch non-deterministic and therefore impossible to fix by adjusting the prompt alone.

**Why `importlib.reload()` was also needed.** During debugging, the Streamlit process (long-running, PID 9552, started at 9:09 PM) had `daily_insights` cached in `sys.modules` from before any code changes. The dashboard's "Regenerate" button was calling `generate_daily_insights()` through the *stale* imported module, not the updated code on disk. This meant every regeneration ran the old logic regardless of what was deployed. Verified by `generation_count` incrementing (4→6) while the numbers stayed wrong — the database was being written to, but by the old code path. Adding `importlib.reload(_di)` to all three regen button handlers (generate, regenerate, force-refresh) ensures the latest version of `daily_insights.py` is executed on every button press, even if the Streamlit process has been alive for hours.

**The fix — three layers.**

**Layer 1: Snapshot before Claude.** `fetch_kpi_snapshot()` is called once, before the Claude API call, and the result is passed into `build_prompt()`. The prompt is rewritten to explicitly say "do not state system-wide on-time percentage or average delay figures — those belong in the KPI panel, not in your narrative." Claude is steered away from the numbers entirely rather than trusted to reproduce them.

**Layer 2: Python writes the summary paragraph.** A new function `build_summary_paragraph(snapshot, is_partial_day)` constructs a factual opening paragraph directly from the frozen snapshot values — no AI involved. The paragraph reads like this:
```
On {date}, {N} buses were tracked across {R} active routes, with a system on-time rate of {OTP}% and an average adherence of {delay} minutes. Service ran from {first_ride} to {last_ride}.
```
Every number in that sentence is taken verbatim from the snapshot dict. Claude never sees or generates this paragraph.

**Layer 3: Defensive scrubber.** `_scrub_and_inject_summary(narrative, snapshot, is_partial_day)` runs after `parse_response()`. It splits the narrative into paragraphs, detects any paragraph that contains a system-stats giveaway phrase (a tuple `_SYSTEM_STATS_GIVEAWAYS` of tell-tale substrings like "on-time rate", "system-wide", "% of buses") **and** a `%` sign, and drops it. The Python-built summary paragraph is then inserted at position 1 (after the hook). This layer catches the case where Claude still tries to write stats despite the prompt instruction — and also protects against future prompt regressions.

**Result.** The narrative now displays exactly the numbers in the KPI strip by construction. The AI writes the hook, the route-level analysis, and the operational insight — the three things it does well — and never touches the system-level statistics.

---

### 16.2 End-of-Day Digest Not Auto-Generating

**The symptom.** The daily digest was supposed to generate automatically when buses stopped each evening, but users noticed it never appeared unless someone manually triggered it from the dashboard or CLI.

**Root cause.** The `--if-idle` flag was implemented in `ai_agent/daily_insights.py` but was never wired into the automated pipeline runners. `run_pipeline.sh` (used by the Oracle VM cron) and `.github/workflows/pipeline.yml` (GitHub Actions) both ended after the `transform.gold` step — neither invoked the daily digest agent at all.

**Fix.** Added the following line to both runners:
```bash
python -m ai_agent.daily_insights --if-idle
```
The `--if-idle` flag is a no-op until (a) the hour is ≥ 22:00 ET, (b) the day is not Sunday, (c) no digest exists for today, and (d) no moving-bus ping has arrived in the last 45 minutes. When all four conditions are met, it generates the digest exactly once. Every subsequent 5-minute cron tick sees the digest already in the database and exits immediately. This means the digest fires automatically from whichever runner first sees the idle window — no manual intervention required.

---

### 16.3 Streamlit Cloud "No Data"

**Root cause.** Two separate issues, both preventing the app from connecting to the database on Streamlit Cloud:

1. **Secrets not found.** The app used `os.environ["SUPABASE_DB_URL"]` which works locally (via `dotenv`) but raises `KeyError` on Cloud where there is no `.env` file. Streamlit Cloud exposes secrets through `st.secrets`, not `os.environ`.

2. **Direct database port blocked.** The `SUPABASE_DB_URL` pointed to Supabase's direct connection on port 5432. Streamlit Cloud's shared infrastructure does not support direct Postgres connections reliably; the pooler endpoint (port 6543) is required.

**Fix — `_get_secret()` and `_promote_secrets_to_env()`.** Added two helper functions to `dashboard/app.py`:

```python
def _get_secret(name: str) -> str | None:
    try:
        return st.secrets[name]
    except Exception:
        return os.environ.get(name)

def _promote_secrets_to_env():
    for key in ("SUPABASE_DB_URL", "ANTHROPIC_API_KEY"):
        val = _get_secret(key)
        if val and key not in os.environ:
            os.environ[key] = val
```

`_promote_secrets_to_env()` runs at module load. It mirrors both secrets from `st.secrets` into `os.environ` so that child modules (`ai_agent/daily_insights.py`, `ingestion/config.py`) that read `os.environ` directly continue to work without modification. The same codebase now works identically on Cloud, locally, and on the Oracle VM.

**Fix — `format_route()` trailing `.0`.** An unrelated but user-visible bug discovered alongside this: route IDs in the `kpi_snapshot` JSONB column were being serialised as JSON numbers (e.g. `5.0`) and deserialised by Python as floats. The `format_route()` function in the dashboard was rendering them as `"5.0 — UPMC Hamot"` instead of `"5 — UPMC Hamot"`. Fixed by adding an integer check:
```python
if isinstance(route_id, float) and route_id.is_integer():
    rid = str(int(route_id))
```

---

### 16.4 Summary Table — May 2 Session

| Area | File | Fix |
|---|---|---|
| AI narrative/KPI mismatch | `ai_agent/daily_insights.py` | Python builds summary paragraph from snapshot; Claude writes hook + route analysis + insight only |
| AI narrative/KPI mismatch | `ai_agent/daily_insights.py` | Defensive scrubber strips any stats paragraph Claude still generates |
| AI narrative/KPI mismatch | `dashboard/app.py` | `importlib.reload(_di)` in all three regen handlers so stale module cache is never used |
| End-of-day auto-digest | `run_pipeline.sh` | Added `python -m ai_agent.daily_insights --if-idle` |
| End-of-day auto-digest | `.github/workflows/pipeline.yml` | Added same `--if-idle` step after gold transform |
| Streamlit Cloud secrets | `dashboard/app.py` | `_get_secret()` reads `st.secrets` first, `os.environ` second |
| Streamlit Cloud secrets | `dashboard/app.py` | `_promote_secrets_to_env()` mirrors secrets into `os.environ` at startup |
| Route ID trailing `.0` | `dashboard/app.py` | `format_route()` strips `.0` from float route IDs deserialised from JSONB |
| Tab titles | `dashboard/app.py` | Centered hero titles added to Daily Digest and Weekly Digest tabs |

No schema changes. One new implicit dependency: Streamlit Cloud must have `SUPABASE_DB_URL` (pooler, port 6543) and `ANTHROPIC_API_KEY` set in Settings → Secrets.

---

## 17. May 11 Session — Sunday Hard-Block, Corridor Map, NULL Route Crash

The May 11 session began as a single bug report ("digest for May 10 failed and didn't make a chart") and uncovered three orthogonal problems: a NULL-coalesce gap that crashed the prompt builder, hallucinated Sunday digests sitting in the database, and a heat map design that compounded route confusion instead of resolving it. All three were fixed in a single commit, plus a cleanup pass on Streamlit 1.50 deprecation warnings and Pylance stub-resolution noise.

### 17.1 The NULL `route_name` Crash

**Symptom.** Generating today's digest from the dashboard returned `"Generation failed: unsupported format string passed to NoneType.__format__"`. The error was opaque enough that the first instinct was a Claude API issue; it wasn't.

**Root cause.** `build_prompt()` in `ai_agent/daily_insights.py` formats the worst-routes table line-by-line with a column-width specifier:
```python
f"  {row['route_name']:<30} OTP={row['on_time_pct']}%  ..."
```
The `:<30` specifier is left-align-to-30-chars padding. Python rejects it when the value is `None`, with the exact `NoneType.__format__` message.

**Where the NULL came from.** `fetch_daily_summary()` selects from `silver_arrivals` grouped by `route_name`. The function had filters for `route_id NOT IN ('98','99','999')` and `speed > 2`, but no `route_name IS NOT NULL` clause. On May 11 there were 13 silver rows from `route_id = 0` with `route_name = NULL` — almost certainly Avail's "no current assignment" sentinel that nonetheless arrived with `is_on_route = TRUE` because both `route_id` and `trip_id` happened to be non-null in the source payload. Those 13 rows collapsed into one NULL group key, and the f-string blew up on the very first iteration.

**Fix.** One-line filter added to `fetch_daily_summary()`:
```sql
AND route_name IS NOT NULL
```
This brings the function in line with `fetch_kpi_snapshot()`'s worst-routes query, which already filtered NULLs. The two queries previously had a subtle divergence in their WHERE clauses — both should have always agreed.

**Verification.** After the fix, `fetch_daily_summary(2026-05-11)` returns 23 rows, zero with NULL `route_name`. Liberty Street is first by avg delay (7.16 min), which matches the corridor map's worst-stretches finding for the same date.

---

### 17.2 The Sunday Hallucination Discovery

**Symptom (chained from 17.1).** After fixing the format crash, a quick scan of the digest archive showed entries for `2026-05-03` and `2026-05-10` — both Sundays. EMTA does not operate on Sundays, so both rows had to be hallucinated.

**How they got there.** Two DB queries confirmed:
```sql
SELECT COUNT(*) FROM bronze_vehicle_pings
 WHERE (observed_at AT TIME ZONE 'America/New_York')::date = '2026-05-10';
-- 0
```
Zero pings on May 10. Same for May 3. The `_run_if_idle()` cron path already had a Sunday guard (`if now_et.weekday() == 6: return`), but it only protected the end-of-day automatic path. Three other entry points — the dashboard "Generate" button, the dashboard "Regenerate" button on past dates, and the `--auto` CLI flag — would all happily call `generate_daily_insights()` with a Sunday `target_date`. With Silver returning zero rows, `fetch_daily_summary()` would return an empty list and trigger a `no_data` return early — *but only after* the NULL crash from 17.1 was fixed. Before the NULL fix, a Sunday with even one stray ping (e.g. a deadhead bus reported during garage hours) could squeeze past the guards and reach Claude, which would then invent a complete digest around a single-bus data point.

The two hallucinated rows in the archive predated the NULL fix and were produced under exactly that mechanism: a thin smear of off-service pings reaching the prompt, Claude filling in the gaps confidently. The narrative for `2026-05-03` cited "Glenwood Route Tanks to 35% Reliability" — but Glenwood is route 1, which doesn't appear in the Avail feed at all (a known upstream gap documented in the Route Detail tab's caption). That's the kind of fabrication that erodes trust in the whole product.

**Fix — hard block in the generator.** Rather than patch each entry point, the guard was moved to the function itself:
```python
if target_date.weekday() == 6:
    log.info("Refusing to generate digest for %s: EMTA does not operate Sundays.", target_date)
    return "no_service_day"
```
This sits at the very top of `generate_daily_insights()`, before the env-var check, before any DB connection, before any Claude call. The function now returns one of six sentinels: `generated`, `regenerated`, `exists`, `no_data`, `missing_env`, and the new `no_service_day`. Every caller — UI, CLI, cron, regenerate flow — inherits the protection automatically.

**Fix — UI surfaces it cleanly.** Both dashboard regenerate handlers and the new-digest "Generate" button now branch on the `no_service_day` sentinel and show `st.info("EMTA does not operate on Sundays — nothing to regenerate for 2026-05-10")` instead of a generic warning. The same dashboard tab also adds an `elif selected_day.weekday() == 6:` branch before the "Generate digest" button so the button never even renders for a Sunday — the user sees the informational message immediately.

**Cleanup.** The two hallucinated rows were deleted with a single targeted query:
```sql
DELETE FROM ai_daily_insights
 WHERE EXTRACT(DOW FROM report_date) = 0;
```
Two rows removed (`2026-05-03`, `2026-05-10`). Zero collateral damage — every non-Sunday digest is preserved.

---

### 17.3 Map Redesign — From Multi-Route Cloud to Single-Route Corridor

**The original map.** The Live Map tab's secondary view ("Show activity by route") rendered every ~100m grid cell where a bus was seen as a colored dot, with:
- `color = route_label` across 27 routes (Light24 palette)
- `size = pings` (popularity / time spent in cell)
- `hover = avg delay`

**Why it was bad.** Three encodings, each fighting the others. Twenty-seven hues exceeded human color-discrimination by roughly a factor of four; adjacent routes were indistinguishable. Size encoded "popular route" and "stuck in traffic" identically — a transfer hub and a bottleneck both produced large dots. The one actually-useful number (avg delay) was buried in the hover, requiring 27 mouse-overs to find a problem area. The result was a city-wide bubble cloud that answered no clear question.

**The intermediate fix (deleted same session).** A first attempt aggregated *across* routes per grid cell, with `color = avg_delay` on a green→amber→red continuous scale. This was correct visually — bottleneck cells lit up red, the rest of the city stayed green — but it discarded the per-route information entirely. A rider asking "where does Route 5 get stuck?" couldn't get an answer because every route was mixed into one cell.

**The final design — corridor view.** Replaces the "show activity by route" toggle with "show route corridor":
1. A **route selectbox** at the top, populated from Silver rows in the selected date range.
2. The map plots **only the chosen route's** grid cells, so the dots naturally trace that route's actual corridor through the city.
3. `color = avg_delay` on the same green→amber→red continuous scale, clamped to `[-5, +15]` minutes so a single very-late outlier doesn't flatten the rest to one hue.
4. `size = pings` for context (how much data underlies each cell).
5. Camera centers on the route's centroid so short downtown loops don't get lost at the default Erie zoom.
6. A **"Top 5 late stretches" table** below the map: avg delay, ping count, and exact lat/lon for each of the worst-performing cells. This gives a dispatcher the bottleneck list numerically without needing to read a colormap.

**Why this works.** It answers exactly one question per render — "where does this route fall behind?" — and answers it well. The legend collapses from 27 swatches to one continuous color bar. The route picker is a single dropdown, not a multi-channel encoding puzzle. The worst-stretches table makes the map self-documenting: even a user who finds the color scale ambiguous can read the same answer from the table beneath it.

**Verification on real data.** Route 3 (Peach Street) on May 11 has 633 silver pings. The corridor view surfaces a cluster of 4-ping cells around (42.13, −80.085) — downtown Peach between roughly 12th and 18th — at +8 to +10 min average delay, with the rest of the corridor showing green and amber. That's a real, actionable signal that the old multi-route map could not produce.

---

### 17.4 Streamlit 1.50 Deprecation Migration

**Symptom.** The archive `st.dataframe` call at the bottom of the Daily Digest tab was emitting `DeprecationWarning: use_container_width=True is deprecated; use width="stretch"` on every rerun. In Streamlit 1.50 this warning surfaces in the UI banner rather than the console, which created a persistent yellow bar at the top of the page.

**Fix.** Migrated the one user-facing dataframe (the digest archive) to the new API:
```python
sel = st.dataframe(
    archive_rows,
    width="stretch",        # was: use_container_width=True
    hide_index=True,
    on_select="rerun",
    selection_mode="single-row",
    key="daily_archive_table",
)
```
The corridor's new "Top 5 late stretches" table also uses `width="stretch"` from the start. The other 13 `use_container_width=True` calls in the file are still on the old API; they'll be migrated in a follow-up pass since they don't generate user-visible UI banners (Streamlit emits the deprecation only for interactive widgets, not static plots).

**A second issue:** Pylance flagged `on_select`, `selection_mode`, and `key` as unknown kwargs because the bundled Streamlit type stubs lag the runtime. Suppressed with `# pyright: ignore[reportCallIssue, reportArgumentType]` on the call. Runtime was always fine — the warnings were purely cosmetic IDE noise.

---

### 17.5 Pylance Stub-Resolution Noise

Three imports were flagged red by Pylance because their type stubs aren't found in the workspace's resolved environment:

| Import | File | Fix |
|---|---|---|
| `from streamlit_autorefresh import st_autorefresh` | `dashboard/app.py` | `# type: ignore[import-not-found]` |
| `import anthropic` | `ai_agent/daily_insights.py` | `# type: ignore[import-not-found]` |
| `st.dataframe(...)` kwargs | `dashboard/app.py` | `# pyright: ignore[reportCallIssue, reportArgumentType]` |

All three were runtime-clean — the packages import and work correctly in the venv. The suppressions are scoped to the specific lines so any future genuine type errors elsewhere in the file remain visible.

---

### 17.6 Summary Table — May 11 Session

| Area | File | Fix |
|---|---|---|
| Format crash on NULL route_name | `ai_agent/daily_insights.py` | `AND route_name IS NOT NULL` added to `fetch_daily_summary()` |
| Sunday hallucinations | `ai_agent/daily_insights.py` | `if target_date.weekday() == 6: return "no_service_day"` at top of `generate_daily_insights()` |
| Sunday hallucinations | `dashboard/app.py` | `elif selected_day.weekday() == 6:` branch + `no_service_day` sentinel handling in both regen handlers |
| Sunday hallucinations | DB | `DELETE FROM ai_daily_insights WHERE EXTRACT(DOW FROM report_date) = 0` (removed 2 hallucinated rows) |
| Activity map redesign | `dashboard/app.py` | Replaced multi-route activity cloud with single-route corridor map + route picker + worst-5 stretches table |
| Streamlit 1.50 deprecation | `dashboard/app.py` | `use_container_width=True` → `width="stretch"` on the archive dataframe |
| Pylance stub noise | `dashboard/app.py`, `ai_agent/daily_insights.py` | Targeted `# type: ignore` / `# pyright: ignore` comments on three import/call sites |

No schema changes. No new dependencies. Two database rows deleted (hallucinated Sunday digests). The Sunday guard is structural — every future entry path will refuse Sundays without re-implementing the check.