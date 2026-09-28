from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import re
import sqlite3
import threading
import unittest
from uuid import uuid4

from app import ROOT, create_app
from auth_support import authenticated_client


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.path = ROOT / 'instance/tests' / f'{uuid4()}.sqlite3'
        self.app = create_app(self.path)
        self.app.config['TESTING'] = True
        self.client = authenticated_client(self.app)
        response = self.client.post('/plans/new', data=dict(title='[검증 전용] 계획', content='',
            start_date='2026-09-16', end_date='2026-09-30', priority='medium', success_criteria='', estimated_minutes='0'))
        self.plan_id = response.headers['Location'].split('/')[-1]
        self.data = dict(plan_id=self.plan_id, title='[검증 전용] 할 일', content='검색할 내용', due_date='2026-09-20',
                         priority='high', estimated_minutes='10', tags='검증, 공통, 검증')

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def rows(self, table):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute('SELECT * FROM ' + table)]

    def add(self, **changes):
        before = {r['id'] for r in self.rows('tasks')}
        self.assertEqual(self.client.post('/tasks/new', data=dict(self.data, **changes)).status_code, 303)
        return next(r for r in self.rows('tasks') if r['id'] not in before)

    def state(self, task, status, key=None, client=None):
        return (client or self.client).post(f"/tasks/{task['id']}/state", data={
            'target_status': status, 'version': task['version'], 'request_id': key or str(uuid4())})

    def listing(self, **query):
        response = self.client.get('/tasks', query_string=dict(plan_id=self.plan_id, **query))
        self.assertEqual(response.status_code, 200)
        return re.findall(r'data-task-id="([^"]+)"', response.get_data(as_text=True))

    def test_crud_refresh_and_delete_confirmation(self):
        task = self.add()
        self.assertEqual(len(self.rows('task_tags')), 2)
        fresh = authenticated_client(create_app(self.path))
        self.assertIn(task['id'].encode(), fresh.get('/tasks', query_string={'plan_id': self.plan_id}).data)
        self.assertEqual(self.rows('tasks')[0], task)
        url = f"/tasks/{task['id']}"
        self.assertEqual(self.client.post(url + '/edit', data=dict(self.data, title='수정 내용', tags='새 태그', version=1)).status_code, 303)
        task = self.rows('tasks')[0]
        self.assertEqual(task['title'], '수정 내용')
        self.assertEqual([r['tag'] for r in self.rows('task_tags')], ['새 태그'])
        self.assertEqual(self.state(task, 'completed').status_code, 303)
        task = self.rows('tasks')[0]
        self.assertIsNotNone(task['completed_at'])
        self.assertEqual(self.state(task, 'active').status_code, 303)
        task = self.rows('tasks')[0]
        self.assertIsNone(task['completed_at'])
        self.assertEqual(self.client.get(url + '/delete').status_code, 200)
        self.assertIsNone(self.rows('tasks')[0]['deleted_at'])
        self.assertEqual(self.client.post(url + '/delete', data={'version': task['version']}).status_code, 400)
        self.assertEqual(self.client.post(url + '/delete', data={'version': task['version'], 'confirm': 'yes'}).status_code, 303)
        self.assertEqual(self.listing(), [])
        self.assertEqual(self.rows('plans')[0]['id'], self.plan_id)
        self.assertEqual(len(self.rows('plan_versions')), 1)
        self.assertEqual(len(self.rows('task_completion_events')), 1)
        self.assertEqual(self.client.get(url + '/edit').status_code, 404)
        self.assertEqual(self.state(self.rows('tasks')[0], 'completed').status_code, 404)

    def test_search_combined_filters_and_stable_orders(self):
        a = self.add(title='검색 A', priority='low', due_date='')
        b = self.add(title='검색 B', due_date='2026-09-19')
        c = self.add(title='검색 C', due_date='2026-09-19')
        d = self.add(title='다른 항목', content='별도', priority='medium', due_date='2026-09-21', tags='다른 태그')
        e = self.add(title='검색 완료', due_date='', tags='공통')
        self.state(e, 'completed')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE tasks SET created_at = '2026-09-16T00:00:00.000000Z'")
        self.assertEqual(self.listing(), sorted([b['id'], c['id']]) + [d['id']] + sorted([a['id'], e['id']]))
        self.assertEqual(self.listing(sort='priority'), sorted([b['id'], c['id'], e['id']]) + [d['id'], a['id']])
        self.assertEqual(self.listing(sort='newest'), sorted(t['id'] for t in (a, b, c, d, e)))
        self.assertEqual(self.listing(q='검색', status='active', priority='high', tag='공통'), sorted([b['id'], c['id']]))
        self.assertEqual(self.listing(q='별도'), [d['id']])
        self.assertEqual(self.listing(q='%'), [])
        self.assertEqual(self.listing(q='검색', status='completed', tag='공통'), [e['id']])
        other = self.client.post('/plans/new', data=dict(title='다른 계획', content='', start_date='2026-09-16',
            end_date='2026-09-30', priority='low', success_criteria='', estimated_minutes='0')).headers['Location'].split('/')[-1]
        foreign = self.add(plan_id=other)
        self.assertNotIn(foreign['id'], self.listing())

    def test_retry_cancel_recomplete_and_stale_request(self):
        task = self.add()
        key = str(uuid4())
        self.assertEqual(self.state(task, 'completed', key).status_code, 303)
        self.assertEqual(self.state(task, 'completed', key).status_code, 303)
        completed = self.rows('tasks')[0]
        self.assertEqual(self.state(completed, 'completed').status_code, 303)
        self.assertEqual(len(self.rows('task_completion_events')), 1)
        self.assertEqual(self.rows('tasks')[0], completed)
        self.assertEqual(self.state(completed, 'active').status_code, 303)
        active = self.rows('tasks')[0]
        self.assertEqual(self.state(task, 'completed', key).status_code, 303)
        self.assertEqual(self.rows('tasks')[0], active)
        self.assertEqual(self.state(task, 'completed').status_code, 409)
        self.assertEqual(self.state(active, 'completed', key).status_code, 409)
        self.assertEqual(self.state(active, 'completed').status_code, 303)
        self.assertEqual(len(self.rows('task_completion_events')), 2)

    def test_concurrent_completion_same_and_different_keys(self):
        for shared_key in (True, False):
            task = self.add()
            barrier = threading.Barrier(6)
            key = str(uuid4())
            def worker(_):
                client = authenticated_client(self.app)
                barrier.wait(timeout=10)
                return self.state(task, 'completed', key if shared_key else str(uuid4()), client).status_code
            with ThreadPoolExecutor(max_workers=6) as pool:
                codes = list(pool.map(worker, range(6)))
            self.assertEqual(codes.count(303), 6 if shared_key else 1)
            self.assertEqual(codes.count(409), 0 if shared_key else 5)
            events = [r for r in self.rows('task_completion_events') if r['task_id'] == task['id']]
            self.assertEqual(len(events), 1)

    def test_validation_and_html_safety(self):
        for invalid in [dict(title=' '), dict(estimated_minutes='-1'), dict(estimated_minutes='1.5'),
                        dict(plan_id='missing'), dict(status='completed'), dict(status='invalid'),
                        dict(priority='bad'), dict(due_date='2026-02-30')]:
            with self.subTest(invalid=invalid):
                self.assertEqual(self.client.post('/tasks/new', data=dict(self.data, **invalid)).status_code, 400)
        self.assertEqual(self.rows('tasks'), [])
        payload = "<script>alert(1)</script>'; DROP TABLE tasks; --"
        task = self.add(title=payload, content=payload, tags=payload)
        html = self.client.get('/tasks', query_string={'plan_id': self.plan_id}).get_data(as_text=True)
        self.assertNotIn('<script>alert(1)</script>', html)
        self.assertIn('&lt;script&gt;', html)
        url = f"/tasks/{task['id']}"
        self.assertEqual(self.client.post(url + '/edit', data=dict(self.data, version=1, estimated_minutes='-1')).status_code, 400)
        self.assertEqual(self.client.post(url + '/edit', data=dict(self.data, version=99)).status_code, 409)
        self.assertEqual(self.state(task, 'invalid').status_code, 400)
        self.assertEqual(self.state(task, 'completed', 'invalid').status_code, 400)
        self.assertEqual(self.rows('tasks')[0], task)
        self.assertEqual(self.client.get('/tasks?sort=invalid').status_code, 400)

    def test_db_constraints_and_completion_rollback(self):
        task = self.add()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TRIGGER reject_completion BEFORE INSERT ON task_completion_events BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.state(task, 'completed')
        self.assertEqual(self.rows('tasks')[0], task)
        self.assertEqual(self.rows('task_state_requests'), [])
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DROP TRIGGER reject_completion')
        self.state(task, 'completed')
        event = self.rows('task_completion_events')[0]
        with closing(sqlite3.connect(self.path)) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute('INSERT INTO task_completion_events VALUES (?, ?, ?, ?, ?)',
                           (str(uuid4()), event['task_id'], event['request_id'], event['task_version'], event['completed_at']))

    def test_existing_plan_migration_preserves_rows(self):
        before = self.rows('plans'), self.rows('plan_versions')
        create_app(self.path)
        self.assertEqual((self.rows('plans'), self.rows('plan_versions')), before)
        self.assertEqual(self.rows('tasks'), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
