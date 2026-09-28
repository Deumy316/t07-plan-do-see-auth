from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
import sqlite3
import unittest
from unittest.mock import patch
from uuid import uuid4

from flask import template_rendered
from app import ROOT, create_app
from auth_support import authenticated_client
from export_data import EXPORT_FIELDS


class ExportTests(unittest.TestCase):
    def setUp(self):
        clock = patch('executions.server_now', return_value=datetime(2026, 9, 18, tzinfo=timezone.utc))
        clock.start()
        self.addCleanup(clock.stop)
        self.path = ROOT / 'instance/tests' / f'{uuid4()}.sqlite3'
        self.app = create_app(self.path)
        self.app.config.update(TESTING=True, SECRET_KEY='secret-config-sentinel')
        self.client = authenticated_client(self.app)
        self.plan_data = dict(title='[내보내기 검증] 한글 계획', content='한글 내용', start_date='2026-09-01',
            end_date='2026-09-30', priority='high', success_criteria='확인', estimated_minutes='999')

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def rows(self, table):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute('SELECT * FROM ' + table + ' ORDER BY 1,2')]

    def plan(self, **changes):
        response = self.client.post('/plans/new', data=dict(self.plan_data, **changes))
        self.assertEqual(response.status_code, 303)
        return response.headers['Location'].split('/')[-1]

    def task(self, plan_id, **changes):
        before = {r['id'] for r in self.rows('tasks')}
        self.client.post('/tasks/new', data=dict(plan_id=plan_id, title='검증 할 일', content='작업 내용', due_date='2026-09-16',
            priority='high', estimated_minutes='10', tags='태그, 한글', **changes))
        return next(r['id'] for r in self.rows('tasks') if r['id'] not in before)

    def record(self, plan, task, seconds='30', reason='막힌 이유'):
        self.assertEqual(self.client.post('/executions', data=dict(plan_id=plan, task_id=task, request_id=str(uuid4()),
            started_at='2026-09-17T09:00:00', ended_at='2026-09-17T09:00:' + seconds,
            content='실제로 한 내용', blocked_reason=reason)).status_code, 303)

    def export(self, **args):
        class FixedClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 9, 16, 15, 0, tzinfo=timezone.utc).astimezone(tz)
        with patch('export_data.datetime', FixedClock), patch.dict(os.environ, {'EXPORT_TEST_SECRET': 'environment-sentinel'}):
            response = self.client.get('/export.json', query_string=args)
        self.assertEqual(response.status_code, 200)
        self.assertIn('attachment;', response.headers['Content-Disposition'])
        self.assertIn('.json', response.headers['Content-Disposition'])
        self.assertEqual(response.content_type, 'application/json; charset=utf-8')
        return response, json.loads(response.data.decode('utf-8'))

    def test_full_export_relations_deleted_records_and_aggregate_match(self):
        previous = self.plan()
        self.client.post('/plans/' + previous + '/edit', data=dict(self.plan_data, title='수정한 한글 계획', version=1))
        self.client.post('/review', data=dict(plan_id=previous, improvement_content='다음에는 나누어 수행'))
        improvement = self.rows('review_improvements')[0]
        following = self.plan(title='다음 계획', source_improvement=improvement['id'])
        live = self.task(previous)
        deleted = self.task(previous)
        other = self.task(following)
        self.record(previous, live, '01')
        self.record(previous, live, '02')
        self.record(previous, deleted)
        self.record(following, other, '59', '')
        for version, status in [(1, 'completed'), (2, 'active'), (3, 'completed')]:
            self.client.post('/tasks/' + live + '/state', data=dict(version=version, target_status=status, request_id=str(uuid4())))
        self.client.post('/tasks/' + deleted + '/delete', data=dict(version=1, confirm='yes'))
        before = {t: self.rows(t) for t in EXPORT_FIELDS}
        response, exported = self.export(plan_id=previous)
        self.assertIn('수정한 한글 계획'.encode('utf-8'), response.data)
        self.assertIn('다음에는 나누어 수행'.encode('utf-8'), response.data)
        self.assertEqual(exported['data'], before)
        self.assertEqual({t: self.rows(t) for t in EXPORT_FIELDS}, before)
        self.assertEqual(exported['data']['next_plan_links'][0]['next_plan_id'], following)
        self.assertEqual(exported['data']['next_plan_links'][0]['improvement_id'], improvement['id'])
        self.assertIsNotNone(next(t for t in exported['data']['tasks'] if t['id'] == deleted)['deleted_at'])
        self.assertEqual(len(exported['data']['task_completion_events']), 2)
        self.assertEqual(len(exported['data']['task_state_requests']), 3)
        self.assertEqual(len(exported['data']['execution_records']), 4)
        reports = {r['plan_id']: r for r in exported['plan_reviews']}
        self.assertEqual(set(reports), {previous, following})
        self.assertEqual(reports[previous]['metrics'], dict(planned=1, completed=1, overdue=0, blocked=1,
            estimated=10, actual='0.05', difference='-9.95'))
        self.assertEqual(reports[following]['metrics']['overdue'], 1)
        self.assertEqual(reports[previous]['today_seoul'], '2026-09-17')
        for plan in (previous, following):
            contexts = []
            def capture(sender, template, context, **extra):
                contexts.append(context)
            with template_rendered.connected_to(capture, self.app), patch('review.seoul_today', return_value=datetime(2026, 9, 17).date()):
                self.client.get('/review', query_string={'plan_id': plan})
            for key, value in contexts[-1]['report']['metrics'].items():
                self.assertEqual(Decimal(str(reports[plan]['metrics'][key])), Decimal(str(value)))

    def test_empty_database_and_plan_without_tasks(self):
        response, exported = self.export()
        self.assertTrue(all(rows == [] for rows in exported['data'].values()))
        self.assertEqual(exported['plan_reviews'], [])
        plan = self.plan()
        _, exported = self.export()
        self.assertEqual(exported['plan_reviews'][0]['plan_id'], plan)
        self.assertTrue(all(Decimal(str(v)) == 0 for v in exported['plan_reviews'][0]['metrics'].values()))

    def test_metadata_allowlist_no_internal_settings_and_button(self):
        self.plan()
        response, exported = self.export()
        self.assertEqual(exported['schema_version'], '4')
        self.assertEqual(exported['export_format_version'], '3')
        self.assertEqual(exported['exported_at'], exported['aggregation_as_of'])
        self.assertEqual(exported['plan_reviews'][0]['calculated_at'], exported['aggregation_as_of'])
        self.assertEqual(exported['timezone']['display_and_date_boundary'], 'Asia/Seoul')
        self.assertEqual(set(exported['data']), set(EXPORT_FIELDS))
        for table, rows in exported['data'].items():
            for row in rows:
                self.assertEqual(set(row), set(EXPORT_FIELDS[table]))
        text = response.data.decode('utf-8')
        for forbidden in ('secret-config-sentinel', 'environment-sentinel', str(self.path), 'DATABASE', 'SECRET_KEY', 'PDS_DB_PATH', 'database_path'):
            self.assertNotIn(forbidden, text)
        self.assertIn('전체 자료 내보내기'.encode(), self.client.get('/').data)
        self.assertIn(b'href="/export.json"', self.client.get('/review').data)
        contract = json.loads((ROOT / 'contracts/pds-schema-v4.json').read_text(encoding='utf-8'))
        self.assertEqual(contract['export_format']['data_fields'], {t: list(c) for t, c in EXPORT_FIELDS.items()})


if __name__ == '__main__':
    unittest.main(verbosity=2)
