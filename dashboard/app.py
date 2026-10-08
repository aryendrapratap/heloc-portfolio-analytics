"""
HELOC portfolio dashboard.

Run from the project folder, after building and loading the database:
    streamlit run dashboard/app.py
"""

import os
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

BLUE = "#2a78d6"
SERIES_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
# Ordered groups (lateness, risk level) use one hue, light to dark
BLUE_STEPS = ["#86b6ef", "#3987e5", "#1c5cab", "#0d366b"]

BUCKET_LABELS = {
    "DPD_1_29": "1-29 days",
    "DPD_30_59": "30-59 days",
    "DPD_60_89": "60-89 days",
    "DPD_90_179": "90-179 days",
}
RISK_ORDER = ["LOW", "MEDIUM", "HIGH", "VERY_HIGH"]

st.set_page_config(page_title="HELOC Portfolio", layout="wide")


# ------------------------------------------------------------------
# Data
# ------------------------------------------------------------------
@st.cache_resource
def get_engine():
    url = URL.create(
        "mysql+pymysql",
        username=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        host=os.getenv("DB_HOST"),
        port=int(os.getenv("DB_PORT", "3306")),
        database=os.getenv("DB_NAME"),
    )
    return create_engine(url)


@st.cache_data(ttl=300)
def query(sql: str) -> pd.DataFrame:
    with get_engine().connect() as conn:
        df = pd.read_sql(text(sql), conn)
    for col in df.columns:
        if col.endswith("_month"):
            df[col] = pd.to_datetime(df[col])
    return df


RISK_DRIVERS_SQL = """
WITH outcomes AS (
    SELECT s.*,
           MAX(s.days_past_due >= 30) OVER (PARTITION BY s.account_id ORDER BY s.snapshot_month
                                            ROWS BETWEEN 1 FOLLOWING AND 6 FOLLOWING) AS late_next_6m
    FROM monthly_snapshot s
),
base AS (
    SELECT *, COALESCE(late_next_6m, 0) AS went_late
    FROM outcomes
    WHERE days_past_due = 0
      AND snapshot_month <= (SELECT MAX(snapshot_month) - INTERVAL 6 MONTH FROM monthly_snapshot)
)
SELECT 'Credit score' AS factor,
       CASE WHEN credit_score < 680 THEN 1 WHEN credit_score < 720 THEN 2
            WHEN credit_score < 760 THEN 3 ELSE 4 END AS band_order,
       CASE WHEN credit_score < 680 THEN '<680' WHEN credit_score < 720 THEN '680-719'
            WHEN credit_score < 760 THEN '720-759' ELSE '760+' END AS band,
       COUNT(*) AS account_months, AVG(went_late) AS late_rate
FROM base GROUP BY 1, 2, 3
UNION ALL
SELECT 'Utilization',
       CASE WHEN utilization_rate < 0.30 THEN 1 WHEN utilization_rate < 0.60 THEN 2
            WHEN utilization_rate < 0.90 THEN 3 ELSE 4 END,
       CASE WHEN utilization_rate < 0.30 THEN '<30%' WHEN utilization_rate < 0.60 THEN '30-59%'
            WHEN utilization_rate < 0.90 THEN '60-89%' ELSE '90%+' END,
       COUNT(*), AVG(went_late)
FROM base GROUP BY 1, 2, 3
UNION ALL
SELECT 'CLTV',
       CASE WHEN cltv <= 0.50 THEN 1 WHEN cltv <= 0.65 THEN 2 WHEN cltv <= 0.80 THEN 3 ELSE 4 END,
       CASE WHEN cltv <= 0.50 THEN '<=50%' WHEN cltv <= 0.65 THEN '50-65%'
            WHEN cltv <= 0.80 THEN '65-80%' ELSE '>80%' END,
       COUNT(*), AVG(went_late)
FROM base GROUP BY 1, 2, 3
UNION ALL
SELECT 'Risk segment',
       CASE risk_segment WHEN 'LOW' THEN 1 WHEN 'MEDIUM' THEN 2 ELSE 3 END,
       CASE risk_segment WHEN 'LOW' THEN 'Low' WHEN 'MEDIUM' THEN 'Medium' ELSE 'High+' END,
       COUNT(*), AVG(went_late)
FROM base GROUP BY 1, 2, 3
"""

