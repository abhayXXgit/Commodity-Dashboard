-- ============================================================================
-- TransformerProcure Intelligence — reference (master) data
-- ============================================================================
-- Run AFTER schema.sql. Contains ONLY master/reference data and alert rules —
-- no prices. Prices come from a licensed feed, an official producer circular,
-- or the clearly-labelled DEMO generator (`python backend/main.py --seed`).
--
-- Nothing in this file is market data.
-- ============================================================================

BEGIN;


-- ---- commodities ----
INSERT INTO commodity_master (code, name, category, base_unit, base_currency, benchmark_source)
VALUES ('CU', 'Copper', 'BASE_METAL', 'MT', 'INR', 'LME Cash / BME India')
ON CONFLICT (code) DO NOTHING;
INSERT INTO commodity_master (code, name, category, base_unit, base_currency, benchmark_source)
VALUES ('AL', 'Aluminium', 'BASE_METAL', 'MT', 'INR', 'NALCO / BALCO / Hindalco circular')
ON CONFLICT (code) DO NOTHING;
INSERT INTO commodity_master (code, name, category, base_unit, base_currency, benchmark_source)
VALUES ('CRGO', 'CRGO Electrical Steel', 'CORE', 'MT', 'INR', 'Import parity / mill offer')
ON CONFLICT (code) DO NOTHING;
INSERT INTO commodity_master (code, name, category, base_unit, base_currency, benchmark_source)
VALUES ('STEEL', 'Mild Steel (Tank)', 'STRUCTURAL', 'MT', 'INR', 'Domestic HR plate index')
ON CONFLICT (code) DO NOTHING;
INSERT INTO commodity_master (code, name, category, base_unit, base_currency, benchmark_source)
VALUES ('OIL', 'Transformer Oil', 'FLUID', 'MT', 'INR', 'Domestic refiner offer')
ON CONFLICT (code) DO NOTHING;

-- ---- providers ----
INSERT INTO provider_master (code, name, provider_type, country, default_currency, default_unit, default_basis, ingestion_mode)
VALUES ('LME', 'London Metal Exchange', 'EXCHANGE', 'United Kingdom', 'USD', 'MT', 'CASH', 'API')
ON CONFLICT (code) DO NOTHING;
INSERT INTO provider_master (code, name, provider_type, country, default_currency, default_unit, default_basis, ingestion_mode)
VALUES ('BME', 'BME / Indian Metal Reference', 'EXCHANGE', 'India', 'INR', 'MT', 'EX_PLANT', 'API')
ON CONFLICT (code) DO NOTHING;
INSERT INTO provider_master (code, name, provider_type, country, default_currency, default_unit, default_basis, ingestion_mode)
VALUES ('NALCO', 'National Aluminium Company', 'PRODUCER', 'India', 'INR', 'MT', 'EX_PLANT', 'CIRCULAR_IMPORT')
ON CONFLICT (code) DO NOTHING;
INSERT INTO provider_master (code, name, provider_type, country, default_currency, default_unit, default_basis, ingestion_mode)
VALUES ('BALCO', 'BALCO / Vedanta', 'PRODUCER', 'India', 'INR', 'MT', 'EX_PLANT', 'CIRCULAR_IMPORT')
ON CONFLICT (code) DO NOTHING;
INSERT INTO provider_master (code, name, provider_type, country, default_currency, default_unit, default_basis, ingestion_mode)
VALUES ('HINDALCO', 'Hindalco / Aditya Birla', 'PRODUCER', 'India', 'INR', 'MT', 'EX_PLANT', 'CIRCULAR_IMPORT')
ON CONFLICT (code) DO NOTHING;
INSERT INTO provider_master (code, name, provider_type, country, default_currency, default_unit, default_basis, ingestion_mode)
VALUES ('MARKET', 'Domestic Market Reference', 'EXCHANGE', 'India', 'INR', 'MT', 'EX_PLANT', 'MANUAL')
ON CONFLICT (code) DO NOTHING;
INSERT INTO provider_master (code, name, provider_type, country, default_currency, default_unit, default_basis, ingestion_mode)
VALUES ('INTERNAL', 'Internal Purchase (ERP)', 'INTERNAL', 'India', 'INR', 'MT', 'DELIVERED', 'MANUAL')
ON CONFLICT (code) DO NOTHING;

