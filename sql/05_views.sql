-- 05_views.sql
-- Saved reports. Query them like tables, e.g. SELECT * FROM vw_portfolio_monthly;

-- Whole portfolio, one row per month
CREATE OR REPLACE VIEW vw_portfolio_monthly AS
SELECT
    snapshot_month,
    SUM(bucket_code <> 'CHARGED_OFF')                                        AS open_accounts,
    SUM(IF(bucket_code <> 'CHARGED_OFF', closing_balance, 0))                AS total_balance,
    SUM(IF(bucket_code <> 'CHARGED_OFF', credit_limit, 0))                   AS total_limit,
    ROUND(SUM(IF(bucket_code <> 'CHARGED_OFF', closing_balance, 0))
          / NULLIF(SUM(IF(bucket_code <> 'CHARGED_OFF', credit_limit, 0)), 0), 4) AS utilization,
    SUM(total_draws)                                                         AS draws,
    SUM(total_payments)                                                      AS payments,
    SUM(interest_charged)                                                    AS interest_income,
    SUM(fees_charged)                                                        AS fee_income,
    MAX(prime_rate)                                                          AS prime_rate,
    ROUND(SUM(interest_rate * closing_balance) / NULLIF(SUM(closing_balance), 0), 4) AS avg_interest_rate,
    SUM(minimum_payment_due)                                                 AS minimum_payments_due,
    SUM(days_past_due >= 30 AND bucket_code <> 'CHARGED_OFF')                AS accounts_30_plus,
    ROUND(SUM(days_past_due >= 30 AND bucket_code <> 'CHARGED_OFF')
          / NULLIF(SUM(bucket_code <> 'CHARGED_OFF'), 0), 4)                 AS rate_30_plus,
    ROUND(SUM(IF(days_past_due >= 30 AND bucket_code <> 'CHARGED_OFF', closing_balance, 0))
          / NULLIF(SUM(IF(bucket_code <> 'CHARGED_OFF', closing_balance, 0)), 0), 4) AS balance_rate_30_plus,
    SUM(days_past_due >= 90 AND bucket_code <> 'CHARGED_OFF')                AS accounts_90_plus,
    SUM(bucket_code = 'CHARGED_OFF')                                         AS charge_offs,
    SUM(IF(bucket_code = 'CHARGED_OFF', closing_balance, 0))                 AS charge_off_balance
FROM monthly_snapshot
GROUP BY snapshot_month;


-- Accounts and balances in each lateness bucket, every month (zeros included for charts)
CREATE OR REPLACE VIEW vw_delinquency_buckets AS
SELECT
    m.snapshot_month,
    b.sort_order,
    b.bucket_code,
    b.bucket_label,
    COUNT(s.snapshot_id)                                                     AS accounts,
    COALESCE(SUM(s.closing_balance), 0)                                      AS balance,
    ROUND(COUNT(s.snapshot_id)
          / SUM(COUNT(s.snapshot_id)) OVER (PARTITION BY m.snapshot_month), 4) AS share_of_accounts
FROM (SELECT DISTINCT snapshot_month FROM monthly_snapshot) m
CROSS JOIN delinquency_bucket b
LEFT JOIN monthly_snapshot s
       ON s.snapshot_month = m.snapshot_month
      AND s.bucket_code = b.bucket_code
GROUP BY m.snapshot_month, b.sort_order, b.bucket_code, b.bucket_label;


-- Where accounts in each bucket went the following month.
-- roll_rate = got worse, cure_rate = back to current
CREATE OR REPLACE VIEW vw_roll_rates AS
SELECT
    nxt.snapshot_month,
    fb.sort_order,
    fb.bucket_code                                                           AS from_bucket,
    COUNT(*)                                                                 AS accounts,
    SUM(tb.sort_order > fb.sort_order)                                       AS rolled_worse,
    SUM(tb.sort_order = fb.sort_order)                                       AS stayed,
    SUM(tb.bucket_code = 'CURRENT' AND fb.bucket_code <> 'CURRENT')          AS cured,
    ROUND(SUM(tb.sort_order > fb.sort_order) / COUNT(*), 4)                  AS roll_rate,
    IF(fb.bucket_code = 'CURRENT', NULL,
       ROUND(SUM(tb.bucket_code = 'CURRENT') / COUNT(*), 4))                 AS cure_rate
