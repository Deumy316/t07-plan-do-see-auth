from contextlib import closing
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
import secrets
import sqlite3
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from flask import template_rendered
from app import ROOT, create_app
from auth import COOKIE
from auth_support import authenticated_client, csrf
from review_experiment import (build_experiment, daily_records, evidence_for,
                               migrate_review, minute_display, utc_text)


def record(day=1, seconds=60, start=None):
    start = start or datetime(2026, 9, day, tzinfo=timezone.utc)
    end = start + timedelta(seconds=seconds)
    return dict(id=str(uuid4()), task_id=str(uuid4()), request_id=str(uuid4()), started_at=utc_text(start),
                ended_at=utc_text(end), actual_minutes=round(seconds / 60, 2),
                content='test-only record', blocked_reason='', created_at=utc_text(end))


class DailyMathTests(unittest.TestCase):
    def test_korean_midnight_splits_exact_time_and_preserves_evidence(self):
        row = record(start=datetime(2026, 9, 18, 14, 59, 30, tzinfo=timezone.utc), seconds=60)
        days = daily_records([row])
        self.assertEqual([d['day'] for d in days], ['2026-09-18', '2026-09-19'])
        self.assertEqual([d['minutes'] for d in days], [Fraction(1, 2), Fraction(1, 2)])
        self.assertTrue(all(d['evidence'][0]['record_id'] == row['id'] for d in days))

    def test_exclusive_midnight_and_explicit_zero_record(self):
        days = daily_records([record(start=datetime(2026, 9, 18, 14, 59, tzinfo=timezone.utc), seconds=60)])
        self.assertEqual(len(days), 1)
        self.assertEqual(days[0]['day'], '2026-09-18')
        zero = daily_records([record(day=20, seconds=0)])
        self.assertEqual([(r['day'], r['minutes']) for r in zero], [('2026-09-20', 0)])

    def test_multiple_records_round_only_final_value(self):
        rows = [record(day=1, seconds=3), record(day=1, seconds=3)]
        days = daily_records(rows)
        self.assertEqual(len(days), 1)
        self.assertEqual(len(days[0]['evidence']), 2)
        self.assertEqual(minute_display(days[0]['minutes']), '0.1')
        self.assertEqual([minute_display(e['minutes']) for e in days[0]['evidence']], ['0.1', '0.1'])

    def test_microsecond_precision_and_half_up_boundaries(self):
        self.assertEqual(minute_display(Fraction(2_999_999, 60_000_000)), '0.0')
        self.assertEqual(minute_display(Fraction(3_000_000, 60_000_000)), '0.1')
        row = record(start=datetime(2026, 9, 18, 14, 59, 59, 999999, tzinfo=timezone.utc), seconds=.000002)
        days = daily_records([row])
        self.assertEqual([d['microseconds'] for d in days], [1, 1])
        self.assertEqual(sum((d['minutes'] for d in days)), Fraction(2, 60_000_000))

    def test_missing_dates_not_filled_and_average_uses_recorded_days(self):
        report = build_experiment([record(day=1, seconds=60), record(day=4, seconds=180)], [])
        self.assertEqual(report['recorded_days'], 2)
        self.assertEqual(report['selected_dates'], ['2026-09-01', '2026-09-04'])
        self.assertEqual(report['current_average'], 2)
        self.assertIsNone(report['five_average'])
        self.assertFalse(report['complete'])

    def test_more_than_five_requires_explicit_valid_selection(self):
        records = [record(day=day, seconds=day * 60) for day in range(1, 7)]
        automatic = build_experiment(records, [])
        self.assertEqual(automatic['selected'], [])
        self.assertIsNone(automatic['five_total'])
        selected = build_experiment(records, [], ['2026-09-06', '2026-09-04', '2026-09-03', '2026-09-02', '2026-09-01'])
        self.assertEqual(selected['five_total'], 16)
        self.assertEqual(selected['five_average'], Fraction(16, 5))
        self.assertEqual(selected['selected_dates'][0], '2026-09-01')
        self.assertIsNotNone(build_experiment(records, [], ['2026-09-01'] * 5)['selection_error'])
        self.assertIsNotNone(build_experiment(records, [], ['2026-10-01'])['selection_error'])

    def test_five_two_three_denominators_and_pending_third_day(self):
        records = [record(day=day, seconds=day * 60) for day in range(1, 6)]
        change = dict(id='test-change', day1='2026-09-01', day2='2026-09-02',
                      created_at='2026-09-02T12:00:00Z', next_work_started_at=None,
                      evidence=evidence_for(daily_records(records[:2]), ['2026-09-01', '2026-09-02']))
        pending = build_experiment(records[:2], [change])
        self.assertFalse(pending['complete'])
        self.assertFalse(pending['chosen']['assessment']['third_day_known'])
        result = build_experiment(records, [change])
        self.assertTrue(result['complete'])
        self.assertEqual(result['before']['total'], 3)
        self.assertEqual(result['before']['average'], Fraction(3, 2))
        self.assertEqual(result['after']['total'], 12)
        self.assertEqual(result['after']['average'], 4)
        self.assertEqual(result['five_average'], 3)


