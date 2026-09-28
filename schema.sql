CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY NOT NULL,
    owner_id TEXT REFERENCES users(id),
    title TEXT NOT NULL CHECK(length(trim(title)) > 0),
    content TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL CHECK(end_date >= start_date),
    priority TEXT NOT NULL CHECK(priority IN ('high', 'medium', 'low')),
    success_criteria TEXT NOT NULL,
    estimated_minutes INTEGER NOT NULL CHECK(typeof(estimated_minutes) = 'integer' AND estimated_minutes BETWEEN 0 AND 9007199254740991),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1)
);
CREATE TABLE IF NOT EXISTS plan_versions (
    plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE RESTRICT,
    version INTEGER NOT NULL CHECK(version >= 1),
    title TEXT NOT NULL CHECK(length(trim(title)) > 0),
    content TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL CHECK(end_date >= start_date),
    priority TEXT NOT NULL CHECK(priority IN ('high', 'medium', 'low')),
    success_criteria TEXT NOT NULL,
    estimated_minutes INTEGER NOT NULL CHECK(typeof(estimated_minutes) = 'integer' AND estimated_minutes BETWEEN 0 AND 9007199254740991),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (plan_id, version)
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY NOT NULL,
    plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE RESTRICT,
    title TEXT NOT NULL CHECK(length(trim(title)) > 0),
    content TEXT NOT NULL,
    due_date TEXT,
    priority TEXT NOT NULL CHECK(priority IN ('high', 'medium', 'low')),
    estimated_minutes INTEGER NOT NULL CHECK(typeof(estimated_minutes) = 'integer' AND estimated_minutes BETWEEN 0 AND 9007199254740991),
    status TEXT NOT NULL CHECK(status IN ('active', 'completed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    deleted_at TEXT,
    version INTEGER NOT NULL CHECK(version >= 1),
    CHECK((status = 'active' AND completed_at IS NULL) OR (status = 'completed' AND completed_at IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS tasks_plan_due ON tasks(plan_id, deleted_at, due_date, id);
CREATE TABLE IF NOT EXISTS task_tags (
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    tag TEXT NOT NULL CHECK(length(trim(tag)) > 0),
    PRIMARY KEY(task_id, tag)
);
CREATE TABLE IF NOT EXISTS task_state_requests (
    request_id TEXT PRIMARY KEY NOT NULL,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    target_status TEXT NOT NULL CHECK(target_status IN ('active', 'completed')),
    expected_version INTEGER NOT NULL CHECK(expected_version >= 1),
    result_version INTEGER NOT NULL CHECK(result_version >= 1),
    changed INTEGER NOT NULL CHECK(changed IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_completion_events (
    id TEXT PRIMARY KEY NOT NULL,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    request_id TEXT NOT NULL UNIQUE REFERENCES task_state_requests(request_id) ON DELETE RESTRICT,
    task_version INTEGER NOT NULL CHECK(task_version >= 1),
    completed_at TEXT NOT NULL,
    UNIQUE(task_id, task_version)
);

CREATE TABLE IF NOT EXISTS execution_records (
    id TEXT PRIMARY KEY NOT NULL,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    request_id TEXT NOT NULL UNIQUE,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL CHECK(ended_at >= started_at),
    actual_minutes REAL NOT NULL CHECK(actual_minutes >= 0),
    content TEXT NOT NULL CHECK(length(trim(content)) > 0),
    blocked_reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS execution_records_task_start ON execution_records(task_id, started_at, id);

CREATE TABLE IF NOT EXISTS review_improvements (
    id TEXT PRIMARY KEY NOT NULL,
    plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE RESTRICT,
    content TEXT NOT NULL CHECK(length(trim(content)) > 0),
    created_at TEXT NOT NULL,
    UNIQUE(id, plan_id)
);
CREATE TABLE IF NOT EXISTS next_plan_links (
    next_plan_id TEXT PRIMARY KEY NOT NULL REFERENCES plans(id) ON DELETE RESTRICT,
    previous_plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE RESTRICT,
    improvement_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(improvement_id, previous_plan_id) REFERENCES review_improvements(id, plan_id) ON DELETE RESTRICT,
    CHECK(next_plan_id <> previous_plan_id)
);
CREATE INDEX IF NOT EXISTS next_plan_links_improvement ON next_plan_links(improvement_id);
