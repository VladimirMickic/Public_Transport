# EMTA Transit Reliability Tracker

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/release/python-3100/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-Supabase-336791.svg)](https://supabase.com/)
[![AI](https://img.shields.io/badge/AI-Anthropic_Claude-171515.svg)](https://www.anthropic.com/)
[![Dashboard](https://img.shields.io/badge/Dashboard-Streamlit-FF4B4B.svg)](https://streamlit.io/)

An automated ETL pipeline, interactive dashboard, and AI-driven insights engine for tracking the performance and reliability of the Erie Metropolitan Transit Authority (EMTA) bus network.

This project pulls live vehicle positions via the Avail InfoPoint API, transforms the data using a strict Medallion Architecture, and provides deep analytical visibility through a Streamlit dashboard and Anthropic's Claude 3.5 Sonnet.

## 🚀 Key Features

*   **Live Data Ingestion**: Continuous 5-minute polling of EMTA vehicle positions via a robust dual-runner architecture (GitHub Actions + Oracle Cloud VM).
*   **Medallion Architecture**: Strict separation of raw data (Bronze), cleaned & time-zoned data (Silver), and aggregated routing performance (Gold) in a Supabase PostgreSQL database.
*   **Interactive Dashboard**: A full-featured Streamlit dashboard providing:
    *   Live system maps and vehicle adherence tracking.
    *   Dynamic route-level reliability heatmaps and historical trend lines.
    *   Peak-hour analytics and delay distributions.
*   **AI-Powered Insights**: Independent AI agents that query the database to write pinpoint-accurate human-readable summaries without hallucination.
    *   *Weekly Reports*: Automatically generated every Sunday from historical Gold data.
    *   *Daily Digests*: On-demand daily performance reviews integrated directly into the dashboard.

## 🏗️ Architecture

The pipeline follows a classic **Medallion Architecture** pattern to guarantee data integrity and idempotency:

1.  **Bronze (`bronze_vehicle_pings`)**: Raw, unfiltered JSON and timestamp snapshots ingested directly from the EMTA API. *"Dump everything, lose nothing."*
2.  **Silver (`silver_arrivals`)**: Timezone-adjusted and categorized data. Filters out inactive buses, assigns delay buckets ("early", "on_time", "late"), and ensures idempotency through rolling window replacement.
3.  **Gold (`gold_route_reliability`)**: Highly compressed, high-value aggregated metrics grouped by route, hour, and day of the week. Employs a weighted reliability score formula, updated continuously via `UPSERT`.

## 🛠️ Technology Stack

*   **Language**: Python 3.10+
*   **Database**: PostgreSQL (hosted on Supabase)
*   **AI Inference**: Anthropic Claude 3.5 Sonnet API
*   **Frontend**: Streamlit, Plotly
*   **Automation/DevOps**: GitHub Actions, Oracle Cloud Always Free VM (Crontab)

## ⚙️ Getting Started

### Prerequisites

*   Python 3.10 or higher
*   A Supabase project with PostgreSQL
*   Anthropic API Key

### Installation

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/yourusername/emta-transit-tracker.git
    cd emta-transit-tracker
    ```

2.  **Set up the virtual environment:**
    ```bash
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
    ```

3.  **Configure environment variables:**
    Create a `.env` file in the root directory:
    ```env
    SUPABASE_DB_URL=postgresql://user:password@host:port/postgres
    ANTHROPIC_API_KEY=your-anthropic-api-key
    ```

### Running the Application

**Run the Dashboard Locally:**
```bash
streamlit run dashboard/app.py
```

**Run the ETL Pipeline Manually:**
```bash
python -m ingestion.fetch_realtime
python -m transform.silver --days-back 1
python -m transform.gold
```

**Run the AI Agents Manually:**
```bash
python -m ai_agent.insights
python -m ai_agent.daily_insights --date 2026-04-18
```

## 🔄 Automation

The system is designed to run completely hands-off:
*   **ETL Pipeline**: Triggers every 5 minutes (`*/5 * * * *`) via GitHub Actions and an Oracle Cloud VM backup to ensure zero downtime.
*   **AI Weekly Digest**: Runs via a separate GitHub Action cron job (`0 8 * * 0`) every Sunday at 8:00 AM ET.

## 📖 Deep Dive

For a comprehensive code walkthrough and an explanation of the design decisions, SQL performance optimizations, and the dual-runner cron architecture, please read our [Explanation Document](Explanation.md) and [CLAUDE.md](CLAUDE.md).

---
*Built to bring transparency and data-driven insights to public transit in Erie, PA.*
