-- 04_procedures.sql
-- Reusable routines: risk scoring, posting a transaction, and month-end processing.

DROP FUNCTION IF EXISTS fn_risk_segment;
DROP PROCEDURE IF EXISTS sp_assign_risk_segments;
DROP PROCEDURE IF EXISTS sp_post_transaction;
DROP PROCEDURE IF EXISTS sp_run_month_end;

DELIMITER $$

-- Risk label from four signals. Each adds points; more points = riskier.
--   credit score  760+ 0 | 720-759 1 | 680-719 2 | 640-679 3 | under 640 4
--   CLTV          <=65% 0 | <=75% 1 | <=80% 2 | over 80% 3
--   utilization   <50% 0 | <80% 1 | <95% 2 | 95%+ 3
--   days late     0 0 | 1-29 2 | 30-59 4 | 60+ 6
-- 0-2 LOW, 3-4 MEDIUM, 5-7 HIGH, 8+ VERY_HIGH
CREATE FUNCTION fn_risk_segment(
    p_score SMALLINT,
    p_cltv DECIMAL(7,4),
    p_utilization DECIMAL(7,4),
    p_dpd SMALLINT
) RETURNS VARCHAR(10)
DETERMINISTIC
BEGIN
    DECLARE v_points INT DEFAULT 0;

    SET v_points = v_points + CASE
        WHEN p_score >= 760 THEN 0
        WHEN p_score >= 720 THEN 1
        WHEN p_score >= 680 THEN 2
        WHEN p_score >= 640 THEN 3
        ELSE 4 END;
    SET v_points = v_points + CASE
        WHEN p_cltv <= 0.65 THEN 0
        WHEN p_cltv <= 0.75 THEN 1
        WHEN p_cltv <= 0.80 THEN 2
        ELSE 3 END;
    SET v_points = v_points + CASE
        WHEN p_utilization < 0.50 THEN 0
        WHEN p_utilization < 0.80 THEN 1
        WHEN p_utilization < 0.95 THEN 2
        ELSE 3 END;
    SET v_points = v_points + CASE
        WHEN p_dpd = 0 THEN 0
        WHEN p_dpd < 30 THEN 2
        WHEN p_dpd < 60 THEN 4
        ELSE 6 END;

    RETURN CASE
        WHEN v_points <= 2 THEN 'LOW'
        WHEN v_points <= 4 THEN 'MEDIUM'
        WHEN v_points <= 7 THEN 'HIGH'
        ELSE 'VERY_HIGH' END;
END$$

-- Fill in risk_segment for one month, or every month if p_month is NULL
CREATE PROCEDURE sp_assign_risk_segments(IN p_month DATE)
BEGIN
    UPDATE monthly_snapshot
       SET risk_segment = IF(bucket_code = 'CHARGED_OFF', 'VERY_HIGH',
                             fn_risk_segment(credit_score, cltv, utilization_rate, days_past_due))
     WHERE p_month IS NULL OR snapshot_month = p_month;
END$$

-- Record a draw, payment, interest charge or fee and return the new balance.
-- The triggers check the rules and update the balance.
CREATE PROCEDURE sp_post_transaction(
    IN p_account_id INT,
    IN p_date DATE,
    IN p_type VARCHAR(10),
    IN p_amount DECIMAL(14,2),
    IN p_channel VARCHAR(12),
    OUT p_new_balance DECIMAL(14,2)
)
BEGIN
    INSERT INTO account_transaction (account_id, transaction_date, transaction_type, amount, channel, description)
    VALUES (p_account_id, p_date, p_type, p_amount, p_channel, 'Posted with sp_post_transaction');

    SELECT current_balance INTO p_new_balance
      FROM heloc_account
     WHERE account_id = p_account_id;
END$$