class DailyRouteTests(unittest.TestCase):
    def setUp(self):
        self.path = ROOT / 'instance/tests' / (uuid4().hex + '.sqlite3')
        self.app = create_app(self.path)
        self.app.config['TESTING'] = True
        self.a = authenticated_client(self.app)
        self.b = self.app.test_client()
        password = secrets.token_urlsafe(24)
        token = csrf(self.b, '/signup')
        self.assertEqual(self.b.post('/signup', data=dict(username='other_user', password=password, csrf_token=token)).status_code, 303)
        self.assertEqual(self.b.post('/login', data=dict(username='other_user', password=password, csrf_token=token)).status_code, 303)
        self.plan, self.task = self.make_plan(self.a)
        self.other_plan, self.other_task = self.make_plan(self.b)

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def post(self, client, path, data):
        return client.post(path, data=dict(data, csrf_token=csrf(client)))

    def make_plan(self, client):
        response = self.post(client, '/plans/new', dict(title='daily fixture', content='', start_date='2026-09-01',
            end_date='2026-09-30', priority='medium', success_criteria='', estimated_minutes=30))
        self.assertEqual(response.status_code, 303)
        plan = response.location.split('/')[-1]
        task = str(uuid4())
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO tasks VALUES (?, ?, 'fixture', '', NULL, 'medium', 30, 'active', '2026-09-01T00:00:00Z', '2026-09-01T00:00:00Z', NULL, NULL, 1)", (task, plan))
        return plan, task

    def seed(self, day, seconds=60, task=None):
        row = record(day, seconds)
        row['task_id'] = task or self.task
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('INSERT INTO execution_records (' + ','.join(row) + ') VALUES (' + ','.join('?' for _ in row) + ')', tuple(row.values()))
        return row['id']

    def rule_data(self, **changes):
        return dict(plan_id=self.plan, before_rule='original <script>rule</script>', after_rule='new rule',
                    reason='test-only reason', day1='2026-09-01', day2='2026-09-02', confirmed_complete='yes', **changes)

    def save_rule(self, at='2026-09-02T12:00:00+00:00', client=None, data=None):
        with patch('review_experiment.server_now', return_value=datetime.fromisoformat(at)):
            response = self.post(client or self.a, '/review/rules', data or self.rule_data())
        self.assertEqual(response.status_code, 303)
        return parse_qs(urlparse(response.location).query)['rule_change_id'][0]

    def report(self, client=None, **query):
        contexts = []
        def capture(sender, template, context, **extra):
            contexts.append(context)
        with template_rendered.connected_to(capture, self.app):
            response = (client or self.a).get('/review', query_string=dict(plan_id=self.plan, **query))
        self.assertEqual(response.status_code, 200)
        return contexts[-1]['experiment'], response

    def snapshot(self):
        with closing(sqlite3.connect(self.path)) as db:
            tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            return {table: db.execute('SELECT * FROM ' + table + ' ORDER BY 1,2').fetchall() for table in tables}

    def test_one_day_progress_no_five_result_and_no_automatic_rule(self):
        self.seed(1, 183)
        before = self.snapshot()
        report, response = self.report()
        self.assertTrue('1/5일 기록, 비교 준비 중'.encode() in response.data)
        self.assertTrue(b'data-current-total>3.1' in response.data)
        self.assertTrue(b'id="five-day-results"' not in response.data)
        self.assertEqual(report['history'], [])
        self.assertTrue(before == self.snapshot())
        self.assertEqual(self.post(self.a, '/review/rules', self.rule_data()).status_code, 400)
        self.assertTrue(before == self.snapshot())

    def test_save_then_complete_preserves_history_and_exports_only_owner(self):
        self.seed(1, 60)
        self.seed(2, 120)
        first = self.save_rule(data=self.rule_data(created_at='1900-01-01T00:00:00Z', user_id='forged'))
        self.assertFalse(self.report()[0]['complete'])
        for day in range(3, 6):
            self.seed(day, day * 60)
        result, response = self.report()
        self.assertTrue(result['complete'])
        self.assertEqual(result['before']['average'], Fraction(3, 2))
        self.assertEqual(result['after']['average'], 4)
        self.assertTrue(b'id="rule-comparison"' in response.data)
        self.assertTrue(b'<script>rule</script>' not in response.data)
        self.assertEqual(result['chosen']['created_at'], '2026-09-02T12:00:00.000000Z')
        before = self.snapshot()['review_rule_changes']
        second = self.save_rule(at='2026-09-05T12:00:00+00:00')
        self.assertNotEqual(first, second)
        self.assertTrue(all(row in self.snapshot()['review_rule_changes'] for row in before))
        self.assertFalse(self.report()[0]['complete'])
        self.assertTrue(self.report(rule_change_id=first)[0]['complete'])
        own = self.a.get('/export.json').json['data']
        other = self.b.get('/export.json').json['data']
        self.assertEqual(len(own['review_rule_changes']), 2)
        self.assertEqual(len(own['review_rule_evidence']), 4)
        self.assertEqual(other['review_rule_changes'], [])
        self.assertEqual(other['review_rule_evidence'], [])

    def test_early_late_and_equal_third_start_never_complete(self):
        self.seed(1)
        self.seed(2)
        early = self.save_rule(at='2026-09-02T00:00:30+00:00')
        for day in range(3, 6):
            self.seed(day)
        self.assertFalse(self.report(rule_change_id=early)[0]['complete'])
        equal = self.save_rule(at='2026-09-03T00:00:00+00:00')
        self.assertFalse(self.report(rule_change_id=equal)[0]['complete'])
        # Editing/removing later work cannot erase that it was already observed.
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE execution_records SET started_at='2026-09-03T01:00:00Z', ended_at='2026-09-03T01:01:00Z' WHERE started_at LIKE '2026-09-03%'")
        self.assertFalse(self.report(rule_change_id=equal)[0]['complete'])

    def test_changed_or_added_baseline_evidence_invalidates_without_overwrite(self):
        self.seed(1)
        target = self.seed(2)
        change = self.save_rule()
        for day in range(3, 6):
            self.seed(day)
        self.assertTrue(self.report()[0]['complete'])
        stored = self.snapshot()['review_rule_evidence']
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE execution_records SET ended_at='2026-09-02T00:02:00Z' WHERE id=?", (target,))
        self.assertFalse(self.report(rule_change_id=change)[0]['complete'])
        self.assertTrue(stored == self.snapshot()['review_rule_evidence'])
        self.seed(1, 30)
        self.assertFalse(self.report(rule_change_id=change)[0]['complete'])

    def test_selection_must_match_rule_and_cannot_hide_earlier_third_work(self):
        for day in range(1, 7):
            self.seed(day)
        change = self.save_rule(at='2026-09-03T12:00:00+00:00')
        self.assertEqual(self.report()[0]['selected'], [])
        selected = ['2026-09-01', '2026-09-02', '2026-09-04', '2026-09-05', '2026-09-06']
        result, _ = self.report(dates=selected, rule_change_id=change)
        self.assertIsNotNone(result['five_total'])
        self.assertFalse(result['complete'])
        self.assertEqual(self.a.get('/review', query_string={'plan_id': self.plan, 'dates': ['invalid']}).status_code, 400)

    def test_bidirectional_ownership_csrf_and_nonexistent_update_routes(self):
        for task in (self.task, self.other_task):
            self.seed(1, task=task)
            self.seed(2, task=task)
        a_rule = self.save_rule()
        data = self.rule_data()
        data['plan_id'] = self.other_plan
        b_rule = self.save_rule(client=self.b, data=data)
        for client, own, foreign, foreign_rule in ((self.a, self.plan, self.other_plan, b_rule), (self.b, self.other_plan, self.plan, a_rule)):
            before = self.snapshot()
            self.assertEqual(client.get('/review/rules', query_string={'plan_id': foreign}).status_code, 404)
            self.assertEqual(self.post(client, '/review/rules', dict(self.rule_data(), plan_id=foreign)).status_code, 404)
            self.assertEqual(client.get('/review', query_string={'plan_id': own, 'rule_change_id': foreign_rule}).status_code, 404)
            self.assertTrue(before == self.snapshot())
        raw = self.app.test_client()
        raw.set_cookie(COOKIE, self.a.get_cookie(COOKIE).value)
        before = self.snapshot()
        self.assertEqual(raw.post('/review/rules', data=self.rule_data()).status_code, 403)
        self.assertEqual(self.post(self.a, '/review/rules/' + a_rule + '/edit', self.rule_data()).status_code, 404)
        self.assertTrue(before == self.snapshot())
        self.assertEqual(self.app.test_client().get('/review/rules?plan_id=' + self.plan).status_code, 303)

    def test_validation_and_evidence_insert_failure_are_atomic(self):
        self.seed(1)
        self.seed(2)
        before = self.snapshot()
        for updates in ({'day2': '2026-09-01'}, {'day1': 'missing'}, {'confirmed_complete': ''}, {'reason': ''}):
            self.assertEqual(self.post(self.a, '/review/rules', dict(self.rule_data(), **updates)).status_code, 400)
            self.assertTrue(before == self.snapshot())
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("CREATE TRIGGER reject_evidence BEFORE INSERT ON review_rule_evidence BEGIN SELECT RAISE(ABORT, 'fixture failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.save_rule()
        self.assertTrue(before == self.snapshot())


class DailyMigrationTests(unittest.TestCase):
    def setUp(self):
        self.path = ROOT / 'instance/tests' / (uuid4().hex + '.sqlite3')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('CREATE TABLE plans (id TEXT PRIMARY KEY, title TEXT)')
            db.execute("INSERT INTO plans VALUES ('fixture', 'preserve')")

    def tearDown(self):
        self.path.unlink(missing_ok=True)
        for backup in (self.path.parent / 'backups').glob(self.path.name + '.before-daily-review.*.sqlite3'):
            backup.unlink()

    def test_backup_before_schema_change_and_idempotent_migration(self):
        with closing(sqlite3.connect(self.path)) as db:
            backup = migrate_review(db, self.path)
            self.assertEqual(db.execute('SELECT count(*) FROM review_rule_changes').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT * FROM plans').fetchall(), [('fixture', 'preserve')])
            self.assertIsNone(migrate_review(db, self.path))
        with closing(sqlite3.connect(backup)) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(db.execute('SELECT * FROM plans').fetchall(), [('fixture', 'preserve')])
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='review_rule_changes'").fetchone())

    def test_backup_failure_prevents_schema_change(self):
        with closing(sqlite3.connect(self.path)) as db:
            with patch('review_experiment.sqlite3.connect', side_effect=sqlite3.OperationalError('fixture backup failure')):
                with self.assertRaises(sqlite3.OperationalError):
                    migrate_review(db, self.path)
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='review_rule_changes'").fetchone())
            self.assertEqual(db.execute('SELECT * FROM plans').fetchall(), [('fixture', 'preserve')])
