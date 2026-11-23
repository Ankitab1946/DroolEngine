-- DBA-EXECUTED, ONE-TIME MIGRATION for existing dq_rule_versions.
-- REVIEW AND BACK UP FIRST. Replace dq_app with your existing PG_SCHEMA.
-- Does not create schemas or modify PRJ master tables.
BEGIN;
SET LOCAL search_path TO dq_app, public;
ALTER TABLE dq_rule_versions ADD COLUMN IF NOT EXISTS effective_from TIMESTAMPTZ;
ALTER TABLE dq_rule_versions ADD COLUMN IF NOT EXISTS effective_to TIMESTAMPTZ;
ALTER TABLE dq_rule_versions ADD COLUMN IF NOT EXISTS is_current BOOLEAN;
ALTER TABLE dq_rule_versions ADD COLUMN IF NOT EXISTS status_snapshot VARCHAR(20);
ALTER TABLE dq_rule_versions ADD COLUMN IF NOT EXISTS name_snapshot VARCHAR(200);
ALTER TABLE dq_rule_versions ADD COLUMN IF NOT EXISTS severity_snapshot VARCHAR(20);
ALTER TABLE dq_rule_versions ADD COLUMN IF NOT EXISTS rule_type_snapshot VARCHAR(50);
ALTER TABLE dq_rule_versions ADD COLUMN IF NOT EXISTS threshold_value NUMERIC(25,8);
ALTER TABLE dq_rule_versions ADD COLUMN IF NOT EXISTS threshold_type VARCHAR(20);
ALTER TABLE dq_rule_versions ADD COLUMN IF NOT EXISTS changed_by VARCHAR(120);
ALTER TABLE dq_rule_versions ADD COLUMN IF NOT EXISTS change_reason VARCHAR(255);
-- Backfill previous versions without changing their JSON/config/threshold source records.
WITH ordered AS (
    SELECT v.id, COALESCE(v.created_at, CURRENT_TIMESTAMP) AS start_at,
        LEAD(v.created_at) OVER (PARTITION BY v.rule_id ORDER BY v.version) AS next_at,
        ROW_NUMBER() OVER (PARTITION BY v.rule_id ORDER BY v.version DESC) AS reverse_rank
    FROM dq_rule_versions v
)
UPDATE dq_rule_versions v SET
    effective_from=COALESCE(v.effective_from,o.start_at),
    effective_to=CASE WHEN o.reverse_rank=1 THEN NULL ELSE COALESCE(v.effective_to,o.next_at) END,
    is_current=(o.reverse_rank=1),
    status_snapshot=COALESCE(v.status_snapshot,r.status),
    name_snapshot=COALESCE(v.name_snapshot,r.name),
    severity_snapshot=COALESCE(v.severity_snapshot,r.severity),
    rule_type_snapshot=COALESCE(v.rule_type_snapshot,r.rule_type),
    threshold_value=COALESCE(v.threshold_value,
       CASE WHEN COALESCE(v.config->>'threshold_value',v.config->>'threshold') ~ '^-?[0-9]+(\.[0-9]+)?$'
       THEN COALESCE(v.config->>'threshold_value',v.config->>'threshold')::NUMERIC(25,8) END),
    threshold_type=COALESCE(v.threshold_type,v.config->>'threshold_type',
       CASE WHEN r.rule_type='MOVEMENT' THEN 'PERCENTAGE'
            WHEN r.rule_type='DUPLICATE' THEN 'COUNT' ELSE 'AMOUNT' END),
    changed_by=COALESCE(v.changed_by,'legacy'),
    change_reason=COALESCE(v.change_reason,'Historical version imported before SCD2 migration')
FROM ordered o JOIN dq_rules r ON r.id=(SELECT vv.rule_id FROM dq_rule_versions vv WHERE vv.id=o.id)
WHERE v.id=o.id;
ALTER TABLE dq_rule_versions ALTER COLUMN effective_from SET NOT NULL;
ALTER TABLE dq_rule_versions ALTER COLUMN is_current SET NOT NULL;
ALTER TABLE dq_rule_versions ALTER COLUMN status_snapshot SET NOT NULL;
ALTER TABLE dq_rule_versions ALTER COLUMN name_snapshot SET NOT NULL;
ALTER TABLE dq_rule_versions ALTER COLUMN severity_snapshot SET NOT NULL;
ALTER TABLE dq_rule_versions ALTER COLUMN rule_type_snapshot SET NOT NULL;
ALTER TABLE dq_rule_versions ALTER COLUMN changed_by SET NOT NULL;
ALTER TABLE dq_rule_versions ALTER COLUMN change_reason SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_dq_rule_versions_current
    ON dq_rule_versions(rule_id) WHERE is_current = TRUE;
-- Existing SCD2 intervals are reconstructed from creation timestamps; historical status
-- snapshots cannot be reliably recovered if earlier statuses were never recorded.
COMMIT;
