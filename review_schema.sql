CREATE TABLE IF NOT EXISTS review_rule_changes (
    id TEXT PRIMARY KEY NOT NULL,
    plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE RESTRICT,
    before_rule TEXT NOT NULL CHECK(length(trim(before_rule)) > 0),
    after_rule TEXT NOT NULL CHECK(length(trim(after_rule)) > 0),
    reason TEXT NOT NULL CHECK(length(trim(reason)) > 0),
    day1 TEXT NOT NULL,
    day2 TEXT NOT NULL CHECK(day2 > day1),
    created_at TEXT NOT NULL,
    next_work_started_at TEXT,
    confirmed_complete INTEGER NOT NULL CHECK(confirmed_complete = 1)
);
CREATE INDEX IF NOT EXISTS review_rules_plan ON review_rule_changes(plan_id, created_at, id);
CREATE TABLE IF NOT EXISTS review_rule_evidence (
    change_id TEXT NOT NULL REFERENCES review_rule_changes(id) ON DELETE RESTRICT,
    day TEXT NOT NULL,
    record_id TEXT NOT NULL REFERENCES execution_records(id) ON DELETE RESTRICT,
    task_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    record_created_at TEXT NOT NULL,
    PRIMARY KEY(change_id, day, record_id)
);
