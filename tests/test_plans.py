import json
from contextlib import closing
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen as raw_urlopen, Request
from uuid import uuid4

from app import ROOT, create_app
from auth_support import authenticated_client


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.path = ROOT / 'instance' / 'tests' / f'{uuid4()}.sqlite3'
        self.app = create_app(self.path)
        self.app.config['TESTING'] = True
        self.client = authenticated_client(self.app)
        self.data = dict(title='[자동 검증 전용] 최초 계획', content='테스트 내용', start_date='2026-09-16',
                         end_date='2026-09-17', priority='high', success_criteria='검증 전용', estimated_minutes='30')

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def rows(self, table):
        # table names are test constants, not user input.
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute('SELECT * FROM ' + table)]

    def create(self):
        response = self.client.post('/plans/new', data=self.data)
        self.assertEqual(response.status_code, 303)
        return response.headers['Location']

    def test_create_refresh_and_history(self):
        url = self.create()
        original = self.rows('plans')[0]
        self.assertEqual(original['title'], self.data['title'])
        self.assertEqual(original['estimated_minutes'], 30)
        self.assertEqual(len(self.rows('plan_versions')), 1)
        first_page = self.client.get(url).data
        self.assertEqual(first_page, authenticated_client(self.app).get(url).data)
        self.assertEqual(original, self.rows('plans')[0])
        self.assertIn(original['id'].encode(), first_page)
        changed = dict(self.data, title='[자동 검증 전용] 수정 계획', estimated_minutes='45', version='1')
        self.assertEqual(self.client.post(url + '/edit', data=changed).status_code, 303)
        current = self.rows('plans')[0]
        versions = sorted(self.rows('plan_versions'), key=lambda row: row['version'])
        self.assertEqual(current['id'], original['id'])
        self.assertEqual(current['created_at'], original['created_at'])
        self.assertEqual(current['version'], 2)
        self.assertEqual([v['title'] for v in versions], [self.data['title'], changed['title']])
        self.assertEqual([v['estimated_minutes'] for v in versions], [30, 45])
        for v in versions:
            self.assertIn(v['title'].encode(), self.client.get(url).data)
        self.assertEqual(self.client.post(url + '/edit', data=changed).status_code, 409)
        self.assertEqual(len(self.rows('plan_versions')), 2)

    def test_invalid_inputs_create_and_edit(self):
        url = self.create()
        original = self.rows('plans')
        for invalid in [dict(title='  '), dict(start_date='2026-02-30'), dict(start_date='20260916'),
                        dict(end_date='2026-09-15'), dict(end_date='no-date'), dict(estimated_minutes='-1'),
                        dict(estimated_minutes='1.5'), dict(estimated_minutes=''),
                        dict(estimated_minutes='99999999999999999'), dict(priority='invalid')]:
            with self.subTest(invalid=invalid):
                data = dict(self.data, **invalid)
                self.assertEqual(self.client.post('/plans/new', data=data).status_code, 400)
                self.assertEqual(self.client.post(url + '/edit', data=dict(data, version='1')).status_code, 400)
                self.assertEqual(self.rows('plans'), original)
                self.assertEqual(len(self.rows('plan_versions')), 1)
        self.assertEqual(self.client.post('/plans/new', data=dict(self.data, estimated_minutes='0', end_date='2026-09-16')).status_code, 303)

    def test_history_failure_rolls_back(self):
        url = self.create()
        original = self.rows('plans')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TRIGGER reject_history BEFORE INSERT ON plan_versions BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.client.post(url + '/edit', data=dict(self.data, title='rollback', version='1'))
        self.assertEqual(self.rows('plans'), original)
        self.assertEqual(len(self.rows('plan_versions')), 1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.client.post('/plans/new', data=self.data)
        self.assertEqual(self.rows('plans'), original)

    def test_escaping_and_parameterized_sql(self):
        payload = '<script>alert(1)</script><img src=x onerror=alert(2)>\' ; DROP TABLE plans; --'
        self.data.update(title=payload, content=payload, success_criteria=payload)
        url = self.create()
        for page in ['/', url, url + '/edit']:
            html = self.client.get(page).get_data(as_text=True)
            self.assertNotIn('<script>alert(1)</script>', html)
            self.assertNotIn('<img src=x', html)
            self.assertIn('&lt;script&gt;', html)
        self.assertEqual(self.rows('plans')[0]['title'], payload)

    def test_timezone_and_contract(self):
        self.assertEqual(self.app.jinja_env.filters['seoul']('2026-09-16T18:30:00.000000Z'), '2026-09-17 03:30:00')
        contract = json.loads((ROOT / 'contracts/pds-schema-v4.json').read_text(encoding='utf-8'))
        with closing(sqlite3.connect(self.path)) as db:
            for table, spec in contract['tables'].items():
                actual = {row[1]: row[2] for row in db.execute('PRAGMA table_info(' + table + ')')}
                self.assertEqual(actual, {name: field['type'] for name, field in spec['fields'].items()})

    def test_real_http_server_restart(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        from auth_support import csrf
        from auth import COOKIE
        token = csrf(self.client)
        cookie = self.client.get_cookie(COOKIE).value
        def urlopen(url, data=None, timeout=5):
            if data is not None:
                data += ('&csrf_token=' + token).encode()
            return raw_urlopen(Request(url, data=data, headers={'Cookie': COOKIE + '=' + cookie}), timeout=timeout)

        env = dict(os.environ, PDS_DB_PATH=str(self.path), PDS_PORT=str(port))

        def start():
            process = subprocess.Popen([sys.executable, str(ROOT / 'app.py')], cwd=ROOT, env=env,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                for _ in range(100):
                    if process.poll() is not None:
                        self.fail('Server exited before becoming ready')
                    try:
                        with urlopen(base, timeout=1) as response:
                            self.assertEqual(response.status, 200)
                            return process
                    except (URLError, TimeoutError):
                        time.sleep(.05)
                self.fail('Server did not become ready')
            except BaseException:
                process.terminate()
                process.wait(timeout=5)
                raise

        process = start()
        try:
            with urlopen(base + '/plans/new', data=urlencode(self.data).encode(), timeout=5) as response:
                url = response.url
                self.assertEqual(response.status, 200)
            original = self.rows('plans')[0]
            with urlopen(url, timeout=5) as response:
                self.assertIn(original['id'].encode(), response.read())
            process.terminate()
            process.wait(timeout=5)
            process = start()
            with urlopen(url, timeout=5) as response:
                html = response.read().decode()
                self.assertIn(original['id'], html)
                self.assertIn(original['title'], html)
            self.assertEqual(self.rows('plans')[0], original)
            self.assertEqual(len(self.rows('plan_versions')), 1)
            with self.assertRaises(HTTPError) as error:
                urlopen(base + '/plans/new', data=urlencode(dict(self.data, estimated_minutes='-1')).encode(), timeout=5)
            self.assertEqual(error.exception.code, 400)
            error.exception.close()
        finally:
            process.terminate()
            process.wait(timeout=5)


if __name__ == '__main__':
    unittest.main(verbosity=2)
