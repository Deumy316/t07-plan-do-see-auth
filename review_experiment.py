"""Exact elapsed-time daily review and append-only rule-change evidence."""
from contextlib import closing
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP, localcontext
from fractions import Fraction
from pathlib import Path
import sqlite3
from uuid import uuid4
from zoneinfo import ZoneInfo

from flask import abort, g, redirect, render_template, request, url_for

SEOUL = ZoneInfo('Asia/Seoul')
MINUTE_US = 60_000_000
EVIDENCE_FIELDS = ('day', 'record_id', 'task_id', 'started_at', 'ended_at', 'record_created_at')


def server_now():
    return datetime.now(timezone.utc)


def parse_time(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)


def utc_text(value):
    return value.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def microseconds(delta):
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def minute_display(value):
    value = Fraction(value)
    with localcontext() as context:
        context.prec = 50
        number = Decimal(value.numerator) / Decimal(value.denominator)
        return format(number.quantize(Decimal('0.1'), rounding=ROUND_HALF_UP), '.1f')


def migrate_review(db, path):
    """Back up populated databases before adding either review table.

    Hold a write reservation while a separate read connection takes the backup.
    No existing data rows, account credentials, or execution times are changed.
    """
    path = Path(path).resolve()
    with db:
        db.execute('BEGIN IMMEDIATE')
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if {'review_rule_changes', 'review_rule_evidence'} <= names:
            return None
        populated = any(db.execute('SELECT 1 FROM "' + name.replace('"', '""') + '" LIMIT 1').fetchone()
                        for name in names if not name.startswith('sqlite_'))
        backup = None
        if populated:
            folder = path.parent / 'backups'
            folder.mkdir(exist_ok=True)
            backup = folder / (path.name + '.before-daily-review.' + uuid4().hex + '.sqlite3')
            with backup.open('xb'):
                pass
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as source:
                with closing(sqlite3.connect(backup)) as target:
                    source.backup(target)
                    if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise RuntimeError('Review migration backup integrity failed')
        ddl = (Path(__file__).parent / 'review_schema.sql').read_text(encoding='utf-8')
        # This DDL contains only table/index statements, no triggers or data writes.
        for statement in ddl.split(';'):
            if statement.strip():
                db.execute(statement)
        return backup


def daily_records(records):
    groups = {}
    for row in records:
        record = dict(row)
        start, end = parse_time(record['started_at']), parse_time(record['ended_at'])
        if end < start:
            raise ValueError('Execution ends before it starts')
        cursor = start
        while True:
            day = cursor.astimezone(SEOUL).date()
            if day == end.astimezone(SEOUL).date():
                stop = end
            else:
                midnight = datetime.combine(day + timedelta(days=1), time.min, tzinfo=SEOUL).astimezone(timezone.utc)
                stop = min(end, midnight)
            duration = microseconds(stop - cursor)
            key = day.isoformat()
            group = groups.setdefault(key, {'day': key, 'microseconds': 0, 'evidence': []})
            group['microseconds'] += duration
            group['evidence'].append(dict(day=key, record_id=record['id'], task_id=record['task_id'],
                started_at=record['started_at'], ended_at=record['ended_at'], record_created_at=record['created_at'],
                segment_start=utc_text(cursor), segment_end=utc_text(stop), minutes=Fraction(duration, MINUTE_US)))
            if stop == end:
                break  # Midnight is an exclusive end; no fabricated next-day zero.
            cursor = stop
    days = [groups[key] for key in sorted(groups)]
    for group in days:
        group['minutes'] = Fraction(group['microseconds'], MINUTE_US)
    return days


def evidence_for(days, selected):
    return [{field: item[field] for field in EVIDENCE_FIELDS}
            for day in days if day['day'] in selected for item in day['evidence']]


