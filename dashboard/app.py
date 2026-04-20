"""
EMTA Transit Reliability Dashboard
───────────────────────────────────
Four-tab Streamlit app powered by Supabase (silver/gold/ai tables).

Usage:
    streamlit run dashboard/app.py
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta

import plotly.express as px
import plotly.graph_objects as go
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


# ── DB connection ────────────────────────────────────────
@st.cache_resource
def get_conn():
    """Single shared DB connection (cached across reruns)."""
    return psycopg2.connect(os.environ["SUPABASE_DB_URL"], connect_timeout=10)


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
    horizontal=True,
)

# Build direction SQL fragment
direction_filter_sql = ""
direction_params = []
if direction_choice == "Inbound":
    direction_filter_sql = "AND direction ILIKE %s"
    direction_params = ["%I%"]
elif direction_choice == "Outbound":
    direction_filter_sql = "AND direction ILIKE %s"
    direction_params = ["%O%"]

st.sidebar.markdown("---")
st.sidebar.caption("Data sourced from EMTA Avail API")
st.sidebar.caption("Updated every 5 minutes")
st.sidebar.caption("Built by Hugo · [GitHub](https://github.com/hugorabbit)")


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

            # ── On-time trend by day ─────────────────────
            trend_sql = f"""
                SELECT observed_at::date AS day,
                       ROUND(
                           COUNT(*) FILTER (WHERE delay_bucket = 'on_time') * 100.0
                           / NULLIF(COUNT(*), 0), 1
                       ) AS on_time_pct
                FROM silver_arrivals
                WHERE observed_at::date BETWEEN %s AND %s
                {direction_filter_sql}
                GROUP BY day
                ORDER BY day
            """
            trend = run_query(trend_sql, [filter_start, filter_end] + direction_params)

            col_pie, col_trend = st.columns([1, 2])
            with col_pie:
                st.plotly_chart(fig_bucket, use_container_width=True)
            with col_trend:
                if trend:
                    fig_trend = px.line(
                        trend, x="day", y="on_time_pct",
                        title="Daily On-Time %",
                        labels={"day": "Date", "on_time_pct": "On-Time %"},
                    )
                    fig_trend.update_layout(**PLOTLY_LAYOUT)
                    fig_trend.update_traces(line=dict(color="#22c55e", width=3))
                    st.plotly_chart(fig_trend, use_container_width=True)

        # ── Worst peak-hour route callout ────────────────
        worst_sql = """
            SELECT route_name, hour_of_day, reliability_score
            FROM gold_route_reliability
            WHERE hour_of_day IN (7, 8, 16, 17, 18)
              AND total_pings >= 5
            ORDER BY reliability_score ASC
            LIMIT 1
        """
        worst = run_query(worst_sql)
        if worst:
            w = worst[0]
            hour_label = f"{w['hour_of_day']}:00"
            st.warning(
                f"⚠️ **Worst peak-hour route:** {w['route_name']} at {hour_label} "
                f"— reliability score **{w['reliability_score']}**/100"
            )

        # ── Route reliability heatmap ────────────────────
        heat_sql = """
            SELECT route_name, hour_of_day, reliability_score
            FROM gold_route_reliability
            WHERE total_pings >= 3
            ORDER BY route_name, hour_of_day
        """
        heat_data = run_query(heat_sql)
        if heat_data:
            import pandas as pd
            df_heat = pd.DataFrame(heat_data)
            pivot = df_heat.pivot_table(
                index="route_name", columns="hour_of_day",
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
        route_options = {r["route_name"]: r["route_id"] for r in routes}
        selected_route_name = st.selectbox("Select Route", list(route_options.keys()))
        selected_route_id = route_options[selected_route_name]

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
            col_h1, col_h2 = st.columns(2)
            with col_h1:
                fig_h = px.bar(
                    hourly, x="hour_of_day", y="on_time_pct",
                    title=f"{selected_route_name} — On-Time % by Hour",
                    labels={"hour_of_day": "Hour", "on_time_pct": "On-Time %"},
                    color_discrete_sequence=["#22c55e"],
                )
                fig_h.update_layout(**PLOTLY_LAYOUT)
                st.plotly_chart(fig_h, use_container_width=True)

            with col_h2:
                fig_d = px.bar(
                    hourly, x="hour_of_day", y="avg_delay",
                    title=f"{selected_route_name} — Avg Delay by Hour",
                    labels={"hour_of_day": "Hour", "avg_delay": "Avg Delay (min)"},
                    color_discrete_sequence=["#f59e0b"],
                )
                fig_d.update_layout(**PLOTLY_LAYOUT)
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

    map_mode = st.toggle("Show today's delay heatmap", value=False)

    if not map_mode:
        # ── Current vehicles (last 15 min of bronze) ─────
        st.caption("Showing vehicles from the last 15 minutes")
        live_sql = """
            SELECT vehicle_id, route_name, latitude, longitude,
                   adherence_minutes, display_status, speed, vehicle_name
            FROM bronze_vehicle_pings
            WHERE observed_at >= NOW() - INTERVAL '15 minutes'
              AND latitude IS NOT NULL
              AND longitude IS NOT NULL
        """
        live = run_query(live_sql)

        if live:
            fig_map = px.scatter_mapbox(
                live,
                lat="latitude",
                lon="longitude",
                hover_name="route_name",
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
        # ── Today's delay heatmap from silver ────────────
        st.caption("Average delay by location today")
        heat_sql = """
            SELECT
                ROUND(latitude::numeric, 3) AS lat_grid,
                ROUND(longitude::numeric, 3) AS lon_grid,
                ROUND(AVG(adherence_minutes)::numeric, 1) AS avg_delay,
                COUNT(*) AS pings
            FROM silver_arrivals
            WHERE observed_at::date = CURRENT_DATE
              AND latitude IS NOT NULL
              AND longitude IS NOT NULL
            GROUP BY lat_grid, lon_grid
            HAVING COUNT(*) >= 2
        """
        heat = run_query(heat_sql)

        if heat:
            fig_hm = px.density_mapbox(
                heat,
                lat="lat_grid",
                lon="lon_grid",
                z="avg_delay",
                radius=20,
                zoom=11,
                height=600,
                title="Today's Delay Heatmap",
                color_continuous_scale="RdYlGn_r",
            )
            fig_hm.update_layout(
                mapbox_style="carto-positron",
                mapbox_center={"lat": 42.129, "lon": -80.085},
                **PLOTLY_LAYOUT,
            )
            st.plotly_chart(fig_hm, use_container_width=True)
        else:
            st.info("No data for today's heatmap yet.")


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
                if ins.get("tweet_draft"):
                    st.markdown("---")
                    st.markdown(f"**🐦 Tweet draft:** {ins['tweet_draft']}")
                st.caption(f"Generated {ins['created_at']}")
    else:
        st.info(
            "No AI insights generated yet. Run `python -m ai_agent.insights` "
            "after collecting at least a week of data."
        )


# ── Footer ───────────────────────────────────────────────
st.markdown("---")
st.markdown(
    "<div style='text-align: center; color: #888; font-size: 0.85em;'>"
    "Data sourced from EMTA Avail API · Updated every 5 minutes · "
    "Built by Hugo · "
    "<a href='https://github.com/hugorabbit' style='color: #888;'>GitHub</a>"
    "</div>",
    unsafe_allow_html=True,
)
