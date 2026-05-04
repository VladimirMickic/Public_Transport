# EMTA Transit Reliability Tracker

A live data pipeline and dashboard for the Erie Metropolitan Transit Authority bus network. Pulls GPS positions from EMTA's API every 5 minutes, scores reliability per route per hour per day, and lets a Claude backed agent write the daily and weekly digests.

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/release/python-3100/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-Supabase-336791.svg)](https://supabase.com/)
[![AI](https://img.shields.io/badge/AI-Anthropic_Claude-171515.svg)](https://www.anthropic.com/)
[![Dashboard](https://img.shields.io/badge/Dashboard-Streamlit-FF4B4B.svg)](https://streamlit.io/)

Live dashboard: [emta-tracker.streamlit.app](https://erietransit.streamlit.app/) · Auto-deploys from `main`.

---

## Why I built this

I rode the Erie bus every day and had no idea whether it would show up on time. Google Maps gives you a live ETA but no history. So when route 5 was 11 minutes late on a Friday, I had no way to know if that was a bad afternoon or a bad route.

There was no answer to that anywhere. So I built one.

The result is a system that collects every bus position every 5 minutes, scores per-route reliability using the same on-time performance formula transit authorities use nationally, and writes its own daily and weekly summaries when the buses stop running. It is also the most I have ever learned in a single project about timezone bugs.

---

## What's in here

A short tour for anyone scanning before reading:

- A working medallion data pipeline (Bronze, Silver, Gold) inside one Postgres database, fully idempotent.
- Two redundant cron runners that share state through database idempotency, not coordination.
- A Claude integration that costs roughly nothing because every digest is cache-first on demand.
- A Streamlit dashboard with five tabs, all reading from the same database, all timezone correct.
- A maintenance script that prunes by storage usage instead of age, so the free tier lasts as long as possible.

If a section below sounds interesting, it is probably that section.

---

## How it works

Every 5 minutes, three Python scripts run in sequence. They hit EMTA's Avail InfoPoint API, dump the raw response into PostgreSQL, clean it, and roll it up into a per-route-per-hour-per-day on-time performance score. A Streamlit dashboard reads from the database. A Claude agent reads the worst buckets and writes a plain English digest when someone asks for one, or once at end-of-service automatically.

There is no backend server, no message queue, no ORM, no Kubernetes. Three Python scripts, one Postgres database, one Streamlit app, two cron runners. That is the whole thing.

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
              + Claude agent (cache-first)
```

---

## The medallion architecture

The data moves through three layers, each one progressively cleaner and smaller. The pattern comes from data lakehouse practice (Bronze, Silver, Gold is what Databricks calls it) and it works fine inside a single Postgres database.

**Bronze (`bronze_vehicle_pings`)** is the raw dump. Every API response gets stored as is, including a full `raw_json` column with the original payload. The philosophy is "lose nothing." If a bug ever corrupts the lower layers, Bronze is the source of truth and Silver can be rebuilt from scratch.

**Silver (`silver_arrivals`)** is the cleaned version. UTC timestamps converted to Eastern Time, parked buses filtered out (speed below 2 mph), each ping tagged with a delay bucket (`early`, `on_time`, `late`, `very_late`). Still row-per-ping so downstream queries can aggregate however they want.

**Gold (`gold_route_reliability`)** is the rollup, grouped by `(route_id, hour_of_day, day_of_week)` with OTP% (on-time performance percentage) per bucket. There is no date column here. Gold is a lifetime aggregate, rewritten every run via `ON CONFLICT (...) DO UPDATE`. The dashboard reads from Gold and gets sub-second responses on aggregates that would otherwise scan millions of Silver rows.

Each layer has one job. If Silver has a bug, Gold is wrong but Bronze is still the source of truth and you rebuild upward. The point of the pattern is that you can never end up in a state where the original data is gone.

---

## The reliability score

Every reliability number in this project uses the same formula that transit authorities across North America report: **On-Time Performance percentage (OTP%)**.

```
OTP% = (on-time observations / total observations) × 100
```

This is the industry standard metric defined in **TCRP Report 88** (*A Guidebook for Developing a Transit Performance-Measurement System*), published by the Transportation Research Board. There is no single federal mandate, but TCRP Report 88 is what agencies like WMATA, AC Transit, TriMet, and dozens of others base their service standards on.

**What counts as "on time."** A bus ping is classified as on time if its schedule adherence falls within **1 minute early to 5 minutes late**. This matches the most common threshold used across the industry. A bus that departs 2 minutes early strands any rider who planned around the posted schedule, so it does not count as on time. A bus 6 minutes late has crossed the threshold where riders start missing transfers, so it does not count either.

The full delay classification (defined in `ingestion/config.py`):

| Bucket | Adherence window |
|---|---|
| `early` | More than 1 minute early |
| `on_time` | 1 minute early to 5 minutes late |
| `late` | 5 to 15 minutes late |
| `very_late` | More than 15 minutes late |

The Silver layer tags every ping with one of these buckets. Gold, the dashboard, and the AI agents all compute OTP% the same way: count the `on_time` pings, divide by total pings, multiply by 100. One formula, everywhere, benchmarkable against any peer agency that uses the same TCRP standard.

---

## Why two cron runners

GitHub Actions cron is best effort. Its `*/5 * * * *` schedule queues into a shared runner pool, and during peak hours it can delay a run by 5 to 15 minutes. For transit data that only exists during a 5 AM to 11 PM service window, a skipped run means permanently lost position pings. There is no way to backfill what you didn't capture.

The Oracle Cloud Always Free VM (1 OCPU, 1 GB RAM) runs a real system crontab on top of that. Millisecond accurate, no queue. Both runners write to the same Supabase database. Because every insert is idempotent (Silver deletes its window before reinserting, Gold uses `ON CONFLICT DO UPDATE`), duplicate runs from both runners produce zero extra rows. The redundancy is deliberate.

A few cron-specific things bit me during setup:

- `cron` runs without a shell profile, so `python3` is not on the PATH. The crontab line uses the absolute virtualenv path: `~/Public_Transport/venv/bin/python`.
- `python -m ingestion.fetch_realtime` requires the working directory to be the repo root or the package import fails. The crontab prepends `cd $PROJECT_DIR &&` to every command.


---

## The AI layer

Two Claude agents, different cadences, different data sources.

The **weekly agent** (`ai_agent/insights.py`) queries Gold for the 40 worst-performing route/hour buckets with at least 5 pings, sends them to Claude with a structured prompt, and writes a multi-paragraph narrative plus a tweet draft and headline into `ai_weekly_insights`. Runs Sunday mornings via `ai_weekly.yml`. It reads Gold because it wants the lifetime worst-performance picture, which is what Gold *is*.

The **daily agent** (`ai_agent/daily_insights.py`) queries Silver for a specific date, filtering to moving buses only (`speed > 2`). It writes a narrative, a headline, KPIs, and a tweet draft into `ai_daily_insights`.

The daily agent is **cache-first on demand**. Once a date has been generated, every subsequent dashboard view reads from the stored row at zero token cost. New generation only happens when the buses stop running for the day (auto), or when a user clicks **Regenerate** on a digest that came out wrong (manual). Running Claude on a cron N times daily would burn tokens nobody asked for; this pattern keeps the AI feature essentially free in steady state.

Two things I had to build because Claude wouldn't behave:

**A KPI snapshot freeze.** Every digest stamps a `kpi_snapshot` JSONB blob with the exact numbers Claude saw at generation time. The dashboard renders the narrative, KPI tiles, hourly arc, and worst routes table all from this same blob. Without it, the narrative says "OTP 68.1%" and the KPI strip says "69.3%" five minutes later because the 5 minute ETL rebuilt Silver in between. The snapshot makes the digest a frozen artefact, live numbers live on the Overview tab.

**A scrub-and-inject pass.** Claude was instructed in the prompt not to cite system-wide totals, because it would hallucinate them every time (saw 72.2% in the prompt, wrote 73.8% in the output). It ignored the instruction. So the post processing detects any paragraph that smells like a system stats paragraph (giveaway phrases plus a percent sign), drops it, and inserts a deterministic summary built from the snapshot. The narrative now cannot disagree with the KPI strip, mathematically.

The function returns one of five sentinel strings (`generated`, `regenerated`, `exists`, `no_data`, `missing_env`) instead of raising exceptions, so the Streamlit UI never shows users a stack trace.

---

## The dashboard

Five tabs, one sidebar with a date picker and a direction filter.

**Overview** is the citywide snapshot. KPI strip (total pings, on-time %, avg delay, active routes), a delay distribution pie, an adaptive trend chart that switches granularity based on the selected range (hourly for one day, 6-hour blocks for 2–4 days, daily after that), and a horizontal bar chart ranking every route by OTP% for the chosen window. There is also a "worst peak hour route" callout that reads from Silver, so it actually responds to the date picker instead of repeating yesterday's banner forever.

**Route Detail** lets you pick a single route and see hourly on time % and average delay broken out side by side. Hours are cast to zero-padded strings (`"07"`, `"08"`) and passed through Plotly's `category_orders` so sparse routes render uniform width bars instead of a continuous axis mess.

**Live Map** shows current vehicle positions on a `carto-darkmatter` basemap, colored by adherence status. Toggleable to a "today's activity by route" view where each route gets its own color from the `Light24` palette (the only Plotly palette bright enough on a dark background).

**Daily Digest** is the per day frozen artefact. Severity banner (green, amber, red based on OTP), KPI strip, hourly arc, top 3 worst routes, and the Claude narrative. Past dates always render from the saved snapshot. A "Regenerate" expander re rolls a hallucinated digest and bumps a `generation_count` audit column.

**Weekly Digest** is the same shape but reads `ai_weekly_insights`, with daily OTP bars instead of hourly ones.

A few things that look small but mattered:

- All date comparisons use `(observed_at AT TIME ZONE 'America/New_York')::date`. Raw `observed_at::date` returns UTC, and UTC midnight is 8 PM ET, so pings from 8–11 PM ET get stamped with the wrong calendar date and show up as phantom bars for hours that haven't happened yet. This took a global replace across 17 occurrences once I figured it out.
- Routes 98 (AM Tripper), 99 (PM Tripper), and 999 (Deadhead) are excluded from every analytics query. They're synthetic non-passenger routes with no schedule to adhere to, and including them distorts every aggregate.
- Route lists sort numerically, not alphabetically. Route labels start with the number, so a string sort puts route 105 before route 3, which is how no rider thinks about the network. A small `_route_sort_key` helper parses numeric prefixes and sorts them as ints.

---

## Bugs worth mentioning

The diagnosis is usually the interesting part with this kind of project. A few of the worse ones:

**Date picker silently running 30-day SQL on single-day picks.** `st.date_input` returns a bare `date` for single date mode and a tuple for range mode. The sidebar only handled the tuple case and fell back to a 30-day default for everything else, which meant every single-date selection was running 30 days of SQL behind the scenes. Fix: handle all four return shapes (`tuple[2]`, `tuple[1]`, bare `date`, fallback) and collapse single-day picks to `(day, day)`.

**Daily digest narrative drifting from KPIs by 1–2 percentage points.** Claude saw a number when it generated, the dashboard re queried Silver when a user opened the page later, and the 5-minute ETL had landed new rows in between. Claude was right at the moment of writing, the KPI strip was right at the moment of viewing, both were "correct," and the page looked broken. Fix described in *The AI layer* above: freeze a snapshot at generation time, render every visible number from the snapshot, never re query.

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
| Secrets | `python-dotenv` locally; encrypted secrets in CI; `st.secrets` on Streamlit Cloud |

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
  daily_insights.py     Daily digest agent (Silver, cache-first, snapshot-frozen)
dashboard/
  app.py                Streamlit dashboard, five tabs
sql/
  schema.sql            Append-only schema
maintenance/
  prune_old_data.py     Storage-driven Bronze pruning for the free tier
.github/workflows/
  pipeline.yml          ETL cron, every 5 minutes
  ai_daily.yml          Daily digest backup workflow
  ai_weekly.yml         Weekly digest cron, Sunday mornings
setup_vm.sh             One-shot Oracle Cloud VM bootstrap script
```

---

## Running it locally

```bash
git clone https://github.com/VladimirMickic/Public_Transport.git
cd Public_Transport
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# .env at the repo root needs two lines:
#   SUPABASE_DB_URL=postgres://...
#   ANTHROPIC_API_KEY=sk-ant-...

python -m ingestion.fetch_realtime         # poll API, write Bronze
python -m transform.silver --days-back 1   # clean into Silver
python -m transform.gold                   # aggregate into Gold

python -m ai_agent.daily_insights --date 2026-04-22   # one daily digest
python -m ai_agent.insights                            # the weekly digest

streamlit run dashboard/app.py             # open the dashboard
```

---

## What I'd do with more time

- **GTFS schedule integration.** Cross referencing scheduled trips with live adherence would let the pipeline discard ghost trips more precisely than the current speed filter.
- **Threshold alerts.** A lightweight notification when a route drops below some reliability number during peak hours. Useful for transit advocates more than riders.
- **Daily pre-aggregation table.** Silver grows month over month and the daily AI agent will eventually start scanning slowly. A daily summary table would keep both the Claude context and the queries fast.

---

## Limitations

- The polling interval is 5 minutes, so anything faster than that is invisible. A bus that was 30 seconds late and then on time again never registered.
- No weather, no holidays, no school calendar. A snowstorm and a normal Tuesday look identical to the database.
- The live map shows the current moment only. There is no playback of where buses were at 4 PM yesterday.
- Adherence values come straight from EMTA's system. If their counters are wrong or stale (and sometimes they are), the scores inherit that noise.

This is a reliability tracker, not a real-time navigator. It won't tell you when the next bus is coming. It'll tell you which one to actually trust.
