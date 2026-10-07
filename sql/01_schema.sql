-- =============================================================
-- HELOC Portfolio Analytics
-- 01_schema.sql: creates every table, key, constraint and index
--
-- Safe to re-run: it drops and recreates all tables,
-- which also deletes any data in them.
-- =============================================================

SET FOREIGN_KEY_CHECKS = 0;

DROP TABLE IF EXISTS
    audit_log,
    monthly_snapshot,
    credit_limit_change,
    account_transaction,
    heloc_account,
    property,
    customer,
    prime_rate_history,
    delinquency_bucket,
    province;

SET FOREIGN_KEY_CHECKS = 1;


-- -------------------------------------------------------------
-- LOOKUP TABLES
-- -------------------------------------------------------------

CREATE TABLE province (
    province_code  CHAR(2)      NOT NULL,
    province_name  VARCHAR(50)  NOT NULL,
    PRIMARY KEY (province_code),
    CONSTRAINT uq_province_name UNIQUE (province_name)
) COMMENT = 'Canadian provinces and territories';

CREATE TABLE delinquency_bucket (
    bucket_code   VARCHAR(20)  NOT NULL,
    bucket_label  VARCHAR(40)  NOT NULL,
    min_dpd       SMALLINT     NOT NULL,
    max_dpd       SMALLINT     NULL,                 -- empty = no upper limit
    sort_order    TINYINT      NOT NULL,
    PRIMARY KEY (bucket_code),
    CONSTRAINT uq_bucket_sort UNIQUE (sort_order),
    CONSTRAINT chk_bucket_range CHECK (min_dpd >= 0 AND (max_dpd IS NULL OR max_dpd >= min_dpd))
) COMMENT = 'Groups accounts by how many days late they are';

CREATE TABLE prime_rate_history (
    effective_date  DATE          NOT NULL,
    prime_rate      DECIMAL(7,4)  NOT NULL,          -- 0.0495 = 4.95%
    PRIMARY KEY (effective_date),
    CONSTRAINT chk_prime_rate CHECK (prime_rate > 0 AND prime_rate < 0.25)
) COMMENT = 'Bank prime rate and the date each change took effect';


-- -------------------------------------------------------------
-- CORE TABLES
-- -------------------------------------------------------------

CREATE TABLE customer (
    customer_id        INT            NOT NULL AUTO_INCREMENT,
    first_name         VARCHAR(50)    NOT NULL,
    last_name          VARCHAR(50)    NOT NULL,
    date_of_birth      DATE           NOT NULL,
    email              VARCHAR(100)   NOT NULL,
    phone              VARCHAR(20)    NULL,
    province_code      CHAR(2)        NOT NULL,
    annual_income      DECIMAL(14,2)  NOT NULL,
    employment_status  VARCHAR(20)    NOT NULL,
    created_at         DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (customer_id),
    CONSTRAINT uq_customer_email UNIQUE (email),
    CONSTRAINT fk_customer_province
        FOREIGN KEY (province_code) REFERENCES province (province_code),
    CONSTRAINT chk_customer_email      CHECK (email LIKE '%_@_%._%'),
    CONSTRAINT chk_customer_dob        CHECK (date_of_birth BETWEEN '1920-01-01' AND '2008-12-31'),
    CONSTRAINT chk_customer_income     CHECK (annual_income >= 0),
    CONSTRAINT chk_customer_employment CHECK (employment_status IN ('EMPLOYED', 'SELF_EMPLOYED', 'RETIRED', 'OTHER'))
) COMMENT = 'Borrowers';

CREATE TABLE property (
    property_id       INT            NOT NULL AUTO_INCREMENT,
    customer_id       INT            NOT NULL,
    street_address    VARCHAR(100)   NOT NULL,
    city              VARCHAR(50)    NOT NULL,
    province_code     CHAR(2)        NOT NULL,
    postal_code       CHAR(7)        NOT NULL,       -- format: A1A 1A1
    property_type     VARCHAR(20)    NOT NULL,
    appraised_value   DECIMAL(14,2)  NOT NULL,
    appraisal_date    DATE           NOT NULL,
    mortgage_balance  DECIMAL(14,2)  NOT NULL DEFAULT 0,
    PRIMARY KEY (property_id),
    CONSTRAINT fk_property_customer
        FOREIGN KEY (customer_id) REFERENCES customer (customer_id),
    CONSTRAINT fk_property_province
        FOREIGN KEY (province_code) REFERENCES province (province_code),
    CONSTRAINT chk_property_postal   CHECK (postal_code REGEXP '^[A-Z][0-9][A-Z] [0-9][A-Z][0-9]$'),
    CONSTRAINT chk_property_type     CHECK (property_type IN ('DETACHED', 'SEMI_DETACHED', 'TOWNHOUSE', 'CONDO')),
    CONSTRAINT chk_property_value    CHECK (appraised_value > 0),
    CONSTRAINT chk_property_mortgage CHECK (mortgage_balance >= 0 AND mortgage_balance < appraised_value)
) COMMENT = 'Homes that secure HELOC accounts';

