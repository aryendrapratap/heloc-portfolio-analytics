"""Checks that the triggers and constraints block bad data."""

import pymysql
import pytest

from conftest import scalar

ADD_TXN = """
    INSERT INTO account_transaction (account_id, transaction_date, transaction_type, amount, channel)
    VALUES (%s, '2026-10-02', %s, %s, 'ONLINE')
"""


@pytest.fixture
def active_account(db):
    db.execute("""
        SELECT account_id, credit_limit, current_balance
        FROM heloc_account
        WHERE account_status = 'ACTIVE' AND credit_limit - current_balance > 10000
        ORDER BY account_id LIMIT 1
    """)
    return db.fetchone()


@pytest.fixture
def charged_off_account(db):
    return scalar(db, "SELECT MIN(account_id) FROM heloc_account WHERE account_status = 'CHARGED_OFF'")


def test_draw_over_the_limit_is_blocked(db, active_account):
    account_id, limit, balance = active_account
    with pytest.raises(pymysql.MySQLError, match="over the credit limit"):
        db.execute(ADD_TXN, (account_id, "DRAW", limit - balance + 1))


def test_draw_updates_balance_and_audit_log(db, active_account):
    account_id, _, balance = active_account
    db.execute(ADD_TXN, (account_id, "DRAW", 5000))

    assert scalar(db, "SELECT current_balance FROM heloc_account WHERE account_id = %s",
                  (account_id,)) == balance + 5000
    db.execute("SELECT old_value, new_value FROM audit_log WHERE record_id = %s "
               "AND column_name = 'current_balance' ORDER BY audit_id DESC LIMIT 1", (account_id,))
    assert db.fetchone() == (str(balance), str(balance + 5000))


def test_charged_off_account_cannot_draw(db, charged_off_account):
    with pytest.raises(pymysql.MySQLError, match="Only active accounts"):
        db.execute(ADD_TXN, (charged_off_account, "DRAW", 100))


def test_payment_bigger_than_balance_is_blocked(db, active_account):
    account_id, _, balance = active_account
    with pytest.raises(pymysql.MySQLError, match="more than the balance"):
        db.execute(ADD_TXN, (account_id, "PAYMENT", balance + 1))


def test_transactions_cannot_be_changed_or_deleted(db):
    with pytest.raises(pymysql.MySQLError, match="cannot be changed"):
        db.execute("UPDATE account_transaction SET amount = 1 WHERE transaction_id = 1")
    with pytest.raises(pymysql.MySQLError, match="cannot be deleted"):
        db.execute("DELETE FROM account_transaction WHERE transaction_id = 1")


def test_limit_above_65_percent_of_home_value_is_blocked(db, active_account):
    account_id = active_account[0]
    value = scalar(db, """
        SELECT p.appraised_value FROM property p
        JOIN heloc_account h ON h.property_id = p.property_id WHERE h.account_id = %s
    """, (account_id,))
    with pytest.raises(pymysql.MySQLError, match="65%"):
        db.execute("INSERT INTO credit_limit_change (account_id, change_date, old_limit, new_limit, reason) "
                   "VALUES (%s, '2026-10-03', 0, %s, 'INCREASE_REQUEST')", (account_id, round(float(value) * 0.66, 2)))


def test_limit_change_updates_the_account(db, active_account):
    account_id, limit, balance = active_account
    new_limit = limit - 5000
    db.execute("INSERT INTO credit_limit_change (account_id, change_date, old_limit, new_limit, reason) "
               "VALUES (%s, '2026-10-03', 0, %s, 'RISK_REDUCTION')", (account_id, new_limit))

    assert scalar(db, "SELECT credit_limit FROM heloc_account WHERE account_id = %s",
                  (account_id,)) == new_limit
    # the old limit is filled in by the trigger, not by whoever inserted the row
    assert scalar(db, "SELECT old_limit FROM credit_limit_change WHERE account_id = %s "
                      "ORDER BY limit_change_id DESC LIMIT 1", (account_id,)) == limit


def test_charged_off_account_cannot_be_reopened(db, charged_off_account):
    with pytest.raises(pymysql.MySQLError, match="cannot be reopened"):
        db.execute("UPDATE heloc_account SET account_status = 'ACTIVE', close_date = NULL "
                   "WHERE account_id = %s", (charged_off_account,))


def test_property_must_belong_to_the_customer(db):
    with pytest.raises(pymysql.MySQLError, match="different customer"):
        db.execute("""
            INSERT INTO heloc_account (account_number, customer_id, property_id, open_date,
                                       original_credit_limit, credit_limit, rate_margin,
                                       origination_credit_score)
            VALUES ('HL-999999', 2, 1, '2026-10-01', 10000, 10000, 0.005, 750)
        """)


@pytest.mark.parametrize("email, province, rule", [
    ("not-an-email", "ON", "chk_customer_email"),
    ("test@example.com", "XX", "foreign key"),
])
def test_bad_customer_data_is_rejected(db, email, province, rule):
    with pytest.raises(pymysql.MySQLError, match=rule):
        db.execute("""
            INSERT INTO customer (first_name, last_name, date_of_birth, email, province_code,
                                  annual_income, employment_status)
            VALUES ('Test', 'User', '1990-01-01', %s, %s, 50000, 'EMPLOYED')
        """, (email, province))
