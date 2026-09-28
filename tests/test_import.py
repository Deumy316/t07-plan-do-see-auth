from contextlib import closing
from copy import deepcopy
from hashlib import sha256
import json
import secrets
import sqlite3
import unittest
from uuid import uuid4

from werkzeug.security import generate_password_hash

from app import ROOT, create_app
from auth_support import csrf
from import_data import FIELDS, ImportRejected, import_export, verify_imported

TEST_PASSWORD = secrets.token_urlsafe(24)
TEST_HASH = generate_password_hash(TEST_PASSWORD, method='scrypt')


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.path = ROOT / 'instance/tests' / (uuid4().hex + '.sqlite3')
        self.source = self.path.with_suffix('.json')
        self.app = create_app(self.path)
        self.app.config['TESTING'] = True
        self.oliver, self.tester = str(uuid4()), str(uuid4())
        self.existing = str(uuid4())
        stamp = '2026-09-01T00:00:00.000000Z'
        self.p1, self.p2, self.t1, self.t2 = [str(uuid4()) for _ in range(4)]
        plan = dict(id=self.p1, title='import fixture', content='synthetic content', start_date='2026-09-01',
                    end_date='2026-09-30', priority='medium', success_criteria='fixture', estimated_minutes=10,
                    created_at=stamp, updated_at=stamp, version=2)
        plans = [plan, dict(plan, id=self.p2, title='next fixture', version=1)]
        versions = [dict({k: v for k, v in p.items() if k != 'id'}, plan_id=p['id']) for p in plans]
        versions.insert(0, dict(versions[0], version=1, content='older fixture'))
        task = dict(id=self.t1, plan_id=self.p1, title='fixture task', content='', due_date=None, priority='high',
                    estimated_minutes=10, status='completed', created_at=stamp, updated_at=stamp,
                    completed_at=stamp, deleted_at=None, version=2)
        request_id, improvement = str(uuid4()), str(uuid4())
        data = dict(plans=plans, plan_versions=versions,
                    tasks=[task, dict(task, id=self.t2, status='active', completed_at=None, deleted_at=stamp)],
                    task_tags=[dict(task_id=self.t1, tag='fixture')],
                    task_state_requests=[dict(request_id=request_id, task_id=self.t1, target_status='completed',
                                              expected_version=1, result_version=2, changed=1, created_at=stamp)],
                    task_completion_events=[dict(id=str(uuid4()), task_id=self.t1, request_id=request_id,
                                                 task_version=2, completed_at=stamp)],
                    execution_records=[dict(id=str(uuid4()), task_id=t, request_id=str(uuid4()), started_at=stamp,
                                            ended_at='2026-09-01T00:10:00.000000Z', actual_minutes=10.0,
                                            content='synthetic execution', blocked_reason='', created_at=stamp) for t in (self.t1, self.t2)],
                    review_improvements=[dict(id=improvement, plan_id=self.p1, content='synthetic improvement', created_at=stamp)],
                    next_plan_links=[dict(next_plan_id=self.p2, previous_plan_id=self.p1, improvement_id=improvement, created_at=stamp)])
        self.document = dict(schema_version='2', export_format_version='1', exported_at=stamp, aggregation_as_of=stamp,
                             timezone={}, time_units={}, review_rules={}, data=data, plan_reviews=[{'planned': 999999}])
        self.write_source(self.document)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executemany('INSERT INTO users VALUES (?, ?, ?, ?)', [(self.oliver, 'oliver', TEST_HASH, 0), (self.tester, 'tester2', TEST_HASH, 0)])
            existing = dict(plan, id=self.existing, owner_id=self.oliver, version=1, title='계정 보호 확인용 계획')
            db.execute('INSERT INTO plans (' + ','.join(existing) + ') VALUES (' + ','.join('?' for _ in existing) + ')', tuple(existing.values()))
            self.tester_plan = str(uuid4())
            existing.update(id=self.tester_plan, owner_id=self.tester, title='tester fixture')
            db.execute('INSERT INTO plans (' + ','.join(existing) + ') VALUES (' + ','.join('?' for _ in existing) + ')', tuple(existing.values()))

    def tearDown(self):
        self.path.unlink(missing_ok=True)
        self.source.unlink(missing_ok=True)
        for backup in (self.path.parent / 'backups').glob(self.path.stem + '-before-import-*.sqlite3'):
            backup.unlink()

    def write_source(self, document):
        self.source.write_text(json.dumps(document, ensure_ascii=False), encoding='utf-8')

    def run_import(self, **kwargs):
        options = dict(apply=True, expected_sha256=sha256(self.source.read_bytes()).hexdigest(),
                       expected_plan_title='계정 보호 확인용 계획', expected_other_user='tester2')
        options.update(kwargs)
        return import_export(self.source, self.path, self.oliver, 'oliver', **options)

    def snapshot(self):
        with closing(sqlite3.connect(self.path)) as db:
            tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            return {t: db.execute('SELECT * FROM ' + t + ' ORDER BY 1,2').fetchall() for t in tables}

    def assert_rejected_unchanged(self):
        before = self.snapshot()
        with self.assertRaises(ImportRejected):
            self.run_import()
        self.assertTrue(before == self.snapshot(), 'Rejected import changed the database')

    def test_dry_run_and_changed_digest_never_write(self):
        before = self.snapshot()
        report = self.run_import(apply=False)
        self.assertEqual(report['mode'], 'dry-run')
        self.assertTrue(before == self.snapshot())
        self.assertEqual(list((self.path.parent / 'backups').glob(self.path.stem + '-before-import-*')), [])
        with self.assertRaises(ImportRejected):
            self.run_import(expected_sha256='changed')
        with self.assertRaises(ImportRejected):
            self.run_import(expected_sha256=None)
        self.assertTrue(before == self.snapshot())

    def test_import_preserves_every_source_field_accounts_existing_data_and_backup(self):
        before = self.snapshot()
        source_before = self.source.read_bytes()
        result = self.run_import()
        after = self.snapshot()
        self.assertEqual(result['imported'], {t: len(rows) for t, rows in self.document['data'].items()})
        for table in FIELDS:
            self.assertEqual(result['after'][table], result['before'][table] + result['imported'][table])
        for table in before:
            self.assertTrue(all(row in after[table] for row in before[table]))
        self.assertTrue(before['users'] == after['users'])
        self.assertTrue(before['auth_sessions'] == after['auth_sessions'])
        self.assertTrue(self.source.read_bytes() == source_before)
        with closing(sqlite3.connect(self.path)) as db:
            verify_imported(db, self.document['data'], self.oliver)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
        with closing(sqlite3.connect(result['backup'])) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            for table in before:
                self.assertTrue(db.execute('SELECT * FROM ' + table + ' ORDER BY 1,2').fetchall() == before[table])

    def test_repeated_file_is_explicit_conflict_not_skip_or_overwrite(self):
        self.run_import()
        self.assert_rejected_unchanged()

    def test_invalid_shapes_types_dates_and_untrusted_owner_are_atomic(self):
        changes = [lambda d: d.update(schema_version='3'),
                   lambda d: d['data'].update(users=[]),
                   lambda d: d['data']['plans'][0].update(owner_id=self.tester),
                   lambda d: d['data']['plans'][0].pop('content'),
                   lambda d: d['data']['tasks'][0].update(estimated_minutes=True),
                   lambda d: d['data']['plans'][0].update(start_date='2026-02-30'),
                   lambda d: d['data']['execution_records'][0].update(created_at='not-a-date'),
                   lambda d: d['data']['execution_records'][0].update(actual_minutes=float('nan')),
                   lambda d: d['data']['task_tags'].append(deepcopy(d['data']['task_tags'][0]))]
        for change in changes:
            document = deepcopy(self.document)
            change(document)
            self.write_source(document)
            self.assert_rejected_unchanged()

    def test_missing_source_references_cannot_bind_to_existing_target(self):
        for table, field, value in [('tasks', 'plan_id', self.existing),
                                    ('execution_records', 'task_id', str(uuid4())),
                                    ('next_plan_links', 'improvement_id', str(uuid4())),
                                    ('task_completion_events', 'request_id', str(uuid4()))]:
            document = deepcopy(self.document)
            document['data'][table][0][field] = value
            self.write_source(document)
            self.assert_rejected_unchanged()

    def test_cross_relationship_and_version_snapshot_mismatch_rejected(self):
        document = deepcopy(self.document)
        document['data']['task_completion_events'][0]['task_id'] = self.t2
        self.write_source(document)
        self.assert_rejected_unchanged()
        document = deepcopy(self.document)
        document['data']['plan_versions'][1]['content'] = 'inconsistent snapshot'
        self.write_source(document)
        self.assert_rejected_unchanged()

    def test_existing_primary_key_conflict_rejects_whole_file(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('UPDATE plans SET id=? WHERE id=?', (self.p2, self.existing))
        self.assert_rejected_unchanged()

    def test_unique_request_id_conflict_rejects_before_any_insert(self):
        row = dict(self.document['data']['tasks'][0], id=str(uuid4()), plan_id=self.existing)
        record = dict(self.document['data']['execution_records'][0], id=str(uuid4()), task_id=row['id'])
        with closing(sqlite3.connect(self.path)) as db, db:
            for table, values in [('tasks', row), ('execution_records', record)]:
                db.execute('INSERT INTO ' + table + ' (' + ','.join(values) + ') VALUES (' + ','.join('?' for _ in values) + ')', tuple(values.values()))
        self.assert_rejected_unchanged()

    def test_late_target_failure_rolls_back_all_nine_tables(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("CREATE TRIGGER reject_last BEFORE INSERT ON next_plan_links BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        self.assert_rejected_unchanged()

    def test_missing_database_or_wrong_identity_never_creates_or_changes_database(self):
        missing = self.path.with_suffix('.missing.sqlite3')
        with self.assertRaises(ImportRejected):
            import_export(self.source, missing, self.oliver, 'oliver')
        self.assertFalse(missing.exists())
        before = self.snapshot()
        for user_id, name in ((self.tester, 'oliver'), (self.oliver, 'missing')):
            with self.assertRaises(ImportRejected):
                import_export(self.source, self.path, user_id, name)
        with self.assertRaises(ImportRejected):
            self.run_import(expected_plan_title='missing')
        self.assertTrue(before == self.snapshot())

    def test_duplicate_json_keys_rejected(self):
        self.source.write_text('{"schema_version":"2","schema_version":"2"}', encoding='utf-8')
        self.assert_rejected_unchanged()

    def test_imported_data_is_accessible_only_to_target_through_real_auth_routes(self):
        self.run_import()
        clients = []
        for name in ('oliver', 'tester2'):
            client = self.app.test_client()
            token = csrf(client, '/login')
            self.assertEqual(client.post('/login', data=dict(username=name, password=TEST_PASSWORD, csrf_token=token)).status_code, 303)
            clients.append(client)
        oliver, tester = clients
        for plan in (self.p1, self.p2):
            self.assertEqual(oliver.get('/plans/' + plan).status_code, 200)
            self.assertEqual(tester.get('/plans/' + plan).status_code, 404)
            self.assertTrue(plan.encode() not in tester.get('/').data)
        exported = oliver.get('/export.json').json['data']
        self.assertTrue({self.p1, self.p2} <= {p['id'] for p in exported['plans']})
        isolated = tester.get('/export.json').json['data']
        self.assertEqual({p['id'] for p in isolated['plans']}, {self.tester_plan})
        for table in FIELDS:
            if table != 'plans':
                self.assertEqual(isolated[table], [])
        # Derived snapshot summaries do not fabricate source records.
        self.assertEqual(len(exported['tasks']), 2)
        self.assertEqual(len(exported['execution_records']), 2)