ROLL_MATRIX_SQL = """
SELECT fb.sort_order, fb.bucket_code AS from_bucket, COUNT(*) AS account_months,
       AVG(nxt.bucket_code = 'CURRENT')     AS to_current,
       AVG(nxt.bucket_code = 'DPD_1_29')    AS to_1_29,
       AVG(nxt.bucket_code = 'DPD_30_59')   AS to_30_59,
       AVG(nxt.bucket_code = 'DPD_60_89')   AS to_60_89,
       AVG(nxt.bucket_code = 'DPD_90_179')  AS to_90_179,
       AVG(nxt.bucket_code = 'CHARGED_OFF') AS to_charged_off
FROM monthly_snapshot prv
JOIN monthly_snapshot nxt
  ON nxt.account_id = prv.account_id
 AND nxt.snapshot_month = prv.snapshot_month + INTERVAL 1 MONTH
JOIN delinquency_bucket fb ON fb.bucket_code = prv.bucket_code
WHERE prv.bucket_code <> 'CHARGED_OFF'
GROUP BY fb.sort_order, fb.bucket_code
ORDER BY fb.sort_order
"""

try:
    portfolio = query("SELECT * FROM vw_portfolio_monthly ORDER BY snapshot_month")
except Exception as err:  # noqa: BLE001
    st.error("Can't reach the database. Check that MySQL is running and that .env is filled in.")
    st.exception(err)
    st.stop()

if portfolio.empty:
    st.warning("The database is empty. Run scripts/build_db.py and data_generator/generate_data.py first.")
    st.stop()


# ------------------------------------------------------------------
# Chart helpers
# ------------------------------------------------------------------
def style(fig, y_format=None, height=320):
    fig.update_layout(
        height=height,
        margin=dict(l=8, r=8, t=48, b=8),
        hovermode="x unified",
        xaxis_title=None,
        yaxis_title=None,
        legend_title_text="",
        legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0),
        bargap=0.3,
    )
    fig.update_xaxes(showgrid=False)
    if y_format:
        fig.update_yaxes(tickformat=y_format)
    return fig


def line(df, x, y, title, y_format=None, hover_format=None):
    fig = px.line(df, x=x, y=y, title=title)
    fig.update_traces(line=dict(color=BLUE, width=2),
                      hovertemplate=f"%{{y:{hover_format or y_format or ',.0f'}}}<extra></extra>")
    return style(fig, y_format)


def show_data(df, label="Show data"):
    with st.expander(label):
        st.dataframe(df, hide_index=True)


# ------------------------------------------------------------------
# Filter: month range
# ------------------------------------------------------------------
months = portfolio["snapshot_month"].dt.strftime("%Y-%m").tolist()
st.sidebar.header("Filters")
start, end = st.sidebar.select_slider("Months shown", options=months, value=(months[0], months[-1]))
st.sidebar.caption("The summary at the top uses the last month in the range.")

in_range = portfolio["snapshot_month"].between(pd.Timestamp(start), pd.Timestamp(end))
p = portfolio[in_range].copy()
cur = p.iloc[-1]
prev = p.iloc[-2] if len(p) > 1 else cur


# ------------------------------------------------------------------
# Header and summary numbers
# ------------------------------------------------------------------
st.title("HELOC Portfolio Analytics")
st.caption(f"Simulated portfolio of 300 Canadian HELOC accounts · showing {start} to {end}")

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Open accounts", f"{int(cur.open_accounts):,}",
          f"{int(cur.open_accounts - prev.open_accounts):+,} vs last month",
          delta_color="off", border=True,
          chart_data=p["open_accounts"].astype(int).tolist())
c2.metric("Balance owed", f"${cur.total_balance / 1e6:,.1f}M",
          f"{cur.total_balance / prev.total_balance - 1:+.1%} vs last month",
          delta_color="off", border=True,
          chart_data=(p["total_balance"] / 1e6).round(2).tolist(), chart_type="area")
c3.metric("Credit used", f"{cur.utilization:.1%}",
          f"{(cur.utilization - prev.utilization) * 100:+.1f} pts", delta_color="off", border=True,
          chart_data=(p["utilization"] * 100).round(1).tolist())
c4.metric("30+ days late", f"{cur.rate_30_plus:.1%}",
          f"{(cur.rate_30_plus - prev.rate_30_plus) * 100:+.1f} pts", delta_color="inverse", border=True,
          chart_data=(p["rate_30_plus"] * 100).round(2).tolist())
c5.metric("Written off in range", f"${p['charge_off_balance'].sum() / 1e6:,.2f}M",
          f"{int(p['charge_offs'].sum())} accounts", delta_color="off", delta_arrow="off", border=True,
          chart_data=(p["charge_off_balance"].cumsum() / 1e6).round(2).tolist(), chart_type="area")


tab_overview, tab_late, tab_risk, tab_vintage, tab_watch, tab_rates = st.tabs(
    ["Overview", "Late payments", "What predicts risk", "Vintages", "Watch list", "Interest rates"]
)


