from contextlib import closing
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
import re
import sqlite3
import unittest
from unittest.mock import patch
from uuid import uuid4

from flask import template_rendered
from app import ROOT, create_app
from auth_support import authenticated_client
from review import seoul_today


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.path = ROOT / 'instance/tests' / f'{uuid4()}.sqlite3'
        self.app = create_app(self.path)
        self.app.config['TESTING'] = True
        self.client = authenticated_client(self.app)
        self.today = date(2026, 9, 17)

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def plan(self):
        response = self.client.post('/plans/new', data=dict(title='[돌아보기 검증 전용] 계획', content='',
            start_date='2026-09-01', end_date='2026-09-30', priority='medium', success_criteria='', estimated_minutes='999'))
        return response.headers['Location'].split('/')[-1]

    def task(self, plan, minutes, due=None, status='active', deleted=False):
        task_id = str(uuid4())
        stamp = '2026-09-16T00:00:00.000000Z'
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('''INSERT INTO tasks VALUES (?, ?, ?, ?, ?, 'medium', ?, ?, ?, ?, ?, ?, 1)''',
                       (task_id, plan, '[검증 전용] ' + task_id, '<script>literal</script>', due, minutes,
                        status, stamp, stamp, stamp if status == 'completed' else None, stamp if deleted else None))
        return task_id

    def record(self, task, minutes, reason):
        record_id = str(uuid4())
        end = (datetime(2026, 9, 16, tzinfo=timezone.utc) + timedelta(seconds=round(minutes * 60))).isoformat(timespec='microseconds').replace('+00:00', 'Z')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('INSERT INTO execution_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (record_id, task, str(uuid4()), '2026-09-16T00:00:00.000000Z', end,
                 minutes, '[검증 전용] 내용', reason, end))
        return record_id

    def report(self, plan=None):
        contexts = []
        def capture(sender, template, context, **extra):
            contexts.append(context)
        with template_rendered.connected_to(capture, self.app), patch('review.seoul_today', return_value=self.today):
            response = self.client.get('/review', query_string={'plan_id': plan} if plan else {})
        self.assertEqual(response.status_code, 200)
        return contexts[-1]['report'], response.get_data(as_text=True)

    def snapshot(self):
        with closing(sqlite3.connect(self.path)) as db:
            tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            return {t: db.execute('SELECT * FROM ' + t + ' ORDER BY 1,2').fetchall() for t in tables}

    def evidence(self, html, metric, kind):
        block = html.split('id="metric-' + metric + '"', 1)[1].split('</details>', 1)[0]
        return re.findall('data-evidence-' + kind + '="([^"]+)"', block)

    def test_mixed_totals_and_every_evidence_list(self):
        plan, other = self.plan(), self.plan()
        past = self.task(plan, 10, '2026-09-16')
        done = self.task(plan, 20, '2026-09-15', 'completed')
        today = self.task(plan, 30, '2026-09-17')
        no_due = self.task(plan, 40)
        future = self.task(plan, 50, '2026-09-18')
        deleted = self.task(plan, 900, '2026-09-01', deleted=True)
        foreign = self.task(other, 700, '2026-09-01')
        r1 = self.record(past, .1, '막힘 1')
        r2 = self.record(past, .2, '막힘 2')
        r3 = self.record(past, 1, '')
        r4 = self.record(done, 5, '완료했지만 막힘 기록 있음')
        r5 = self.record(today, 10, ' \t\n\u3000')
        self.record(deleted, 900, '제외')
        self.record(foreign, 700, '다른 계획 제외')
        before = self.snapshot()
        report, html = self.report(plan)
        self.assertEqual(report['metrics'], dict(planned=5, completed=1, overdue=1, blocked=2,
            estimated=150, actual=Decimal('16.3'), difference=Decimal('-133.7')))
        task_ids, record_ids = {past, done, today, no_due, future}, {r1, r2, r3, r4, r5}
        for metric, ids in [('planned', task_ids), ('completed', {done}), ('overdue', {past}),
                            ('blocked', {past, done}), ('estimated', task_ids), ('difference', task_ids)]:
            actual = self.evidence(html, metric, 'task')
            self.assertEqual(set(actual), ids)
            self.assertEqual(len(actual), len(ids))
        for metric, ids in [('actual', record_ids), ('blocked', {r1, r2, r4}), ('difference', record_ids)]:
            self.assertEqual(set(self.evidence(html, metric, 'record')), ids)
        for name, value in report['metrics'].items():
            self.assertIn(f'data-metric="{name}">{value}', html)
        self.assertEqual(sum(t['estimated_minutes'] for t in report['tasks']), report['metrics']['estimated'])
        self.assertEqual(sum(Decimal(str(r['actual_minutes'])) for r in report['records']), report['metrics']['actual'])
        self.assertIn('계획한 할 일', html)
        self.assertIn('2026-09-17 (Asia/Seoul)', html)
        self.assertNotIn('<script>literal</script>', html)
        self.assertEqual(self.snapshot(), before)

    def test_empty_no_plan_empty_plan_deleted_only(self):
        report, html = self.report()
        self.assertTrue(all(v == 0 for v in report['metrics'].values()))
        self.assertIn('아직 계획이 없습니다', html)
        for plan in [self.plan()]:
            report, html = self.report(plan)
            self.assertTrue(all(v == 0 for v in report['metrics'].values()))
            self.assertEqual(report['tasks'], [])
            self.assertEqual(report['records'], [])
        plan = self.plan()
        deleted = self.task(plan, 60, '2026-09-01', 'completed', True)
        self.record(deleted, 90, '삭제된 막힘')
        report, html = self.report(plan)
        self.assertTrue(all(v == 0 for v in report['metrics'].values()))
        self.assertEqual(self.evidence(html, 'difference', 'task'), [])
        self.assertEqual(self.evidence(html, 'difference', 'record'), [])

    def test_current_status_not_completion_events(self):
        plan = self.plan()
        task = self.task(plan, 5, '2026-09-16')
        self.client.post(f'/tasks/{task}/state', data=dict(target_status='completed', version=1, request_id=str(uuid4())))
        self.assertEqual(self.report(plan)[0]['metrics']['completed'], 1)
        self.client.post(f'/tasks/{task}/state', data=dict(target_status='active', version=2, request_id=str(uuid4())))
        report, html = self.report(plan)
        self.assertEqual(report['metrics']['completed'], 0)
        self.assertEqual(report['metrics']['overdue'], 1)
        self.assertEqual(self.evidence(html, 'completed', 'task'), [])
        self.assertEqual(self.evidence(html, 'overdue', 'task'), [task])

    def test_korean_today_utc_boundary_and_invalid_plan(self):
        class FixedClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 9, 16, 15, 0, tzinfo=timezone.utc).astimezone(tz)
        with patch('review.datetime', FixedClock):
            self.assertEqual(seoul_today(), date(2026, 9, 17))
        self.assertEqual(self.client.get('/review?plan_id=missing').status_code, 404)


if __name__ == '__main__':
    unittest.main(verbosity=2)
