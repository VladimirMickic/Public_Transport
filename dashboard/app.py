"""
EMTA Transit Reliability Dashboard
───────────────────────────────────
Four-tab Streamlit app powered by Supabase (silver/gold/ai tables).

Usage:
    streamlit run dashboard/app.py
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

# Make the project root importable when Streamlit launches this file directly
# (`streamlit run dashboard/app.py` only adds dashboard/ to sys.path).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import plotly.express as px
import streamlit as st
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

# ── Page config ──────────────────────────────────────────
st.set_page_config(
    page_title="EMTA Transit Tracker",
    page_icon="🚌",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Auto-refresh every 5 minutes ─────────────────────────
# Matches the ETL cadence (GitHub Action runs every 5 min), so a user
# leaving the tab open always sees fresh numbers without reloading.
try:
    from streamlit_autorefresh import st_autorefresh
    st_autorefresh(interval=5 * 60 * 1000, key="dashboard_autorefresh")
except ImportError:
    st.sidebar.caption(
        "⚠️ `streamlit-autorefresh` not installed — run "
        "`pip install streamlit-autorefresh` for live 5-min updates."
    )

# ── Plotly theme defaults ────────────────────────────────
PLOTLY_LAYOUT = dict(
    plot_bgcolor="rgba(0,0,0,0)",
    paper_bgcolor="rgba(0,0,0,0)",
    font=dict(family="Inter, sans-serif"),
    margin=dict(l=40, r=20, t=40, b=40),
)

COLORS = {
    "on_time": "#22c55e",
    "early": "#3b82f6",
    "late": "#f59e0b",
    "very_late": "#ef4444",
}


def format_route(route_id, route_name) -> str:
    """Format route as '<bus#> — <name>'. Locals know buses by number first."""
    rid = "" if route_id is None else str(route_id).strip()
    rname = "" if route_name is None else str(route_name).strip()
    if rid and rname and rid != rname:
        return f"{rid} — {rname}"
    return rname or rid or "Unknown route"

def render_kpi(label: str, value: str, color: str, help_text: str = ""):
    st.markdown(f"""
        <div title="{help_text}" style="text-align: center; background-color: rgba(24, 26, 32, 0.8);
                    border-radius: 8px; padding: 15px; border: 1px solid {color};
                    box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
            <p style="margin: 0; font-size: 14px; color: #a1a1aa; font-weight: 600;">{label}</p>
            <p style="margin: 0; font-size: 28px; color: {color}; font-weight: bold;">{value}</p>
        </div>
    """, unsafe_allow_html=True)


# ── DB connection ────────────────────────────────────────
@st.cache_resource
def get_conn():
    """Single shared DB connection (cached across reruns)."""
    return psycopg2.connect(os.environ["SUPABASE_DB_URL"], connect_timeout=10)


@st.cache_data(ttl=300)
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
    """Cached DB read.

    - Default TTL is 300 s so the dashboard KPIs refresh each time the 5-min
      ETL cycle lands new rows (the auto-refresh timer fires at the same
      cadence, so a user leaving the tab open always sees current numbers).
    - `live=True` drops to 60 s for bronze/live-vehicle queries.
    """
    try:
        p = tuple(params or [])
        if live:
            return _run_live_query_cached(sql, p)
        return _run_query_cached(sql, p)
    except Exception:
        get_conn.clear()
        st.error("Could not load data. Check connection.")
        return []


# ── Sidebar filters ──────────────────────────────────────
st.sidebar.image("https://emta.availtec.com/InfoPoint/Content/images/logo.png", width=180)
st.sidebar.title("Filters")

today = date.today()
# Streamlit's range-mode date_input always renders with a trailing "–"
# even when both handles sit on the same day, which looks broken for a
# single-day pick. Default to single-date mode; users who want a range
# tick the checkbox and get a proper Start/End pair.
compare_range = st.sidebar.checkbox(
    "Compare a range",
    value=False,
    help="On: pick a start and end date. Off: single-day view.",
)
if compare_range:
    range_start = st.sidebar.date_input(
        "Start date",
        value=today - timedelta(days=7),
        max_value=today,
        key="range_start",
    )
    range_end = st.sidebar.date_input(
        "End date",
        value=today,
        max_value=today,
        key="range_end",
    )
    # Normalise regardless of which way the user dragged the handles.
    filter_start, filter_end = (
        (range_start, range_end) if range_start <= range_end else (range_end, range_start)
    )
else:
    picked = st.sidebar.date_input(
        "Date",
        value=today,
        max_value=today,
        key="single_date",
    )
    # date_input in single mode returns a bare `date`, but belt-and-braces.
    filter_start = filter_end = picked if isinstance(picked, date) else today

direction_choice = st.sidebar.radio(
    "Direction",
    options=["All", "Inbound", "Outbound"],
    horizontal=False,
)

# Build direction SQL fragment. Pings with NULL/blank direction (buses on
# layover, circular shuttles, or rows the Avail API left unclassified) are
# excluded from Inbound/Outbound so those two totals reflect real directional
# service. They stay visible under "All".
direction_filter_sql = ""
direction_params = []
if direction_choice == "Inbound":
    direction_filter_sql = "AND UPPER(LEFT(direction, 1)) = %s"
    direction_params = ["I"]
elif direction_choice == "Outbound":
    direction_filter_sql = "AND UPPER(LEFT(direction, 1)) = %s"
    direction_params = ["O"]

if direction_choice != "All":
    st.sidebar.caption(
        "ℹ️ Inbound/Outbound exclude pings with no direction data from the "
        "Avail API (typically buses on layover or circular routes). "
        "Switch to **All** to see every ping."
    )

st.sidebar.markdown("---")
st.sidebar.caption("Data sourced from EMTA Avail API")
st.sidebar.caption("Updated every 5 minutes")
st.sidebar.caption("Built by Vladimir · [GitHub](https://github.com/VladimirMickic/Public_Transport)")


# ── Title ────────────────────────────────────────────────
st.title("🚌 EMTA Transit Reliability Tracker")
st.caption("Erie Metropolitan Transit Authority · Real-time performance analytics")

# ── Tabs ─────────────────────────────────────────────────
tab_overview, tab_route, tab_map, tab_digest = st.tabs(
    ["📊 Overview", "🔍 Route Detail", "🗺️ Live Map", "🤖 AI Digest"]
)


# ══════════════════════════════════════════════════════════
# TAB 1: Overview
# ══════════════════════════════════════════════════════════
with tab_overview:
    st.subheader("System Performance Overview")

    # Single-day pick reads "2026-04-21"; a range reads "2026-04-01 – 2026-04-21".
    if filter_start == filter_end:
        st.caption(f"📅 {filter_start.strftime('%Y-%m-%d')}")
    else:
        st.caption(
            f"📅 {filter_start.strftime('%Y-%m-%d')} – "
            f"{filter_end.strftime('%Y-%m-%d')}"
        )

    # ── Key metrics from silver (date-filtered) ──────────
    # The citywide reliability score uses the same symmetric-adherence
    # formula as the Gold table and the worst-route banner: 100 minus
    # 10× the average absolute deviation, clamped to [0, 100], computed
    # only over moving buses (speed > 2). That way every reliability
    # number on the page — city, route, route×hour — is comparable.
    metrics_sql = f"""
        SELECT
            COUNT(*) AS total_pings,
            ROUND(AVG(adherence_minutes)::numeric, 1) AS avg_delay,
            ROUND(
                COUNT(*) FILTER (WHERE delay_bucket = 'on_time') * 100.0
                / NULLIF(COUNT(*), 0), 1
            ) AS on_time_pct,
            COUNT(DISTINCT route_id) AS active_routes,
            GREATEST(0, ROUND(
                (100 - LEAST(
                    100,
                    AVG(LEAST(30, ABS(adherence_minutes))) FILTER (
                        WHERE speed > 2 AND adherence_minutes IS NOT NULL
                    ) * 10
                ))::numeric, 1
            )) AS city_reliability
        FROM silver_arrivals
        WHERE (observed_at AT TIME ZONE 'America/New_York')::date BETWEEN %s AND %s
        {direction_filter_sql}
    """
    # live=True (60s TTL) so the city KPI strip reflects each 5-min ETL
    # batch as soon as it lands, instead of waiting out a 5-min cache that
    # can drift out of phase with the autorefresh timer.
    metrics = run_query(
        metrics_sql,
        [filter_start, filter_end] + direction_params,
        live=True,
    )

    if metrics and metrics[0]["total_pings"] and metrics[0]["total_pings"] > 0:
        m = metrics[0]
        
        otp = float(m['on_time_pct']) if m['on_time_pct'] is not None else 0
        otp_color = "#22c55e" if otp >= 80 else "#f59e0b" if otp >= 65 else "#ef4444"
        
        delay = float(m['avg_delay']) if m['avg_delay'] is not None else 0
        delay_color = "#22c55e" if delay <= 2 else "#f59e0b" if delay <= 5 else "#ef4444"
        
        rel = m['city_reliability']
        if rel is None:
            rel_color = "#3b82f6"
            rel_val = "—"
        else:
            rel_val = f"{rel}/100"
            rel_float = float(rel)
            rel_color = "#22c55e" if rel_float >= 80 else "#f59e0b" if rel_float >= 65 else "#ef4444"

        c1, c2, c3, c4, c5 = st.columns(5)
        with c1:
            render_kpi("Total Pings", f"{m['total_pings']:,}", "#3b82f6")
        with c2:
            render_kpi("On-Time %", f"{m['on_time_pct']}%", otp_color)
        with c3:
            render_kpi("Avg Delay", f"{m['avg_delay']} min", delay_color)
        with c4:
            render_kpi("Active Routes", str(m["active_routes"]), "#3b82f6")
        with c5:
            render_kpi(
                "City Reliability", 
                rel_val, 
                rel_color, 
                "Citywide reliability score over the selected range. 100 minus 10× average |adherence| in minutes, moving buses only. Each ping's |adherence| is clamped at 30 min first, so a handful of buses parked with stale adherence can't drag the whole city score to zero. Updates each 5-min ETL cycle."
            )

        # ── Delay bucket breakdown ───────────────────────
        bucket_sql = f"""
            SELECT delay_bucket, COUNT(*) AS cnt
            FROM silver_arrivals
            WHERE (observed_at AT TIME ZONE 'America/New_York')::date BETWEEN %s AND %s
            {direction_filter_sql}
            GROUP BY delay_bucket
        """
        buckets = run_query(bucket_sql, [filter_start, filter_end] + direction_params)
        if buckets:
            fig_bucket = px.pie(
                buckets,
                values="cnt",
                names="delay_bucket",
                color="delay_bucket",
                color_discrete_map=COLORS,
                title="Delay Distribution",
            )
            fig_bucket.update_layout(**PLOTLY_LAYOUT)

            col_pie, col_trend = st.columns([1, 2])
            with col_pie:
                st.plotly_chart(
                    fig_bucket,
                    use_container_width=True,
                    key="delay_bucket_pie",
                )

            # Pie-slice click selection via on_select is unreliable for
            # pie charts, so drive the trend chart with an explicit
            # radio above it. Options are built from the actual buckets
            # present in the current filter, so empty categories don't
            # appear.
            bucket_options = ["on_time"] + [
                b["delay_bucket"] for b in buckets
                if b["delay_bucket"] and b["delay_bucket"] != "on_time"
            ]
            with col_trend:
                selected_bucket = st.radio(
                    "Trend metric",
                    options=bucket_options,
                    format_func=lambda b: b.replace("_", " ").title() + " %",
                    horizontal=True,
                    key="trend_bucket_choice",
                )

            # Adaptive granularity: a single day gets an hourly arc,
            # medium ranges (2–4 days) get 6-hour windows that align to the
            # natural morning/midday/evening/overnight frame, and longer
            # ranges roll up to daily points. Prevents the "one dot" result
            # that a day-granularity query produces on a single-day filter.
            span_days = (filter_end - filter_start).days + 1
            if span_days <= 1:
                grain = "hour"
            elif span_days <= 4:
                grain = "6h"
            else:
                grain = "day"

            # Every branch returns rows with (bucket, pct). The bucket
            # expression is different per grain; the metric expression is
            # different depending on whether the user picked on_time vs
            # early/late/very_late.
            if selected_bucket and selected_bucket != "on_time":
                metric_sql = (
                    "ROUND(COUNT(*) FILTER (WHERE delay_bucket = %s) * 100.0"
                    " / NULLIF(COUNT(*), 0), 1) AS pct"
                )
                metric_params = [selected_bucket]
                pretty_metric = selected_bucket.replace("_", " ").title()
                line_color = COLORS.get(selected_bucket, "#22c55e")
            else:
                metric_sql = (
                    "ROUND(COUNT(*) FILTER (WHERE delay_bucket = 'on_time') * 100.0"
                    " / NULLIF(COUNT(*), 0), 1) AS pct"
                )
                metric_params = []
                pretty_metric = "On-Time"
                line_color = COLORS["on_time"]

            if grain == "hour":
                # Use the silver hour_of_day column (already ET-local) for a
                # 24-bin hourly arc. Return integer hour 0–23; we format it
                # to a "HH:00" category axis in Python.
                trend_sql = f"""
                    SELECT hour_of_day AS bucket_key,
                           {metric_sql}
                    FROM silver_arrivals
                    WHERE (observed_at AT TIME ZONE 'America/New_York')::date BETWEEN %s AND %s
                    {direction_filter_sql}
                    GROUP BY hour_of_day
                    ORDER BY hour_of_day
                """
                trend_title = f"Hourly {pretty_metric} %"
            elif grain == "6h":
                # 6-hour windows anchored at 00/06/12/18 ET. Dividing
                # hour_of_day by 6 via integer math gives the window index,
                # then * 6 gets the start hour.
                trend_sql = f"""
                    SELECT (observed_at AT TIME ZONE 'America/New_York')::date AS bucket_day,
                           (hour_of_day / 6) * 6 AS bucket_hour,
                           {metric_sql}
                    FROM silver_arrivals
                    WHERE (observed_at AT TIME ZONE 'America/New_York')::date BETWEEN %s AND %s
                    {direction_filter_sql}
                    GROUP BY bucket_day, bucket_hour
                    ORDER BY bucket_day, bucket_hour
                """
                trend_title = f"{pretty_metric} % — 6-hour windows"
            else:
                trend_sql = f"""
                    SELECT (observed_at AT TIME ZONE 'America/New_York')::date AS bucket_day,
                           {metric_sql}
                    FROM silver_arrivals
                    WHERE (observed_at AT TIME ZONE 'America/New_York')::date BETWEEN %s AND %s
                    {direction_filter_sql}
                    GROUP BY bucket_day
                    ORDER BY bucket_day
                """
                trend_title = f"Daily {pretty_metric} %"

            trend_params = metric_params + [filter_start, filter_end] + direction_params
            trend = run_query(trend_sql, trend_params)

            with col_trend:
                if trend:
                    import pandas as pd
                    df_trend = pd.DataFrame(trend)
                    if grain == "hour":
                        df_trend["x_label"] = df_trend["bucket_key"].apply(
                            lambda h: f"{int(h):02d}:00"
                        )
                        # Force all 24 slots so the line spans the full day
                        # even when some hours have no data. Missing hours
                        # plot as gaps, not compressed points.
                        full_hours = pd.DataFrame({
                            "x_label": [f"{h:02d}:00" for h in range(24)],
                        })
                        df_trend = full_hours.merge(df_trend, on="x_label", how="left")
                        x_col, x_label = "x_label", "Hour (ET)"
                        xaxis_cfg = dict(
                            type="category",
                            categoryorder="array",
                            categoryarray=[f"{h:02d}:00" for h in range(24)],
                        )
                    elif grain == "6h":
                        df_trend["bucket_ts"] = pd.to_datetime(
                            df_trend["bucket_day"]
                        ) + pd.to_timedelta(df_trend["bucket_hour"], unit="h")
                        x_col, x_label = "bucket_ts", "Window start (ET)"
                        xaxis_cfg = dict(type="date", tickformat="%m/%d %H:00")
                    else:
                        df_trend["bucket_ts"] = pd.to_datetime(df_trend["bucket_day"])
                        x_col, x_label = "bucket_ts", "Date"
                        xaxis_cfg = dict(type="date", tickformat="%b %d")

                    fig_trend = px.line(
                        df_trend, x=x_col, y="pct",
                        title=trend_title,
                        labels={x_col: x_label, "pct": f"{pretty_metric} %"},
                        markers=True,
                    )
                    fig_trend.update_layout(
                        **PLOTLY_LAYOUT,
                        xaxis=xaxis_cfg,
                        yaxis=dict(range=[0, 100]),
                    )
                    fig_trend.update_traces(
                        line=dict(color=line_color, width=3),
                        marker=dict(size=8, color=line_color),
                        connectgaps=False,
                    )
                    st.plotly_chart(fig_trend, use_container_width=True)

        # ── Worst peak-hour route callout ────────────────
        # Query silver directly (not the lifetime-aggregated gold table) so
        # the banner reflects the active date range AND refreshes each 5-min
        # ETL cycle. Formula mirrors the gold reliability score: symmetric
        # adherence penalty (10 points per minute of |lateness or earliness|,
        # capped at 100). Only moving buses (speed > 2) count, matching the
        # rest of the dashboard's convention.
        #
        # HAVING COUNT(*) >= 100 (was 30) — a single rush hour across a range
        # needs a real sample before we label it "worst"; below that we get
        # tripper / detour noise. Tie-breaker is AVG(|adherence|) DESC so
        # when multiple routes bottom out at 0/100 the banner names the one
        # that's *actually* the most off schedule, not whichever the planner
        # happened to return first.
        # Reliability score uses LEAST(30, |adh|) — clamping each ping at
        # 30 min prevents a handful of buses stranded with stale adherence
        # (we've seen values up to 345 min) from pinning every rush-hour
        # route to 0/100. The displayed avg_abs_delay stays *uncapped* so
        # the operator still sees the true magnitude, not the scoring proxy.
        worst_sql = f"""
            SELECT route_id, route_name, hour_of_day,
                   GREATEST(0, ROUND(
                       (100 - LEAST(100, AVG(LEAST(30, ABS(adherence_minutes))) * 10))::numeric, 2
                   )) AS reliability_score,
                   ROUND(AVG(ABS(adherence_minutes))::numeric, 1) AS avg_abs_delay,
                   ROUND(AVG(adherence_minutes)::numeric, 1) AS avg_signed_delay,
                   COUNT(*) AS total_pings
            FROM silver_arrivals
            WHERE (observed_at AT TIME ZONE 'America/New_York')::date BETWEEN %s AND %s
              AND hour_of_day IN (7, 8, 16, 17, 18)
              AND speed > 2
              AND adherence_minutes IS NOT NULL
              AND route_id NOT IN ('98', '99', '999')
              {direction_filter_sql}
            GROUP BY route_id, route_name, hour_of_day
            HAVING COUNT(*) >= 100
            ORDER BY reliability_score ASC, avg_abs_delay DESC
            LIMIT 1
        """
        worst = run_query(
            worst_sql,
            [filter_start, filter_end] + direction_params,
            live=True,
        )
        if worst:
            w = worst[0]
            hour_label = f"{int(w['hour_of_day']):02d}:00"
            route_label = format_route(w["route_id"], w["route_name"])
            signed = w["avg_signed_delay"]
            direction_word = "late" if signed is not None and signed >= 0 else "early"
            st.warning(
                f"⚠️ **Worst peak-hour route** (in selected range): "
                f"{route_label} at {hour_label} — reliability "
                f"**{w['reliability_score']}**/100 "
                f"· avg {abs(signed) if signed is not None else '—'} min "
                f"{direction_word} across {w['total_pings']:,} pings"
            )

        # ── Route reliability heatmap ────────────────────
        # Compute reliability per route×hour from silver within the active
        # date range (rather than reading the lifetime gold table) so every
        # route that ran any service in the window shows up — including
        # low-volume shuttles and trippers that gold previously filtered out.
        # Formula matches the worst-route banner above.
        heat_sql = f"""
            SELECT route_id, route_name, hour_of_day,
                   GREATEST(0, ROUND(
                       (100 - LEAST(100, AVG(LEAST(30, ABS(adherence_minutes))) * 10))::numeric, 2
                   )) AS reliability_score,
                   COUNT(*) AS total_pings
            FROM silver_arrivals
            WHERE (observed_at AT TIME ZONE 'America/New_York')::date BETWEEN %s AND %s
              AND speed > 2
              AND adherence_minutes IS NOT NULL
              AND route_name IS NOT NULL
              AND route_id NOT IN ('98', '99', '999')
              {direction_filter_sql}
            GROUP BY route_id, route_name, hour_of_day
            HAVING COUNT(*) >= 2
            ORDER BY route_id, hour_of_day
        """
        heat_data = run_query(
            heat_sql,
            [filter_start, filter_end] + direction_params,
            live=True,
        )
        if heat_data:
            import pandas as pd
            df_heat = pd.DataFrame(heat_data)
            df_heat["route_label"] = df_heat.apply(
                lambda r: format_route(r["route_id"], r["route_name"]), axis=1
            )
            # Sort route_label rows by numeric route_id where possible,
            # else alphabetically. Without this, pandas sorts by string and
            # puts "105" before "3" on the Y-axis.
            def _route_sort_key(rid):
                rid_s = "" if rid is None else str(rid).strip()
                try:
                    return (0, int(rid_s), rid_s)
                except (TypeError, ValueError):
                    return (1, 0, rid_s.lower())
            route_order = [
                r["route_label"] for r in sorted(
                    df_heat[["route_id", "route_label"]]
                        .drop_duplicates().to_dict("records"),
                    key=lambda r: _route_sort_key(r["route_id"]),
                )
            ]
            pivot = df_heat.pivot_table(
                index="route_label", columns="hour_of_day",
                values="reliability_score", aggfunc="mean"
            ).reindex(route_order)
            fig_heat = px.imshow(
                pivot,
                color_continuous_scale="RdYlGn",
                aspect="auto",
                title="Reliability Score by Route × Hour",
                labels=dict(x="Hour of Day", y="Route", color="Score"),
            )
            fig_heat.update_layout(**PLOTLY_LAYOUT)
            st.plotly_chart(fig_heat, use_container_width=True)
    else:
        st.info("No data available for the selected date range. "
                "The pipeline collects data during EMTA service hours (6 AM–11 PM ET).")


# ══════════════════════════════════════════════════════════
# TAB 2: Route Detail
# ══════════════════════════════════════════════════════════
with tab_route:
    st.subheader("Route Detail Analysis")

    # Dropdown is sourced from bronze (last 30 days) so every route the
    # Avail API has actually reported in-service shows up — not just the
    # ones with adherence data in silver. 98 (AM Tripper), 99 (PM Tripper)
    # and 999 (dead-head repositioning) are filtered out because they
    # aren't rider-facing services.
    #
    # NOTE: we can only surface routes that Avail publishes. EMTA's printed
    # schedules use some route numbers (e.g. 1 Glenwood, 11 Harborcreek,
    # 15 E 38th) that never appear in the live API feed. If you're
    # expecting a route here that isn't listed, it's missing upstream —
    # see the Avail-feed caption at the top of this tab.
    routes_sql = """
        SELECT DISTINCT route_id, route_name
        FROM bronze_vehicle_pings
        WHERE route_name IS NOT NULL
          AND route_id NOT IN ('98', '99', '999')
          AND observed_at >= NOW() - INTERVAL '30 days'
    """
    routes = run_query(routes_sql)

    st.caption(
        "ℹ️ Routes listed here reflect EMTA's live Avail API feed. "
        "Some numbers on printed rider schedules (e.g. 1 Glenwood, "
        "11 Harborcreek, 15 E 38th) don't appear in the feed and can't "
        "be tracked until EMTA publishes them upstream."
    )

    if routes:
        # Sort routes by numeric route_id when possible (so the dropdown
        # reads 3, 5, 14, 105 instead of 105, 14, 3 like a string sort
        # would produce). Non-numeric IDs (e.g. "PM Tripper") fall to the
        # end, alphabetised among themselves.
        def _route_sort_key(r):
            rid = "" if r.get("route_id") is None else str(r["route_id"]).strip()
            try:
                return (0, int(rid), rid)
            except (TypeError, ValueError):
                return (1, 0, rid.lower())
        routes = sorted(routes, key=_route_sort_key)

        # Label routes as "<bus#> — <name>" so locals can pick them by
        # number (which is how EMTA signage + riders refer to them).
        route_options = {
            format_route(r["route_id"], r["route_name"]): r["route_id"]
            for r in routes
        }
        selected_route_label = st.selectbox("Select Route", list(route_options.keys()))
        selected_route_id = route_options[selected_route_label]
        # Keep the plain name available for chart titles that read better without the prefix.
        selected_route_name = selected_route_label

        # ── Hourly on-time % for this route ──────────────
        hourly_sql = f"""
            SELECT hour_of_day,
                   ROUND(
                       COUNT(*) FILTER (WHERE delay_bucket = 'on_time') * 100.0
                       / NULLIF(COUNT(*), 0), 1
                   ) AS on_time_pct,
                   ROUND(AVG(adherence_minutes)::numeric, 1) AS avg_delay,
                   COUNT(*) AS pings
            FROM silver_arrivals
            WHERE route_id = %s
              AND (observed_at AT TIME ZONE 'America/New_York')::date BETWEEN %s AND %s
              {direction_filter_sql}
            GROUP BY hour_of_day
            ORDER BY hour_of_day
        """
        hourly = run_query(
            hourly_sql,
            [selected_route_id, filter_start, filter_end] + direction_params,
        )

        if hourly:
            # Coerce hour_of_day to a zero-padded string ("00" … "23") so
            # Plotly treats the axis as categorical. Numeric bars auto-pick a
            # width based on the tiny data spread for sparse routes, which
            # produced the pencil-thin bars and the axis running to hour 60
            # in earlier builds. Category bars are uniform and full-width.
            hour_labels = [f"{int(h['hour_of_day']):02d}" for h in hourly]
            for row, lbl in zip(hourly, hour_labels):
                row["hour_label"] = lbl
            hour_order = sorted({h["hour_label"] for h in hourly})

            col_h1, col_h2 = st.columns(2)
            with col_h1:
                fig_h = px.bar(
                    hourly, x="hour_label", y="on_time_pct",
                    title=f"{selected_route_name} — On-Time % by Hour",
                    labels={"hour_label": "Hour", "on_time_pct": "On-Time %"},
                    color="on_time_pct",
                    color_continuous_scale="Blues",
                    category_orders={"hour_label": hour_order},
                )
                fig_h.update_layout(
                    **PLOTLY_LAYOUT,
                    bargap=0.15,
                    yaxis=dict(range=[0, 100]),
                    xaxis=dict(type="category"),
                )
                st.plotly_chart(fig_h, use_container_width=True)

            with col_h2:
                fig_d = px.bar(
                    hourly, x="hour_label", y="avg_delay",
                    title=f"{selected_route_name} — Avg Delay by Hour",
                    labels={"hour_label": "Hour", "avg_delay": "Avg Delay (min)"},
                    color_discrete_sequence=["#f59e0b"],
                    category_orders={"hour_label": hour_order},
                )
                fig_d.update_layout(
                    **PLOTLY_LAYOUT,
                    bargap=0.15,
                    xaxis=dict(type="category"),
                )
                st.plotly_chart(fig_d, use_container_width=True)

        # ── Bucket breakdown for this route ──────────────
        rbucket_sql = f"""
            SELECT delay_bucket, COUNT(*) AS cnt
            FROM silver_arrivals
            WHERE route_id = %s
              AND (observed_at AT TIME ZONE 'America/New_York')::date BETWEEN %s AND %s
              {direction_filter_sql}
            GROUP BY delay_bucket
        """
        rbuckets = run_query(
            rbucket_sql,
            [selected_route_id, filter_start, filter_end] + direction_params,
        )
        if rbuckets:
            fig_rb = px.pie(
                rbuckets, values="cnt", names="delay_bucket",
                color="delay_bucket", color_discrete_map=COLORS,
                title=f"{selected_route_name} — Delay Breakdown",
            )
            fig_rb.update_layout(**PLOTLY_LAYOUT)
            st.plotly_chart(fig_rb, use_container_width=True)

        # ── 3 worst days for this route ──────────────────
        # Always scans the last 7 days regardless of the sidebar date filter,
        # so a single-day selection doesn't collapse this into 1 row. On a
        # free Supabase plan we only retain ~7 days of silver anyway, so this
        # query naturally maps to "all available history for this route".
        st.markdown("#### 3 Worst Days (last 7 days)")
        worst_days_sql = """
            SELECT (observed_at AT TIME ZONE 'America/New_York')::date AS day,
                   COUNT(*) AS pings,
                   ROUND(AVG(adherence_minutes)::numeric, 1) AS avg_delay,
                   ROUND(
                       COUNT(*) FILTER (WHERE delay_bucket = 'on_time') * 100.0
                       / NULLIF(COUNT(*), 0), 1
                   ) AS on_time_pct,
                   COUNT(*) FILTER (WHERE delay_bucket = 'very_late') AS very_late_count
            FROM silver_arrivals
            WHERE route_id = %s
              AND (observed_at AT TIME ZONE 'America/New_York')::date
                  >= (NOW() AT TIME ZONE 'America/New_York')::date - INTERVAL '7 days'
            GROUP BY day
            HAVING COUNT(*) >= 3
            ORDER BY on_time_pct ASC
            LIMIT 3
        """
        worst_days = run_query(worst_days_sql, [selected_route_id])
        if worst_days:
            st.dataframe(worst_days, use_container_width=True)
        else:
            st.info("Not enough data for worst-days analysis yet.")
    else:
        st.info("No route data available yet. Check back after the pipeline has collected data.")


# ══════════════════════════════════════════════════════════
# TAB 3: Live Map
# ══════════════════════════════════════════════════════════
with tab_map:
    st.subheader("Live Vehicle Map")

    map_mode = st.toggle("Show activity by route (uses sidebar date filter)", value=False)

    if not map_mode:
        # ── Current vehicles (last 15 min of bronze) ─────
        st.caption(
            "Showing vehicles from the last 15 minutes. "
            "The sidebar date picker does not apply here — this view is always live. "
            "The Direction filter does apply."
        )
        live_sql = f"""
            SELECT vehicle_id, route_id, route_name, latitude, longitude,
                   adherence_minutes, display_status, speed, vehicle_name,
                   observed_at
            FROM bronze_vehicle_pings
            WHERE observed_at >= NOW() - INTERVAL '15 minutes'
              AND latitude IS NOT NULL
              AND longitude IS NOT NULL
              AND route_id NOT IN ('98', '99', '999')
              {direction_filter_sql}
        """
        live = run_query(live_sql, direction_params, live=True)

        if live:
            for v in live:
                v["route_label"] = format_route(v.get("route_id"), v.get("route_name"))
                # Derive a clean status bucket from adherence so "Very Late"
                # gets its own red dot. Avail's DisplayStatus only emits
                # "On Time" / "Early" / "Late", which collapses the worst
                # delays into the same amber as a 6-min late bus.
                adh = v.get("adherence_minutes")
                if adh is None:
                    v["status_bucket"] = "Unknown"
                elif adh < -1:
                    v["status_bucket"] = "Early"
                elif adh <= 5:
                    v["status_bucket"] = "On Time"
                elif adh <= 15:
                    v["status_bucket"] = "Late"
                else:
                    v["status_bucket"] = "Very Late"
            fig_map = px.scatter_mapbox(
                live,
                lat="latitude",
                lon="longitude",
                hover_name="route_label",
                hover_data=["vehicle_name", "adherence_minutes", "display_status", "speed"],
                color="status_bucket",
                category_orders={"status_bucket": ["On Time", "Early", "Late", "Very Late", "Unknown"]},
                color_discrete_map={
                    "On Time": "#22c55e",
                    "Early": "#3b82f6",
                    "Late": "#f59e0b",
                    "Very Late": "#ef4444",
                    "Unknown": "#6b7280",
                },
                zoom=11,
                height=600,
                title="Live EMTA Vehicles",
            )
            fig_map.update_layout(
                # Both map views (live + today's activity) share the same
                # darkish-gray basemap so switching the toggle doesn't flash
                # between a light page and a near-black page. Dark-matter is
                # the darkest neutral Plotly provides without a Mapbox token.
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
            st.plotly_chart(fig_map, use_container_width=True)
            latest = max((v["observed_at"] for v in live if v.get("observed_at")), default=None)
            if latest is not None:
                latest_et = latest.astimezone().strftime("%H:%M:%S")
                st.caption(f"🕑 Most recent ping: {latest_et} · {len(live)} vehicles shown")
        else:
            st.info("No live vehicles right now. Buses typically run 6 AM – 10 PM ET on weekdays.")

    else:
        # ── Route activity map from silver — honours sidebar date filter ──
        if filter_start == filter_end:
            activity_caption_date = filter_start.strftime("%b %d, %Y")
            activity_title = f"Activity by Route — {activity_caption_date}"
        else:
            activity_caption_date = (
                f"{filter_start.strftime('%b %d')} – {filter_end.strftime('%b %d, %Y')}"
            )
            activity_title = f"Activity by Route — {activity_caption_date}"
        st.caption(
            f"Each dot is a grid cell where a route was seen ({activity_caption_date}) — "
            "color = route, size = ping count, hover = avg delay."
        )
        heat_sql = f"""
            SELECT
                route_id,
                route_name,
                ROUND(latitude::numeric, 3) AS lat_grid,
                ROUND(longitude::numeric, 3) AS lon_grid,
                COUNT(*) AS pings,
                ROUND(AVG(adherence_minutes)::numeric, 1) AS avg_delay
            FROM silver_arrivals
            WHERE (observed_at AT TIME ZONE 'America/New_York')::date
                  BETWEEN %s AND %s
              AND latitude IS NOT NULL
              AND longitude IS NOT NULL
              AND route_name IS NOT NULL
              AND route_id NOT IN ('98', '99', '999')
              {direction_filter_sql}
            GROUP BY route_id, route_name, lat_grid, lon_grid
            HAVING COUNT(*) >= 2
            ORDER BY route_id
        """
        heat = run_query(heat_sql, [filter_start, filter_end] + direction_params)

        if heat:
            for r in heat:
                r["route_label"] = format_route(r.get("route_id"), r.get("route_name"))
            fig_hm = px.scatter_mapbox(
                heat,
                lat="lat_grid",
                lon="lon_grid",
                color="route_label",
                size="pings",
                size_max=18,
                zoom=11,
                height=600,
                title=activity_title,
                # Light24 is designed for dark backgrounds — each hue stays
                # bright and readable against the dark-matter basemap, so
                # routes that were hard to spot with Dark24 (16, 14, etc.)
                # now pop off the map.
                color_discrete_sequence=px.colors.qualitative.Light24,
                hover_data={"route_label": True, "pings": True, "avg_delay": True,
                            "lat_grid": False, "lon_grid": False},
            )
            fig_hm.update_layout(
                mapbox_style="carto-darkmatter",
                mapbox_center={"lat": 42.129, "lon": -80.085},
                legend=dict(
                    title=dict(text="Route", font=dict(color="#e5e7eb")),
                    yanchor="top", y=1.0,
                    xanchor="left", x=1.02,
                    bgcolor="rgba(24, 26, 32, 0.85)",
                    bordercolor="rgba(120, 120, 130, 0.5)",
                    borderwidth=1,
                    font=dict(size=11, color="#e5e7eb"),
                ),
                **PLOTLY_LAYOUT,
            )
            st.plotly_chart(fig_hm, use_container_width=True)
        else:
            st.info(f"No activity data for {activity_caption_date}.")


# ══════════════════════════════════════════════════════════
# TAB 4: AI Digest
# ══════════════════════════════════════════════════════════
with tab_digest:
    st.subheader("🤖 AI Weekly Digest")

    # ── Latest headline banner ───────────────────────────
    headline_sql = """
        SELECT headline_text, week_start
        FROM ai_weekly_insights
        WHERE headline_text IS NOT NULL
        ORDER BY week_start DESC
        LIMIT 1
    """
    headline = run_query(headline_sql)
    if headline and headline[0]["headline_text"]:
        st.info(f"📰 **{headline[0]['headline_text']}**")

    # ── All weekly insights ──────────────────────────────
    insights_sql = """
        SELECT week_start, narrative, tweet_draft, headline_text, created_at
        FROM ai_weekly_insights
        ORDER BY week_start DESC
        LIMIT 12
    """
    insights = run_query(insights_sql)

    if insights:
        for ins in insights:
            with st.expander(
                f"Week of {ins['week_start']} — {ins.get('headline_text', '')}",
                expanded=(ins == insights[0]),
            ):
                st.markdown(ins["narrative"])
                # Tweet drafts remain in ai_weekly_insights for internal use
                # but are not surfaced in the dashboard UI — the share text
                # is an operator tool, not a rider-facing artefact.
                st.caption(f"Generated {ins['created_at']}")
    else:
        st.info(
            "📊 **Not enough data yet.** The tracker is still young — the "
            "weekly digest activates automatically once we've collected a "
            "full week of service data. Check back soon."
        )

    st.markdown("---")
    st.subheader("📅 AI Daily Digest")
    st.caption("Metrics exclude pings where the bus is parked (speed ≤ 2 mph).")

    # ── Pick a date and fetch/generate its digest ────────
    # Use ET (not UTC / server-local) so "today" flips at midnight in Erie.
    today_et_date = datetime.now(ZoneInfo("America/New_York")).date()
    picker_max = today_et_date
    picker_min = picker_max - timedelta(days=60)
    selected_day = st.date_input(
        "Pick a date",
        value=picker_max,
        min_value=picker_min,
        max_value=picker_max,
        key="daily_digest_date",
    )
    is_today = selected_day == today_et_date

    existing = run_query(
        "SELECT report_date, narrative, tweet_draft, headline_text, created_at, "
        "generation_count "
        "FROM ai_daily_insights WHERE report_date = %s",
        [selected_day],
    )

    if existing:
        ins = existing[0]

        # ── KPI strip + severity banner ──────────────────────
        # Pull the day's moving-bus metrics straight from silver so we can
        # present hard numbers *above* the AI narrative. This costs zero
        # extra Anthropic tokens — it's just another cached SQL read. The
        # OTP % also drives a severity colour stripe that frames the
        # headline (red / amber / green), giving the digest visual weight
        # at a glance without any additional AI calls.
        kpi_sql = """
            SELECT
                ROUND(
                    COUNT(*) FILTER (WHERE delay_bucket = 'on_time') * 100.0
                    / NULLIF(COUNT(*), 0), 1
                ) AS otp_pct,
                ROUND(AVG(adherence_minutes)::numeric, 1) AS avg_delay,
                COUNT(DISTINCT route_id) AS active_routes,
                COUNT(*) AS total_pings,
                COUNT(*) FILTER (WHERE delay_bucket = 'very_late') AS very_late
            FROM silver_arrivals
            WHERE (observed_at AT TIME ZONE 'America/New_York')::date = %s
              AND speed > 2
        """
        kpi_rows = run_query(kpi_sql, [selected_day])
        kpi = kpi_rows[0] if kpi_rows else None

        severity_color = "#6b7280"
        severity_label = "📊 Digest"
        if kpi and kpi.get("otp_pct") is not None:
            otp_val = float(kpi["otp_pct"])
            if otp_val < 70:
                severity_color, severity_label = "#ef4444", "🔴 Poor day"
            elif otp_val < 85:
                severity_color, severity_label = "#f59e0b", "🟡 Mixed day"
            else:
                severity_color, severity_label = "#22c55e", "🟢 Strong day"

        headline_txt = ins.get("headline_text") or "Daily summary"
        st.markdown(
            f"<div style='padding: 16px 20px; border-left: 4px solid {severity_color}; "
            f"background: rgba(255,255,255,0.04); border-radius: 6px; margin: 8px 0 18px;'>"
            f"<div style='font-size:0.78em; color:#9ca3af; letter-spacing:0.05em; "
            f"text-transform:uppercase; margin-bottom:6px;'>"
            f"{severity_label} · {selected_day}</div>"
            f"<div style='font-size:1.1em; font-weight:600; line-height:1.4;'>"
            f"{headline_txt}</div>"
            f"</div>",
            unsafe_allow_html=True,
        )

        if kpi and kpi.get("total_pings"):
            otp_val = float(kpi['otp_pct']) if kpi['otp_pct'] is not None else 0
            otp_color = "#22c55e" if otp_val >= 80 else "#f59e0b" if otp_val >= 65 else "#ef4444"
            
            delay_val = float(kpi['avg_delay']) if kpi['avg_delay'] is not None else 0
            delay_color = "#22c55e" if delay_val <= 2 else "#f59e0b" if delay_val <= 5 else "#ef4444"
            
            very_late = int(kpi["very_late"]) if kpi["very_late"] is not None else 0
            late_color = "#22c55e" if very_late == 0 else "#f59e0b" if very_late <= 10 else "#ef4444"

            k1, k2, k3, k4 = st.columns(4)
            with k1:
                render_kpi("On-Time %", f"{kpi['otp_pct']}%", otp_color, "Share of moving-bus pings within EMTA's on-time window (−1 to +5 min of schedule).")
            with k2:
                render_kpi("Avg Delay", f"{kpi['avg_delay']} min", delay_color, "Average signed adherence. Negative = running early, positive = running late.")
            with k3:
                render_kpi("Active Routes", str(kpi["active_routes"]), "#3b82f6", "Distinct route IDs that produced at least one moving-bus ping today.")
            with k4:
                render_kpi("Very Late Pings", str(kpi["very_late"]), late_color, "Number of pings where the bus was > 10 minutes behind schedule.")
            st.write("")

        # ── Day-arc hourly OTP chart ────────────────────────
        # A single-line chart of on-time percentage hour by hour tells the
        # executive story "when did the day go wrong?" at a glance. Pulled
        # from silver with the same moving-bus filter used everywhere else.
        # No new Anthropic tokens.
        arc_sql = """
            SELECT hour_of_day,
                   ROUND(
                       COUNT(*) FILTER (WHERE delay_bucket = 'on_time') * 100.0
                       / NULLIF(COUNT(*), 0), 1
                   ) AS otp_pct,
                   COUNT(*) AS pings
            FROM silver_arrivals
            WHERE (observed_at AT TIME ZONE 'America/New_York')::date = %s
              AND speed > 2
            GROUP BY hour_of_day
            ORDER BY hour_of_day
        """
        arc_rows = run_query(arc_sql, [selected_day])
        if arc_rows:
            arc_labels = [f"{int(r['hour_of_day']):02d}" for r in arc_rows]
            for r, lbl in zip(arc_rows, arc_labels):
                r["hour_label"] = lbl
            fig_arc = px.bar(
                arc_rows,
                x="hour_label",
                y="otp_pct",
                title="Hourly On-Time % — how the day unfolded",
                labels={"hour_label": "Hour", "otp_pct": "On-Time %"},
                color="otp_pct",
                color_continuous_scale="RdYlGn",
                range_color=[0, 100],
            )
            fig_arc.update_layout(
                **PLOTLY_LAYOUT,
                bargap=0.15,
                height=260,
                yaxis=dict(range=[0, 100]),
                xaxis=dict(type="category"),
                coloraxis_showscale=False,
            )
            st.plotly_chart(fig_arc, use_container_width=True)

        # ── Top-3 worst routes for the day ──────────────────
        # Same symmetric reliability formula the Overview/heatmap use, so
        # the digest, the banner, and the heatmap all agree on "worst".
        worst_routes_sql = """
            SELECT route_id, route_name,
                   GREATEST(0, ROUND(
                       (100 - LEAST(100, AVG(LEAST(30, ABS(adherence_minutes))) * 10))::numeric, 1
                   )) AS reliability,
                   ROUND(AVG(adherence_minutes)::numeric, 1) AS avg_delay,
                   COUNT(*) AS pings
            FROM silver_arrivals
            WHERE (observed_at AT TIME ZONE 'America/New_York')::date = %s
              AND speed > 2
              AND adherence_minutes IS NOT NULL
              AND route_name IS NOT NULL
              AND route_id NOT IN ('98', '99', '999')
            GROUP BY route_id, route_name
            HAVING COUNT(*) >= 20
            ORDER BY avg_delay DESC
            LIMIT 3
        """
        worst_routes = run_query(worst_routes_sql, [selected_day])
        if worst_routes:
            st.markdown("**⚠️ Top 3 problem routes**")
            worst_display = [
                {
                    "Route": format_route(r["route_id"], r["route_name"]),
                    "Reliability": f"{r['reliability']}/100",
                    "Avg Delay (min)": r["avg_delay"],
                    "Pings": r["pings"],
                }
                for r in worst_routes
            ]
            st.dataframe(worst_display, use_container_width=True, hide_index=True)

        with st.container(border=True):
            st.markdown(ins["narrative"])
        st.caption(f"Generated {ins['created_at']}")

        # Today's digest can always be regenerated to reflect newer data.
        if is_today:
            if st.button("Regenerate with latest data", key="regenerate_daily"):
                with st.spinner(f"Calling Claude for {selected_day} …"):
                    try:
                        from ai_agent.daily_insights import generate_daily_insights
                        status = generate_daily_insights(selected_day)
                        _run_query_cached.clear()
                        if status == "regenerated":
                            st.success("Digest regenerated with latest data.")
                            st.rerun()
                        elif status == "no_data":
                            st.warning(f"No moving-bus data available for {selected_day}.")
                        elif status == "missing_env":
                            st.error("Server is missing SUPABASE_DB_URL or ANTHROPIC_API_KEY.")
                        else:
                            st.error(f"Unexpected status: {status}")
                    except Exception as exc:
                        st.error(f"Regeneration failed: {exc}")
    else:
        st.warning(f"No digest yet for {selected_day}.")
        if st.button("Generate digest", key="generate_daily"):
            with st.spinner(f"Calling Claude for {selected_day} …"):
                try:
                    # Lazy import: anthropic is only needed when generating.
                    from ai_agent.daily_insights import generate_daily_insights
                    status = generate_daily_insights(selected_day)
                    _run_query_cached.clear()
                    if status in ("generated", "regenerated"):
                        st.success("Digest generated.")
                        st.rerun()
                    elif status == "exists":
                        st.info("Digest already existed — reloading.")
                        st.rerun()
                    elif status == "no_data":
                        st.warning(f"No moving-bus data available for {selected_day}.")
                    elif status == "missing_env":
                        st.error("Server is missing SUPABASE_DB_URL or ANTHROPIC_API_KEY.")
                    else:
                        st.error(f"Unexpected status: {status}")
                except Exception as exc:
                    st.error(f"Generation failed: {exc}")

    # ── Recent digests list ──────────────────────────────
    st.markdown("##### Recent daily digests")
    recent_sql = """
        SELECT report_date, narrative, tweet_draft, headline_text, created_at
        FROM ai_daily_insights
        ORDER BY report_date DESC
        LIMIT 30
    """
    recent = run_query(recent_sql)
    if recent:
        for ins in recent:
            with st.expander(
                f"{ins['report_date']} — {ins.get('headline_text', '')}",
                expanded=False,
            ):
                st.markdown(ins["narrative"])
                # Tweet draft kept in DB; intentionally hidden from UI.
                st.caption(f"Generated {ins['created_at']}")
    else:
        st.info(
            "No daily insights generated yet. Pick a date above and click **Generate digest**, "
            "or run `python -m ai_agent.daily_insights`."
        )


# ── Footer ───────────────────────────────────────────────
st.markdown("---")
st.markdown(
    "<div style='text-align: center; color: #888; font-size: 0.85em;'>"
    "Data sourced from EMTA Avail API · Updated every 5 minutes · "
    "Built by Vladimir · "
    "<a href='https://github.com/VladimirMickic/Public_Transport' style='color: #888;'>GitHub</a>"
    "</div>",
    unsafe_allow_html=True,
)
