# EMTA Transit Reliability Tracker

A live data pipeline and dashboard for the Erie Metropolitan Transit Authority's bus network. Pulls GPS positions from Erie Metro's API every 5 minutes, scores reliability per route per hour per day, and lets a Claude-backed agent write the weekly report.

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/release/python-3100/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-Supabase-336791.svg)](https://supabase.com/)
[![AI](https://img.shields.io/badge/AI-Anthropic_Claude-171515.svg)](https://www.anthropic.com/)
[![Dashboard](https://img.shields.io/badge/Dashboard-Streamlit-FF4B4B.svg)](https://streamlit.io/)

---

## Why I built this

I rode the Erie bus every day and had no idea whether it was going to show up on time. Google Maps gives you live data but no history. Was route 5 late because of today, or is route 5 always late on Friday evenings? There was no answer to that anywhere, so I built one.

The result is a system that quietly collects every bus position every 5 minutes, processes it overnight while nobody is watching, and produces a per-route reliability score you can actually plan around. It also writes its own weekly summary, which is more fun than checking a dashboard.

---

## What it actually does

Every 5 minutes, three Python scripts run in sequence. They hit Erie Metro's Avail InfoPoint API, dump the raw response into PostgreSQL, clean it, and roll it up into a per-route-per-hour-per-day reliability score. A Streamlit dashboard reads from the database. A Claude agent reads the worst buckets and writes a plain-English digest when someone asks for one.

There is no backend server, no message queue, no ORM, no Kubernetes. Three Python scripts, one Postgres database, one Streamlit app, two cron jobs. That is the whole thing.

```
GitHub Actions cron               Oracle Cloud VM cron
        |                                |
        +------- every ~5 min -----------+
                       |
                       v
              Supabase PostgreSQL
              +-------------------+
              |  Bronze (raw)     |
              |  Silver (clean)   |
              |  Gold (aggregated)|
              +-------------------+
                       |
                       v
              Streamlit dashboard
              + Claude agent (on demand)
```

---

## The Medallion architecture

The data moves through three layers, each one progressively cleaner and smaller. The pattern comes from data lakehouse practice — Bronze/Silver/Gold is what Databricks calls it — and it works fine inside a single Postgres database.

**Bronze (`bronze_vehicle_pings`)** is the raw dump. Every API response gets stored as-is, including a full `raw_json` column with the original payload. The philosophy is "lose nothing." If a bug ever corrupts the lower layers, Bronze is the source of truth and Silver can be rebuilt from scratch.

**Silver (`silver_arrivals`)** is the cleaned version. UTC timestamps converted to Eastern Time, parked buses filtered out (speed below 2 mph), each ping tagged with a delay bucket (`early`, `on_time`, `late`, `very_late`). Still row-per-ping so downstream queries can aggregate however they want.

**Gold (`gold_route_reliability`)** is the rollup, grouped by `(route_id, hour_of_day, day_of_week)` with a single composite reliability score per bucket. There is no date column here — Gold is a lifetime aggregate, rewritten every run via `ON CONFLICT (...) DO UPDATE`. The dashboard reads from Gold and gets sub-second responses on aggregates that would otherwise scan millions of Silver rows.

Each layer has one job. If Silver has a bug, Gold is wrong but Bronze is still the source of truth and you rebuild upward. The point of this pattern is that you can never end up in a state where the original data is gone.

---

## The reliability score

```
score = (on_time_pct * 0.7) + (1 - LEAST(AVG(LEAST(30, |adherence|)), 30) / 30) * 100 * 0.3
```

70% of the score is on-time percentage — the share of pings between 1 minute early and 5 minutes late. That's the part a rider feels directly.

The other 30% is the average delay magnitude, scaled 0–100. A route where every bus is 4 minutes late technically clears the on-time threshold but feels worse than a genuinely punctual route. This component catches that.

Two things in this formula are not obvious and matter a lot:

**Why ABS instead of GREATEST.** Adherence is signed: negative is early, positive is late. Early is *worse* for a rider than late, because a bus that leaves before you get there strands you completely; a late bus just makes you wait. Using `GREATEST(x, 0)` would give a chronically 5-minutes-early route a perfect penalty score, which is wrong. ABS treats both directions the same.

**Why the 30-minute cap.** Raw adherence in the Avail feed sometimes hits 200 or 300 minutes — buses with stale counters, ghost trips that never got cleared. Without a cap, that 1% of bad pings drags the citywide average past 12 minutes and pins the entire reliability score at 0/100. Each individual ping is clamped at 30 before averaging. A bus 30 minutes late is a real failure; 200 minutes is data garbage. After this fix went in, the citywide score read 47/100 instead of 0/100.

---

## Why two cron runners

GitHub Actions cron is best-effort. Its `*/5 * * * *` schedule queues into a shared runner pool, and during peak hours it can delay a run by 5 to 15 minutes. For transit data that only exists during a 5 AM to 11 PM service window, a skipped run means permanently lost position pings — there is no way to backfill what you didn't capture.

The Oracle Cloud Always Free VM (1 OCPU, 1 GB RAM) runs a real system crontab on top of that. Millisecond-accurate, no queue. Both runners write to the same Supabase database. Because every insert is idempotent — Silver deletes its window before reinserting, Gold uses `ON CONFLICT DO UPDATE` — duplicate runs from both runners produce zero extra rows. The redundancy is deliberate.

A few cron-specific things bit me during setup:

- `cron` runs without a shell profile, so `python3` is not on the PATH. The crontab line uses the absolute virtualenv path: `~/Public_Transport/venv/bin/python`.
- `python -m ingestion.fetch_realtime` requires the working directory to be the repo root or the package import fails. The crontab prepends `cd $PROJECT_DIR &&` to every command.
- Cron failures are silent unless you redirect output. Every command appends to `~/pipeline.log` with `>> $LOG 2>&1`.

---

## The AI layer

Two Claude agents, different cadences, different data sources.

The **weekly agent** (`ai_agent/insights.py`) queries Gold for the 40 worst-performing route/hour buckets with at least 5 pings, sends them to Claude with a structured prompt, and writes a multi-paragraph narrative plus a tweet draft into `ai_weekly_insights`. Runs Sunday mornings via `ai_weekly.yml`. It reads Gold because it wants the lifetime worst-performance picture, which is what Gold *is*.

The **daily agent** (`ai_agent/daily_insights.py`) queries Silver for a specific date, filtering to moving buses only (`speed > 2`). It writes a narrative, a headline, KPIs, and a tweet draft into `ai_daily_insights`. It reads Silver because Gold doesn't have a date column.

The daily agent is cache-first on demand. Once a date has been generated, every subsequent dashboard view reads from the stored row at zero token cost. New generation only happens when a user clicks the button for a date with no cached row, and that's rate-limited to 5 calls per day per date. Running Claude on a cron N times daily would burn tokens nobody asked for; this pattern keeps the AI feature essentially free in steady state.

The function returns one of five sentinel strings (`generated`, `exists`, `no_data`, `missing_env`, or anything else) instead of raising exceptions, so the Streamlit UI never shows users a stack trace.

---

## The dashboard

Four tabs, one sidebar with a date picker and direction filter.

**Overview** — citywide KPIs (total pings, on-time %, avg delay, active routes, city reliability score), a delay distribution pie chart, and a trend chart that adapts to the date range (hourly for one day, 6-hour blocks for 2–4 days, daily for 5+). The pie chart and trend chart are coupled — clicking a delay bucket slice in the pie reruns the page and filters the trend to that bucket. There's also a "worst peak-hour route in selected range" banner that reads from Silver so it actually responds to the date picker.

**Route Detail** — pick a route, get hourly on-time % bars and average delay bars. Hours are cast to zero-padded strings (`"07"`, `"08"`) and passed through Plotly's `category_orders` so sparse routes render uniform-width bars instead of a continuous-axis mess.

**Live Map** — current vehicle positions on a `carto-darkmatter` basemap, colored by adherence status. Toggleable to "today's activity by route" where each route gets its own color from the `Light24` palette (the only Plotly palette that's bright enough on a dark background). Both views share the same basemap so toggling doesn't flash from white to black.

**AI Digest** — on-demand daily report. Severity banner (red/amber/green based on OTP), four-KPI strip, hourly OTP arc chart, top-3 worst routes, narrative paragraph from Claude. Zero new API calls per render — every element reads from stored database rows or cached Silver aggregates.

A few things that look small but mattered:

- All date comparisons use `(observed_at AT TIME ZONE 'America/New_York')::date`. Raw `observed_at::date` returns UTC, and UTC midnight is 8 PM ET, so pings from 8–11 PM ET get stamped with the wrong calendar date and show up as phantom bars for hours that haven't happened yet. This took a global replace across 17 occurrences once I figured it out.
- Routes 98 (AM Tripper), 99 (PM Tripper), and 999 (Deadhead) are excluded from every analytics query. They're synthetic non-passenger routes with no schedule to adhere to, and including them distorts every aggregate.
- Route lists sort numerically, not alphabetically. Route labels start with the number, so a string sort puts route 105 before route 3, which is how no rider thinks about the network. A small `_route_sort_key` helper parses numeric prefixes and sorts them as ints.

---

## Bugs worth mentioning

The diagnosis is usually the interesting part with this kind of project, so a few of the worse ones:

**Reliability score stuck at 0/100.** Symptom: city reliability read 0 even when most buses were obviously on time. Root cause: ~10% of pings reported absolute adherence over 30 minutes, with a max of 345. The unbounded `AVG(|adherence|)` pulled the citywide average past 12 minutes, which floored the original `100 - 10 * AVG(...)` formula at zero. Fix: `LEAST(30, ABS(adherence_minutes))` per ping before averaging. Score went from 0/100 to 47/100.

**Phantom bars for future hours.** Route Detail charts at 7 PM ET showed authentic-looking bars for hours 20, 21, 22 — hours that hadn't started. UTC vs ET, again. After 8 PM ET it's already tomorrow in UTC, so last night's late-evening pings landed in today's filter. Silver's `hour_of_day` column was correct (it stored the ET hour), which is why the bars looked real. The fix was the global timezone replace mentioned above.

**Heatmap painting central Erie bright red regardless of delay.** `px.density_mapbox` does a 2-D kernel density estimate weighted by the z value, so a thousand on-time pings clustered downtown integrated to a huge weight even though every individual ping was fine. The map was accurately drawing ping density and I was reading it as delay. Switched to `px.scatter_mapbox` with one dot per ping, color keyed to route. Different chart, no KDE distortion.

**Date picker silently running 30-day SQL on single-day picks.** `st.date_input` returns a bare `date` for single-date mode and a tuple for range mode. The sidebar only handled the tuple case and fell back to a 30-day default for everything else, which meant every single-date selection was running 30 days of SQL behind the scenes. Fix: handle all four return shapes (`tuple[2]`, `tuple[1]`, bare `date`, fallback) and collapse single-day picks to `(day, day)`.

---

## Tech stack

| Concern | Tool |
|---|---|
| Data source | Avail InfoPoint REST API |
| ETL | Custom Python (`fetch_realtime.py`, `silver.py`, `gold.py`) |
| Database | PostgreSQL on Supabase |
| DB driver | `psycopg2`, no ORM |
| Dashboard | Streamlit + Plotly Express |
| AI | Anthropic Claude API |
| Cron | GitHub Actions + Oracle Cloud Free VM |
| Secrets | `python-dotenv` locally; encrypted secrets in CI |

There is no Flask, no Django, no FastAPI, no Redis, no Celery, no Airflow. Streamlit is both the frontend and the query layer because there is exactly one writer (the pipeline) and exactly one reader (the dashboard), so a separate REST API would be a moving part with no purpose.

---

## Project layout

```
ingestion/
  fetch_realtime.py     Bronze: poll Avail API, bulk insert raw pings
  config.py             Delay thresholds, excluded routes, service hours
transform/
  silver.py             Silver: timezone, filter, classify, idempotent reinsert
  gold.py               Gold: aggregate by (route, hour, day), upsert
ai_agent/
  insights.py           Weekly digest agent (Gold)
  daily_insights.py     Daily digest agent (Silver, cache-first)
dashboard/
  app.py                Streamlit dashboard, four tabs
sql/
  schema.sql            Append-only schema
maintenance/
  prune_old_data.py     Storage-driven Bronze pruning for the free tier
.github/workflows/
  pipeline.yml          ETL cron, every 5 minutes
  ai_weekly.yml         Weekly digest cron, Sunday mornings
  ai_daily.yml          Daily digest workflow
```


---

## What I'd do with more time

A few things on the list:

- **GTFS schedule integration.** Cross-referencing scheduled trips with live adherence would let the pipeline discard ghost trips more precisely than the current speed filter.
- **Threshold alerts.** A lightweight notification when a route drops below some reliability number during peak hours. Useful for transit advocates more than riders.
- **Daily pre-aggregation table.** Silver grows month over month and the daily AI agent is going to start scanning slowly. A daily summary table would keep both the Claude context and the queries fast.


---

## Limitations, honestly

- The polling interval is 5 minutes, so anything faster than that is invisible. A bus that was 30 seconds late and then on time again never registered.
- No weather, no holidays, no school calendar. A snowstorm and a normal Tuesday look identical to the database.
- The live map shows the current moment only — there's no playback of where buses were at 4 PM yesterday.
- Adherence values come straight from Erie Metro's system. If their counters are wrong or stale (and sometimes they are), the scores inherit that noise.

This is a reliability tracker, not a real-time navigator. It won't tell you when the next bus is coming. It'll tell you which one to actually trust.