CREATE TABLE heloc_account (
    account_id                INT            NOT NULL AUTO_INCREMENT,
    account_number            VARCHAR(12)    NOT NULL,
    customer_id               INT            NOT NULL,
    property_id               INT            NOT NULL,
    open_date                 DATE           NOT NULL,
    original_credit_limit     DECIMAL(14,2)  NOT NULL,
    credit_limit              DECIMAL(14,2)  NOT NULL,
    rate_margin               DECIMAL(7,4)   NOT NULL,   -- added to prime: 0.0050 = prime + 0.50%
    current_balance           DECIMAL(14,2)  NOT NULL DEFAULT 0,
    origination_credit_score  SMALLINT       NOT NULL,
    account_status            VARCHAR(20)    NOT NULL DEFAULT 'ACTIVE',
    close_date                DATE           NULL,
    created_at                DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at                DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (account_id),
    CONSTRAINT uq_account_number   UNIQUE (account_number),
    CONSTRAINT uq_account_property UNIQUE (property_id),
    CONSTRAINT fk_account_customer
        FOREIGN KEY (customer_id) REFERENCES customer (customer_id),
    CONSTRAINT fk_account_property
        FOREIGN KEY (property_id) REFERENCES property (property_id),
    CONSTRAINT chk_account_number  CHECK (account_number REGEXP '^HL-[0-9]{6}$'),
    CONSTRAINT chk_account_limits  CHECK (original_credit_limit > 0 AND credit_limit >= 0),
    CONSTRAINT chk_account_margin  CHECK (rate_margin BETWEEN 0 AND 0.05),
    CONSTRAINT chk_account_balance CHECK (current_balance >= 0),
    CONSTRAINT chk_account_score   CHECK (origination_credit_score BETWEEN 300 AND 900),
    CONSTRAINT chk_account_status  CHECK (account_status IN ('ACTIVE', 'FROZEN', 'CLOSED', 'CHARGED_OFF')),
    CONSTRAINT chk_account_dates   CHECK (close_date IS NULL OR close_date >= open_date),
    CONSTRAINT chk_account_close   CHECK (
        (account_status IN ('CLOSED', 'CHARGED_OFF') AND close_date IS NOT NULL)
        OR (account_status IN ('ACTIVE', 'FROZEN') AND close_date IS NULL)
    ),
    INDEX idx_account_status (account_status),
    INDEX idx_account_open_date (open_date)
) COMMENT = 'One HELOC per property: limit, pricing, status and live balance';

CREATE TABLE account_transaction (
    transaction_id    BIGINT         NOT NULL AUTO_INCREMENT,
    account_id        INT            NOT NULL,
    transaction_date  DATE           NOT NULL,
    transaction_type  VARCHAR(10)    NOT NULL,
    amount            DECIMAL(14,2)  NOT NULL,
    channel           VARCHAR(12)    NOT NULL,
    description       VARCHAR(255)   NULL,
    created_at        DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (transaction_id),
    CONSTRAINT fk_txn_account
        FOREIGN KEY (account_id) REFERENCES heloc_account (account_id),
    CONSTRAINT chk_txn_type    CHECK (transaction_type IN ('DRAW', 'PAYMENT', 'INTEREST', 'FEE')),
    CONSTRAINT chk_txn_amount  CHECK (amount > 0),
    CONSTRAINT chk_txn_channel CHECK (channel IN ('ONLINE', 'BRANCH', 'AUTO_DEBIT', 'SYSTEM')),
    INDEX idx_txn_account_date (account_id, transaction_date),
    INDEX idx_txn_date_type (transaction_date, transaction_type)
) COMMENT = 'Every money movement: draws, payments, interest and fees';

CREATE TABLE credit_limit_change (
    limit_change_id  INT            NOT NULL AUTO_INCREMENT,
    account_id       INT            NOT NULL,
    change_date      DATE           NOT NULL,
    old_limit        DECIMAL(14,2)  NOT NULL,
    new_limit        DECIMAL(14,2)  NOT NULL,
    reason           VARCHAR(25)    NOT NULL,
    PRIMARY KEY (limit_change_id),
    CONSTRAINT fk_limit_account
        FOREIGN KEY (account_id) REFERENCES heloc_account (account_id),
    CONSTRAINT chk_limit_values CHECK (old_limit >= 0 AND new_limit >= 0 AND old_limit <> new_limit),
    CONSTRAINT chk_limit_reason CHECK (reason IN ('INCREASE_REQUEST', 'PROPERTY_REVALUATION', 'RISK_REDUCTION', 'ACCOUNT_CLOSURE')),
    INDEX idx_limit_account_date (account_id, change_date)
) COMMENT = 'History of credit limit increases and decreases';