-- Close out a month for every open account:
--   1. collect the minimum payment by automatic debit from customers who were up to date
--   2. charge interest, and a late fee if the minimum wasn't paid
--   3. save the month-end snapshot
--   4. freeze, unfreeze or charge off accounts based on how late they are
--   5. assign risk segments
-- Months must be run in order. Everything is undone if any step fails.
CREATE PROCEDURE sp_run_month_end(IN p_month DATE)
BEGIN
    DECLARE v_done INT DEFAULT 0;
    DECLARE v_end DATE;
    DECLARE v_last_month DATE;
    DECLARE v_prime DECIMAL(7,4);
    DECLARE v_account INT;
    DECLARE v_status VARCHAR(20);
    DECLARE v_limit DECIMAL(14,2);
    DECLARE v_margin DECIMAL(7,4);
    DECLARE v_balance DECIMAL(14,2);
    DECLARE v_has_prev INT;
    DECLARE v_opening DECIMAL(14,2);
    DECLARE v_prev_interest DECIMAL(14,2);
    DECLARE v_prev_fees DECIMAL(14,2);
    DECLARE v_prev_min_due DECIMAL(14,2);
    DECLARE v_prev_payments DECIMAL(14,2);
    DECLARE v_prev_dpd INT;
    DECLARE v_score INT;
    DECLARE v_value DECIMAL(14,2);
    DECLARE v_cltv DECIMAL(7,4);
    DECLARE v_min_due DECIMAL(14,2);
    DECLARE v_paid DECIMAL(14,2);
    DECLARE v_autopay DECIMAL(14,2);
    DECLARE v_interest DECIMAL(14,2);
    DECLARE v_missed INT;
    DECLARE v_draws DECIMAL(14,2);
    DECLARE v_payments DECIMAL(14,2);
    DECLARE v_interest_total DECIMAL(14,2);
    DECLARE v_fees DECIMAL(14,2);
    DECLARE v_closing DECIMAL(14,2);
    DECLARE v_dpd INT;
    DECLARE v_bucket VARCHAR(20);

    DECLARE account_cursor CURSOR FOR
        SELECT account_id
          FROM heloc_account
         WHERE account_status IN ('ACTIVE', 'FROZEN')
           AND open_date <= v_end
         ORDER BY account_id;

    DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_done = 1;
    DECLARE EXIT HANDLER FOR SQLEXCEPTION
    BEGIN
        ROLLBACK;
        RESIGNAL;
    END;

    SET p_month = DATE_FORMAT(p_month, '%Y-%m-01');
    SET v_end = LAST_DAY(p_month);

    SELECT MAX(snapshot_month) INTO v_last_month FROM monthly_snapshot;
    IF v_last_month IS NOT NULL AND p_month <> v_last_month + INTERVAL 1 MONTH THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'Months must be run in order: run the month after the last one processed';
    END IF;

    SELECT prime_rate INTO v_prime
      FROM prime_rate_history
     WHERE effective_date <= v_end
     ORDER BY effective_date DESC
     LIMIT 1;

    START TRANSACTION;
    SET v_done = 0;
    OPEN account_cursor;

    account_loop: LOOP
        FETCH account_cursor INTO v_account;
        IF v_done = 1 THEN
            LEAVE account_loop;
        END IF;

        SELECT account_status, credit_limit, rate_margin
          INTO v_status, v_limit, v_margin
          FROM heloc_account
         WHERE account_id = v_account;

        -- Last month's snapshot (MAX on one row; returns NULLs for a brand-new account)
        SELECT COUNT(*), MAX(closing_balance), MAX(interest_charged), MAX(fees_charged),
               MAX(minimum_payment_due), MAX(total_payments), MAX(days_past_due),
               MAX(credit_score), MAX(property_value), MAX(cltv)
          INTO v_has_prev, v_opening, v_prev_interest, v_prev_fees,
               v_prev_min_due, v_prev_payments, v_prev_dpd,
               v_score, v_value, v_cltv
          FROM monthly_snapshot
         WHERE account_id = v_account
           AND snapshot_month = v_last_month;

        IF v_has_prev = 0 THEN
            -- New account: start from what was known when it opened
            SELECT 0, 0, 0, 0, 0, 0, h.origination_credit_score, p.appraised_value,
                   ROUND((p.mortgage_balance + h.credit_limit) / p.appraised_value, 4)
              INTO v_opening, v_prev_interest, v_prev_fees, v_prev_min_due, v_prev_payments, v_prev_dpd,
                   v_score, v_value, v_cltv
              FROM heloc_account h
              JOIN property p ON p.property_id = h.property_id
             WHERE h.account_id = v_account;
        END IF;

        -- Minimum due: last month's interest and fees, plus anything left unpaid
        SET v_min_due = v_prev_interest + v_prev_fees + GREATEST(0, v_prev_min_due - v_prev_payments);

        SELECT COALESCE(SUM(amount), 0) INTO v_paid
          FROM account_transaction
         WHERE account_id = v_account
           AND transaction_type = 'PAYMENT'
           AND transaction_date BETWEEN p_month AND v_end;

        -- 1. Automatic debit on the 25th for customers who were up to date last month
        IF v_min_due > v_paid AND v_prev_dpd = 0 THEN
            SELECT current_balance INTO v_balance FROM heloc_account WHERE account_id = v_account;
            SET v_autopay = LEAST(v_min_due - v_paid, v_balance);
            IF v_autopay > 0 THEN
                INSERT INTO account_transaction (account_id, transaction_date, transaction_type, amount, channel, description)
                VALUES (v_account, p_month + INTERVAL 24 DAY, 'PAYMENT', v_autopay, 'AUTO_DEBIT', 'Automatic minimum payment');
                SET v_paid = v_paid + v_autopay;
            END IF;
        END IF;

        -- 2. Interest on the balance, then a late fee if the minimum wasn't paid
        SELECT current_balance INTO v_balance FROM heloc_account WHERE account_id = v_account;
        SET v_interest = ROUND(v_balance * (v_prime + v_margin) / 12, 2);
        IF v_interest > 0 THEN
            INSERT INTO account_transaction (account_id, transaction_date, transaction_type, amount, channel, description)
            VALUES (v_account, v_end, 'INTEREST', v_interest, 'SYSTEM', 'Monthly interest');
        END IF;

        SET v_missed = (v_min_due > 0 AND v_paid < v_min_due);
        IF v_missed THEN
            INSERT INTO account_transaction (account_id, transaction_date, transaction_type, amount, channel, description)
            VALUES (v_account, p_month + INTERVAL 25 DAY, 'FEE', 45.00, 'SYSTEM', 'Late payment fee');
        END IF;

        -- 3. Month totals from the transactions, and a check that they match the balance
        SELECT COALESCE(SUM(IF(transaction_type = 'DRAW', amount, 0)), 0),
               COALESCE(SUM(IF(transaction_type = 'PAYMENT', amount, 0)), 0),
               COALESCE(SUM(IF(transaction_type = 'INTEREST', amount, 0)), 0),
               COALESCE(SUM(IF(transaction_type = 'FEE', amount, 0)), 0)
          INTO v_draws, v_payments, v_interest_total, v_fees
          FROM account_transaction
         WHERE account_id = v_account
           AND transaction_date BETWEEN p_month AND v_end;

        SET v_closing = v_opening + v_draws - v_payments + v_interest_total + v_fees;
        SELECT current_balance INTO v_balance FROM heloc_account WHERE account_id = v_account;
        IF v_closing <> v_balance THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Account balance does not match its transactions';
        END IF;

        -- Days late: counted from the 25th of the first missed month
        IF NOT v_missed THEN
            SET v_dpd = 0;
        ELSEIF v_prev_dpd > 0 THEN
            SET v_dpd = v_prev_dpd + DAY(v_end);
        ELSE
            SET v_dpd = DATEDIFF(v_end, p_month + INTERVAL 24 DAY);
        END IF;

        SELECT bucket_code INTO v_bucket
          FROM delinquency_bucket
         WHERE v_dpd BETWEEN min_dpd AND COALESCE(max_dpd, 99999);

        IF v_missed THEN
            SET v_score = GREATEST(300, v_score - 40);
        END IF;

        INSERT INTO monthly_snapshot (
            account_id, snapshot_month, opening_balance, total_draws, total_payments,
            interest_charged, fees_charged, closing_balance, credit_limit, utilization_rate,
            prime_rate, interest_rate, minimum_payment_due, days_past_due, bucket_code,
            credit_score, property_value, cltv)
        VALUES (
            v_account, p_month, v_opening, v_draws, v_payments,
            v_interest_total, v_fees, v_closing, v_limit, IF(v_limit > 0, ROUND(v_closing / v_limit, 4), 0),
            v_prime, v_prime + v_margin, v_min_due, v_dpd, v_bucket,
            v_score, v_value, v_cltv);

        -- 4. Account status
        IF v_dpd >= 180 THEN
            IF v_limit > 0 THEN
                INSERT INTO credit_limit_change (account_id, change_date, old_limit, new_limit, reason)
                VALUES (v_account, v_end, v_limit, 0, 'ACCOUNT_CLOSURE');
            END IF;
            UPDATE heloc_account
               SET account_status = 'CHARGED_OFF', close_date = v_end
             WHERE account_id = v_account;
        ELSEIF v_dpd >= 60 AND v_status = 'ACTIVE' THEN
            UPDATE heloc_account SET account_status = 'FROZEN' WHERE account_id = v_account;
        ELSEIF v_dpd = 0 AND v_status = 'FROZEN' THEN
            UPDATE heloc_account SET account_status = 'ACTIVE' WHERE account_id = v_account;
        END IF;
    END LOOP;

    CLOSE account_cursor;

    -- 5. Risk segments for the new month
    CALL sp_assign_risk_segments(p_month);

    COMMIT;

    SELECT snapshot_month AS month_processed,
           COUNT(*) AS accounts,
           SUM(closing_balance) AS total_balance,
           SUM(days_past_due >= 30) AS accounts_30_plus_days_late,
           SUM(bucket_code = 'CHARGED_OFF') AS charged_off_this_month
      FROM monthly_snapshot
     WHERE snapshot_month = p_month
     GROUP BY snapshot_month;
END$$

DELIMITER ;
