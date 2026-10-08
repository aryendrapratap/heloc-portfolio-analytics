"""Checks that the reporting views return sensible numbers."""

from conftest import scalar


def test_portfolio_view_has_one_row_per_month(db):
    months = scalar(db, "SELECT COUNT(DISTINCT snapshot_month) FROM monthly_snapshot")
    assert scalar(db, "SELECT COUNT(*) FROM vw_portfolio_monthly") == months


def test_portfolio_balance_matches_the_snapshots(db):
    from_view = scalar(db, "SELECT total_balance FROM vw_portfolio_monthly "
                           "ORDER BY snapshot_month DESC LIMIT 1")
    from_table = scalar(db, """
        SELECT SUM(closing_balance) FROM monthly_snapshot
        WHERE snapshot_month = (SELECT MAX(snapshot_month) FROM monthly_snapshot)
          AND bucket_code <> 'CHARGED_OFF'
    """)
    assert from_view == from_table


def test_bucket_shares_add_up_to_100_percent(db):
    db.execute("SELECT snapshot_month, SUM(share_of_accounts) FROM vw_delinquency_buckets "
               "GROUP BY snapshot_month")
    for month, total in db.fetchall():
        assert abs(total - 1) < 0.001, month


def test_roll_and_cure_rates_are_between_0_and_1(db):
    bad = scalar(db, """
        SELECT COUNT(*) FROM vw_roll_rates
        WHERE roll_rate NOT BETWEEN 0 AND 1 OR (cure_rate IS NOT NULL AND cure_rate NOT BETWEEN 0 AND 1)
    """)
    assert bad == 0


def test_vintage_curves_never_go_down(db):
    drops = scalar(db, """
        SELECT COUNT(*) FROM (
            SELECT cumulative_30_plus_rate
                   - LAG(cumulative_30_plus_rate) OVER (PARTITION BY vintage ORDER BY months_on_book) AS change_
            FROM vw_vintage_curve
        ) x
        WHERE change_ < 0
    """)
    assert drops == 0


def test_every_account_appears_once_in_latest_view(db):
    assert scalar(db, "SELECT COUNT(*) FROM vw_account_latest") == scalar(db, "SELECT COUNT(*) FROM heloc_account")
    assert scalar(db, "SELECT COUNT(DISTINCT account_id) FROM vw_account_latest") == \
        scalar(db, "SELECT COUNT(*) FROM heloc_account")


def test_watch_list_only_has_open_accounts_not_yet_30_days_late(db):
    bad = scalar(db, """
        SELECT COUNT(*) FROM vw_early_warning w
        JOIN heloc_account h ON h.account_id = w.account_id
        WHERE h.account_status NOT IN ('ACTIVE', 'FROZEN') OR w.days_past_due >= 30 OR w.warning_count < 1
    """)
    assert bad == 0