FROM monthly_snapshot prv
JOIN monthly_snapshot nxt
  ON nxt.account_id = prv.account_id
 AND nxt.snapshot_month = prv.snapshot_month + INTERVAL 1 MONTH
JOIN delinquency_bucket fb ON fb.bucket_code = prv.bucket_code
JOIN delinquency_bucket tb ON tb.bucket_code = nxt.bucket_code
WHERE prv.bucket_code <> 'CHARGED_OFF'
GROUP BY nxt.snapshot_month, fb.sort_order, fb.bucket_code;


-- Vintage curves: of the accounts opened in a quarter, what share had been 30+ days late
-- by each month after opening. Only accounts opened inside the data window are used,
-- because older accounts are missing their early months.
CREATE OR REPLACE VIEW vw_vintage_curve AS
WITH RECURSIVE month_numbers (n) AS (
    SELECT 1
    UNION ALL
    SELECT n + 1 FROM month_numbers WHERE n < 36
),
vintage_accounts AS (
    SELECT
        h.account_id,
        CONCAT(YEAR(h.open_date), '-Q', QUARTER(h.open_date)) AS vintage,
        TIMESTAMPDIFF(MONTH, DATE_FORMAT(h.open_date, '%Y-%m-01'),
                      (SELECT MAX(snapshot_month) FROM monthly_snapshot)) AS months_observed,
        (SELECT MIN(TIMESTAMPDIFF(MONTH, DATE_FORMAT(h.open_date, '%Y-%m-01'), s.snapshot_month))
           FROM monthly_snapshot s
          WHERE s.account_id = h.account_id
            AND s.days_past_due >= 30) AS first_late_month
    FROM heloc_account h
    WHERE h.open_date >= (SELECT MIN(snapshot_month) FROM monthly_snapshot)
)
SELECT
    v.vintage,
    m.n                                                                      AS months_on_book,
    COUNT(*)                                                                 AS accounts,
    COALESCE(SUM(v.first_late_month <= m.n), 0)                              AS ever_30_plus,
    ROUND(COALESCE(SUM(v.first_late_month <= m.n), 0) / COUNT(*), 4)        AS cumulative_30_plus_rate
FROM vintage_accounts v
JOIN month_numbers m ON m.n <= v.months_observed
GROUP BY v.vintage, m.n;


-- Risk segments over time
CREATE OR REPLACE VIEW vw_risk_segment_monthly AS
SELECT
    snapshot_month,
    risk_segment,
    COUNT(*)                                                                 AS accounts,
    SUM(closing_balance)                                                     AS balance,
    ROUND(AVG(utilization_rate), 4)                                          AS avg_utilization,
    ROUND(AVG(credit_score))                                                 AS avg_credit_score,
    ROUND(AVG(cltv), 4)                                                      AS avg_cltv,
    ROUND(SUM(days_past_due >= 30) / COUNT(*), 4)                            AS rate_30_plus
FROM monthly_snapshot
WHERE bucket_code <> 'CHARGED_OFF'
GROUP BY snapshot_month, risk_segment;


-- Province breakdown for the latest month
CREATE OR REPLACE VIEW vw_province_latest AS
SELECT
    p.province_code,
    pr.province_name,
    COUNT(*)                                                                 AS accounts,
    SUM(s.closing_balance)                                                   AS balance,
    ROUND(SUM(s.closing_balance) / NULLIF(SUM(s.credit_limit), 0), 4)        AS utilization,
    ROUND(AVG(s.cltv), 4)                                                    AS avg_cltv,
    ROUND(AVG(s.property_value))                                             AS avg_property_value,
    SUM(s.days_past_due >= 30)                                               AS accounts_30_plus