-- ---- products ----
INSERT INTO product_master (commodity_id, code, name, grade, specification, default_unit)
SELECT id, 'CU_CATHODE', 'Copper Cathode', 'Grade A / ETP', 'LME Grade A cathode, 99.99% Cu', 'MT' FROM commodity_master WHERE code = 'CU'
ON CONFLICT (code) DO NOTHING;
INSERT INTO product_master (commodity_id, code, name, grade, specification, default_unit)
SELECT id, 'CU_WIREROD', 'Copper Wire Rod', 'ETP 8mm', '8 mm ETP copper wire rod', 'MT' FROM commodity_master WHERE code = 'CU'
ON CONFLICT (code) DO NOTHING;
INSERT INTO product_master (commodity_id, code, name, grade, specification, default_unit)
SELECT id, 'AL_INGOT', 'Aluminium Ingot', 'P1020A', 'Primary aluminium ingot 99.70%', 'MT' FROM commodity_master WHERE code = 'AL'
ON CONFLICT (code) DO NOTHING;
INSERT INTO product_master (commodity_id, code, name, grade, specification, default_unit)
SELECT id, 'AL_WIREROD', 'Aluminium Wire Rod', 'EC Grade', '9.5 mm EC grade aluminium wire rod', 'MT' FROM commodity_master WHERE code = 'AL'
ON CONFLICT (code) DO NOTHING;
INSERT INTO product_master (commodity_id, code, name, grade, specification, default_unit)
SELECT id, 'AL_BILLET', 'Aluminium Billet', '6063', 'Extrusion billet 6063', 'MT' FROM commodity_master WHERE code = 'AL'
ON CONFLICT (code) DO NOTHING;
INSERT INTO product_master (commodity_id, code, name, grade, specification, default_unit)
SELECT id, 'CRGO_M4', 'CRGO Lamination', 'M4 / 0.27mm', 'Grain oriented electrical steel', 'MT' FROM commodity_master WHERE code = 'CRGO'
ON CONFLICT (code) DO NOTHING;
INSERT INTO product_master (commodity_id, code, name, grade, specification, default_unit)
SELECT id, 'STEEL_MS', 'MS Plate', 'IS 2062 E250', 'Hot rolled mild steel plate', 'MT' FROM commodity_master WHERE code = 'STEEL'
ON CONFLICT (code) DO NOTHING;
INSERT INTO product_master (commodity_id, code, name, grade, specification, default_unit)
SELECT id, 'OIL_TRF', 'Transformer Oil', 'IS 335 / IEC 60296', 'Inhibited mineral insulating oil', 'MT' FROM commodity_master WHERE code = 'OIL'
ON CONFLICT (code) DO NOTHING;

