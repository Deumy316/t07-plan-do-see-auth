import re
from datetime import datetime, timezone
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from flask import g, render_template, request, redirect, url_for

SEOUL = ZoneInfo('Asia/Seoul')


def parse_local(value):
    if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(?::[0-9]{2})?', value):
        raise ValueError('invalid format')
    local = datetime.fromisoformat(value)
    # Historical daylight-saving gaps/overlaps cannot identify a unique instant.
    candidates = set()
    for fold in (0, 1):
        utc = local.replace(tzinfo=SEOUL, fold=fold).astimezone(timezone.utc)
        if utc.astimezone(SEOUL).replace(tzinfo=None) == local:
            candidates.add(utc)
    if len(candidates) != 1:
        raise ValueError('ambiguous or nonexistent local time')
    return candidates.pop()


def utc_text(value):
    return value.isoformat(timespec='microseconds').replace('+00:00', 'Z')


def server_now():
    return datetime.now(timezone.utc)


def validate_execution(values):
    errors = []
    times = {}
    for field, label in (('started_at', '시작'), ('ended_at', '종료')):
        try:
            times[field] = parse_local(values[field])
        except (ValueError, OverflowError):
            errors.append(f'{label} 시각을 유효하고 명확한 한국 날짜·시간으로 입력해 주세요.')
    start, end = times.get('started_at'), times.get('ended_at')
    if start and end and end < start:
        errors.append('종료 시각은 시작 시각보다 빠를 수 없습니다.')
    if end and end > server_now():
        errors.append('종료 시각은 서버 현재 시각보다 미래일 수 없습니다.')
    if not values['content']:
        errors.append('수행 내용을 입력해 주세요.')
    return start, end, errors


def duration_minutes(start, end):
    seconds = int((end - start).total_seconds())
    return ((seconds * 100 + 30) // 60) / 100


def register_executions(app, get_db):
    @app.context_processor
    def execution_context():
        return {'execution_menu': request.endpoint in ('execution_list', 'execution_edit')}

    @app.route('/executions', methods=['GET', 'POST'])
    def execution_list():
        db = get_db()
        plans = db.execute('SELECT id, title FROM plans WHERE owner_id = ? ORDER BY created_at, id', (g.user['id'],)).fetchall()
        source = request.form if request.method == 'POST' else request.args
        plan_id = source.get('plan_id', plans[0]['id'] if plans else '')
        plan = next((p for p in plans if p['id'] == plan_id), None)
        tasks = db.execute('SELECT id, title, status FROM tasks WHERE plan_id = ? AND deleted_at IS NULL ORDER BY created_at, id', (plan_id,)).fetchall()
        values = {key: source.get(key, '').strip() for key in ('task_id', 'started_at', 'ended_at', 'content', 'blocked_reason')}
        request_id = request.form.get('request_id', '') if request.method == 'POST' else str(uuid4())
        errors, status = [], 200
        if plan_id and plan is None:
            errors.append('존재하는 계획을 선택해 주세요.')
            status = 400
        if request.method == 'POST':
            if not plan:
                errors.append('기록을 연결할 계획이 필요합니다.')
            try:
                if str(UUID(request_id)) != request_id:
                    raise ValueError()
            except (ValueError, AttributeError):
                errors.append('요청 식별자가 올바르지 않습니다. 실행 기록 메뉴를 다시 열어 주세요.')
            start, end, validation_errors = validate_execution(values)
            errors.extend(validation_errors)
            if not errors:
                minutes = duration_minutes(start, end)
                payload = (values['task_id'], utc_text(start), utc_text(end), values['content'], values['blocked_reason'])
                with db:
                    db.execute('BEGIN IMMEDIATE')
                    task = db.execute('SELECT plan_id, deleted_at FROM tasks WHERE id = ?', (values['task_id'],)).fetchone()
                    prior = db.execute('SELECT * FROM execution_records WHERE request_id = ?', (request_id,)).fetchone()
                    if not task or task['plan_id'] != plan_id:
                        errors.append('선택한 계획에 속한 할 일을 선택해 주세요.')
                        status = 400
                    elif prior:
                        original = tuple(prior[key] for key in ('task_id', 'started_at', 'ended_at', 'content', 'blocked_reason'))
                        if original != payload:
                            errors.append('이미 사용한 요청 식별자입니다. 새 기록은 실행 기록 메뉴를 다시 열어 작성해 주세요.')
                            status = 409
                        else:
                            return redirect(url_for('execution_list', plan_id=plan_id), code=303)
                    elif task['deleted_at']:
                        errors.append('삭제된 할 일에는 기록을 추가할 수 없습니다.')
                        status = 400
                    else:
                        db.execute('''INSERT INTO execution_records(id, task_id, request_id, started_at, ended_at,
                            actual_minutes, content, blocked_reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                            (str(uuid4()), payload[0], request_id, payload[1], payload[2], minutes,
                             payload[3], payload[4], utc_text(datetime.now(timezone.utc))))
                if not errors:
                    return redirect(url_for('execution_list', plan_id=plan_id), code=303)
            if errors and status == 200:
                status = 400
        records = db.execute('''SELECT e.*, t.title AS task_title FROM execution_records e
            JOIN tasks t ON t.id = e.task_id WHERE t.plan_id = ? AND t.deleted_at IS NULL
            ORDER BY e.started_at DESC, e.id ASC''', (plan_id,)).fetchall()
        return render_template('executions.html', plans=plans, plan=plan, tasks=tasks, values=values,
                               request_id=request_id, records=records, errors=errors), status

    @app.route('/executions/<record_id>/edit', methods=['GET', 'POST'])
    def execution_edit(record_id):
        db = get_db()
        record = db.execute('''SELECT e.*, t.plan_id FROM execution_records e
            JOIN tasks t ON t.id = e.task_id WHERE e.id = ?''', (record_id,)).fetchone()
        if record is None:
            return render_template('error.html', message='실행 기록을 찾을 수 없습니다.'), 404
        tasks = db.execute('''SELECT t.id, t.title, t.status, p.title AS plan_title FROM tasks t
            JOIN plans p ON p.id = t.plan_id WHERE t.deleted_at IS NULL AND p.owner_id = ? ORDER BY p.created_at, p.id, t.created_at, t.id''', (g.user['id'],)).fetchall()
        values = dict(record)
        for field in ('started_at', 'ended_at'):
            values[field] = datetime.fromisoformat(record[field].replace('Z', '+00:00')).astimezone(SEOUL).isoformat(timespec='seconds')[:19]
        errors = []
        if request.method == 'POST':
            values = {key: request.form.get(key, '').strip() for key in
                      ('task_id', 'started_at', 'ended_at', 'content', 'blocked_reason')}
            start, end, errors = validate_execution(values)
            if not errors:
                with db:
                    db.execute('BEGIN IMMEDIATE')
                    target = db.execute('SELECT plan_id FROM tasks WHERE id = ? AND deleted_at IS NULL', (values['task_id'],)).fetchone()
                    if target is None:
                        errors.append('삭제되지 않은 할 일을 선택해 주세요.')
                    else:
                        db.execute('''UPDATE execution_records SET task_id = ?, started_at = ?, ended_at = ?,
                            actual_minutes = ?, content = ?, blocked_reason = ? WHERE id = ?''',
                            (values['task_id'], utc_text(start), utc_text(end), duration_minutes(start, end),
                             values['content'], values['blocked_reason'], record_id))
                if not errors:
                    return redirect(url_for('execution_list', plan_id=target['plan_id']), code=303)
        return render_template('execution_edit.html', record=record, tasks=tasks, values=values, errors=errors), 400 if errors else 200
