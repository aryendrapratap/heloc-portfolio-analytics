"""Checks that the generated data is complete and adds up."""

from conftest import run_script, scalar


def test_each_customer_has_one_home_and_one_account(db):
    customers = scalar(db, "SELECT COUNT(*) FROM customer")
    assert customers > 0
    assert scalar(db, "SELECT COUNT(*) FROM property") == customers
    assert scalar(db, "SELECT COUNT(*) FROM heloc_account") == customers


def test_snapshot_totals_match_transactions(db):
    mismatches = scalar(db, """
        SELECT COUNT(*)
        FROM monthly_snapshot s
        LEFT JOIN (
            SELECT account_id,
                   DATE_FORMAT(transaction_date, '%Y-%m-01') AS month,
                   SUM(IF(transaction_type = 'DRAW', amount, 0))     AS draws,
                   SUM(IF(transaction_type = 'PAYMENT', amount, 0))  AS payments,
                   SUM(IF(transaction_type = 'INTEREST', amount, 0)) AS interest,
                   SUM(IF(transaction_type = 'FEE', amount, 0))      AS fees
            FROM account_transaction
            GROUP BY account_id, month
        ) t ON t.account_id = s.account_id AND t.month = s.snapshot_month
        WHERE COALESCE(t.draws, 0) <> s.total_draws
           OR COALESCE(t.payments, 0) <> s.total_payments
           OR COALESCE(t.interest, 0) <> s.interest_charged
           OR COALESCE(t.fees, 0) <> s.fees_charged
    """)
    assert mismatches == 0


def test_each_month_starts_where_the_last_one_ended(db):
    broken = scalar(db, """
        SELECT COUNT(*)
        FROM monthly_snapshot a
        JOIN monthly_snapshot b
          ON b.account_id = a.account_id
         AND b.snapshot_month = a.snapshot_month + INTERVAL 1 MONTH
        WHERE b.opening_balance <> a.closing_balance
    """)
    assert broken == 0


def test_account_balance_matches_latest_snapshot(db):
    mismatches = scalar(db, """
        SELECT COUNT(*)
        FROM heloc_account h
        JOIN monthly_snapshot s
          ON s.account_id = h.account_id
         AND s.snapshot_month = (SELECT MAX(snapshot_month) FROM monthly_snapshot
                                  WHERE account_id = h.account_id)
        WHERE s.closing_balance <> h.current_balance
    """)
    assert mismatches == 0


def test_credit_limits_follow_canadian_rules(db):
    over_65 = scalar(db, """
        SELECT COUNT(*) FROM heloc_account h JOIN property p ON p.property_id = h.property_id
        WHERE h.original_credit_limit > 0.65 * p.appraised_value
    """)
    over_80 = scalar(db, """
        SELECT COUNT(*) FROM heloc_account h JOIN property p ON p.property_id = h.property_id
        WHERE h.original_credit_limit + p.mortgage_balance > 0.80 * p.appraised_value
    """)
    assert over_65 == 0
    assert over_80 == 0


def test_no_transactions_outside_account_dates(db):
    outside = scalar(db, """
        SELECT COUNT(*)
        FROM account_transaction t
        JOIN heloc_account h ON h.account_id = t.account_id
        WHERE t.transaction_date < h.open_date
           OR (h.close_date IS NOT NULL AND t.transaction_date > h.close_date)
    """)
    assert outside == 0


def test_closed_accounts_are_settled(db):
    db.execute("SELECT account_status, current_balance, credit_limit FROM heloc_account "
               "WHERE account_status IN ('CLOSED', 'CHARGED_OFF')")
    for status, balance, limit in db.fetchall():
        assert limit == 0
        if status == "CLOSED":
            assert balance == 0


def test_every_snapshot_has_a_risk_segment(db):
    assert scalar(db, "SELECT COUNT(*) FROM monthly_snapshot WHERE risk_segment IS NULL") == 0


def test_generator_makes_the_same_data_every_time(db):
    fingerprint_sql = """
        SELECT CONCAT((SELECT COUNT(*) FROM account_transaction), '|',
                      (SELECT SUM(amount) FROM account_transaction), '|',
                      (SELECT SUM(closing_balance) FROM monthly_snapshot))
    """
    before = scalar(db, fingerprint_sql)
    db.connection.commit()  # finish this read first, or the reload would wait on it
    run_script("data_generator/generate_data.py")
    assert scalar(db, fingerprint_sql) == before
