"""Checks the risk scoring function and the stored procedures."""

import pymysql
import pytest

from conftest import connect, scalar


@pytest.mark.parametrize("score, cltv, utilization, dpd, expected", [
    (800, 0.50, 0.20, 0, "LOW"),        # 0 points
    (760, 0.65, 0.49, 0, "LOW"),        # 0 points, right on the edges
    (700, 0.70, 0.60, 0, "MEDIUM"),     # 2 + 1 + 1 = 4
    (650, 0.78, 0.90, 0, "HIGH"),       # 3 + 2 + 2 = 7
    (780, 0.50, 0.20, 45, "MEDIUM"),    # 30-59 days late alone = 4
    (600, 0.85, 0.97, 45, "VERY_HIGH"), # 4 + 3 + 3 + 4 = 14
])
def test_risk_segment(db, score, cltv, utilization, dpd, expected):
    assert scalar(db, "SELECT fn_risk_segment(%s, %s, %s, %s)", (score, cltv, utilization, dpd)) == expected


def test_post_transaction_returns_the_new_balance(db):
    db.execute("SELECT account_id, current_balance FROM heloc_account "
               "WHERE account_status = 'ACTIVE' AND current_balance > 1000 ORDER BY account_id LIMIT 1")
    account_id, balance = db.fetchone()

    db.execute("CALL sp_post_transaction(%s, '2026-10-12', 'PAYMENT', 500, 'ONLINE', @new_balance)",
               (account_id,))
    assert scalar(db, "SELECT @new_balance") == balance - 500


def test_month_end_must_run_in_order(db):
    with pytest.raises(pymysql.MySQLError, match="Months must be run in order"):
        db.execute("CALL sp_run_month_end('2026-12-01')")


def test_month_end_closes_the_next_month(rebuild_after):
    conn = connect()
    try:
        with conn.cursor() as cur:
            open_accounts = scalar(cur, """
                SELECT COUNT(*) FROM heloc_account
                WHERE account_status IN ('ACTIVE', 'FROZEN') AND open_date <= '2026-10-31'
            """)

            cur.execute("CALL sp_run_month_end('2026-10-01')")
            while cur.nextset():
                pass
            conn.commit()

            assert scalar(cur, "SELECT COUNT(*) FROM monthly_snapshot "
                               "WHERE snapshot_month = '2026-10-01'") == open_accounts
            assert scalar(cur, "SELECT COUNT(*) FROM monthly_snapshot "
                               "WHERE snapshot_month = '2026-10-01' AND risk_segment IS NULL") == 0
            # every account's balance still matches its newest snapshot
            assert scalar(cur, """
                SELECT COUNT(*) FROM heloc_account h
                JOIN monthly_snapshot s ON s.account_id = h.account_id AND s.snapshot_month = '2026-10-01'
                WHERE s.closing_balance <> h.current_balance
            """) == 0
    finally:
        conn.rollback()
        conn.close()