# ------------------------------------------------------------------
# Overview
# ------------------------------------------------------------------
with tab_overview:
    left, right = st.columns(2)
    with left:
        st.plotly_chart(line(p, "snapshot_month", "total_balance", "Total balance owed", "$,.2s", "$,.0f"))
    with right:
        st.plotly_chart(line(p, "snapshot_month", "utilization", "Share of credit limit in use", ".0%", ".1%"))

    st.subheader("By province (latest month)")
    provinces = query("SELECT * FROM vw_province_latest ORDER BY balance DESC")
    st.dataframe(
        provinces,
        hide_index=True,
        column_config={
            "province_code": None,
            "province_name": "Province",
            "accounts": "Accounts",
            "balance": st.column_config.NumberColumn("Balance", format="dollar"),
            "utilization": st.column_config.ProgressColumn("Credit used", format="percent",
                                                           min_value=0.0, max_value=1.0),
            "avg_cltv": st.column_config.NumberColumn("Avg CLTV", format="percent"),
            "avg_property_value": st.column_config.NumberColumn("Avg home value", format="$%,d"),
            "accounts_30_plus": "30+ days late",
        },
    )
    show_data(p, "Show monthly data")


# ------------------------------------------------------------------
# Late payments
# ------------------------------------------------------------------
with tab_late:
    buckets = query("SELECT * FROM vw_delinquency_buckets ORDER BY snapshot_month, sort_order")
    buckets = buckets[buckets["snapshot_month"].between(pd.Timestamp(start), pd.Timestamp(end))]
    late = buckets[buckets["bucket_code"].isin(BUCKET_LABELS)].copy()
    late["Days late"] = late["bucket_code"].map(BUCKET_LABELS)

    left, right = st.columns(2)
    with left:
        fig = px.bar(late, x="snapshot_month", y="accounts", color="Days late",
                     title="Accounts behind on payments",
                     category_orders={"Days late": list(BUCKET_LABELS.values())},
                     color_discrete_sequence=BLUE_STEPS)
        fig.update_traces(hovertemplate="%{y}<extra>%{fullData.name}</extra>")
        st.plotly_chart(style(fig))
    with right:
        st.plotly_chart(line(p, "snapshot_month", "rate_30_plus",
                             "Share of open accounts 30+ days late", ".1%", ".2%"))

    st.subheader("Where accounts go the next month")
    st.caption("Each row: accounts in that bucket, and where they were one month later. All months combined.")
    roll = query(ROLL_MATRIX_SQL).drop(columns="sort_order")
    st.dataframe(
        roll,
        hide_index=True,
        column_config={
            "from_bucket": "From",
            "account_months": "Account-months",
            "to_current": st.column_config.NumberColumn("Current", format="percent"),
            "to_1_29": st.column_config.NumberColumn("1-29", format="percent"),
            "to_30_59": st.column_config.NumberColumn("30-59", format="percent"),
            "to_60_89": st.column_config.NumberColumn("60-89", format="percent"),
            "to_90_179": st.column_config.NumberColumn("90-179", format="percent"),
            "to_charged_off": st.column_config.NumberColumn("Written off", format="percent"),
        },
    )


# ------------------------------------------------------------------
# What predicts risk
# ------------------------------------------------------------------
with tab_risk:
    st.caption("Accounts that were paid up in a month, and the share that fell 30+ days behind "
               "within the next 6 months, by what we knew at the time.")
    drivers = query(RISK_DRIVERS_SQL).sort_values(["factor", "band_order"])

    cols = st.columns(4)
    for col, factor in zip(cols, ["Risk segment", "Utilization", "Credit score", "CLTV"]):
        d = drivers[drivers["factor"] == factor]
        fig = px.bar(d, x="band", y="late_rate", title=factor, text="late_rate",
                     custom_data=["account_months"])
        fig.update_traces(marker_color=BLUE, texttemplate="%{y:.1%}", textposition="outside",
                          cliponaxis=False,
                          hovertemplate="%{y:.1%} went late<br>%{customdata[0]:,} account-months<extra></extra>")
        fig = style(fig, ".0%", height=300)
        fig.update_layout(hovermode="closest")
        with col:
            st.plotly_chart(fig)
    show_data(drivers.drop(columns="band_order"))

    st.subheader("Risk segment mix over time")
    segments = query("SELECT * FROM vw_risk_segment_monthly ORDER BY snapshot_month")
    segments = segments[segments["snapshot_month"].between(pd.Timestamp(start), pd.Timestamp(end))]
    fig = px.bar(segments, x="snapshot_month", y="accounts", color="risk_segment",
                 category_orders={"risk_segment": RISK_ORDER}, color_discrete_sequence=BLUE_STEPS)
    fig.update_traces(hovertemplate="%{y}<extra>%{fullData.name}</extra>")
    st.plotly_chart(style(fig))


