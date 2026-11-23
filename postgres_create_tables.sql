-- Financial DQ Engine - MANUAL PostgreSQL deployment
-- Execute as a DBA / role granted CREATE on an EXISTING schema.
-- No CREATE SCHEMA or DROP statements are included.
-- Replace dq_app below with your already-existing PG_SCHEMA.
-- Set PG_SCHEMA to the same name in .env.
-- Running in another schema will not satisfy app startup.
SET search_path TO dq_app, public;

CREATE TABLE IF NOT EXISTS demo_cfv_attribute_masters (
	prj_id VARCHAR(80) NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	is_cde VARCHAR(1) NOT NULL, 
	cfi_id VARCHAR(80), 
	PRIMARY KEY (prj_id)
);

CREATE TABLE IF NOT EXISTS demo_normalized_financial_facts (
	id SERIAL NOT NULL, 
	document_id VARCHAR(80) NOT NULL, 
	company VARCHAR(160) NOT NULL, 
	portfolio_code VARCHAR(50) NOT NULL, 
	prj_id VARCHAR(80) NOT NULL, 
	period VARCHAR(10) NOT NULL, 
	value NUMERIC(25, 8), 
	currency VARCHAR(10) NOT NULL, 
	unit VARCHAR(30) NOT NULL, 
	collection_type VARCHAR(20) NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS demo_portfolio_refrence (
	short_code VARCHAR(50) NOT NULL, 
	portfolio_name VARCHAR(100) NOT NULL, 
	sector_name VARCHAR(100), 
	sub_sector VARCHAR(100), 
	document_type_id VARCHAR(80), 
	is_active BOOLEAN NOT NULL, 
	PRIMARY KEY (short_code)
);

CREATE TABLE IF NOT EXISTS dq_notification_history (
	id SERIAL NOT NULL, 
	execution_id VARCHAR(36) NOT NULL, 
	recipient VARCHAR(255) NOT NULL, 
	status VARCHAR(30) NOT NULL, 
	reason TEXT, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS dq_rule_executions (
	id VARCHAR(36) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	scope JSON NOT NULL, 
	requested_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	completed_at TIMESTAMP WITH TIME ZONE, 
	error TEXT, 
	idempotency_key VARCHAR(120), 
	PRIMARY KEY (id), 
	UNIQUE (idempotency_key)
);

CREATE TABLE IF NOT EXISTS dq_rule_import_batches (
	id VARCHAR(36) NOT NULL, 
	filename VARCHAR(255) NOT NULL, 
	candidates JSON NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS dq_rule_schedules (
	id SERIAL NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	frequency VARCHAR(20) NOT NULL, 
	hour_utc INTEGER NOT NULL, 
	minute_utc INTEGER NOT NULL, 
	day_of_month INTEGER NOT NULL, 
	rule_ids JSON NOT NULL, 
	active BOOLEAN NOT NULL, 
	last_fire_key VARCHAR(30), 
	PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS dq_rules (
	id VARCHAR(80) NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	rule_type VARCHAR(50) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	severity VARCHAR(20) NOT NULL, 
	current_version INTEGER NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS dq_rule_cde_mapping (
	id SERIAL NOT NULL, 
	rule_id VARCHAR(80) NOT NULL, 
	prj_id VARCHAR(80) NOT NULL, 
	role VARCHAR(30) NOT NULL, 
	collection_type VARCHAR(20) NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (rule_id, prj_id, role, collection_type), 
	FOREIGN KEY(rule_id) REFERENCES dq_rules (id)
);

CREATE TABLE IF NOT EXISTS dq_rule_versions (
	id SERIAL NOT NULL, 
	rule_id VARCHAR(80) NOT NULL, 
	version INTEGER NOT NULL, 
	config JSON NOT NULL, 
	targets JSON NOT NULL, 
	portfolios JSON NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (rule_id, version), 
	FOREIGN KEY(rule_id) REFERENCES dq_rules (id)
);

CREATE TABLE IF NOT EXISTS dq_validation_results (
	id SERIAL NOT NULL, 
	execution_id VARCHAR(36) NOT NULL, 
	rule_id VARCHAR(80) NOT NULL, 
	rule_version INTEGER NOT NULL, 
	prj_id VARCHAR(80) NOT NULL, 
	outcome VARCHAR(25) NOT NULL, 
	reason TEXT NOT NULL, 
	company VARCHAR(160), 
	portfolio_code VARCHAR(50), 
	period VARCHAR(10), 
	document_id VARCHAR(80), 
	actual VARCHAR(100), 
	expected VARCHAR(100), 
	evidence JSON NOT NULL, 
	rule_snapshot JSON NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(execution_id) REFERENCES dq_rule_executions (id)
);

CREATE INDEX IF NOT EXISTS ix_demo_fact_scope ON demo_normalized_financial_facts (portfolio_code, company, prj_id, period);
CREATE INDEX IF NOT EXISTS ix_dq_result_exec ON dq_validation_results (execution_id);

-- Verify after execution:
SELECT table_name FROM information_schema.tables WHERE table_schema = 'dq_app' AND (table_name LIKE 'dq_%' OR table_name LIKE 'demo_%') ORDER BY table_name;
