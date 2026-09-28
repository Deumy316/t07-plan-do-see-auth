from contextlib import closing
import sqlite3
import unittest
import test_auth
from auth import COOKIE
from auth_support import csrf


class AccountDeleteTests(unittest.TestCase):
    setUp = test_auth.AuthTests.setUp
    tearDown = test_auth.AuthTests.tearDown
    account = test_auth.AuthTests.account
    post = test_auth.AuthTests.post
    rows = test_auth.AuthTests.rows
    snapshot = test_auth.AuthTests.snapshot
    plan_data = test_auth.AuthTests.plan_data
    seed = test_auth.AuthTests.seed

    def populate(self):
        for client, marker in ((self.a, 'a'), (self.b, 'b')):
            data = self.seed(client, marker)
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute("INSERT INTO review_rule_changes VALUES (?, ?, 'before', 'after', 'reason', '2026-09-01', '2026-09-02', '2026-09-02T12:00:00Z', NULL, 1)", (marker, data['plan']))
                db.execute("INSERT INTO review_rule_evidence VALUES (?, '2026-09-01', ?, ?, '2026-09-01T00:00:00Z', '2026-09-01T00:10:00Z', '2026-09-01T00:10:00Z')", (marker, data['record'], data['task']))
                # Soft-deleted tasks must also be removed on account deletion.
                db.execute("UPDATE tasks SET deleted_at='2026-09-03T00:00:00Z' WHERE id=?", (data['task'],))

    def delete(self, **extra):
        return self.post(self.a, '/account/delete', dict(current_password=self.password, confirm_delete='yes', **extra))

    def test_success_all_tables_other_user_and_all_old_sessions(self):
        self.populate()
        second = self.app.test_client()
        token = csrf(second, '/login')
        self.assertEqual(second.post('/login', data=dict(username='account_a', password=self.password, csrf_token=token)).status_code, 303)
        old = [self.a.get_cookie(COOKIE).value, second.get_cookie(COOKIE).value]
        other_export = self.b.get('/export.json').json['data']
        before = self.snapshot()
        other = next(r['id'] for r in self.rows('users') if r['username'] == 'account_b')
        response = self.a.post('/account/delete?user_id='+other, headers={'X-User-ID': other}, data=dict(current_password=self.password, confirm_delete='yes', user_id=other, owner_id=other, csrf_token=csrf(self.a)))
        self.assertEqual(response.status_code, 303)
        self.assertIsNone(self.a.get_cookie(COOKIE))
        after = self.snapshot()
        # All fixture tables have one half owned by A and one by B (except sessions).
        for table in before:
            if table != 'auth_sessions':
                self.assertEqual(len(after[table]) * 2, len(before[table]), table)
                self.assertTrue(all(row in before[table] for row in after[table]), table)
        self.assertEqual([r['id'] for r in self.rows('users')], [other])
        self.assertTrue(all(r['user_id'] == other for r in self.rows('auth_sessions')))
        for value in old:
            replay = self.app.test_client(); replay.set_cookie(COOKIE, value)
            self.assertEqual(replay.get('/export.json').status_code, 303)
        self.assertEqual(self.b.get('/export.json').json['data'], other_export)
        self.assertTrue(after['users'] == [r for r in before['users'] if r[0] == other])
        self.assertTrue(after['auth_sessions'] == [r for r in before['auth_sessions'] if r[1] == other])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_wrong_password_and_missing_consent_no_changes(self):
        self.populate(); before = self.snapshot()
        for data in (dict(current_password='incorrect', confirm_delete='yes'), dict(current_password=self.password), dict(current_password=self.password, confirm_delete='no')):
            response = self.post(self.a, '/account/delete', data)
            self.assertEqual(response.status_code, 400)
            self.assertTrue(self.password.encode() not in response.data)
            self.assertTrue(before == self.snapshot())

    def test_csrf_rejection_and_anonymous_access(self):
        self.populate(); before = self.snapshot()
        for token in ('', 'invalid', csrf(self.b)):
            response = self.a.post('/account/delete', data=dict(current_password=self.password, confirm_delete='yes', csrf_token=token))
            self.assertEqual(response.status_code, 403)
            self.assertTrue(before == self.snapshot())
        client = self.app.test_client()
        self.assertEqual(client.get('/account/delete').status_code, 303)
        self.assertEqual(client.post('/account/delete').status_code, 303)
        self.assertTrue(before == self.snapshot())

    def test_get_is_read_only_and_explains_backups_and_export(self):
        before = self.snapshot()
        response = self.a.get('/account/delete')
        self.assertEqual(response.status_code, 200)
        for text in ('자동 삭제되지 않습니다', '복구할 수 없습니다', 'confirm_delete', '/export.json', 'current_password'):
            self.assertIn(text.encode(), response.data)
        self.assertTrue(before == self.snapshot())

    def test_failure_at_last_delete_rolls_back_every_table_and_sessions(self):
        self.populate()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TRIGGER fail_delete BEFORE DELETE ON users BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        before = self.snapshot()
        self.assertEqual(self.delete().status_code, 503)
        self.assertTrue(before == self.snapshot())
        self.assertEqual(self.a.get('/export.json').status_code, 200)
        self.assertEqual(self.b.get('/export.json').status_code, 200)
