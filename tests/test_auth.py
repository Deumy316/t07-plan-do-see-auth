from contextlib import closing
import json
import secrets
import sqlite3
import time
import unittest
from uuid import uuid4

from flask import template_rendered

from app import ROOT, create_app
from auth import COOKIE, SESSION_SECONDS, digest
from auth_support import csrf
from export_data import EXPORT_FIELDS


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.path = ROOT / 'instance/tests' / (uuid4().hex + '.sqlite3')
        self.app = create_app(self.path)
        self.app.config['TESTING'] = True
        self.password = secrets.token_urlsafe(24)
        self.a = self.account('account_a')
        self.b = self.account('account_b')

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def account(self, name):
        client = self.app.test_client()
        token = csrf(client, '/signup')
        self.assertEqual(client.post('/signup', data=dict(username=name, password=self.password, csrf_token=token)).status_code, 303)
        self.assertEqual(client.post('/login', data=dict(username=name, password=self.password, csrf_token=token)).status_code, 303)
        return client

    def post(self, client, path, data=None):
        return client.post(path, data=dict(data or {}, csrf_token=csrf(client)))

    def rows(self, table):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute('SELECT * FROM ' + table + ' ORDER BY 1,2')]

    def snapshot(self):
        # Includes credentials/session rows without writing their values to logs.
        with closing(sqlite3.connect(self.path)) as db:
            return {r[0]: db.execute('SELECT * FROM "' + r[0] + '" ORDER BY 1,2').fetchall()
                    for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    def plan_data(self, **extra):
        return dict(title='검증 계획', content='검증 본문', start_date='2026-09-01', end_date='2026-09-30',
                    priority='medium', success_criteria='확인', estimated_minutes='10', **extra)

    def seed(self, client, marker):
        data = self.plan_data()
        data['title'] = marker
        response = self.post(client, '/plans/new', data)
        self.assertEqual(response.status_code, 303)
        plan = response.location.split('/')[-1]
        task_data = dict(plan_id=plan, title=marker, content=marker, due_date='2026-09-01',
                         priority='high', estimated_minutes='10', tags=marker)
        self.assertEqual(self.post(client, '/tasks/new', task_data).status_code, 303)
        task = next(r['id'] for r in self.rows('tasks') if r['plan_id'] == plan)
        key = str(uuid4())
        record_data = dict(plan_id=plan, task_id=task, request_id=key, started_at='2026-09-01T09:00',
                           ended_at='2026-09-01T09:10', content=marker, blocked_reason=marker)
        self.assertEqual(self.post(client, '/executions', record_data).status_code, 303)
        record = next(r['id'] for r in self.rows('execution_records') if r['task_id'] == task)
        self.assertEqual(self.post(client, '/review', dict(plan_id=plan, improvement_content=marker)).status_code, 303)
        improvement = next(r['id'] for r in self.rows('review_improvements') if r['plan_id'] == plan)
        response = self.post(client, '/plans/new', self.plan_data(source_improvement=improvement))
        self.assertEqual(response.status_code, 303)
        state_key = str(uuid4())
        self.assertEqual(self.post(client, '/tasks/' + task + '/state', dict(version=1, target_status='completed', request_id=state_key)).status_code, 303)
        return dict(plan=plan, task=task, record=record, improvement=improvement, task_data=task_data,
                    record_data=record_data, state_key=state_key, following=response.location.split('/')[-1])

    def test_password_hash_salts_unique_username_and_generic_error(self):
        users = self.rows('users')
        self.assertTrue(all(u['password_hash'].startswith('scrypt:32768:8:1$') for u in users))
        self.assertTrue(users[0]['password_hash'] != users[1]['password_hash'])
        self.assertTrue(self.password not in json.dumps(self.snapshot()))
        with closing(sqlite3.connect(self.path)) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute('INSERT INTO users VALUES (?, ?, ?, ?)', (str(uuid4()), 'ACCOUNT_A', users[0]['password_hash'], 0))
        client = self.app.test_client()
        token = csrf(client, '/signup')
        duplicate = client.post('/signup', data=dict(username='ACCOUNT_A', password=self.password, csrf_token=token))
        self.assertEqual(duplicate.status_code, 400)
        replies = [client.post('/login', data=dict(username=name, password=secrets.token_urlsafe(24), csrf_token=token))
                   for name in ('missing_user', 'account_a')]
        self.assertEqual([r.status_code for r in replies], [400, 400])
        self.assertTrue(replies[0].data == replies[1].data)
        for response in [duplicate, *replies]:
            self.assertTrue(self.password.encode() not in response.data)

    def test_anonymous_access_and_post_only_actions(self):
        client = self.app.test_client()
        for path in ('/', '/plans/new', '/plans/unknown', '/tasks', '/executions', '/review', '/export.json', '/account/password'):
            for method in ('GET', 'POST'):
                with self.subTest(path=path, method=method):
                    response = client.open(path, method=method)
                    self.assertEqual(response.status_code, 303)
                    self.assertEqual(response.location, '/login')
        before = self.snapshot()
        self.assertEqual(self.a.get('/logout').status_code, 405)
        self.assertEqual(self.a.get('/account/password').status_code, 200)
        self.assertTrue(before == self.snapshot())

    def test_session_rotation_storage_expiry_and_cookie_flags(self):
        client = self.app.test_client()
        token = csrf(client, '/login')
        old = client.get_cookie(COOKIE).value
        response = client.post('/login', data=dict(username='account_a', password=self.password, csrf_token=token))
        raw = client.get_cookie(COOKIE).value
        self.assertTrue(raw != old)
        sessions = self.rows('auth_sessions')
        self.assertTrue(all(row['token_hash'] != raw for row in sessions))
        self.assertTrue(all(row['token_hash'] != digest(old) for row in sessions))
        row = next(r for r in sessions if r['token_hash'] == digest(raw))
        self.assertEqual(row['expires_at'] - row['created_at'], SESSION_SECONDS)
        cookie = response.headers['Set-Cookie']
        self.assertTrue('HttpOnly' in cookie and 'SameSite=Lax' in cookie and 'Max-Age=28800' in cookie)
        replay = self.app.test_client()
        replay.set_cookie(COOKIE, old)
        self.assertEqual(replay.get('/').status_code, 303)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('UPDATE auth_sessions SET expires_at=? WHERE token_hash=?', (int(time.time()) - 1, digest(raw)))
        self.assertEqual(client.get('/').status_code, 303)
        self.app.config['SESSION_COOKIE_SECURE'] = True
        secure = self.app.test_client().get('/login', base_url='https://localhost')
        self.assertTrue('Secure' in secure.headers['Set-Cookie'])

    def test_logout_rejects_copied_cookie_and_password_change_revokes_all(self):
        raw = self.a.get_cookie(COOKIE).value
        self.assertEqual(self.post(self.a, '/logout').status_code, 303)
        replay = self.app.test_client()
        replay.set_cookie(COOKIE, raw)
        self.assertEqual(replay.get('/export.json').status_code, 303)
        clients = []
        for _ in range(2):
            client = self.app.test_client()
            token = csrf(client, '/login')
            self.assertEqual(client.post('/login', data=dict(username='account_a', password=self.password, csrf_token=token)).status_code, 303)
            clients.append(client)
        new = secrets.token_urlsafe(24)
        copied = clients[0].get_cookie(COOKIE).value
        self.assertEqual(self.post(clients[0], '/account/password', dict(current_password=self.password, password=new)).status_code, 303)
        clients[0].set_cookie(COOKIE, copied)
        for client in clients:
            self.assertEqual(client.get('/').status_code, 303)
        self.assertEqual(self.b.get('/').status_code, 200)
        client = self.app.test_client()
        token = csrf(client, '/login')
        self.assertEqual(client.post('/login', data=dict(username='account_a', password=self.password, csrf_token=token)).status_code, 400)
        self.assertEqual(client.post('/login', data=dict(username='account_a', password=new, csrf_token=token)).status_code, 303)

    def test_csrf_missing_wrong_other_session_and_unicode_are_rejected(self):
        owned = self.seed(self.a, 'csrf-check')
        paths = ['/plans/new', '/plans/' + owned['plan'] + '/edit', '/tasks/new',
                 '/tasks/' + owned['task'] + '/edit', '/tasks/' + owned['task'] + '/state',
                 '/tasks/' + owned['task'] + '/delete', '/executions',
                 '/executions/' + owned['record'] + '/edit', '/review', '/logout', '/account/password']
        foreign_csrf = csrf(self.b)
        for path in paths:
            for value in ('', 'invalid', foreign_csrf, '잘못된값'):
                with self.subTest(path=path, token_kind='redacted'):
                    before = self.snapshot()
                    response = self.a.post(path, data=dict(csrf_token=value))
                    self.assertEqual(response.status_code, 403)
                    self.assertTrue(before == self.snapshot())
        public = self.app.test_client()
        csrf(public, '/login')
        for path in ('/signup', '/login'):
            self.assertEqual(public.post(path).status_code, 403)
        # A token in the URL never substitutes for a form/header token.
        self.assertEqual(self.a.post('/logout', query_string={'csrf_token': csrf(self.a)}).status_code, 403)

    def test_bidirectional_resource_access_denied_without_any_database_change(self):
        a = self.seed(self.a, 'private-A')
        b = self.seed(self.b, 'private-B')
        for client, other in ((self.a, b), (self.b, a)):
            p, t, r, i = (other[k] for k in ('plan', 'task', 'record', 'improvement'))
            requests = [
                ('GET', '/plans/' + p, {}), ('GET', '/plans/' + p + '/edit', {}),
                ('POST', '/plans/' + p + '/edit', dict(self.plan_data(), version=1)),
                ('GET', '/tasks?plan_id=' + p, {}), ('GET', '/tasks/new?plan_id=' + p, {}),
                ('POST', '/tasks/new', other['task_data']),
                ('GET', '/tasks/' + t + '/edit', {}), ('POST', '/tasks/' + t + '/edit', dict(other['task_data'], version=2)),
                ('GET', '/tasks/' + t + '/delete', {}), ('POST', '/tasks/' + t + '/delete', dict(confirm='yes', version=2)),
                ('POST', '/tasks/' + t + '/state', dict(version=1, target_status='completed', request_id=other['state_key'])),
                ('GET', '/executions?plan_id=' + p, {}), ('POST', '/executions', other['record_data']),
                ('GET', '/executions/' + r + '/edit', {}), ('POST', '/executions/' + r + '/edit', other['record_data']),
                ('GET', '/review?plan_id=' + p, {}), ('POST', '/review', dict(plan_id=p, improvement_content='forbidden')),
                ('GET', '/plans/new?source_improvement=' + i, {}), ('POST', '/plans/new', self.plan_data(source_improvement=i)),
                ('GET', '/plans/' + other['following'], {}),
            ]
            for method, path, data in requests:
                with self.subTest(method=method, path=path):
                    before = self.snapshot()
                    response = self.post(client, path, data) if method == 'POST' else client.get(path)
                    self.assertEqual(response.status_code, 404)
                    self.assertTrue(before == self.snapshot(), 'Rejected request changed database')

    def test_body_reference_swaps_and_identity_spoofing(self):
        a = self.seed(self.a, 'swap-A')
        b = self.seed(self.b, 'swap-B')
        for client, own, foreign in ((self.a, a, b), (self.b, b, a)):
            attempts = [('/executions/' + own['record'] + '/edit', dict(own['record_data'], task_id=foreign['task'])),
                        ('/executions/' + own['record'] + '/edit', dict(own['record_data'], task_id=' \t' + foreign['task'] + '\n')),
                        ('/tasks/new', dict(own['task_data'], plan_id=' ' + foreign['plan'] + ' ')),
                        ('/executions', dict(own['record_data'], task_id=foreign['task'], request_id=str(uuid4()))),
                        ('/tasks/' + own['task'] + '/edit', dict(own['task_data'], plan_id=foreign['plan'], version=2)),
                        ('/executions', dict(own['record_data'], request_id=foreign['record_data']['request_id']))]
            for path, data in attempts:
                before = self.snapshot()
                self.assertEqual(self.post(client, path, data).status_code, 404)
                self.assertTrue(before == self.snapshot())
        owner_b = next(r['owner_id'] for r in self.rows('plans') if r['id'] == b['plan'])
        token = csrf(self.a)
        response = self.a.post('/plans/new?user_id=' + owner_b, headers={'X-User-ID': owner_b},
                               data=self.plan_data(owner_id=owner_b, user_id=owner_b, csrf_token=token))
        self.assertEqual(response.status_code, 303)
        created = response.location.split('/')[-1]
        actual = next(r['owner_id'] for r in self.rows('plans') if r['id'] == created)
        owner_a = next(r['owner_id'] for r in self.rows('plans') if r['id'] == a['plan'])
        self.assertEqual(actual, owner_a)

    def test_lists_search_review_and_all_export_tables_are_isolated(self):
        a = self.seed(self.a, 'only-A-marker')
        b = self.seed(self.b, 'only-B-marker')
        for client, own, foreign, marker in ((self.a, a, b, 'only-B-marker'), (self.b, b, a, 'only-A-marker')):
            for path in ('/', '/tasks', '/executions', '/executions/' + own['record'] + '/edit', '/review'):
                response = client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(marker.encode() not in response.data)
                self.assertTrue(foreign['plan'].encode() not in response.data)
            contexts = []
            def capture(sender, template, context, **extra):
                contexts.append(context)
            with template_rendered.connected_to(capture, self.app):
                response = client.get('/tasks', query_string={'q': marker})
            self.assertEqual(response.status_code, 200)
            # The search input echoes its value; inspect result rows, not that input.
            self.assertEqual(contexts[-1]['tasks'], [])
            exported = client.get('/export.json?user_id=forged', headers={'X-User-ID': 'forged'})
            self.assertEqual(exported.status_code, 200)
            data = exported.json['data']
            self.assertEqual(set(data), set(EXPORT_FIELDS))
            self.assertEqual({p['id'] for p in data['plans']}, {own['plan'], own['following']})
            self.assertEqual({t['id'] for t in data['tasks']}, {own['task']})
            for table in ('task_tags', 'task_state_requests', 'task_completion_events', 'execution_records'):
                self.assertEqual({r['task_id'] for r in data[table]}, {own['task']})
            self.assertEqual({r['plan_id'] for r in data['plan_versions']}, {own['plan'], own['following']})
            self.assertEqual({r['id'] for r in data['review_improvements']}, {own['improvement']})
            self.assertEqual({r['next_plan_id'] for r in data['next_plan_links']}, {own['following']})
            report = next(r for r in exported.json['plan_reviews'] if r['plan_id'] == own['plan'])
            self.assertEqual(report['metrics']['planned'], 1)
            self.assertEqual(report['metrics']['actual'], '10.0')
            self.assertNotIn(marker.encode(), exported.data)
            self.assertNotIn(b'password_hash', exported.data)
            self.assertNotIn(b'token_hash', exported.data)

    def test_legacy_migration_backup_and_unassigned_records_stay_hidden(self):
        legacy = self.path.parent / (uuid4().hex + '.sqlite3')
        self.addCleanup(lambda: legacy.unlink(missing_ok=True))
        # The legacy DDL is derived locally; no external or real database is read.
        ddl = (ROOT / 'schema.sql').read_text(encoding='utf-8').replace('    owner_id TEXT REFERENCES users(id),\n', '')
        with closing(sqlite3.connect(legacy)) as db, db:
            db.executescript(ddl)
            db.execute("INSERT INTO plans VALUES ('legacy-plan', 'quarantined', '', '2026-09-01', '2026-09-30', 'medium', '', 0, '2026-09-01T00:00:00Z', '2026-09-01T00:00:00Z', 1)")
            original = db.execute('SELECT * FROM plans').fetchall()
        migrated = create_app(legacy)
        for backup in (legacy.parent / 'backups').glob(legacy.name + '.before-daily-review.*.sqlite3'):
            self.addCleanup(lambda p=backup: p.unlink(missing_ok=True))
        backups = list((legacy.parent / 'backups').glob(legacy.name + '.*.bak'))
        self.assertEqual(len(backups), 1)
        for backup in backups:
            self.addCleanup(lambda p=backup: p.unlink(missing_ok=True))
        with closing(sqlite3.connect(backups[0])) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(db.execute('SELECT * FROM plans').fetchall(), original)
            self.assertNotIn('owner_id', [r[1] for r in db.execute('PRAGMA table_info(plans)')])
        with closing(sqlite3.connect(legacy)) as db:
            self.assertIsNone(db.execute('SELECT owner_id FROM plans').fetchone()[0])
        create_app(legacy)
        self.assertEqual(len(list((legacy.parent / 'backups').glob(legacy.name + '.*.bak'))), 1)
        client = migrated.test_client()
        token = csrf(client, '/signup')
        self.assertEqual(client.post('/signup', data=dict(username='first_user', password=self.password, csrf_token=token)).status_code, 303)
        self.assertEqual(client.post('/login', data=dict(username='first_user', password=self.password, csrf_token=token)).status_code, 303)
        self.assertNotIn(b'quarantined', client.get('/').data)
        self.assertEqual(client.get('/plans/legacy-plan').status_code, 404)
        self.assertEqual(client.get('/export.json').json['data']['plans'], [])
