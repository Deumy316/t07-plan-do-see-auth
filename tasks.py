import re
from datetime import date, datetime, timezone
from uuid import UUID, uuid4

from flask import g, redirect, render_template, request, url_for

SORTS = {'due': '마감일순', 'priority': '우선순위순', 'newest': '최신 생성순'}
ORDERS = {
    'due': 't.due_date IS NULL, t.due_date ASC, t.id ASC',
    'priority': "CASE t.priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, t.id ASC",
    'newest': 't.created_at DESC, t.id ASC',
}
STATUSES = {'active': '진행 중', 'completed': '완료'}


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def register_tasks(app, get_db):
    def fail(message, status=400):
        return render_template('task_error.html', message=message), status

    def find(task_id):
        return get_db().execute('SELECT * FROM tasks WHERE id = ? AND deleted_at IS NULL', (task_id,)).fetchone()

    def version_value():
        value = request.form.get('version', '')
        return int(value) if re.fullmatch(r'[0-9]{1,10}', value) and int(value) > 0 else None

    def values_for(task=None):
        values = {key: request.form.get(key, '').strip() for key in
                  ('plan_id', 'title', 'content', 'due_date', 'priority', 'estimated_minutes', 'tags')}
        errors = []
        if not get_db().execute('SELECT id FROM plans WHERE id = ?', (values['plan_id'],)).fetchone():
            errors.append('존재하는 계획을 선택해 주세요.')
        if task and values['plan_id'] != task['plan_id']:
            errors.append('연결된 계획은 변경할 수 없습니다.')
        if not values['title']:
            errors.append('제목을 입력해 주세요.')
        if values['priority'] not in ('high', 'medium', 'low'):
            errors.append('올바른 우선순위를 선택해 주세요.')
        raw = values['estimated_minutes']
        if not re.fullmatch(r'[0-9]{1,16}', raw) or int(raw) > 9007199254740991:
            errors.append('예상 시간은 0 이상 9007199254740991 이하의 정수(분)여야 합니다.')
        else:
            values['estimated_minutes'] = int(raw)
        if values['due_date']:
            try:
                if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', values['due_date']):
                    raise ValueError()
                date.fromisoformat(values['due_date'])
            except ValueError:
                errors.append('마감일은 유효한 YYYY-MM-DD 날짜여야 합니다.')
        expected_status = task['status'] if task else 'active'
        if request.form.get('status', expected_status) != expected_status:
            errors.append('상태는 목록의 완료 또는 완료 취소 버튼으로 변경해 주세요.')
        return values, errors

    def save_tags(db, task_id, raw):
        db.execute('DELETE FROM task_tags WHERE task_id = ?', (task_id,))
        tags = sorted({tag.strip() for tag in raw.split(',') if tag.strip()})
        db.executemany('INSERT INTO task_tags(task_id, tag) VALUES (?, ?)', [(task_id, tag) for tag in tags])

    @app.context_processor
    def task_context():
        return {'task_statuses': STATUSES, 'task_menu': request.endpoint in
                ('task_list', 'task_new', 'task_edit', 'task_state', 'task_delete')}

    @app.get('/tasks')
    def task_list():
        db = get_db()
        plans = db.execute('SELECT id, title FROM plans WHERE owner_id = ? ORDER BY created_at, id', (g.user['id'],)).fetchall()
        plan_id = request.args.get('plan_id', plans[0]['id'] if plans else '')
        plan = next((p for p in plans if p['id'] == plan_id), None)
        if plan_id and plan is None:
            return fail('계획을 찾을 수 없습니다.', 404)
        filters = {key: request.args.get(key, '').strip() for key in ('q', 'status', 'priority', 'tag')}
        sort = request.args.get('sort', 'due')
        if sort not in SORTS or filters['status'] not in ('', *STATUSES) or filters['priority'] not in ('', 'high', 'medium', 'low'):
            return fail('검색 조건 또는 정렬 기준이 올바르지 않습니다.')
        where, args = ['t.plan_id = ?', 't.deleted_at IS NULL'], [plan_id]
        if filters['q']:
            where.append('(instr(lower(t.title), lower(?)) > 0 OR instr(lower(t.content), lower(?)) > 0)')
            args.extend([filters['q'], filters['q']])
        for key in ('status', 'priority'):
            if filters[key]:
                where.append(f't.{key} = ?')  # fixed allowlist, never user-provided SQL
                args.append(filters[key])
        if filters['tag']:
            where.append('EXISTS (SELECT 1 FROM task_tags tt WHERE tt.task_id = t.id AND tt.tag = ?)')
            args.append(filters['tag'])
        tasks = [dict(row) for row in db.execute('SELECT t.* FROM tasks t WHERE ' + ' AND '.join(where) + ' ORDER BY ' + ORDERS[sort], args)]
        for task in tasks:
            task['tags'] = [r['tag'] for r in db.execute('SELECT tag FROM task_tags WHERE task_id = ? ORDER BY tag', (task['id'],))]
            task['request_id'] = str(uuid4())
        tags = [r['tag'] for r in db.execute('''SELECT DISTINCT tt.tag FROM task_tags tt JOIN tasks t ON t.id = tt.task_id
            WHERE t.plan_id = ? AND t.deleted_at IS NULL ORDER BY tt.tag''', (plan_id,))]
        return render_template('tasks.html', plans=plans, plan=plan, tasks=tasks, tags=tags, filters=filters, sort=sort, sorts=SORTS)

    @app.route('/tasks/new', methods=['GET', 'POST'])
    def task_new():
        db = get_db()
        plans = db.execute('SELECT id, title FROM plans WHERE owner_id = ? ORDER BY created_at, id', (g.user['id'],)).fetchall()
        values = dict(plan_id=request.args.get('plan_id', ''), title='', content='', due_date='', priority='medium', estimated_minutes=0, tags='')
        errors = []
        if request.method == 'POST':
            values, errors = values_for()
            if not errors:
                task_id, timestamp = str(uuid4()), now()
                with db:
                    db.execute('''INSERT INTO tasks(id, plan_id, title, content, due_date, priority, estimated_minutes,
                        status, created_at, updated_at, completed_at, deleted_at, version)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, NULL, NULL, 1)''',
                        (task_id, values['plan_id'], values['title'], values['content'], values['due_date'] or None,
                         values['priority'], values['estimated_minutes'], timestamp, timestamp))
                    save_tags(db, task_id, values['tags'])
                return redirect(url_for('task_list', plan_id=values['plan_id']), code=303)
        return render_template('task_form.html', task=None, plans=plans, values=values, errors=errors), 400 if errors else 200

    @app.route('/tasks/<task_id>/edit', methods=['GET', 'POST'])
    def task_edit(task_id):
        task = find(task_id)
        if task is None:
            return fail('할 일을 찾을 수 없습니다.', 404)
        db = get_db()
        plans = db.execute('SELECT id, title FROM plans WHERE id = ?', (task['plan_id'],)).fetchall()
        values = dict(task, tags=', '.join(r['tag'] for r in db.execute('SELECT tag FROM task_tags WHERE task_id = ? ORDER BY tag', (task_id,))))
        values['due_date'] = values['due_date'] or ''
        errors, code = [], 200
        if request.method == 'POST':
            values, errors = values_for(task)
            version = version_value()
            if version is None:
                errors.append('버전이 올바르지 않습니다. 목록에서 다시 열어 주세요.')
            if errors:
                code = 400
            else:
                with db:
                    result = db.execute('''UPDATE tasks SET title = ?, content = ?, due_date = ?, priority = ?,
                        estimated_minutes = ?, updated_at = ?, version = version + 1
                        WHERE id = ? AND version = ? AND deleted_at IS NULL''',
                        (values['title'], values['content'], values['due_date'] or None, values['priority'],
                         values['estimated_minutes'], now(), task_id, version))
                    if result.rowcount:
                        save_tags(db, task_id, values['tags'])
                    else:
                        errors.append('다른 요청으로 변경된 할 일입니다. 목록에서 최신 내용을 확인해 주세요.')
                        code = 409
                if not errors:
                    return redirect(url_for('task_list', plan_id=task['plan_id']), code=303)
        return render_template('task_form.html', task=task, plans=plans, values=values, errors=errors), code

    @app.post('/tasks/<task_id>/state')
    def task_state(task_id):
        target = request.form.get('target_status')
        version = version_value()
        request_id = request.form.get('request_id', '')
        try:
            if str(UUID(request_id)) != request_id:
                raise ValueError()
        except (ValueError, AttributeError):
            return fail('요청 식별자가 올바르지 않습니다. 목록에서 다시 요청해 주세요.')
        if target not in STATUSES or version is None:
            return fail('상태 또는 버전이 올바르지 않습니다.')
        db = get_db()
        with db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT * FROM task_state_requests WHERE request_id = ?', (request_id,)).fetchone()
            if prior:
                if (prior['task_id'], prior['target_status'], prior['expected_version']) != (task_id, target, version):
                    return fail('같은 요청 식별자를 다른 요청에 사용할 수 없습니다.', 409)
                owner = db.execute('SELECT plan_id FROM tasks WHERE id = ?', (task_id,)).fetchone()
                return redirect(url_for('task_list', plan_id=owner['plan_id']), code=303)
            task = find(task_id)
            if task is None:
                return fail('할 일을 찾을 수 없습니다.', 404)
            if task['version'] != version:
                return fail('오래된 요청입니다. 목록에서 최신 상태를 확인해 주세요.', 409)
            changed = task['status'] != target
            result_version = version + int(changed)
            timestamp = now()
            if changed:
                db.execute('''UPDATE tasks SET status = ?, completed_at = ?, updated_at = ?, version = ? WHERE id = ?''',
                           (target, timestamp if target == 'completed' else None, timestamp, result_version, task_id))
            db.execute('''INSERT INTO task_state_requests(request_id, task_id, target_status, expected_version,
                       result_version, changed, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)''',
                       (request_id, task_id, target, version, result_version, int(changed), timestamp))
            if changed and target == 'completed':
                db.execute('INSERT INTO task_completion_events(id, task_id, request_id, task_version, completed_at) VALUES (?, ?, ?, ?, ?)',
                           (str(uuid4()), task_id, request_id, result_version, timestamp))
        return redirect(url_for('task_list', plan_id=task['plan_id']), code=303)

    @app.route('/tasks/<task_id>/delete', methods=['GET', 'POST'])
    def task_delete(task_id):
        task = find(task_id)
        if task is None:
            return fail('할 일을 찾을 수 없습니다.', 404)
        if request.method == 'POST':
            if request.form.get('confirm') != 'yes' or version_value() is None:
                return fail('삭제 확인과 올바른 버전이 필요합니다.')
            db = get_db()
            with db:
                timestamp = now()
                result = db.execute('''UPDATE tasks SET deleted_at = ?, updated_at = ?, version = version + 1
                    WHERE id = ? AND version = ? AND deleted_at IS NULL''', (timestamp, timestamp, task_id, version_value()))
                if not result.rowcount:
                    return fail('변경된 할 일입니다. 목록에서 다시 확인해 주세요.', 409)
            return redirect(url_for('task_list', plan_id=task['plan_id']), code=303)
        return render_template('task_delete.html', task=task)
