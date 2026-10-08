-- portfolio_analysis.sql
-- Business questions about the HELOC portfolio. Run each query on its own.


-- Q1. How are balances and utilization trending?
SELECT
    snapshot_month,
    open_accounts,
    total_balance,
    total_balance - LAG(total_balance) OVER (ORDER BY snapshot_month) AS balance_change,
    utilization,
    ROUND(AVG(utilization) OVER (ORDER BY snapshot_month
                                 ROWS BETWEEN 2 PRECEDING AND CURRENT ROW), 4) AS utilization_3m_avg
FROM vw_portfolio_monthly
ORDER BY snapshot_month;


-- Q2. Which provinces use the most of their credit, and how has that changed in a year?
WITH province_month AS (
    SELECT
        p.province_code,
        s.snapshot_month,
        COUNT(*) AS accounts,
        SUM(s.closing_balance) / SUM(s.credit_limit) AS utilization
    FROM monthly_snapshot s
    JOIN heloc_account h ON h.account_id = s.account_id
    JOIN property p      ON p.property_id = h.property_id
    WHERE s.bucket_code <> 'CHARGED_OFF'
    GROUP BY p.province_code, s.snapshot_month
)
SELECT
    now_.province_code,
    now_.accounts,
    ROUND(year_ago.utilization, 4)                    AS utilization_year_ago,
    ROUND(now_.utilization, 4)                        AS utilization_now,
    ROUND(now_.utilization - year_ago.utilization, 4) AS change_in_year
FROM province_month now_
LEFT JOIN province_month year_ago
       ON year_ago.province_code = now_.province_code
      AND year_ago.snapshot_month = now_.snapshot_month - INTERVAL 12 MONTH
WHERE now_.snapshot_month = (SELECT MAX(snapshot_month) FROM monthly_snapshot)
  AND now_.accounts >= 10
ORDER BY utilization_now DESC;


-- Q3. What share of accounts and balances are 30+ and 90+ days late each month?
SELECT
    snapshot_month,
    open_accounts,
    accounts_30_plus,
    rate_30_plus,
    balance_rate_30_plus,
    accounts_90_plus,
    ROUND(AVG(rate_30_plus) OVER (ORDER BY snapshot_month
                                  ROWS BETWEEN 2 PRECEDING AND CURRENT ROW), 4) AS rate_30_plus_3m_avg
FROM vw_portfolio_monthly
ORDER BY snapshot_month;


-- Q4. Where do accounts in each bucket end up next month? (% of accounts, all months combined)
SELECT
    fb.bucket_code                                         AS from_bucket,
    COUNT(*)                                               AS account_months,
    ROUND(100 * AVG(nxt.bucket_code = 'CURRENT'), 1)       AS pct_to_current,
    ROUND(100 * AVG(nxt.bucket_code = 'DPD_1_29'), 1)      AS pct_to_1_29,
    ROUND(100 * AVG(nxt.bucket_code = 'DPD_30_59'), 1)     AS pct_to_30_59,
    ROUND(100 * AVG(nxt.bucket_code = 'DPD_60_89'), 1)     AS pct_to_60_89,
    ROUND(100 * AVG(nxt.bucket_code = 'DPD_90_179'), 1)    AS pct_to_90_179,
    ROUND(100 * AVG(nxt.bucket_code = 'CHARGED_OFF'), 1)   AS pct_to_charged_off
FROM monthly_snapshot prv
JOIN monthly_snapshot nxt
  ON nxt.account_id = prv.account_id
 AND nxt.snapshot_month = prv.snapshot_month + INTERVAL 1 MONTH
JOIN delinquency_bucket fb ON fb.bucket_code = prv.bucket_code
WHERE prv.bucket_code <> 'CHARGED_OFF'
GROUP BY fb.sort_order, fb.bucket_code
ORDER BY fb.sort_order;


-- Q5. Which opening quarters (vintages) perform worst?
SELECT
    vintage,
    MAX(accounts)                                                        AS accounts,
    MAX(IF(months_on_book = 3,  cumulative_30_plus_rate, NULL))          AS late_by_month_3,
    MAX(IF(months_on_book = 6,  cumulative_30_plus_rate, NULL))          AS late_by_month_6,
    MAX(IF(months_on_book = 9,  cumulative_30_plus_rate, NULL))          AS late_by_month_9,
    MAX(IF(months_on_book = 12, cumulative_30_plus_rate, NULL))          AS late_by_month_12
FROM vw_vintage_curve
GROUP BY vintage
ORDER BY vintage;


