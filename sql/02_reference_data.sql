-- =============================================================
-- HELOC Portfolio Analytics
-- 02_reference_data.sql: fills the lookup tables
-- =============================================================

INSERT INTO province (province_code, province_name) VALUES
    ('AB', 'Alberta'),
    ('BC', 'British Columbia'),
    ('MB', 'Manitoba'),
    ('NB', 'New Brunswick'),
    ('NL', 'Newfoundland and Labrador'),
    ('NS', 'Nova Scotia'),
    ('NT', 'Northwest Territories'),
    ('NU', 'Nunavut'),
    ('ON', 'Ontario'),
    ('PE', 'Prince Edward Island'),
    ('QC', 'Quebec'),
    ('SK', 'Saskatchewan'),
    ('YT', 'Yukon');

INSERT INTO delinquency_bucket (bucket_code, bucket_label, min_dpd, max_dpd, sort_order) VALUES
    ('CURRENT',     'Current',                  0,    0,  1),
    ('DPD_1_29',    '1-29 days late',           1,   29,  2),
    ('DPD_30_59',   '30-59 days late',         30,   59,  3),
    ('DPD_60_89',   '60-89 days late',         60,   89,  4),
    ('DPD_90_179',  'Seriously late (90-179)', 90,  179,  5),
    ('CHARGED_OFF', 'Written off (180+)',     180, NULL,  6);

-- Canadian big-bank prime rate. Each change takes effect the day after
-- the Bank of Canada announcement. Source: Bank of Canada decisions.
-- 0.0445 = 4.45%
INSERT INTO prime_rate_history (effective_date, prime_rate) VALUES
    ('2023-01-26', 0.0670),
    ('2023-06-08', 0.0695),
    ('2023-07-13', 0.0720),
    ('2024-06-06', 0.0695),
    ('2024-07-25', 0.0670),
    ('2024-09-05', 0.0645),
    ('2024-10-24', 0.0595),
    ('2024-12-12', 0.0545),
    ('2025-01-30', 0.0520),
    ('2025-03-13', 0.0495),
    ('2025-09-18', 0.0470),
    ('2025-10-30', 0.0445);
