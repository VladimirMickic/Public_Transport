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


# ── DB connection ────────────────────────────────────────
@st.cache_resource
def get_conn():
    """Single shared DB connection (cached across reruns)."""
    return psycopg2.connect(os.environ["SUPABASE_DB_URL"], connect_timeout=10)


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


# ── Sidebar filters ──────────────────────────────────────
st.sidebar.image("https://emta.availtec.com/InfoPoint/Content/images/logo.png", width=180)
st.sidebar.title("Filters")

default_end = date.today()
default_start = default_end - timedelta(days=30)
date_range = st.sidebar.date_input(
    "Date range",
    value=(default_start, default_end),
    max_value=default_end,
)
if isinstance(date_range, tuple) and len(date_range) == 2:
    filter_start, filter_end = date_range
else:
    filter_start, filter_end = default_start, default_end

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

    # ── Key metrics from silver (date-filtered) ──────────
    metrics_sql = f"""
        SELECT
            COUNT(*) AS total_pings,
            ROUND(AVG(adherence_minutes)::numeric, 1) AS avg_delay,
            ROUND(
                COUNT(*) FILTER (WHERE delay_bucket = 'on_time') * 100.0
                / NULLIF(COUNT(*), 0), 1
            ) AS on_time_pct,
            COUNT(DISTINCT route_id) AS active_routes
        FROM silver_arrivals
        WHERE observed_at::date BETWEEN %s AND %s
        {direction_filter_sql}
    """
    metrics = run_query(metrics_sql, [filter_start, filter_end] + direction_params)

    if metrics and metrics[0]["total_pings"] and metrics[0]["total_pings"] > 0:
        m = metrics[0]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total Pings", f"{m['total_pings']:,}")
        c2.metric("On-Time %", f"{m['on_time_pct']}%")
        c3.metric("Avg Delay", f"{m['avg_delay']} min")
        c4.metric("Active Routes", m["active_routes"])

        # ── Delay bucket breakdown ───────────────────────
        bucket_sql = f"""
            SELECT delay_bucket, COUNT(*) AS cnt
            FROM silver_arrivals
            WHERE observed_at::date BETWEEN %s AND %s
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

            if selected_bucket and selected_bucket != "on_time":
                trend_sql = f"""
                    SELECT observed_at::date AS day,
                           ROUND(
                               COUNT(*) FILTER (WHERE delay_bucket = %s) * 100.0
                               / NULLIF(COUNT(*), 0), 1
                           ) AS pct
                    FROM silver_arrivals
                    WHERE observed_at::date BETWEEN %s AND %s
                    {direction_filter_sql}
                    GROUP BY day
                    ORDER BY day
                """
                trend_params = [selected_bucket, filter_start, filter_end] + direction_params
                trend_title = f"Daily {selected_bucket.replace('_', ' ').title()} %"
                line_color = COLORS.get(selected_bucket, "#22c55e")
            else:
                trend_sql = f"""
                    SELECT observed_at::date AS day,
                           ROUND(
                               COUNT(*) FILTER (WHERE delay_bucket = 'on_time') * 100.0
                               / NULLIF(COUNT(*), 0), 1
                           ) AS pct
                    FROM silver_arrivals
                    WHERE observed_at::date BETWEEN %s AND %s
                    {direction_filter_sql}
                    GROUP BY day
                    ORDER BY day
                """
                trend_params = [filter_start, filter_end] + direction_params
                trend_title = "Daily On-Time %"
                line_color = COLORS["on_time"]

            trend = run_query(trend_sql, trend_params)

            with col_trend:
                if trend:
                    fig_trend = px.line(
                        trend, x="day", y="pct",
                        title=trend_title,
                        labels={"day": "Date", "pct": trend_title.split(" ", 1)[1]},
                        markers=True,
                    )
                    fig_trend.update_layout(
                        **PLOTLY_LAYOUT,
                        xaxis=dict(type="date", tickformat="%b %d"),
                        yaxis=dict(range=[0, 100]),
                    )
                    fig_trend.update_traces(
                        line=dict(color=line_color, width=3),
                        marker=dict(size=10, color=line_color),
                    )
                    st.plotly_chart(fig_trend, use_container_width=True)

        # ── Worst peak-hour route callout ────────────────
        # Higher threshold + non-zero score filter so tiny/edge slots
        # (e.g. a chartered "AM Tripper" with 1 bad trip) don't
        # masquerade as the worst route on the system.
        worst_sql = """
            SELECT route_id, route_name, hour_of_day, reliability_score
            FROM gold_route_reliability
            WHERE hour_of_day IN (7, 8, 16, 17, 18)
              AND total_pings >= 50
              AND reliability_score IS NOT NULL
              AND reliability_score > 0
            ORDER BY reliability_score ASC
            LIMIT 1
        """
        worst = run_query(worst_sql)
        if worst:
            w = worst[0]
            hour_label = f"{w['hour_of_day']}:00"
            route_label = format_route(w["route_id"], w["route_name"])
            st.warning(
                f"⚠️ **Worst peak-hour route:** {route_label} at {hour_label} "
                f"— reliability score **{w['reliability_score']}**/100"
            )

        # ── Route reliability heatmap ────────────────────
        heat_sql = """
            SELECT route_id, route_name, hour_of_day, reliability_score
            FROM gold_route_reliability
            WHERE total_pings >= 3
            ORDER BY route_id, hour_of_day
        """
        heat_data = run_query(heat_sql)
        if heat_data:
            import pandas as pd
            df_heat = pd.DataFrame(heat_data)
            df_heat["route_label"] = df_heat.apply(
                lambda r: format_route(r["route_id"], r["route_name"]), axis=1
            )
            pivot = df_heat.pivot_table(
                index="route_label", columns="hour_of_day",
                values="reliability_score", aggfunc="mean"
            )
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

    # Get available routes
    routes_sql = """
        SELECT DISTINCT route_id, route_name
        FROM silver_arrivals
        WHERE route_name IS NOT NULL
        ORDER BY route_name
    """
    routes = run_query(routes_sql)

    if routes:
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
              AND observed_at::date BETWEEN %s AND %s
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
                    color_discrete_sequence=["#22c55e"],
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
              AND observed_at::date BETWEEN %s AND %s
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

        # ── 10 worst days for this route ─────────────────
        st.markdown("#### 10 Worst Days")
        worst_days_sql = f"""
            SELECT observed_at::date AS day,
                   COUNT(*) AS pings,
                   ROUND(AVG(adherence_minutes)::numeric, 1) AS avg_delay,
                   ROUND(
                       COUNT(*) FILTER (WHERE delay_bucket = 'on_time') * 100.0
                       / NULLIF(COUNT(*), 0), 1
                   ) AS on_time_pct,
                   COUNT(*) FILTER (WHERE delay_bucket = 'very_late') AS very_late_count
            FROM silver_arrivals
            WHERE route_id = %s
              AND observed_at::date BETWEEN %s AND %s
              {direction_filter_sql}
            GROUP BY day
            HAVING COUNT(*) >= 3
            ORDER BY on_time_pct ASC
            LIMIT 10
        """
        worst_days = run_query(
            worst_days_sql,
            [selected_route_id, filter_start, filter_end] + direction_params,
        )
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

    map_mode = st.toggle("Show today's activity by route", value=False)

    if not map_mode:
        # ── Current vehicles (last 15 min of bronze) ─────
        st.caption("Showing vehicles from the last 15 minutes")
        live_sql = """
            SELECT vehicle_id, route_id, route_name, latitude, longitude,
                   adherence_minutes, display_status, speed, vehicle_name
            FROM bronze_vehicle_pings
            WHERE observed_at >= NOW() - INTERVAL '15 minutes'
              AND latitude IS NOT NULL
              AND longitude IS NOT NULL
        """
        live = run_query(live_sql, live=True)

        if live:
            for v in live:
                v["route_label"] = format_route(v.get("route_id"), v.get("route_name"))
            fig_map = px.scatter_mapbox(
                live,
                lat="latitude",
                lon="longitude",
                hover_name="route_label",
                hover_data=["vehicle_name", "adherence_minutes", "display_status", "speed"],
                color="display_status",
                color_discrete_map={
                    "On Time": "#22c55e", "Early": "#3b82f6",
                    "Late": "#f59e0b", "LATE": "#f59e0b",
                },
                zoom=11,
                height=600,
                title="Live EMTA Vehicles",
            )
            fig_map.update_layout(
                mapbox_style="carto-positron",
                mapbox_center={"lat": 42.129, "lon": -80.085},
                **PLOTLY_LAYOUT,
            )
            st.plotly_chart(fig_map, use_container_width=True)
        else:
            st.info("No live vehicles right now. Buses typically run 6 AM – 10 PM ET on weekdays.")

    else:
        # ── Today's route activity map from silver ───────
        st.caption("Each dot is a grid cell where a route was seen today — "
                   "color = route, size = ping count, hover = avg delay.")
        heat_sql = """
            SELECT
                route_id,
                route_name,
                ROUND(latitude::numeric, 3) AS lat_grid,
                ROUND(longitude::numeric, 3) AS lon_grid,
                COUNT(*) AS pings,
                ROUND(AVG(adherence_minutes)::numeric, 1) AS avg_delay
            FROM silver_arrivals
            WHERE observed_at::date = CURRENT_DATE
              AND latitude IS NOT NULL
              AND longitude IS NOT NULL
              AND route_name IS NOT NULL
            GROUP BY route_id, route_name, lat_grid, lon_grid
            HAVING COUNT(*) >= 2
            ORDER BY route_id
        """
        heat = run_query(heat_sql)

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
                title="Today's Activity by Route",
                # Dark24 is a muted, harmonious palette — easier on the eyes
                # than the neon Alphabet palette on our dark-themed dashboard.
                color_discrete_sequence=px.colors.qualitative.Dark24,
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
            st.info("No data for today's route map yet.")


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
    picker_max = date.today() - timedelta(days=1)
    picker_min = picker_max - timedelta(days=60)
    selected_day = st.date_input(
        "Pick a date",
        value=picker_max,
        min_value=picker_min,
        max_value=picker_max,
        key="daily_digest_date",
    )

    existing = run_query(
        "SELECT report_date, narrative, tweet_draft, headline_text, created_at "
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
            WHERE observed_at::date = %s
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
            k1, k2, k3, k4 = st.columns(4)
            k1.metric("On-Time %", f"{kpi['otp_pct']}%")
            k2.metric("Avg Delay", f"{kpi['avg_delay']} min")
            k3.metric("Active Routes", kpi["active_routes"])
            k4.metric("Very Late", kpi["very_late"])

        with st.container(border=True):
            st.markdown(ins["narrative"])
        st.caption(f"Generated {ins['created_at']}")
    else:
        st.warning(f"No digest yet for {selected_day}.")
        if st.button("Generate digest", key="generate_daily"):
            with st.spinner(f"Calling Claude for {selected_day} …"):
                try:
                    # Lazy import: anthropic is only needed when generating.
                    from ai_agent.daily_insights import generate_daily_insights
                    status = generate_daily_insights(selected_day)
                    _run_query_cached.clear()
                    if status == "generated":
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