-- Q6. What predicts falling behind?
-- For accounts that were up to date in a month, the share that went 30+ days late
-- within the next 6 months, split by what we knew about them at the time.
WITH outcomes AS (
    SELECT
        s.*,
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
       CASE WHEN credit_score < 680 THEN 'Under 680' WHEN credit_score < 720 THEN '680-719'
            WHEN credit_score < 760 THEN '720-759' ELSE '760+' END AS band,
       COUNT(*) AS account_months,
       ROUND(100 * AVG(went_late), 1) AS pct_late_next_6m
FROM base GROUP BY 1, 2, 3
UNION ALL
SELECT 'Utilization',
       CASE WHEN utilization_rate < 0.30 THEN 1 WHEN utilization_rate < 0.60 THEN 2
            WHEN utilization_rate < 0.90 THEN 3 ELSE 4 END,
       CASE WHEN utilization_rate < 0.30 THEN 'Under 30%' WHEN utilization_rate < 0.60 THEN '30-59%'
            WHEN utilization_rate < 0.90 THEN '60-89%' ELSE '90%+' END,
       COUNT(*), ROUND(100 * AVG(went_late), 1)
FROM base GROUP BY 1, 2, 3
UNION ALL
SELECT 'CLTV',
       CASE WHEN cltv <= 0.50 THEN 1 WHEN cltv <= 0.65 THEN 2 WHEN cltv <= 0.80 THEN 3 ELSE 4 END,
       CASE WHEN cltv <= 0.50 THEN 'Up to 50%' WHEN cltv <= 0.65 THEN '50-65%'
            WHEN cltv <= 0.80 THEN '65-80%' ELSE 'Over 80%' END,
       COUNT(*), ROUND(100 * AVG(went_late), 1)
FROM base GROUP BY 1, 2, 3
UNION ALL
SELECT 'Risk segment',
       FIELD(risk_segment, 'LOW', 'MEDIUM', 'HIGH', 'VERY_HIGH'),
       risk_segment,
       COUNT(*), ROUND(100 * AVG(went_late), 1)
FROM base GROUP BY 1, 2, 3
ORDER BY factor, band_order;


-- Q7. Which open accounts need attention right now?
SELECT
    account_number,
    customer_name,
    balance,
    utilization_rate,
    days_past_due,
    credit_score,
    risk_segment,
    warning_count
FROM vw_early_warning
ORDER BY warning_count DESC, balance DESC
LIMIT 20;


-- Q8. Does the early-warning list actually work?
-- Same warning signs, checked on past months, compared with what happened next.
WITH history AS (
    SELECT
        s.account_id,
        s.snapshot_month,
        s.days_past_due,
        (s.utilization_rate >= 0.90)
          + (s.utilization_rate - COALESCE(LAG(s.utilization_rate, 3) OVER w, s.utilization_rate) >= 0.25)
          + (s.credit_score - COALESCE(LAG(s.credit_score, 3) OVER w, s.credit_score) <= -40)
          + (MAX(s.days_past_due) OVER (PARTITION BY s.account_id ORDER BY s.snapshot_month
                                        ROWS BETWEEN 5 PRECEDING AND CURRENT ROW) > 0)
          + (s.cltv > 0.80) AS warning_count,
        MAX(s.days_past_due >= 30) OVER (PARTITION BY s.account_id ORDER BY s.snapshot_month
                                         ROWS BETWEEN 1 FOLLOWING AND 6 FOLLOWING) AS late_next_6m
    FROM monthly_snapshot s
    WINDOW w AS (PARTITION BY s.account_id ORDER BY s.snapshot_month)
)
SELECT
    CASE WHEN warning_count = 0 THEN '0 warnings'
         WHEN warning_count = 1 THEN '1 warning'
         ELSE '2+ warnings' END                       AS warnings,
    COUNT(*)                                          AS account_months,
    ROUND(100 * AVG(COALESCE(late_next_6m, 0)), 1)    AS pct_late_next_6m
FROM history
WHERE days_past_due < 30
  AND snapshot_month <= (SELECT MAX(snapshot_month) - INTERVAL 6 MONTH FROM monthly_snapshot)
GROUP BY 1
ORDER BY 1;


-- Q9. How did prime rate cuts affect interest income and minimum payments?
SELECT
    snapshot_month,
    prime_rate,
    prime_rate - LAG(prime_rate) OVER (ORDER BY snapshot_month) AS prime_change,
    total_balance,
    interest_income,
    ROUND(1000 * interest_income / total_balance, 2)            AS interest_per_1000_borrowed,
    ROUND(minimum_payments_due / open_accounts, 2)              AS avg_minimum_payment
FROM vw_portfolio_monthly
ORDER BY snapshot_month;


-- Q10. How much has been charged off, and what did those accounts look like 6 months before?
SELECT
    h.account_number,
    co.snapshot_month                                        AS charged_off_month,
    co.closing_balance                                       AS amount_lost,
    TIMESTAMPDIFF(MONTH, h.open_date, co.snapshot_month)     AS months_since_opening,
    h.origination_credit_score,
    before_.credit_score                                     AS score_6m_before,
    before_.utilization_rate                                 AS utilization_6m_before,
    before_.risk_segment                                     AS risk_segment_6m_before
FROM monthly_snapshot co
JOIN heloc_account h ON h.account_id = co.account_id
LEFT JOIN monthly_snapshot before_
       ON before_.account_id = co.account_id
      AND before_.snapshot_month = co.snapshot_month - INTERVAL 6 MONTH
WHERE co.bucket_code = 'CHARGED_OFF'
ORDER BY co.snapshot_month;

SELECT
    COUNT(*)                                                        AS accounts_charged_off,
    SUM(closing_balance)                                            AS total_lost,
    ROUND(SUM(closing_balance)
          / (SELECT AVG(total_balance) FROM vw_portfolio_monthly) / 2, 4) AS yearly_loss_rate
FROM monthly_snapshot
WHERE bucket_code = 'CHARGED_OFF';


-- Q11. How concentrated is the portfolio? (balance held by each 10% of borrowers)
WITH ranked AS (
    SELECT
        closing_balance,
        NTILE(10) OVER (ORDER BY closing_balance DESC) AS decile
    FROM monthly_snapshot
    WHERE snapshot_month = (SELECT MAX(snapshot_month) FROM monthly_snapshot)
      AND bucket_code <> 'CHARGED_OFF'
)
SELECT
    decile,
    COUNT(*)                                                         AS accounts,
    SUM(closing_balance)                                             AS balance,
    ROUND(SUM(closing_balance) / SUM(SUM(closing_balance)) OVER (), 4) AS share_of_balance,
    ROUND(SUM(SUM(closing_balance)) OVER (ORDER BY decile)
          / SUM(SUM(closing_balance)) OVER (), 4)                    AS running_share
FROM ranked
GROUP BY decile
ORDER BY decile;
