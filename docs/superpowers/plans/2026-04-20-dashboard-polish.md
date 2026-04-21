# Dashboard & AI Agent Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix two real gaps in the already-generated dashboard and AI agent: missing `@st.cache_data` caching (the `ttl` param in `run_query` is accepted but never used for Streamlit caching) and a stale model ID in the AI agent.

**Architecture:** The dashboard's `run_query` wrapper must be split into private cached functions (two TTLs: 3600s for historical data, 60s for live bronze) and a public wrapper that handles errors and delegates. `@st.cache_data` requires hashable params — `RealDictRow` objects are converted to plain `dict`. The AI agent uses an old date-versioned model ID that should be the current alias.

**Tech Stack:** Python 3.9, Streamlit, psycopg2, Plotly Express, Anthropic Python SDK

---

## File Map

| File | Change |
|------|--------|
| `dashboard/app.py:54-65` | Replace single `run_query` with two private `@st.cache_data` functions + public wrapper |
| `dashboard/app.py:376` | Change `run_query(live_sql)` → `run_query(live_sql, live=True)` |
| `ai_agent/insights.py:194` | Update model ID `claude-sonnet-4-20250514` → `claude-sonnet-4-6` |

---

### Task 1: Fix `@st.cache_data` caching in `dashboard/app.py`

**Files:**
- Modify: `dashboard/app.py:54-65`

The current `run_query` function accepts a `ttl` parameter but is a plain Python function — no Streamlit caching happens. Every Streamlit rerender hits the DB.

The fix: two private `@st.cache_data` functions (different TTLs), plus a public `run_query` wrapper that handles errors and converts params to tuples.

- [ ] **Step 1: Replace the `run_query` block** (`dashboard/app.py` lines 54-65)

Replace:
```python
def run_query(sql, params=None, ttl=3600):
    """Run a read query and return list of dicts. Wrapped for error handling."""
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params or ())
            return cur.fetchall()
    except Exception:
        # Reset the cached connection on failure
        get_conn.clear()
        st.error("Could not load data. Check connection.")
        return []
```

With:
```python
@st.cache_data(ttl=3600)
def _run_query_cached(sql: str, params: tuple) -> list[dict]:
    conn = get_conn()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


@st.cache_data(ttl=60)
def _run_live_query_cached(sql: str, params: tuple) -> list[dict]:
    conn = get_conn()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def run_query(sql: str, params=None, live: bool = False) -> list[dict]:
    """Cached DB read. Use live=True for bronze (60 s TTL), default is 3600 s."""
    try:
        p = tuple(params or [])
        if live:
            return _run_live_query_cached(sql, p)
        return _run_query_cached(sql, p)
    except Exception:
        get_conn.clear()
        st.error("Could not load data. Check connection.")
        return []
```

- [ ] **Step 2: Update the bronze live query call** (`dashboard/app.py`, the live map section)

Find the line:
```python
        live = run_query(live_sql)
```

Change to:
```python
        live = run_query(live_sql, live=True)
```

- [ ] **Step 3: Verify the app starts without error**

```bash
cd "/Users/hugorabbit/Public Transport" && source venv/bin/activate && streamlit run dashboard/app.py --server.headless true &
sleep 5 && curl -s http://localhost:8501 | head -20
pkill -f "streamlit run"
```

Expected: HTTP 200, no import errors in the terminal output.

- [ ] **Step 4: Commit**

```bash
git add dashboard/app.py
git commit -m "fix: add @st.cache_data caching to dashboard DB queries

Historical queries cache for 1 h; live bronze queries cache for 60 s.
Converts RealDictRow to plain dict for cache serialisation.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 2: Update model ID in `ai_agent/insights.py`

**Files:**
- Modify: `ai_agent/insights.py:194`

The script uses `claude-sonnet-4-20250514` (old date-versioned alias). The current model ID for Claude Sonnet 4.6 is `claude-sonnet-4-6`.

- [ ] **Step 1: Update the model string**

Find:
```python
        model="claude-sonnet-4-20250514",
```

Replace with:
```python
        model="claude-sonnet-4-6",
```

- [ ] **Step 2: Verify the file is syntactically valid**

```bash
cd "/Users/hugorabbit/Public Transport" && source venv/bin/activate && python -c "import ai_agent.insights; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add ai_agent/insights.py
git commit -m "fix: update Claude model ID to claude-sonnet-4-6

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage check:**

| Spec requirement | Covered |
|-----------------|---------|
| Sidebar date range + direction filters | ✅ Already in app.py |
| Worst peak-hour route callout (st.warning) | ✅ Already in app.py |
| 10 worst days table on Route Detail | ✅ Already in app.py |
| Live Map toggle current/heatmap | ✅ Already in app.py |
| `@st.cache_data` ttl=3600 / ttl=60 | ✅ **This plan** (Task 1) |
| Footer | ✅ Already in app.py |
| headline_text st.info banner in AI Digest | ✅ Already in app.py |
| Tweet ≤280 char truncation | ✅ Already in insights.py |
| headline_text column ADD IF NOT EXISTS | ✅ Already in insights.py |
| Once-per-week guard | ✅ Already in insights.py |
| Prompt requires 3 routes + 2 time windows | ✅ Already in insights.py |
| Raw Claude response logged | ✅ Already in insights.py |
| Model ID current | ✅ **This plan** (Task 2) |

**Placeholder scan:** None found.

**Type consistency:** `run_query` public signature unchanged at call sites (only `live=True` kwarg added for one call). `params` coerced to `tuple` inside the wrapper — call sites can keep passing lists.
