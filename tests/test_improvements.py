from contextlib import closing
import sqlite3
import unittest
from uuid import uuid4

from app import ROOT, create_app
from auth_support import authenticated_client


class ImprovementTests(unittest.TestCase):
    def setUp(self):
        self.path = ROOT / 'instance/tests' / f'{uuid4()}.sqlite3'
        self.app = create_app(self.path)
        self.app.config['TESTING'] = True
        self.client = authenticated_client(self.app)
        self.plan_data = dict(title='[검증 전용] 이전 계획', content='원래 내용', start_date='2026-09-01',
            end_date='2026-09-30', priority='high', success_criteria='원래 기준', estimated_minutes='60')
        response = self.client.post('/plans/new', data=self.plan_data)
        self.previous = response.headers['Location'].split('/')[-1]

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def rows(self, table):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute('SELECT * FROM ' + table)]

    def save_improvement(self):
        content = '[검증 전용] 다음에는 작은 단위로 나누기\n<script>alert(1)</script>'
        result = self.client.post('/review', data={'plan_id': self.previous, 'improvement_content': content})
        self.assertEqual(result.status_code, 303)
        return self.rows('review_improvements')[0]

    def test_save_reload_prefill_and_get_does_not_create(self):
        before = self.rows('plans'), self.rows('plan_versions')
        improvement = self.save_improvement()
        self.assertEqual(improvement['plan_id'], self.previous)
        fresh = authenticated_client(create_app(self.path))
        page = fresh.get('/review', query_string={'plan_id': self.previous}).get_data(as_text=True)
        self.assertIn('작은 단위로 나누기', page)
        self.assertIn('&lt;script&gt;', page)
        self.assertNotIn('<script>alert(1)</script>', page)
        response = fresh.get('/plans/new', query_string={'source_improvement': improvement['id']})
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('작은 단위로 나누기', html)
        self.assertIn('name="source_improvement" value="' + improvement['id'], html)
        for field in ('title', 'start_date', 'end_date'):
            self.assertIn('name="' + field + '" value=""', html)
        self.assertEqual((self.rows('plans'), self.rows('plan_versions')), before)
        self.assertEqual(self.rows('next_plan_links'), [])

    def test_next_plan_link_persists_and_original_is_unchanged(self):
        before = self.rows('plans')[0], self.rows('plan_versions')[0]
        improvement = self.save_improvement()
        data = dict(self.plan_data, title='[검증 전용] 다음 계획', content=improvement['content'], source_improvement=improvement['id'])
        response = self.client.post('/plans/new', data=data)
        self.assertEqual(response.status_code, 303)
        next_id = response.headers['Location'].split('/')[-1]
        link = self.rows('next_plan_links')[0]
        self.assertEqual((link['previous_plan_id'], link['improvement_id'], link['next_plan_id']),
                         (self.previous, improvement['id'], next_id))
        fresh = authenticated_client(create_app(self.path))
        page = fresh.get('/review', query_string={'plan_id': self.previous}).get_data(as_text=True)
        self.assertIn('href="/plans/' + next_id + '"', page)
        self.assertIn('[검증 전용] 다음 계획', page)
        self.assertEqual(fresh.get('/plans/' + next_id).status_code, 200)
        self.assertEqual(next(r for r in self.rows('plans') if r['id'] == self.previous), before[0])
        self.assertEqual(next(r for r in self.rows('plan_versions') if r['plan_id'] == self.previous), before[1])
        self.assertEqual(self.client.post('/plans/' + next_id + '/edit', data=dict(data, version=1, title='제목 수정')).status_code, 303)
        self.assertEqual(self.rows('next_plan_links')[0], link)

    def test_invalid_improvement_and_plan_validation_preserve_source(self):
        self.assertEqual(self.client.post('/review', data={'plan_id': self.previous, 'improvement_content': ' \n '}).status_code, 400)
        self.assertEqual(self.client.post('/review', data={'plan_id': 'missing', 'improvement_content': '검증'}).status_code, 404)
        self.assertEqual(self.rows('review_improvements'), [])
        self.assertEqual(self.client.get('/plans/new?source_improvement=missing').status_code, 404)
        self.assertEqual(self.client.post('/plans/new', data=dict(self.plan_data, source_improvement='missing')).status_code, 404)
        improvement = self.save_improvement()
        response = self.client.post('/plans/new', data=dict(self.plan_data, title='', source_improvement=improvement['id'], content='사용자가 바꾼 내용'))
        self.assertEqual(response.status_code, 400)
        self.assertIn(improvement['id'].encode(), response.data)
        self.assertIn('사용자가 바꾼 내용'.encode(), response.data)
        self.assertEqual(len(self.rows('plans')), 1)
        self.assertEqual(self.rows('next_plan_links'), [])

    def test_link_failure_rolls_back_plan_and_initial_history(self):
        improvement = self.save_improvement()
        before = self.rows('plans'), self.rows('plan_versions')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TRIGGER reject_link BEFORE INSERT ON next_plan_links BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.client.post('/plans/new', data=dict(self.plan_data, source_improvement=improvement['id']))
        self.assertEqual((self.rows('plans'), self.rows('plan_versions')), before)
        self.assertEqual(self.rows('next_plan_links'), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
