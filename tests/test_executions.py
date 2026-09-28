from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from unittest.mock import patch
import sqlite3
import threading
import unittest
from uuid import uuid4

from app import ROOT, create_app
from auth_support import authenticated_client


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        clock = patch('executions.server_now', return_value=datetime(2026, 9, 18, tzinfo=timezone.utc))
        clock.start()
        self.addCleanup(clock.stop)
        self.path = ROOT / 'instance/tests' / f'{uuid4()}.sqlite3'
        self.app = create_app(self.path)
        self.app.config['TESTING'] = True
        self.client = authenticated_client(self.app)
        response = self.client.post('/plans/new', data=dict(title='[실행 검증 전용] 계획', content='',
            start_date='2026-09-16', end_date='2026-09-30', priority='medium', success_criteria='', estimated_minutes='100'))
        self.plan = response.headers['Location'].split('/')[-1]
        self.client.post('/tasks/new', data=dict(plan_id=self.plan, title='[검증 전용] 할 일', content='',
            due_date='', priority='high', estimated_minutes='50', tags=''))
        self.task = self.rows('tasks')[0]['id']
        self.data = dict(plan_id=self.plan, task_id=self.task, request_id=str(uuid4()),
            started_at='2026-09-16T23:50:00', ended_at='2026-09-17T00:20:30', content='검증용 수행 내용', blocked_reason='')

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def rows(self, table):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute('SELECT * FROM ' + table)]

    def test_save_calculation_reload_multiple_completed_and_preservation(self):
        self.client.post(f'/tasks/{self.task}/state', data=dict(target_status='completed', version=1, request_id=str(uuid4())))
        tables = ('plans', 'plan_versions', 'tasks', 'task_tags', 'task_completion_events', 'task_state_requests')
        before = {t: self.rows(t) for t in tables}
        self.assertEqual(self.client.post('/executions', data=dict(self.data, actual_minutes='9999')).status_code, 303)
        record = self.rows('execution_records')[0]
        self.assertEqual(record['actual_minutes'], 30.5)
        self.assertEqual(record['started_at'], '2026-09-16T14:50:00.000000Z')
        self.assertEqual(record['ended_at'], '2026-09-16T15:20:30.000000Z')
        fresh = authenticated_client(create_app(self.path))
        html = fresh.get('/executions', query_string={'plan_id': self.plan}).get_data(as_text=True)
        self.assertIn('[검증 전용] 할 일', html)
        self.assertIn('2026-09-17 00:20:30', html)
        self.assertIn('검증용 수행 내용', html)
        self.assertEqual(self.rows('execution_records')[0], record)
        self.assertEqual(self.client.post('/executions', data=dict(self.data, request_id=str(uuid4()))).status_code, 303)
        self.assertEqual(len(self.rows('execution_records')), 2)
        self.assertEqual({t: self.rows(t) for t in tables}, before)

    def test_time_rounding_and_zero(self):
        for seconds, expected in [('00', 0), ('01', .02), ('02', .03), ('59', .98)]:
            data = dict(self.data, request_id=str(uuid4()), started_at='2026-09-16T00:00', ended_at='2026-09-16T00:00:' + seconds)
            self.assertEqual(self.client.post('/executions', data=data).status_code, 303)
            record = next(r for r in self.rows('execution_records') if r['request_id'] == data['request_id'])
            self.assertEqual(record['actual_minutes'], expected)

    def test_invalid_inputs_plan_mismatch_and_deleted_task(self):
        invalid = [dict(started_at=''), dict(ended_at='2026-02-30T12:00'), dict(ended_at='2026-09-16T01:00'),
            dict(started_at='2026-09-16T23:50:00Z'), dict(started_at='2026-09-16T25:00'),
            dict(started_at='2026-09-16T23:50:00.123'), dict(plan_id='missing'), dict(task_id='missing'),
            dict(content=' '), dict(request_id='bad')]
        for fields in invalid:
            with self.subTest(fields=fields):
                self.assertEqual(self.client.post('/executions', data=dict(self.data, **fields)).status_code, 400)
        response = self.client.post('/plans/new', data=dict(title='다른 검증 계획', content='', start_date='2026-09-16',
            end_date='2026-09-30', priority='low', success_criteria='', estimated_minutes='0'))
        other_plan = response.headers['Location'].split('/')[-1]
        self.assertEqual(self.client.post('/executions', data=dict(self.data, plan_id=other_plan)).status_code, 400)
        self.client.post(f'/tasks/{self.task}/delete', data=dict(version=1, confirm='yes'))
        self.assertEqual(self.client.post('/executions', data=self.data).status_code, 400)
        self.assertEqual(self.rows('execution_records'), [])

    def test_duplicate_sequential_concurrent_and_conflict(self):
        barrier = threading.Barrier(6)
        def send(_):
            client = authenticated_client(self.app)
            barrier.wait(timeout=10)
            return client.post('/executions', data=self.data).status_code
        with ThreadPoolExecutor(max_workers=6) as pool:
            self.assertEqual(list(pool.map(send, range(6))), [303] * 6)
        self.assertEqual(self.client.post('/executions', data=self.data).status_code, 303)
        self.assertEqual(len(self.rows('execution_records')), 1)
        self.assertEqual(self.client.post('/executions', data=dict(self.data, content='다른 요청')).status_code, 409)
        self.assertEqual(len(self.rows('execution_records')), 1)
        row = self.rows('execution_records')[0]
        with closing(sqlite3.connect(self.path)) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute('''INSERT INTO execution_records(id, task_id, request_id, started_at, ended_at,
                    actual_minutes, content, blocked_reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    (str(uuid4()), row['task_id'], row['request_id'], row['started_at'], row['ended_at'],
                     row['actual_minutes'], row['content'], row['blocked_reason'], row['created_at']))

    def test_escaped_content_and_empty_input_form(self):
        html = self.client.get('/executions', query_string={'plan_id': self.plan}).get_data(as_text=True)
        self.assertIn('name="started_at" id="started_at" value=""', html)
        payload = '<script>alert(1)</script>'
        self.assertEqual(self.client.post('/executions', data=dict(self.data, content=payload, blocked_reason=payload)).status_code, 303)
        html = self.client.get('/executions', query_string={'plan_id': self.plan}).get_data(as_text=True)
        self.assertNotIn(payload, html)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', html)

    def test_edit_record_recalculate_move_and_preserve_id(self):
        self.client.post('/executions', data=self.data)
        original = self.rows('execution_records')[0]
        url = f"/executions/{original['id']}/edit"
        html = self.client.get(url).get_data(as_text=True)
        self.assertIn('2026-09-16T23:50:00', html)
        self.client.post('/tasks/new', data=dict(plan_id=self.plan, title='새 연결 할 일', content='',
            due_date='', priority='low', estimated_minutes='70', tags=''))
        target = next(r['id'] for r in self.rows('tasks') if r['id'] != self.task)
        before = self.rows('plans'), self.rows('plan_versions'), self.rows('tasks')
        response = self.client.post(url, data=dict(self.data, task_id=target, ended_at='2026-09-17T00:50:30',
            content='수정한 수행 내용', blocked_reason='수정한 이유', actual_minutes='9999'))
        self.assertEqual(response.status_code, 303)
        updated = self.rows('execution_records')[0]
        self.assertEqual(len(self.rows('execution_records')), 1)
        self.assertEqual(updated['id'], original['id'])
        self.assertEqual(updated['created_at'], original['created_at'])
        self.assertEqual(updated['request_id'], original['request_id'])
        self.assertEqual(updated['task_id'], target)
        self.assertEqual(updated['actual_minutes'], 60.5)
        self.assertEqual(updated['content'], '수정한 수행 내용')
        self.assertEqual(updated['blocked_reason'], '수정한 이유')
        self.assertEqual((self.rows('plans'), self.rows('plan_versions'), self.rows('tasks')), before)
        self.assertIn('수정한 수행 내용'.encode(), authenticated_client(create_app(self.path)).get('/executions?plan_id=' + self.plan).data)
        self.assertEqual(self.client.post('/executions', data=self.data).status_code, 409)
        self.assertEqual(self.rows('execution_records')[0], updated)

    def test_future_end_rejected_on_create_and_edit(self):
        current = datetime(2026, 9, 16, 15, 20, 30, tzinfo=timezone.utc)
        with patch('executions.server_now', return_value=current):
            future = dict(self.data, ended_at='2026-09-17T00:20:31')
            self.assertEqual(self.client.post('/executions', data=future).status_code, 400)
            self.assertEqual(self.rows('execution_records'), [])
            self.assertEqual(self.client.post('/executions', data=self.data).status_code, 303)
            row = self.rows('execution_records')[0]
            url = f"/executions/{row['id']}/edit"
            for values in [future, dict(self.data, ended_at='2026-09-16T23:49'),
                           dict(self.data, task_id='missing'), dict(self.data, content=' '),
                           dict(self.data, started_at='bad')]:
                self.assertEqual(self.client.post(url, data=values).status_code, 400)
                self.assertEqual(self.rows('execution_records')[0], row)
            self.assertEqual(self.client.post(url, data=self.data).status_code, 303)


if __name__ == '__main__':
    unittest.main(verbosity=2)
