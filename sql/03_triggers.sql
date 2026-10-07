-- 03_triggers.sql
-- Rules the database enforces automatically.
-- Triggers that move balances or write the audit log are skipped while
-- @bulk_load = 1, which the data generator sets when loading history.

DROP TRIGGER IF EXISTS trg_account_before_insert;
DROP TRIGGER IF EXISTS trg_account_after_insert;
DROP TRIGGER IF EXISTS trg_account_before_update;
DROP TRIGGER IF EXISTS trg_account_after_update;
DROP TRIGGER IF EXISTS trg_txn_before_insert;
DROP TRIGGER IF EXISTS trg_txn_after_insert;
DROP TRIGGER IF EXISTS trg_txn_before_update;
DROP TRIGGER IF EXISTS trg_txn_before_delete;
DROP TRIGGER IF EXISTS trg_limit_before_insert;
DROP TRIGGER IF EXISTS trg_limit_after_insert;

DELIMITER $$

-- New account: property must belong to the customer, limit must follow the 65% / 80% rules
CREATE TRIGGER trg_account_before_insert
BEFORE INSERT ON heloc_account
FOR EACH ROW
BEGIN
    DECLARE v_value DECIMAL(14,2);
    DECLARE v_mortgage DECIMAL(14,2);
    DECLARE v_owner INT;

    SELECT appraised_value, mortgage_balance, customer_id
      INTO v_value, v_mortgage, v_owner
      FROM property
     WHERE property_id = NEW.property_id;

    IF v_owner <> NEW.customer_id THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'This property belongs to a different customer';
    END IF;
    IF NEW.credit_limit > v_value * 0.65 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Credit limit is more than 65% of the home value';
    END IF;
    IF NEW.credit_limit + v_mortgage > v_value * 0.80 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Mortgage plus credit limit is more than 80% of the home value';
    END IF;
END$$

CREATE TRIGGER trg_account_after_insert
AFTER INSERT ON heloc_account
FOR EACH ROW
BEGIN
    IF COALESCE(@bulk_load, 0) = 0 THEN
        INSERT INTO audit_log (table_name, record_id, action, column_name, old_value, new_value, changed_by)
        VALUES ('heloc_account', NEW.account_id, 'INSERT', 'credit_limit', NULL, NEW.credit_limit, CURRENT_USER());
    END IF;
END$$

-- Account changes: no reopening closed accounts, limit increases must follow the 65% / 80% rules
CREATE TRIGGER trg_account_before_update
BEFORE UPDATE ON heloc_account
FOR EACH ROW
BEGIN
    DECLARE v_value DECIMAL(14,2);
    DECLARE v_mortgage DECIMAL(14,2);

    IF OLD.account_status IN ('CLOSED', 'CHARGED_OFF') AND NEW.account_status <> OLD.account_status THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Closed or charged-off accounts cannot be reopened';
    END IF;

    IF NEW.credit_limit > OLD.credit_limit THEN
        SELECT appraised_value, mortgage_balance
          INTO v_value, v_mortgage
          FROM property
         WHERE property_id = NEW.property_id;

        IF NEW.credit_limit > v_value * 0.65 THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Credit limit is more than 65% of the home value';
        END IF;
        IF NEW.credit_limit + v_mortgage > v_value * 0.80 THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Mortgage plus credit limit is more than 80% of the home value';
        END IF;
    END IF;
END$$

-- Record every change to balance, limit and status
CREATE TRIGGER trg_account_after_update
AFTER UPDATE ON heloc_account
FOR EACH ROW
BEGIN
    IF COALESCE(@bulk_load, 0) = 0 THEN
        IF NEW.current_balance <> OLD.current_balance THEN
            INSERT INTO audit_log (table_name, record_id, action, column_name, old_value, new_value, changed_by)
            VALUES ('heloc_account', NEW.account_id, 'UPDATE', 'current_balance',
                    OLD.current_balance, NEW.current_balance, CURRENT_USER());
        END IF;
        IF NEW.credit_limit <> OLD.credit_limit THEN
            INSERT INTO audit_log (table_name, record_id, action, column_name, old_value, new_value, changed_by)
            VALUES ('heloc_account', NEW.account_id, 'UPDATE', 'credit_limit',
                    OLD.credit_limit, NEW.credit_limit, CURRENT_USER());
        END IF;
        IF NEW.account_status <> OLD.account_status THEN
            INSERT INTO audit_log (table_name, record_id, action, column_name, old_value, new_value, changed_by)
            VALUES ('heloc_account', NEW.account_id, 'UPDATE', 'account_status',
                    OLD.account_status, NEW.account_status, CURRENT_USER());
        END IF;
    END IF;
END$$

-- Check a transaction before it's saved
CREATE TRIGGER trg_txn_before_insert
BEFORE INSERT ON account_transaction
FOR EACH ROW
BEGIN
    DECLARE v_status VARCHAR(20);
    DECLARE v_balance DECIMAL(14,2);
    DECLARE v_limit DECIMAL(14,2);
    DECLARE v_open DATE;

    IF COALESCE(@bulk_load, 0) = 0 THEN
        -- FOR UPDATE locks the account row so two draws at once can't both pass the limit check
        SELECT account_status, current_balance, credit_limit, open_date
          INTO v_status, v_balance, v_limit, v_open
          FROM heloc_account
         WHERE account_id = NEW.account_id
           FOR UPDATE;

        IF NEW.transaction_date < v_open THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Transaction date is before the account was opened';
        END IF;
        IF NEW.transaction_type = 'DRAW' AND v_status <> 'ACTIVE' THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Only active accounts can draw funds';
        END IF;
        IF NEW.transaction_type = 'DRAW' AND v_balance + NEW.amount > v_limit THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Draw would go over the credit limit';
        END IF;
        IF NEW.transaction_type = 'PAYMENT' AND NEW.amount > v_balance THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Payment is more than the balance owed';
        END IF;
    END IF;
END$$

-- Keep the account balance up to date
CREATE TRIGGER trg_txn_after_insert
AFTER INSERT ON account_transaction
FOR EACH ROW
BEGIN
    IF COALESCE(@bulk_load, 0) = 0 THEN
        UPDATE heloc_account
           SET current_balance = current_balance
                                 + IF(NEW.transaction_type = 'PAYMENT', -NEW.amount, NEW.amount)
         WHERE account_id = NEW.account_id;
    END IF;
END$$

-- Transactions are permanent: mistakes are fixed with a new transaction, never by editing
CREATE TRIGGER trg_txn_before_update
BEFORE UPDATE ON account_transaction
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Transactions cannot be changed. Add a new transaction instead';
END$$

CREATE TRIGGER trg_txn_before_delete
BEFORE DELETE ON account_transaction
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Transactions cannot be deleted. Add a new transaction instead';
END$$

-- Limit changes: fill in the old limit automatically, then update the account
CREATE TRIGGER trg_limit_before_insert
BEFORE INSERT ON credit_limit_change
FOR EACH ROW
BEGIN
    IF COALESCE(@bulk_load, 0) = 0 THEN
        SET NEW.old_limit = (SELECT credit_limit FROM heloc_account WHERE account_id = NEW.account_id);
    END IF;
END$$

CREATE TRIGGER trg_limit_after_insert
AFTER INSERT ON credit_limit_change
FOR EACH ROW
BEGIN
    IF COALESCE(@bulk_load, 0) = 0 THEN
        UPDATE heloc_account
           SET credit_limit = NEW.new_limit
         WHERE account_id = NEW.account_id;
    END IF;
END$$

DELIMITER ;
