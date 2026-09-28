import os
import re
import sqlite3
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from flask import Flask, abort, g, redirect, render_template, request, url_for

ROOT = Path(__file__).resolve().parent
FIELDS = ('title', 'content', 'start_date', 'end_date', 'priority', 'success_criteria', 'estimated_minutes')
PRIORITIES = {'high': '높음', 'medium': '보통', 'low': '낮음'}


def create_app(db_path=None):
    app = Flask(__name__)
    path = Path(db_path or os.environ.get('PDS_DB_PATH') or ROOT / 'instance/pds.sqlite3')
    if not path.is_absolute():
        path = ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    app.config.update(DATABASE=str(path), MAX_CONTENT_LENGTH=1024 * 1024,
                      SESSION_COOKIE_SECURE=os.environ.get('PDS_COOKIE_SECURE') == '1')
    with closing(sqlite3.connect(path)) as db:
        from auth import migrate
        migrate(db, path)
        db.executescript((ROOT / 'schema.sql').read_text(encoding='utf-8'))
        from review_experiment import migrate_review
        migrate_review(db, path)

    def get_db():
        if 'db' not in g:
            g.db = sqlite3.connect(app.config['DATABASE'], timeout=10)
            g.db.row_factory = sqlite3.Row
            g.db.execute('PRAGMA foreign_keys = ON')
        return g.db

    @app.teardown_appcontext
    def close_db(error=None):
        db = g.pop('db', None)
        if db is not None:
            db.close()

    from auth import register_auth
    register_auth(app, get_db)

    @app.template_filter('seoul')
    def seoul(value):
        return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(ZoneInfo('Asia/Seoul')).strftime('%Y-%m-%d %H:%M:%S')

    @app.context_processor
    def context():
        return {'priorities': PRIORITIES}

    @app.after_request
    def headers(response):
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Cache-Control'] = 'no-store'
        return response

    def find_plan(plan_id):
        row = get_db().execute('SELECT * FROM plans WHERE id = ? AND owner_id = ?', (plan_id, g.user['id'])).fetchone()
        if row is None:
            abort(404)
        return row

    def form_values():
        values = {key: request.form.get(key, '').strip() for key in FIELDS}
        errors = []
        if not values['title']:
            errors.append('제목을 입력해 주세요.')
        for key, label in [('start_date', '시작일'), ('end_date', '종료일')]:
            try:
                if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', values[key]):
                    raise ValueError()
                date.fromisoformat(values[key])
            except ValueError:
                errors.append(f'{label}을 유효한 YYYY-MM-DD 날짜로 입력해 주세요.')
        if not errors and values['end_date'] < values['start_date']:
            errors.append('종료일은 시작일보다 빠를 수 없습니다.')
        if values['priority'] not in PRIORITIES:
            errors.append('우선순위를 선택해 주세요.')
        raw = values['estimated_minutes']
        if not re.fullmatch(r'[0-9]{1,16}', raw) or int(raw) > 9007199254740991:
            errors.append('예상 시간은 0 이상 9007199254740991 이하의 정수(분)로 입력해 주세요.')
        else:
            values['estimated_minutes'] = int(raw)
        return values, errors

    def snapshot(db, plan_id):
        db.execute('''INSERT INTO plan_versions
            (plan_id, version, title, content, start_date, end_date, priority,
             success_criteria, estimated_minutes, created_at, updated_at)
            SELECT id, version, title, content, start_date, end_date, priority,
             success_criteria, estimated_minutes, created_at, updated_at
            FROM plans WHERE id = ?''', (plan_id,))

    @app.get('/')
    def index():
        plans = get_db().execute('SELECT * FROM plans WHERE owner_id = ? ORDER BY updated_at DESC, id', (g.user['id'],)).fetchall()
        return render_template('index.html', plans=plans)

    @app.route('/plans/new', methods=['GET', 'POST'])
    def new_plan():
        values = dict.fromkeys(FIELDS, '')
        values.update(priority='medium', estimated_minutes=0)
        source_id = request.form.get('source_improvement', request.args.get('source_improvement', ''))
        source_improvement = None
        if source_id:
            source_improvement = get_db().execute('''SELECT i.*, p.title AS plan_title
                FROM review_improvements i JOIN plans p ON p.id = i.plan_id WHERE i.id = ? AND p.owner_id = ?''', (source_id, g.user['id'])).fetchone()
            if source_improvement is None:
                return render_template('error.html', message='연결할 개선점을 찾을 수 없습니다.'), 404
            values['content'] = source_improvement['content']
        errors = []
        if request.method == 'POST':
            values, errors = form_values()
            if not errors:
                plan_id = str(uuid4())
                now = datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')
                db = get_db()
                with db:
                    db.execute('''INSERT INTO plans
                        (id, title, content, start_date, end_date, priority, success_criteria,
                         estimated_minutes, created_at, updated_at, version, owner_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)''',
                        (plan_id, *(values[k] for k in FIELDS), now, now, g.user['id']))
                    snapshot(db, plan_id)
                    if source_improvement:
                        db.execute('''INSERT INTO next_plan_links(next_plan_id, previous_plan_id, improvement_id, created_at)
                            VALUES (?, ?, ?, ?)''', (plan_id, source_improvement['plan_id'], source_improvement['id'], now))
                return redirect(url_for('detail', plan_id=plan_id), code=303)
        return render_template('form.html', values=values, errors=errors, plan=None, source_improvement=source_improvement), 400 if errors else 200

    @app.get('/plans/<plan_id>')
    def detail(plan_id):
        plan = find_plan(plan_id)
        versions = get_db().execute('SELECT * FROM plan_versions WHERE plan_id = ? ORDER BY version DESC', (plan_id,)).fetchall()
        return render_template('detail.html', plan=plan, versions=versions)

    @app.route('/plans/<plan_id>/edit', methods=['GET', 'POST'])
    def edit_plan(plan_id):
        plan = find_plan(plan_id)
        values, errors = dict(plan), []
        status = 200
        if request.method == 'POST':
            values, errors = form_values()
            version = request.form.get('version', '')
            if not version.isascii() or not version.isdecimal() or len(version) > 10:
                errors.append('수정 버전이 올바르지 않습니다. 상세 화면에서 다시 수정해 주세요.')
            if errors:
                status = 400
            else:
                db = get_db()
                now = datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')
                with db:
                    result = db.execute('''UPDATE plans SET title = ?, content = ?, start_date = ?,
                        end_date = ?, priority = ?, success_criteria = ?, estimated_minutes = ?,
                        updated_at = ?, version = version + 1 WHERE id = ? AND version = ?''',
                        (*(values[k] for k in FIELDS), now, plan_id, int(version)))
                    if result.rowcount:
                        snapshot(db, plan_id)
                    else:
                        errors.append('다른 창에서 수정된 계획입니다. 상세 화면에서 최신 내용을 확인한 뒤 다시 수정해 주세요.')
                        status = 409
                if not errors:
                    return redirect(url_for('detail', plan_id=plan_id), code=303)
        return render_template('form.html', values=values, errors=errors, plan=plan), status

    @app.errorhandler(404)
    def missing(error):
        return render_template('error.html', message='계획을 찾을 수 없습니다.'), 404

    from tasks import register_tasks
    register_tasks(app, get_db)
    from executions import register_executions
    register_executions(app, get_db)
    from review import register_review
    register_review(app, get_db)
    from review_experiment import register_experiment
    register_experiment(app, get_db)
    from export_data import register_export
    register_export(app, get_db)
    return app


if __name__ == '__main__':
    create_app().run(host='127.0.0.1', port=int(os.environ.get('PDS_PORT', '5000')), debug=False)