def evaluate_change(change, days):
    reasons = []
    selected = [change['day1'], change['day2']]
    current = evidence_for(days, selected)
    fingerprint = lambda rows: sorted(tuple(row[field] for field in EVIDENCE_FIELDS) for row in rows)
    if not all(any(row['day'] == day for row in current) for day in selected):
        reasons.append('연결한 1·2일차 기록을 현재 계획에서 찾을 수 없습니다.')
    if fingerprint(current) != fingerprint(change['evidence']):
        reasons.append('저장 후 근거 기록이 추가·수정·이동·삭제되었습니다. 저장 당시 근거와 현재 기록이 달라 비교를 완료할 수 없습니다.')
    saved = parse_time(change['created_at'])
    original = change['evidence']
    if original and saved < max(max(parse_time(row['ended_at']), parse_time(row['record_created_at'])) for row in original):
        reasons.append('2일차 실행 종료와 기록 저장을 모두 마친 이후에 변경한 기록이 아닙니다.')
    later = [item for day in days if day['day'] > change['day2'] for item in day['evidence']]
    later_starts = [parse_time(row['started_at']) for row in later]
    if change['next_work_started_at']:
        later_starts.append(parse_time(change['next_work_started_at']))
    if later_starts and saved >= min(later_starts):
        reasons.append('규칙 저장 시각이 3일차 이후 작업의 첫 시작보다 빠르지 않습니다. 선택에서 제외한 날짜의 작업도 검사합니다.')
    return {'reasons': reasons, 'eligible': not reasons, 'third_day_known': bool(later),
            'pending': None if later else '3일차 작업 기록이 아직 없어 변경 시점의 최종 확인을 기다립니다.'}


def build_experiment(records, changes, requested_dates=None, rule_id=None):
    days = daily_records(records)
    by_day = {day['day']: day for day in days}
    choices = list(requested_dates or [])
    selection_error = None
    if choices:
        if len(choices) != len(set(choices)) or len(choices) > 5 or any(day not in by_day for day in choices):
            selection_error = '현재 계획에 기록이 있는 서로 다른 날짜를 최대 5개 선택해 주세요.'
            choices = []
        else:
            choices.sort()
    elif len(days) <= 5:
        choices = list(by_day)
    selected = [by_day[day] for day in choices]
    current_total = sum((day['minutes'] for day in days), Fraction(0))
    five_total = sum((day['minutes'] for day in selected), Fraction(0)) if len(selected) == 5 else None
    history = [dict(change, assessment=evaluate_change(change, days)) for change in changes]
    chosen = next((change for change in history if change['id'] == rule_id), None) if rule_id else (history[0] if history else None)
    if rule_id and chosen is None:
        abort(404)
    reasons = []
    if len(selected) != 5:
        reasons.append('비교할 실제 기록 날짜 5일이 필요합니다.')
    if len(days) > 5 and not choices:
        reasons.append('기록 날짜가 5일보다 많습니다. 비교에 포함할 5일을 직접 선택해 주세요.')
    if chosen is None:
        reasons.append('사용자가 저장한 규칙 변경 기록이 필요합니다.')
    else:
        reasons.extend(chosen['assessment']['reasons'])
        if choices[:2] != [chosen['day1'], chosen['day2']]:
            reasons.append('선택한 날짜의 첫 2일이 규칙 변경에 연결한 1·2일차와 다릅니다.')
        if not chosen['assessment']['third_day_known']:
            reasons.append(chosen['assessment']['pending'])
    before = after = None
    if not reasons:
        before_days, after_days = selected[:2], selected[2:]
        before_total = sum((day['minutes'] for day in before_days), Fraction(0))
        after_total = sum((day['minutes'] for day in after_days), Fraction(0))
        before = dict(days=before_days, total=before_total, average=before_total / 2)
        after = dict(days=after_days, total=after_total, average=after_total / 3)
    return dict(days=days, selected=selected, selected_dates=choices, recorded_days=len(days),
                current_total=current_total, current_average=current_total / len(days) if days else None,
                five_total=five_total, five_average=five_total / 5 if five_total is not None else None,
                history=history, chosen=chosen, reasons=reasons, before=before, after=after,
                selection_error=selection_error, complete=not reasons)