# ------------------------------------------------------------------
# Vintages
# ------------------------------------------------------------------
with tab_vintage:
    vintages = query("SELECT * FROM vw_vintage_curve ORDER BY vintage, months_on_book")
    fig = px.line(vintages, x="months_on_book", y="cumulative_30_plus_rate", color="vintage",
                  title="Share of each opening quarter that has been 30+ days late",
                  color_discrete_sequence=SERIES_COLORS, custom_data=["accounts"])
    fig.update_traces(line=dict(width=2),
                      hovertemplate="%{y:.1%} of %{customdata[0]} accounts<extra>%{fullData.name}</extra>")
    fig = style(fig, ".0%", height=420)
    fig.update_xaxes(title_text="Months since opening")
    st.plotly_chart(fig)
    st.caption("Only accounts opened inside the data window are shown. "
               "Each quarter has 15-30 accounts, so one late account moves a line by several points.")
    show_data(vintages)


# ------------------------------------------------------------------
# Watch list
# ------------------------------------------------------------------
with tab_watch:
    watch = query("SELECT * FROM vw_early_warning ORDER BY warning_count DESC, balance DESC")
    st.caption(f"{len(watch)} open accounts that are not 30 days late yet but show warning signs. "
               "On past months, accounts with 2 or more signs went 30+ days late within 6 months "
               "far more often than accounts with none (see the analysis queries).")
    min_flags = st.radio("Show accounts with at least", [1, 2, 3], horizontal=True,
                         format_func=lambda n: f"{n} warning sign" + ("s" if n > 1 else ""))
    shown = watch[watch["warning_count"] >= min_flags]
    flag = st.column_config.CheckboxColumn
    st.dataframe(
        shown,
        hide_index=True,
        column_config={
            "account_id": None,
            "as_of_month": None,
            "account_number": "Account",
            "customer_name": "Customer",
            "balance": st.column_config.NumberColumn("Balance", format="dollar"),
            "utilization_rate": st.column_config.ProgressColumn("Credit used", format="percent",
                                                                min_value=0.0, max_value=1.0),
            "days_past_due": "Days late",
            "credit_score": "Score",
            "risk_segment": "Risk",
            "flag_maxed_out": flag("Maxed out"),
            "flag_borrowing_jump": flag("Borrowing jump"),
            "flag_score_drop": flag("Score drop"),
            "flag_recently_late": flag("Recently late"),
            "flag_high_cltv": flag("High CLTV"),
            "warning_count": "Signs",
        },
    )
    st.download_button("Download as CSV", shown.to_csv(index=False), "watch_list.csv", "text/csv")


# ------------------------------------------------------------------
# Interest rates
# ------------------------------------------------------------------
with tab_rates:
    rates = p.melt(id_vars="snapshot_month", value_vars=["prime_rate", "avg_interest_rate"],
                   var_name="rate", value_name="value")
    rates["rate"] = rates["rate"].map({"prime_rate": "Prime rate",
                                       "avg_interest_rate": "Average rate customers pay"})
    left, right = st.columns(2)
    with left:
        fig = px.line(rates, x="snapshot_month", y="value", color="rate", line_shape="hv",
                      title="Prime rate and customer rate", color_discrete_sequence=SERIES_COLORS)
        fig.update_traces(line=dict(width=2), hovertemplate="%{y:.2%}<extra>%{fullData.name}</extra>")
        st.plotly_chart(style(fig, ".1%"))
    with right:
        fig = go.Figure(go.Bar(x=p["snapshot_month"], y=p["interest_income"], marker_color=BLUE,
                               hovertemplate="$%{y:,.0f}<extra></extra>"))
        fig.update_layout(title="Interest earned each month")
        st.plotly_chart(style(fig, "$,.2s"))

    first, last = p.iloc[0], p.iloc[-1]
    st.markdown(
        f"From {start} to {end}, prime went from **{first.prime_rate:.2%}** to **{last.prime_rate:.2%}**. "
        f"Interest earned per \\$1,000 borrowed went from "
        f"**\\${1000 * first.interest_income / first.total_balance:.2f}** to "
        f"**\\${1000 * last.interest_income / last.total_balance:.2f}**, but balances grew "
        f"**{last.total_balance / first.total_balance - 1:.0%}**, so monthly interest earned changed by "
        f"**{last.interest_income / first.interest_income - 1:+.0%}**."
    )
