-- =============================================================
-- HELOC Portfolio Analytics
-- 02_reference_data.sql: fills the lookup tables
-- (prime rate history is added in Step 4)
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