def load_changes(db, plan_id):
    changes = [dict(row) for row in db.execute('SELECT * FROM review_rule_changes WHERE plan_id=? ORDER BY created_at DESC, id DESC', (plan_id,))]
    for change in changes:
        change['evidence'] = [dict(row) for row in db.execute('SELECT * FROM review_rule_evidence WHERE change_id=? ORDER BY day, record_id', (change['id'],))]
    return changes


def plan_records(db, plan_id):
    return db.execute('''SELECT e.* FROM execution_records e JOIN tasks t ON t.id=e.task_id
        WHERE t.plan_id=? AND t.deleted_at IS NULL ORDER BY e.started_at, e.id''', (plan_id,)).fetchall()


def register_experiment(app, get_db):
    app.jinja_env.filters['minute1'] = minute_display

    @app.route('/review/rules', methods=['GET', 'POST'])
    def rule_change_new():
        source = request.form if request.method == 'POST' else request.args
        plan_id = source.get('plan_id', '')
        db = get_db()
        errors = []
        values = {field: request.form.get(field, '').strip() for field in ('before_rule', 'after_rule', 'reason', 'day1', 'day2')}
        with db:
            db.execute('BEGIN IMMEDIATE' if request.method == 'POST' else 'BEGIN')
            plan = db.execute('SELECT id,title FROM plans WHERE id=? AND owner_id=?', (plan_id, g.user['id'])).fetchone()
            if plan is None:
                abort(404)
            days = daily_records(plan_records(db, plan_id))
            available = {day['day'] for day in days}
            if request.method == 'GET':
                values.update(day1=days[0]['day'] if days else '', day2=days[1]['day'] if len(days) > 1 else '')
            else:
                for field in ('before_rule', 'after_rule', 'reason'):
                    if not values[field] or len(values[field]) > 5000:
                        errors.append('변경 전·후 규칙과 이유는 각각 1~5000자로 입력해 주세요.')
                        break
                if values['before_rule'] == values['after_rule']:
                    errors.append('변경 전 규칙과 변경 후 규칙을 구분해 입력해 주세요.')
                if values['day1'] not in available or values['day2'] not in available or values['day1'] >= values['day2']:
                    errors.append('현재 계획에 기록이 있는 서로 다른 1·2일차 날짜를 시간순으로 선택해 주세요.')
                if request.form.get('confirmed_complete') != 'yes':
                    errors.append('1·2일차 실행 기록을 모두 마쳤는지 직접 확인해 주세요.')
                if not errors:
                    change_id = str(uuid4())
                    stamp = utc_text(server_now())  # Never accept a client-supplied save timestamp.
                    later_starts = [parse_time(item['started_at']) for day in days if day['day'] > values['day2'] for item in day['evidence']]
                    next_start = utc_text(min(later_starts)) if later_starts else None
                    db.execute('''INSERT INTO review_rule_changes
                        (id,plan_id,before_rule,after_rule,reason,day1,day2,created_at,next_work_started_at,confirmed_complete)
                        VALUES (?,?,?,?,?,?,?,?,?,1)''', (change_id, plan_id, values['before_rule'], values['after_rule'],
                            values['reason'], values['day1'], values['day2'], stamp, next_start))
                    for item in evidence_for(days, [values['day1'], values['day2']]):
                        db.execute('INSERT INTO review_rule_evidence VALUES (?,?,?,?,?,?,?)',
                                   (change_id, *(item[field] for field in EVIDENCE_FIELDS)))
                    return redirect(url_for('review_page', plan_id=plan_id, rule_change_id=change_id) + '#daily-review', code=303)
        return render_template('rule_change.html', plan=plan, days=days, values=values, errors=errors), 400 if errors else 200