-- -------------------------------------------------------------
-- ANALYTICS TABLE
-- -------------------------------------------------------------

CREATE TABLE monthly_snapshot (
    snapshot_id          BIGINT         NOT NULL AUTO_INCREMENT,
    account_id           INT            NOT NULL,
    snapshot_month       DATE           NOT NULL,    -- always the 1st of the month
    opening_balance      DECIMAL(14,2)  NOT NULL,
    total_draws          DECIMAL(14,2)  NOT NULL DEFAULT 0,
    total_payments       DECIMAL(14,2)  NOT NULL DEFAULT 0,
    interest_charged     DECIMAL(14,2)  NOT NULL DEFAULT 0,
    fees_charged         DECIMAL(14,2)  NOT NULL DEFAULT 0,
    closing_balance      DECIMAL(14,2)  NOT NULL,
    credit_limit         DECIMAL(14,2)  NOT NULL,
    utilization_rate     DECIMAL(7,4)   NOT NULL,
    prime_rate           DECIMAL(7,4)   NOT NULL,
    interest_rate        DECIMAL(7,4)   NOT NULL,
    minimum_payment_due  DECIMAL(14,2)  NOT NULL DEFAULT 0,
    days_past_due        SMALLINT       NOT NULL DEFAULT 0,
    bucket_code          VARCHAR(20)    NOT NULL,
    credit_score         SMALLINT       NOT NULL,
    property_value       DECIMAL(14,2)  NOT NULL,
    cltv                 DECIMAL(7,4)   NOT NULL,
    risk_segment         VARCHAR(10)    NULL,        -- filled in later by a stored procedure
    PRIMARY KEY (snapshot_id),
    CONSTRAINT uq_snapshot_account_month UNIQUE (account_id, snapshot_month),
    CONSTRAINT fk_snapshot_account
        FOREIGN KEY (account_id) REFERENCES heloc_account (account_id),
    CONSTRAINT fk_snapshot_bucket
        FOREIGN KEY (bucket_code) REFERENCES delinquency_bucket (bucket_code),
    CONSTRAINT chk_snapshot_first_of_month CHECK (DAY(snapshot_month) = 1),
    CONSTRAINT chk_snapshot_amounts CHECK (
        opening_balance >= 0 AND closing_balance >= 0
        AND total_draws >= 0 AND total_payments >= 0
        AND interest_charged >= 0 AND fees_charged >= 0
        AND credit_limit >= 0 AND minimum_payment_due >= 0
    ),
    CONSTRAINT chk_snapshot_balance CHECK (
        closing_balance = opening_balance + total_draws - total_payments + interest_charged + fees_charged
    ),
    CONSTRAINT chk_snapshot_rates CHECK (
        utilization_rate >= 0 AND prime_rate > 0 AND interest_rate >= prime_rate AND cltv >= 0
    ),
    CONSTRAINT chk_snapshot_dpd   CHECK (days_past_due >= 0),
    CONSTRAINT chk_snapshot_score CHECK (credit_score BETWEEN 300 AND 900),
    CONSTRAINT chk_snapshot_risk  CHECK (risk_segment IS NULL OR risk_segment IN ('LOW', 'MEDIUM', 'HIGH', 'VERY_HIGH')),
    INDEX idx_snapshot_month_bucket (snapshot_month, bucket_code)
) COMMENT = 'Month-end state of each account: one row per account per month';


-- -------------------------------------------------------------
-- CONTROL TABLE
-- -------------------------------------------------------------

CREATE TABLE audit_log (
    audit_id     BIGINT        NOT NULL AUTO_INCREMENT,
    table_name   VARCHAR(64)   NOT NULL,
    record_id    BIGINT        NOT NULL,
    action       VARCHAR(10)   NOT NULL,
    column_name  VARCHAR(64)   NULL,
    old_value    VARCHAR(255)  NULL,
    new_value    VARCHAR(255)  NULL,
    changed_by   VARCHAR(100)  NOT NULL,
    changed_at   DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (audit_id),
    CONSTRAINT chk_audit_action CHECK (action IN ('INSERT', 'UPDATE', 'DELETE')),
    INDEX idx_audit_record (table_name, record_id),
    INDEX idx_audit_changed_at (changed_at)
) COMMENT = 'Change history written by triggers';