-- ---- alert rules (thresholds are editable without a code change) ----
INSERT INTO alert_rules (alert_code, description, rule_type, commodity_code, threshold, direction, severity, is_enabled)
VALUES ('CU_DAY_UP', 'Copper rises more than 2% in one day', 'DAY_MOVE', 'CU', 2.0, 'UP', 'WARNING', TRUE)
ON CONFLICT (alert_code) DO NOTHING;
INSERT INTO alert_rules (alert_code, description, rule_type, commodity_code, threshold, direction, severity, is_enabled)
VALUES ('CU_DAY_DOWN', 'Copper falls more than 2% in one day', 'DAY_MOVE', 'CU', 2.0, 'DOWN', 'INFO', TRUE)
ON CONFLICT (alert_code) DO NOTHING;
INSERT INTO alert_rules (alert_code, description, rule_type, commodity_code, threshold, direction, severity, is_enabled)
VALUES ('AL_DAY_UP', 'Aluminium rises more than 2% in one day', 'DAY_MOVE', 'AL', 2.0, 'UP', 'WARNING', TRUE)
ON CONFLICT (alert_code) DO NOTHING;
INSERT INTO alert_rules (alert_code, description, rule_type, commodity_code, threshold, direction, severity, is_enabled)
VALUES ('AL_DAY_DOWN', 'Aluminium falls more than 2% in one day', 'DAY_MOVE', 'AL', 2.0, 'DOWN', 'INFO', TRUE)
ON CONFLICT (alert_code) DO NOTHING;
INSERT INTO alert_rules (alert_code, description, rule_type, commodity_code, threshold, direction, severity, is_enabled)
VALUES ('PCTL_HIGH', 'Price reaches the 90th historical percentile', 'PERCENTILE', NULL, 90.0, 'UP', 'WARNING', TRUE)
ON CONFLICT (alert_code) DO NOTHING;
INSERT INTO alert_rules (alert_code, description, rule_type, commodity_code, threshold, direction, severity, is_enabled)
VALUES ('PCTL_LOW', 'Price reaches the 10th historical percentile', 'PERCENTILE', NULL, 10.0, 'DOWN', 'INFO', TRUE)
ON CONFLICT (alert_code) DO NOTHING;
INSERT INTO alert_rules (alert_code, description, rule_type, commodity_code, threshold, direction, severity, is_enabled)
VALUES ('FC_UP', 'Forecast increase greater than 5%', 'FORECAST_MOVE', NULL, 5.0, 'UP', 'CRITICAL', TRUE)
ON CONFLICT (alert_code) DO NOTHING;
INSERT INTO alert_rules (alert_code, description, rule_type, commodity_code, threshold, direction, severity, is_enabled)
VALUES ('FC_DOWN', 'Forecast decrease greater than 5%', 'FORECAST_MOVE', NULL, 5.0, 'DOWN', 'INFO', TRUE)
ON CONFLICT (alert_code) DO NOTHING;
INSERT INTO alert_rules (alert_code, description, rule_type, commodity_code, threshold, direction, severity, is_enabled)
VALUES ('SUP_SPREAD', 'Supplier price differs >1.5% from benchmark', 'SUPPLIER_SPREAD', NULL, 1.5, 'BOTH', 'WARNING', TRUE)
ON CONFLICT (alert_code) DO NOTHING;
INSERT INTO alert_rules (alert_code, description, rule_type, commodity_code, threshold, direction, severity, is_enabled)
VALUES ('STALE_LIVE', 'Live data unavailable or stale', 'STALE', NULL, 26.0, 'BOTH', 'CRITICAL', TRUE)
ON CONFLICT (alert_code) DO NOTHING;
INSERT INTO alert_rules (alert_code, description, rule_type, commodity_code, threshold, direction, severity, is_enabled)
VALUES ('QUOTE_EXP', 'Supplier quotation expired or expiring', 'QUOTE_EXPIRY', NULL, 7.0, 'BOTH', 'WARNING', TRUE)
ON CONFLICT (alert_code) DO NOTHING;

-- ---- procurement policy settings ----
INSERT INTO user_settings (setting_key, setting_value, value_type, description)
VALUES ('data_mode', 'DEMO', 'string', 'DEMO or LIVE')
ON CONFLICT (setting_key) DO NOTHING;
INSERT INTO user_settings (setting_key, setting_value, value_type, description)
VALUES ('base_currency', 'INR', 'string', 'Reporting currency')
ON CONFLICT (setting_key) DO NOTHING;
INSERT INTO user_settings (setting_key, setting_value, value_type, description)
VALUES ('base_unit', 'MT', 'string', 'Reporting unit')
ON CONFLICT (setting_key) DO NOTHING;
INSERT INTO user_settings (setting_key, setting_value, value_type, description)
VALUES ('default_tax_pct', '18', 'float', 'GST used in landed cost')
ON CONFLICT (setting_key) DO NOTHING;
INSERT INTO user_settings (setting_key, setting_value, value_type, description)
VALUES ('target_cover_pct', '70', 'float', 'Policy coverage target before delivery')
ON CONFLICT (setting_key) DO NOTHING;
INSERT INTO user_settings (setting_key, setting_value, value_type, description)
VALUES ('budget_price_CU', '890000', 'float', 'Approved budget rate, copper INR/MT')
ON CONFLICT (setting_key) DO NOTHING;
INSERT INTO user_settings (setting_key, setting_value, value_type, description)
VALUES ('budget_price_AL', '292000', 'float', 'Approved budget rate, aluminium INR/MT')
ON CONFLICT (setting_key) DO NOTHING;
INSERT INTO user_settings (setting_key, setting_value, value_type, description)
VALUES ('risk_appetite', 'MEDIUM', 'string', 'LOW / MEDIUM / HIGH')
ON CONFLICT (setting_key) DO NOTHING;

COMMIT;