FROM monthly_snapshot s
JOIN heloc_account h ON h.account_id = s.account_id
JOIN property p      ON p.property_id = h.property_id
JOIN province pr     ON pr.province_code = p.province_code
WHERE s.snapshot_month = (SELECT MAX(snapshot_month) FROM monthly_snapshot)
  AND s.bucket_code <> 'CHARGED_OFF'
GROUP BY p.province_code, pr.province_name;


-- Every account with its most recent month-end numbers
CREATE OR REPLACE VIEW vw_account_latest AS
SELECT
    h.account_id,
    h.account_number,
    CONCAT(c.first_name, ' ', c.last_name)                                   AS customer_name,
    p.province_code,
    p.city,
    h.account_status,
    h.open_date,
    s.snapshot_month                                                         AS as_of_month,
    s.credit_limit,
    s.closing_balance                                                        AS balance,
    s.utilization_rate,
    s.days_past_due,
    s.bucket_code,
    h.origination_credit_score,
    s.credit_score,
    s.credit_score - h.origination_credit_score                              AS score_change,
    s.cltv,
    s.risk_segment
FROM heloc_account h
JOIN customer c ON c.customer_id = h.customer_id
JOIN property p ON p.property_id = h.property_id
JOIN monthly_snapshot s
  ON s.account_id = h.account_id
 AND s.snapshot_month = (SELECT MAX(s2.snapshot_month)
                           FROM monthly_snapshot s2
                          WHERE s2.account_id = h.account_id);


-- Open accounts that are not seriously late yet but show warning signs
CREATE OR REPLACE VIEW vw_early_warning AS
WITH history AS (
    SELECT
        s.account_id,
        s.snapshot_month,
        s.closing_balance,
        s.utilization_rate,
        s.days_past_due,
        s.credit_score,
        s.cltv,
        s.risk_segment,
        LAG(s.credit_score, 3)     OVER w                                    AS score_3_months_ago,
        LAG(s.utilization_rate, 3) OVER w                                    AS utilization_3_months_ago,
        MAX(s.days_past_due) OVER (PARTITION BY s.account_id ORDER BY s.snapshot_month
                                   ROWS BETWEEN 5 PRECEDING AND CURRENT ROW) AS worst_dpd_6_months
    FROM monthly_snapshot s
    WINDOW w AS (PARTITION BY s.account_id ORDER BY s.snapshot_month)
),
flags AS (
    SELECT
        h.*,
        (h.utilization_rate >= 0.90)                                         AS flag_maxed_out,
        (h.utilization_rate - COALESCE(h.utilization_3_months_ago, h.utilization_rate) >= 0.25) AS flag_borrowing_jump,
        (h.credit_score - COALESCE(h.score_3_months_ago, h.credit_score) <= -40) AS flag_score_drop,
        (h.worst_dpd_6_months > 0)                                           AS flag_recently_late,
        (h.cltv > 0.80)                                                      AS flag_high_cltv
    FROM history h
    WHERE h.snapshot_month = (SELECT MAX(snapshot_month) FROM monthly_snapshot)
      AND h.days_past_due < 30
)
SELECT
    f.account_id,
    a.account_number,
    CONCAT(c.first_name, ' ', c.last_name)                                   AS customer_name,
    f.snapshot_month                                                         AS as_of_month,
    f.closing_balance                                                        AS balance,
    f.utilization_rate,
    f.days_past_due,
    f.credit_score,
    f.risk_segment,
    f.flag_maxed_out,
    f.flag_borrowing_jump,
    f.flag_score_drop,
    f.flag_recently_late,
    f.flag_high_cltv,
    f.flag_maxed_out + f.flag_borrowing_jump + f.flag_score_drop
        + f.flag_recently_late + f.flag_high_cltv                            AS warning_count
FROM flags f
JOIN heloc_account a ON a.account_id = f.account_id
JOIN customer c      ON c.customer_id = a.customer_id
WHERE a.account_status IN ('ACTIVE', 'FROZEN')
  AND f.flag_maxed_out + f.flag_borrowing_jump + f.flag_score_drop
      + f.flag_recently_late + f.flag_high_cltv > 0;
