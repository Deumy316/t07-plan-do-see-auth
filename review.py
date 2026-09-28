from datetime import datetime, timezone
from uuid import uuid4
from decimal import Decimal, localcontext
from zoneinfo import ZoneInfo

from flask import g, render_template, request, redirect, url_for
from review_experiment import build_experiment, load_changes


def seoul_today():
    return datetime.now(ZoneInfo('Asia/Seoul')).date()


def build_review(tasks, records, today):
    completed = [t for t in tasks if t['status'] == 'completed']
    overdue = [t for t in tasks if t['status'] == 'active' and t['due_date'] and t['due_date'] < today.isoformat()]
    blocked_records = [r for r in records if r['blocked_reason'].strip()]
    blocked_ids = {r['task_id'] for r in blocked_records}
    blocked = [t for t in tasks if t['id'] in blocked_ids]
    estimated = sum(t['estimated_minutes'] for t in tasks)
    # Sum the stored two-decimal minutes as decimal values, not binary floats.
    with localcontext() as context:
        context.prec = 50
        actual = sum((Decimal(str(r['actual_minutes'])) for r in records), Decimal(0))
        difference = actual - Decimal(estimated)
    return {
        'tasks': tasks, 'completed': completed, 'overdue': overdue, 'blocked': blocked,
        'records': records, 'blocked_records': blocked_records,
        'metrics': {'planned': len(tasks), 'completed': len(completed), 'overdue': len(overdue),
                    'blocked': len(blocked), 'estimated': estimated, 'actual': actual, 'difference': difference},
    }


def register_review(app, get_db):
    @app.context_processor
    def review_context():
        return {'review_menu': request.endpoint in ('review_page', 'rule_change_new')}

    @app.template_filter('review_number')
    def review_number(value):
        text = format(value, 'f') if isinstance(value, Decimal) else str(value)
        return text.rstrip('0').rstrip('.') if '.' in text else text

    @app.route('/review', methods=['GET', 'POST'])
    def review_page():
        today = seoul_today()
        db = get_db()
        improvement_text = request.form.get('improvement_content', '').strip() if request.method == 'POST' else ''
        errors = []
        # All evidence and totals come from a single consistent read snapshot.
        with db:
            db.execute('BEGIN IMMEDIATE' if request.method == 'POST' else 'BEGIN')
            plans = db.execute('SELECT id, title FROM plans WHERE owner_id = ? ORDER BY created_at, id', (g.user['id'],)).fetchall()
            plan_id = (request.form.get('plan_id', '') if request.method == 'POST'
                       else request.args.get('plan_id', plans[0]['id'] if plans else ''))
            plan = next((p for p in plans if p['id'] == plan_id), None)
            if plan_id and not plan:
                return render_template('error.html', message='계획을 찾을 수 없습니다.'), 404
            if request.method == 'POST':
                if not plan:
                    errors.append('개선점을 연결할 계획을 선택해 주세요.')
                if not improvement_text:
                    errors.append('다음 계획에서 고칠 점을 입력해 주세요.')
                if not errors:
                    db.execute('INSERT INTO review_improvements(id, plan_id, content, created_at) VALUES (?, ?, ?, ?)',
                               (str(uuid4()), plan_id, improvement_text,
                                datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')))
                    return redirect(url_for('review_page', plan_id=plan_id) + '#improvements', code=303)
            tasks = db.execute('''SELECT * FROM tasks WHERE plan_id = ? AND deleted_at IS NULL
                ORDER BY due_date IS NULL, due_date, id''', (plan_id,)).fetchall()
            records = db.execute('''SELECT e.*, t.title AS task_title FROM execution_records e
                JOIN tasks t ON t.id = e.task_id WHERE t.plan_id = ? AND t.deleted_at IS NULL
                ORDER BY e.started_at DESC, e.id''', (plan_id,)).fetchall()
            improvements = [dict(row) for row in db.execute('''SELECT * FROM review_improvements
                WHERE plan_id = ? ORDER BY created_at DESC, id''', (plan_id,))]
            for improvement in improvements:
                improvement['next_plans'] = db.execute('''SELECT p.id, p.title FROM next_plan_links l
                    JOIN plans p ON p.id = l.next_plan_id WHERE l.improvement_id = ? AND p.owner_id = ? ORDER BY l.created_at, p.id''',
                    (improvement['id'], g.user['id'])).fetchall()
            changes = load_changes(db, plan_id)
        report = build_review(tasks, records, today)
        experiment = build_experiment(records, changes, request.args.getlist('dates'), request.args.get('rule_change_id'))
        return render_template('review.html', plans=plans, plan=plan, today=today.isoformat(), report=report,
                               experiment=experiment, improvements=improvements, improvement_text=improvement_text,
                               errors=errors), 400 if errors or experiment['selection_error'] else 200
