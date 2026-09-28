import json
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from flask import Response, g
from review import build_review

# Explicit data-only allowlist. Never serialize application config or the contract file.
EXPORT_FIELDS = {
    'plans': ('id', 'owner_id', 'title', 'content', 'start_date', 'end_date', 'priority', 'success_criteria', 'estimated_minutes', 'created_at', 'updated_at', 'version'),
    'plan_versions': ('plan_id', 'version', 'title', 'content', 'start_date', 'end_date', 'priority', 'success_criteria', 'estimated_minutes', 'created_at', 'updated_at'),
    'tasks': ('id', 'plan_id', 'title', 'content', 'due_date', 'priority', 'estimated_minutes', 'status', 'created_at', 'updated_at', 'completed_at', 'deleted_at', 'version'),
    'task_tags': ('task_id', 'tag'),
    'task_state_requests': ('request_id', 'task_id', 'target_status', 'expected_version', 'result_version', 'changed', 'created_at'),
    'task_completion_events': ('id', 'task_id', 'request_id', 'task_version', 'completed_at'),
    'execution_records': ('id', 'task_id', 'request_id', 'started_at', 'ended_at', 'actual_minutes', 'content', 'blocked_reason', 'created_at'),
    'review_improvements': ('id', 'plan_id', 'content', 'created_at'),
    'next_plan_links': ('next_plan_id', 'previous_plan_id', 'improvement_id', 'created_at'),
    'review_rule_changes': ('id', 'plan_id', 'before_rule', 'after_rule', 'reason', 'day1', 'day2', 'created_at', 'next_work_started_at', 'confirmed_complete'),
    'review_rule_evidence': ('change_id', 'day', 'record_id', 'task_id', 'started_at', 'ended_at', 'record_created_at'),
}


def register_export(app, get_db):
    @app.get('/export.json')
    def export_all():
        db = get_db()
        data = {}
        with db:
            db.execute('BEGIN')
            for table, columns in EXPORT_FIELDS.items():
                # Identifiers are fixed constants, not request parameters.
                owned = 'SELECT id FROM plans WHERE owner_id = ?'
                if table == 'plans':
                    scope = 'id IN (' + owned + ')'
                elif table == 'next_plan_links':
                    scope = 'next_plan_id IN (' + owned + ') AND previous_plan_id IN (' + owned + ')'
                elif table in ('tasks', 'plan_versions', 'review_improvements', 'review_rule_changes'):
                    scope = 'plan_id IN (' + owned + ')'
                elif table == 'review_rule_evidence':
                    scope = 'change_id IN (SELECT id FROM review_rule_changes WHERE plan_id IN (' + owned + '))'
                else:
                    scope = 'task_id IN (SELECT id FROM tasks WHERE plan_id IN (' + owned + '))'
                args = (g.user['id'],) * (2 if table == 'next_plan_links' else 1)
                data[table] = [dict(row) for row in db.execute(
                    'SELECT ' + ', '.join(columns) + ' FROM ' + table + ' WHERE ' + scope + ' ORDER BY 1, 2', args)]
                if table == 'plans':
                    # First SELECT has established the snapshot; one time for all plans.
                    snapshot_time = datetime.now(timezone.utc)
        timestamp = snapshot_time.isoformat(timespec='microseconds').replace('+00:00', 'Z')
        today = snapshot_time.astimezone(ZoneInfo('Asia/Seoul')).date()
        tasks_by_plan = {plan['id']: [] for plan in data['plans']}
        records_by_plan = {plan['id']: [] for plan in data['plans']}
        active_task_plans = {}
        for task in data['tasks']:
            if task['deleted_at'] is None:
                tasks_by_plan[task['plan_id']].append(task)
                active_task_plans[task['id']] = task['plan_id']
        for record in data['execution_records']:
            plan_id = active_task_plans.get(record['task_id'])
            if plan_id is not None:
                records_by_plan[plan_id].append(record)
        reviews = []
        for plan in data['plans']:
            report = build_review(tasks_by_plan[plan['id']], records_by_plan[plan['id']], today)
            # Exact decimal strings prevent precision loss for summed minute values.
            metrics = {key: format(value, 'f') if isinstance(value, Decimal) else value
                       for key, value in report['metrics'].items()}
            reviews.append({'plan_id': plan['id'], 'calculated_at': timestamp,
                            'today_seoul': today.isoformat(), 'metrics': metrics})
        document = {
            'schema_version': '4',
            'export_format_version': '3',
            'exported_at': timestamp,
            'aggregation_as_of': timestamp,
            'timezone': {'stored_timestamps': 'UTC', 'display_and_date_boundary': 'Asia/Seoul'},
            'time_units': {'estimated_minutes': 'integer minutes', 'actual_minutes': 'minutes rounded half-up to 2 decimal places',
                           'review_actual_and_difference': 'exact decimal strings in minutes'},
            'review_rules': {
                'scope': '현재 로그인 계정이 소유한 모든 계획별로 삭제되지 않은 할 일을 집계합니다.',
                'planned': '계획한 할 일 수 (계획 자체의 개수가 아님)',
                'completed': 'Current status is completed, not historical completion event count.',
                'overdue': 'status=active and due_date < today_seoul; missing/today/future deadlines excluded.',
                'blocked': 'Distinct task IDs with at least one execution record whose blocked_reason.strip() is nonempty.',
                'estimated': 'Sum current task estimated_minutes once per task; not plan estimates.',
                'actual': 'Sum stored execution actual_minutes for target tasks with Decimal; no recomputation from timestamps.',
                'difference': 'actual - estimated; may be negative.',
                'empty': 'All metrics zero; actual/difference represented as decimal strings.',
                'deleted': 'Raw rows retained including deleted_at; deleted tasks and their execution records excluded from metrics.',
            },
            'data': data,
            'plan_reviews': reviews,
        }
        payload = json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')
        filename = snapshot_time.strftime('pds-export-%Y%m%dT%H%M%S%fZ.json')
        return Response(payload, content_type='application/json; charset=utf-8', headers={
            'Content-Disposition': f'attachment; filename="{filename}"',
        })
